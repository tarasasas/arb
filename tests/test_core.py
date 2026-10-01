import unittest

from arb import kalshi, matching, polymarket
from arb.engine import outcome_table, size_opportunity
from arb.model import NO, YES, Contract, guaranteed_payout, kalshi_fee, polymarket_fee


def c(var, op, line, exchange="kalshi", **kw):
    return Contract(exchange, f"{exchange}-{op}{line}", "G", var, op, line, "t", **kw)


MARGIN = ("margin", "FG", "A")
TOTAL = ("total", "FG")


class PayoutTests(unittest.TestCase):
    def test_same_line_yes_plus_no_is_one(self):
        self.assertEqual(guaranteed_payout([(c(TOTAL, ">", 5.5), YES), (c(TOTAL, ">", 5.5), NO)]), 1)

    def test_lower_line_yes_plus_higher_line_no_covers(self):
        # win by >3.5 on one exchange, NOT win by >6.5 on the other: always >= $1, $2 if margin 4-6.
        self.assertEqual(guaranteed_payout([(c(MARGIN, ">", 3.5), YES), (c(MARGIN, ">", 6.5), NO)]), 1)

    def test_higher_line_yes_plus_lower_line_no_does_not_cover(self):
        self.assertEqual(guaranteed_payout([(c(MARGIN, ">", 6.5), YES), (c(MARGIN, ">", 3.5), NO)]), 0)

    def test_opposite_team_spreads(self):
        # A wins by >3.5 (margin_A > 3.5) and B +3.5 (margin_A < 3.5): complementary.
        self.assertEqual(guaranteed_payout([(c(MARGIN, ">", 3.5), YES), (c(MARGIN, "<", 3.5), YES)]), 1)

    def test_nfl_moneyline_tie_pays_half_each(self):
        a = c(MARGIN, ">", 0.0, tie_half=True)
        b = c(MARGIN, "<", 0.0, tie_half=True)
        self.assertEqual(guaranteed_payout([(a, YES), (b, YES)]), 1)

    def test_moneylines_need_no_draw_to_cover(self):
        w = dict(winner=True)
        self.assertEqual(guaranteed_payout([(c(MARGIN, ">", 0.0, **w), YES), (c(MARGIN, "<", 0.0, **w), YES)]), 0)
        a = c(MARGIN, ">", 0.0, no_draw=True, **w)
        b = c(MARGIN, "<", 0.0, no_draw=True, **w)
        self.assertEqual(guaranteed_payout([(a, YES), (b, YES)]), 1)

    def test_soccer_three_way_winner_is_not_a_push(self):
        # Team A wins on one exchange + NO on team A wins on the other covers everything, draws included.
        a1, a2 = c(MARGIN, ">", 0.0, winner=True), c(MARGIN, ">", 0.0, winner=True)
        self.assertEqual(guaranteed_payout([(a1, YES), (a2, NO)]), 1)
        d1, d2 = c(MARGIN, "==", 0.0, winner=True), c(MARGIN, "==", 0.0, winner=True)
        self.assertEqual(guaranteed_payout([(d1, YES), (d2, NO)]), 1)
        # A wins + B wins misses the draw.
        b = c(MARGIN, "<", 0.0, winner=True)
        self.assertEqual(guaranteed_payout([(a1, YES), (b, YES)]), 0)

    def test_integer_line_push_is_conservative(self):
        self.assertEqual(guaranteed_payout([(c(TOTAL, ">", 3.0), YES), (c(TOTAL, ">", 3.0), NO)]), 0)


class FeeTests(unittest.TestCase):
    def test_kalshi_published_example(self):
        # Kalshi: 100 contracts at $0.55 -> $1.74 (0.07 x 100 x 0.55 x 0.45 = 1.7325, rounded up).
        self.assertEqual(kalshi_fee([(0.55, 100)], 0.07), 1.74)

    def test_polymarket_published_examples(self):
        self.assertEqual(polymarket_fee([(0.10, 1000)], 0.0695), 6.26)   # 6.255 -> half-even up
        self.assertEqual(polymarket_fee([(0.65, 1000)], 0.0695), 15.81)
        self.assertEqual(polymarket_fee([(0.50, 1000)], 0.0695), 17.38)  # 17.375 -> 17.38
        self.assertEqual(polymarket_fee([(0.50, 1)], 0.0695), 0.02)      # 0.017375 -> 0.02


class SizingTests(unittest.TestCase):
    def test_walks_books_until_edge_disappears(self):
        k = c(TOTAL, ">", 5.5, fee_coef=0.07)
        p = c(TOTAL, ">", 5.5, exchange="polymarket", fee_coef=0.0695)
        cand = {"k": k, "sk": YES, "p": p, "sp": NO, "payout": 1.0}
        levels_k = [(0.40, 100), (0.45, 100)]
        # 0.40+0.50 -> edge ~0.066 (100); 0.45+0.50 -> ~0.015 (50 left at 0.50); 0.45+0.60 -> negative.
        levels_p = [(0.50, 150), (0.60, 100)]
        s = size_opportunity(cand, levels_k, levels_p)
        self.assertEqual(s["size"], 150)
        self.assertGreater(s["profit"], 0)


class OutcomeTableTests(unittest.TestCase):
    def test_cross_line_spread_lists_every_region(self):
        rows = outcome_table(c(MARGIN, ">", 3.5), YES, c(MARGIN, ">", 6.5, exchange="polymarket"), NO)
        self.assertEqual([(r["outcome"], r["total"]) for r in rows], [
            ("A loses, ties or wins by 3 or less", 1), ("A wins by 4 to 6", 2), ("A wins by 7 or more", 1)])

    def test_total_and_period_wording(self):
        rows = outcome_table(c(TOTAL, ">", 69.5), YES, c(TOTAL, ">", 69.5, exchange="polymarket"), NO)
        self.assertEqual([r["outcome"] for r in rows], ["Combined score 69 or less", "Combined score 70 or more"])
        h1 = ("margin", "1H", "A")
        rows = outcome_table(c(h1, ">", 0.0, winner=True), YES, c(h1, ">", 0.0, exchange="polymarket", winner=True), NO)
        self.assertEqual([r["outcome"] for r in rows], ["1st half: A loses or ties", "1st half: A wins"])

    def test_every_region_pays_at_least_guaranteed_payout(self):
        k, p = c(MARGIN, ">", 0.0, tie_half=True, winner=True), c(MARGIN, "<", 0.0, tie_half=True, winner=True)
        rows = outcome_table(k, YES, p, YES)
        self.assertEqual(min(r["total"] for r in rows), guaranteed_payout([(k, YES), (p, YES)]))
        self.assertIn("Tie", [r["outcome"] for r in rows])


class ParsingTests(unittest.TestCase):
    def test_parse_kalshi_series(self):
        self.assertEqual(kalshi.parse_series("KXNFL1HSPREAD"), ("NFL", "nfl", "football", "1H", "SPREAD"))
        self.assertEqual(kalshi.parse_series("KXNCAAF1H"), ("NCAAF", "cfb", "football", "1H", "GAME"))
        self.assertEqual(kalshi.parse_series("KXBRASILEIROBGAME")[0], "BRASILEIROB")
        self.assertIsNone(kalshi.parse_series("KXNFLWINS"))

    def test_parse_kalshi_spread_market(self):
        m = {"ticker": "KXNFLSPREAD-26OCT05ATLNO-NO8", "event_ticker": "KXNFLSPREAD-26OCT05ATLNO",
             "strike_type": "greater", "floor_strike": 7.5, "title": "NO Saints wins by over 7.5 points?"}
        km = kalshi.parse_market(m, kalshi.parse_series("KXNFLSPREAD"), 0.07)
        self.assertEqual((km.team, km.op, km.line, km.teams_str, km.date_code), ("NO", ">", 7.5, "ATLNO", "26OCT05"))

    def test_parse_polymarket_spread(self):
        m = {"slug": "asc-nfl-pit-cle-2026-10-01-pos-1pt5", "active": True, "closed": False,
             "status": "MARKET_STATUS_OPEN", "sportsMarketType": "football_team_full_game_spread", "line": 1.5,
             "marketSides": [{"long": True, "team": {"abbreviation": "pit"}},
                             {"long": False, "team": {"abbreviation": "cle"}}]}
        pm = polymarket.parse_market(m)
        # PIT +1.5 covers <=> margin(PIT) > -1.5
        self.assertEqual((pm.kind, pm.team, pm.op, pm.line, pm.period), ("SPREAD", "pit", ">", -1.5, "FG"))

    def test_parse_polymarket_team_total_and_draw(self):
        base = {"active": True, "closed": False, "status": "MARKET_STATUS_OPEN"}
        tt = polymarket.parse_market({**base, "slug": "tsc-nfl-pit-cle-2026-10-01-tt1h-pit-5pt5",
                                      "sportsMarketType": "football_team_first_half_total", "line": 5.5})
        self.assertEqual((tt.kind, tt.team, tt.period), ("TEAMTOTAL", "pit", "1H"))
        draw = polymarket.parse_market({**base, "slug": "atc-unl-aze-lie-2026-10-01-draw",
                                        "sportsMarketType": "soccer_team_full_time_winner"})
        self.assertEqual((draw.team, draw.op), ("draw", "=="))

    def test_name_matching(self):
        self.assertTrue(matching.name_matches("Los Angeles R", {"Los Angeles Rams"}))
        self.assertTrue(matching.name_matches("Jacksonville St.", {"Jacksonville State Gamecocks"}))
        self.assertFalse(matching.name_matches("Los Angeles C", {"Los Angeles Rams"}))


if __name__ == "__main__":
    unittest.main()


class SettleTimeTests(unittest.TestCase):
    def test_event_placeholder_is_capped_by_the_markets_latest_settle(self):
        from arb.kalshi import settle_time
        m = {"close_time": "2027-01-01T04:59:00Z", "expected_expiration_time": "2028-12-31T15:00:00Z",
             "latest_expiration_time": "2027-01-01T15:00:00Z"}                 # KXFEDHIKE "Before 2027"
        self.assertEqual(settle_time(m), "2027-01-01T15:00:00Z")
        self.assertEqual(settle_time({**m, "expected_expiration_time": "2026-12-20T15:00:00Z"}), "2026-12-20T15:00:00Z")
        self.assertEqual(settle_time({"close_time": "2026-11-01T00:00:00Z"}), "2026-11-01T00:00:00Z")
