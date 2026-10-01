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

    def levels(self, _mid):
        return {s: [lv for lv in self.book[s] if lv[1] > 0] for s in ("yes", "no")}

    def market_info(self, _mid):
        return {"open": True, "tick": lambda _p: 0.01, "min_qty": self.min_qty}

    def balance(self, shard=None):
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
@mock.patch.object(trader_mod.config, "TRADE_ORDER", "thinner_first")
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
        k.book["yes"] = [(0.40, 10)]                            # kalshi thins out before we trade...
        k.refills = [(0.41, 100)]                               # ...then more arrives at 0.41
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 30)
        self.assertEqual(res["status"], "ok")

    def test_unfillable_second_leg_is_sold_back(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.55, 100)])   # yes book lets us sell NO back
        t = make(k, p)
        plan = t.prepare(LEGS)
        k.book["yes"] = [(0.40, 5), (0.60, 1000)]               # only 5 left below break-even
        res = t.execute(plan["id"])
        self.assertEqual(res["hedged_pairs"], 5)
        self.assertEqual(res["status"], "partial")
        sells = [o for o in p.orders if o[0] == "sell"]
        self.assertEqual(sells[0][2], 15)                        # the 15 unhedged shares sold back
        self.assertEqual(res["unhedged_shares"], 0)
        # never paid above break-even: no Kalshi buy at 0.60
        self.assertTrue(all(o[3] < 0.6 for o in k.orders if o[0] == "buy"))

    def test_first_leg_no_fill(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.70, 20)]                              # price ran away before our order
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual(k.orders, [])                           # second leg never sent

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



class ShardTests(unittest.TestCase):
    def test_empty_shard_explains_what_to_do(self):
        class ShardVenue(FakeVenue):
            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 2}

            def balance(self, shard=None):
                return 0.0 if shard == 2 else 500.0
        t = make(ShardVenue("kalshi", yes=[(0.40, 500)]), FakeVenue("polymarket", no=[(0.50, 500)]))
        with self.assertRaises(TradeError) as cm:
            t.prepare(LEGS)
        self.assertIn("shard 2", str(cm.exception))
        self.assertIn("kalshi-shards.bat", str(cm.exception))

    def test_shard_cash_limits_the_size(self):
        class ShardVenue(FakeVenue):
            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 2}

            def balance(self, shard=None):
                return 20.0 if shard == 2 else 10_000.0
        t = make(ShardVenue("kalshi", yes=[(0.40, 500)]), FakeVenue("polymarket", no=[(0.50, 500)]))
        plan = t.prepare(LEGS)
        self.assertLessEqual(plan["legs"]["kalshi"]["amount"] + plan["legs"]["kalshi"]["fee"], 20.0)


class ShardFundingTests(unittest.TestCase):
    def test_trade_moves_cash_onto_the_markets_shard_first(self):
        class ShardVenue(FakeVenue):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cash, self.funded = {0: 500.0, 2: 0.0}, []

            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 2}

            def balance(self, shard=None):
                return self.cash[shard] if shard is not None else sum(self.cash.values())

            def fund_shard(self, shard, dollars):
                self.funded.append((shard, dollars))
                self.cash[0] -= dollars
                self.cash[shard] += dollars
                return [(0, dollars)], self.cash[shard]
        k = ShardVenue("kalshi", yes=[(0.40, 500)])
        t = make(k, FakeVenue("polymarket", no=[(0.50, 500)]))
        with mock.patch.object(trader_mod.config, "KALSHI_AUTO_SHARD_FUNDING", True):
            plan = t.prepare(LEGS)
        self.assertEqual(plan["size"], 107)                      # full $100 cap, as if the cash were there
        self.assertEqual(len(k.funded), 1)
        kalshi_spend = plan["legs"]["kalshi"]["amount"] + plan["legs"]["kalshi"]["fee"]
        self.assertGreaterEqual(k.funded[0][1], kalshi_spend)     # moved enough for the Kalshi leg
        self.assertLess(k.funded[0][1], kalshi_spend + 0.10)      # and not much more
        self.assertEqual(plan["shard_transfers"], [{"from": 0, "to": 2, "amount": k.funded[0][1]}])

    def test_off_means_no_transfer(self):
        class ShardVenue(FakeVenue):
            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 2}

            def balance(self, shard=None):
                return 0.0 if shard == 2 else 500.0

            def fund_shard(self, shard, dollars):
                raise AssertionError("moved cash with funding off")
        t = make(ShardVenue("kalshi", yes=[(0.40, 500)]), FakeVenue("polymarket", no=[(0.50, 500)]))
        with mock.patch.object(trader_mod.config, "KALSHI_AUTO_SHARD_FUNDING", False):
            with self.assertRaises(TradeError):
                t.prepare(LEGS)


class KalshiVenueFundTests(unittest.TestCase):
    def test_moves_from_richest_in_centicents_and_waits(self):
        from arb.venues import KalshiVenue

        class HTTP:
            def __init__(self):
                self.cash, self.posts, self.reads = {0: 30.0, 3: 100.0, 2: 1.0}, [], 0

            def get(self, path, params=None):
                if params and "exchange_index" in params:
                    self.reads += 1
                    if self.reads < 2:                            # not arrived on the first check
                        return {"balance": 100}
                    return {"balance": int(self.cash[params["exchange_index"]] * 100)}
                return {"balance_breakdown": [{"exchange_index": i, "balance": f"{b:.2f}"} for i, b in self.cash.items()]}

            def post(self, path, body):
                self.posts.append((path, body))
                src, dst, amt = body["source_exchange_shard"], body["destination_exchange_shard"], body["amount"] / 10_000
                self.cash[src] -= amt
                self.cash[dst] += amt
                return {"transfer_id": "t1"}
        http = HTTP()
        v = KalshiVenue(mock.Mock(http=http))
        moves, now = v.fund_shard(2, 110.0, wait=5, sleep=lambda s: None)
        self.assertEqual(moves, [(3, 100.0), (0, 10.0)])         # richest first, then the next
        self.assertEqual(http.posts[0], ("/portfolio/intra_exchange_instance_transfer", {
            "source": "event_contract", "destination": "event_contract", "amount": 1_000_000,
            "source_exchange_shard": 3, "destination_exchange_shard": 2}))
        self.assertAlmostEqual(now, 111.0)



@mock.patch.object(trader_mod.config, "TRADES_LOG", new_callable=lambda: __import__("pathlib").Path(__import__("tempfile").gettempdir()) / "arb_test_trades.jsonl")
@mock.patch.object(trader_mod.time, "sleep", lambda _s: None)
@mock.patch.object(trader_mod.config, "TRADE_ORDER", "together")
class TogetherTests(unittest.TestCase):
    def test_both_orders_are_in_flight_at_once(self, *_):
        import threading
        barrier = threading.Barrier(2, timeout=2)        # each buy waits for the other: deadlocks if sequential

        class Waiting(FakeVenue):
            def buy(self, *a):
                barrier.wait()
                return super().buy(*a)
        k, p = Waiting("kalshi", yes=[(0.40, 500)]), Waiting("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        res = t.execute(t.prepare(LEGS)["id"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hedged_pairs"], 107)
        self.assertEqual([o[2] for o in k.orders + p.orders], [107, 107])

    def test_uneven_fills_are_evened_up(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 500)])
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.50, 60)]                            # only 60 left at the planned price...
        p.refills = [(0.51, 500)]                              # ...and more shows up a bit higher
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["hedged_pairs"], plan["size"])   # Kalshi filled all; Polymarket caught up
        self.assertEqual(p.orders[0][2], plan["size"])        # first try: the full size at once
        self.assertGreater(len(p.orders), 1)                   # then the rest on a retry
        self.assertEqual(res["unhedged_shares"], 0)

    def test_one_side_rejected_sells_the_other_back(self, *_):
        class Rejecting(FakeVenue):
            def buy(self, *a):
                raise trader_mod.ApiError(400, "insufficient shard balance")
        k = Rejecting("kalshi", yes=[(0.40, 500)])
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        p.book["yes"] = [(0.48, 500)]                          # buyers for the sell-back
        t = make(k, p)
        res = t.execute(t.prepare(LEGS)["id"])
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["hedged_pairs"], 0)
        self.assertIn("sell", p.orders[-1][0])
        self.assertTrue(any("rejected" in s for s in res["steps"]))

    def test_unconfirmed_order_stops_without_selling(self, *_):
        class Broken(FakeVenue):
            def buy(self, *a):
                raise ConnectionError("timed out")
        k = Broken("kalshi", yes=[(0.40, 500)])
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        with self.assertRaises(TradeError) as cm:
            t.execute(t.prepare(LEGS)["id"])
        self.assertIn("Check that account", str(cm.exception))
        self.assertEqual([o[0] for o in p.orders], ["buy"])    # no sell-back on a maybe-filled order


class MainShardFundingTests(unittest.TestCase):
    def test_main_shard_is_funded_from_the_others_too(self):
        class ShardVenue(FakeVenue):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.cash, self.funded = {0: 0.0, 2: 300.0}, []

            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 0}

            def balance(self, shard=None):
                return self.cash[shard] if shard is not None else sum(self.cash.values())

            def shard_balances(self):
                return dict(self.cash)

            def fund_shard(self, shard, dollars):
                self.funded.append((shard, dollars))
                self.cash[2] -= dollars
                self.cash[shard] += dollars
                return [(2, dollars)], self.cash[shard]
        k = ShardVenue("kalshi", yes=[(0.40, 500)])
        t = make(k, FakeVenue("polymarket", no=[(0.50, 500)]))
        with mock.patch.object(trader_mod.config, "KALSHI_AUTO_SHARD_FUNDING", True):
            plan = t.prepare(LEGS)
        self.assertEqual(k.funded[0][0], 0)
        self.assertEqual(plan["size"], 107)

    def test_refused_transfer_is_explained(self):
        class ShardVenue(FakeVenue):
            def market_info(self, _mid):
                return {**super().market_info(_mid), "shard": 0}

            def balance(self, shard=None):
                return 0.0

            def shard_balances(self):
                return {0: 0.0, 3: 80.0}

            def fund_shard(self, shard, dollars):
                raise trader_mod.ApiError(403, "transfers not enabled")
        t = make(ShardVenue("kalshi", yes=[(0.40, 500)]), FakeVenue("polymarket", no=[(0.50, 500)]))
        with mock.patch.object(trader_mod.config, "KALSHI_AUTO_SHARD_FUNDING", True):
            with self.assertRaises(TradeError) as cm:
                t.prepare(LEGS)
        msg = str(cm.exception)
        self.assertIn("$80.00 on shard 3", msg)
        self.assertIn("transfers not enabled", msg)


@mock.patch.object(trader_mod.config, "TRADES_LOG", new_callable=lambda: __import__("pathlib").Path(__import__("tempfile").gettempdir()) / "arb_test_trades.jsonl")
@mock.patch.object(trader_mod.time, "sleep", lambda _s: None)
@mock.patch.object(trader_mod.config, "TRADE_ORDER", "polymarket_first")
class PolymarketFirstTests(unittest.TestCase):
    def test_polymarket_goes_first_then_kalshi_for_what_filled(self, *_):
        sent = []

        class Logging(FakeVenue):
            def buy(self, *a):
                sent.append(self.name)
                return super().buy(*a)
        k = Logging("kalshi", yes=[(0.40, 500)])
        p = Logging("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = [(0.50, 30)]                          # Polymarket only fills 30 by the time it arrives
        res = t.execute(plan["id"])
        self.assertEqual(sent[0], "polymarket")
        self.assertEqual(k.orders[0][2], 30)                  # Kalshi bought for exactly what filled
        self.assertEqual((res["status"], res["hedged_pairs"]), ("ok", 30))

    def test_polymarket_miss_trades_nothing(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 500)])
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        t = make(k, p)
        plan = t.prepare(LEGS)
        p.book["no"] = []                                     # gone before the order arrived
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual(k.orders, [])


class ParallelChecksTests(unittest.TestCase):
    def test_checks_on_both_sites_run_at_once(self):
        import threading
        barrier = threading.Barrier(2, timeout=2)            # each site's book read waits for the other's

        class Waiting(FakeVenue):
            def levels(self, mid):
                barrier.wait()
                return super().levels(mid)
        t = make(Waiting("kalshi", yes=[(0.40, 500)]), Waiting("polymarket", no=[(0.50, 500)]))
        self.assertEqual(t.prepare(LEGS)["size"], 107)
