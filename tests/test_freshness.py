import time
import unittest

from arb import engine, kalshi, polymarket, streams
from arb.model import Contract


def km(ticker="K-1"):
    return kalshi.KalshiMarket(ticker, "K", "KX", "NFL", "nfl", "football", "", "", "", "GAME", "FG", "A",
                               ">", 0.0, "t", "", "", "", 0.07)


def pmm(slug="s"):
    return polymarket.PMMarket(slug, "nfl", "football", "2026-10-04", "a", "b", "GAME", "FG", "a", ">", 0.0,
                               False, "t", "", "", 0.0695, {})


def kalshi_client(get):
    c = kalshi.KalshiClient.__new__(kalshi.KalshiClient)
    c.workers, c.http = 1, type("H", (), {"get": get})()
    return c


def pm_client(get):
    c = polymarket.PolymarketClient.__new__(polymarket.PolymarketClient)
    c.http = type("H", (), {"get": get})()
    return c


BOOK = {"yes_dollars": [["0.40", "10"]], "no_dollars": [["0.55", "10"]]}     # buy YES at 0.45, NO at 0.60


class StaleQuoteTests(unittest.TestCase):
    def test_poll_sent_before_a_stream_update_does_not_overwrite_it(self):
        m = km()

        def get(_self, path, params):
            streams.apply_levels(m, {"yes": [(0.47, 5)], "no": [(0.55, 5)]})   # stream lands mid-request
            return {"orderbooks": [{"ticker": "K-1", "orderbook_fp": BOOK}]}
        kalshi_client(get).refresh_books([m])
        self.assertEqual((m.yes_ask, m.levels["yes"]), (0.47, [(0.47, 5)]))

    def test_poll_sent_after_the_stream_update_applies(self):
        m = km()
        streams.apply_levels(m, {"yes": [(0.47, 5)], "no": [(0.55, 5)]})
        m.quoted_at -= 1
        kalshi_client(lambda _s, p, q: {"orderbooks": [{"ticker": "K-1", "orderbook_fp": BOOK}]}).refresh_books([m])
        self.assertEqual((m.yes_ask, m.no_ask), (0.45, 0.60))

    def test_failed_poll_does_not_blank_a_newer_stream_book(self):
        m = km()

        def get(_self, path, params):
            streams.apply_levels(m, {"yes": [(0.47, 5)], "no": [(0.55, 5)]})
            raise TimeoutError()
        self.assertEqual(kalshi_client(get).refresh_books([m]), {"K-1"})
        self.assertEqual(m.yes_ask, 0.47)

    def test_polymarket_quotes_and_book_respect_newer_streams(self):
        pm = pmm()

        def quotes(_self, path, params):
            streams.apply_levels(pm, {"yes": [(0.50, 5)], "no": [(0.52, 5)]})
            return {"markets": [{"slug": "s", "active": True, "bestBidQuote": {"value": "0.40"},
                                 "bestAskQuote": {"value": "0.43"}}]}
        pm_client(quotes).refresh_quotes([pm])
        self.assertEqual(pm.yes_ask, 0.50)


class ListedTopsTests(unittest.TestCase):
    def test_list_prices_in_chunks_and_missing_or_closed_are_unpriced(self):
        calls = []

        def get(_self, path, params):
            ts = params["tickers"].split(",")
            calls.append(len(ts))
            return {"markets": [{"ticker": t, "status": "closed" if t == "K-8" else "active", "yes_ask_dollars": "0.4500",
                                 "no_ask_dollars": "1.0000", "yes_ask_size_fp": "12.00"} for t in ts if t != "K-7"]}
        ms = [km(f"K-{i}") for i in range(450)]
        ms[7].yes_ask = ms[8].yes_ask = 0.30
        failed = kalshi_client(get).refresh_tops(ms)
        self.assertEqual((calls, failed), ([200, 200, 50], set()))
        self.assertEqual((ms[0].yes_ask, ms[0].no_ask, ms[0].yes_ask_size), (0.45, None, 12.0))   # 1.00 = nobody selling
        self.assertIsNone(ms[7].yes_ask)                       # not in the reply
        self.assertIsNone(ms[8].yes_ask)                       # closed

    def test_depth_that_no_longer_matches_the_listed_price_is_dropped(self):
        m = km()
        m.levels = {"yes": [(0.44, 10)], "no": [(0.58, 10)]}
        kalshi_client(lambda _s, p, q: {"markets": [{"ticker": "K-1", "status": "active", "yes_ask_dollars": "0.4500",
                                                     "no_ask_dollars": "0.5800"}]}).refresh_tops([m])
        self.assertEqual(m.levels, {})


class SuspendedBookTests(unittest.TestCase):
    def test_suspended_polymarket_book_has_no_prices(self):
        pm = pmm()
        c = pm_client(lambda _s, path, params=None: {"marketData": {
            "state": "MARKET_STATE_SUSPENDED", "bids": [{"px": {"value": "0.40"}, "qty": "10"}],
            "offers": [{"px": {"value": "0.43"}, "qty": "10"}]}})
        c.refresh_book(pm)
        self.assertEqual((pm.yes_ask, pm.no_ask, pm.state), (None, None, "MARKET_STATE_SUSPENDED"))

    def test_suspended_in_the_market_list_is_unpriced(self):
        pm = pmm()
        pm_client(lambda _s, p, q: {"markets": [{"slug": "s", "active": True, "status": "MARKET_STATUS_SUSPENDED",
                                                 "bestBidQuote": {"value": "0.40"},
                                                 "bestAskQuote": {"value": "0.43"}}]}).refresh_quotes([pm])
        self.assertIsNone(pm.yes_ask)


class PayoutCacheTests(unittest.TestCase):
    def test_screen_caches_payout_and_gives_the_same_answer(self):
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

    def test_contract_saved_by_older_code_still_screens(self):
        import pickle
        var = ("total", "FG")
        k = Contract("kalshi", "k", "G", var, ">", 5.5, "t", fee_coef=0.07)
        p = Contract("polymarket", "p", "G", var, ">", 5.5, "t", fee_coef=0.0695)
        del k.__dict__["pay_cache"], p.__dict__["pay_cache"]       # as unpickled from before the field existed
        k, p = pickle.loads(pickle.dumps((k, p)))
        k.ask, p.ask = {"yes": 0.45, "no": 0.56}, {"yes": 0.47, "no": 0.50}
        self.assertEqual(engine.screen(engine.group_pairs([k, p]), -0.05)[0]["payout"], 1.0)

    def test_market_saved_by_older_code_takes_fresh_quotes(self):
        import pickle
        m = km()
        del m.__dict__["quoted_at"]
        m = pickle.loads(pickle.dumps(m))
        kalshi_client(lambda _s, p, q: {"orderbooks": [{"ticker": "K-1", "orderbook_fp": BOOK}]}).refresh_books([m])
        self.assertEqual(m.yes_ask, 0.45)


if __name__ == "__main__":
    unittest.main()
