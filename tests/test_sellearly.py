import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arb import myarbs, sellearly
from arb.model import total_fee
from arb.trader import TradeError
from arb.venues import Fill


class FakeVenue:
    """levels: {"yes": [(price, qty)], "no": [...]}, the asks to buy each side. A sale fills at its minimum price
    (fills: how many fill per order, default all)."""

    def __init__(self, levels, open_=True, fills=None, min_qty=1.0):
        self.lv, self.open, self.fills, self.min_qty, self.orders = levels, open_, fills, min_qty, []

    def market_info(self, mid):
        return {"open": self.open, "tick": lambda p: 0.01, "min_qty": self.min_qty, "shard": 0, "fee_coef": 0.0695}

    def levels(self, mid):
        return self.lv

    def sell(self, mid, side, qty, min_price, coef):
        self.orders.append(("sell", mid, side, qty, min_price))
        got = qty if self.fills is None else min(qty, self.fills)
        return Fill(qty=got, amount=round(got * min_price, 4), fee=0.01 if got else 0.0, order_id="o1")


class Scanner:
    def __init__(self, path, venues):
        self.my_arbs = myarbs.MyArbs(path)
        self.trader = type("T", (), {"venues": venues})()
        self.kalshi = type("K", (), {"series_fee_coefs": lambda _s: {}})()

    def find_any_contract(self, ex, mid):
        return None


def held(k_shares=100, k_paid=45.0, p_shares=100, p_paid=50.0):
    """Kalshi YES at 45c + Polymarket NO at 50c (fees in): 95c a pair for a $1 payout."""
    return {"source": "account", "game": "Matt Damon", "payout": 1.0, "legs": [
        {"exchange": "kalshi", "market_id": "KX-DAMON", "side": "yes", "title": "k", "shares": k_shares, "paid": k_paid},
        {"exchange": "polymarket", "market_id": "oscar-damon", "side": "no", "title": "p", "shares": p_shares,
         "paid": p_paid}]}


# Kalshi YES now sells for 58c (60 shares), then 50c; Polymarket NO for 45c (100 shares).
KALSHI = {"yes": [(0.60, 100)], "no": [(0.42, 60), (0.50, 100)]}
POLY = {"yes": [(0.55, 100)], "no": [(0.47, 100)]}


@mock.patch.object(sellearly.config, "TRADES_LOG", Path(tempfile.gettempdir()) / "arb-test-sellearly-trades.jsonl")
@mock.patch.object(sellearly.config, "SECOND_LEG_RETRY_PAUSE", 0)
class SellEarlyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def make(self, a=None, kalshi=None, poly=None):
        self.k, self.p = kalshi or FakeVenue(KALSHI), poly or FakeVenue(POLY)
        s = Scanner(Path(tempfile.mkdtemp(dir=self.dir.name)) / "my_arbs.json", {"kalshi": self.k, "polymarket": self.p})
        entry = s.my_arbs.save(a or held())
        return s, sellearly.EarlySeller(s), entry["id"]

    def test_sells_the_pairs_that_make_money_and_keeps_the_rest(self):
        v = sellearly.sale_value(held(), {"kalshi": KALSHI, "polymarket": POLY}, {"kalshi": 0.07, "polymarket": 0.0695})
        # 58c + 45c less fees beats 95c a pair; at 50c + 45c the deeper 40 would sell under what they cost
        self.assertEqual((v["pairs"], v["n"], v["all"]["n"]), (100, 60, 100))
        want = 60 * 0.58 - total_fee("kalshi", [(0.58, 60)], 0.07) + 60 * 0.45 - total_fee("polymarket", [(0.45, 60)], 0.0695)
        self.assertAlmostEqual(v["proceeds"], want, 6)
        self.assertAlmostEqual(v["profit"], want - 60 * 0.95, 6)
        self.assertGreater(v["profit"], v["all"]["profit"])

    def test_preview_compares_selling_now_with_holding(self):
        s, seller, aid = self.make()
        plan = seller.preview(aid)
        self.assertTrue(plan["ok"])
        self.assertEqual((plan["n"], plan["kept"], plan["first"]), (60, 40, "kalshi"))   # Kalshi's book is thinner
        self.assertEqual((plan["hold_payout"], plan["hold_profit"]), (60.0, 3.0))
        self.assertGreater(plan["profit"], 0)
        self.assertEqual((plan["legs"]["kalshi"]["limit"], plan["legs"]["polymarket"]["limit"]), (0.58, 0.45))

    def test_sells_the_thinner_book_first_then_the_other_for_what_sold(self):
        s, seller, aid = self.make()
        plan = seller.preview(aid)
        res = seller.execute(plan["id"])
        self.assertEqual(res["status"], "ok")
        self.assertEqual(self.k.orders, [("sell", "KX-DAMON", "yes", 60, 0.58)])
        self.assertEqual(self.p.orders[0][:4], ("sell", "oscar-damon", "no", 60))
        self.assertEqual(self.p.orders[0][4], 0.40)            # down to 5c under the preview, above break-even
        a = s.my_arbs.items[0]
        k, p = a["legs"]
        self.assertEqual((k["shares"], k["paid"], p["shares"], p["paid"]), (40, 18.0, 40, 20.0))
        self.assertAlmostEqual(a["realized"], round(60 * 0.58 - 0.01 + 60 * 0.40 - 0.01 - 57.0, 2))
        self.assertEqual(len(a["sales"]), 1)
        self.assertNotIn("closed", a)
        self.assertIn("40 pairs are still held", res["text"])
        row = myarbs.summarize(a)                                # locked profit on the rest + what the sale made
        self.assertAlmostEqual(row["profit"], round(40 - 38 + a["realized"], 2))

    def test_selling_everything_closes_it_with_what_it_sold_for(self):
        s, seller, aid = self.make(kalshi=FakeVenue({"yes": [(0.60, 100)], "no": [(0.42, 500)]}))
        plan = seller.preview(aid)
        self.assertEqual((plan["n"], plan["kept"]), (100, 0))
        seller.execute(plan["id"])
        a = s.my_arbs.items[0]
        self.assertEqual(a["closed"]["profit"], a["realized"])
        self.assertAlmostEqual(a["closed"]["sold_for"], a["sales"][0]["proceeds"])
        self.assertEqual(s.my_arbs._rows([a])[0]["phase"], "sold")

    def test_not_offered_when_selling_now_would_lose(self):
        s, seller, aid = self.make(kalshi=FakeVenue({"yes": [(0.60, 100)], "no": [(0.52, 500)]}))   # bid 48c
        plan = seller.preview(aid)
        self.assertFalse(plan["ok"])
        self.assertIn("would lose", plan["why"])
        with self.assertRaisesRegex(TradeError, "Not selling"):
            seller.execute(plan["id"])
        self.assertEqual(self.k.orders, [])

    def test_a_closed_market_or_no_buyers(self):
        s, seller, aid = self.make(poly=FakeVenue(POLY, open_=False))
        self.assertIn("Polymarket market isn't open", seller.preview(aid)["why"])
        s, seller, aid = self.make(poly=FakeVenue({"yes": [], "no": [(0.47, 100)]}))
        self.assertIn("no buyers on Polymarket", seller.preview(aid)["why"])

    def test_first_leg_unsold_changes_nothing(self):
        s, seller, aid = self.make(kalshi=FakeVenue(KALSHI, fills=0))
        res = seller.execute(seller.preview(aid)["id"])
        self.assertEqual(res["status"], "no_fill")
        self.assertEqual(self.p.orders, [])
        self.assertEqual(s.my_arbs.items[0]["legs"][0]["shares"], 100)

    def test_second_leg_short_after_retries_is_left_for_balance(self):
        s, seller, aid = self.make(poly=FakeVenue(POLY, fills=0))
        res = seller.execute(seller.preview(aid)["id"])
        self.assertEqual(res["status"], "partial")
        self.assertEqual(len(self.p.orders), 3)                 # first try + 2 retries
        self.assertIn("Balance", res["text"])
        a = s.my_arbs.items[0]
        self.assertEqual((a["legs"][0]["shares"], a["legs"][1]["shares"]), (40, 100))
        self.assertEqual(myarbs.summarize(a)["unhedged"][0]["shares"], 60)

    def test_a_fraction_the_other_site_cant_sell_is_not_a_miss(self):
        poly = FakeVenue({"yes": [(0.55, 59.5)], "no": [(0.47, 100)]}, min_qty=0.01)       # Polymarket thinner: first
        s, seller, aid = self.make(kalshi=FakeVenue({"yes": [(0.60, 100)], "no": [(0.42, 500)]}), poly=poly)
        plan = seller.preview(aid)
        self.assertEqual((plan["first"], plan["n"]), ("polymarket", 59))
        poly.fills = 58.5                                                                     # sells a fraction short
        res = seller.execute(plan["id"])
        self.assertEqual((res["status"], self.k.orders[0][3]), ("ok", 58))                  # Kalshi: whole contracts

    def test_a_preview_is_used_once(self):
        s, seller, aid = self.make()
        plan = seller.preview(aid)
        seller.execute(plan["id"])
        with self.assertRaisesRegex(TradeError, "already used"):
            seller.execute(plan["id"])


class FloorPriceTests(unittest.TestCase):
    def test_the_lowest_cent_that_covers_it_after_the_fee(self):
        x = sellearly.floor_price("polymarket", 22.21, 60, 0.0695)
        self.assertGreaterEqual(x * 60 - total_fee("polymarket", [(x, 60)], 0.0695), 22.21)
        y = round(x - 0.01, 2)
        self.assertLess(y * 60 - total_fee("polymarket", [(y, 60)], 0.0695), 22.21)
        self.assertEqual(sellearly.floor_price("kalshi", -5, 60, 0.07), 0.01)       # already covered


class BookkeepingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.m = myarbs.MyArbs(Path(self.dir.name) / "my_arbs.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_an_account_sync_keeps_what_sales_made(self):
        a = self.m.save({**held(), "id": "acct-KX-DAMON-oscar-damon"})
        self.m.apply_sale(a["id"], {"kalshi": (10, 5.8, 0.1), "polymarket": (10, 4.5, 0.1)})
        realized = self.m.items[0]["realized"]
        self.m.save({**held(90, 40.5, 90, 45.0), "id": a["id"]})                     # the next sync, from the accounts
        self.assertEqual((self.m.items[0]["realized"], len(self.m.items[0]["sales"])), (realized, 1))

    def test_a_sale_in_flight_is_not_undone_by_a_position_read_from_before_it(self):
        a = self.m.save(held())
        kpos = {"KX-DAMON": {"side": "yes", "shares": 100, "paid": 45.0}}
        ppos = {"oscar-damon": {"side": "no", "shares": 100, "paid": 50.0}}
        read_at = __import__("time").time()
        self.m.hold(a)
        self.m.apply_sale(a["id"], {"kalshi": (60, 34.8, 0.01), "polymarket": (60, 27.0, 0.01)})
        self.m.release(a)
        self.m._live = {("kalshi", "KX-DAMON"): {"state": "open"}, ("polymarket", "oscar-damon"): {"state": "open"}}
        self.assertEqual(self.m.reconcile(kpos, ppos, read_at=read_at), [])            # read before the sale
        self.assertEqual(self.m.items[0]["legs"][0]["shares"], 40)
        later = {"KX-DAMON": {"side": "yes", "shares": 40, "paid": 18.0}}
        self.assertEqual(self.m.reconcile(later, {"oscar-damon": {"side": "no", "shares": 40}},
                                          read_at=__import__("time").time() + 1), [])  # agrees: nothing to do

    def test_a_closed_arb_stays_when_the_pair_is_bought_again(self):
        a = self.m.save(held())
        self.m.apply_sale(a["id"], {"kalshi": (100, 58.0, 0.2), "polymarket": (100, 45.0, 0.2)})
        self.m.save({**held(), "id": "acct-KX-DAMON-oscar-damon"})
        self.assertEqual(len(self.m.items), 2)
        new = next(x for x in self.m.items if not x.get("closed"))
        self.assertIsNone(new.get("realized"))

    def test_the_list_estimates_selling_now_at_the_best_bids(self):
        a = self.m.save(held())
        self.m._live = {("kalshi", "KX-DAMON"): {"state": "open", "bid": {"yes": 0.58}},
                        ("polymarket", "oscar-damon"): {"state": "open", "bid": {"no": 0.45}}}
        row = self.m._rows([a])[0]
        want = (100 * 0.58 - total_fee("kalshi", [(0.58, 100)], 0.07)
                + 100 * 0.45 - total_fee("polymarket", [(0.45, 100)], 0.0695))
        self.assertAlmostEqual(row["sell_now"], round(want, 2))
        self.assertAlmostEqual(row["sell_profit"], round(want - 95.0, 2))


if __name__ == "__main__":
    unittest.main()
