import time
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


class PolymarketStreamVenueTests(unittest.TestCase):
    def venue(self, stream):
        v = venues.PolymarketVenue.__new__(venues.PolymarketVenue)
        v.private, v._seen = stream, {}
        return v

    def test_buying_power_from_the_stream_skips_the_download(self):
        class S:
            connected, buying_power = True, 512.25
        v = self.venue(S())
        v.http = mock.Mock()
        self.assertEqual(v.balance(), 512.25)
        v.http.get.assert_not_called()

    def test_order_confirmation_waits_on_the_stream_not_a_poll(self):
        from arb import streams
        s = streams.PolymarketPrivateStream(lambda m, p: {}, lambda m: None, connect=lambda u, h: None)
        s.connected = True
        v = self.venue(s)
        reads = []

        def get(path, params=None):            # the order fills when the stream says so
            reads.append(path)
            return {"order": {"id": "o-1", "state": "ORDER_STATE_FILLED" if s.version("o-1") else "ORDER_STATE_NEW",
                              "cumQuantity": "5"}}
        v.http = mock.Mock(get=get)
        import threading
        threading.Timer(0.2, lambda: s._handle({"orderSubscriptionUpdate": {"execution": {"order": {"id": "o-1"}}}})).start()
        t = time.monotonic()
        with mock.patch.object(venues.time, "sleep", side_effect=AssertionError("polled")):
            o = v._final_order({"id": "o-1", "executions": [{"order": {"id": "o-1", "state": "ORDER_STATE_NEW"}}]})
        self.assertEqual(o["state"], "ORDER_STATE_FILLED")
        self.assertEqual(len(reads), 1)                   # read once, right when the fill was pushed
        self.assertLess(time.monotonic() - t, 0.6)        # not at the 1s backstop

    def test_without_the_stream_it_polls_as_before(self):
        v = self.venue(None)
        self.assertIsNone(v.wait_order("o-1", 1.0))


class FillPriceTests(unittest.TestCase):
    """Near 50/50 an average fill price fits the limit read either way; the expected price decides."""

    def test_reading(self):
        self.assertAlmostEqual(venues._per_share(0.49, 0.52, True, expect=0.49), 0.49)
        self.assertAlmostEqual(venues._per_share(0.51, 0.52, True, expect=0.49), 0.49)   # quoted on the other side
        self.assertAlmostEqual(venues._per_share(0.49, 0.52, True), 0.51)                # no expectation: conservative
        self.assertAlmostEqual(venues._per_share(0.72, 0.30, True, expect=0.50), 0.28)   # only one reading fits
        self.assertEqual(venues._per_share(0, 0.52, True, expect=0.49), 0.52)            # no average: the limit
        self.assertAlmostEqual(venues._per_share(0.49, 0.47, False, expect=0.51), 0.51)  # selling

    def test_kalshi_no_buy_quoted_on_the_yes_book(self):
        class Http:
            def post(self, path, body):
                return {"fill_count": "10", "average_fill_price": "0.51", "average_fee_paid": "0.0175"}
        f = venue(Http()).buy("T", "no", 10, 0.52, 0.07, expect=0.49)
        self.assertAlmostEqual(f.avg, 0.49)                      # was recorded as 0.51 before
