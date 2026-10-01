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


class DuplicateTests(unittest.TestCase):
    """One entry per pair of markets, however the trade reached My arbs."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "my_arbs.json"
        self.store = myarbs.MyArbs(self.path)
        self.plan = {"payout": 1.0, "legs": {"kalshi": legs()[0], "polymarket": legs()[1]}}

    def tearDown(self):
        self.dir.cleanup()

    def trade(self, n, kp, pp):
        return self.store.add_from_trade(self.plan, {"hedged_pairs": n, "legs_filled": {
            "kalshi": {"shares": n, "paid": kp}, "polymarket": {"shares": n, "paid": pp}}}, {"game": "Gas"})

    def account_sync(self, shares, kp, pp, note=""):
        k = {"K1": {"side": "yes", "shares": shares, "paid": kp}}
        p = {"p1": {"side": "no", "shares": shares, "paid": pp}}

        class C:
            title, game_label = "t", "Gas"
        self.store.sync_from_accounts([("K1", "p1", C, C, 1.0)], k, p, [],
                                      lambda kc: {"game": "Gas", "tab": "Economics", "closes": None})

    def test_make_trade_then_position_check_is_one_entry(self):
        a = self.trade(50, 6.5, 42.5)
        self.store.save({**a, "note": "first fill"})
        self.account_sync(50, 6.6, 42.6)
        self.assertEqual(len(self.store.items), 1)
        e = self.store.items[0]
        self.assertEqual((e["source"], e["legs"][0]["paid"], e["note"]), ("account", 6.6, "first fill"))
        self.assertEqual(e["created"], a["created"])                   # keeps when you first traded

    def test_two_trades_on_the_same_pair_add_up(self):
        self.trade(50, 6.5, 42.5)
        self.trade(30, 3.9, 25.5)
        self.assertEqual(len(self.store.items), 1)
        k, p = self.store.items[0]["legs"]
        self.assertEqual((k["shares"], k["paid"], p["shares"], p["paid"]), (80, 10.4, 80, 68.0))

    def test_trade_after_the_position_check_doesnt_double(self):
        self.account_sync(50, 6.6, 42.6)
        self.trade(30, 3.9, 25.5)
        self.assertEqual(len(self.store.items), 1)                     # the next sync adds the new fill
        self.account_sync(80, 10.5, 68.1)
        self.assertEqual((len(self.store.items), self.store.items[0]["legs"][0]["shares"]), (1, 80))

    def test_tracking_a_pair_twice_by_hand_is_refused(self):
        self.store.save({"game": "Gas", "legs": legs()})
        with self.assertRaises(ValueError):
            self.store.save({"game": "Gas again", "legs": legs()})
        self.assertEqual(len(self.store.items), 1)

    def test_doubles_from_older_versions_are_merged_on_load(self):
        import json
        dupes = [{"id": "a1", "created": "2026-09-30T10:00:00", "source": "make_trade", "game": "Gas", "note": "",
                  "legs": legs(50, 6.5, 50, 42.5)},
                 {"id": "acct-K1-p1", "created": "2026-09-30T10:01:00", "source": "account", "game": "Gas", "note": "",
                  "legs": legs(50, 6.6, 50, 42.6)},
                 {"id": "other", "created": "2026-09-30T11:00:00", "source": "manual", "game": "BTC", "note": "",
                  "legs": [{**legs()[0], "market_id": "K9"}, {**legs()[1], "market_id": "p9"}]}]
        self.path.write_text(json.dumps({"arbs": dupes}))
        items = myarbs.MyArbs(self.path).items
        self.assertEqual(sorted(a["id"] for a in items), ["acct-K1-p1", "other"])
        self.assertEqual(len(json.loads(self.path.read_text())["arbs"]), 2)   # the file is cleaned too


if __name__ == "__main__":
    unittest.main()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        self.dir = tempfile.TemporaryDirectory()
        self.m = m.MyArbs(Path(self.dir.name) / "my_arbs.json")
        self.a = self.m.save({"source": "make_trade", "game": "Hanshin vs Yomiuri", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "KXNPB-HAN", "side": "yes", "shares": 20, "paid": 10.0},
            {"exchange": "polymarket", "market_id": "npb-han-ygo", "side": "no", "shares": 20, "paid": 9.6}]})
        self.m._live = {("kalshi", "KXNPB-HAN"): {"state": "open"}, ("polymarket", "npb-han-ygo"): {"state": "open"}}

    def tearDown(self):
        self.dir.cleanup()

    def test_selling_a_whole_leg_closes_the_arb(self):
        changed = self.m.reconcile({}, {"npb-han-ygo": {"side": "no", "shares": 20}})
        self.assertEqual(len(changed), 1)
        a = self.m.items[0]
        self.assertIn("you sold 20 Kalshi YES", a["closed"]["why"])
        self.assertEqual(a["legs"][0]["shares"], 0)
        self.assertEqual(self.m.reconcile({}, {}), [])                       # already closed: left alone

    def test_selling_part_of_a_leg_shrinks_it(self):
        self.m.reconcile({"KXNPB-HAN": {"side": "yes", "shares": 5}}, {"npb-han-ygo": {"side": "no", "shares": 20}})
        a = self.m.items[0]
        self.assertNotIn("closed", a)
        self.assertEqual((a["legs"][0]["shares"], a["legs"][0]["paid"]), (5, 2.5))
        self.assertIn("You sold 15 Kalshi YES", a["note"])

    def test_settled_markets_and_unread_accounts_are_left_alone(self):
        self.m._live[("kalshi", "KXNPB-HAN")] = {"state": "settled"}
        self.assertEqual(self.m.reconcile({}, {"npb-han-ygo": {"side": "no", "shares": 20}}), [])
        self.m._live[("kalshi", "KXNPB-HAN")] = {"state": "open"}
        self.assertEqual(self.m.reconcile({}, {"npb-han-ygo": {"side": "no", "shares": 20}}, read=("polymarket",)), [])
        self.assertEqual(self.m.items[0]["legs"][0]["shares"], 20)


class CostBasisTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "my_arbs.json"
        self.m = m.MyArbs(self.path)
        self.m.save({"source": "make_trade", "game": "BTC", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "K", "side": "yes", "shares": 20, "paid": 9.00},
            {"exchange": "polymarket", "market_id": "p", "side": "no", "shares": 20, "paid": 10.00}]})

    def tearDown(self):
        self.dir.cleanup()

    def test_real_cost_replaces_the_recorded_one(self):
        from arb import myarbs as m
        changed = self.m.update_cost_basis({"K": {"side": "yes", "shares": 30, "paid": 14.40}},     # 0.48/share
                                           {"p": {"side": "no", "shares": 20, "paid": 10.00}})
        self.assertEqual(len(changed), 1)
        k, p = self.m.items[0]["legs"]
        self.assertEqual((k["paid"], k["paid_recorded"], k["cost_from"]), (9.60, 9.00, "account"))
        self.assertEqual((p["paid"], p["cost_from"]), (10.00, "account"))
        self.assertNotIn("paid_recorded", p)                                   # unchanged leg
        self.assertAlmostEqual(m.summarize(self.m.items[0])["profit"], 20 - 19.60)
        again = m.MyArbs(self.path)                                            # saved
        self.assertEqual(again.items[0]["legs"][0]["paid"], 9.60)

    def test_estimated_wrong_side_or_unread_are_left_alone(self):
        self.m.update_cost_basis({"K": {"side": "no", "shares": 20, "paid": 1.0}},
                                 {"p": {"side": "no", "shares": 20, "paid": 1.0, "paid_estimated": True}})
        self.m.update_cost_basis({"K": {"side": "yes", "shares": 20, "paid": 1.0}}, {}, read=("polymarket",))
        self.assertEqual([l["paid"] for l in self.m.items[0]["legs"]], [9.00, 10.00])
