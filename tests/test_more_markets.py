import unittest

from arb import engine, kalshi, matching, polymarket
from arb.model import Contract, guaranteed_payout


def kseries(t):
    return kalshi.parse_series(t)


class KalshiNewKindsTests(unittest.TestCase):
    def test_mlb_first_inning_run_whose_ticker_is_the_event(self):
        info = kseries("KXMLBRFI")
        m = {"ticker": "KXMLBRFI-26OCT031600ATLLAD", "event_ticker": "KXMLBRFI-26OCT031600ATLLAD",
             "strike_type": "greater_or_equal", "floor_strike": 1, "title": "1st inning: Over 0.5 runs"}
        km = kalshi.parse_market(m, info, 0.07)
        self.assertEqual((km.kind, km.period, km.op, km.line, km.teams_str), ("TOTAL", "I1", ">", 0.5, "ATLLAD"))

    def test_npb_first_inning_run_with_suffix(self):
        m = {"ticker": "KXNPBRFI-26OCT050500SAICHI-Y", "event_ticker": "KXNPBRFI-26OCT050500SAICHI",
             "strike_type": "greater_or_equal", "floor_strike": 1}
        km = kalshi.parse_market(m, kseries("KXNPBRFI"), 0.07)
        self.assertEqual((km.period, km.line), ("I1", 0.5))

    def test_both_teams_to_score_full_game_and_periods(self):
        self.assertEqual(kseries("KXMLSBTTS")[3:], ("FG", "BTTS"))
        self.assertEqual(kseries("KXNFL3QBTTS")[3:], ("3Q", "BTTS"))
        m = {"ticker": "KXUEFANLBTTS-26OCT06SUIMKD-BTTS", "event_ticker": "KXUEFANLBTTS-26OCT06SUIMKD", "strike_type": None}
        km = kalshi.parse_market(m, kseries("KXUEFANLBTTS"), 0.07)
        self.assertEqual((km.kind, km.period, km.op, km.line, km.team), ("BTTS", "FG", ">", 0.5, None))


class PolymarketNewKindsTests(unittest.TestCase):
    def pm(self, slug, stype, line=1):
        return polymarket.parse_market({"slug": slug, "sportsMarketType": stype, "active": True, "line": line,
                                        "status": "MARKET_STATUS_OPEN", "question": "q",
                                        "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.42"}})

    def test_btts_slugs_and_periods(self):
        full = self.pm("astatc-unl-kaz-mda-2026-10-02-btts", "soccer_game_btts")
        second = self.pm("astatc-unl-kaz-mda-2026-10-02-sh-btts", "soccer_game_second_half_btts")
        q3 = self.pm("astatc-nfl-det-car-2026-10-04-bp3q-0pt5", "football_game_third_quarter_both_teams_score_points", 0.5)
        self.assertEqual([(m.kind, m.period, m.line, m.t1, m.t2) for m in (full, second, q3)],
                         [("BTTS", "FG", 0.5, "kaz", "mda"), ("BTTS", "2H", 0.5, "kaz", "mda"), ("BTTS", "3Q", 0.5, "det", "car")])

    def test_first_inning_run(self):
        m = self.pm("astatc-mlb-cws-cle-2026-10-03-yrfi", "baseball_team_first_inning_run")
        self.assertEqual((m.kind, m.period, m.op, m.line), ("TOTAL", "I1", ">", 0.5))


class PairingTests(unittest.TestCase):
    def test_btts_yes_on_one_site_and_no_on_the_other_is_an_arb(self):
        var = matching._var("BTTS", "FG", None, "A")
        k = Contract("kalshi", "k", "G", var, ">", 0.5, "BTTS")
        p = Contract("polymarket", "p", "G", var, ">", 0.5, "BTTS")
        self.assertEqual(guaranteed_payout([(k, "yes"), (p, "no")]), 1.0)
        self.assertEqual(guaranteed_payout([(k, "yes"), (p, "yes")]), 0.0)
        self.assertEqual([r["outcome"] for r in engine.outcome_table(k, "yes", p, "no")],
                         ["Not both teams score", "Both teams score"])
        self.assertEqual(engine.describe_var(("btts", "2H")), "Both teams score (2H)")


if __name__ == "__main__":
    unittest.main()
