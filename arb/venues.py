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
from .http import ApiError, RateLimitedClient
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


def book_avg(levels, qty):
    """Average price of taking qty from [(price, qty)] best first; None if the book is empty."""
    left, cost, got = qty, 0.0, 0.0
    for p, q in levels or []:
        if left <= 1e-9:
            break
        t = min(q, left)
        cost, got, left = cost + p * t, got + t, left - t
    return cost / got if got else None


def _per_share(avg, limit, buying, expect=None):
    """Exchanges report an average price; which side it's quoted in isn't always explicit.
    Keep the readings consistent with the limit. When both are (near 50/50 they're only a few
    cents apart), take the one closest to what the book we just hit predicts; without a book,
    fall back to the conservative one (higher cost / lower proceeds). Getting this wrong near
    50/50 shifts the second leg's break-even by several cents."""
    if not avg:
        return limit
    cands = [avg, 1 - avg]
    ok = [c for c in cands if (c <= limit + 1e-6 if buying else c >= limit - 1e-6)]
    if not ok:
        return limit
    if expect is not None and len(ok) == 2:
        return min(ok, key=lambda c: abs(c - expect))
    return max(ok) if buying else min(ok)


class KalshiVenue:
    name = "kalshi"

    def __init__(self, kalshi_client):
        self.client = kalshi_client          # signed RateLimitedClient lives at client.http

    # Every call here is part of a trade, so it skips the scanner's queue on the shared client.
    def levels(self, ticker):
        return self.client.live_levels(ticker, priority=True)

    def market_info(self, ticker):
        m = self.client.http.get(f"/markets/{ticker}", priority=True)["market"]
        ranges = [(float(r["start"]), float(r["end"]), float(r["step"])) for r in m.get("price_ranges") or []]

        def step_at(price):
            for start, end, step in ranges:
                if start - 1e-9 <= price <= end + 1e-9:
                    return step
            return 0.01

        # Orders are priced on the YES book, so a NO cost c goes out as 1 - c: use a step valid at both.
        return {"open": m.get("status") == "active", "tick": lambda p: max(step_at(p), step_at(1 - p)),
                "min_qty": 0.01 if m.get("fractional_trading_enabled") else 1.0}

    def balance(self):
        return float(self.client.http.get("/portfolio/balance", priority=True)["balance"]) / 100

    def _order(self, ticker, book_side, qty, yes_price, reduce_only=False):
        body = {"ticker": ticker, "side": book_side, "count": f"{qty:.2f}", "price": f"{yes_price:.4f}",
                "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": str(uuid.uuid4())}
        if reduce_only:
            body["reduce_only"] = True
        return body, self.client.http.post("/portfolio/events/orders", body, priority=True)

    def buy(self, ticker, side, qty, limit, fee_coef, book=None):
        """book: the levels this order is expected to hit (cost space), to read the fill price."""
        # buy YES at <= limit: bid at limit.  buy NO at <= limit: sell YES (ask) at >= 1 - limit.
        body, r = self._order(ticker, "bid" if side == "yes" else "ask", qty,
                              limit if side == "yes" else 1 - limit)
        n = float(r.get("fill_count") or 0)
        f = Fill(qty=n, order_id=r.get("order_id", ""), request=body, response=r)
        if n > 0:
            f.amount = n * _per_share(float(r.get("average_fill_price") or 0), limit, True, book_avg(book, n))
            fee = r.get("average_fee_paid")
            f.fee = float(fee) * n if fee is not None else math.ceil(fee_per_contract(fee_coef, f.avg) * n * 100) / 100
        return f

    def sell(self, ticker, side, qty, min_price, fee_coef, book=None):
        # sell YES at >= min: ask at min.  sell NO at >= min: buy YES (bid) at <= 1 - min.
        body, r = self._order(ticker, "ask" if side == "yes" else "bid", qty,
                              min_price if side == "yes" else 1 - min_price, reduce_only=True)
        n = float(r.get("fill_count") or 0)
        f = Fill(qty=n, order_id=r.get("order_id", ""), request=body, response=r)
        if n > 0:
            f.amount = n * _per_share(float(r.get("average_fill_price") or 0), min_price, False, book_avg(book, n))
            fee = r.get("average_fee_paid")
            f.fee = float(fee) * n if fee is not None else math.ceil(fee_per_contract(fee_coef, f.avg) * n * 100) / 100
        return f


class PolymarketVenue:
    name = "polymarket"

    def __init__(self, public_client, signer):
        self.public = public_client          # gateway (market data)
        self.http = RateLimitedClient(config.POLYMARKET_TRADE_BASE, 8.0, signer=signer)

    def levels(self, slug):
        return self.public.live_levels(slug, priority=True)       # skips the scanner's queue

    def market_info(self, slug):
        m = self.public.http.get(f"/market/slug/{slug}", priority=True)["market"]
        tick = float(m.get("orderPriceMinTickSize") or 0.01)
        return {"open": bool(m.get("active")) and not m.get("closed") and m.get("status") == "MARKET_STATUS_OPEN",
                "tick": lambda _p: tick, "min_qty": float(m.get("minimumTradeQty") or 1)}

    def balance(self):
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
        for i in range(23):             # 0.05s, 0.1s, 0.2s, then every 0.25s: about 5s in all
            time.sleep(min(0.05 * 2 ** i, 0.25))
            d = self.http.get(f"/v1/order/{oid}")
            o = d.get("order", d)
            if o.get("state") in self.TERMINAL:
                return o
        raise RuntimeError(f"Polymarket order {oid} didn't reach a final state; check the account")

    def _fill(self, body, r, order, limit, buying, fee_coef, book):
        n = float(order.get("cumQuantity") or 0)
        f = Fill(qty=n, order_id=r.get("id", ""), request=body, response=r)
        if n > 0:
            avg = float(((order.get("avgPx") or {}).get("value")) or 0)
            f.amount = n * _per_share(avg, limit, buying, book_avg(book, n))
            fee = (order.get("commissionNotionalTotalCollected") or {}).get("value")
            f.fee = float(fee) if fee is not None else round(fee_per_contract(fee_coef, f.avg) * n, 2)
        return f

    def buy(self, slug, side, qty, limit, fee_coef, book=None):
        intent, yes_price = ("ORDER_INTENT_BUY_LONG", limit) if side == "yes" else ("ORDER_INTENT_BUY_SHORT", 1 - limit)
        body, r, order = self._order(slug, intent, qty, yes_price)
        return self._fill(body, r, order, limit, True, fee_coef, book)

    def sell(self, slug, side, qty, min_price, fee_coef, book=None):
        intent, yes_price = ("ORDER_INTENT_SELL_LONG", min_price) if side == "yes" else ("ORDER_INTENT_SELL_SHORT", 1 - min_price)
        body, r, order = self._order(slug, intent, qty, yes_price)
        return self._fill(body, r, order, min_price, False, fee_coef, book)
