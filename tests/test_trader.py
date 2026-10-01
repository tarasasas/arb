import unittest
from unittest import mock

from arb import trader as trader_mod
from arb.model import NO, YES, Contract, total_fee
from arb.trader import Trader, TradeError
from arb.venues import Fill

TOTAL = ("total", "FG")


class FakeVenue:
    """Order book in cost space per side; IOC buys fill from levels at or below the limit."""

    def __init__(self, name, yes=None, no=None, min_qty=1.0, balance=10_000.0, fractional_fill=None):
        self.name, self.min_qty, self.bal = name, min_qty, balance
        self.book = {"yes": list(yes or []), "no": list(no or [])}
        self.orders, self.refills, self.fractional_fill = [], [], fractional_fill
        self.after_buy = None                         # callback(): the market moves right after our order

    def levels(self, _mid):
        return {s: [lv for lv in self.book[s] if lv[1] > 0] for s in ("yes", "no")}

    def market_info(self, _mid):
        return {"open": True, "tick": lambda _p: 0.01, "min_qty": self.min_qty}

    def balance(self):
        return self.bal

    def buy(self, mid, side, qty, limit, coef):
        self.orders.append(("buy", side, qty, limit))
        if self.fractional_fill is not None:          # simulate an exchange filling less than asked
            qty, self.fractional_fill = min(qty, self.fractional_fill), None
        fills, left = [], qty
        for i, (p, q) in enumerate(self.book[side]):
            if left <= 1e-9 or p > limit + 1e-9:
                break
            t = min(q, left)
            fills.append((p, t))
            self.book[side][i] = (p, q - t)
            left -= t
        if self.refills:                              # liquidity that appears after this order
            self.book[side] = sorted(self.book[side] + [self.refills.pop(0)])
        n = sum(q for _, q in fills)
        if self.after_buy:
            self.after_buy()
            self.after_buy = None
        return Fill(qty=n, amount=sum(p * q for p, q in fills), fee=total_fee(self.name, fills, coef))

    def sell(self, mid, side, qty, min_price, coef):
        self.orders.append(("sell", side, qty, min_price))
        return Fill(qty=qty, amount=qty * min_price, fee=total_fee(self.name, [(min_price, qty)], coef))


class FakeScanner:
    def __init__(self):
        self.k = Contract("kalshi", "K", "G", TOTAL, ">", 5.5, "Over 5.5", fee_coef=0.07)
        self.p = Contract("polymarket", "P", "G", TOTAL, ">", 5.5, "Over 5.5?", fee_coef=0.0695)

    def find_contract(self, ex, mid):
        return self.k if ex == "kalshi" else self.p


LEGS = [{"exchange": "kalshi", "market_id": "K", "side": YES}, {"exchange": "polymarket", "market_id": "P", "side": NO}]


def make(kalshi, poly):
    return Trader(FakeScanner(), {"kalshi": kalshi, "polymarket": poly})


@mock.patch.object(trader_mod.config, "TRADES_LOG", new_callable=lambda: __import__("pathlib").Path(__import__("tempfile").gettempdir()) / "arb_test_trades.jsonl")
@mock.patch.object(trader_mod.time, "sleep", lambda _s: None)
class TraderTests(unittest.TestCase):
    def test_cap_and_full_hedge(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 500)])
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        self.assertLessEqual(plan["capital"], 100.0)            # $100 hard cap
        self.assertEqual(plan["size"], 107)                      # 107 x ~0.934 = $99.95; 108 would exceed $100
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hedged_pairs"], 107)
        self.assertGreater(res["locked_profit"], 0)
        self.assertEqual(res["unhedged_shares"], 0)

    def test_thinner_book_goes_first(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 50)])                 # thin
        p = FakeVenue("polymarket", no=[(0.50, 5000)])
        plan = make(k, p).prepare(LEGS)
        self.assertEqual(plan["first"], "kalshi")

    def test_second_leg_retry_fills_the_rest(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 30)])
        t = make(k, p)
        plan = t.prepare(LEGS)                                   # 30 pairs, polymarket thinner -> first
        self.assertEqual(plan["first"], "polymarket")

        def thin():                                             # kalshi thins out while the first leg fills...
            k.book["yes"] = [(0.40, 10)]
            k.refills = [(0.41, 100)]                           # ...then more arrives at 0.41
        p.after_buy = thin
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 30)
        self.assertEqual(res["status"], "ok")

    def test_unfillable_second_leg_is_sold_back(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.55, 100)])   # yes book lets us sell NO back
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.after_buy = lambda: k.book.update(yes=[(0.40, 5), (0.60, 1000)])   # only 5 left below break-even
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 5)
        self.assertEqual(res["status"], "partial")
        sells = [o for o in p.orders if o[0] == "sell"]
        self.assertEqual(sells[0][2], 15)                        # the 15 unhedged shares sold back
        self.assertEqual(res["unhedged_shares"], 0)
        # never paid above break-even: no Kalshi buy at 0.60
        self.assertTrue(all(o[3] < 0.6 for o in k.orders if o[0] == "buy"))

    def test_arb_gone_at_confirm_sends_nothing(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.70, 20)]                              # price ran away while the dialog was open
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "moved")
        self.assertEqual((k.orders, p.orders), ([], []))         # no order on either exchange

    def test_confirm_resizes_to_live_depth(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 30)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        self.assertEqual(plan["size"], 30)
        p.book["no"] = [(0.50, 12), (0.70, 100)]                 # only 12 still profitable at confirm
        res = t.execute(plan["id"])
        self.assertEqual(p.orders[0][2], 12)                     # first leg re-sized, not sent for 30
        self.assertEqual((res["status"], res["hedged_pairs"]), ("ok", 12))

    def test_confirm_never_grows_the_trade(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 30)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.45, 1000)]                            # better and deeper at confirm
        t.execute(plan["id"])
        self.assertEqual(p.orders[0][2], 30)

    def test_first_leg_no_fill(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], fractional_fill=0)   # gone by the time the order lands
        t = make(k, p)
        plan = t.prepare(LEGS)
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual(k.orders, [])                           # second leg never sent

    def test_second_leg_limit_is_break_even_not_planned_price(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        self.assertEqual(plan["first"], "polymarket")
        p.after_buy = lambda: k.book.update(yes=[(0.43, 1000)])  # kalshi ticks up 3c while leg 1 fills
        res = t.execute(plan["id"])
        buys = [o for o in k.orders if o[0] == "buy"]
        self.assertEqual(len(buys), 1)                           # hedged on the first attempt
        self.assertGreaterEqual(buys[0][3], 0.43)
        self.assertEqual((res["status"], res["hedged_pairs"]), ("ok", 20))
        self.assertGreaterEqual(res["net"], 0)                   # never below break-even

    def test_close_out_hedges_above_break_even_when_cheaper_than_selling_back(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.60, 100)])   # selling NO back gets only 0.40
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.after_buy = lambda: k.book.update(yes=[(0.40, 5), (0.52, 1000)])   # 0.52 is ~2c above break-even
        res = t.execute(plan["id"])
        self.assertEqual([o for o in p.orders if o[0] == "sell"], [])
        self.assertEqual((res["status"], res["hedged_pairs"], res["unhedged_shares"]), ("ok", 20, 0))
        sell_back_net = 15 * (0.40 - 0.517)                      # what selling back would have lost, roughly
        self.assertGreater(res["net"], sell_back_net)

    def test_close_out_never_hedges_beyond_the_loss_limit(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])             # no bids to sell back into at all
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.after_buy = lambda: k.book.update(yes=[(0.40, 5), (0.80, 1000)])
        res = t.execute(plan["id"])
        self.assertTrue(all(o[3] < 0.6 for o in k.orders if o[0] == "buy"))
        self.assertEqual(res["unhedged_shares"], 15)             # left for you to handle, flagged in red

    def test_fractional_first_fill_matches_whole_contracts(self, *_):
        k = FakeVenue("kalshi", yes=[(0.05, 1000)], min_qty=1.0)
        p = FakeVenue("polymarket", no=[(0.94, 50)], yes=[(0.07, 500)], min_qty=0.01, fractional_fill=40.3)
        t = make(k, p)
        plan = t.prepare(LEGS)
        self.assertEqual(plan["first"], "polymarket")
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 40)                # Kalshi whole contracts
        sells = [o for o in p.orders if o[0] == "sell"]
        self.assertAlmostEqual(sells[0][2], 0.3, places=6)       # 0.3 fractional excess sold back

    def test_plan_is_single_use_and_expires(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 1000)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        t.execute(plan["id"])
        with self.assertRaises(TradeError):
            t.execute(plan["id"])
        plan = t.prepare(LEGS)
        t.plans[plan["id"]][0]["created"] -= 60
        with self.assertRaises(TradeError):
            t.execute(plan["id"])

    def test_balance_limits_size(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)], balance=8.0)   # only $8 on Kalshi
        p = FakeVenue("polymarket", no=[(0.50, 1000)])
        plan = make(k, p).prepare(LEGS)
        leg = plan["legs"]["kalshi"]
        self.assertLessEqual(leg["amount"] + leg["fee"], 8.0)

    def test_unprofitable_raises(self, *_):
        k = FakeVenue("kalshi", yes=[(0.50, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 100)])
        with self.assertRaises(TradeError):
            make(k, p).prepare(LEGS)


if __name__ == "__main__":
    unittest.main()
