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
        s.streams, s.merge_lock, s.pairs_cat = {}, __import__("threading").Lock(), ([], {})
        s.alerter = __import__("arb.alerts", fromlist=["Alerter"]).Alerter(lambda m: None, send=lambda t: None)
        s.autotrader = __import__("arb.autotrade", fromlist=["AutoTrader"]).AutoTrader(s)

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
        s.contract_index = {(c.exchange, c.market_id): c for c in cs}
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


    def test_rows_for_markets_that_left_the_list_are_dropped(self):
        self.s.refresh_prices(hot=False)
        self.assertEqual(len(self.s.state["opportunities"]), 2)
        self.s.contract_index = {k: v for k, v in self.s.contract_index.items() if k[1] not in ("kB", "pB")}
        self.s.hot_groups = {k: v for k, v in self.s.hot_groups.items() if k[0] == "T:A"}
        self.s.refresh_prices(hot=True)                         # game B not re-checked, but its markets are gone
        self.assertEqual({r["game"] for r in self.s.state["opportunities"]}, {"A"})

    def test_full_sweep_keeps_a_row_whose_request_failed(self):
        self.s.refresh_prices(hot=False)
        km = self.s.source[("kalshi", "kA")]

        def failing(ms):                   # the request for kA errors: its book is cleared, not refreshed
            km.levels, km.yes_ask, km.no_ask = {}, None, None
            return {"kA"}
        self.s.kalshi.refresh_books = failing
        self.s.refresh_prices(hot=False)
        self.assertEqual({r["game"] for r in self.s.state["opportunities"]}, {"A", "B"})

    def test_streamed_update_rechecks_without_polling(self):
        class Live:
            connected, seen = True, {"kA", "pA", "kB", "pB"}

            def fresh(self, max_age):
                return set(self.seen)

            def status(self):
                return {"connected": True}
        self.s.streams = {"kalshi": Live(), "polymarket": Live()}

        class NoPoll:
            def __getattr__(self, name):
                raise AssertionError(f"polled {name} for a streamed market")
        self.s.kalshi, self.s.pm = NoPoll(), NoPoll()
        self.s.market_groups = {}
        for g, by_ex in self.s.groups.items():
            for lst in by_ex.values():
                for c in lst:
                    self.s.market_groups.setdefault((c.exchange, c.market_id), set()).add(g)
        self.s.dirty, self.s.dirty_lock = set(), __import__("threading").Lock()
        self.s._tick = __import__("threading").Event()
        self.s.on_stream_update("polymarket", "pA")           # a price moved on game A
        gs = set().union(*(self.s.market_groups[d] for d in self.s.dirty))
        self.s.refresh_prices(stream_groups=gs)
        self.assertEqual({r["game"] for r in self.s.state["opportunities"]}, {"A"})


    def test_full_sweep_polls_even_streamed_markets(self):
        class Live:
            connected, seen = True, {"kA", "pA", "kB", "pB"}

            def fresh(self, max_age):
                return set(self.seen)

            def status(self):
                return {"connected": True}
        Live.want = lambda self, ids: None
        self.s.streams = {"kalshi": Live(), "polymarket": Live()}
        self.s.crypto_cat = ([], {})
        polled = []
        self.s.kalshi.refresh_books = lambda ms: polled.extend(m.ticker for m in ms)
        self.s.refresh_prices(hot=False)
        self.assertEqual(sorted(polled), ["kA", "kB"])       # a quiet stream can't freeze prices

    def test_stale_streamed_market_is_polled_on_hot_pass(self):
        class Quiet:
            connected, seen = True, {"kA", "pA"}

            def fresh(self, max_age):
                return set()                                  # nothing streamed recently

            def status(self):
                return {"connected": True}
        self.s.refresh_prices(hot=False)
        self.s.streams = {"kalshi": Quiet(), "polymarket": Quiet()}
        polled = []
        self.s.kalshi.refresh_books = lambda ms: polled.extend(m.ticker for m in ms)
        self.s.refresh_prices(hot=True)
        self.assertIn("kA", polled)


if __name__ == "__main__":
    unittest.main()


class StartupTests(unittest.TestCase):
    def test_trading_status_is_published_at_start(self):
        s = scanner.Scanner.__new__(scanner.Scanner)
        s.lock = __import__("threading").Lock()
        s.logs, s.log_to_console = __import__("collections").deque(maxlen=10), False
        s.state = {"stats": {}}
        s.kalshi = type("K", (), {"auth_info": "API key, basic tier"})()
        s.trading_status = "on (cap $100 per trade)"
        s.alerter = __import__("arb.alerts", fromlist=["Alerter"]).Alerter(lambda m: None, send=lambda t: None)
        s.start_message()
        self.assertEqual(s.state["stats"]["trading"], "on (cap $100 per trade)")


class FocusTests(unittest.TestCase):
    def make(self):
        import collections
        import tempfile
        from datetime import timedelta
        from pathlib import Path
        s = scanner.Scanner.__new__(scanner.Scanner)
        s.lock, s.streams, s.state = __import__("threading").Lock(), {}, {"stats": {}}
        s.logs, s.log_to_console = collections.deque(maxlen=10), False
        now = scanner.engine.now_utc()
        cs = []
        for g, days in (("SOON", 0.5), ("WEEK", 6), ("LATER", 60)):
            k, p = contract("kalshi", f"k{g}", f"T:{g}"), contract("polymarket", f"p{g}", f"T:{g}", op="<")
            k.close_time = (now + timedelta(days=days)).isoformat()
            cs += [k, p]
        s.sports_cat, s.pairs_cat, s.crypto_cat = (cs, {}), ([], {}), ([], {})
        self.dir = tempfile.TemporaryDirectory()
        self.patch = __import__("unittest.mock", fromlist=["mock"]).patch.object(
            scanner.config, "FOCUS_FILE", Path(self.dir.name) / "focus.json")
        self.patch.start()
        s.load_focus()
        s._publish()
        return s

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_focus_scans_only_markets_settling_soon_and_is_remembered(self):
        s = self.make()
        self.assertEqual(len(s.contracts), 6)
        self.assertEqual(s.set_focus(1)["contracts"], 2)
        self.assertEqual({k[0] for k in s.groups}, {"T:SOON"})
        self.assertNotIn(("kalshi", "kLATER"), s.contract_index)
        s.set_focus(7)
        self.assertEqual({k[0] for k in s.groups}, {"T:SOON", "T:WEEK"})
        s2 = scanner.Scanner.__new__(scanner.Scanner)
        s2.load_focus()
        self.assertEqual(s2.focus_days, 7)                      # kept for the next start
        s.set_focus(0)
        self.assertEqual(len(s.contracts), 6)


class PrefundTests(unittest.TestCase):
    def make(self, shards, rows):
        import threading
        from types import SimpleNamespace
        s = scanner.Scanner.__new__(scanner.Scanner)
        s.lock = threading.Lock()
        s.logs, s.log_to_console = __import__("collections").deque(maxlen=10), False
        moved = []
        kv = SimpleNamespace(transfer=lambda src, dst, amt: moved.append((src, dst, amt)))
        s.trader = SimpleNamespace(venues={"kalshi": kv})
        s.state = {"balances": {"kalshi_shards": {str(k): v for k, v in shards.items()}, "time": "x"},
                   "opportunities": rows}
        return s, moved

    def test_tops_up_shards_with_opportunities(self):
        s, moved = self.make({0: 300.0, 2: 5.0, 3: 0.0}, [{"kalshi_shard": 2}, {"kalshi_shard": 0}])
        s.prefund_shards(now=1000)
        self.assertEqual(moved, [(0, 2, 45.0)])                  # up to one trade's worth ($50); 3 has no arbs
        self.assertTrue(s.state["balances"]["stale"])
        moved.clear()
        s.state["balances"].pop("stale")
        s.prefund_shards(now=1030)
        self.assertEqual(moved, [])                               # at most once a minute per shard

    def test_never_drains_a_shard_that_needs_its_cash(self):
        s, moved = self.make({0: 60.0, 2: 0.0}, [{"kalshi_shard": 2}, {"kalshi_shard": 0}])
        s.prefund_shards(now=1000)
        self.assertEqual(moved, [(0, 2, 10.0)])                   # shard 0 keeps its own $50
