import unittest
from unittest import mock

from arb import venues
from arb.http import ApiError
from arb.venues import KalshiVenue


class FakeHttp:
    """Kalshi client whose order answers get lost on the way back."""

    def __init__(self, post_error, orders=lambda body: []):
        self.post_error, self.orders, self.posted, self.lookups = post_error, orders, [], []

    def post(self, path, body):
        self.posted.append(body)
        raise self.post_error

    def get(self, path, params=None):
        self.lookups.append((path, params))
        return {"orders": self.orders(self.posted[-1])}


def venue(http):
    return KalshiVenue(type("Client", (), {"http": http})())


@mock.patch.object(venues.time, "sleep", lambda _s: None)
class KalshiRecoveryTests(unittest.TestCase):
    def test_lost_answer_is_recovered_by_client_order_id(self):
        def orders(body):
            return [{"client_order_id": "someone-else", "status": "executed", "fill_count_fp": "99.00"},
                    {"client_order_id": body["client_order_id"], "order_id": "o1", "status": "canceled",
                     "fill_count_fp": "7.00", "taker_fill_cost_dollars": "2.8000", "taker_fees_dollars": "0.1200"}]
        http = FakeHttp(TimeoutError("read timed out"), orders)
        f = venue(http).buy("KX-1", "yes", 10, 0.40, 0.07)
        self.assertEqual((f.qty, round(f.amount, 4), round(f.fee, 4), f.order_id), (7.0, 2.8, 0.12, "o1"))
        self.assertEqual((http.lookups[0][0], http.lookups[0][1]["ticker"]), ("/portfolio/orders", "KX-1"))

    def test_order_that_cant_be_found_stays_unknown(self):
        http = FakeHttp(ConnectionResetError("reset"))
        with self.assertRaises(ConnectionResetError):
            venue(http).buy("KX-1", "yes", 10, 0.40, 0.07)
        self.assertEqual(len(http.lookups), 3)

    def test_resting_order_is_not_treated_as_final(self):
        http = FakeHttp(TimeoutError(), lambda body: [{"client_order_id": body["client_order_id"],
                                                       "status": "resting", "fill_count_fp": "0"}])
        with self.assertRaises(TimeoutError):
            venue(http).buy("KX-1", "yes", 10, 0.40, 0.07)

    def test_rejection_is_not_looked_up(self):
        http = FakeHttp(ApiError(400, "insufficient balance"))
        with self.assertRaises(ApiError):
            venue(http).buy("KX-1", "yes", 10, 0.40, 0.07)
        self.assertEqual(http.lookups, [])


if __name__ == "__main__":
    unittest.main()
