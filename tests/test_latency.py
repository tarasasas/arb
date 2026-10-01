import unittest

from arb import latency
from arb.venues import Fill


class FakeHTTP:
    def __init__(self, items):
        self.items = items

    def get(self, path, params=None):
        return self.items


class FakeKalshi:
    def __init__(self, fill=0):
        self.client = type("C", (), {"http": FakeHTTP({"markets": [
            {"ticker": "CHEAP", "exchange_index": 0}, {"ticker": "K1", "exchange_index": 0}]})})()
        self.orders, self.fill = [], fill

    def levels(self, t):
        return {"yes": [(0.02, 5)] if t == "CHEAP" else [(0.45, 10)], "no": []}

    def market_info(self, t):
        return {"open": True}

    def balance(self, shard=None):
        return 50.0

    def shard_balances(self):
        return {0: 50.0}

    def buy(self, *a):
        self.orders.append(a)
        return Fill(qty=self.fill)


class FakePM:
    def __init__(self):
        self.public = type("P", (), {"http": FakeHTTP({"markets": [{"slug": "p1"}]})})()
        self.orders = []

    def levels(self, s):
        return {"yes": [(0.30, 10)], "no": []}

    def market_info(self, s):
        return {"open": True}

    def balance(self, shard=None):
        return 20.0

    def buy(self, *a):
        self.orders.append(a)
        return Fill()


class LatencyTests(unittest.TestCase):
    def test_times_every_step_and_orders_only_after_yes(self):
        k, p = FakeKalshi(), FakePM()
        code, lines = latency.run(k, p, ask=lambda _q: "n", runs=2, log=lambda s: None)
        text = "\n".join(lines)
        self.assertIn("K1", text)                               # skipped the market with a 2 cent seller
        self.assertIn("Polymarket  order book", text)
        self.assertEqual((k.orders, p.orders), ([], []))         # "n": nothing sent

        code, lines = latency.run(k, p, ask=lambda _q: "y", runs=2, log=lambda s: None)
        self.assertEqual(len(k.orders), 4)                       # 2 alone + 2 together
        self.assertTrue(all(o[2] == 1 and o[3] == 0.01 for o in k.orders + p.orders))   # 1 share at 1 cent
        self.assertIn("No test order filled.", "\n".join(lines))

    def test_stops_if_a_test_order_fills(self):
        k, p = FakeKalshi(fill=1), FakePM()
        code, lines = latency.run(k, p, ask=lambda _q: "y", runs=3, log=lambda s: None)
        self.assertEqual(len(k.orders), 1)
        self.assertIn("A test order filled", "\n".join(lines))
