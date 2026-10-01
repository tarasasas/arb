import threading
import unittest
from datetime import timedelta
from unittest import mock

from arb import config, engine, maker
from arb.http import ApiError
from arb.venues import Fill

TL = "/tmp/arb_test_maker_trades.jsonl"


def row(game="G", hl=0.45, cost=0.50, closes_h=5, **kw):
    return {"game": game, "tab": "Sports", "payout": 1.0, "profit": 0.5, "size": 100, "edge_per_contract": 0.01,
            "closes": (engine.now_utc() + timedelta(hours=closes_h)).isoformat(), "warnings": [],
            "fee_coef": {"kalshi": 0.07, "polymarket": -0.0125},
            "maker": {"post_yes_price": cost, "cost": cost, "hedge_limit": hl, "tick": 0.01},
            "legs": [{"exchange": "Kalshi", "market_id": "K", "side": "no", "title": "k"},
                     {"exchange": "Polymarket", "market_id": "p", "side": "yes", "title": "p", "maker": True}], **kw}


class FakeKalshi:
    def __init__(self, ask=0.44, depth=1000, fill=True):
        self.ask, self.depth, self.fill, self.buys = ask, depth, fill, []

    def market_info(self, t):
        return {"open": True, "min_qty": 1.0, "shard": 0, "tick": lambda p: 0.01}

    def levels(self, t):
        return {"yes": [(1 - self.ask + 0.02, 100)], "no": [(self.ask, self.depth)]}

    def balance(self, shard=None):
        return 1000.0

    def buy(self, t, side, qty, limit, coef):
        self.buys.append((qty, limit))
        if not self.fill or self.ask > limit + 1e-9:
            return Fill()
        return Fill(qty=qty, amount=qty * self.ask, fee=0.02 * qty)


class FakePoly:
    """A resting order whose fills arrive over successive reads (script: cumulative fills per read)."""

    def __init__(self, script=(0, 0, 10, 10, 30), reject=None):
        self.script, self.reads, self.reject = list(script), 0, reject
        self.cancelled, self.sold, self.state = [], [], "ORDER_STATE_NEW"

    def post_maker(self, slug, side, qty, cost, ttl):
        if self.reject:
            raise ApiError(400, self.reject)
        self.qty = qty
        return "o1", {}, {}

    def order(self, oid):
        i = min(self.reads, len(self.script) - 1)
        self.reads += 1
        cum = min(self.script[i], self.qty)
        st = "ORDER_STATE_FILLED" if cum >= self.qty else self.state
        return {"state": st, "cumQuantity": cum, "avgPx": {"value": "0.5"}}

    def maker_fills(self, o, cost, coef):
        n = float(o["cumQuantity"])
        return n, n * 0.5, -0.003 * n

    def cancel(self, oid, slug):
        self.cancelled.append(oid)
        self.state = "ORDER_STATE_CANCELED"

    def balance(self, shard=None):
        return 1000.0

    def levels(self, slug):
        return {"yes": [(0.52, 100)], "no": [(0.51, 100)]}

    def sell(self, slug, side, qty, min_price, coef):
        self.sold.append(qty)
        return Fill(qty=qty, amount=qty * min_price, fee=0.0)


class FakeTrader:
    def __init__(self, k, p):
        self.venues = {"kalshi": k, "polymarket": p}

    def _cached_cash(self, ex, shard=None):
        return None

    def _live_book(self, ex, mid):
        return None


class FakeScanner:
    def __init__(self, k, p, rows):
        self.trader, self.lock, self.state = FakeTrader(k, p), threading.Lock(), {"maker": rows, "balances": {}}
        self.logs, self.my_arbs, self.alerter, self.trading_status = [], mock.Mock(), None, "on"

    def log(self, m):
        self.logs.append(m)

    def refresh_balances(self):
        pass


@mock.patch.object(config, "TRADES_LOG", TL)
@mock.patch.object(config, "MAKER_AUTO_POLL_SECS", 0)
class MakerBotTests(unittest.TestCase):
    def make(self, k=None, p=None, rows=None):
        rows = rows if rows is not None else [row()]
        s = FakeScanner(k or FakeKalshi(), p or FakePoly(), rows)
        b = maker.MakerBot(s, run_async=False, sleep=lambda _s: None)
        b.set(True)
        return s, b

    def test_every_fill_is_hedged_on_kalshi(self):
        s, b = self.make()
        b.check(s.state["maker"])
        h = b.history[0]
        self.assertEqual(h["status"], "ok")
        k = s.trader.venues["kalshi"]
        self.assertEqual(sum(q for q, _ in k.buys), h["filled"])
        self.assertEqual(h["pairs"], h["filled"])
        self.assertGreater(h["net"], 0)
        s.my_arbs.add_from_trade.assert_called_once()
        self.assertEqual(b.active, {})

    def test_size_fits_the_budget_and_kalshi_depth(self):
        s, b = self.make(k=FakeKalshi(depth=20))
        b.check(s.state["maker"])
        self.assertEqual(s.trader.venues["polymarket"].qty, 10)          # 20 shares / 2x hedge depth

    def test_kalshi_moving_past_the_limit_cancels(self):
        k = FakeKalshi()
        p = FakePoly(script=(0, 0, 0, 0))
        s, b = self.make(k=k, p=p)
        orig = p.order

        def order(oid):
            if p.reads == 2:
                k.ask = 0.47                                             # Kalshi now above the 0.45 hedge limit
            return orig(oid)
        p.order = order
        b.check(s.state["maker"])
        self.assertEqual(p.cancelled, ["o1"])
        self.assertIn("past the hedge limit", b.history[0]["note"])
        self.assertEqual(b.history[0]["status"], "no_fill")

    def test_unhedgeable_fill_is_sold_back_and_counted(self):
        k = FakeKalshi(fill=False)
        p = FakePoly(script=(0, 10))
        s, b = self.make(k=k, p=p)
        with mock.patch.object(config, "AUTO_TRADE_MAX_MISSES", 1):
            b.check(s.state["maker"])
        self.assertEqual(p.cancelled, ["o1"])
        self.assertEqual(p.sold, [10])
        self.assertEqual(b.history[0]["status"], "partial")
        self.assertFalse(b.on)
        self.assertIn("in a row", b.halted)

    def test_arb_leaving_the_list_cancels(self):
        p = FakePoly(script=(0, 0, 0))
        s, b = self.make(p=p)
        orig = p.order

        def order(oid):
            s.state["maker"] = []
            return orig(oid)
        p.order = order
        b.check([row()])
        self.assertIn("left the Maker mode list", b.history[0]["note"])

    def test_turning_off_cancels(self):
        p = FakePoly(script=(0, 0, 0))
        s, b = self.make(p=p)
        orig = p.order

        def order(oid):
            b.on = False
            return orig(oid)
        p.order = order
        b.check(s.state["maker"])
        self.assertEqual(p.cancelled, ["o1"])

    def test_rejected_post_trades_nothing(self):
        s, b = self.make(p=FakePoly(reject="would cross"))
        b.check(s.state["maker"])
        self.assertEqual(b.history[0]["status"], "skipped")
        self.assertTrue(b.on)

    def test_pick_rules(self):
        s, b = self.make(rows=[])
        self.assertIsNone(b.pick([row(closes_h=48)]))                     # settles too late
        self.assertIsNone(b.pick([row(warnings=["Game already started: x"])]))
        self.assertIsNone(b.pick([row(hl=None)]))
        self.assertIsNotNone(b.pick([row()]))
        b.tried[maker.pair_id(row()["legs"])] = b.clock()
        self.assertIsNone(b.pick([row()]))                                # cooldown

    def test_off_does_nothing(self):
        s, b = self.make()
        b.set(False)
        self.assertIsNone(b.check(s.state["maker"]))


if __name__ == "__main__":
    unittest.main()


class PolymarketMakerOrderTests(unittest.TestCase):
    def test_post_only_good_till_date_buy_no(self):
        from arb.venues import PolymarketVenue
        pv = PolymarketVenue.__new__(PolymarketVenue)
        pv.http = mock.Mock()
        pv.http.post.return_value = {"id": "abc"}
        oid, body, _ = pv.post_maker("slug", "no", 10, 0.47, 120)
        self.assertEqual(oid, "abc")
        self.assertEqual(body["tif"], "TIME_IN_FORCE_GOOD_TILL_DATE")
        self.assertTrue(body["participateDontInitiate"])
        self.assertEqual((body["intent"], body["price"]["value"]), ("ORDER_INTENT_BUY_SHORT", "0.53"))
        self.assertTrue(body["goodTillTime"].endswith("Z"))
        pv.cancel("abc", "slug")
        pv.http.post.assert_called_with("/v1/order/abc/cancel", {"marketSlug": "slug"})

    def test_rejected_post(self):
        from arb.venues import PolymarketVenue
        pv = PolymarketVenue.__new__(PolymarketVenue)
        pv.http = mock.Mock()
        pv.http.post.return_value = {"id": "x", "executions": [{"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "PDI"}]}
        with self.assertRaises(ApiError):
            pv.post_maker("slug", "yes", 10, 0.5, 120)
