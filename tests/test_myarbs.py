import tempfile
import unittest
from pathlib import Path

from arb import myarbs


def legs(ks=100, kp=13.0, ps=100, pp=85.0):
    return [{"exchange": "kalshi", "market_id": "K1", "side": "yes", "title": "k", "shares": ks, "paid": kp},
            {"exchange": "polymarket", "market_id": "p1", "side": "no", "title": "p", "shares": ps, "paid": pp}]


class FakeKalshi:
    def markets_by_ticker(self, tickers):
        return {"K1": {"status": "finalized", "result": "yes", "yes_bid_dollars": "0.99", "no_bid_dollars": "0.00"}}


class FakePM:
    def markets_by_slug(self, slugs):
        return {"p1": {"active": True, "closed": False, "bestBidQuote": {"value": "0.02"}, "bestAskQuote": {"value": "0.03"}}}


class MyArbsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "my_arbs.json"
        self.store = myarbs.MyArbs(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_numbers(self):
        s = myarbs.summarize({"payout": 1.0, "legs": legs()})
        self.assertEqual((s["pairs"], s["paid"], s["guaranteed"], s["profit"]), (100, 98.0, 100.0, 2.0))
        self.assertEqual(s["unhedged"], [])

    def test_extra_shares_on_one_side_count_as_worth_nothing(self):
        s = myarbs.summarize({"payout": 1.0, "legs": legs(ks=120, kp=15.6)})
        self.assertEqual((s["pairs"], s["guaranteed"], s["profit"]), (100, 100.0, -0.6))
        self.assertEqual(s["unhedged"], [{"exchange": "kalshi", "side": "yes", "shares": 20}])

    def test_save_edit_delete_persist(self):
        a = self.store.save({"game": "BTC", "legs": legs()})
        self.assertEqual(myarbs.MyArbs(self.path).items[0]["game"], "BTC")          # written to disk
        self.store.save({**a, "note": "filled at 13c", "legs": legs(ks=90, kp=11.7, ps=90, pp=76.5)})
        again = myarbs.MyArbs(self.path).items
        self.assertEqual((len(again), again[0]["note"], again[0]["legs"][0]["shares"]), (1, "filled at 13c", 90))
        self.store.delete(a["id"])
        self.assertEqual(myarbs.MyArbs(self.path).items, [])

    def test_bad_input_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save({"legs": legs()[:1]})
        with self.assertRaises(ValueError):
            self.store.save({"legs": legs(ks=-1)})

    def test_make_trade_result_is_recorded(self):
        plan = {"payout": 1.0, "legs": {"kalshi": legs()[0], "polymarket": legs()[1]}}
        result = {"hedged_pairs": 50, "legs_filled": {"kalshi": {"shares": 50, "paid": 6.5},
                                                      "polymarket": {"shares": 50, "paid": 42.5}}}
        a = self.store.add_from_trade(plan, result, {"game": "BTC", "tab": "Crypto", "closes": None})
        self.assertEqual((a["source"], a["legs"][0]["shares"], a["legs"][1]["paid"]), ("make_trade", 50, 42.5))
        self.assertIsNone(self.store.add_from_trade(plan, {"hedged_pairs": 0}))       # nothing hedged: not added

    def test_snapshot_shows_live_state(self):
        self.store.save({"game": "BTC", "legs": legs()})
        a = self.store.snapshot(FakeKalshi(), FakePM())[0]
        k, p = a["legs"]
        self.assertEqual((k["state"], k["result"], p["state"]), ("settled", "yes", "open"))
        self.assertEqual(k["worth_now"], 99.0)          # 100 YES at the 99c bid
        self.assertEqual(p["worth_now"], 97.0)          # closing 100 NO costs the 3c ask: worth 97c each
        self.assertFalse(a["settled"])                  # Polymarket side still open


if __name__ == "__main__":
    unittest.main()
