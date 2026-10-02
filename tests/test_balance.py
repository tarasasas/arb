import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arb import balance, myarbs
from arb.trader import TradeError
from arb.venues import Fill


class FakeVenue:
    """levels: {"yes": [(price, qty)], "no": [...]}, prices to buy each side."""

    def __init__(self, levels, min_qty=1.0, open_=True, cash=1000.0, fills=None):
        self.lv, self.min_qty, self.open, self.cash, self.orders = levels, min_qty, open_, cash, []
        self.fills = fills

    def market_info(self, mid):
        return {"open": self.open, "tick": lambda p: 0.01, "min_qty": self.min_qty, "shard": 0, "fee_coef": 0.0695}

    def levels(self, mid):
        return self.lv

    def balance(self, shard=None):
        return self.cash

    def _fill(self, kind, mid, side, qty, price):
        self.orders.append((kind, mid, side, qty, price))
        got = qty if self.fills is None else self.fills
        return Fill(qty=got, amount=round(got * price, 4), fee=0.01 if got else 0.0, order_id="o1")

    def buy(self, mid, side, qty, limit, coef):
        return self._fill("buy", mid, side, qty, limit)

    def sell(self, mid, side, qty, min_price, coef):
        return self._fill("sell", mid, side, qty, min_price)


class Scanner:
    def __init__(self, path, venues):
        self.my_arbs = myarbs.MyArbs(path)
        self.trader = type("T", (), {"venues": venues})()
        self.kalshi = type("K", (), {"series_fee_coefs": lambda _s: {}})()

    def find_any_contract(self, ex, mid):
        return None


def arb(k_shares, k_paid, p_shares, p_paid):
    return {"source": "account", "game": "Matt Damon", "payout": 1.0, "legs": [
        {"exchange": "kalshi", "market_id": "KXOSCAR-DAMON", "side": "yes", "title": "k", "shares": k_shares, "paid": k_paid},
        {"exchange": "polymarket", "market_id": "oscar-damon", "side": "no", "title": "p", "shares": p_shares, "paid": p_paid}]}


@mock.patch.object(balance.config, "TRADES_LOG", Path(tempfile.gettempdir()) / "arb-test-balance-trades.jsonl")
class BalanceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "my_arbs.json"

    def tearDown(self):
        self.dir.cleanup()

    def make(self, a, kalshi, poly):
        s = Scanner(self.path, {"kalshi": kalshi, "polymarket": poly})
        entry = s.my_arbs.save(a)
        return s, balance.Balancer(s), entry["id"]

    def test_a_fraction_extra_on_polymarket_is_sold_there(self):
        # 10 Kalshi YES vs 10.1 Polymarket NO; Kalshi takes whole contracts only, so buying 0.1 there can't happen
        k = FakeVenue({"yes": [(0.93, 50)], "no": [(0.08, 50)]})
        p = FakeVenue({"yes": [(0.06, 50)], "no": [(0.95, 50)]}, min_qty=0.01)
        s, b, aid = self.make(arb(10, 9.25, 10.1, 0.45), k, p)
        plan = b.preview(aid)
        self.assertEqual((plan["extra"], plan["extra_exchange"], plan["recommended"]), (0.1, "Polymarket", "sell"))
        self.assertFalse(plan["options"]["buy"]["ok"])
        self.assertIn("smallest order", plan["options"]["buy"]["why"])
        sell = plan["options"]["sell"]
        self.assertEqual((sell["qty"], sell["limit"]), (0.1, 0.94))         # NO sells into YES buyers: 1 - 0.06
        res = b.execute(plan["id"], "sell")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(p.orders, [("sell", "oscar-damon", "no", 0.1, 0.94)])
        legs = {l["exchange"]: l for l in s.my_arbs.items[0]["legs"]}
        self.assertEqual(legs["polymarket"]["shares"], 10.0)
        self.assertEqual(myarbs.summarize(s.my_arbs.items[0])["leftover"], [])
        self.assertIn("Balanced: sold 0.1 extra NO", s.my_arbs.items[0]["note"])

    def test_a_fraction_extra_on_kalshi_is_matched_on_polymarket(self):
        # 9 Kalshi YES vs 8.8 Polymarket NO
        k = FakeVenue({"yes": [(0.93, 50)], "no": [(0.08, 50)]})
        p = FakeVenue({"yes": [(0.06, 50)], "no": [(0.05, 50)]}, min_qty=0.01)
        s, b, aid = self.make(arb(9, 8.44, 8.8, 0.40), k, p)
        plan = b.preview(aid)
        self.assertEqual((plan["extra"], plan["recommended"]), (0.2, "buy"))
        self.assertFalse(plan["options"]["sell"]["ok"])                     # 0.2 is under Kalshi's 1-contract minimum
        buy = plan["options"]["buy"]
        self.assertEqual((buy["exchange"], buy["side"], buy["qty"], buy["limit"]), ("polymarket", "no", 0.2, 0.05))
        b.execute(plan["id"], "buy")
        legs = {l["exchange"]: l for l in s.my_arbs.items[0]["legs"]}
        self.assertEqual((legs["polymarket"]["shares"], legs["polymarket"]["paid"]), (9.0, 0.42))
        self.assertEqual(myarbs.summarize(s.my_arbs.items[0])["pairs"], 9.0)

    def test_recommends_whichever_leaves_more_money(self):
        # 5 extra Kalshi YES. Selling: YES bids at 1 - 0.60 = 0.40. Buying NO on Polymarket at 0.50 makes pairs paying $1.
        k = FakeVenue({"yes": [(0.45, 50)], "no": [(0.60, 50)]})
        p = FakeVenue({"yes": [(0.52, 50)], "no": [(0.50, 50)]}, min_qty=0.01)
        _, b, aid = self.make(arb(15, 6.75, 10, 5.0), k, p)
        plan = b.preview(aid)
        sell, buy = plan["options"]["sell"], plan["options"]["buy"]
        self.assertGreater(buy["value"], sell["value"])                    # ~$2.50 left vs ~$2.00 back
        self.assertEqual(plan["recommended"], "buy")

    def test_not_enough_cash_or_closed_market_rules_an_option_out(self):
        k = FakeVenue({"yes": [(0.45, 50)], "no": [(0.60, 50)]}, open_=False)
        p = FakeVenue({"yes": [(0.52, 50)], "no": [(0.50, 50)]}, cash=1.0)
        _, b, aid = self.make(arb(15, 6.75, 10, 5.0), k, p)
        plan = b.preview(aid)
        self.assertIn("isn't open", plan["options"]["sell"]["why"])
        self.assertIn("not enough cash", plan["options"]["buy"]["why"])
        self.assertIsNone(plan["recommended"])

    def test_nothing_filled_changes_nothing_and_a_plan_is_used_once(self):
        k = FakeVenue({"yes": [(0.45, 50)], "no": [(0.60, 50)]}, fills=0)
        p = FakeVenue({"yes": [(0.52, 50)], "no": [(0.50, 50)]})
        s, b, aid = self.make(arb(15, 6.75, 10, 5.0), k, p)
        plan = b.preview(aid)
        self.assertEqual(b.execute(plan["id"], "sell")["status"], "no_fill")
        self.assertEqual(s.my_arbs.items[0]["legs"][0]["shares"], 15)
        with self.assertRaises(TradeError):
            b.execute(plan["id"], "sell")

    def test_already_balanced(self):
        _, b, aid = self.make(arb(10, 9.25, 10, 0.45), FakeVenue({}), FakeVenue({}))
        with self.assertRaises(TradeError):
            b.preview(aid)


class ApplyBalanceTests(unittest.TestCase):
    def test_selling_extra_keeps_what_it_made_or_lost(self):
        with tempfile.TemporaryDirectory() as d:
            m = myarbs.MyArbs(Path(d) / "a.json")
            a = m.save(arb(15, 7.50, 10, 5.0))                 # Kalshi YES at $0.50 each
            m.apply_balance(a["id"], "kalshi", "sell", 5, 2.0, 0.05)
            k = m.items[0]["legs"][0]
            self.assertEqual((k["shares"], k["paid"], m.items[0]["realized"]), (10, 5.0, -0.55))
            s = myarbs.summarize(m.items[0])
            self.assertEqual((s["pairs"], s["paid"], s["profit"]), (10, 10.0, -0.55))


if __name__ == "__main__":
    unittest.main()


@mock.patch.object(balance.config, "MAX_TRADE_DOLLARS", 10.0)
class CapTests(unittest.TestCase):
    def test_buying_the_difference_stays_within_the_cap_per_trade(self):
        with tempfile.TemporaryDirectory() as d:
            s = Scanner(Path(d) / "a.json", {"kalshi": FakeVenue({"yes": [(0.45, 500)], "no": [(0.60, 500)]}),
                                             "polymarket": FakeVenue({"yes": [(0.52, 500)], "no": [(0.50, 500)]})})
            aid = s.my_arbs.save(arb(100, 45.0, 50, 25.0))["id"]          # 50 extra Kalshi YES
            buy = balance.Balancer(s).preview(aid)["options"]["buy"]
            self.assertLessEqual(buy["amount"] + buy["fee"], 10.0)
            self.assertGreater(buy["qty"], 15)
            self.assertIn("cap per trade", buy["text"])
            self.assertGreater(buy["left"], 0)
