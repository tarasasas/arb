import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from arb import autotrade, config, engine
from arb.trader import TradeError

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


def row(game="BTC", profit=2.0, roi=0.02, tab="Crypto", fast=True, k="KX-1", p="btc-1", **kw):
    return {"game": game, "tab": tab, "profit": profit, "roi": roi, "closes": None, "warnings": [],
            "fast": {"ok": fast, "why": "test"},
            "legs": [{"exchange": "Kalshi", "market_id": k, "side": "no"},
                     {"exchange": "Polymarket", "market_id": p, "side": "yes"}], **kw}


class FastCheckTests(unittest.TestCase):
    def check(self, **kw):
        r = {"warnings": [], "tab": "Politics", "closes": None, **kw}
        return engine.fast_check(r, NOW)

    def test_crypto_ok(self):
        self.assertTrue(self.check(tab="Crypto")["ok"])

    def test_soon_ok_far_not(self):
        self.assertTrue(self.check(closes=(NOW + timedelta(hours=3)).isoformat())["ok"])
        self.assertFalse(self.check(closes=(NOW + timedelta(days=5)).isoformat())["ok"])
        self.assertFalse(self.check()["ok"])

    def test_never_auto_matched_suspicious_or_flagged(self):
        self.assertFalse(self.check(tab="Crypto", suspicious=True)["ok"])
        self.assertFalse(self.check(tab="Crypto", pair={"auto": True})["ok"])
        for w in ("ONE-WAY RULES: x", "DIFFERENT SETTLEMENT SOURCES (a vs b). x", "PRICES CONTRADICT THIS MATCH: x"):
            self.assertFalse(self.check(tab="Crypto", warnings=[w])["ok"], w)


class FakeTrader:
    def __init__(self, profit=1.0, capital=20.0, result=None, fail=None):
        self.venues, self.plans, self.calls = {"kalshi": 1, "polymarket": 1}, {}, []
        self.profit, self.capital, self.fail = profit, capital, fail
        self.result = result or {"status": "ok", "plan": {"payout": 1.0}, "hedged_pairs": 20, "net": 1.0, "unhedged_shares": 0,
                                 "legs_filled": {"kalshi": {"paid": 9.0}, "polymarket": {"paid": 10.5}}}

    def prepare(self, legs, cap):
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
        s, a = self.make([row(fast=False), row(profit=0.01, k="K2", p="p2")])
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
