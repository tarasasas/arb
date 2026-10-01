"""Tiny rate-limited JSON client on http.client (no third-party dependencies).

GETs reuse one keep-alive connection per thread (a fresh TLS handshake costs ~150ms, a reused
connection ~50ms) and ask for gzip (market listings shrink 10-20x)."""

import gzip
import http.client
import io
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode

USER_AGENT = "kalshi-polymarket-arb-scanner/0.1"


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


def _proxy_for(host):
    """(host, port, extra CONNECT headers) of the HTTPS proxy to tunnel through, or None."""
    proxy = urllib.request.getproxies().get("https")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    p = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
    headers = {}
    if p.username:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + b64encode(cred.encode()).decode()
    return p.hostname, p.port or 80, headers


class RateLimitedClient:
    def __init__(self, base_url, rps, max_retries=6, timeout=30, signer=None):
        """signer: optional callable(method, url_path) -> extra headers, applied per attempt
        (signatures carry a timestamp, so every retry is re-signed)."""
        self.base_url = base_url.rstrip("/")
        u = urllib.parse.urlparse(self.base_url)
        self.host, self.port, self.base_path = u.hostname, u.port or 443, u.path
        self.min_interval = 1.0 / rps
        self.max_retries = max_retries
        self.timeout = timeout
        self.signer = signer
        self._cv = threading.Condition()
        self._next_slot = 0.0
        self._high_waiting = 0
        self._local = threading.local()
        self._ssl = ssl.create_default_context()
        self._proxy = _proxy_for(self.host)
        self.request_count = 0

    def set_rate(self, rps):
        self.min_interval = 1.0 / rps

    def _wait_turn(self, high=False):
        """Take the next request slot. High-priority callers (the near-arb re-check) always go
        before normal ones, so a long full sweep can't starve them of the shared rate budget."""
        with self._cv:
            if high:
                self._high_waiting += 1
            try:
                while True:
                    now = time.monotonic()
                    if not high and self._high_waiting:
                        self._cv.wait(0.05)
                        continue
                    if now >= self._next_slot:
                        self._next_slot = now + self.min_interval
                        self.request_count += 1
                        return
                    self._cv.wait(self._next_slot - now)
            finally:
                if high:
                    self._high_waiting -= 1
                    self._cv.notify_all()

    # ---- connections ------------------------------------------------------------------

    def _new_conn(self):
        if self._proxy:
            phost, pport, pheaders = self._proxy
            conn = http.client.HTTPSConnection(phost, pport, timeout=self.timeout, context=self._ssl)
            conn.set_tunnel(self.host, self.port, headers=pheaders)
        else:
            conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._ssl)
        return conn

    def _drop_conn(self):
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn:
            conn.close()

    def _send(self, method, path, body=None, headers=None, reuse=True):
        """One HTTP exchange -> (status, headers, decoded body text)."""
        conn = getattr(self._local, "conn", None) if reuse else None
        if conn is None:
            conn = self._new_conn()
            if reuse:
                self._local.conn = conn
        try:
            conn.request(method, self.base_path + path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
        except BaseException:
            if reuse:
                self._drop_conn()
            else:
                conn.close()
            raise
        if not reuse or resp.will_close:
            if reuse:
                self._drop_conn()
            else:
                conn.close()
        if resp.getheader("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return resp.status, resp.headers, raw.decode("utf-8", "replace")

    def _headers(self, method, path, extra=None):
        h = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip", **(extra or {})}
        if self.signer:
            h.update(self.signer(method, self.base_path + path.split("?", 1)[0]))
        return h

    # ---- requests ---------------------------------------------------------------------

    def post(self, path, body):
        """POST JSON. Orders are not idempotent, so the only retry is on 429 (the request
        was refused before reaching the exchange). Other failures raise ApiError at once.
        Each POST opens a fresh connection: a stale keep-alive socket could fail after the
        exchange already received the order, leaving its outcome unknown."""
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            self._wait_turn(high=True)
            headers = self._headers("POST", path, {"Content-Type": "application/json"})
            status, _, text = self._send("POST", path, data, headers, reuse=False)
            if status < 300:
                return json.loads(text) if text.strip() else {}
            if status == 429 and attempt < self.max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise ApiError(status, text[:500])

    def get(self, path, params=None, high=False):
        """GET base_url + path. `params` may be a dict or a list of (key, value) pairs
        (use the list form for repeated keys such as ?slug=a&slug=b). high=True jumps the
        rate-limit queue (time-critical re-checks)."""
        full = path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        for attempt in range(self.max_retries):
            self._wait_turn(high)
            reused = getattr(self._local, "conn", None) is not None
            try:
                status, hdrs, text = self._send("GET", full, headers=self._headers("GET", full))
            except (http.client.HTTPException, OSError) as e:
                # Timeouts, dropped connections, truncated bodies. A reused keep-alive socket the
                # server already closed fails on first use: retry that at once on a new one.
                if reused and attempt == 0:
                    continue
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise urllib.error.URLError(e) from e
            if status >= 300:
                if status in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise urllib.error.HTTPError(self.base_url + full, status, text[:200], hdrs, io.BytesIO(text.encode()))
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
