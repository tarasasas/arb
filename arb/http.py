"""Tiny rate-limited JSON GET client built on urllib (no third-party dependencies)."""

import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "kalshi-polymarket-arb-scanner/0.1"


class ApiError(Exception):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status, self.detail = status, detail


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
        self.request_count = 0

    def set_rate(self, rps):
        self.min_interval = 1.0 / rps

    def _wait_turn(self, priority=False):
        with self._lock:
            now = time.monotonic()
            if priority:
                # Trades go now, ahead of the scanner's queued reads, and push that queue back one
                # slot so the average rate stays within budget.
                self._next_slot = max(now, self._next_slot) + self.min_interval
                return
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def post(self, path, body, priority=False):
        """POST JSON. Orders are not idempotent, so the only retry is on 429 (the request
        was refused before reaching the exchange). Other failures raise ApiError at once."""
        url = self.base_url + path
        data = json.dumps(body).encode()
        for attempt in range(self.max_retries):
            self._wait_turn(priority)
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

    def get(self, path, params=None, priority=False):
        """GET base_url + path. `params` may be a dict or a list of (key, value) pairs
        (use the list form for repeated keys such as ?slug=a&slug=b). priority=True skips the queue."""
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(self.max_retries):
            self._wait_turn(priority)
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
