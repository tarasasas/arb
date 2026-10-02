import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

from arb import accounts, shards


class SplitTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(shards.parse_split(""), {0: 34, 2: 33, 3: 33})          # Enter = even
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


class RebalancingTests(unittest.TestCase):
    class HTTP:
        def __init__(self, cur):
            self.cur, self.posts = cur, []

        def get(self, path, params=None):
            return {"allocations": self.cur}

        def post(self, path, body):
            self.posts.append((path, body))
            self.cur = body["allocations"]
            return {}

    def venue(self, cur):
        from arb.venues import KalshiVenue
        http = self.HTTP(cur)
        return KalshiVenue(mock.Mock(http=http)), http

    def test_even_split_is_set_and_left_alone_once_set(self):
        self.assertEqual(shards.even_split(), {0: 34, 2: 33, 3: 33})
        v, http = self.venue([{"exchange_index": 0, "percent": 50}, {"exchange_index": 2, "percent": 50}])
        self.assertEqual(len(v.set_rebalancing(shards.even_split())), 2)     # returns what it replaced
        self.assertEqual(http.posts, [("/portfolio/target_balance_allocation", {"allocations": [
            {"exchange_index": 0, "percent": 34}, {"exchange_index": 2, "percent": 33},
            {"exchange_index": 3, "percent": 33}]})])
        self.assertIsNone(v.set_rebalancing(shards.even_split()))          # already set: nothing sent
        self.assertEqual(len(http.posts), 1)

    def test_turning_it_off(self):
        v, http = self.venue([{"exchange_index": 0, "percent": 50}, {"exchange_index": 2, "percent": 50}])
        v.set_rebalancing({})
        self.assertEqual(http.posts, [("/portfolio/target_balance_allocation", {"allocations": []})])
        v, http = self.venue([])
        self.assertIsNone(v.set_rebalancing({}))
        self.assertEqual(http.posts, [])

    def test_percent_formats_kalshi_may_send(self):
        v, http = self.venue([{"exchange_index": "0", "percent": "34"}, {"exchange_index": 2, "percent": 33.0},
                              {"exchange_index": 3, "percent": 33}])
        self.assertIsNone(v.set_rebalancing(shards.even_split()))
        self.assertEqual(http.posts, [])


class ShardModeTests(unittest.TestCase):
    def test_mode_from_env(self):
        from arb import config
        self.assertEqual(config._shard_mode({}), "even")                                      # default
        self.assertEqual(config._shard_mode({"KALSHI_SHARD_MODE": "per_trade"}), "per_trade")
        self.assertEqual(config._shard_mode({"KALSHI_AUTO_SHARD_FUNDING": "0"}), "manual")    # older .env
        self.assertEqual(config._shard_mode({"KALSHI_AUTO_SHARD_FUNDING": "1"}), "even")
        self.assertEqual(config._shard_mode({"KALSHI_SHARD_MODE": "nonsense"}), "even")
