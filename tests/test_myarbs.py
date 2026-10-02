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

    def test_fraction_of_a_share_extra_is_leftover_not_unhedged(self):
        # 12 Kalshi NO against 12.04 Polymarket YES (bought by dollar amount) is fully hedged
        s = myarbs.summarize({"payout": 1.0, "legs": [
            {"exchange": "kalshi", "side": "no", "shares": 12, "paid": 9.89},
            {"exchange": "polymarket", "side": "yes", "shares": 12.04, "paid": 1.89}]})
        self.assertEqual((s["pairs"], s["unhedged"]), (12, []))
        self.assertEqual(s["leftover"], [{"exchange": "polymarket", "side": "yes", "shares": 0.04}])
        # 9 Kalshi YES against 8.8 Polymarket NO: 0.2 Kalshi share left over
        s = myarbs.summarize({"payout": 1.0, "legs": [
            {"exchange": "kalshi", "side": "yes", "shares": 9, "paid": 8.44},
            {"exchange": "polymarket", "side": "no", "shares": 8.8, "paid": 0.40}]})
        self.assertEqual((s["pairs"], s["unhedged"], s["leftover"]),
                         (8.8, [], [{"exchange": "kalshi", "side": "yes", "shares": 0.2}]))

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

    def test_hedging_by_hand_raises_the_short_leg(self):
        # Kalshi 20 / Polymarket 12 tracked (8 unhedged); you buy 7 more on Polymarket yourself.
        self.m.items[0]["legs"][1].update(shares=12, paid=5.76)
        changed = self.m.reconcile({"KXNPB-HAN": {"side": "yes", "shares": 20, "paid": 10.0}},
                                   {"npb-han-ygo": {"side": "no", "shares": 19, "paid": 9.31}})
        self.assertEqual(len(changed), 1)
        a = self.m.items[0]
        self.assertEqual((a["legs"][1]["shares"], a["legs"][1]["paid"]), (19, 9.31))
        self.assertIn("You bought 7 Polymarket NO more", a["note"])
        from arb.myarbs import summarize
        self.assertEqual(summarize(a)["unhedged"][0]["shares"], 1)

    def test_a_market_shared_by_two_arbs_is_not_raised(self):
        self.m.save({"source": "manual", "game": "Other line", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "KXNPB-HAN2", "side": "no", "shares": 5, "paid": 2.0},
            {"exchange": "polymarket", "market_id": "npb-han-ygo", "side": "no", "shares": 5, "paid": 2.0}]})
        self.m.reconcile({}, {"npb-han-ygo": {"side": "no", "shares": 40}}, read=("polymarket",))
        self.assertEqual([a["legs"][1]["shares"] for a in self.m.items], [20, 5])


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

    def test_kalshi_fees_are_kept_alongside_the_cost(self):
        # Kalshi's app: Cost $9.25 for 10 shares; the account also charged $0.15 in fees
        self.m.update_cost_basis({"K": {"side": "yes", "shares": 20, "paid": 18.80, "fees": 0.30}}, {})
        k = self.m.items[0]["legs"][0]
        self.assertEqual((k["paid"], k["fees"]), (18.80, 0.30))

    def test_estimated_wrong_side_or_unread_are_left_alone(self):
        self.m.update_cost_basis({"K": {"side": "no", "shares": 20, "paid": 1.0}},
                                 {"p": {"side": "no", "shares": 20, "paid": 1.0, "paid_estimated": True}})
        self.m.update_cost_basis({"K": {"side": "yes", "shares": 20, "paid": 1.0}}, {}, read=("polymarket",))
        self.assertEqual([l["paid"] for l in self.m.items[0]["legs"]], [9.00, 10.00])


class VerifyTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        from arb.model import Contract
        self.dir = tempfile.TemporaryDirectory()
        self.m = m.MyArbs(Path(self.dir.name) / "my_arbs.json")
        var = ("total", "FG")
        self.c = {("kalshi", "K"): Contract("kalshi", "K", "G", var, ">", 8.5, "Over 8.5", fee_coef=0.07),
                  ("polymarket", "p"): Contract("polymarket", "p", "G", var, ">", 8.5, "Over 8.5?", fee_coef=0.07)}

    def tearDown(self):
        self.dir.cleanup()

    def track(self, kside, pside, kpaid=9.0, ppaid=10.0):
        self.m.items = []
        return self.m.save({"source": "manual", "game": "G", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "K", "side": kside, "shares": 20, "paid": kpaid},
            {"exchange": "polymarket", "market_id": "p", "side": pside, "shares": 20, "paid": ppaid}]})

    def test_real_arb_is_confirmed(self):
        from arb import myarbs as m
        self.track("yes", "no")
        self.m.verify(lambda ex, mid: self.c.get((ex, mid)))
        a = self.m.items[0]
        self.assertEqual(a["check"]["structure"], "ok")
        s = m.summarize(a)
        self.assertEqual((s["pair_cost"], s["pair_edge"]), (0.95, 0.05))

    def test_both_sides_that_can_lose_are_flagged(self):
        self.track("yes", "yes")                                    # Over on both sites: Under loses both
        changed = self.m.verify(lambda ex, mid: self.c.get((ex, mid)))
        self.assertEqual(changed[0]["check"]["structure"], "broken")

    def test_losing_at_real_cost_and_unlisted_markets(self):
        from arb import myarbs as m
        self.track("yes", "no", kpaid=10.5, ppaid=10.5)            # $1.05 per $1 pair
        self.assertLess(m.summarize(self.m.items[0])["pair_edge"], 0)
        self.m.verify(lambda ex, mid: None)
        self.assertEqual(self.m.items[0]["check"]["structure"], "unknown")

    def test_a_market_in_several_pairs_is_checked_against_each(self):
        # Kalshi "16-100%" is matched to two Polymarket markets; the pairing in your account is the second.
        from arb.model import Contract
        var = ("event", "x")
        other = Contract("kalshi", "K", "POL:other|K", ("event", "other|K"), ">", 0.5, "16-100%")
        mine_k = Contract("kalshi", "K", "POL:p|K", ("event", "p|K"), ">", 0.5, "16-100%")
        mine_p = Contract("polymarket", "p", "POL:p|K", ("event", "p|K"), ">", 0.5, "16%+")
        lookup = {("kalshi", "K"): [other, mine_k], ("polymarket", "p"): [mine_p]}
        self.track("yes", "no")
        self.m.verify(lambda ex, mid: lookup.get((ex, mid)))
        self.assertEqual(self.m.items[0]["check"]["structure"], "ok")
        self.m.verify(lambda ex, mid: [other] if ex == "kalshi" else [mine_p])   # not matched together
        self.assertEqual(self.m.items[0]["check"]["structure"], "unknown")

    def test_a_matched_arb_stays_confirmed_when_a_market_stops_trading(self):
        self.track("yes", "no")
        self.m.verify(lambda ex, mid: self.c.get((ex, mid)))
        self.m.verify(lambda ex, mid: self.c.get((ex, mid)) if ex == "kalshi" else None)   # Polymarket closed
        check = self.m.items[0]["check"]
        self.assertEqual(check["structure"], "ok")
        self.assertIn("Polymarket market isn't trading", check["why"])


class PayoutDateTests(VerifyTests):
    def test_payout_date_follows_the_markets(self):
        self.track("yes", "no")
        self.m.items[0]["closes"] = "2028-12-31T15:00:00+00:00"              # recorded from the old placeholder
        self.c[("kalshi", "K")].close_time = "2027-01-01T15:00:00Z"
        self.c[("polymarket", "p")].close_time = "2026-12-31T23:59:00Z"
        self.m.verify(lambda ex, mid: self.c.get((ex, mid)))
        self.assertEqual(self.m.items[0]["closes"], "2027-01-01T15:00:00+00:00")


class LivePayoutDateTests(unittest.TestCase):
    def test_payout_date_from_both_sites_live_data(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        with tempfile.TemporaryDirectory() as d:
            store = m.MyArbs(Path(d) / "my_arbs.json")
            store.save({"source": "manual", "game": "Another Fed Rate Hike in 2026?", "payout": 1.0,
                        "closes": "2028-12-31T15:00:00+00:00", "legs": [
                            {"exchange": "kalshi", "market_id": "KXFEDHIKE-2-26DEC31", "side": "no", "shares": 75, "paid": 30},
                            {"exchange": "polymarket", "market_id": "fed-hike-2026", "side": "yes", "shares": 75, "paid": 39}]})

            class K:
                def markets_by_ticker(self, t):
                    return {"KXFEDHIKE-2-26DEC31": {"status": "active", "close_time": "2027-01-01T04:59:00Z",
                                                     "expected_expiration_time": "2028-12-31T15:00:00Z",
                                                     "latest_expiration_time": "2027-01-01T15:00:00Z"}}

            class P:
                def markets_by_slug(self, s):
                    return {"fed-hike-2026": {"active": True, "endDate": "2026-12-31T00:00:00Z"}}
            snap = store.snapshot(K(), P())
            self.assertEqual(snap[0]["closes"], "2027-01-01T15:00:00+00:00")
            self.assertEqual(m.MyArbs(Path(d) / "my_arbs.json").items[0]["closes"], "2027-01-01T15:00:00+00:00")


class ClaimTests(unittest.TestCase):
    def test_tracked_pair_with_an_unlisted_market_is_not_one_sided(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mine = m.MyArbs(Path(d.name) / "my_arbs.json")
        mine.save({"source": "account", "game": "Vance VP", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "KXVP", "side": "no", "shares": 34, "paid": 32.35},
            {"exchange": "polymarket", "market_id": "vance-vp", "side": "yes", "shares": 34, "paid": 0.73}]})
        unpaired = [{"exchange": "kalshi", "market_id": "KXVP", "side": "no", "shares": 34},
                    {"exchange": "polymarket", "market_id": "vance-vp", "side": "yes", "shares": 34},
                    {"exchange": "kalshi", "market_id": "KXOTHER", "side": "yes", "shares": 3}]
        mine.sync_from_accounts([], {}, {}, unpaired, None)
        self.assertEqual(mine.known_pairs, 1)
        self.assertEqual([u["market_id"] for u in mine.unpaired], ["KXOTHER"])


class PlacedTimeTests(unittest.TestCase):
    def test_placed_time_from_first_kalshi_fill_once(self):
        import tempfile
        from pathlib import Path
        from arb import myarbs as m
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        mine = m.MyArbs(Path(d.name) / "my_arbs.json")
        mine.save({"source": "account", "game": "G", "payout": 1.0, "legs": [
            {"exchange": "kalshi", "market_id": "K", "side": "no", "shares": 1, "paid": 0.5},
            {"exchange": "polymarket", "market_id": "p", "side": "yes", "shares": 1, "paid": 0.4}]})
        calls = []
        first = lambda t: calls.append(t) or "2026-09-20T15:00:00Z"
        self.assertEqual(mine.fill_placed_times(first), 1)
        self.assertEqual(mine.items[0]["placed"], "2026-09-20T15:00:00Z")
        self.assertEqual(mine.fill_placed_times(first), 0)                    # not looked up again
        self.assertEqual(calls, ["K"])



class PaidOutTests(unittest.TestCase):
    """Arbs whose markets have both paid out: what they really paid, saved for good."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "my_arbs.json"
        self.store = myarbs.MyArbs(self.path)

    def tearDown(self):
        self.dir.cleanup()

    @staticmethod
    def sites(k_status="finalized", k_result="yes", k_value="1.0000", pm_long="1", pm_status="MARKET_STATUS_RESOLVED"):
        class K:
            def markets_by_ticker(self, t):
                return {"K1": {"status": k_status, "result": k_result, "settlement_value_dollars": k_value,
                               "settlement_ts": "2026-10-01T05:09:36Z"}}

        class P:
            def markets_by_slug(self, s):
                return {"p1": {"active": False, "closed": True, "status": pm_status,
                               "marketSides": [{"long": True, "price": pm_long}, {"long": False, "price": "0"}]}}
        return K(), P()

    def test_paid_out_with_what_each_leg_really_paid(self):
        self.store.save({"game": "SD vs CHC", "legs": legs()})            # 100 Kalshi YES + 100 Polymarket NO, $98
        a = self.store.snapshot(*self.sites())[0]                          # YES happened: Kalshi pays, Polymarket NO doesn't
        self.assertEqual(a["phase"], "paid")
        po = a["paid_out"]
        self.assertEqual((po["amount"], po["cost"], po["profit"], po["time"]), (100.0, 98.0, 2.0, "2026-10-01T05:09:36Z"))
        self.assertEqual([(l["exchange"], l["result"], l["paid_out"]) for l in po["legs"]],
                         [("kalshi", "yes", 100.0), ("polymarket", "yes", 0.0)])

    def test_extra_unhedged_shares_count_at_their_real_result(self):
        self.store.save({"game": "SD vs CHC", "legs": legs(ks=120, kp=15.6)})
        po = self.store.snapshot(*self.sites())[0]["paid_out"]
        self.assertEqual((po["amount"], po["profit"]), (120.0, 19.4))      # the 20 extra YES won too

    def test_kept_after_the_markets_drop_off_the_sites(self):
        self.store.save({"game": "SD vs CHC", "legs": legs()})
        self.store.snapshot(*self.sites())

        class Gone:
            def markets_by_ticker(self, t):
                return {}

            def markets_by_slug(self, s):
                return {}
        again = myarbs.MyArbs(self.path)
        a = again.snapshot(Gone(), Gone())[0]
        self.assertEqual((a["phase"], a["paid_out"]["amount"]), ("paid", 100.0))

    def test_waits_until_both_sides_have_paid(self):
        self.store.save({"game": "SD vs CHC", "legs": legs()})
        a = self.store.snapshot(*self.sites(k_status="determined"))[0]    # result known, not paid yet
        self.assertEqual(a["phase"], "awaiting")
        self.assertNotIn("paid_out", a)
        self.store._live_time = 0
        a = self.store.snapshot(*self.sites(pm_status="MARKET_STATUS_OPEN"))[0]
        self.assertNotIn("paid_out", a)

    def test_void_result_pays_what_the_site_paid(self):
        self.store.save({"game": "Rained out", "legs": legs()})
        po = self.store.snapshot(*self.sites(k_value="0.5000", pm_long="0.5"))[0]["paid_out"]
        self.assertEqual(po["amount"], 100.0)                              # 100 x 0.50 + 100 x 0.50

    def test_sold_early_is_not_a_payout(self):
        a = self.store.save({"game": "SD vs CHC", "legs": legs()})
        self.store.items[0]["closed"] = {"time": "2026-10-01T00:00:00Z", "why": "you sold 100 Kalshi YES"}
        row = self.store.snapshot(*self.sites())[0]
        self.assertEqual(row["phase"], "sold")
        self.assertNotIn("paid_out", row)
