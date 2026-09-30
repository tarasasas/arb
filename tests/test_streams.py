import json
import threading
import time
import unittest

from arb import streams


class Market:
    def __init__(self, mid):
        self.ticker = self.slug = mid
        self.levels, self.yes_ask, self.no_ask, self.state = {}, None, None, None


class FakeWS:
    """Scripted connection: recv() returns queued messages, then blocks until closed."""
    def __init__(self, messages):
        self.inbox, self.sent, self.closed = list(messages), [], threading.Event()

    def send(self, s):
        self.sent.append(json.loads(s))

    def recv(self):
        if self.inbox:
            m = self.inbox.pop(0)
            return m if isinstance(m, str) else json.dumps(m)
        self.closed.wait(5)
        return ""

    def close(self):
        self.closed.set()


def run_until(cond, timeout=3):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.01)
    return cond()


class BookMathTests(unittest.TestCase):
    def test_kalshi_bids_to_buy_levels(self):
        lv = streams.kalshi_levels({"yes": {0.40: 10, 0.38: 5}, "no": {0.55: 7, 0.50: 3}})
        self.assertEqual(lv["yes"], [(0.45, 7), (0.5, 3)])      # buy YES = 1 - NO bid, cheapest first
        self.assertEqual(lv["no"], [(0.6, 10), (0.62, 5)])

    def test_polymarket_book_to_buy_levels(self):
        lv = streams.polymarket_levels({"bids": [{"px": {"value": "0.55"}, "qty": "2"}, {"px": {"value": "0.555"}, "qty": "1"}],
                                        "offers": [{"px": {"value": "0.57"}, "qty": "3"}, {"px": {"value": "0.56"}, "qty": "4"}]})
        self.assertEqual(lv["yes"], [(0.56, 4), (0.57, 3)])
        self.assertEqual(lv["no"], [(0.445, 1), (0.45, 2)])      # Buy No costs 1 - bid, best bid first

    def test_untradable_market_gets_no_prices(self):
        m = Market("x")
        streams.apply_levels(m, {"yes": [(0.5, 1)], "no": [(0.5, 1)]}, tradable=False)
        self.assertEqual((m.yes_ask, m.no_ask, m.levels), (None, None, {"yes": [], "no": []}))


class KalshiStreamTests(unittest.TestCase):
    def make(self, scripts):
        self.conns, self.updates, self.logs = [], [], []
        self.m = {"K1": Market("K1"), "K2": Market("K2")}

        def connect(url, headers):
            ws = FakeWS(scripts.pop(0) if scripts else [])
            self.conns.append((headers, ws))
            return ws
        s = streams.KalshiStream(lambda method, path: {"KALSHI-ACCESS-KEY": "id", "path": path}, self.m,
                                 lambda ex, mid: self.updates.append((ex, mid)), self.logs.append, connect=connect)
        s.want(["K1"])
        return s

    def test_snapshot_then_deltas_keep_the_book(self):
        s = self.make([[{"type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 7}},
                        {"type": "orderbook_snapshot", "sid": 7, "seq": 1,
                         "msg": {"market_ticker": "K1", "yes_dollars_fp": [["0.40", "10.00"]], "no_dollars_fp": [["0.55", "5.00"]]}},
                        {"type": "orderbook_delta", "sid": 7, "seq": 2,
                         "msg": {"market_ticker": "K1", "price_dollars": "0.5600", "delta_fp": "3.00", "side": "no"}},
                        {"type": "orderbook_delta", "sid": 7, "seq": 3,
                         "msg": {"market_ticker": "K1", "price_dollars": "0.4000", "delta_fp": "-10.00", "side": "yes"}}]])
        s.start()
        self.assertTrue(run_until(lambda: len(self.updates) == 3))
        self.assertEqual(self.m["K1"].levels, {"yes": [(0.44, 3), (0.45, 5)], "no": []})
        self.assertEqual((self.m["K1"].yes_ask, self.m["K1"].no_ask), (0.44, None))
        headers, ws = self.conns[0]
        self.assertEqual(headers["path"], "/trade-api/ws/v2")          # signed like the docs say
        self.assertEqual(ws.sent[0]["params"], {"channels": ["orderbook_delta"], "market_tickers": ["K1"]})
        s.want(["K1", "K2"])                                             # new market on the live connection
        self.assertEqual(ws.sent[-1]["cmd"], "update_subscription")
        self.assertEqual(ws.sent[-1]["params"], {"sid": 7, "market_tickers": ["K2"], "action": "add_markets"})
        s.stop()

    def test_missed_message_forces_a_resync(self):
        snap = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                "msg": {"market_ticker": "K1", "yes_dollars_fp": [["0.40", "10.00"]], "no_dollars_fp": []}}
        gap = {"type": "orderbook_delta", "sid": 1, "seq": 5,
               "msg": {"market_ticker": "K1", "price_dollars": "0.40", "delta_fp": "1.00", "side": "yes"}}
        s = self.make([[snap, gap], [snap]])
        s.start()
        self.assertTrue(run_until(lambda: len(self.conns) == 2, timeout=5))   # reconnected after the gap
        self.assertTrue(any("missed Kalshi messages" in line for line in self.logs))
        s.stop()


class PolymarketStreamTests(unittest.TestCase):
    def test_market_data_updates_the_market(self):
        m, updates = {"p1": Market("p1")}, []
        ws = FakeWS([{"heartbeat": {}},
                     {"requestId": "md-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                      "marketData": {"marketSlug": "p1", "state": "MARKET_STATE_OPEN",
                                     "bids": [{"px": {"value": "0.40"}, "qty": "5"}],
                                     "offers": [{"px": {"value": "0.42"}, "qty": "8"}]}},
                     {"marketData": {"marketSlug": "p1", "state": "MARKET_STATE_HALTED", "bids": [], "offers": []}}])
        s = streams.PolymarketStream(lambda method, path: {"X-PM-Access-Key": "id"}, m,
                                     lambda ex, mid: updates.append(mid), lambda msg: None,
                                     connect=lambda url, headers: ws)
        s.want([f"p{i}" for i in range(1, 151)])                         # 150 markets -> 2 subscriptions
        s.start()
        self.assertTrue(run_until(lambda: len(updates) == 2))
        subs = [x["subscribe"] for x in ws.sent]
        self.assertEqual([len(x["marketSlugs"]) for x in subs], [100, 50])
        self.assertEqual(subs[0]["subscriptionType"], "SUBSCRIPTION_TYPE_MARKET_DATA")
        self.assertEqual((m["p1"].yes_ask, m["p1"].no_ask, m["p1"].state), (None, None, "MARKET_STATE_HALTED"))
        s.stop()


@unittest.skipUnless(streams.available(), "websocket-client not installed")
class RealSocketTests(unittest.TestCase):
    def test_end_to_end_over_a_real_websocket(self):
        try:
            import asyncio
            import websockets
        except ImportError:
            self.skipTest("websockets (server side) not installed")
        got_headers, ready, port = {}, threading.Event(), []

        async def handler(ws):
            got_headers.update(dict(ws.request.headers))
            sub = json.loads(await ws.recv())
            await ws.send(json.dumps({"type": "subscribed", "id": sub["id"], "msg": {"sid": 1}}))
            await ws.send(json.dumps({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
                "market_ticker": sub["params"]["market_tickers"][0], "yes_dollars_fp": [["0.30", "4.00"]],
                "no_dollars_fp": [["0.65", "9.00"]]}}))
            await asyncio.sleep(5)

        def serve():
            async def main():
                async with websockets.serve(handler, "127.0.0.1", 0) as server:
                    port.append(server.sockets[0].getsockname()[1])
                    ready.set()
                    await asyncio.sleep(10)
            asyncio.run(main())
        threading.Thread(target=serve, daemon=True).start()
        self.assertTrue(ready.wait(5))
        m, updates = {"K1": Market("K1")}, []
        s = streams.KalshiStream(lambda method, path: {"KALSHI-ACCESS-KEY": "key-id", "KALSHI-ACCESS-SIGNATURE": "sig"},
                                 m, lambda ex, mid: updates.append(mid), lambda msg: None, url=f"ws://127.0.0.1:{port[0]}")
        s.want(["K1"])
        s.start()
        self.assertTrue(run_until(lambda: updates, timeout=5))
        self.assertEqual((m["K1"].yes_ask, m["K1"].no_ask), (0.35, 0.7))
        self.assertEqual(got_headers.get("kalshi-access-key") or got_headers.get("KALSHI-ACCESS-KEY"), "key-id")
        s.stop()


if __name__ == "__main__":
    unittest.main()
