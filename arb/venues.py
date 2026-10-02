"""Order placement on each exchange behind one interface.

All prices here are in "cost space": what one share of the side you're trading costs
(or pays), from $0 to $1. Each venue translates to its own convention:
  - Kalshi V2 orders quote the YES book only: buy YES = bid at p; buy NO at c = ask at 1 - c.
  - Polymarket US price.value is always the YES (long) price: buy NO at c = BUY_SHORT at 1 - c.
"""

import math
import time
import uuid
from dataclasses import dataclass, field

from . import config
from .http import ApiError, RateLimitedClient, priority
from .model import fee_per_contract


@dataclass
class Fill:
    qty: float = 0.0          # shares filled
    amount: float = 0.0       # dollars paid (buy) or received (sell), fees excluded
    fee: float = 0.0
    order_id: str = ""
    request: dict = field(default_factory=dict)
    response: dict = field(default_factory=dict)

    @property
    def avg(self):
        return self.amount / self.qty if self.qty else 0.0


def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def _per_share(avg, limit, buying):
    """Exchanges report an average price; which side it's quoted in isn't always explicit.
    Pick the reading consistent with the limit, conservatively (higher cost / lower proceeds)."""
    cands = [avg, 1 - avg]
    if buying:
        ok = [c for c in cands if c <= limit + 1e-6]
        return max(ok) if ok else limit
    ok = [c for c in cands if c >= limit - 1e-6]
    return min(ok) if ok else limit


class KalshiVenue:
    name = "kalshi"

    def __init__(self, kalshi_client):
        self.client = kalshi_client          # signed RateLimitedClient lives at client.http

    def levels(self, ticker):
        return self.client.live_levels(ticker)

    def market_info(self, ticker):
        m = self.client.http.get(f"/markets/{ticker}")["market"]
        ranges = [(float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or []]

        def tick(price):
            for start, end, step in ranges:
                if start - 1e-9 <= price <= end + 1e-9:
                    return step
            return 0.01

        return {"open": m.get("status") == "active", "tick": tick,
                "min_qty": 0.01 if m.get("fractional_trading_enabled") else 1.0,
                "shard": int(m.get("exchange_index") or 0)}

    def balance(self, shard=None):
        """Cash available for orders: on one exchange shard if given (orders can only use cash on
        their market's shard), else in total."""
        params = {"exchange_index": shard} if shard is not None else None
        return float(self.client.http.get("/portfolio/balance", params)["balance"]) / 100

    def stop_kalshi_rebalancing(self):
        """Turn off Kalshi's own automatic rebalancing between shards, so cash only moves when a trade
        needs it (fund_shard). Returns the allocation that was turned off ([] if it was already off)."""
        cur = self.client.http.get("/portfolio/target_balance_allocation").get("allocations") or []
        if cur:
            self.client.http.post("/portfolio/target_balance_allocation", {"allocations": []})
        return cur

    def shard_balances(self):
        d = self.client.http.get("/portfolio/balance")
        return {int(b.get("exchange_index", 0)): float(b.get("balance") or 0) for b in d.get("balance_breakdown") or []}

    def transfer(self, source_shard, dest_shard, dollars):
        """Move cash between your own exchange shards (POST /portfolio/intra_exchange_instance_transfer;
        the amount is in centicents). Kalshi processes it asynchronously."""
        body = {"source": "event_contract", "destination": "event_contract", "amount": int(round(dollars * 10_000)),
                "source_exchange_shard": source_shard, "destination_exchange_shard": dest_shard}
        return self.client.http.post("/portfolio/intra_exchange_instance_transfer", body)

    def fund_shard(self, shard, dollars, wait=None, sleep=time.sleep):
        """Move up to `dollars` onto `shard` from your other shards (richest first), then wait for it
        to arrive. Returns ([(from shard, amount)], cash now on `shard`)."""
        have = self.shard_balances()
        start, moves, left = have.get(shard, 0.0), [], dollars
        for src, bal in sorted(((i, b) for i, b in have.items() if i != shard), key=lambda t: -t[1]):
            amount = math.floor(min(left, bal) * 100) / 100
            if left < 0.01:
                break
            if amount < 0.01:
                continue
            self.transfer(src, shard, amount)
            moves.append((src, amount))
            left -= amount
        now = start
        if moves:
            target = start + sum(a for _, a in moves) - 0.01
            deadline = time.time() + (config.SHARD_TRANSFER_WAIT_SECS if wait is None else wait)
            while True:
                now = self.balance(shard)
                if now >= target or time.time() >= deadline:
                    break
                sleep(0.5)
        return moves, now

    def warm(self):
        self.client.http.warm()

    def _order(self, ticker, book_side, qty, yes_price, reduce_only=False):
        body = {"ticker": ticker, "side": book_side, "count": f"{qty:.2f}", "price": f"{yes_price:.4f}",
                "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4())}
        if reduce_only:
            body["reduce_only"] = True
        sent = time.time()
        try:
            return body, self.client.http.post("/portfolio/events/orders", body)
        except ApiError:
            raise                       # Kalshi answered: refused, nothing traded
        except Exception:
            # The answer was lost (timeout, dropped connection), so the order may or may not exist.
            # Find it by our client_order_id before calling its outcome unknown.
            r = self._recover(ticker, body["client_order_id"], sent)
            if r is None:
                raise
            return body, r

    def _recover(self, ticker, client_order_id, sent):
        """A lost order's final result in the create-order response format (GET /portfolio/orders),
        or None if it can't be found."""
        for wait in (0.3, 0.7, 1.5):
            time.sleep(wait)
            try:
                with priority():
                    d = self.client.http.get("/portfolio/orders", {"ticker": ticker, "min_ts": int(sent) - 60,
                                                                   "limit": 100})
            except Exception:
                continue
            o = next((o for o in d.get("orders") or [] if o.get("client_order_id") == client_order_id), None)
            if not o or o.get("status") not in ("canceled", "executed"):    # IOC: final once it's either
                continue
            n = float(o.get("fill_count_fp") or 0)
            r = {"order_id": o.get("order_id", ""), "fill_count": str(n), "recovered": True}
            if n > 0:
                cost = sum(float(o.get(k) or 0) for k in ("taker_fill_cost_dollars", "maker_fill_cost_dollars"))
                r["average_fill_price"] = str(cost / n)
                fee_keys = [k for k in ("taker_fees_dollars", "maker_fees_dollars") if o.get(k) is not None]
                if fee_keys:
                    r["average_fee_paid"] = str(sum(float(o[k]) for k in fee_keys) / n)
            return r
        return None

    def buy(self, ticker, side, qty, limit, fee_coef):
        # buy YES at <= limit: bid at limit.  buy NO at <= limit: sell YES (ask) at >= 1 - limit.
        body, r = self._order(ticker, "bid" if side == "yes" else "ask", qty,
                              limit if side == "yes" else 1 - limit)
        n = float(r.get("fill_count") or 0)
        f = Fill(qty=n, order_id=r.get("order_id", ""), request=body, response=r)
        if n > 0:
            f.amount = n * _per_share(float(r.get("average_fill_price") or limit), limit, True)
            fee = r.get("average_fee_paid")
            f.fee = float(fee) * n if fee is not None else math.ceil(fee_per_contract(fee_coef, f.avg) * n * 100) / 100
        return f

    def sell(self, ticker, side, qty, min_price, fee_coef):
        # sell YES at >= min: ask at min.  sell NO at >= min: buy YES (bid) at <= 1 - min.
        body, r = self._order(ticker, "ask" if side == "yes" else "bid", qty,
                              min_price if side == "yes" else 1 - min_price, reduce_only=True)
        n = float(r.get("fill_count") or 0)
        f = Fill(qty=n, order_id=r.get("order_id", ""), request=body, response=r)
        if n > 0:
            f.amount = n * _per_share(float(r.get("average_fill_price") or min_price), min_price, False)
            fee = r.get("average_fee_paid")
            f.fee = float(fee) * n if fee is not None else math.ceil(fee_per_contract(fee_coef, f.avg) * n * 100) / 100
        return f


class PolymarketVenue:
    name = "polymarket"

    def __init__(self, public_client, signer):
        self.public = public_client          # gateway (market data)
        self.http = RateLimitedClient(config.POLYMARKET_TRADE_BASE, 8.0, signer=signer)
        self.private = None                  # streams.PolymarketPrivateStream once started
        self._seen = {}                      # order id -> stream updates already acted on

    def streaming(self):
        s = self.private
        return bool(s and s.connected)

    def stream_buying_power(self):
        """Buying power pushed by the private stream, or None (not connected, or not known yet)."""
        s = self.private
        return s.buying_power if s and s.connected else None

    def wait_order(self, oid, timeout):
        """With the private stream: wait up to `timeout` for news about this order. True if some came,
        False if not; None without the stream (the caller paces itself)."""
        if not self.streaming():
            return None
        seen = self._seen.get(oid, 0)
        now = self.private.wait(oid, seen, timeout)
        if len(self._seen) > 2000:
            self._seen.clear()
        self._seen[oid] = now
        return now > seen

    def levels(self, slug):
        return self.public.live_levels(slug)

    def warm(self):
        self.http.warm()

    def market_info(self, slug):
        m = self.public.http.get(f"/market/slug/{slug}")["market"]
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        return {"open": bool(m.get("active")) and not m.get("closed") and m.get("status") == "MARKET_STATUS_OPEN",
                "tick": lambda _p: tick, "min_qty": float(m.get("minimumTradeQty") or 1),
                "fee_coef": float(m.get("feeCoefficient") or config.POLYMARKET_DEFAULT_COEF)}

    def balance(self, shard=None):
        live = self.stream_buying_power()
        if live is not None:
            return live
        bals = self.http.get("/v1/account/balances").get("balances") or []
        usd = next((b for b in bals if b.get("currency") in (None, "", "USD")), bals[0] if bals else {})
        return float(usd.get("buyingPower") or 0)

    def _order(self, slug, intent, qty, yes_price):
        body = {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_price:.4f}".rstrip("0").rstrip("."), "currency": "USD"},
                "quantity": round(qty, 2), "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "intent": intent,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL",
                "synchronousExecution": True, "maxBlockTime": "5"}
        r = self.http.post("/v1/orders", body)
        order = self._final_order(r)
        return body, r, order

    TERMINAL = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED",
                "ORDER_STATE_EXPIRED", "ORDER_STATE_REPLACED"}

    def _final_order(self, r):
        """The order's final state. The synchronous response can still say ORDER_STATE_NEW
        (seen in live testing), so fills are only trusted once the state is terminal;
        otherwise poll GET /v1/order/{id}. Never guess: raise if it can't be confirmed."""
        execs = r.get("executions") or []
        for e in execs:
            if e.get("type") == "EXECUTION_TYPE_REJECTED":
                raise ApiError(400, f"rejected: {e.get('orderRejectReason')} {e.get('text') or ''}".strip())
        orders = [e.get("order") or {} for e in execs if e.get("order")]
        best = max(orders, key=lambda o: float(o.get("cumQuantity") or 0)) if orders else {}
        if any(o.get("state") in self.TERMINAL for o in orders):
            return best
        oid = r.get("id") or best.get("id")
        delay, deadline = 0.05, time.monotonic() + 12.0    # check soon, then back off
        while time.monotonic() < deadline:
            # With the private stream: read the order as soon as news about it is pushed (at most 1s
            # apart as a backstop) instead of polling it every 50-250ms.
            if self.wait_order(oid, 1.0) is None:
                time.sleep(delay)
                delay = min(delay * 1.6, 0.25)
            with priority():
                d = self.http.get(f"/v1/order/{oid}")
            o = d.get("order", d)
            if o.get("state") in self.TERMINAL:
                return o
        raise RuntimeError(f"Polymarket order {oid} didn't reach a final state; check the account")

    def _fill(self, body, r, order, limit, buying, fee_coef):
        n = float(order.get("cumQuantity") or 0)
        f = Fill(qty=n, order_id=r.get("id", ""), request=body, response=r)
        if n > 0:
            avg = float(((order.get("avgPx") or {}).get("value")) or limit)
            f.amount = n * _per_share(avg, limit, buying)
            fee = (order.get("commissionNotionalTotalCollected") or {}).get("value")
            f.fee = float(fee) if fee is not None else round(fee_per_contract(fee_coef, f.avg) * n, 2)
        return f

    def buy(self, slug, side, qty, limit, fee_coef):
        intent, yes_price = ("ORDER_INTENT_BUY_LONG", limit) if side == "yes" else ("ORDER_INTENT_BUY_SHORT", 1 - limit)
        body, r, order = self._order(slug, intent, qty, yes_price)
        return self._fill(body, r, order, limit, True, fee_coef)

    # ---- resting (maker) orders ------------------------------------------------------------

    def post_maker(self, slug, side, qty, cost, ttl_secs):
        """Rest a post-only buy of `side` at `cost` per share. It expires on Polymarket's side after
        ttl_secs even if this app stops, and is rejected (never fills as a taker) if it would cross.
        Returns (order id, request, response)."""
        intent, yes_price = ("ORDER_INTENT_BUY_LONG", cost) if side == "yes" else ("ORDER_INTENT_BUY_SHORT", 1 - cost)
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_secs))
        body = {"marketSlug": slug, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_price:.4f}".rstrip("0").rstrip("."), "currency": "USD"},
                "quantity": round(qty, 2), "tif": "TIME_IN_FORCE_GOOD_TILL_DATE", "goodTillTime": until,
                "participateDontInitiate": True, "intent": intent,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_MANUAL"}
        r = self.http.post("/v1/orders", body)
        for e in r.get("executions") or []:
            if e.get("type") == "EXECUTION_TYPE_REJECTED":
                raise ApiError(400, f"rejected: {e.get('orderRejectReason')} {e.get('text') or ''}".strip())
        oid = r.get("id") or ((r.get("executions") or [{}])[0].get("order") or {}).get("id")
        if not oid:
            raise RuntimeError(f"Polymarket didn't return an order id: {r}")
        return oid, body, r

    def order(self, oid):
        """The order's current state: {state, cumQuantity, leavesQuantity, avgPx, ...}."""
        d = self.http.get(f"/v1/order/{oid}")
        return d.get("order", d)

    def cancel(self, oid, slug):
        return self.http.post(f"/v1/order/{oid}/cancel", {"marketSlug": slug})

    def maker_fills(self, order, cost, fee_coef):
        """(shares filled, dollars paid for them, fee) from an order's state; a maker rebate is a negative fee."""
        n = float(order.get("cumQuantity") or 0)
        if n <= 0:
            return 0.0, 0.0, 0.0
        avg = float(((order.get("avgPx") or {}).get("value")) or cost)
        fee = (order.get("commissionNotionalTotalCollected") or {}).get("value")
        per = _per_share(avg, cost, True)
        fee = float(fee) if fee is not None else -config.POLYMARKET_MAKER_REBATE * per * (1 - per) * n
        return n, n * per, fee

    def sell(self, slug, side, qty, min_price, fee_coef):
        intent, yes_price = ("ORDER_INTENT_SELL_LONG", min_price) if side == "yes" else ("ORDER_INTENT_SELL_SHORT", 1 - min_price)
        body, r, order = self._order(slug, intent, qty, yes_price)
        return self._fill(body, r, order, min_price, False, fee_coef)
