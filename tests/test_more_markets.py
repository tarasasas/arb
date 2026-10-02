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


class TennisTests(unittest.TestCase):
    def test_every_kalshi_tour_is_one_tennis_league(self):
        for s in ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXITFWMATCH"):
            self.assertEqual(kseries(s), ("TENNIS", "atp", "tennis", "FG", "GAME"))
        self.assertIsNone(kseries("KXATPEXACTMATCH"))              # exact set score: a different market

    def test_polymarket_match_winner_and_pairing(self):
        m = polymarket.parse_market({
            "slug": "aec-atp-gushei-laumid-2026-10-02", "sportsMarketType": "tennis_match_winner", "active": True,
            "status": "MARKET_STATUS_OPEN", "question": "q",
            "marketSides": [{"long": True, "team": {"abbreviation": "gushei", "name": "Gustavo Heide"}},
                            {"long": False, "team": {"abbreviation": "laumid", "name": "Lautaro Midon"}}],
            "bestBidQuote": {"value": "0.70"}, "bestAskQuote": {"value": "0.71"}})
        self.assertEqual((m.sport, m.kind, m.team, m.op, m.line), ("tennis", "GAME", "gushei", ">", 0.0))
        g = matching.KalshiGame("TENNIS", "26OCT02HEIMID", "26OCT02", "HEIMID")
        for code, name in (("HEI", "Gustavo Heide"), ("MID", "Lautaro Midon")):
            g.markets.append(kalshi.KalshiMarket(f"KXATPMATCH-26OCT02HEIMID-{code}", "KXATPMATCH-26OCT02HEIMID", "KXATPMATCH",
                                                 "TENNIS", "atp", "tennis", "26OCT02HEIMID", "26OCT02", "HEIMID", "GAME",
                                                 "FG", code, ">", 0.0, "t", name, "", "", 0.07))
            g.names[code] = name
        matches, _ = matching.match_games({("TENNIS", "26OCT02HEIMID"): g}, {("atp", "2026-10-02", "gushei", "laumid"): [m]},
                                          {"atp": ("TENNIS", "tennis")})
        self.assertEqual(matches[0][3], {"gushei": "HEI", "laumid": "MID"})
        contracts, _ = matching.build_contracts(matches)
        self.assertTrue(all(c.no_draw for c in contracts))            # a tennis match can't be drawn
        k_hei = next(c for c in contracts if c.market_id.endswith("-HEI"))
        p = next(c for c in contracts if c.exchange == "polymarket")
        self.assertEqual(guaranteed_payout([(k_hei, "no"), (p, "yes")]), 1.0)   # Kalshi NO Heide + PM YES Heide
        w = engine.warnings_for(k_hei, p, engine.now_utc())
        self.assertTrue(any("never starts" in x for x in w))
