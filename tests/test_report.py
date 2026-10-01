import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from arb import report


class ReportTests(unittest.TestCase):
    def test_app_trades_and_kalshi_record(self):
        log = {"started": "2026-10-01T09:01:00", "status": "partial",
               "plan": {"size": 20, "capital": 19.0, "expected_profit": 0.4, "first": "kalshi",
                        "legs": {"kalshi": {"market_id": "KXNPBGAME-26OCT010900HANYOM-HAN", "side": "yes", "limit": 0.52,
                                            "title": "Hanshin", "balance": 0.0},
                                 "polymarket": {"market_id": "aec-npb-hta-ygo-2026-10-01", "side": "no", "limit": 0.46,
                                                "title": "Hanshin vs Yomiuri", "balance": 50}}},
               "orders": [{"kind": "first", "exchange": "kalshi", "side": "yes", "error": "insufficient shard balance"}]}
        other = {**log, "plan": {**log["plan"], "legs": {"kalshi": {"market_id": "KXNBA-X", "title": "NBA"}}}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "trades.jsonl"
            p.write_text(json.dumps(log) + "\n" + json.dumps(other) + "\nnot json\n", encoding="utf-8")
            trades = report.app_trades("npb", p)
        self.assertEqual(len(trades), 1)
        self.assertIn("ERROR insufficient shard balance", report.describe_app_trade(trades[0]))

        class HTTP:
            def get(self, path, params=None):
                if path == "/portfolio/fills":
                    return {"fills": [{"ticker": "KXNPBGAME-A", "side": "yes", "action": "buy", "count_fp": "10",
                                       "yes_price_dollars": "0.55", "fee_cost": "0.18", "created_time": "2026-10-01T09:00:00Z"},
                                      {"ticker": "KXNBA-B", "side": "yes", "action": "buy", "count_fp": "1"}]}
                return {"settlements": [{"ticker": "KXNPBGAME-A", "market_result": "no", "revenue": 0,
                                         "yes_count_fp": "10", "no_count_fp": "0", "settled_time": "2026-10-01T13:00:00Z"}]}
        rec = report.kalshi_record(HTTP(), "NPB")
        self.assertEqual(list(rec), ["KXNPBGAME-A"])
        text = report.describe_kalshi("KXNPBGAME-A", rec["KXNPBGAME-A"])
        self.assertIn("result no, paid out $0.00", text)
        self.assertIn("net on Kalshi: $-5.68", text)
