"""Live order books over WebSocket, so near-arbs are re-checked the moment a price moves.

Both streams need your API keys (Kalshi and Polymarket US authenticate the connection even for
public market data) and the `websocket-client` package. Without either, the scanner keeps polling.

Kalshi  wss://api.elections.kalshi.com/trade-api/ws/v2, channel orderbook_delta: one
        orderbook_snapshot per market, then orderbook_delta messages with a per-subscription seq.
Polymarket US  wss://api.polymarket.us/v1/ws/markets, SUBSCRIPTION_TYPE_MARKET_DATA: the top of
        the book on every change, at most 100 markets per subscription.

A stream writes each fresh book onto the scanner's market object (levels, yes_ask, no_ask) and calls
on_update(exchange, market_id); the scanner re-evaluates just the pairs that market is in.
"""

import itertools
import json
import threading
import time

from . import config

KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"
POLYMARKET_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
POLYMARKET_WS_PATH = "/v1/ws/markets"
PM_MAX_PER_SUB = 100
KALSHI_MAX_PER_CMD = 500


def available():
    try:
        import websocket  # noqa: F401  (package websocket-client)
        return True
    except ImportError:
        return False


def _f(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError, AttributeError):
        return None


# ---- book math (pure, tested) ------------------------------------------------------------------

def kalshi_levels(bids):
    """bids: {"yes": {price: qty}, "no": {price: qty}} (Kalshi books are bids only).
    Buying YES lifts NO bids at 1 - price, and vice versa. Returns buy levels, best first."""
    def asks(opposite):
        return sorted(((round(1 - p, 4), q) for p, q in opposite.items() if q > 1e-9), key=lambda t: t[0])
    return {"yes": asks(bids["no"]), "no": asks(bids["yes"])}


def polymarket_levels(md):
    """A MARKET_DATA message body -> buy levels. Buy YES lifts offers; Buy NO sells YES into the
    bids and costs 1 - bid."""
    bids = [(_f(l.get("px")), _f(l.get("qty"))) for l in md.get("bids") or []]
    offers = [(_f(l.get("px")), _f(l.get("qty"))) for l in md.get("offers") or []]
    bids = sorted(((p, q) for p, q in bids if p is not None and q), key=lambda t: -t[0])
    offers = sorted(((p, q) for p, q in offers if p is not None and q), key=lambda t: t[0])
    return {"yes": offers, "no": [(round(1 - p, 4), q) for p, q in bids]}


def apply_levels(market, levels, tradable=True):
    """Write a fresh book onto a scanner market object; an untradable market gets no prices."""
    if not tradable:
        levels = {"yes": [], "no": []}
    market.levels = levels
    market.yes_ask = levels["yes"][0][0] if levels["yes"] else None
    market.no_ask = levels["no"][0][0] if levels["no"] else None
    market.quoted_at = time.time()          # a poll sent before this can't overwrite it


# ---- connection handling ----------------------------------------------------------------------

class _Stream(threading.Thread):
    """One WebSocket connection with reconnect and backoff. Subclasses build subscribe messages
    and handle incoming ones."""
    exchange = ""

    def __init__(self, url, sign_path, signer, markets, on_update, log, connect=None):
        super().__init__(daemon=True, name=f"{self.exchange}-stream")
        self.url, self.sign_path, self.signer = url, sign_path, signer
        self.markets = markets              # market_id -> scanner market object (shared)
        self.on_update, self.log = on_update, log
        self._connect = connect             # injectable for tests: connect(url, headers) -> ws
        self.wanted, self.subscribed = set(), set()
        self.first_seen = {}                        # market -> time of its first message (the snapshot)
        self.tops, self.top_changed_at = {}, {}     # market -> best prices, and when they last changed
        self.lock, self.stop_event = threading.Lock(), threading.Event()
        self.ws, self.connected, self.last_msg, self.updates = None, False, 0.0, 0
        self.seen = set()                   # markets with a live book since the last (re)connect
        self.updated_at = {}                # market id -> time of its last streamed book
        self.waiters = {}                   # market id -> Event set on its next book (a trade waiting on it)
        self.reconnects = 0
        self.error = None

    # public
    def fresh(self, max_age):
        """Markets whose streamed book is recent enough to trust without polling."""
        if not self.connected:
            return set()
        cutoff = time.time() - max_age
        return {mid for mid, t in list(self.updated_at.items()) if t >= cutoff}

    def want(self, ids):
        """Set the markets to stream; new ones are subscribed on the live connection. Subscriptions
        only add up on a connection, so once most of them are no longer wanted, reconnect and
        subscribe the current set from scratch."""
        with self.lock:
            self.wanted = set(ids)
            stale = len(self.subscribed - self.wanted)
        ws = self.ws
        if ws and self.connected and stale > max(200, len(self.wanted)):
            self.log(f"{self.exchange} stream: {stale} old subscriptions, reconnecting with the current list")
            self._drop(ws)
            return
        if ws and self.connected:
            try:
                self._sync_subscriptions(ws)
            except Exception as e:
                self.error = repr(e)

    def status(self):
        age = time.time() - self.last_msg if self.last_msg else None
        return {"connected": self.connected, "markets": len(self.subscribed), "live": len(self.seen),
                "fresh": len(self.fresh(config.STREAM_FRESH_SECS)), "updates": self.updates,
                "reconnects": self.reconnects,
                "last_message_secs": round(age, 1) if age is not None else None, "error": self.error}

    def _drop(self, ws):
        try:
            ws.close()
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()
        try:
            self.ws and self.ws.close()
        except Exception:
            pass

    # loop
    def run(self):
        backoff = 1
        while not self.stop_event.is_set():
            try:
                self.ws = self._open()
                self.connected, self.error, backoff = True, None, 1
                self.subscribed, self.seen, self.updated_at, self.first_seen = set(), set(), {}, {}
                self.tops, self.top_changed_at = {}, {}
                self.last_msg = time.time()
                self._on_connect()
                self._sync_subscriptions(self.ws)
                while not self.stop_event.is_set():
                    try:
                        raw = self.ws.recv()
                    except Exception as e:
                        if type(e).__name__ in ("WebSocketTimeoutException", "TimeoutError", "timeout"):
                            quiet = getattr(self, "quiet_secs", None) or config.STREAM_QUIET_SECS
                            raw = None if time.time() - self.last_msg > quiet else "{}"
                        else:
                            raise
                    if raw is None or raw == "":
                        raise ConnectionError("stream closed or went quiet")
                    if raw == "{}":
                        continue
                    self.last_msg = time.time()
                    self._handle(json.loads(raw))
            except Exception as e:
                if self.stop_event.is_set():
                    break
                self.error = repr(e)
                self.log(f"{self.exchange} stream: {e!r}; reconnecting in {backoff}s")
            finally:
                self.connected, self.seen, self.updated_at = False, set(), {}
                self.reconnects += 1
                try:
                    self.ws and self.ws.close()
                except Exception:
                    pass
            self.stop_event.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _open(self):
        headers = self.signer("GET", self.sign_path)
        if self._connect:
            return self._connect(self.url, headers)
        import websocket
        return websocket.create_connection(self.url, header=[f"{k}: {v}" for k, v in headers.items()],
                                           timeout=30, enable_multithread=True)

    def _updated(self, market_id, top=None):
        """top: the market's best prices after this message; a change of them (not of a deeper level) is
        recorded in top_changed_at, the stale-side signal Auto-trade uses."""
        self.updates += 1
        now = time.time()
        if market_id not in self.seen:
            self.first_seen[market_id] = now        # the snapshot after subscribing: not a price move
        self.seen.add(market_id)
        self.updated_at[market_id] = now
        if top is not None and self.tops.get(market_id) != top:
            self.tops[market_id] = top
            self.top_changed_at[market_id] = now
        ev = self.waiters.get(market_id)
        if ev is not None:
            ev.set()
        self.on_update(self.exchange, market_id)

    def _on_connect(self):
        pass


class KalshiStream(_Stream):
    exchange = "kalshi"

    def __init__(self, signer, markets, on_update, log, connect=None, url=KALSHI_WS_URL):
        super().__init__(url, KALSHI_WS_PATH, signer, markets, on_update, log, connect)
        self.books, self.seq, self.sid, self.ids = {}, {}, None, itertools.count(1)

    def _on_connect(self):
        self.books, self.seq, self.sid = {}, {}, None

    def _sync_subscriptions(self, ws):
        with self.lock:
            add = sorted(self.wanted - self.subscribed)
        for i in range(0, len(add), KALSHI_MAX_PER_CMD):
            chunk = add[i:i + KALSHI_MAX_PER_CMD]
            if self.sid is None:
                msg = {"id": next(self.ids), "cmd": "subscribe",
                       "params": {"channels": ["orderbook_delta"], "market_tickers": chunk}}
            else:
                msg = {"id": next(self.ids), "cmd": "update_subscription",
                       "params": {"sid": self.sid, "market_tickers": chunk, "action": "add_markets"}}
            ws.send(json.dumps(msg))
            self.subscribed.update(chunk)

    def _handle(self, d):
        kind, msg = d.get("type"), d.get("msg") or {}
        # Every message in a subscription is numbered, including the "ok" reply to adding markets,
        # so count them all: a gap then really means a missed update and the books must be resynced.
        sid, seq = d.get("sid"), d.get("seq")
        if sid is not None and seq is not None:
            last = self.seq.get(sid)
            if last is not None and seq != last + 1:
                raise ConnectionError(f"missed Kalshi messages (seq {last} -> {seq}, after {kind}); resyncing")
            self.seq[sid] = seq
        if kind == "subscribed":
            self.sid = msg.get("sid", self.sid)
            return
        if kind == "error":
            self.error = f"Kalshi error {msg.get('code')}: {msg.get('msg')}"
            return
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return
        ticker = msg.get("market_ticker")
        if not ticker:
            return
        if kind == "orderbook_snapshot":
            self.books[ticker] = {side: {float(p): float(q) for p, q in msg.get(f"{side}_dollars_fp") or []}
                                  for side in ("yes", "no")}
        else:
            book = self.books.get(ticker)
            if book is None:
                return                        # delta before its snapshot: wait for the snapshot
            side, price = msg.get("side"), float(msg["price_dollars"])
            book[side][price] = book[side].get(price, 0.0) + float(msg["delta_fp"])
            if book[side][price] <= 1e-9:
                del book[side][price]
        m = self.markets.get(ticker)
        if m is not None:
            apply_levels(m, kalshi_levels(self.books[ticker]))
            self._updated(ticker, (m.yes_ask, m.no_ask))


class PolymarketStream(_Stream):
    exchange = "polymarket"

    def __init__(self, signer, markets, on_update, log, connect=None, url=POLYMARKET_WS_URL):
        super().__init__(url, POLYMARKET_WS_PATH, signer, markets, on_update, log, connect)
        self.ids = itertools.count(1)

    def _sync_subscriptions(self, ws):
        with self.lock:
            add = sorted(self.wanted - self.subscribed)
        for i in range(0, len(add), PM_MAX_PER_SUB):
            chunk = add[i:i + PM_MAX_PER_SUB]
            ws.send(json.dumps({"subscribe": {"requestId": f"md-{next(self.ids)}",
                                              "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                              "marketSlugs": chunk}}))
            self.subscribed.update(chunk)

    def _handle(self, d):
        if "heartbeat" in d:
            return
        if d.get("error"):
            self.error = f"Polymarket error: {d['error']}"
            return
        md = d.get("marketData") or d.get("market_data")
        if not md:
            return
        slug = md.get("marketSlug") or md.get("market_slug")
        m = self.markets.get(slug)
        if m is None:
            return
        state = md.get("state")
        apply_levels(m, polymarket_levels(md), tradable=state in (None, "MARKET_STATE_OPEN"))
        m.state = state
        self._updated(slug, (m.yes_ask, m.no_ask))


POLYMARKET_PRIVATE_WS_URL = "wss://api.polymarket.us/v1/ws/private"
POLYMARKET_PRIVATE_WS_PATH = "/v1/ws/private"


class PolymarketPrivateStream(_Stream):
    """Your own Polymarket orders and buying power, pushed (wss://api.polymarket.us/v1/ws/private), so
    the app stops polling them, as Polymarket's rate-limit guide asks.

    Orders: every snapshot or execution for an order bumps that order's counter and wakes anyone waiting
    on it; the order itself is then read once over REST (GET /v1/order/{id}), whose fields (filled
    quantity, average price, fees) the fill accounting already uses. Buying power: taken from the
    balance snapshot and every balance change; a change without it clears it, so the app downloads it."""
    exchange = "polymarket-private"
    quiet_secs = 300                        # no orders for a while is normal: reconnect only after 5 quiet minutes

    def __init__(self, signer, log, connect=None, url=POLYMARKET_PRIVATE_WS_URL):
        super().__init__(url, POLYMARKET_PRIVATE_WS_PATH, signer, {}, lambda *a: None, log, connect)
        self.cond = threading.Condition()
        self.versions = {}                  # order id -> updates seen
        self.first_at = {}                  # order id -> when this stream first mentioned it
        self.buying_power, self.balance_at = None, 0.0
        self._announced = False

    def _on_connect(self):
        self.buying_power, self._subscribed = None, False

    def _sync_subscriptions(self, ws):
        if getattr(self, "_subscribed", False):
            return
        ws.send(json.dumps({"subscribe": {"requestId": "orders", "subscriptionType": "SUBSCRIPTION_TYPE_ORDER",
                                          "marketSlugs": []}}))          # empty: every market
        ws.send(json.dumps({"subscribe": {"requestId": "balance",
                                          "subscriptionType": "SUBSCRIPTION_TYPE_ACCOUNT_BALANCE"}}))
        self._subscribed = True

    def _note(self, order_id):
        if not order_id:
            return
        with self.cond:
            if order_id not in self.versions:
                self.first_at[order_id] = time.time()
            self.versions[order_id] = self.versions.get(order_id, 0) + 1
            if len(self.versions) > 5000:   # forget the oldest orders
                for k in list(self.versions)[:1000]:
                    del self.versions[k]
                    self.first_at.pop(k, None)
            self.cond.notify_all()

    def orders_since(self, t):
        """Ids of orders this stream first mentioned at or after time t (oldest first)."""
        with self.cond:
            return [oid for oid, at in self.first_at.items() if at >= t]

    def version(self, order_id):
        with self.cond:
            return self.versions.get(order_id, 0)

    def wait(self, order_id, seen, timeout):
        """Wait up to `timeout` for an update to order_id newer than `seen`. Returns its update count."""
        end = time.monotonic() + timeout
        with self.cond:
            while self.versions.get(order_id, 0) <= seen:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cond.wait(left)
            return self.versions.get(order_id, 0)

    @staticmethod
    def _usd(balances):
        return next((b for b in balances or [] if b.get("currency") in (None, "", "USD")), None)

    def _handle(self, d):
        if "heartbeat" in d:
            return
        if d.get("error"):
            self.error = f"Polymarket private stream: {d['error']}"
            return
        snap = d.get("orderSubscriptionSnapshot")
        if snap:
            for o in snap.get("orders") or []:
                self._note(o.get("id"))
            if not self._announced:
                self._announced = True
                self.log("Polymarket order stream on: fills and buying power now arrive live instead of being polled")
        upd = d.get("orderSubscriptionUpdate")
        if upd:
            ex = upd.get("execution") or {}
            self._note((ex.get("order") or {}).get("id") or ex.get("orderId"))
        bsnap = d.get("accountBalancesSnapshot")
        if bsnap:
            usd = self._usd(bsnap.get("balances"))
            self._set_power((usd or {}).get("buyingPower"))
        bupd = d.get("accountBalancesUpdate")
        if bupd:
            after = (bupd.get("balanceChange") or {}).get("afterBalance") or {}
            self._set_power(after.get("buyingPower") if after.get("currency") in (None, "", "USD") else None)
        self.updates += 1

    def _set_power(self, v):
        try:
            self.buying_power, self.balance_at = float(v.get("value") if isinstance(v, dict) else v), time.time()
        except (TypeError, ValueError, AttributeError):
            self.buying_power = None        # unreadable: the app downloads it instead


def build(kalshi_client, on_update, log, kalshi_markets, pm_markets):
    """Start whichever streams the keys and packages allow. Returns {exchange: stream}."""
    if not available():
        log("Live streams off: run  python -m pip install websocket-client  (start-dashboard.bat does it)")
        return {}
    out = {}
    if kalshi_client.http.signer:
        out["kalshi"] = KalshiStream(kalshi_client.http.signer, kalshi_markets, on_update, log)
    else:
        log("Kalshi live stream off: it needs your Kalshi API key")
    if config.POLYMARKET_KEY_ID and config.POLYMARKET_SECRET_KEY:
        try:
            from .polymarket_auth import load_signer
            signer = load_signer(config.POLYMARKET_KEY_ID, config.POLYMARKET_SECRET_KEY)
            out["polymarket"] = PolymarketStream(signer, pm_markets, on_update, log)
        except Exception as e:
            log(f"Polymarket live stream off: {e!r}")
    else:
        log("Polymarket live stream off: it needs your Polymarket API key")
    for s in out.values():
        s.start()
    return out
