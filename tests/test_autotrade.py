import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from arb import autotrade, config, engine
from arb.trader import TradeError

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
SOON = (NOW + timedelta(hours=1)).isoformat()


def row(game="BTC", profit=2.0, roi=0.02, tab="Crypto", fast=True, k="KX-1", p="btc-1", **kw):
    return {"game": game, "tab": tab, "profit": profit, "roi": roi, "closes": None, "warnings": [],
            "fast": {"ok": fast, "why": "test"},
            "legs": [{"exchange": "Kalshi", "market_id": k, "side": "no"},
                     {"exchange": "Polymarket", "market_id": p, "side": "yes"}], **kw}


class FastCheckTests(unittest.TestCase):
    def check(self, **kw):
        r = {"warnings": [], "tab": "Politics", "closes": None, **kw}
        return engine.fast_check(r, NOW)

    def test_crypto_needs_to_settle_soon_too(self):
        self.assertTrue(self.check(tab="Crypto", closes=(NOW + timedelta(minutes=15)).isoformat())["ok"])
        self.assertFalse(self.check(tab="Crypto", closes=(NOW + timedelta(days=90)).isoformat())["ok"])
        self.assertFalse(self.check(tab="Crypto")["ok"])                  # no close time: not known to be soon

    def test_soon_ok_far_not(self):
        self.assertTrue(self.check(closes=(NOW + timedelta(hours=3)).isoformat())["ok"])
        self.assertFalse(self.check(closes=(NOW + timedelta(days=5)).isoformat())["ok"])
        self.assertFalse(self.check()["ok"])

    def test_rule_warnings_always_block(self):
        for w in ("ONE-WAY RULES: x", "DIFFERENT SETTLEMENT SOURCES (a vs b). x", "PRICES CONTRADICT THIS MATCH: x"):
            self.assertFalse(self.check(tab="Crypto", closes=SOON, warnings=[w])["ok"], w)
            self.assertFalse(self.check(tab="Crypto", closes=SOON, pair={"auto": True}, warnings=[w])["ok"], w)

    def test_auto_matched_allowed_unless_turned_off(self):
        auto = dict(tab="Crypto", closes=SOON, pair={"auto": True}, warnings=["AUTO-MATCHED, NOT VERIFIED: the scanner paired these"])
        with mock.patch.object(config, "FAST_ALLOW_AUTO_MATCHED", True):
            self.assertTrue(self.check(**auto)["ok"])
            soon = self.check(**{**auto, "tab": "Politics", "closes": (NOW + timedelta(hours=2)).isoformat()})
            self.assertTrue(soon["ok"])
            self.assertIn("auto-matched", soon["why"])
        with mock.patch.object(config, "FAST_ALLOW_AUTO_MATCHED", False):
            self.assertFalse(self.check(**auto)["ok"])
            self.assertFalse(self.check(tab="Crypto", closes=SOON, warnings=auto["warnings"])["ok"])

    def test_too_good_allowed_unless_turned_off(self):
        with mock.patch.object(config, "FAST_ALLOW_TOO_GOOD", True):
            self.assertTrue(self.check(tab="Crypto", closes=SOON, suspicious=True)["ok"])
        with mock.patch.object(config, "FAST_ALLOW_TOO_GOOD", False):
            self.assertFalse(self.check(tab="Crypto", closes=SOON, suspicious=True)["ok"])

    def test_far_out_still_needs_make_trade(self):
        far = self.check(tab="Politics", pair={"auto": True}, closes=(NOW + timedelta(days=200)).isoformat())
        self.assertFalse(far["ok"])


class FakeTrader:
    def __init__(self, profit=1.0, capital=20.0, result=None, fail=None):
        self.venues, self.plans, self.calls = {"kalshi": 1, "polymarket": 1}, {}, []
        self.profit, self.capital, self.fail = profit, capital, fail
        self.result = result or {"status": "ok", "plan": {"payout": 1.0}, "hedged_pairs": 20, "net": 1.0, "unhedged_shares": 0,
                                 "legs_filled": {"kalshi": {"paid": 9.0}, "polymarket": {"paid": 10.5}}}

    def prepare(self, legs, cap, timeline=None):
        self.calls.append(("prepare", cap))
        if self.fail:
            raise TradeError(self.fail)
        self.plans["p1"] = 1
        return {"id": "p1", "expected_profit": self.profit, "capital": self.capital}

    def execute(self, plan_id):
        self.calls.append(("execute", plan_id))
        return dict(self.result)


class FakeScanner:
    def __init__(self, rows, trader):
        self.lock, self.state, self.trader = threading.Lock(), {"opportunities": rows}, trader
        self.trading_status, self.logs, self.alerter = "on", [], None
        self.my_arbs = mock.Mock()

    def log(self, m):
        self.logs.append(m)

    def refresh_balances(self):
        pass


class AutoTraderTests(unittest.TestCase):
    def make(self, rows, **kw):
        s = FakeScanner(rows, FakeTrader(**kw))
        a = autotrade.AutoTrader(s, run_async=False)
        return s, a

    def test_off_by_default_and_trades_once_on(self):
        s, a = self.make([row()])
        self.assertIsNone(a.check(s.state["opportunities"]))
        a.set(True)
        self.assertIsNotNone(a.check(s.state["opportunities"]))
        self.assertEqual(s.trader.calls, [("prepare", config.AUTO_TRADE_MAX_TRADE), ("execute", "p1")])
        self.assertEqual(a.spent_today(), 19.5)
        self.assertEqual(a.status()["net_today"], 1.0)
        self.assertIsNone(a.check(s.state["opportunities"]))        # same pair: cooldown
        s.my_arbs.add_from_trade.assert_called_once()

    def test_skips_rows_not_fast_or_too_small(self):
        s, a = self.make([row(fast=False), row(roi=0.004, k="K2", p="p2")])
        a.set(True)
        self.assertIsNone(a.check(s.state["opportunities"]))

    def test_daily_limit_caps_and_stops(self):
        s, a = self.make([row()])
        a.set(True)
        a.spend[a._today()] = config.AUTO_TRADE_DAILY_LIMIT - 5
        a.check(s.state["opportunities"])
        self.assertEqual(s.trader.calls[0], ("prepare", 5))
        a.spend[a._today()] = config.AUTO_TRADE_DAILY_LIMIT
        a.tried.clear()
        self.assertIsNone(a.check(s.state["opportunities"]))

    def test_too_little_profit_at_live_prices_is_not_executed(self):
        s, a = self.make([row()], profit=0.01)
        a.set(True)
        a.check(s.state["opportunities"])
        self.assertNotIn(("execute", "p1"), s.trader.calls)
        self.assertEqual(a.history[0]["status"], "skipped")
        self.assertTrue(a.on)

    def test_halts_when_shares_left_unhedged(self):
        s, a = self.make([row()], result={"status": "partial", "hedged_pairs": 5, "net": -0.2, "unhedged_shares": 3,
                                          "legs_filled": {"kalshi": {"paid": 4}, "polymarket": {"paid": 2}}})
        a.set(True)
        a.check(s.state["opportunities"])
        self.assertFalse(a.on)
        self.assertIn("unhedged", a.halted)

    def test_fast_trade_refuses_rows_not_listed_or_not_fast(self):
        s, _ = self.make([row(fast=False)])
        with self.assertRaises(TradeError):
            autotrade.fast_trade(s, autotrade.legs_of(row()))
        with self.assertRaises(TradeError):
            autotrade.fast_trade(s, autotrade.legs_of(row(k="other")))
        self.assertEqual(s.trader.calls, [])

    def test_fast_trade_uses_fast_cap_and_budget(self):
        s, _ = self.make([row()])
        autotrade.fast_trade(s, autotrade.legs_of(row()), max_invest=10)
        self.assertEqual(s.trader.calls[0], ("prepare", 10))
        autotrade.fast_trade(s, autotrade.legs_of(row()))
        self.assertEqual(s.trader.calls[2], ("prepare", config.FAST_MAX_TRADE))


class SafetyTests(unittest.TestCase):
    def make(self, rows, **kw):
        s = FakeScanner(rows, FakeTrader(**kw))
        a = autotrade.AutoTrader(s, run_async=False)
        a.set(True)
        return s, a

    def test_games_in_play_are_skipped(self):
        live = row(warnings=["Game already started: prices move fast, and the quotes may be seconds apart."])
        s, a = self.make([live])
        self.assertIsNone(a.check(s.state["opportunities"]))
        with mock.patch.object(config, "AUTO_TRADE_LIVE_GAMES", True):
            self.assertIsNotNone(a.check(s.state["opportunities"]))

    def test_a_miss_pauses_the_whole_game(self):
        partial = {"status": "partial", "plan": {"payout": 1.0}, "hedged_pairs": 0, "net": -1.1, "unhedged_shares": 0,
                   "legs_filled": {"kalshi": {"paid": 0}, "polymarket": {"paid": 0}},
                   "steps": ["Kalshi: bought 20 YES", "Polymarket: order rejected", "Kalshi: sold back 20 unhedged YES"]}
        s, a = self.make([row(game="KTI vs KWT", k="K1", p="p1"), row(game="KTI vs KWT", k="K2", p="p2")], result=partial)
        a.check(s.state["opportunities"])
        self.assertIn("sold back", a.history[0]["note"])
        self.assertIsNone(a.check(s.state["opportunities"]))        # other line of the same game: paused

    def test_daily_loss_limit_stops_it(self):
        loss = {"status": "partial", "plan": {"payout": 1.0}, "hedged_pairs": 0, "net": -3.0, "unhedged_shares": 0,
                "legs_filled": {"kalshi": {"paid": 0}, "polymarket": {"paid": 0}}, "steps": []}
        s, a = self.make([row(game="A", k="K1", p="p1"), row(game="B", k="K2", p="p2")], result=loss)
        with mock.patch.object(config, "AUTO_TRADE_MAX_DAILY_LOSS", 5):
            a.check(s.state["opportunities"])
            self.assertTrue(a.on)
            a.check(s.state["opportunities"])
        self.assertFalse(a.on)
        self.assertIn("net loss today", a.halted)
