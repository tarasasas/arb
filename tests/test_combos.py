import unittest
from datetime import datetime, timedelta, timezone

from arb import combos
from arb.model import NO, YES, Contract, total_fee

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
MARGIN = ("margin", "FG", "ARS")
TOTAL = ("total", "FG")


def c(ex, mid, var, op, line, yes=None, no=None, **kw):
    k = Contract(ex, mid, "EPL:26OCT03ARSCHE", var, op, line, f"{ex} {mid}",
                 fee_coef=0.07 if ex == "kalshi" else 0.0695,
                 close_time=(NOW + timedelta(hours=3 if ex == "kalshi" else 1)).isoformat(), winner=var[0] == "margin",
                 game_label="ARS vs CHE, Oct 3", **kw)
    k.ask = {YES: yes, NO: no}
    return k


def soccer(home=(0.40, 0.62), draw=(0.30, 0.72), away=(0.25, 0.77), away_site="polymarket"):
    """Arsenal win / draw / Chelsea win, as (YES ask, NO ask)."""
    return {(c("kalshi", "x", MARGIN, ">", 0).game_key, MARGIN): {
        "kalshi": [c("kalshi", "KHOME", MARGIN, ">", 0.0, *home), c("kalshi", "KTIE", MARGIN, "==", 0.0, *draw)]
                  + ([c("kalshi", "KAWAY", MARGIN, "<", 0.0, *away)] if away_site == "kalshi" else []),
        "polymarket": [c("polymarket", "pm-away", MARGIN, "<", 0.0, *away)] if away_site == "polymarket" else
                      [c("polymarket", "pm-home", MARGIN, ">", 0.0, 0.45, 0.58)]}}


class ThreeWayTests(unittest.TestCase):
    def test_every_result_bought_for_under_a_dollar(self):
        found = [x for x in combos.screen(soccer(), 0.0, NOW) if x["strategy"] == "3-way"]
        self.assertEqual(len(found), 1)
        cand = found[0]
        self.assertEqual(sorted((l.market_id, s) for l, s in cand["legs"]),
                         [("KHOME", YES), ("KTIE", YES), ("pm-away", YES)])
        self.assertEqual(cand["payout"], 1.0)
        fees = 0.07 * (0.4 * 0.6 + 0.3 * 0.7) + 0.0695 * 0.25 * 0.75
        self.assertAlmostEqual(cand["edge"], 1 - 0.95 - fees, 6)

    def test_all_no_pays_two(self):
        g = soccer(home=(0.70, 0.31), draw=(0.80, 0.21), away=(0.55, 0.46))     # the three NOs: 98c for $2
        found = [x for x in combos.screen(g, -1.0, NOW) if x["strategy"] == "3-way" and x["payout"] == 2.0]
        self.assertEqual(len(found), 1)
        self.assertEqual({s for _, s in found[0]["legs"]}, {NO})
        self.assertAlmostEqual(found[0]["edge"], 2 - 0.98 - sum(
            k.fee_coef * a * (1 - a) for (k, _), a in zip(found[0]["legs"], found[0]["asks"])), 6)

    def test_the_cheaper_site_is_used_for_each_result(self):
        g = soccer()
        key = next(iter(g))
        g[key]["polymarket"].append(c("polymarket", "pm-home", MARGIN, ">", 0.0, 0.38, 0.64))   # cheaper than Kalshi's 0.40
        cand = next(x for x in combos.screen(g, -1.0, NOW) if x["strategy"] == "3-way" and x["payout"] == 1.0)
        self.assertIn(("pm-home", YES), [(l.market_id, s) for l, s in cand["legs"]])

    def test_no_draw_possible_means_no_three_way(self):
        g = soccer()
        for lst in next(iter(g.values())).values():
            for k in lst:
                k.no_draw = True
        self.assertEqual([x for x in combos.screen(g, -1.0, NOW) if x["strategy"] == "3-way"], [])

    def test_a_moneyline_paying_half_on_a_tie_isnt_a_result(self):
        k = c("kalshi", "NFL", MARGIN, ">", 0.0, 0.4, 0.6, tie_half=True)
        self.assertIsNone(combos.region_of(k, YES))
        self.assertEqual(combos.region_of(c("kalshi", "S", MARGIN, ">", 0.5), YES), ("win", True))
        self.assertEqual(combos.region_of(c("kalshi", "T", MARGIN, "==", 0.0), NO), ("draw", False))
        self.assertIsNone(combos.region_of(c("kalshi", "S2", MARGIN, ">", 1.5), YES))   # wins by 2+: not a result

    def test_overpriced_isnt_listed(self):
        self.assertEqual(combos.screen(soccer(home=(0.45, 0.57)), 0.0, NOW), [])


class SameSiteTests(unittest.TestCase):
    def ladder(self, o45=(0.40, 0.62), o55=(0.47, 0.50), ex="kalshi"):
        g = {"kalshi": [], "polymarket": [c("polymarket", "pm-o5", TOTAL, ">", 5.5, 0.6, 0.45)]}
        g[ex] += [c(ex, "O45", TOTAL, ">", 4.5, *o45), c(ex, "O55", TOTAL, ">", 5.5, *o55)]
        return {("EPL:x", TOTAL): g}

    def test_an_inverted_ladder_on_one_site(self):
        found = [x for x in combos.screen(self.ladder(), 0.0, NOW) if x["strategy"] == "same-site"]
        self.assertEqual(len(found), 1)
        self.assertEqual([(l.market_id, s) for l, s in found[0]["legs"]], [("O45", YES), ("O55", NO)])
        self.assertEqual(found[0]["payout"], 1.0)
        self.assertEqual({l.exchange for l, _ in found[0]["legs"]}, {"kalshi"})

    def test_polymarket_too(self):
        g = self.ladder(ex="polymarket")
        g[("EPL:x", TOTAL)]["polymarket"] = g[("EPL:x", TOTAL)]["polymarket"][1:]
        found = [x for x in combos.screen(g, 0.0, NOW) if x["strategy"] == "same-site"]
        self.assertEqual({l.exchange for x in found for l, _ in x["legs"]}, {"polymarket"})

    def test_a_consistent_ladder_has_none(self):
        self.assertEqual(combos.screen(self.ladder(o45=(0.60, 0.42), o55=(0.47, 0.55)), 0.0, NOW), [])

    def test_never_both_sides_of_one_market(self):
        k = c("kalshi", "M", TOTAL, ">", 4.5, 0.30, 0.30)          # crossed quotes on one market: not a combo
        self.assertEqual(combos.screen({("g", TOTAL): {"kalshi": [k, k], "polymarket": []}}, 0.0, NOW), [])

    def test_non_sports_quantities_are_left_to_the_pairs(self):
        ev = ("event", "x")
        g = {("POL:x", ev): {"kalshi": [c("kalshi", "A", ev, ">", 0.5, 0.3, 0.3), c("kalshi", "B", ev, ">", 0.5, 0.3, 0.3)],
                             "polymarket": []}}
        self.assertEqual(combos.screen(g, -1.0, NOW), [])


class SizeAndRowTests(unittest.TestCase):
    def test_walks_every_book_while_each_set_makes_money(self):
        cand = next(x for x in combos.screen(soccer(), 0.0, NOW) if x["strategy"] == "3-way")
        books = {"KHOME": [(0.40, 100), (0.43, 100)], "KTIE": [(0.30, 60), (0.30, 500)], "pm-away": [(0.25, 1000)]}
        levels = [books[l.market_id] for l, _ in cand["legs"]]
        s = combos.size(cand["legs"], levels, cand["payout"])
        self.assertEqual(s["size"], 100)                       # the 0.43 level makes the set lose money
        capital = sum(total_fee(l.exchange, combos._take(lv, 100), l.fee_coef) + sum(p * q for p, q in combos._take(lv, 100))
                      for (l, _), lv in zip(cand["legs"], levels))
        self.assertAlmostEqual(s["capital"], capital, 6)
        self.assertAlmostEqual(s["profit"], 100 - capital, 6)
        self.assertIsNone(combos.size(cand["legs"], levels[:2] + [[]], cand["payout"]))

    def test_row_has_every_leg_and_each_result(self):
        cand = next(x for x in combos.screen(soccer(), 0.0, NOW) if x["strategy"] == "3-way")
        levels = [[(a, 100)] for a in cand["asks"]]
        row = combos.to_row(cand, combos.size(cand["legs"], levels, cand["payout"]), NOW, {"KHOME": 0, "KTIE": 0})
        self.assertEqual((row["kind"], row["strategy"], len(row["legs"]), row["size"]), ("combo", "3-way", 3, 100))
        self.assertEqual([l["shard"] for l in row["legs"]][:2], [0, 0])
        self.assertEqual({sc["total"] for sc in row["scenarios"]}, {1.0})        # every result pays $1
        self.assertEqual(len(row["scenarios"]), 3)
        self.assertEqual(row["closes"], (NOW + timedelta(hours=3)).isoformat())   # Kalshi's settle time, not the start
        self.assertGreater(row["profit"], 0)

    def test_started_game_is_flagged(self):
        cand = next(x for x in combos.screen(soccer(), 0.0, NOW) if x["strategy"] == "3-way")
        row = combos.to_row(cand, None, NOW + timedelta(hours=2))
        self.assertTrue(any(w.startswith("Game already started") for w in row["warnings"]))


if __name__ == "__main__":
    unittest.main()
