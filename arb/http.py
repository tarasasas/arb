"""Tiny rate-limited JSON GET client built on urllib (no third-party dependencies)."""

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
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

USER_AGENT = "kalshi-polymarket-arb-scanner/0.1"


_seq = __import__("itertools").count()


def unique_token():
    """A value no other request carries (time + a counter: Windows clocks tick in ms). CDNs in front of
    both exchanges' public data cache replies by URL, so a query parameter with this always reaches them."""
    return f"{time.time_ns():x}{next(_seq):x}"


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


# ---- priority lane ---------------------------------------------------------------------------
# Price re-checks of near-arbs, stream re-checks and trades run in the priority lane: while any of
# them is waiting for a request slot, background work (market lists, full sweeps) holds back, so a
# restart or a catalog reload never delays the checks that find and take arbs.
_lane = threading.local()


def is_priority():
    return getattr(_lane, "priority", False)


@contextmanager
def priority():
    old = is_priority()
    _lane.priority = True
    try:
        yield
    finally:
        _lane.priority = old


# A trade that has been decided goes ahead of everything, the priority lane included: the fast lane alone
# can keep that lane's queue full (up to 15 book downloads every half second), and a trade's checks used to
# wait behind it for seconds while the prices it was about to trade on moved.
def is_trade():
    return getattr(_lane, "trade", False)


@contextmanager
def trading():
    old = (is_trade(), is_priority())
    _lane.trade = _lane.priority = True
    try:
        yield
    finally:
        _lane.trade, _lane.priority = old


class LanePool(ThreadPoolExecutor):
    """A thread pool whose workers keep the lane of the thread that handed them the work."""
    def submit(self, fn, *args, **kwargs):
        lane, trade = is_priority(), is_trade()

        def run(*a, **kw):
            _lane.priority, _lane.trade = lane, trade
            return fn(*a, **kw)
        return super().submit(run, *args, **kwargs)


class RateLimitedClient:
    def __init__(self, base_url, rps, max_retries=6, timeout=30, signer=None, burst=None):
        """signer: optional callable(method, url_path) -> extra headers, applied per attempt
        (signatures carry a timestamp, so every retry is re-signed)."""
        self.base_url = base_url.rstrip("/")
        self.base_path = urllib.parse.urlparse(self.base_url).path
        self.min_interval = 1.0 / rps
        # Token bucket (GCRA): priority requests may run up to `burst` requests ahead of the steady
        # pace, so a trade's checks go out at once instead of one slot apart; the average rate holds.
        self.burst = burst if burst is not None else max(1, int(rps * 0.4))
        self.max_retries = max_retries
        self.timeout = timeout
        self.signer = signer
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._priority_waiting = self._bg_waiting = self._priority_streak = self._trade_waiting = 0
        self.request_count = 0
        # Kept-alive connections: a new HTTPS connection costs a TCP + TLS handshake on every call.
        u = urllib.parse.urlparse(self.base_url)
        self._host, self._port, self._https = u.hostname, u.port or (443 if u.scheme == "https" else 80), u.scheme == "https"
        self._pool, self._pool_lock = [], threading.Lock()
        self._ssl = ssl.create_default_context() if self._https else None
        proxy = urllib.request.getproxies().get(u.scheme)
        bypass = urllib.request.proxy_bypass(self._host) if proxy else True
        self._proxy = None if bypass else urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)

    # ---- connections -----------------------------------------------------------------------

    GET_IDLE_MAX = 30.0     # reuse a connection idle up to this long for reads (retried if it went stale)
    POST_IDLE_MAX = 15.0    # orders only reuse a recently used connection: a retry could double an order
                            # (both exchanges were measured keeping idle connections open 50s+)
    POOL_MAX = 32

    def _new_conn(self):
        if self._proxy:
            conn = http.client.HTTPSConnection(self._proxy.hostname, self._proxy.port or 80, timeout=self.timeout,
                                               context=self._ssl) if self._https else \
                http.client.HTTPConnection(self._proxy.hostname, self._proxy.port or 80, timeout=self.timeout)
            headers = {}
            if self._proxy.username:
                import base64
                cred = f"{urllib.parse.unquote(self._proxy.username)}:{urllib.parse.unquote(self._proxy.password or '')}"
                headers["Proxy-Authorization"] = "Basic " + base64.b64encode(cred.encode()).decode()
            conn.set_tunnel(self._host, self._port, headers=headers)
            return conn
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=self.timeout, context=self._ssl)
        return http.client.HTTPConnection(self._host, self._port, timeout=self.timeout)

    def _take(self, max_idle):
        now = time.monotonic()
        with self._pool_lock:
            while self._pool:
                conn, used = self._pool.pop()              # most recently used first
                if now - used <= max_idle:
                    return conn, True
                conn.close()
        return self._new_conn(), False

    def _give(self, conn):
        with self._pool_lock:
            if len(self._pool) < self.POOL_MAX:
                self._pool.append((conn, time.monotonic()))
                return
        conn.close()

    def _send(self, method, url, body, headers, idempotent):
        """One HTTP exchange on a kept-alive connection. Returns (status, reason, headers, bytes).
        A reused connection that turns out stale is retried on a fresh one, except an order
        whose request may have reached the exchange."""
        target = urllib.parse.urlsplit(url)
        path = target.path + (f"?{target.query}" if target.query else "")
        for attempt in (0, 1):
            conn, reused = self._take(self.GET_IDLE_MAX if idempotent else self.POST_IDLE_MAX)
            try:
                conn.request(method, path, body=body, headers=headers)
            except (OSError, http.client.HTTPException):
                conn.close()                               # nothing reached the server
                if reused and attempt == 0:
                    continue
                raise
            try:
                resp = conn.getresponse()
                data = resp.read()
            except (OSError, http.client.HTTPException):
                conn.close()
                if reused and idempotent and attempt == 0:
                    continue
                raise
            if resp.will_close:
                conn.close()
            else:
                self._give(conn)
            return resp.status, resp.reason, resp.headers, data
        raise ConnectionError("unreachable")

    def warm(self):
        """Have a connection ready for the next order, so it skips the TCP + TLS handshake."""
        conn, reused = self._take(self.POST_IDLE_MAX)
        if not reused:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException):
                conn.close()
                return
        self._give(conn)

    def _http_error(self, url, status, reason, headers, data):
        return urllib.error.HTTPError(url, status, reason, headers, io.BytesIO(data))

    def set_rate(self, rps):
        self.min_interval = 1.0 / rps
        self.burst = max(1, int(rps * 0.4))

    PRIORITY_BURST = 1     # while background work waits, it gets every other slot (it must never starve)

    def _wait_turn(self, order=False):
        """Take the next request slot. Slots are claimed only when due (not reserved ahead), so a
        priority request waits about one slot however many background threads are queued; background
        work still gets one slot in every PRIORITY_BURST + 1 while it's waiting. Priority requests
        may also use the burst allowance; an order never waits (it still counts toward the pace). A trade's
        own reads (trading()) take the next slot their burst allowance gives, before any other waiter."""
        if order:
            with self._lock:
                self._next_slot = max(time.monotonic(), self._next_slot) + self.min_interval
            return
        trade, pri = is_trade(), is_priority()
        full_tau = (self.burst - 1) * self.min_interval
        with self._lock:
            if trade:
                self._trade_waiting += 1
            elif pri:
                self._priority_waiting += 1
            else:
                self._bg_waiting += 1
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    if trade:
                        if now >= self._next_slot - full_tau:
                            self._next_slot = max(now, self._next_slot) + self.min_interval
                            return
                        wait = self._next_slot - full_tau - now
                    elif self._trade_waiting:
                        wait = 0.005                      # a trade's read is waiting: it goes first
                    else:
                        bg_turn = self._bg_waiting and self._priority_streak >= self.PRIORITY_BURST
                        # Priority may burst; background shares that allowance only on its turns while
                        # priority is waiting (so the alternation holds), and otherwise keeps the plain pace.
                        tau = full_tau if pri or (bg_turn and self._priority_waiting) else 0.0
                        if now >= self._next_slot - tau:
                            if pri and not bg_turn:
                                self._priority_streak += 1 if self._bg_waiting else 0
                                self._next_slot = max(now, self._next_slot) + self.min_interval
                                return
                            if not pri and (bg_turn or not self._priority_waiting):
                                self._priority_streak = 0
                                self._next_slot = max(now, self._next_slot) + self.min_interval
                                return
                        wait = self._next_slot - tau - now
                time.sleep(min(max(wait, 0.002), self.min_interval))
        finally:
            with self._lock:
                if trade:
                    self._trade_waiting -= 1
                elif pri:
                    self._priority_waiting -= 1
                else:
                    self._bg_waiting -= 1

    def post(self, path, body):
        """POST JSON. Orders are not idempotent, so the only retry is on 429 (the request
        was refused before reaching the exchange). Other failures raise ApiError at once."""
        url = self.base_url + path
        data = json.dumps(body).encode() if body is not None else b""       # None: no body at all
        for attempt in range(self.max_retries):
            self._wait_turn(order=True)
            self.request_count += 1
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            if self.signer:
                headers.update(self.signer("POST", self.base_path + path))
            status, reason, hdrs, raw = self._send("POST", url, data, headers, idempotent=False)
            if status < 400:
                text = raw.decode("utf-8")
                return json.loads(text) if text.strip() else {}
            detail = raw.decode("utf-8", "replace")[:500]
            if status == 429 and attempt < self.max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise ApiError(status, detail) from self._http_error(url, status, reason, hdrs, raw)

    def get(self, path, params=None):
        """GET base_url + path. `params` may be a dict or a list of (key, value) pairs
        (use the list form for repeated keys such as ?slug=a&slug=b)."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(self.max_retries):
            self._wait_turn()
            self.request_count += 1
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}
            if self.signer:
                headers.update(self.signer("GET", self.base_path + path))
            try:
                status, reason, hdrs, raw = self._send("GET", url, None, headers, idempotent=True)
                if (hdrs.get("Content-Encoding") or "").lower() == "gzip":     # market lists shrink 10-20x
                    raw = gzip.decompress(raw)
                if status >= 400:
                    raise self._http_error(url, status, reason, hdrs, raw)
                return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError, EOFError,
                    zlib.error):
                # Timeouts, dropped connections, truncated or corrupt bodies (IncompleteRead, bad gzip).
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
