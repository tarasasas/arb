import unittest
from unittest import mock

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
        s.makerbot = __import__("arb.maker", fromlist=["MakerBot"]).MakerBot(s)

        class K:
            def refresh_books(self, ms):
                pass

            def refresh_tops(self, ms):
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
        self.s.kalshi.refresh_tops = self.s.kalshi.refresh_books = failing
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
        self.s.kalshi.refresh_tops = lambda ms: polled.extend(m.ticker for m in ms)   # full sweeps read the list
        self.s.refresh_prices(hot=False)
        self.assertEqual(sorted(polled), ["kA", "kB"])       # a quiet stream can't freeze prices

    def test_full_sweep_reads_list_prices_then_books_for_candidates(self):
        tops, books = [], []
        self.s.kalshi.refresh_tops = lambda ms: tops.extend(m.ticker for m in ms)
        self.s.kalshi.refresh_books = lambda ms: books.extend(m.ticker for m in ms)
        self.s.refresh_prices(hot=False)
        self.assertEqual(sorted(tops), ["kA", "kB"])
        self.assertEqual(sorted(books), ["kA", "kB"])         # depth for the two arbs, fetched once
        self.assertEqual(len(self.s.state["opportunities"]), 2)

    def test_hot_list_keeps_only_the_closest_pairs(self):
        with mock.patch.object(scanner.config, "HOT_MAX_PAIRS", 1):
            self.s.refresh_prices(hot=False)
        self.assertEqual(len(self.s.hot_groups), 1)

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


class FastLaneTests(unittest.TestCase):
    """The pairs Auto-trade could take get their own, faster pass (and the first stream slots)."""
    tearDown = HotPassTests.tearDown

    def setUp(self):
        HotPassTests.setUp(self)                             # the same two games, each with an arb
        from datetime import timedelta
        self.now = scanner.engine.now_utc()
        iso = lambda h: (self.now + timedelta(hours=h)).isoformat()
        times = {"A": (iso(3), iso(1)), "B": (iso(72), iso(70))}       # (Kalshi pays out, Polymarket game start)
        for c in self.s.contracts:
            g = c.game_key[-1]
            c.close_time = times[g][0] if c.exchange == "kalshi" else times[g][1]
        self.s.groups = scanner.engine.group_pairs(self.s.contracts)

    def lane_games(self):
        return [g[0] for g in self.s.lane_groups()]

    def test_only_pairs_paying_out_within_the_hours_soonest_first(self):
        self.assertEqual(self.lane_games(), ["T:A"])
        self.s._lane_cache = None
        with mock.patch.object(scanner.config, "FAST_MAX_HOURS", 100):
            self.assertEqual(self.lane_games(), ["T:A", "T:B"])

    def test_games_under_way_are_left_out_unless_live_games_are_on(self):
        from datetime import timedelta
        for c in self.s.contracts:
            if c.exchange == "polymarket" and c.game_key == "T:A":
                c.close_time = (self.now - timedelta(minutes=10)).isoformat()
        self.assertEqual(self.lane_games(), [])
        self.s._lane_cache = None
        with mock.patch.object(scanner.config, "AUTO_TRADE_LIVE_GAMES", True):
            self.assertEqual(self.lane_games(), ["T:A"])

    def test_lane_pass_polls_only_its_markets_and_hands_rows_to_auto_trade(self):
        self.s.refresh_prices(hot=False)                     # full sweep: both rows
        tops, books, seen = [], [], []
        self.s.kalshi.refresh_tops = lambda ms: tops.extend(m.ticker for m in ms)
        self.s.kalshi.refresh_books = lambda ms: books.extend(m.ticker for m in ms)
        self.s.autotrader.check = lambda rows: seen.append(sorted(r["game"] for r in rows))
        self.s.state["near_misses"] = ["from the full sweep"]
        self.s.refresh_prices(lane=True)
        self.assertEqual((tops, books), (["kA"], ["kA"]))    # list price for the lane, depth for its arb
        self.assertEqual(seen, [["A", "B"]])                  # B's row kept from the full sweep
        self.assertEqual((self.s.state["lane"]["pairs"], self.s.state["lane"]["markets"]), (1, 2))
        self.assertEqual(self.s.state["near_misses"], ["from the full sweep"])   # a lane pass leaves these alone
        self.assertNotIn("hot_seconds", self.s.state)

    def test_active_by_mode(self):
        for mode, on, want in (("auto", False, False), ("auto", True, True), ("always", False, True), ("off", True, False)):
            self.s.autotrader.on = on
            with mock.patch.object(scanner.config, "FAST_LANE", mode):
                self.assertEqual(self.s.lane_active(), want, (mode, on))

    def test_lane_markets_get_the_stream_slots_first(self):
        got = {}

        class S:
            def __init__(self, ex):
                self.ex = ex

            def want(self, ids):
                got[self.ex] = ids
        self.s.streams = {"kalshi": S("kalshi"), "polymarket": S("polymarket")}
        self.s.crypto_cat = ([], {})
        self.s._stream_pairs = [("kB", "pB"), ("kA", "pA")]   # B is closer to an arb
        with mock.patch.object(scanner.config, "STREAM_MAX_MARKETS", 1):
            self.s.autotrader.on = False
            self.s._apply_stream_wants()
            self.assertEqual(got["kalshi"], ["kB"])
            self.s.autotrader.on = True
            self.s._apply_stream_wants()
            self.assertEqual((got["kalshi"], got["polymarket"]), (["kA"], ["pA"]))


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


class PairingIgnoresFocusTests(FocusTests):
    def test_positions_outside_focus_still_pair(self):
        s = self.make()
        s.set_focus(1)
        self.assertIsNone(s.find_contract("kalshi", "kLATER"))           # not scanned
        self.assertIsNotNone(s.find_any_contract("kalshi", "kLATER"))    # but still known for your positions
