import unittest
from unittest import mock

from arb import trader as trader_mod
from arb.model import NO, YES, Contract, total_fee
from arb.trader import Trader, TradeError
from arb.venues import Fill, _per_share

TOTAL = ("total", "FG")


class FakeVenue:
    """Order book in cost space per side; IOC buys fill from levels at or below the limit."""

    def __init__(self, name, yes=None, no=None, min_qty=1.0, balance=10_000.0, fractional_fill=None):
        self.name, self.min_qty, self.bal = name, min_qty, balance
        self.book = {"yes": list(yes or []), "no": list(no or [])}
        self.orders, self.refills, self.fractional_fill = [], [], fractional_fill
        self.before_buy = self.after_buy = None         # hooks to move the books mid-trade

    def levels(self, _mid):
        return {s: [lv for lv in self.book[s] if lv[1] > 0] for s in ("yes", "no")}

    def market_info(self, _mid):
        return {"open": True, "tick": lambda _p: 0.01, "min_qty": self.min_qty}

    def balance(self):
        return self.bal

    def buy(self, mid, side, qty, limit, coef, book=None):
        if self.before_buy:
            self.before_buy()
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
        return Fill(qty=n, amount=sum(p * q for p, q in fills), fee=total_fee(self.name, fills, coef))

    def sell(self, mid, side, qty, min_price, coef, book=None):
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

        def thin():                                             # kalshi thins out as the first leg fills...
            k.book["yes"] = [(0.40, 10)]
            k.refills = [(0.41, 100)]                           # ...then more arrives at 0.41
        p.after_buy = thin
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 30)
        self.assertEqual(res["status"], "ok")
        self.assertEqual([o[2] for o in k.orders], [30, 20])    # one retry, for the 20 still open

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
        # never paid near 0.60, and never re-sent an order with nothing under the ceiling to hit
        self.assertEqual(len(k.orders), 1)
        self.assertLess(k.orders[0][3], 0.5)

    def test_first_leg_no_fill(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.before_buy = lambda: p.book.update(no=[(0.70, 20)])   # price ran away as our order went in
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual(k.orders, [])                           # second leg never sent

    def test_preflight_cancels_when_books_moved_past_the_limits(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.70, 20)]                              # moved while the confirm dialog was open
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual((k.orders, p.orders), ([], []))         # nothing sent on either exchange

    def test_preflight_trims_to_what_the_second_leg_still_holds(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.55, 100)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        self.assertEqual((plan["size"], plan["first"]), (20, "polymarket"))
        k.book["yes"] = [(0.40, 5), (0.60, 1000)]               # Kalshi thinned while the dialog was open
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "ok")                    # trimmed to 5 pairs, no sell-back
        self.assertEqual(res["hedged_pairs"], 5)
        self.assertEqual([o for o in p.orders if o[0] == "sell"], [])
        self.assertEqual([o[2] for o in p.orders], [5])
        self.assertGreater(res["locked_profit"], 0)

    def test_second_leg_goes_straight_to_break_even(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 30)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.after_buy = lambda: k.book.update(yes=[(0.42, 1000)])   # ticks up 2c, still under break-even
        res = t.execute(plan["id"])
        self.assertEqual((res["status"], res["hedged_pairs"]), ("ok", 30))
        self.assertEqual(len(k.orders), 1)                       # first try fills; no miss, no wait
        self.assertGreater(res["locked_profit"], 0)

    def test_hedges_past_break_even_when_cheaper_than_selling_back(self, *_):
        def run():
            k = FakeVenue("kalshi", yes=[(0.40, 1000)])
            p = FakeVenue("polymarket", no=[(0.50, 30)], yes=[(0.55, 100)])
            t = make(k, p)
            plan = t.prepare(LEGS)
            p.after_buy = lambda: k.book.update(yes=[(0.47, 1000)])  # past break-even (0.46), within 2c
            return t.execute(plan["id"]), k, p

        res, k, p = run()
        self.assertEqual((res["status"], res["hedged_pairs"]), ("ok", 30))
        self.assertLess(res["locked_profit"], 0)                 # a small locked loss...
        self.assertGreater(res["locked_profit"], -0.02 * 30)     # ...bounded by the allowance
        self.assertEqual([o for o in p.orders if o[0] == "sell"], [])

        with mock.patch.object(trader_mod.config, "HEDGE_MAX_LOSS_PER_SHARE", 0.0):
            strict, k, p = run()
        self.assertEqual((strict["status"], strict["hedged_pairs"]), ("partial", 0))
        self.assertEqual(len(k.orders), 1)                       # no blind retries with nothing to hit
        self.assertLess(strict["net"], res["net"])               # selling back cost more than hedging

    def test_rejected_second_leg_unwinds_without_retrying(self, *_):
        from arb.http import ApiError
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.55, 100)])
        t = make(k, p)
        plan = t.prepare(LEGS)

        def reject():
            k.orders.append(("rejected",))
            raise ApiError(400, "insufficient balance")
        k.before_buy = reject
        res = t.execute(plan["id"])
        self.assertEqual(len(k.orders), 1)                       # not re-sent: it would be refused again
        self.assertEqual(res["status"], "partial")
        self.assertEqual([o[2] for o in p.orders if o[0] == "sell"], [20])

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

    def test_closed_market_reported_even_if_its_book_fails(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 1000)])
        k.market_info = lambda _mid: {"open": False, "tick": lambda _p: 0.01, "min_qty": 1.0}
        k.levels = mock.Mock(side_effect=RuntimeError("404"))
        with self.assertRaisesRegex(TradeError, "Kalshi market isn't open"):
            make(k, p).prepare(LEGS)

    def test_preflight_failure_sends_nothing(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 1000)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.levels = mock.Mock(side_effect=RuntimeError("timeout"))
        with self.assertRaisesRegex(TradeError, "nothing was traded"):
            t.execute(plan["id"])
        self.assertEqual((k.orders, p.orders), ([], []))

    def test_unprofitable_raises(self, *_):
        k = FakeVenue("kalshi", yes=[(0.50, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 100)])
        with self.assertRaises(TradeError):
            make(k, p).prepare(LEGS)


class FillPriceTests(unittest.TestCase):
    def test_book_settles_which_side_the_average_is_quoted_in(self):
        # Bought at an average of 0.49 with a 0.52 limit: 0.49 and 0.51 both fit under the limit.
        self.assertAlmostEqual(_per_share(0.49, 0.52, True, expect=0.49), 0.49)
        self.assertAlmostEqual(_per_share(0.51, 0.52, True, expect=0.49), 0.49)   # quoted on the other side
        self.assertAlmostEqual(_per_share(0.49, 0.52, True), 0.51)                # no book: conservative
        self.assertAlmostEqual(_per_share(0.72, 0.30, True, expect=0.28), 0.28)   # only one reading fits
        self.assertEqual(_per_share(0, 0.52, True, expect=0.49), 0.52)            # no average: the limit


class PriorityTests(unittest.TestCase):
    def test_priority_request_skips_the_queue(self):
        from arb.http import RateLimitedClient
        c = RateLimitedClient("https://example.invalid", rps=1.0)
        with mock.patch("arb.http.time.sleep") as sleep:
            c._wait_turn()                                       # takes the free slot
            queued = c._next_slot
            c._wait_turn(priority=True)                          # goes now...
            sleep.assert_not_called()
            self.assertAlmostEqual(c._next_slot, queued + 1.0, places=2)   # ...and pushes the queue back
            c._wait_turn()
            sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
