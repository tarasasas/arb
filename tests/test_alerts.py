import os
import unittest
from unittest import mock

from arb import alerts


def row(profit=12.4, key="K1", suspicious=False, warnings=()):
    return {"profit": profit, "roi": 0.012, "tab": "Politics", "game": "CA-39 — Manos", "size": 120,
            "suspicious": suspicious, "warnings": list(warnings),
            "legs": [{"exchange": "Kalshi", "market_id": key, "side": "yes", "price": 0.13},
                     {"exchange": "Polymarket", "market_id": "p1", "side": "no", "price": 0.86}],
            "book": {"kalshi": [[0.12, 60], [0.13, 60]], "polymarket": [[0.86, 120]]}}


class AlertTests(unittest.TestCase):
    def make(self, **env):
        sent = []
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example/hook", "ALERT_MIN_PROFIT": "5", **env}):
            a = alerts.Alerter(lambda m: None, send=sent.append)
        a._safe_send = sent.append                                      # send inline for the test
        return a, sent

    def test_alerts_once_for_a_new_arb_with_what_to_buy(self):
        a, sent = self.make()
        self.assertEqual(len(a.check([row()])), 1)
        self.assertEqual(a.check([row()]), [])                           # same arb again: cooldown
        self.assertIn("Kalshi: Buy YES 120 @ ≤ 13.0¢", sent[0])          # limit = worst fill, not the top
        self.assertIn("Polymarket: Buy NO 120 @ ≤ 86.0¢", sent[0])
        self.assertIn("$12.40 arb", sent[0])

    def test_bigger_profit_alerts_again(self):
        a, sent = self.make()
        a.check([row(10)])
        self.assertEqual(len(a.check([row(16)])), 1)                     # grew by half or more

    def test_skips_small_suspicious_and_trap_rows(self):
        a, sent = self.make()
        self.assertEqual(a.check([row(2), row(50, "K2", suspicious=True),
                                  row(9, "K3", warnings=["ONE-WAY RULES: Kalshi also resolves YES…"])]), [])

    def test_off_without_a_channel(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            a = alerts.Alerter(lambda m: None)
        self.assertFalse(a.enabled)
        self.assertEqual(a.check([row()]), [])
        with self.assertRaises(ValueError):
            a.test()


if __name__ == "__main__":
    unittest.main()
