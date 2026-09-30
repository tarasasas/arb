import unittest

from arb import engine, nonsports
from arb.model import NO, YES, Contract

from tests.test_nonsports import k_market, pm_market


def c(var, op, line, exchange="kalshi", **kw):
    return Contract(exchange, f"{exchange}-{op}{line}", "G", var, op, line, "t", **kw)


def cand(k, sk, p, sp, edge=0.02):
    return {"k": k, "sk": sk, "p": p, "sp": sp, "edge": edge}


MARGIN = ("margin", "FG", "A")
TOTAL = ("total", "FG")


class SimpleTradeTests(unittest.TestCase):
    def test_same_line_yes_no_is_simple(self):
        k, p = c(TOTAL, ">", 8.5), c(TOTAL, "<", 8.5, "polymarket")
        self.assertEqual(engine.not_simple_reasons(cand(k, YES, p, YES)), [])
        self.assertTrue(engine.not_simple_reasons(cand(k, NO, p, YES)))      # both pay on the under

    def test_moneyline_opposite_teams_is_simple(self):
        k = c(MARGIN, ">", 0.5, no_draw=True, winner=True)
        p = c(MARGIN, "<", -0.5, "polymarket", no_draw=True, winner=True)
        self.assertEqual(engine.not_simple_reasons(cand(k, YES, p, YES)), [])

    def test_cross_line_is_not_simple(self):
        k, p = c(MARGIN, ">", 3.5), c(MARGIN, "<", 6.5, "polymarket")
        self.assertIn("different lines (some results pay $2)", engine.not_simple_reasons(cand(k, YES, p, YES)))

    def test_whole_number_line_short_and_too_good(self):
        k, p = c(TOTAL, ">", 8), c(TOTAL, ">", 8, "polymarket")
        why = engine.not_simple_reasons(cand(k, YES, p, NO, edge=0.2))
        self.assertEqual(why, ["whole-number line (push possible)", "shorts on Polymarket (locks $1)",
                               "too good to be true"])

    def test_settlement_source_mismatch(self):
        self.assertIn("CF Benchmarks", engine.source_mismatch("Settles on the CF Benchmarks BRTI at 5pm ET.",
                                                              "Resolves using the Binance BTC/USDT 1m candle."))
        self.assertIsNone(engine.source_mismatch("CF Benchmarks RTI", "CF Benchmarks Bitcoin Real Time Index"))
        self.assertIsNone(engine.source_mismatch("Federal Reserve target rate", "FOMC statement"))


class RuleTrapTests(unittest.TestCase):
    def test_announcement_counts_only_on_kalshi(self):
        k = Contract("kalshi", "K", "E", ("event", "x"), ">", 0.5, "t",
                     "If Zelenskyy has either officially announced their intention to leave or has left...")
        p = Contract("polymarket", "P", "E", ("event", "x"), ">", 0.5, "t",
                     "Must formally cease to hold the position. Announcements of a future departure do not qualify.")
        self.assertTrue(engine.not_simple_reasons(cand(k, NO, p, YES)))
        self.assertTrue(engine.trade_warnings(cand(k, NO, p, YES)))
        self.assertEqual(engine.trade_warnings(cand(k, YES, p, NO)), [])      # Kalshi YES side stays safe

    def test_inclusive_vs_strict_threshold(self):
        self.assertFalse(nonsports.outcomes_compatible("90+", "Above 90"))
        self.assertTrue(nonsports.outcomes_compatible("Democratic Party 16%+", "Democrats, ≥16%"))
        self.assertTrue(nonsports.outcomes_compatible("Above $5.20", "Above $5.20"))

    def test_more_sources(self):
        self.assertIsNotNone(engine.source_mismatch("Forbes Real-Time Billionaires", "Bloomberg Billionaires Index"))
        self.assertIsNotNone(engine.source_mismatch("Luminate year-end report", "Spotify Wrapped"))
        self.assertIsNotNone(engine.source_mismatch("LiveBench.ai Coding Average", "Arena AI Text Arena leaderboard"))


class MatcherTests(unittest.TestCase):
    def test_wrong_pairs_seen_live(self):
        bad = [("Republican Party 6%+", "Democrats, ≥6%"), ("Democrat 3%+", "Lamb, 5+ pts"),
               ("Flavio Bolsonaro", "Jair Bolsonaro"), ("By December 31, 2027", "Before March, 2027"),
               ("By December 31, 2026", "Before December"), ("Above 0.4%", "Above -0.4%"),
               ("D House, R Senate", "R-House, D-Senate"), ("Billie Jean - Michael Jackson", "Billie Eilish")]
        for a, b in bad:
            self.assertFalse(nonsports.outcomes_compatible(a, b), (a, b))
        good = [("By December 31, 2026", "Before 2027"), ("Mark Takano (D)", "Mark Takano"),
                ("Volodymyr Zelensky", "Volodymyr Zelenskyy"), ("20-25%", "20-25%"),
                ("Sudans Emergency Response Rooms (ERRs)", "Sudan’s Emergency Response Rooms")]
        for a, b in good:
            self.assertTrue(nonsports.outcomes_compatible(a, b), (a, b))

    def test_at_large_and_cities(self):
        self.assertIn("alaska", nonsports.tokens("AK-AL House Election Winner"))
        self.assertNotIn("maine", nonsports.tokens("MEAL DEAL"))
        pm = [pm_market("p", "Highest temperature in NYC on October 1?", "72 to 73", 0.3, 0.32,
                        end="2026-10-02T00:00:00Z", category="climate")]
        ev = [{"event_ticker": "KXHIGHCHI-26OCT01", "title": "Highest temperature in Chicago on Oct 1, 2026",
               "sub_title": "", "category": "Climate and Weather",
               "markets": [k_market("KXHIGHCHI-26OCT01-B72.5", "KXHIGHCHI-26OCT01", "72° to 73°", 0.3, 0.32)]}]
        self.assertEqual(nonsports.suggest(pm, ev, set(), set()), [])

    def test_settlement_early_next_year_is_same_year(self):
        self.assertIn(2026, nonsports.years("BTC price on Jan 1, 2027?", "KXBTCY-27JAN0100"))
        self.assertIn(2026, nonsports.years("", "KXARREST-27JAN"))
        self.assertEqual(nonsports.years("", "KXNOBELPEACE-27"), {2027})


class TimeAndYearTests(unittest.TestCase):
    def test_clock_times(self):
        self.assertEqual(nonsports.clock_times("Bitcoin above $120k on Oct 1 at 5pm ET?"), {17 * 60})
        self.assertEqual(nonsports.clock_times("at 12:00 PM EDT"), {12 * 60})
        self.assertEqual(nonsports.clock_times("at 21:00 UTC"), {17 * 60, 16 * 60})
        self.assertTrue(nonsports.times_compatible("5pm ET", "21:00 UTC"))
        self.assertFalse(nonsports.times_compatible("Bitcoin at 5pm ET", "Bitcoin at 12pm ET"))
        self.assertTrue(nonsports.times_compatible("Bitcoin on Oct 1", "Bitcoin at 5pm ET"))

    def test_before_year_is_previous_year(self):
        self.assertIn(2026, nonsports.years("Will it happen before 2027?"))

    def test_approved_pair_from_another_year_is_not_scanned(self):
        km = nonsports.kalshi_market_obj(k_market("KXNOBELPEACE-27-PLEO", "KXNOBELPEACE-27", "Pope Leo XIV",
                                                  0.03, 0.05), 0.07)
        pm = nonsports.pm_market_obj(pm_market("p1", "2026 Nobel Peace Prize Winner", "Pope Leo XIV", 0.03, 0.04),
                                     0.0695)
        conflicts = []
        cs, _ = nonsports.approved_contracts([{"pm": "p1", "kalshi": km.ticker, "relation": "same"}],
                                             {km.ticker: km}, {"p1": pm}, conflicts)
        self.assertEqual(cs, [])
        self.assertIn("different years", conflicts[0]["why"])

    def test_hourly_crypto_suggestions_match_on_time(self):
        pm = [pm_market("p5", "Bitcoin above ___ on October 1, 5PM ET?", "$120,000", 0.4, 0.42,
                        end="2026-10-01T21:00:00Z", category="crypto")]
        ev = [{"event_ticker": "KXBTCD-26OCT0112", "title": "Bitcoin price on Oct 1, 2026 at 12pm EDT?",
               "sub_title": "", "category": "Crypto",
               "markets": [k_market("KXBTCD-26OCT0112-T120000", "KXBTCD-26OCT0112", "$120,000 or above", 0.4, 0.42)]},
              {"event_ticker": "KXBTCD-26OCT0117", "title": "Bitcoin price on Oct 1, 2026 at 5pm EDT?",
               "sub_title": "", "category": "Crypto",
               "markets": [k_market("KXBTCD-26OCT0117-T120000", "KXBTCD-26OCT0117", "$120,000 or above", 0.4, 0.42)]}]
        groups = nonsports.suggest(pm, ev, set(), set())
        self.assertEqual([g["kalshi"]["key"] for g in groups], ["KXBTCD-26OCT0117"])

    def test_tab_coverage(self):
        pm = [pm_market("a", "Q", "x", 0.1, 0.2, category="crypto"), pm_market("b", "Q", "y", 0.1, 0.2, category="crypto")]
        ev = [{"event_ticker": "E", "category": "Crypto", "markets": [{}, {}, {}]}]
        g = [{"pm": {"category": "crypto"}, "kalshi": {"category": "Crypto"}, "pairs": [{}]}]
        rows = {(r["exchange"], r["category"]): (r["markets"], r["paired"]) for r in nonsports.tab_coverage(pm, ev, g)}
        self.assertEqual(rows, {("Polymarket", "crypto"): (2, 1), ("Kalshi", "Crypto"): (3, 1)})


if __name__ == "__main__":
    unittest.main()
