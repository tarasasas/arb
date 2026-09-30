import unittest
from datetime import datetime, timedelta, timezone

from arb import kalshi
from arb.model import kalshi_fee, polymarket_fee

NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)


class EventFeeOverrideTests(unittest.TestCase):
    def test_no_override_keeps_series_rate(self):
        self.assertEqual(kalshi.effective_multiplier(0.5, [], NOW, 360), 0.5)

    def test_override_in_effect_replaces_series_rate(self):
        self.assertEqual(kalshi.effective_multiplier(0.5, [(NOW - H, 1)], NOW, 360), 1.0)
        self.assertEqual(kalshi.effective_multiplier(1.0, [(NOW - H, 0)], NOW, 360), 0.0)    # fee holiday

    def test_cleared_override_goes_back_to_series_rate(self):
        self.assertEqual(kalshi.effective_multiplier(0.5, [(NOW - 2 * H, 1), (NOW - H, None)], NOW, 360), 0.5)

    def test_higher_override_starting_before_next_reload_counts(self):
        # A playoff game going from 0.5x to 1x in 3 minutes: charge 1x now, fees are re-read every ~6 min.
        self.assertEqual(kalshi.effective_multiplier(0.5, [(NOW + timedelta(minutes=3), 1)], NOW, 360), 1.0)
        self.assertEqual(kalshi.effective_multiplier(0.5, [(NOW + 6 * H, 1)], NOW, 360), 0.5)   # far off

    def test_apply_to_markets(self):
        class M:
            def __init__(self, ev, coef):
                self.event_ticker, self.fee_coef = ev, coef
        a, b = M("KXMLBGAME-X", 0.035), M("KXMLBGAME-Y", 0.035)
        kalshi.apply_fee_overrides([a, b], {"KXMLBGAME-X": [(NOW - H, 1)]}, NOW, 360)
        self.assertAlmostEqual(a.fee_coef, 0.07)
        self.assertAlmostEqual(b.fee_coef, 0.035)


class KalshiBookTests(unittest.TestCase):
    def test_market_missing_from_reply_is_unpriced(self):
        c = kalshi.KalshiClient.__new__(kalshi.KalshiClient)
        c.workers = 1

        class H:
            def get(self, path, params):
                return {"orderbooks": [{"ticker": "A", "orderbook_fp": {"yes_dollars": [["0.40", "10"]],
                                                                         "no_dollars": [["0.55", "5"]]}}]}
        c.http = H()

        class M:
            def __init__(self, t):
                self.ticker, self.levels, self.yes_ask, self.no_ask = t, {"yes": [(0.3, 9)]}, 0.3, 0.6
        a, b = M("A"), M("B")
        c.refresh_books([a, b])
        self.assertEqual((a.yes_ask, a.no_ask), (0.45, 0.6))      # YES = 1 - NO bid, NO = 1 - YES bid
        self.assertEqual((b.levels, b.yes_ask, b.no_ask), ({}, None, None))


class PublishedFeeExamples(unittest.TestCase):
    """Worked examples from the exchanges' own fee pages."""

    def test_polymarket_fee_schedule(self):
        # docs.polymarket.us/fees: 0.0695 x C x p x (1-p), banker's rounding.
        self.assertEqual(polymarket_fee([(0.10, 1000)], 0.0695), 6.26)
        self.assertEqual(polymarket_fee([(0.65, 1000)], 0.0695), 15.81)
        self.assertEqual(polymarket_fee([(0.50, 1000)], 0.0695), 17.38)
        self.assertEqual(polymarket_fee([(0.50, 100)], 0.0695), 1.74)
        self.assertEqual(polymarket_fee([(0.01, 100)], 0.0695), 0.07)

    def test_kalshi_rounds_the_order_total_up(self):
        # 0.07 x 100 x 0.5 x 0.5 = 1.75 exactly; 0.07 x 3 x 0.37 x 0.63 = 0.048951 -> 0.05.
        self.assertEqual(kalshi_fee([(0.50, 100)], 0.07), 1.75)
        self.assertEqual(kalshi_fee([(0.37, 3)], 0.07), 0.05)
        self.assertEqual(kalshi_fee([(0.37, 1), (0.37, 2)], 0.07), 0.05)   # one order, not rounded per fill

    def test_kalshi_sub_cent_price_rounds_cost_plus_fee(self):
        # 10 x 12.3c = $1.23 exactly; 3 x 12.3c = $0.369 -> total rounded up with the fee.
        fee = kalshi_fee([(0.123, 3)], 0.07)                    # exact fee 0.0226...
        self.assertAlmostEqual(0.369 + fee, 0.40, 9)            # balance moves by a whole number of cents
        self.assertAlmostEqual(kalshi_fee([(0.123, 3)], 0.0), 0.001, 9)   # even a zero-fee series rounds


if __name__ == "__main__":
    unittest.main()
