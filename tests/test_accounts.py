import tempfile
import unittest
from pathlib import Path

from arb import accounts, myarbs
from arb.model import Contract


class FakeHTTP:
    """Replies shaped like the exchanges' documented responses, one page each."""
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == "/portfolio/fills":
            return {"fills": []}              # no fill history: the position keeps its lifetime fees
        return self.pages.pop(0)


KALSHI = {"market_positions": [
    {"ticker": "KXGAS-5.20", "position_fp": "100.00", "market_exposure_dollars": "19.00", "fees_paid_dollars": "1.08"},
    {"ticker": "KXNOBEL-X", "position_fp": "-40.00", "market_exposure_dollars": "38.00", "fees_paid_dollars": "0.10"},
    {"ticker": "KXFLAT", "position_fp": "0.00", "market_exposure_dollars": "0", "fees_paid_dollars": "0.50"}],
    "cursor": ""}

PM = {"positions": {
    "gas-5pt20": {"netPositionDecimal": "-100", "cost": {"value": "-25.00", "currency": "USD"},
                  "marketMetadata": {"slug": "gas-5pt20", "title": "Gas above $5.20"}},
    "btc-range": {"netPositionDecimal": "10", "cost": {"value": "8.40", "currency": "USD"},
                  "marketMetadata": {"slug": "btc-range", "title": "BTC 80-85k"}},
    "old": {"netPositionDecimal": "5", "cost": {"value": "1"}, "expired": True}},
    "eof": True}


def contract(ex, mid, key="MACRO:gas"):
    return Contract(ex, mid, key, ("event", key), ">", 0.5, f"{ex} {mid}", game_label="Gas above $5.20")


class ParseTests(unittest.TestCase):
    def setUp(self):
        accounts._fee_cache.clear()

    def test_kalshi_positions(self):
        k = accounts.kalshi_positions(FakeHTTP([KALSHI]))
        self.assertEqual(k["KXGAS-5.20"], {"side": "yes", "shares": 100, "paid": 20.08, "fees": 1.08,
                                             "title": "KXGAS-5.20"})
        self.assertEqual((k["KXNOBEL-X"]["side"], k["KXNOBEL-X"]["shares"]), ("no", 40))
        self.assertNotIn("KXFLAT", k)                                    # closed out: nothing held

    def test_polymarket_positions(self):
        p = accounts.polymarket_positions(FakeHTTP([PM]))
        self.assertEqual((p["gas-5pt20"]["side"], p["gas-5pt20"]["shares"], p["gas-5pt20"]["paid"]), ("no", 100, 75.0))
        self.assertFalse(p["gas-5pt20"]["paid_estimated"])              # short proceeds converted to 1 - price
        self.assertEqual((p["btc-range"]["side"], p["btc-range"]["paid"]), ("yes", 8.4))
        self.assertNotIn("old", p)                                       # expired

    def test_pagination(self):
        h = FakeHTTP([{"market_positions": [KALSHI["market_positions"][0]], "cursor": "c2"},
                      {"market_positions": [KALSHI["market_positions"][1]], "cursor": ""}])
        self.assertEqual(set(accounts.kalshi_positions(h)), {"KXGAS-5.20", "KXNOBEL-X"})
        pages = [params for path, params in h.calls if path == "/portfolio/positions"]
        self.assertEqual(pages[1]["cursor"], "c2")


class PairTests(unittest.TestCase):
    def setUp(self):
        self.contracts = {("kalshi", "KXGAS-5.20"): contract("kalshi", "KXGAS-5.20"),
                          ("polymarket", "gas-5pt20"): contract("polymarket", "gas-5pt20")}
        self.lookup = lambda ex, mid: self.contracts.get((ex, mid))
        self.k = accounts.kalshi_positions(FakeHTTP([KALSHI]))
        self.p = accounts.polymarket_positions(FakeHTTP([PM]))

    def test_pairs_complementary_positions_and_lists_the_rest(self):
        pairs, unpaired = accounts.pair_positions(self.k, self.p, self.lookup)
        self.assertEqual([(t, s, pay) for t, s, _, _, pay in pairs], [("KXGAS-5.20", "gas-5pt20", 1.0)])
        self.assertEqual({(u["exchange"], u["market_id"]) for u in unpaired},
                         {("kalshi", "KXNOBEL-X"), ("polymarket", "btc-range")})

    def test_same_side_on_both_sites_is_not_an_arb(self):
        self.p["gas-5pt20"]["side"] = "yes"                              # YES on both: not hedged
        pairs, unpaired = accounts.pair_positions(self.k, self.p, self.lookup)
        self.assertEqual(pairs, [])
        self.assertEqual(len(unpaired), 4)

    def test_sync_into_my_arbs_respects_your_edits(self):
        with tempfile.TemporaryDirectory() as d:
            store = myarbs.MyArbs(Path(d) / "my_arbs.json")
            pairs, unpaired = accounts.pair_positions(self.k, self.p, self.lookup)
            info = lambda kc: {"game": kc.game_label, "tab": "Economics", "closes": None}
            store.sync_from_accounts(pairs, self.k, self.p, unpaired, info)
            a = store.items[0]
            self.assertEqual((a["id"], a["source"], a["legs"][0]["paid"], a["legs"][1]["paid"]),
                             ("acct-KXGAS-5.20-gas-5pt20", "account", 20.08, 75.0))
            self.assertEqual(myarbs.summarize(a)["profit"], round(100 - 20.08 - 75.0, 2))
            self.k["KXGAS-5.20"]["shares"] = 120                          # bought more: sync follows
            store.sync_from_accounts(pairs, self.k, self.p, unpaired, info)
            self.assertEqual((len(store.items), store.items[0]["legs"][0]["shares"]), (1, 120))
            store.save({**store.items[0], "note": "mine", "edited": True})  # you edited it: sync leaves it
            self.k["KXGAS-5.20"]["shares"] = 150
            store.sync_from_accounts(pairs, self.k, self.p, unpaired, info)
            self.assertEqual((store.items[0]["legs"][0]["shares"], store.items[0]["note"]), (120, "mine"))
            self.assertEqual(len(store.unpaired), 2)


if __name__ == "__main__":
    unittest.main()


class BalanceTests(unittest.TestCase):
    def accts(self, k_pages=None, p_pages=None):
        a = accounts.Accounts.__new__(accounts.Accounts)
        a.kalshi_http = FakeHTTP(k_pages) if k_pages is not None else None
        a.pm_http = FakeHTTP(p_pages) if p_pages is not None else None
        return a

    def test_both_sites(self):
        a = self.accts([{"balance": 12345}], [{"balances": [{"currentBalance": 500, "currency": "USD",
                                                             "buyingPower": 412.5}]}])
        self.assertEqual(a.balances(), {"kalshi": 123.45, "polymarket": 412.5})
        self.assertEqual(a.kalshi_http.calls[0][0], "/portfolio/balance")
        self.assertEqual(a.pm_http.calls[0][0], "/v1/account/balances")

    def test_kalshi_dollars_field_wins(self):
        self.assertEqual(self.accts([{"balance": 100, "balance_dollars": "7.25"}]).balances(), {"kalshi": 7.25})

    def test_missing_site_is_left_out(self):
        self.assertEqual(self.accts(None, [{"balances": []}]).balances(), {"polymarket": 0.0})
        self.assertEqual(self.accts().balances(), {})

    def test_scanner_keeps_last_amounts_on_error(self):
        from arb import scanner

        class Boom:
            missing = ["Polymarket"]
            n = 0

            def balances(self):
                self.n += 1
                if self.n > 1:
                    raise ConnectionError("down")
                return {"kalshi": 50.0}

        s = scanner.Scanner.__new__(scanner.Scanner)
        s.lock, s.state, s.accounts = __import__("threading").Lock(), {}, Boom()
        s.refresh_balances()
        self.assertEqual(s.state["balances"]["kalshi"], 50.0)
        s.refresh_balances()
        self.assertEqual(s.state["balances"]["kalshi"], 50.0)
        self.assertIn("down", s.state["balances"]["error"])


class HeldFeeTests(unittest.TestCase):
    """Kalshi's positions list gives lifetime fees for a market; the cost of what you hold uses only
    the fees on the contracts still held (what Kalshi's app shows as "includes fee of")."""

    def setUp(self):
        accounts._fee_cache.clear()

    def fill(self, side, n, fee, ts, **kw):
        return {"outcome_side": side, "count_fp": f"{n:.2f}", "fee_cost": f"{fee:.4f}", "ts": ts, **kw}

    def test_a_round_trip_earlier_is_not_part_of_the_cost(self):
        fills = [self.fill("yes", 5, 0.05, 1), self.fill("no", 5, 0.05, 2),      # bought 5, sold them
                 self.fill("yes", 9, 0.04, 3), self.fill("yes", 1, 0.01, 4)]     # then the 10 held
        self.assertEqual(accounts.held_fee(fills, 10), 0.05)

    def test_partial_sale_takes_out_its_share_and_a_flip_keeps_the_new_side(self):
        self.assertEqual(accounts.held_fee([self.fill("yes", 10, 0.10, 1), self.fill("no", 4, 0.03, 2)], 6), 0.06)
        self.assertEqual(accounts.held_fee([self.fill("yes", 5, 0.05, 1), self.fill("no", 8, 0.08, 2)], -3), 0.03)

    def test_legacy_action_and_side_fields(self):
        fills = [{"action": "buy", "side": "no", "count_fp": "4.00", "fee_cost": "0.0200", "ts": 1},
                 {"action": "sell", "side": "no", "count_fp": "1.00", "fee_cost": "0.0100", "ts": 2}]
        self.assertEqual(accounts.held_fee(fills, -3), 0.015)

    def test_fills_that_dont_add_up_give_none(self):
        self.assertIsNone(accounts.held_fee([self.fill("yes", 9, 0.04, 1)], 10))

    def http(self, fills):
        class H:
            calls = []

            def get(self, path, params=None):
                self.calls.append(path)
                if path == "/portfolio/positions":
                    return {"market_positions": [{"ticker": "KXOSCAR-DAMON", "position_fp": "10.00",
                                                  "market_exposure_dollars": "9.2000", "fees_paid_dollars": "0.1500"}]}
                return {"fills": fills, "cursor": ""}
        return H()

    def test_cost_is_the_position_plus_the_fee_on_it(self):
        h = self.http([self.fill("yes", 5, 0.05, 1), self.fill("no", 5, 0.05, 2), self.fill("yes", 10, 0.05, 3)])
        k = accounts.kalshi_positions(h)["KXOSCAR-DAMON"]
        self.assertEqual((k["paid"], k["fees"]), (9.25, 0.05))                    # Kalshi's app: Cost $9.25
        accounts.kalshi_positions(h)
        self.assertEqual(h.calls.count("/portfolio/fills"), 1)                     # once per position change

    def test_a_direct_position_fee_field_is_used_as_is(self):
        h = self.http([])
        h.get = lambda path, params=None: {"market_positions": [{
            "ticker": "KX-1", "position_fp": "10.00", "market_exposure_dollars": "9.2000",
            "fees_paid_dollars": "0.1500", "position_fee_cost_dollars": "0.0500"}]} if path == "/portfolio/positions" \
            else self.fail("fills read")
        self.assertEqual(accounts.kalshi_positions(h)["KX-1"]["paid"], 9.25)
