import tempfile
import unittest
from pathlib import Path

from arb import engine, nonsports
from arb.matchstore import MatchStore
from arb.model import NO, YES, guaranteed_payout


def pm_market(slug, question, title, bid, ask, end="2026-11-18T00:00:00Z", category="politics"):
    return {"slug": slug, "question": question, "title": title, "active": True, "closed": False, "category": category,
            "endDate": end, "description": f"rules for {title}", "feeCoefficient": 0.0695,
            "bestBidQuote": {"value": str(bid)}, "bestAskQuote": {"value": str(ask)}}


def k_market(ticker, event, sub, bid, ask, close="2026-11-04T00:00:00Z"):
    return {"ticker": ticker, "event_ticker": event, "yes_sub_title": sub, "title": f"Will {sub} win?",
            "status": "active", "close_time": close, "yes_bid_dollars": str(bid), "yes_ask_dollars": str(ask),
            "no_ask_dollars": str(round(1 - bid, 4)), "rules_primary": f"If {sub} wins, Yes."}


class TextTests(unittest.TestCase):
    def test_district_codes_expand_and_normalize(self):
        self.assertEqual(nonsports.tokens("NJ-09 House winner?"), ["newjersey", "district", "9", "house", "win"])
        self.assertIn("california", nonsports.tokens("California's 14th District"))

    def test_shapes(self):
        self.assertEqual(nonsports._shape("Republican 20-25%"), "range")
        self.assertEqual(nonsports._shape("Rulli, 25+ pts"), "threshold")
        self.assertEqual(nonsports._shape("Above $5.60"), "threshold")
        self.assertIsNone(nonsports._shape("Norma Torres (D)"))


class SuggestTests(unittest.TestCase):
    def setUp(self):
        self.pm = [pm_market("p-a", "CA-39 House Election Winner", "Mark Takano (D)", 0.94, 0.96),
                   pm_market("p-b", "CA-39 House Election Winner", "Steve Manos (R)", 0.05, 0.07),
                   pm_market("p-c", "CA-35 House Election Winner", "Norma Torres (D)", 0.9, 0.92)]
        self.ev = [{"event_ticker": "KXHOUSERACE-CA39-26", "title": "CA-39 House winner?", "sub_title": "CA-39",
                    "category": "Elections",
                    "markets": [k_market("K-TAK", "KXHOUSERACE-CA39-26", "Mark Takano", 0.93, 0.97),
                                k_market("K-MAN", "KXHOUSERACE-CA39-26", "Steve Manos", 0.03, 0.05)]}]

    def test_matches_right_district_and_candidates(self):
        groups = nonsports.suggest(self.pm, self.ev, set(), set())
        self.assertEqual(len(groups), 1)                        # CA-35 is never paired with CA-39
        pairs = {(p["pm"], p["kalshi"]) for p in groups[0]["pairs"]}
        self.assertEqual(pairs, {("p-a", "K-TAK"), ("p-b", "K-MAN")})
        self.assertTrue(all(p["hint"] == "same" for p in groups[0]["pairs"]))

    def test_decided_pairs_are_not_suggested_again(self):
        groups = nonsports.suggest(self.pm, self.ev, {("p-a", "K-TAK")}, set())
        self.assertEqual({p["pm"] for p in groups[0]["pairs"]}, {"p-b"})
        groups = nonsports.suggest(self.pm, self.ev, set(), {("CA-39 House Election Winner|2026-11-18",
                                                              "KXHOUSERACE-CA39-26")})
        self.assertEqual(groups, [])


class SafetyTests(unittest.TestCase):
    def test_years_from_titles_and_kalshi_tickers(self):
        self.assertEqual(nonsports.years("2026 Nobel Peace Prize Winner"), {2026})
        self.assertEqual(nonsports.years("Nobel Peace Prize winner", "KXNOBELPEACE-27"), {2027})
        self.assertEqual(nonsports.years("", "KXHOUSERACE-CA39-26"), {2026})
        self.assertEqual(nonsports.years("", "KXBIGBROTHER-26DEC31"), {2026})

    def test_different_year_never_suggested(self):
        pm = [pm_market("p1", "2026 Nobel Peace Prize Winner", "Pope Leo XIV", 0.03, 0.04)]
        ev = [{"event_ticker": "KXNOBELPEACE-27", "title": "Nobel Peace Prize winner", "sub_title": "In 2027",
               "category": "World", "markets": [k_market("KXNOBELPEACE-27-PLEO", "KXNOBELPEACE-27", "Pope Leo XIV", 0.03, 0.05)]}]
        self.assertEqual(nonsports.suggest(pm, ev, set(), set()), [])

    def test_opposite_with_agreeing_prices_is_flagged(self):
        km = nonsports.kalshi_market_obj(k_market("K1", "EV", "Zelensky", 0.02, 0.03), 0.07)
        pm = nonsports.pm_market_obj(pm_market("p1", "Q", "Zelensky", 0.02, 0.03), 0.0695)
        k, p = nonsports.approved_contracts([{"pm": "p1", "kalshi": "K1", "relation": "opposite"}],
                                            {"K1": km}, {"p1": pm})[0]
        for c, src in ((k, km), (p, pm)):
            c.ask = {YES: src.yes_ask, NO: src.no_ask}
        self.assertTrue(any("CONTRADICT" in w for w in engine.warnings_for(k, p, engine.now_utc())))


class OutcomeCompatibilityTests(unittest.TestCase):
    def test_fed_outcomes(self):
        ok = nonsports.outcomes_compatible
        self.assertFalse(ok("25 bps Increase", "Hike >25bps"))        # exactly 25 vs more than 25
        self.assertFalse(ok("25 bps Decrease", "Hike 25bps"))         # cut vs hike
        self.assertTrue(ok("25 bps Increase", "Hike 25bps"))
        self.assertTrue(ok("25 bps Decrease", "Cut 25bps"))
        self.assertFalse(ok("Above $5.60", "Below $5.60"))
        self.assertTrue(ok("Above $5.60", "Above $5.60"))
        self.assertTrue(ok("85 to 86", "85° to 86°"))
        self.assertTrue(ok("Mark Takano (D)", "Mark Takano"))
        self.assertFalse(ok("By December 31, 2026", "Before Dec 1, 2026"))   # different deadlines
        self.assertTrue(ok("By December 31, 2026", "Before Dec 31, 2026"))
        self.assertEqual(nonsports.years("", "KXFEDDECISION-27DEC"), {2027})

    def test_wildly_different_prices_are_not_auto_accepted(self):
        g = {"score": 0.9, "pm": {"question": "Fed"}, "kalshi": {},
             "pairs": [{"pm": "a", "kalshi": "A", "score": 0.9, "hint": None, "pm_bid": 0.70, "pm_ask": 0.72,
                        "k_bid": 0.02, "k_ask": 0.04, "pm_label": "", "k_label": "", "k_title": ""}]}
        auto, review = nonsports.split_auto([g], 0.5, 0.3)
        self.assertEqual(auto, [])
        self.assertEqual(len(review), 1)


class AutoAcceptTests(unittest.TestCase):
    def test_confident_pairs_auto_accepted_rest_kept_for_review(self):
        g = {"score": 0.9, "pm": {"question": "Q"}, "kalshi": {},
             "pairs": [{"pm": "a", "kalshi": "A", "score": 0.8, "hint": "same", "pm_label": "", "k_label": "", "k_title": ""},
                       {"pm": "b", "kalshi": "B", "score": 0.1, "hint": None, "pm_label": "", "k_label": "", "k_title": ""},
                       {"pm": "c", "kalshi": "C", "score": 0.9, "hint": "opposite", "pm_label": "", "k_label": "", "k_title": ""}]}
        weak = {**g, "score": 0.45, "pairs": [dict(g["pairs"][0], pm="d", kalshi="D")]}
        auto, review = nonsports.split_auto([g, weak], 0.5, 0.3)
        self.assertEqual([(a["pm"], a["relation"], a["auto"]) for a in auto], [("a", "same", True)])
        self.assertEqual([[p["pm"] for p in r["pairs"]] for r in review], [["b", "c"], ["d"]])

    def test_auto_pairs_are_flagged_on_contracts(self):
        km = nonsports.kalshi_market_obj(k_market("K1", "EV", "X", 0.4, 0.5), 0.07)
        pm = nonsports.pm_market_obj(pm_market("p1", "Q", "X", 0.4, 0.5), 0.0695)
        cs, _ = nonsports.approved_contracts([{"pm": "p1", "kalshi": "K1", "relation": "same", "auto": True}],
                                             {"K1": km}, {"p1": pm})
        self.assertTrue(all(c.note == "auto" for c in cs))
        self.assertIn("AUTO-MATCHED", engine.warnings_for(cs[0], cs[1], engine.now_utc())[0])


class ApprovedPairTests(unittest.TestCase):
    def _contracts(self, relation):
        km = nonsports.kalshi_market_obj(k_market("K-TAK", "KXHOUSERACE-CA39-26", "Mark Takano", 0.93, 0.97), 0.07)
        pm = nonsports.pm_market_obj(pm_market("p-a", "CA-39 House Election Winner", "Mark Takano (D)", 0.94, 0.96), 0.0695)
        cs, _ = nonsports.approved_contracts([{"pm": "p-a", "kalshi": "K-TAK", "relation": relation}],
                                             {"K-TAK": km}, {"p-a": pm})
        return cs

    def test_same_meaning_pairs_yes_with_no(self):
        k, p = self._contracts("same")
        self.assertEqual(guaranteed_payout([(k, YES), (p, NO)]), 1)
        self.assertEqual(guaranteed_payout([(k, YES), (p, YES)]), 0)
        self.assertEqual(len(engine.group_pairs([k, p])), 1)

    def test_opposite_meaning_pairs_yes_with_yes(self):
        k, p = self._contracts("opposite")
        self.assertEqual(guaranteed_payout([(k, YES), (p, YES)]), 1)
        self.assertEqual(guaranteed_payout([(k, NO), (p, NO)]), 1)


class StoreTests(unittest.TestCase):
    def test_decisions_persist(self):
        path = Path(tempfile.mkdtemp()) / "matches.json"
        s = MatchStore(path)
        s.decide("p", "k", "same", question="Q")
        s.decide("p2", "k2", "reject")
        s2 = MatchStore(path)
        self.assertEqual([(a["pm"], a["relation"]) for a in s2.approved()], [("p", "same")])
        pairs, _ = s2.decided()
        self.assertEqual(pairs, {("p", "k"), ("p2", "k2")})
        s2.decide("p", "k", "remove")
        self.assertEqual(MatchStore(path).approved(), [])


if __name__ == "__main__":
    unittest.main()
