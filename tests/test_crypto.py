import unittest
from datetime import datetime, timezone

from arb import crypto, engine, nonsports
from arb.model import NO, YES


def pm(slug, start, end, beat, horizon="15m", coin="btc"):
    return {"slug": slug, "question": "BTC Up or Down: 15 min", "title": "BTC Up or Down: 15 min", "active": True,
            "closed": False, "category": "crypto", "endDate": end, "feeCoefficient": 0.0695,
            "description": "Settles Up if the price at the close is greater than or equal to the open. CF Benchmarks BRTI.",
            "bestBidQuote": {"value": "0.40"}, "bestAskQuote": {"value": "0.41"},
            "assetPriceTerms": {"marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN", "asset": {"symbol": coin},
                                "indexSymbol": "BRTI", "horizon": horizon, "windowStart": start, "windowEnd": end,
                                "priceToBeat": {"value": str(beat)} if beat else None}}


def km(ticker, start, end, strike, series="KXBTC15M"):
    return {"ticker": ticker, "event_ticker": f"{series}-26SEP301530", "status": "active", "open_time": start,
            "close_time": end, "floor_strike": strike, "strike_type": "greater_or_equal",
            "title": "BTC price up in next 15 mins?", "yes_sub_title": f"Target Price: ${strike:,.2f}",
            "yes_ask_dollars": "0.44", "no_ask_dollars": "0.57", "yes_bid_dollars": "0.43",
            "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 3:30 PM EDT is "
                             "at least the average before 3:15 PM EDT, then the market resolves to Yes."}


S, E = "2026-09-30T19:15:00Z", "2026-09-30T19:30:00Z"
NOW = datetime(2026, 9, 30, 19, 20, tzinfo=timezone.utc)      # inside the window


class CryptoPairTests(unittest.TestCase):
    def test_same_window_and_price_to_beat_pairs(self):
        rows = crypto.pairs([pm("p15", S, E, 83728.93)], [km("K1", S, E, 83728.93)], now=NOW)
        self.assertEqual([(r["pm"], r["kalshi"], r["relation"]) for r in rows], [("p15", "K1", "same")])
        self.assertTrue(rows[0]["structural"])
        self.assertIn("19:15–19:30 UTC", rows[0]["label"])

    def test_mismatches_never_pair(self):
        self.assertEqual(crypto.pairs([pm("p", S, E, 83728.93)], [km("K", S, E, 83935.01)], now=NOW), [])      # other open
        self.assertEqual(crypto.pairs([pm("p", S, E, 83728.93)], [km("K", E, "2026-09-30T19:45:00Z", 83728.93)], now=NOW), [])
        self.assertEqual(crypto.pairs([pm("p", S, "2026-09-30T20:15:00Z", 83728.93, horizon="1h")],
                                      [km("K", S, E, 83728.93)], now=NOW), [])                                  # 60-min window
        self.assertEqual(crypto.pairs([pm("p", S, E, None)], [km("K", S, E, 83728.93)], now=NOW), [])        # not started
        self.assertEqual(crypto.pairs([pm("p", S, E, 3000.0, coin="eth")], [km("K", S, E, 3000.0)], now=NOW), [])  # coin

    def test_closed_window_is_dropped(self):
        after = datetime(2026, 9, 30, 19, 31, tzinfo=timezone.utc)
        self.assertEqual(crypto.pairs([pm("p", S, E, 83728.93)], [km("K", S, E, 83728.93)], now=after), [])

    def test_screen_ignores_contracts_past_trade_until(self):
        rows = crypto.pairs([pm("p15", S, E, 83728.93)], [km("K1", S, E, 83728.93)], now=NOW)
        k_obj = nonsports.kalshi_market_obj(km("K1", S, E, 83728.93), 0.07)
        p_obj = nonsports.pm_market_obj(pm("p15", S, E, 83728.93), 0.0695)
        k, p = nonsports.approved_contracts(rows, {"K1": k_obj}, {"p15": p_obj})[0]
        k.ask, p.ask = {YES: 0.30, NO: 0.24}, {YES: 0.44, NO: 0.70}          # stale quotes after the close
        self.assertEqual(engine.screen(engine.group_pairs([k, p]), 0), [])     # E is in the past

    def test_wording_matcher_leaves_up_down_markets_alone(self):
        ev = [{"event_ticker": "KXBTC15M-26SEP301530", "series_ticker": "KXBTC15M", "title": "BTC price up in next 15 mins?",
               "sub_title": "", "category": "Crypto", "markets": [km("K1", S, E, 83728.93)]}]
        self.assertEqual(nonsports.suggest([pm("p15", S, E, 83728.93)], ev, set(), set()), [])

    def test_series_to_fetch(self):
        self.assertEqual(crypto.kalshi_series_for([pm("p", S, E, 1.0), pm("q", S, E, 1.0, coin="eth")]),
                         ["KXBTC15M", "KXETH15M"])

    def test_pair_becomes_verified_simple_contract(self):
        rows = crypto.pairs([pm("p15", S, E, 83728.93)], [km("K1", S, E, 83728.93)], now=NOW)
        k_obj = nonsports.kalshi_market_obj(km("K1", S, E, 83728.93), 0.07)
        p_obj = nonsports.pm_market_obj(pm("p15", S, E, 83728.93), 0.0695)
        k, p = nonsports.approved_contracts(rows, {"K1": k_obj}, {"p15": p_obj})[0]
        self.assertEqual(k.note, "structural")
        self.assertEqual(engine.not_simple_reasons({"k": k, "sk": NO, "p": p, "sp": YES, "edge": 0.01}), [])
        self.assertTrue(any("contract terms" in w for w in engine.warnings_for(k, p, engine.now_utc())))


if __name__ == "__main__":
    unittest.main()
