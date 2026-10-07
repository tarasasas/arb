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

    def sync(self, read_at, k_shares=100, p_shares=100):
        """An account sync that reads 100 + 100 shares of the pair (still listed, or bought again)."""
        kc = type("C", (), {"title": "k"})()
        pairs = [("KX-DAMON", "oscar-damon", kc, kc, 1.0)]
        kpos = {"KX-DAMON": {"side": "yes", "shares": k_shares, "paid": 45.0}}
        ppos = {"oscar-damon": {"side": "no", "shares": p_shares, "paid": 50.0}}
        self.m.sync_from_accounts(pairs, kpos, ppos, [], lambda c: {"game": "Matt Damon", "tab": "", "closes": None},
                                  read_at)

    def test_a_site_still_listing_sold_shares_does_not_reopen_the_arb(self):
        # The report: sold with Sell, then the next position check (the sites' lists lagging the fills) rebuilt the
        # arb from the old shares: back among the open ones, "sold for" gone.
        a = self.m.save({**held(), "id": "acct-KX-DAMON-oscar-damon"})
        self.m.apply_sale(a["id"], {"kalshi": (100, 58.0, 0.2), "polymarket": (100, 45.0, 0.2)})
        self.sync(read_at=__import__("time").time() + 1)                         # read after the sale finished
        self.assertEqual(len(self.m.items), 1)
        closed = self.m.items[0]
        self.assertEqual((closed["closed"]["sold_for"], closed["legs"][0]["shares"]), (102.6, 0))
        row = self.m._rows([closed])[0]
        self.assertEqual((row["phase"], row["paid"], row["profit"]), ("sold", 95.0, 7.6))
        self.assertAlmostEqual(row["roi"], 7.6 / 95.0)

    def test_the_pair_bought_again_later_is_a_new_arb(self):
        a = self.m.save({**held(), "id": "acct-KX-DAMON-oscar-damon"})
        self.m.apply_sale(a["id"], {"kalshi": (100, 58.0, 0.2), "polymarket": (100, 45.0, 0.2)})
        self.m.touched = {k: v - myarbs.SALE_GRACE_SECS - 1 for k, v in self.m.touched.items()}
        self.sync(read_at=__import__("time").time())
        self.assertEqual(sorted(bool(x.get("closed")) for x in self.m.items), [False, True])
        new = next(x for x in self.m.items if not x.get("closed"))
        self.assertEqual((new["id"], new.get("realized"), new["legs"][0]["shares"]), ("acct-KX-DAMON-oscar-damon", None, 100))

    def test_a_fraction_left_over_still_closes_it(self):
        a = self.m.save(held(6, 0.57, 6.04, 5.05))                     # Polymarket filled 6.04 by dollar amount
        self.m.apply_sale(a["id"], {"kalshi": (6, 0.36, 0.03), "polymarket": (6, 5.46, 0.03)})
        c = self.m.items[0]["closed"]
        self.assertIn("0.04 Polymarket NO left over", c["why"])
        self.assertEqual(c["sold_for"], 5.76)

    def test_a_whole_share_left_unsold_stays_open_for_balance(self):
        a = self.m.save(held(6, 0.57, 6, 5.01))
        self.m.apply_sale(a["id"], {"kalshi": (6, 0.36, 0.03)})         # Polymarket's buyers were gone
        self.assertNotIn("closed", self.m.items[0])

    def test_sold_shares_the_sync_had_put_back_close_with_what_they_sold_for(self):
        # An arb an earlier version reopened that way: its sale is in `sales`, its legs back at full size. Once the
        # sites' lists show the shares gone, the check closes it, and the sale's price is still known.
        a = self.m.save(held())
        self.m.apply_sale(a["id"], {"kalshi": (60, 34.8, 0.01), "polymarket": (60, 27.0, 0.01)})
        x = self.m.items[0]
        x["legs"][0].update(shares=100, paid=45.0)
        x["legs"][1].update(shares=100, paid=50.0)
        self.m.touched = {}
        self.m._live = {("kalshi", "KX-DAMON"): {"state": "open"}, ("polymarket", "oscar-damon"): {"state": "open"}}
        self.m.reconcile({}, {}, read_at=__import__("time").time())
        c = self.m.items[0]["closed"]
        self.assertEqual((c["sold_for"], c["profit"]), (61.78, x["realized"]))

    def test_a_sold_out_arb_saved_open_by_an_earlier_version_closes_on_load(self):
        a = self.m.save(held(6, 0.57, 6.04, 5.05))
        x = self.m.items[0]
        x["legs"][0]["shares"], x["legs"][1]["shares"] = 0, 0.04
        x["sales"], x["realized"] = [{"proceeds": 5.76, "cost": 5.58, "profit": 0.18}], 0.18
        self.m._save()
        again = myarbs.MyArbs(self.m.path)
        self.assertEqual(again.items[0]["closed"]["sold_for"], 5.76)

    def test_a_sale_takes_the_legs_fees_with_its_shares(self):
        a = self.m.save(held())
        self.m.items[0]["legs"][0]["fees"] = 0.10                 # Kalshi: $45.00 includes $0.10 of fees
        self.m.apply_sale(a["id"], {"kalshi": (60, 34.8, 0.01), "polymarket": (60, 27.0, 0.01)})
        self.assertEqual(self.m.items[0]["legs"][0]["fees"], 0.04)
        self.m.apply_sale(a["id"], {"kalshi": (40, 23.2, 0.01), "polymarket": (40, 18.0, 0.01)})
        k = self.m.items[0]["legs"][0]
        self.assertEqual((k["paid"], k["fees"]), (0.0, 0.0))  # it showed "real cost: $-0.10 + $0.10 fees"

    def test_older_closed_arbs_get_what_their_sales_sold_for(self):
        a = self.m.save(held())
        self.m.apply_sale(a["id"], {"kalshi": (100, 58.0, 0.2), "polymarket": (100, 45.0, 0.2)})
        x = self.m.items[0]
        x["closed"] = {"time": x["closed"]["time"], "why": "you sold 100 Kalshi YES and 100 Polymarket NO"}
        x["legs"][0]["fees"] = 0.1
        self.m._save()
        again = myarbs.MyArbs(self.m.path)
        c = again.items[0]["closed"]
        self.assertEqual((c["sold_for"], c["profit"], again.items[0]["legs"][0]["fees"]), (102.6, x["realized"], 0.0))

    def test_worth_now_walks_the_books_like_sell(self):
        # The case that showed it: Polymarket's best YES offer is 4c but holds 0.11 shares (next 9c), so its NO
        # sells for 96c only 0.11 times and 91c for the rest. The best price alone made Worth now $6.07, Sell $5.77.
        kalshi = {"yes": [(0.11, 100)], "no": [(0.94, 41.78)]}                      # Kalshi YES bid 6c
        poly = {"yes": [(0.04, 0.11), (0.09, 109.89)], "no": [(0.97, 8333)]}
        a = self.m.save(held(6, 0.57, 6, 5.01))
        self.m._live = {("kalshi", "KX-DAMON"): {"state": "open"}, ("polymarket", "oscar-damon"): {"state": "open"}}
        self.m._books = {("kalshi", "KX-DAMON"): kalshi, ("polymarket", "oscar-damon"): poly}
        self.m.fee_coef = lambda ex, mid: 0.07 if ex == "kalshi" else 0.0695
        row = self.m._rows([a])[0]
        self.assertEqual((row["sell_now"], row["sell_profit"], row["sell_pairs"]), (5.77, 0.19, 6))
        s = Scanner(Path(self.dir.name) / "sell.json", {"kalshi": FakeVenue(kalshi), "polymarket": FakeVenue(poly)})
        plan = sellearly.EarlySeller(s).preview(s.my_arbs.save(held(6, 0.57, 6, 5.01))["id"])
        self.assertEqual((plan["proceeds"], plan["profit"], plan["n"]), (row["sell_now"], row["sell_profit"], 6))

    def test_no_books_no_estimate(self):
        a = self.m.save(held())
        self.m._live = {("kalshi", "KX-DAMON"): {"state": "open"}, ("polymarket", "oscar-damon"): {"state": "open"}}
        self.m._books = {("kalshi", "KX-DAMON"): KALSHI}                             # Polymarket's didn't load
        self.assertIsNone(self.m._rows([a])[0]["sell_profit"])

    def test_books_are_read_for_open_arbs_only(self):
        a = self.m.save(held())
        b = self.m.save({**held(), "legs": [{**l, "market_id": l["market_id"] + "-2"} for l in held()["legs"]]})
        self.m.apply_sale(b["id"], {"kalshi": (100, 58.0, 0.2), "polymarket": (100, 45.0, 0.2)})       # closed
        self.m._live = {(l["exchange"], l["market_id"]): {"state": "open"} for x in (a, b) for l in x["legs"]}
        asked = []
        kc = type("K", (), {"books_by_ticker": lambda _s, ts: asked.append(sorted(ts)) or {t: KALSHI for t in ts}})()
        pc = type("P", (), {"live_levels": lambda _s, slug: asked.append(slug) or POLY})()
        self.m._refresh_books([dict(x) for x in self.m.items], kc, pc)
        self.assertEqual(asked, [["KX-DAMON"], "oscar-damon"])
        self.assertEqual(set(self.m._books), {("kalshi", "KX-DAMON"), ("polymarket", "oscar-damon")})

if __name__ == "__main__":
    unittest.main()


class BookVenue(FakeVenue):
    """A FakeVenue with a book (and open state) per market; sales on a market in `fail` raise."""

    def __init__(self, books, shut=(), fail=()):
        super().__init__({})
        self.books, self.shut, self.fail = books, set(shut), set(fail)

    def market_info(self, mid):
        return {**super().market_info(mid), "open": mid not in self.shut}

    def levels(self, mid):
        return self.books[mid]

    def sell(self, mid, side, qty, min_price, coef):
        if mid in self.fail:
            raise RuntimeError("refused")
        return super().sell(mid, side, qty, min_price, coef)


def pair_of(mid, game):
    a = held()
    a["game"] = game
    for l in a["legs"]:
        l["market_id"] = f"{l['market_id']}-{mid}"
    return a


@mock.patch.object(sellearly.config, "TRADES_LOG", Path(tempfile.gettempdir()) / "arb-test-sellearly-trades.jsonl")
@mock.patch.object(sellearly.config, "SECOND_LEG_RETRY_PAUSE", 0)
class SellAllTests(unittest.TestCase):
    """Three arbs: "win" makes money on the first 60 pairs (as above), "lose" sells under cost (Kalshi bid 48c),
    "shut" has a market that isn't trading."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        lose_k = {"yes": [(0.60, 100)], "no": [(0.52, 500)]}
        k_books = {"KX-DAMON-win": KALSHI, "KX-DAMON-lose": lose_k, "KX-DAMON-shut": KALSHI}
        p_books = {"oscar-damon-win": POLY, "oscar-damon-lose": POLY, "oscar-damon-shut": POLY}
        self.k, self.p = BookVenue(k_books, shut={"KX-DAMON-shut"}), BookVenue(p_books)
        self.s = Scanner(Path(self.dir.name) / "m.json", {"kalshi": self.k, "polymarket": self.p})
        self.ids = {g: self.s.my_arbs.save(pair_of(g, g))["id"] for g in ("win", "lose", "shut")}
        self.seller = sellearly.EarlySeller(self.s)

    def tearDown(self):
        self.dir.cleanup()

    def arb(self, g):
        return next(a for a in self.s.my_arbs.items if a["game"] == g)

    def test_the_preview_totals_the_profitable_ones_and_everything(self):
        b = self.seller.preview_all()
        win = sellearly.sale_value(pair_of("win", "win"), {"kalshi": KALSHI, "polymarket": POLY},
                                   {"kalshi": 0.07, "polymarket": 0.0695})
        self.assertEqual((b["profitable"]["arbs"], b["profitable"]["pairs"]), (1, 60))
        self.assertAlmostEqual(b["profitable"]["profit"], round(win["profit"], 2))
        self.assertEqual((b["everything"]["arbs"], b["everything"]["pairs"], b["unsellable"]), (2, 200, 1))
        self.assertLess(b["everything"]["profit"], 0)                     # "lose" sells under what it cost
        self.assertEqual(b["everything"]["hold_profit"], 10.0)            # 200 pairs at 95c paying $1
        shut = next(r for r in b["arbs"] if r["game"] == "shut")
        self.assertIn("Kalshi market isn't open", shut["why"])

    def test_selling_the_profitable_ones_leaves_the_rest(self):
        b = self.seller.preview_all()
        res = self.seller.execute_all(b["id"], "profitable")
        self.assertEqual((res["status"], len(res["results"])), ("ok", 1))
        self.assertEqual({o[1] for o in self.k.orders + self.p.orders}, {"KX-DAMON-win", "oscar-damon-win"})
        self.assertEqual(self.arb("win")["legs"][0]["shares"], 40)
        self.assertEqual(self.arb("lose")["legs"][0]["shares"], 100)
        with self.assertRaisesRegex(TradeError, "already used"):
            self.seller.execute_all(b["id"], "profitable")

    def test_selling_everything_takes_the_loss_too(self):
        res = self.seller.execute_all(self.seller.preview_all()["id"], "everything")
        self.assertEqual([r["status"] for r in res["results"]], ["ok", "ok"])
        lose = self.arb("lose")
        self.assertTrue(lose["closed"])
        self.assertLess(lose["closed"]["profit"], 0)
        # Polymarket's book is the thinner one, so it went first; Kalshi's second leg (bid 48c) could go 5c under the
        # preview instead of stopping at break-even, which a sale at a loss never reaches
        self.assertEqual(next(o for o in self.p.orders if o[1] == "oscar-damon-lose")[4], 0.45)
        self.assertEqual(next(o for o in self.k.orders if o[1] == "KX-DAMON-lose")[4], 0.43)
        self.assertNotIn("closed", self.arb("shut"))

    def test_one_arb_failing_does_not_stop_the_rest(self):
        self.k.fail = {"KX-DAMON-win"}
        res = self.seller.execute_all(self.seller.preview_all()["id"], "everything")
        self.assertEqual(sorted(r["status"] for r in res["results"]), ["error", "ok"])
        self.assertEqual(res["status"], "partial")
        self.assertTrue(self.arb("lose")["closed"])

    def test_the_list_has_the_totals_the_card_adds_up(self):
        m = self.s.my_arbs
        m._live = {(l["exchange"], l["market_id"]): {"state": "open"} for a in m.items for l in a["legs"]}
        m._books = {("kalshi", k): v for k, v in self.k.books.items()} | {("polymarket", k): v for k, v in self.p.books.items()}
        m.fee_coef = lambda ex, mid: 0.07 if ex == "kalshi" else 0.0695
        rows = {r["game"]: r for r in m._rows([dict(a) for a in m.items])}
        b = self.seller.preview_all()
        self.assertAlmostEqual(rows["win"]["sell_profit"], b["profitable"]["profit"])
        self.assertAlmostEqual(rows["win"]["sell_all_profit"] + rows["lose"]["sell_all_profit"], b["everything"]["profit"], 2)
        self.assertEqual(rows["win"]["hold_all_profit"] + rows["lose"]["hold_all_profit"], b["everything"]["hold_profit"])

    def test_sells_just_the_ones_you_pick_each_its_own_way(self):
        b = self.seller.preview_all()
        ids = {r["game"]: r["arb_id"] for r in b["arbs"]}
        res = self.seller.execute_all(b["id"], picks=[{"arb_id": ids["win"], "sale": "best"},
                                                       {"arb_id": ids["lose"], "sale": "everything"}])
        self.assertEqual([r["status"] for r in res["results"]], ["ok", "ok"])
        self.assertEqual(self.arb("win")["legs"][0]["shares"], 40)          # the 60 that made money
        self.assertTrue(self.arb("lose")["closed"])                          # every pair, at a loss
        b2 = self.seller.preview_all()
        only = next(r["arb_id"] for r in b2["arbs"] if r["game"] == "win")
        self.seller.execute_all(b2["id"], picks=[{"arb_id": only, "sale": "everything"}])
        self.assertEqual(self.arb("win")["legs"][0]["shares"], 0)

    def test_a_choice_the_preview_cant_do_is_refused_before_anything_is_sent(self):
        b = self.seller.preview_all()
        lose = next(r["arb_id"] for r in b["arbs"] if r["game"] == "lose")
        shut = next(r["arb_id"] for r in b["arbs"] if r["game"] == "shut")
        for picks, why in (([{"arb_id": lose, "sale": "best"}], "can't sell that way"),     # selling now loses
                           ([{"arb_id": shut, "sale": "everything"}], "can't sell that way"),
                           ([{"arb_id": "nope", "sale": "best"}], "Refresh prices"), ([], "choose at least one")):
            with self.assertRaisesRegex(TradeError, why):
                self.seller.execute_all(b["id"], picks=picks)
        self.assertEqual(self.k.orders + self.p.orders, [])
        self.seller.execute_all(b["id"], picks=[{"arb_id": lose, "sale": "everything"}])       # still usable
        self.assertTrue(self.arb("lose")["closed"])

    def test_choosing_has_two_minutes(self):
        b = self.seller.preview_all()
        self.seller.plans[b["id"]]["created"] -= 90                         # past a single Sell's 30 seconds
        self.seller.preview(self.ids["win"])                                # (keeping another plan prunes old ones)
        res = self.seller.execute_all(b["id"], which="profitable")
        self.assertEqual(res["status"], "ok")
        b = self.seller.preview_all()
        self.seller.plans[b["id"]]["created"] -= sellearly.SELL_ALL_TTL_SECS + 1
        with self.assertRaisesRegex(TradeError, "expired"):
            self.seller.execute_all(b["id"], which="profitable")
