"""Tiny rate-limited JSON GET client built on urllib (no third-party dependencies)."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

USER_AGENT = "kalshi-polymarket-arb-scanner/0.1"


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


class LanePool(ThreadPoolExecutor):
    """A thread pool whose workers keep the lane of the thread that handed them the work."""
    def submit(self, fn, *args, **kwargs):
        lane = is_priority()

        def run(*a, **kw):
            _lane.priority = lane
            return fn(*a, **kw)
        return super().submit(run, *args, **kwargs)


class RateLimitedClient:
    def __init__(self, base_url, rps, max_retries=6, timeout=30, signer=None):
        """signer: optional callable(method, url_path) -> extra headers, applied per attempt
        (signatures carry a timestamp, so every retry is re-signed)."""
        self.base_url = base_url.rstrip("/")
        self.base_path = urllib.parse.urlparse(self.base_url).path
        self.min_interval = 1.0 / rps
        self.max_retries = max_retries
        self.timeout = timeout
        self.signer = signer
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._priority_waiting = 0
        self.request_count = 0

    def set_rate(self, rps):
        self.min_interval = 1.0 / rps

    def _wait_turn(self):
        """Take the next request slot. Slots are claimed only when due (not reserved ahead), so a
        priority request waits at most about one slot, however many background threads are queued."""
        pri = is_priority()
        if pri:
            with self._lock:
                self._priority_waiting += 1
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    if now >= self._next_slot and (pri or not self._priority_waiting):
                        self._next_slot = max(now, self._next_slot) + self.min_interval
                        return
                    wait = self._next_slot - now
                time.sleep(min(max(wait, 0.002), self.min_interval))
        finally:
            if pri:
                with self._lock:
                    self._priority_waiting -= 1

    def post(self, path, body):
        """POST JSON. Orders are not idempotent, so the only retry is on 429 (the request
        was refused before reaching the exchange). Other failures raise ApiError at once."""
        url = self.base_url + path
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            self._wait_turn()
            self.request_count += 1
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Content-Type": "application/json"}
            if self.signer:
                headers.update(self.signer("POST", self.base_path + path))
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:500]
                if e.code == 429 and attempt < self.max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise ApiError(e.code, detail) from e

    def get(self, path, params=None):
        """GET base_url + path. `params` may be a dict or a list of (key, value) pairs
        (use the list form for repeated keys such as ?slug=a&slug=b)."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(self.max_retries):
            self._wait_turn()
            self.request_count += 1
            headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
            if self.signer:
                headers.update(self.signer("GET", self.base_path + path))
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
            except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError):
                # Timeouts, dropped connections, truncated bodies (IncompleteRead).
                if attempt < self.max_retries - 1:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
