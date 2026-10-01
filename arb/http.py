"""Tiny rate-limited JSON client on http.client (no third-party dependencies).

Requests reuse keep-alive connections from a small pool (a fresh TLS handshake costs ~150ms, a
reused connection ~50ms) and ask for gzip (market listings shrink 10-20x)."""

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
GET_MAX_IDLE = 30.0     # reuse a pooled connection for a GET if it was last used this recently
POST_MAX_IDLE = 15.0    # ...and for an order only if it's this fresh (a socket the server closed could
                        # fail after the order was received, leaving its outcome unknown). Both
                        # exchanges were seen keeping idle connections open for 50s+.


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
        self._pool, self._pool_lock = [], threading.Lock()     # idle connections: [(conn, last_used)]
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

    def _take_conn(self, max_idle):
        """The most recently used pooled connection if it's fresh enough, else None."""
        now = time.monotonic()
        with self._pool_lock:
            while self._pool:
                conn, last = self._pool.pop()
                if now - last <= max_idle:
                    return conn
                conn.close()
        return None

    def _give_back(self, conn):
        with self._pool_lock:
            self._pool.append((conn, time.monotonic()))
            extra, self._pool[:] = self._pool[:-16], self._pool[-16:]
        for c, _ in extra:
            c.close()

    def _send(self, method, path, body=None, headers=None, max_idle=GET_MAX_IDLE):
        """One HTTP exchange -> (status, headers, decoded body text)."""
        conn = self._take_conn(max_idle)
        if conn is None:
            conn = self._new_conn()
        try:
            conn.request(method, self.base_path + path, body=body, headers=headers or {})
            resp = conn.getresponse()
            raw = resp.read()
        except BaseException:
            conn.close()
            raise
        if resp.will_close:
            conn.close()
        else:
            self._give_back(conn)
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
        Orders don't wait for the read-rate budget: exchanges meter writes separately."""
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            headers = self._headers("POST", path, {"Content-Type": "application/json"})
            status, _, text = self._send("POST", path, data, headers, max_idle=POST_MAX_IDLE)
            if status < 300:
                return json.loads(text) if text.strip() else {}
            if status == 429 and attempt < self.max_retries - 1:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise ApiError(status, text[:500])

    def warm(self):
        """Have a fresh connection ready so the next order skips the TLS handshake."""
        conn = self._take_conn(POST_MAX_IDLE)
        if conn is None:
            conn = self._new_conn()
            conn.connect()
        self._give_back(conn)

    def get(self, path, params=None, high=False):
        """GET base_url + path. `params` may be a dict or a list of (key, value) pairs
        (use the list form for repeated keys such as ?slug=a&slug=b). high=True jumps the
        rate-limit queue (time-critical re-checks)."""
        full = path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        for attempt in range(self.max_retries):
            self._wait_turn(high)
            try:
                status, hdrs, text = self._send("GET", full, headers=self._headers("GET", full))
            except (http.client.HTTPException, OSError) as e:
                # Timeouts, dropped connections, truncated bodies, or a pooled keep-alive socket
                # the server already closed. GETs are safe to repeat.
                if attempt < self.max_retries - 1:
                    time.sleep(0 if attempt == 0 else min(2 ** attempt, 20))
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
