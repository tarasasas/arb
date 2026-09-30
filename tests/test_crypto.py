import unittest
from datetime import datetime, timezone

from arb import crypto, engine, nonsports
from arb.model import NO, YES, guaranteed_payout


def pm(slug, start, end, beat, horizon="15m", coin="btc", bid=0.40, ask=0.41):
    return {"slug": slug, "question": f"BTC Up or Down: {horizon}", "title": f"BTC Up or Down: {horizon}", "active": True,
            "closed": False, "category": "crypto", "endDate": end, "feeCoefficient": 0.0695,
            "description": "Settles Up if the price at the close is greater than or equal to the open. CF Benchmarks BRTI.",
            "bestBidQuote": {"value": str(bid)}, "bestAskQuote": {"value": str(ask)},
            "assetPriceTerms": {"marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN", "asset": {"symbol": coin},
                                "indexSymbol": "BRTI", "horizon": horizon, "windowStart": start, "windowEnd": end,
                                "priceToBeat": {"value": str(beat)} if beat else None}}


def km(ticker, end, strike, series="KXBTC15M", kind="greater_or_equal", yes_ask=0.44, no_ask=0.57):
    return {"ticker": ticker, "event_ticker": f"{series}-26SEP302000", "status": "active", "open_time": S,
            "close_time": end, "floor_strike": strike, "strike_type": kind,
            "title": "BTC price", "yes_sub_title": f"{strike}", "yes_ask_dollars": str(yes_ask),
            "no_ask_dollars": str(no_ask), "yes_bid_dollars": str(round(1 - no_ask, 2)),
            "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' BRTI before 8 PM EDT is above "
                             "the strike, then the market resolves to Yes."}


S, E, H = "2026-09-30T23:45:00Z", "2026-10-01T00:00:00Z", "2026-09-30T23:00:00Z"
NOW = datetime(2026, 9, 30, 23, 50, tzinfo=timezone.utc)
FEE = lambda series: 0.07


def build(pms, kms, now=NOW):
    return crypto.price_contracts(pms, kms, FEE, 0.0695, now)


def by_id(cs):
    return {c.market_id: c for c in cs}


class PriceContractTests(unittest.TestCase):
    def test_exact_twin_is_an_exact_hedge(self):
        cs, src = build([pm("p15", S, E, 83728.93)], [km("K15", E, 83728.93)])
        c = by_id(cs)
        self.assertEqual((c["p15"].line, c["K15"].line), (8372893 - 0.5, 8372893 - 0.5))   # x >= open, in cents
        self.assertTrue(engine.exact_hedge(c["K15"], NO, c["p15"], YES))
        self.assertEqual(engine.row_tab(c["K15"]), "Crypto")
        self.assertEqual(set(src), {("kalshi", "K15"), ("polymarket", "p15")})

    def test_cross_strike_with_the_hourly_ladder(self):
        cs, _ = build([pm("p1h", H, E, 83642.70, horizon="1h")],
                      [km("KD-83700", E, 83699.99, series="KXBTCD", kind="greater"),
                       km("KD-83600", E, 83599.99, series="KXBTCD", kind="greater")])
        c = by_id(cs)
        self.assertEqual(c["KD-83700"].line, 8369999 + 0.5)                 # "above 83,699.99" = x >= 83,700.00
        # Up from 83,642.70 + NOT above 83,699.99: $1 either way, $2 if it closes in between.
        self.assertEqual(guaranteed_payout([(c["KD-83700"], NO), (c["p1h"], YES)]), 1.0)
        self.assertFalse(engine.exact_hedge(c["KD-83700"], NO, c["p1h"], YES))
        # The other direction (Up + NOT above 83,599.99) can lose both: not an arb.
        self.assertEqual(guaranteed_payout([(c["KD-83600"], NO), (c["p1h"], YES)]), 0.0)
        rows = engine.outcome_table(c["KD-83700"], NO, c["p1h"], YES)
        self.assertEqual([r["total"] for r in rows], [1.0, 2.0, 1.0])
        self.assertEqual(rows[1]["outcome"], "BTC closes $83,642.70 to $83,699.99")

    def test_only_same_coin_same_instant_open_markets_group(self):
        cs, _ = build([pm("p", S, E, 83728.93), pm("eth", S, E, 3000.0, coin="eth"),
                       pm("started-not", S, E, None), pm("done", "2026-09-30T23:30:00Z", "2026-09-30T23:45:00Z", 1.0)],
                      [km("K-other-time", "2026-10-01T01:00:00Z", 83728.93),
                       km("K-range", E, 83700, series="KXBTC", kind="between"),
                       km("K", E, 83728.93)])
        self.assertEqual(set(by_id(cs)), {"p", "K"})

    def test_rows_from_the_engine(self):
        cs, _ = build([pm("p1h", H, E, 83642.70, horizon="1h", bid=0.40, ask=0.41)],
                      [km("KD", E, 83699.99, series="KXBTCD", kind="greater", yes_ask=0.30, no_ask=0.55)])
        for c in cs:
            c.ask = {YES: 0.41 if c.exchange == "polymarket" else 0.30, NO: 0.60 if c.exchange == "polymarket" else 0.55}
        cands = engine.screen(engine.group_pairs(cs), -1)
        best = max(cands, key=lambda x: x["edge"])
        self.assertEqual((best["sk"], best["sp"]), (NO, YES))              # 55c + 41c = 96c for >= $1
        row = engine.to_row(best, None, NOW)
        self.assertEqual(row["tab"], "Crypto")
        self.assertTrue(any("CF Benchmarks" in w for w in row["warnings"]))

    def test_closed_window_is_dropped_and_ignored(self):
        self.assertEqual(build([pm("p", S, E, 1.0)], [km("K", E, 1.0)], now=datetime(2026, 10, 1, 0, 1, tzinfo=timezone.utc))[0], [])
        cs, _ = build([pm("p", S, E, 83728.93)], [km("K", E, 83728.93)])
        for c in cs:
            c.ask = {YES: 0.30, NO: 0.24}                                   # stale quotes once it's over
        later = [c for c in cs]
        for c in later:
            c.trade_until = "2026-09-30T00:00:00Z"
        self.assertEqual(engine.screen(engine.group_pairs(later), 0), [])

    def test_series_to_fetch(self):
        self.assertEqual(crypto.kalshi_series_for([pm("p", S, E, 1.0), pm("q", S, E, 1.0, coin="eth")]),
                         ["KXBTC15M", "KXBTCD", "KXETH15M", "KXETHD"])

    def test_wording_matcher_leaves_up_down_markets_alone(self):
        ev = [{"event_ticker": "KXBTC15M-26SEP301530", "series_ticker": "KXBTC15M", "title": "BTC price up in next 15 mins?",
               "sub_title": "", "category": "Crypto", "markets": [km("K1", E, 83728.93)]}]
        self.assertEqual(nonsports.suggest([pm("p15", S, E, 83728.93)], ev, set(), set()), [])


if __name__ == "__main__":
    unittest.main()
