"""Auto-trade robustness: stale side first, fast-market edge and learned buffer, per-type throttle,
paper trading, both legs at once where it helps, and never the whole book."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from arb import autotrade, config, execpolicy
from arb import trader as trader_mod
from arb.model import NO, YES
from arb.trader import TradeError

from tests.test_trader import LEGS, FakeVenue, make

SLEEP = mock.patch.object(trader_mod.time, "sleep", lambda _s: None)
LOG = mock.patch.object(trader_mod.config, "TRADES_LOG", Path(tempfile.gettempdir()) / "arb_test_trades.jsonl")


def row(league="MLB", tab="Sports", warnings=(), edge=0.05, **kw):
    return {"game": "NYY vs TB", "league": league, "tab": tab, "warnings": list(warnings), "profit": 1.0, "roi": 0.05,
            "edge_per_contract": edge, "fast": {"ok": True, "why": "t"}, "closes": None,
            "legs": [{"exchange": "Kalshi", "market_id": "K", "side": "yes"},
                     {"exchange": "Polymarket", "market_id": "P", "side": "no"}], **kw}


class CategoryTests(unittest.TestCase):
    def test_market_types(self):
        self.assertEqual(execpolicy.category(row()), "MLB")
        self.assertEqual(execpolicy.category(row(warnings=["Game already started: x"])), "MLB live")
        self.assertEqual(execpolicy.category(row(league="CRYPTO", tab="Crypto")), "Crypto windows")
        self.assertEqual(execpolicy.category(row(league="politics", tab="Politics")), "Politics")
        self.assertTrue(execpolicy.is_fast("Crypto windows") and execpolicy.is_fast("NFL live"))
        self.assertFalse(execpolicy.is_fast("MLB"))


class Stream:
    connected = True

    def __init__(self, times):
        self.updated_at, self.seen = dict(times), set(times)


class StaleSideTests(unittest.TestCase):
    def scanner(self, k_age, p_age, now):
        s = type("S", (), {})()
        s.streams = {"kalshi": Stream({"K": now - k_age}), "polymarket": Stream({"P": now - p_age})}
        return s

    def legs(self):
        return [{"exchange": "kalshi", "market_id": "K"}, {"exchange": "polymarket", "market_id": "P"}]

    def test_the_site_that_hasnt_moved_goes_first(self):
        now = time.time()
        # Polymarket just repriced (0.5s ago); Kalshi's price is 20s old: Kalshi is about to catch up
        self.assertEqual(execpolicy.stale_side(self.scanner(20, 0.5, now), self.legs(), now), "kalshi")
        self.assertEqual(execpolicy.stale_side(self.scanner(0.3, 30, now), self.legs(), now), "polymarket")

    def test_a_snapshot_after_subscribing_is_not_a_move(self):
        now = time.time()
        s = self.scanner(20, 0.5, now)
        s.streams["polymarket"].first_seen = {"P": now - 0.5}      # Polymarket's only message: its first book
        self.assertIsNone(execpolicy.stale_side(s, self.legs(), now))
        s.streams["polymarket"].first_seen = {"P": now - 40}       # subscribed long ago, then moved
        self.assertEqual(execpolicy.stale_side(s, self.legs(), now), "kalshi")

    def test_unclear_when_both_moved_both_quiet_or_not_streamed(self):
        now = time.time()
        self.assertIsNone(execpolicy.stale_side(self.scanner(1, 0.5, now), self.legs(), now))      # both just moved
        self.assertIsNone(execpolicy.stale_side(self.scanner(60, 30, now), self.legs(), now))      # both quiet
        s = self.scanner(20, 0.5, now)
        s.streams["kalshi"].seen = set()                                                           # no book yet
        self.assertIsNone(execpolicy.stale_side(s, self.legs(), now))


class ChooseOrderTests(unittest.TestCase):
    def setUp(self):
        self.stats = execpolicy.ExecStats()
        self.s = type("S", (), {"streams": {}})()

    def test_smart_order(self):
        with mock.patch.object(config, "AUTO_TRADE_ORDER", "smart"):
            self.assertEqual(execpolicy.choose_order(self.s, row(), self.stats, "MLB")[:2], ("thinner_first", None))
            self.assertEqual(execpolicy.choose_order(self.s, row(), self.stats, "Crypto windows")[:2], ("together", None))
            for _ in range(3):
                self.stats.record("MLB", "partial", -0.2, 0.02, "thinner_first")
            mode, first, why = execpolicy.choose_order(self.s, row(), self.stats, "MLB")
            self.assertEqual(mode, "together")
            self.assertIn("second legs missed", why)
            now = time.time()
            self.s.streams = {"kalshi": Stream({"K": now - 20}), "polymarket": Stream({"P": now - 0.2})}
            self.assertEqual(execpolicy.choose_order(self.s, row(), self.stats, "Crypto windows")[:2],
                             ("thinner_first", "kalshi"))           # stale side first beats "together"

    def test_your_setting_wins(self):
        with mock.patch.object(config, "AUTO_TRADE_ORDER", "polymarket_first"):
            self.assertEqual(execpolicy.choose_order(self.s, row(), self.stats, "Crypto windows"),
                             ("polymarket_first", None, "your setting"))


class StatsTests(unittest.TestCase):
    def test_fast_markets_need_more_edge_and_buffers_are_learned(self):
        st = execpolicy.ExecStats()
        self.assertEqual(st.min_edge("MLB")[0], 0.0)
        self.assertEqual(st.min_edge("NFL live")[0], config.AUTO_TRADE_FAST_EDGE)
        for slip in (0.01, 0.028, 0.03, 0.04):                      # moves met while the orders went out
            st.record("Crypto windows", "partial", -0.4, slip, "thinner_first")
        need, why = st.min_edge("Crypto windows")
        self.assertEqual(need, 0.04)                                   # 75th percentile beats the 2c floor
        self.assertIn("typical price move", why)
        with mock.patch.object(config, "AUTO_TRADE_LEARN_BUFFER", False):
            self.assertEqual(st.min_edge("Crypto windows")[0], config.AUTO_TRADE_FAST_EDGE)

    def test_a_type_that_keeps_missing_is_paused_then_resumed(self):
        clock = [1000.0]
        st = execpolicy.ExecStats(now=lambda: clock[0])
        whys = [st.record("Crypto windows", s, 0.0, None, "thinner_first")
                for s in ("ok", "no_fill", "no_fill", "partial", "no_fill")]
        self.assertIsNone(whys[3])
        self.assertIn("only 20%", whys[4])
        self.assertTrue(st.paused_why("Crypto windows"))
        clock[0] += config.AUTO_TRADE_THROTTLE_HOURS * 3600 + 1
        self.assertIsNone(st.paused_why("Crypto windows"))           # the pause runs out
        st.paused["MLB"] = (clock[0] + 99, "x")
        st.resume("MLB")
        self.assertIsNone(st.paused_why("MLB"))

    def test_a_losing_type_is_paused_even_when_it_fills(self):
        st = execpolicy.ExecStats()
        whys = [st.record("NHL", "ok", n, None, "thinner_first") for n in (0.1, 0.1, -0.5, 0.1, 0.1)]
        self.assertIn("lost $0.10", whys[-1])

    def test_paper_results_never_pause_and_skipped_ones_dont_count(self):
        st = execpolicy.ExecStats()
        for _ in range(6):
            st.record("MLB", "no_fill", 0.0, None, "thinner_first", paper=True)
            st.record("MLB", "skipped", 0.0)
        self.assertIsNone(st.paused_why("MLB"))
        s = next(x for x in st.summary() if x["category"] == "MLB")
        self.assertEqual((s["tries"], s["paper_tries"]), (0, 6))

    def test_kept_across_restarts(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "exec_stats.json"
            st = execpolicy.ExecStats(path)
            st.record("MLB", "ok", 0.3, 0.004, "thinner_first")
            st.paused["NHL"] = (time.time() + 600, "lost")
            st._save()
            again = execpolicy.ExecStats(path)
            self.assertEqual(len(again.results["MLB"]), 1)
            self.assertEqual(again.paused_why("NHL"), "lost")


@SLEEP
@LOG
class TraderOrderTests(unittest.TestCase):
    def test_caller_picks_the_first_leg(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 1000)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        self.assertEqual(make(k, p).prepare(LEGS, order="thinner_first")["first"], "polymarket")   # thinner
        self.assertEqual(make(k, p).prepare(LEGS, order="thinner_first", first="kalshi")["first"], "kalshi")
        self.assertTrue(make(k, p).prepare(LEGS, order="together", first="kalshi")["together"])

    def test_never_more_than_a_share_of_the_shown_book(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 20), (0.58, 500)])     # 20 shares at a profitable price
        self.assertEqual(make(k, p).prepare(LEGS)["size"], 20)
        self.assertEqual(make(k, p).prepare(LEGS, book_share=0.5)["size"], 10)
        thin = FakeVenue("polymarket", no=[(0.50, 1)])
        with self.assertRaises(TradeError):
            make(k, thin).prepare(LEGS, book_share=0.5)


@SLEEP
@LOG
class PaperTradeTests(unittest.TestCase):
    def test_paper_plan_ignores_cash_and_moves_none(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)], balance=0.0)
        p = FakeVenue("polymarket", no=[(0.50, 100)], balance=0.0)
        k.fund_shard = mock.Mock(side_effect=AssertionError("moved cash on a paper trade"))
        plan = make(k, p).prepare(LEGS, dry=True)
        self.assertEqual((plan["size"], plan["dry"]), (100, True))

    def test_both_legs_would_fill(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 100)])
        t = make(k, p)
        res = t.simulate(t.prepare(LEGS, order="thinner_first", dry=True)["id"])
        self.assertEqual((res["status"], res["hedged_pairs"], res["paper"]), ("ok", 100, True))
        self.assertGreater(res["net"], 0)
        self.assertEqual(k.orders + p.orders, [])                     # nothing was sent

    def test_second_leg_that_moved_is_a_paper_miss_with_its_slip(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)], no=[(0.55, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 20)], yes=[(0.48, 100)])
        t = make(k, p)
        plan = t.prepare(LEGS, order="thinner_first", dry=True)          # Polymarket (thinner) first
        k.book["yes"] = [(0.48, 100)]                                       # Kalshi reprices meanwhile
        res = t.simulate(plan["id"])
        self.assertEqual(res["status"], "partial")
        self.assertEqual(res["missed"][0]["exchange"], "Kalshi")
        self.assertAlmostEqual(res["slip"], 0.08, places=4)                 # planned 0.40, Kalshi at 0.48 by then
        self.assertEqual(k.orders + p.orders, [])

    def test_first_leg_gone_is_a_paper_no_fill(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 20)])
        t = make(k, p)
        plan = t.prepare(LEGS, order="thinner_first", dry=True)
        p.book["no"] = [(0.53, 20)]
        res = t.simulate(plan["id"])
        self.assertEqual((res["status"], res["hedged_pairs"]), ("no_fill", 0))
        self.assertAlmostEqual(res["slip"], 0.03, places=4)

    def test_together_reads_each_book_when_its_order_would_land(self, *_):
        k = FakeVenue("kalshi", yes=[(0.40, 100)])
        p = FakeVenue("polymarket", no=[(0.50, 100)])
        t = make(k, p)
        waits = []
        res = t.simulate(t.prepare(LEGS, order="together", dry=True)["id"], sleep=waits.append)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(waits[:2], [config.PAPER_LATENCY["kalshi"],
                                     config.PAPER_LATENCY["polymarket"] - config.PAPER_LATENCY["kalshi"]])


class FakeTrader:
    def __init__(self, sim=None):
        self.venues, self.plans, self.calls = {"kalshi": 1, "polymarket": 1}, {}, []
        self.sim = sim or {"status": "ok", "hedged_pairs": 10, "net": 0.4, "unhedged_shares": 0, "slip": 0.002,
                           "order_mode": "thinner_first", "steps": ["Kalshi: would have bought 10"], "missed": [],
                           "paper": True}

    def prepare(self, legs, cap, timeline=None, hedge_depth=1.0, order=None, **kw):
        self.calls.append(("prepare", order, kw))
        self.plans["p1"] = 1
        return {"id": "p1", "expected_profit": 0.4, "capital": 9.6, "size": 10}

    def execute(self, plan_id):
        raise AssertionError("a paper trade must not execute")

    def simulate(self, plan_id, latency=None):
        self.calls.append(("simulate", plan_id))
        return dict(self.sim)


class FakeScanner:
    def __init__(self, rows, trader):
        self.lock, self.state, self.trader = threading.Lock(), {"opportunities": rows}, trader
        self.trading_status, self.logs, self.alerter, self.streams = "on", [], None, {}
        self.my_arbs = mock.Mock()

    def log(self, m):
        self.logs.append(m)

    def refresh_balances(self):
        pass


class AutoTradeIntegrationTests(unittest.TestCase):
    def make(self, rows, trader=None):
        s = FakeScanner(rows, trader or FakeTrader())
        a = autotrade.AutoTrader(s, run_async=False)
        a.set(True)
        return s, a

    def test_dry_run_simulates_and_spends_nothing(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(config, "AUTO_TRADE_DRY_RUN", True), \
                mock.patch.object(config, "PAPER_LOG", Path(d) / "paper.jsonl"):
            s, a = self.make([row()])
            a.check(s.state["opportunities"])
            self.assertEqual([c[0] for c in s.trader.calls], ["prepare", "simulate"])
            self.assertTrue(s.trader.calls[0][2]["dry"])
            self.assertEqual((a.spent_today(), a.history[0]["paper"], a.history[0]["status"]), (0, True, "ok"))
            s.my_arbs.add_from_trade.assert_not_called()
            line = json.loads((Path(d) / "paper.jsonl").read_text().splitlines()[0])
            self.assertEqual((line["category"], line["status"]), ("MLB", "ok"))
            self.assertEqual(a.stats.summary()[0]["paper_tries"], 1)

    def test_paused_types_and_thin_edges_are_not_tried(self):
        s, a = self.make([row(league="CRYPTO", tab="Crypto", edge=0.01)])
        self.assertIsNone(a.check(s.state["opportunities"]))            # crypto windows need 2c
        s.state["opportunities"][0]["edge_per_contract"] = 0.03
        a.stats.paused["Crypto windows"] = (time.time() + 600, "kept missing")
        self.assertIsNone(a.check(s.state["opportunities"]))
        a.resume("Crypto windows")
        self.assertIsNotNone(a.check(s.state["opportunities"]))
        _, order, kw = s.trader.calls[0]
        self.assertEqual((order, kw.get("book_share")), ("together", config.AUTO_TRADE_BOOK_SHARE))

    def test_stale_side_is_sent_first(self):
        now = time.time()
        s, a = self.make([row()])
        s.streams = {"kalshi": Stream({"K": now - 30}), "polymarket": Stream({"P": now - 0.1})}
        with mock.patch.object(config, "AUTO_TRADE_DRY_RUN", True), \
                mock.patch.object(config, "PAPER_LOG", Path(tempfile.gettempdir()) / "arb_test_paper.jsonl"):
            a.check(s.state["opportunities"])
        _, order, kw = s.trader.calls[0]
        self.assertEqual((order, kw.get("first")), ("thinner_first", "kalshi"))
        self.assertIn("Kalshi first", a.history[0]["order"])

    def test_real_results_feed_the_throttle(self):
        miss = {"status": "partial", "hedged_pairs": 0, "net": -0.3, "unhedged_shares": 0, "slip": 0.03,
                "order_mode": "thinner_first", "steps": [], "missed": [{"exchange": "Kalshi", "why": "moved", "gap": 0.03}],
                "legs_filled": {}, "plan": {"payout": 1.0}}
        t = FakeTrader()
        t.execute = lambda plan_id: dict(miss)
        s, a = self.make([row()], t)
        with mock.patch.object(config, "AUTO_TRADE_MAX_MISSES", 99), \
                mock.patch.object(config, "AUTO_TRADE_MAX_DAILY_LOSS", 99), \
                mock.patch.object(config, "AUTO_TRADE_GAME_COOLDOWN_SECS", 0), \
                mock.patch.object(config, "AUTO_TRADE_COOLDOWN_SECS", 0):
            for _ in range(5):
                a.check(s.state["opportunities"])
        self.assertIn("MLB paused", a.history[0]["paused"])
        self.assertIsNone(a.check(s.state["opportunities"]))


if __name__ == "__main__":
    unittest.main()
