"""Brute-force checks of the arbitrage math against independent re-implementations."""

import random
import unittest
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal

from arb.engine import exact_hedge, size_opportunity
from arb.model import NO, YES, Contract, guaranteed_payout, kalshi_fee, polymarket_fee

VARS = [("margin", "FG", "A"), ("total", "FG"), ("tt", "FG", "A"), ("event", "x")]


def rand_contract(rng, ex, var):
    if var[0] == "event":
        return Contract(ex, ex, "G", var, rng.choice([">", "<"]), 0.5, "t")
    line = rng.choice([0.5, 1, 1.5, 2.5, 3, 3.5, 6.5, 7, 7.5, 10.5]) if var[0] != "margin" else \
        rng.choice([-10.5, -7, -3.5, -1.5, -0.5, 0.5, 1.5, 3, 3.5, 7, 7.5])
    op = rng.choice([">", "<"])
    winner = var[0] == "margin" and abs(line) == 0.5 and rng.random() < 0.3
    tie_half = var[0] == "margin" and line == 0.5 and op == ">" and rng.random() < 0.2
    return Contract(ex, ex, "G", var, op, line, "t", tie_half=tie_half, winner=winner,
                    no_draw=var[0] == "margin" and rng.random() < 0.5)


def pays(c, side, x):
    """Independent statement of the contract rules: YES pays $1 when x beats the line; a whole-number
    line landing exactly is a push worth $0 to both sides (conservative); an NFL tie pays $0.50 each."""
    if c.tie_half and x == 0:
        yes = 0.5
    elif c.op == ">":
        yes = 1.0 if x > c.line else 0.0
    else:
        yes = 1.0 if x < c.line else 0.0
    if float(c.line).is_integer() and x == c.line and not c.tie_half and not c.winner:
        return 0.0
    return yes if side == YES else 1.0 - yes


def worst_case(k, sk, p, sp):
    lo = 0 if k.var[0] != "margin" else -60
    xs = [x for x in range(lo, 61) if not (k.var[0] == "margin" and (k.no_draw or p.no_draw) and x == 0)]
    if k.var[0] == "event":
        xs = [0, 1]
    return min(pays(k, sk, x) + pays(p, sp, x) for x in xs)


def exact_fee(fills, coef, rounding):
    d = sum((Decimal(str(coef)) * Decimal(str(q)) * Decimal(str(p)) * (1 - Decimal(str(p))) for p, q in fills),
            Decimal(0))
    return float(d.quantize(Decimal("0.01"), rounding=rounding))


def take(levels, n):
    out, left = [], n
    for p, q in levels:
        if left <= 0:
            break
        t = min(q, left)
        out.append((p, t))
        left -= t
    return out


class PayoutAudit(unittest.TestCase):
    def test_guaranteed_payout_matches_every_result(self):
        rng = random.Random(7)
        for _ in range(20000):
            var = rng.choice(VARS)
            k, p = rand_contract(rng, "kalshi", var), rand_contract(rng, "polymarket", var)
            if var[0] != "margin":
                k.tie_half = p.tie_half = False
            for sk in (YES, NO):
                for sp in (YES, NO):
                    self.assertAlmostEqual(guaranteed_payout([(k, sk), (p, sp)]), worst_case(k, sk, p, sp), 9,
                                           (vars(k), sk, vars(p), sp))

    def test_exact_hedge_means_every_result_pays_one(self):
        rng = random.Random(11)
        for _ in range(5000):
            var = rng.choice(VARS)
            k, p = rand_contract(rng, "kalshi", var), rand_contract(rng, "polymarket", var)
            sk, sp = rng.choice((YES, NO)), rng.choice((YES, NO))
            if exact_hedge(k, sk, p, sp):
                self.assertEqual(worst_case(k, sk, p, sp), 1.0)


class FeeAudit(unittest.TestCase):
    def test_fee_rounding_matches_decimal_arithmetic(self):
        rng = random.Random(3)
        for _ in range(5000):
            fills = [(rng.randint(1, 99) / 100, rng.randint(1, 500)) for _ in range(rng.randint(1, 4))]
            self.assertAlmostEqual(kalshi_fee(fills, 0.07), exact_fee(fills, 0.07, ROUND_CEILING), 9)   # cent prices
            self.assertEqual(polymarket_fee(fills, 0.0695), exact_fee(fills, 0.0695, ROUND_HALF_EVEN))


class SizingAudit(unittest.TestCase):
    def test_book_walk_is_profitable_exact_and_near_best(self):
        rng = random.Random(5)
        checked = 0
        for _ in range(3000):
            k = Contract("kalshi", "k", "G", ("total", "FG"), ">", 8.5, "t", fee_coef=0.07)
            p = Contract("polymarket", "p", "G", ("total", "FG"), "<", 8.5, "t", fee_coef=0.0695)
            base = rng.uniform(0.2, 0.8)
            lk = sorted((round(min(0.99, base + rng.uniform(-0.05, 0.06)), 2), rng.randint(1, 60)) for _ in range(4))
            lp = sorted((round(min(0.99, 1 - base + rng.uniform(-0.08, 0.04)), 2), rng.randint(1, 60)) for _ in range(4))
            cand = {"k": k, "p": p, "sk": YES, "sp": YES, "payout": 1.0}
            sizing = size_opportunity(cand, lk, lp)

            def profit(n):
                fk, fp = take(lk, n), take(lp, n)
                return (n - sum(a * b for a, b in fk) - sum(a * b for a, b in fp)
                        - exact_fee(fk, 0.07, ROUND_CEILING) - exact_fee(fp, 0.0695, ROUND_HALF_EVEN))

            depth = int(min(sum(q for _, q in lk), sum(q for _, q in lp)))
            best = max((profit(n), n) for n in range(0, depth + 1))
            if sizing:
                checked += 1
                n = sizing["size"]
                self.assertGreater(profit(n), 0)                              # never a losing size
                self.assertAlmostEqual(sizing["profit"], profit(n), 9)       # reported = recomputed
                self.assertGreaterEqual(profit(n), best[0] - 0.05)            # within 5c of the best size
            else:
                self.assertLessEqual(best[0], 0.05)                           # didn't miss a real arb
        self.assertGreater(checked, 200)


if __name__ == "__main__":
    unittest.main()
