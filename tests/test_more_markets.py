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


def kgame(markets):
    """A matched game holding Kalshi `markets`, with AZE and LTU moneylines so the teams are known."""
    g = matching.KalshiGame("UEFANL", "26OCT04AZELTU", "26OCT04", "AZELTU")
    g.markets = markets
    return g


def pm_raw(slug, stype, line=None, player=None, bid="0.40", ask="0.42"):
    m = {"slug": slug, "sportsMarketType": stype, "active": True, "status": "MARKET_STATUS_OPEN", "question": "q",
         "bestBidQuote": {"value": bid}, "bestAskQuote": {"value": ask}}
    if line is not None:
        m["line"] = line
    if player:
        m["metadata"] = {"playerName": player}
    return m


class ExactScoreTests(unittest.TestCase):
    def kscore(self, suffix, series="KXUEFANLSCORE"):
        ev = f"{series}-26OCT04AZELTU"
        return kalshi.parse_market({"ticker": f"{ev}-{suffix}", "event_ticker": ev, "strike_type": "custom",
                                    "title": suffix}, kseries(series), 0.07)

    def test_kalshi_scores_and_first_half(self):
        self.assertEqual(kseries("KXEPL1HSCORE")[3:], ("1H", "SCORE"))
        km = self.kscore("AZE0LTU1")                                        # Lithuania wins 1-0
        self.assertEqual((km.kind, km.period, km.op, km.line, km.score), ("SCORE", "FG", ">", 0.5,
                                                                           (("AZE", 0), ("LTU", 1))))
        self.assertIsNone(self.kscore("AZE1XYZ0"))                          # not this game's teams
        self.assertIsNone(self.kscore("OTHER"))

    def test_polymarket_slug_score_is_t1_then_t2(self):
        m = polymarket.parse_market(pm_raw("atc-unl-aze-ltu-2026-10-04-exact-score-0-1", "soccer_game_exact_score"))
        h = polymarket.parse_market(pm_raw("atc-unl-aze-ltu-2026-10-04-fh-exact-score-2-2",
                                           "soccer_game_first_half_exact_score"))
        self.assertEqual((m.kind, m.period, m.score), ("SCORE", "FG", (("aze", 0), ("ltu", 1))))
        self.assertEqual((h.period, h.score), ("1H", (("aze", 2), ("ltu", 2))))

    def test_same_score_pairs_whichever_way_round_the_teams_are(self):
        ks = [self.kscore("AZE0LTU1"), self.kscore("AZE1LTU0")]
        pms = [polymarket.parse_market(pm_raw("atc-unl-ltu-aze-2026-10-04-exact-score-1-0", "soccer_game_exact_score"))]
        contracts, _ = matching.build_contracts([(("unl", "2026-10-04", "ltu", "aze"), pms, kgame(ks),
                                                 {"ltu": "LTU", "aze": "AZE"})])
        groups = engine.group_pairs(contracts)
        self.assertEqual(list(groups), [("UEFANL:26OCT04AZELTU", ("score", "FG", "AZE 0-1 LTU"))])
        g = groups[("UEFANL:26OCT04AZELTU", ("score", "FG", "AZE 0-1 LTU"))]
        self.assertEqual((g["kalshi"][0].market_id, g["polymarket"][0].market_id),
                         ("KXUEFANLSCORE-26OCT04AZELTU-AZE0LTU1", "atc-unl-ltu-aze-2026-10-04-exact-score-1-0"))
        k, p = g["kalshi"][0], g["polymarket"][0]
        self.assertEqual(guaranteed_payout([(k, "yes"), (p, "no")]), 1.0)
        self.assertEqual([r["outcome"] for r in engine.outcome_table(k, "yes", p, "no")],
                         ["Any other score", "Ends AZE 0-1 LTU"])


class PlayerPropTests(unittest.TestCase):
    def kprop(self, series, player, n, ev_body="26OCT031830NYYTB", code="NYYGCOLE45"):
        ev = f"{series}-{ev_body}"
        return kalshi.parse_market({"ticker": f"{ev}-{code}-{n}", "event_ticker": ev, "strike_type": "greater",
                                    "floor_strike": n - 0.5, "title": f"{player}: {n}+",
                                    "yes_sub_title": f"{player}: {n}+"}, kseries(series), 0.07)

    def test_kalshi_props(self):
        self.assertEqual(kseries("KXMLBKS"), ("MLB", "mlb", "baseball", "FG", "PROP"))
        self.assertIsNone(kseries("KXEPLGOAL"))                  # soccer goals: rules differ, left out
        km = self.kprop("KXMLBKS", "Gerrit Cole", 7)
        self.assertEqual((km.kind, km.stat, km.player, km.op, km.line, km.teams_str),
                         ("PROP", "k", "Gerrit Cole", ">", 6.5, "NYYTB"))

    def test_polymarket_props_need_a_whole_at_least_line_and_a_player(self):
        m = polymarket.parse_market(pm_raw("astatc-mlb-nyy-tb-2026-10-03-k-gercol-gte7", "baseball_player_strikeouts",
                                           7, "Gerrit Cole"))
        self.assertEqual((m.kind, m.stat, m.player, m.op, m.line), ("PROP", "k", "Gerrit Cole", ">", 6.5))
        self.assertIsNone(polymarket.parse_market(pm_raw("astatc-mlb-nyy-tb-2026-10-03-k-gercol-gte7",
                                                         "baseball_player_strikeouts", 7)))          # no name
        self.assertIsNone(polymarket.parse_market(pm_raw("astatc-mlb-nyy-tb-2026-10-03-k-gercol-gte7",
                                                         "baseball_player_strikeouts", 6.5, "Gerrit Cole")))
        self.assertIsNone(polymarket.parse_market(pm_raw("astatc-unl-kaz-mda-2026-10-02-g-ramkar-gte1",
                                                         "soccer_player_goals", 1, "Ramazan Karimov")))

    def test_player_names_compare_without_punctuation_or_accents(self):
        self.assertEqual(matching.player_key("T.J. Hockenson"), matching.player_key("TJ Hockenson"))
        self.assertEqual(matching.player_key("T. J. Hockenson"), "tj hockenson")
        self.assertEqual(matching.player_key("José Ramírez"), "jose ramirez")
        self.assertNotEqual(matching.player_key("Luis Garcia Jr."), matching.player_key("Luis Garcia"))

    def test_same_player_and_stat_pair_across_lines(self):
        ks = [self.kprop("KXMLBKS", "Gerrit Cole", 7), self.kprop("KXMLBHA", "Gerrit Cole", 5),
              self.kprop("KXMLBKS", "Gerrit Colé", 9)]
        pms = [polymarket.parse_market(pm_raw(f"astatc-mlb-nyy-tb-2026-10-03-k-gercol-gte{n}",
                                              "baseball_player_strikeouts", n, "Gerrit Cole")) for n in (5, 7)]
        pms.append(polymarket.parse_market(pm_raw("astatc-mlb-nyy-tb-2026-10-03-k-carrod-gte7",
                                                  "baseball_player_strikeouts", 7, "Carlos Rodon")))
        g = matching.KalshiGame("MLB", "26OCT031830NYYTB", "26OCT03", "NYYTB")
        g.markets = ks
        contracts, _ = matching.build_contracts([(("mlb", "2026-10-03", "nyy", "tb"), pms, g, {"nyy": "NYY", "tb": "TB"})])
        groups = engine.group_pairs(contracts)
        self.assertEqual(list(groups), [("MLB:26OCT031830NYYTB", ("player", "k", "gerrit cole"))])
        grp = groups[("MLB:26OCT031830NYYTB", ("player", "k", "gerrit cole"))]
        self.assertEqual(sorted(c.line for c in grp["kalshi"]), [6.5, 8.5])
        self.assertEqual(sorted(c.line for c in grp["polymarket"]), [4.5, 6.5])
        k7 = next(c for c in grp["kalshi"] if c.line == 6.5)
        p5 = next(c for c in grp["polymarket"] if c.line == 4.5)
        self.assertEqual(guaranteed_payout([(k7, "no"), (p5, "yes")]), 1.0)     # 5-6 Ks pays both: $2
        self.assertEqual(engine.outcome_text(k7.var, 7, None), "Gerrit Cole: 7 or more strikeouts")
        self.assertEqual(engine.describe_var(k7.var), "Gerrit Cole: strikeouts")

    def test_prop_warning_and_auto_trade_switch(self):
        from datetime import datetime, timezone
        from unittest import mock
        from arb import config
        var = ("player", "td", "derrick henry")
        k = Contract("kalshi", "k", "NFL:G", var, ">", 0.5, "t")
        p = Contract("polymarket", "p", "NFL:G", var, ">", 0.5, "t")
        now = datetime(2026, 10, 3, tzinfo=timezone.utc)
        w = engine.warnings_for(k, p, now)
        self.assertTrue(w[0].startswith("Player prop: if Derrick Henry doesn't play"))
        row = {"warnings": w, "closes": "2026-10-03T12:00:00+00:00"}
        self.assertTrue(engine.fast_check(row, now)["ok"])
        with mock.patch.object(config, "FAST_ALLOW_PLAYER_PROPS", False):
            self.assertEqual(engine.fast_check(row, now), {"ok": False, "why": "player prop"})
