import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from arb import accounts, shards


class SplitTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(shards.parse_split(""), {0: 50, 2: 30, 3: 20})
        self.assertEqual(shards.parse_split("60, 40, 0"), {0: 60, 2: 40, 3: 0})
        for bad in ("50 30", "50 30 30", "-10 60 50", "a b c"):
            with self.assertRaises(ValueError):
                shards.parse_split(bad)

    def test_body(self):
        self.assertEqual(shards.allocation_body({0: 60, 2: 40, 3: 0}),
                         {"allocations": [{"exchange_index": 0, "percent": 60}, {"exchange_index": 2, "percent": 40}]})
        self.assertEqual(shards.allocation_body({0: 100, 2: 0, 3: 0}), {"allocations": []})


class FakeHTTP:
    signer = object()

    def __init__(self):
        self.posts = []

    def get(self, path, params=None):
        if path == "/portfolio/balance":
            return {"balance": 50000, "balance_breakdown": [{"exchange_index": 0, "balance": "500.00"}]}
        return {"allocations": []}

    def post(self, path, body):
        self.posts.append((path, body))
        return {}


class MainTests(unittest.TestCase):
    def test_sets_the_chosen_split(self):
        http = FakeHTTP()
        out = io.StringIO()
        answers = iter(["oops", "40 40 20"])
        with mock.patch.object(shards, "KalshiClient", return_value=mock.Mock(http=http)), redirect_stdout(out):
            self.assertEqual(shards.main(ask=lambda p: next(answers)), 0)
        self.assertEqual(http.posts, [("/portfolio/target_balance_allocation", {"allocations": [
            {"exchange_index": 0, "percent": 40}, {"exchange_index": 2, "percent": 40}, {"exchange_index": 3, "percent": 20}]})])
        self.assertIn("shard 0 (main", out.getvalue())


class BalanceBreakdownTests(unittest.TestCase):
    def test_per_shard_cash(self):
        a = accounts.Accounts.__new__(accounts.Accounts)
        a.pm_http = None

        class H:
            def get(self, path, params=None):
                return {"balance": 12000, "balance_breakdown": [{"exchange_index": 0, "balance": "100.00"},
                                                                {"exchange_index": 2, "balance": "20.00"}]}
        a.kalshi_http = H()
        self.assertEqual(a.balances(), {"kalshi": 120.0, "kalshi_shards": {"0": 100.0, "2": 20.0}})
