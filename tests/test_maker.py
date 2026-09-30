import unittest

from arb import engine
from arb.model import NO, YES, Contract, polymarket_fee


class PM:
    def __init__(self, yes_ask, no_ask, tick=0.01):
        self.yes_ask, self.no_ask, self.tick = yes_ask, no_ask, tick


def cand(ak=0.45, ap=0.56, sp=YES):
    k = Contract("kalshi", "k", "G", ("total", "FG"), ">", 8.5, "k", fee_coef=0.07)
    p = Contract("polymarket", "p", "G", ("total", "FG"), "<" if sp == YES else ">", 8.5, "p", fee_coef=0.0695)
    return {"k": k, "sk": YES, "p": p, "sp": sp, "ak": ak, "ap": ap, "payout": 1.0,
            "edge": 1.0 - ak - ap - 0.07 * ak * (1 - ak) - 0.0695 * ap * (1 - ap)}


class MakerTests(unittest.TestCase):
    def test_quote_steps_inside_the_spread_or_joins(self):
        # YES bid 0.50 (NO ask 0.50), YES ask 0.53: rest a YES bid one tick up at 0.51.
        self.assertEqual(engine.maker_quote(PM(0.53, 0.50), YES), (0.51, 0.51))
        # Buy NO = rest an offer to sell YES one tick under the ask: 0.52, costing 0.48.
        self.assertEqual(engine.maker_quote(PM(0.53, 0.50), NO), (0.52, 0.48))
        # One-tick spread: join the best price instead of crossing it.
        self.assertEqual(engine.maker_quote(PM(0.51, 0.50), YES), (0.50, 0.50))
        self.assertEqual(engine.maker_quote(PM(0.51, 0.50), NO), (0.51, 0.49))
        self.assertIsNone(engine.maker_quote(PM(None, 0.5), YES))
        self.assertIsNone(engine.maker_quote(PM(0.37, 0.95), NO, max_spread=0.03))   # 5c bid / 37c ask: too wide

    def test_rebate_turns_a_near_miss_into_an_arb(self):
        c = cand(ak=0.40, ap=0.60)                       # 40 + 60 = 100c plus fees: a loss as a taker
        self.assertLess(c["edge"], 0)
        mc = engine.maker_candidate(c, PM(0.60, 0.47), 0.0125)   # YES bid 0.53 -> rest at 0.54
        self.assertEqual(mc["maker"]["cost"], 0.54)
        expected = 1 - 0.40 - 0.54 - 0.07 * 0.40 * 0.60 + 0.0125 * 0.54 * 0.46
        self.assertAlmostEqual(mc["edge"], expected, 9)
        self.assertGreater(mc["edge"], 0)
        self.assertEqual(mc["p"].fee_coef, -0.0125)
        self.assertEqual(c["p"].fee_coef, 0.0695)       # the original contract is untouched

    def test_sizing_uses_the_rebate(self):
        mc = engine.maker_candidate(cand(ak=0.40, ap=0.60), PM(0.60, 0.47), 0.0125)
        sizing = engine.size_opportunity(mc, [(0.40, 100), (0.47, 100)], [(mc["ap"], 1e9)])
        self.assertEqual(sizing["size"], 100)            # the 47c Kalshi level isn't profitable
        self.assertEqual(sizing["fee_p"], polymarket_fee([(0.54, 100)], -0.0125))
        self.assertLess(sizing["fee_p"], 0)             # a rebate: money back
        self.assertAlmostEqual(sizing["profit"], 100 - 40 - 54 - sizing["fee_k"] - sizing["fee_p"], 9)

    def test_hedge_limit_is_the_highest_profitable_kalshi_price(self):
        lim = engine.hedge_limit(1.0, 0.54, 0.0125, 0.07)
        self.assertEqual(lim, 0.44)                     # 44c + fee fits under 46c + rebate; 45c + fee doesn't
        room = 1 - 0.54 + 0.0125 * 0.54 * 0.46
        self.assertLess(lim + 0.07 * lim * (1 - lim), room)
        self.assertGreaterEqual(0.45 + 0.07 * 0.45 * 0.55, room)


if __name__ == "__main__":
    unittest.main()
