import unittest

from arb import scanner
from arb.model import Contract


class FakeMarket:
    def __init__(self, slug, yes, no):
        self.slug, self.ticker = slug, slug
        self.levels = {"yes": [(yes, 100)], "no": [(no, 100)]}
        self.yes_ask, self.no_ask = yes, no


def contract(ex, mid, game, op=">"):
    return Contract(ex, mid, game, ("total", "FG"), op, 8.5, f"{mid} total", fee_coef=0.07)


class HotPassTests(unittest.TestCase):
    def setUp(self):
        s = scanner.Scanner.__new__(scanner.Scanner)       # no network clients
        s.lock = __import__("threading").Lock()
        s.logs, s.log_to_console = __import__("collections").deque(maxlen=10), False
        s.state = {"opportunities": [], "near_misses": []}
        s.hot_groups = {}

        class K:
            def refresh_books(self, ms):
                pass

        class P:
            def refresh_quotes(self, ms):
                pass

            def refresh_book(self, pm):
                pass

        s.kalshi, s.pm = K(), P()
        # Two games, each with a 5-cent arb: Kalshi YES 0.45 + Polymarket YES (under) 0.45.
        cs, src = [], {}
        for g in ("A", "B"):
            k, p = contract("kalshi", f"k{g}", f"T:{g}"), contract("polymarket", f"p{g}", f"T:{g}", op="<")
            cs += [k, p]
            src[("kalshi", k.market_id)] = FakeMarket(k.market_id, 0.45, 0.56)
            src[("polymarket", p.market_id)] = FakeMarket(p.market_id, 0.45, 0.56)
        s.contracts, s.source = cs, src
        s.groups = scanner.engine.group_pairs(cs)
        self._sync = scanner.matching.sync_quotes
        scanner.matching.sync_quotes = lambda contracts, source: [
            setattr(c, "ask", {"yes": source[(c.exchange, c.market_id)].yes_ask,
                               "no": source[(c.exchange, c.market_id)].no_ask}) for c in contracts]
        self.s = s

    def tearDown(self):
        scanner.matching.sync_quotes = self._sync

    def test_hot_pass_keeps_rows_it_did_not_recheck(self):
        self.s.refresh_prices(hot=False)
        self.assertEqual(len(self.s.state["opportunities"]), 2)
        self.s.hot_groups = {k: v for k, v in self.s.hot_groups.items() if k[0] == "T:A"}    # hot list: game A only
        self.s.refresh_prices(hot=True)
        self.assertEqual({r["game"] for r in self.s.state["opportunities"]}, {"A", "B"})

    def test_hot_pass_drops_a_rechecked_row_that_is_gone(self):
        self.s.refresh_prices(hot=False)
        self.s.source[("polymarket", "pA")].levels["yes"] = [(0.60, 100)]
        self.s.source[("polymarket", "pA")].yes_ask = 0.60
        self.s.hot_groups = {k: v for k, v in self.s.hot_groups.items() if k[0] == "T:A"}
        self.s.refresh_prices(hot=True)
        self.assertEqual({r["game"] for r in self.s.state["opportunities"]}, {"B"})


if __name__ == "__main__":
    unittest.main()
