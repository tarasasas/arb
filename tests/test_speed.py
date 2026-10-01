import threading
import time
import unittest

from arb import engine, kalshi, polymarket
from arb.http import RateLimitedClient
from arb.model import Contract


def book(yes_bid, no_bid):
    return {"yes_dollars": [[str(yes_bid), "10"]], "no_dollars": [[str(no_bid), "10"]]}


class StaleQuoteTests(unittest.TestCase):
    def km(self, ticker="K-1"):
        return kalshi.KalshiMarket(ticker, "K", "KX", "NFL", "nfl", "football", "", "", "", "GAME", "FG", "A",
                                   ">", 0.0, "t", "", "", "", 0.07)

    def test_kalshi_older_book_does_not_overwrite_newer(self):
        client = kalshi.KalshiClient.__new__(kalshi.KalshiClient)
        client.workers = 1
        client.http = type("H", (), {"get": lambda self, path, params, high=False: {
            "orderbooks": [{"ticker": "K-1", "orderbook_fp": book(0.40, 0.55)}]}})()
        m = self.km()
        m.yes_ask, m.quoted_at = 0.42, time.time() + 60      # a fresher quote already applied
        client.refresh_books([m])
        self.assertEqual(m.yes_ask, 0.42)
        m.quoted_at = 0.0
        client.refresh_books([m])
        self.assertEqual(m.yes_ask, 0.45)                    # buy YES = 1 - best NO bid
        self.assertGreater(m.quoted_at, 0)

    def test_kalshi_tops_from_listing_chunks_and_unprices_missing(self):
        calls = []

        def get(self_, path, params, high=False):
            tickers = params["tickers"].split(",")
            calls.append(len(tickers))
            return {"markets": [{"ticker": t, "status": "active", "yes_ask_dollars": "0.4500",
                                 "no_ask_dollars": "0.5600", "yes_ask_size_fp": "12.00"} for t in tickers if t != "K-7"]}

        client = kalshi.KalshiClient.__new__(kalshi.KalshiClient)
        client.workers = 1
        client.http = type("H", (), {"get": get})()
        ms = [self.km(f"K-{i}") for i in range(450)]
        ms[7].yes_ask = 0.30
        client.refresh_tops(ms)
        self.assertEqual(calls, [200, 200, 50])
        self.assertEqual((ms[0].yes_ask, ms[0].no_ask, ms[0].yes_ask_size), (0.45, 0.56, 12.0))
        self.assertIsNone(ms[7].yes_ask)                     # not returned: closed or delisted

    def test_polymarket_older_quote_does_not_overwrite_newer(self):
        client = polymarket.PolymarketClient.__new__(polymarket.PolymarketClient)
        client.http = type("H", (), {"get": lambda self, path, params, high=False: {"markets": [
            {"slug": "s", "active": True, "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.43"}}]}})()
        pm = polymarket.PMMarket("s", "nfl", "football", "2026-10-04", "a", "b", "GAME", "FG", "a", ">", 0.0,
                                 False, "t", "", "", 0.0695, {})
        pm.yes_ask, pm.quoted_at = 0.50, time.time() + 60
        client.refresh_quotes([pm])
        self.assertEqual(pm.yes_ask, 0.50)
        pm.quoted_at = 0.0
        client.refresh_quotes([pm])
        self.assertEqual((pm.yes_ask, pm.no_ask), (0.43, 0.6))


class PayoutCacheTests(unittest.TestCase):
    def test_screen_caches_payout_and_matches_uncached(self):
        var = ("total", "FG")
        k = Contract("kalshi", "k", "G", var, ">", 5.5, "t", fee_coef=0.07)
        p = Contract("polymarket", "p", "G", var, ">", 5.5, "t", fee_coef=0.0695)
        k.ask, p.ask = {"yes": 0.45, "no": 0.56}, {"yes": 0.47, "no": 0.50}
        groups = engine.group_pairs([k, p])
        first = engine.screen(groups, -0.05)
        self.assertTrue(k.pay_cache)
        second = engine.screen(groups, -0.05)
        self.assertEqual([(c["sk"], c["sp"], c["payout"], c["edge"]) for c in first],
                         [(c["sk"], c["sp"], c["payout"], c["edge"]) for c in second])
        self.assertEqual(first[0]["payout"], 1.0)


class PriorityTests(unittest.TestCase):
    def test_high_priority_goes_before_queued_normal_requests(self):
        client = RateLimitedClient("https://example.invalid", rps=20)
        order = []

        def take(tag, high):
            client._wait_turn(high)
            order.append(tag)

        lows = [threading.Thread(target=take, args=(f"low{i}", False)) for i in range(6)]
        for t in lows:
            t.start()
        time.sleep(0.06)          # a slot or two go to the normal queue first
        hi = threading.Thread(target=take, args=("high", True))
        hi.start()
        for t in lows + [hi]:
            t.join()
        self.assertLess(order.index("high"), 4)


if __name__ == "__main__":
    unittest.main()
