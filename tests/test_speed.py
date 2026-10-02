"""Auto-trade speed: kept-alive connections that are safe to reuse, the pre-trade checks in one round
trip, market details loaded ahead of time, and no waiting the live feeds can spare."""

import http.server
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from arb import accounts, autotrade, config, streams
from arb import trader as trader_mod
from arb.http import RateLimitedClient, is_priority
from arb.trader import Trader
from tests.test_trader import LEGS, FakeScanner, FakeVenue


# ---- connections ---------------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive

    def _reply(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        self.server.requests.append((self.command, body))
        out = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = _reply

    def log_message(self, *a):
        pass


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests, self.accepted = [], []

    def get_request(self):
        sock, addr = super().get_request()
        self.accepted.append(sock)
        return sock, addr

    def drop_idle(self):
        """Close every connection from the server's side, as an exchange does with idle ones: its FIN reaches
        us, but (as over the internet, where the reset to anything we send comes back a round trip later) a
        request sent on it still goes out."""
        import socket
        for s in self.accepted:
            try:
                s.shutdown(socket.SHUT_WR)
            except OSError:
                pass


class ConnectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.srv = _Server()
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.c = RateLimitedClient(f"http://127.0.0.1:{self.srv.server_address[1]}", rps=1000, max_retries=1)
        self.c._proxy = None               # straight to the local server, whatever proxy the machine has

    def tearDown(self):
        for conn, _ in self.c._pool:
            conn.close()
        self.srv.shutdown()
        self.srv.server_close()

    def test_requests_share_one_connection(self):
        for _ in range(3):
            self.c.get("/x")
        self.c.post("/order", {"a": 1})
        self.assertEqual(len(self.srv.accepted), 1)

    def test_an_order_never_goes_out_on_a_connection_the_server_closed(self):
        self.c.get("/x")
        self.srv.drop_idle()
        time.sleep(0.05)                   # the close reaches us
        self.assertEqual(self.c.post("/order", {"a": 1}), {"ok": True})     # not an unconfirmed order
        self.assertEqual([r for r in self.srv.requests if r[0] == "POST"], [("POST", b'{"a": 1}')])
        self.assertEqual(len(self.srv.accepted), 2)

    def test_warm_opens_a_connection_only_when_none_is_recent(self):
        self.c.warm()
        time.sleep(0.05)
        self.assertEqual(len(self.srv.accepted), 1)
        self.c.post("/order", {"a": 1})                   # used the warmed one
        self.assertEqual(len(self.srv.accepted), 1)
        used = self.c._pool[-1][1]
        self.c.warm()                                      # recent enough: left alone, idle clock untouched
        self.assertEqual((len(self.srv.accepted), self.c._pool[-1][1]), (1, used))
        self.c._pool[-1] = (self.c._pool[-1][0], time.monotonic() - self.c.POST_IDLE_MAX)
        self.c.warm()                                      # too old for an order soon: a new one
        time.sleep(0.05)
        self.assertEqual(len(self.srv.accepted), 2)


# ---- the pre-trade checks -------------------------------------------------------------------------

def scanner(shard=None, streams_on=False, cash=None):
    sc = FakeScanner()
    sc.lock = threading.Lock()
    sc.source = {("kalshi", "K"): SimpleNamespace(levels={"yes": [(0.40, 500)], "no": []}, shard=shard),
                 ("polymarket", "P"): SimpleNamespace(levels={"yes": [(0.55, 500)], "no": [(0.50, 500)]})}
    now = time.time()
    if streams_on:
        sc.streams = {ex: SimpleNamespace(connected=True, updated_at={mid: now}, last_msg=now)
                      for ex, mid in (("kalshi", "K"), ("polymarket", "P"))}
    if cash is not None:
        sc.state = {"balances": {"kalshi": cash, "polymarket": cash, "kalshi_shards": {"0": cash, "2": cash},
                                 "time": trader_mod.engine.now_utc().isoformat()}}
    return sc


class Recording(FakeVenue):
    def __init__(self, *a, shard=None, wait=None, **kw):
        super().__init__(*a, **kw)
        self.shard, self.wait, self.calls = shard, wait or (lambda kind: None), []

    def levels(self, mid):
        self.calls.append("levels")
        self.wait("levels")
        return super().levels(mid)

    def market_info(self, mid):
        self.calls.append(("info", is_priority()))
        self.wait("info")
        return {**super().market_info(mid), "shard": self.shard}

    def balance(self, shard=None):
        self.calls.append(("balance", shard))
        self.wait("balance")
        return super().balance(shard)


class OneRoundChecksTests(unittest.TestCase):
    def test_every_download_on_both_sites_is_in_flight_at_once(self):
        barrier = threading.Barrier(6, timeout=3)         # book, details and cash on each site
        wait = lambda _kind: barrier.wait()
        k = Recording("kalshi", yes=[(0.40, 500)], shard=2, wait=wait)
        p = Recording("polymarket", no=[(0.50, 500)], wait=wait)
        plan = Trader(scanner(shard=2), {"kalshi": k, "polymarket": p}).prepare(LEGS)
        self.assertEqual(plan["size"], 107)
        self.assertIn(("balance", 2), k.calls)             # the market's own shard
        self.assertEqual({plan["checks"][f"{ex}_{w}"] for ex in ("kalshi", "polymarket")
                          for w in ("book", "info", "cash")}, {"download"})

    def test_kalshi_cash_doesnt_wait_for_the_market_details(self):
        barrier = threading.Barrier(2, timeout=3)
        wait = lambda kind: barrier.wait() if kind in ("info", "balance") else None
        k = Recording("kalshi", yes=[(0.40, 500)], shard=2, wait=wait)
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        sc = scanner(shard=2, streams_on=True)
        sc.state = {"balances": {"polymarket": 500.0, "time": trader_mod.engine.now_utc().isoformat()}}
        Trader(sc, {"kalshi": k, "polymarket": p}).prepare(LEGS)
        self.assertEqual([c for c in k.calls if c[0] == "balance"], [("balance", 2)])

    def test_a_shard_that_moved_is_read_again_on_the_right_one(self):
        k = Recording("kalshi", yes=[(0.40, 500)], shard=3)
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        Trader(scanner(shard=2), {"kalshi": k, "polymarket": p}).prepare(LEGS)
        self.assertEqual([c for c in k.calls if c[0] == "balance"][-1], ("balance", 3))

    def test_unknown_shard_waits_for_the_details_as_before(self):
        k = Recording("kalshi", yes=[(0.40, 500)], shard=2)
        p = FakeVenue("polymarket", no=[(0.50, 500)])
        Trader(scanner(shard=None), {"kalshi": k, "polymarket": p}).prepare(LEGS)
        self.assertEqual([c for c in k.calls if c[0] == "balance"], [("balance", 2)])


class PrefetchTests(unittest.TestCase):
    def wait_idle(self, t):
        for _ in range(200):
            if not t._prefetching:
                return
            time.sleep(0.005)

    def test_prefetched_details_make_the_checks_download_nothing(self):
        k = Recording("kalshi", yes=[(0.40, 500)], shard=0)
        p = Recording("polymarket", no=[(0.50, 500)])
        t = Trader(scanner(shard=0, streams_on=True, cash=500.0), {"kalshi": k, "polymarket": p})
        self.assertEqual(t.prefetch_info([("kalshi", "K"), ("polymarket", "P")]), 2)
        self.wait_idle(t)
        self.assertEqual((k.calls, p.calls), ([("info", False)], [("info", False)]))   # never the priority lane
        k.calls.clear(); p.calls.clear()
        plan = t.prepare(LEGS)
        self.assertEqual((k.calls, p.calls), ([], []))
        self.assertEqual((plan["checks"]["kalshi_info"], plan["checks"]["polymarket_info"]), ("cached", "cached"))

    def test_loaded_again_only_once_older_than_the_refresh_age(self):
        k = Recording("kalshi", yes=[(0.40, 500)])
        t = Trader(scanner(), {"kalshi": k, "polymarket": FakeVenue("polymarket")})
        t.prefetch_info([("kalshi", "K")])
        self.wait_idle(t)
        self.assertEqual(t.prefetch_info([("kalshi", "K")]), 0)            # still young
        sent, info = t._info_cache[("kalshi", "K")]
        t._info_cache[("kalshi", "K")] = (sent - config.INFO_PREFETCH_AGE - 1, info)
        self.assertEqual(t.prefetch_info([("kalshi", "K")]), 1)
        self.wait_idle(t)
        self.assertEqual(len(k.calls), 2)

    def test_nothing_without_trading(self):
        self.assertEqual(Trader(scanner(), None).prefetch_info([("kalshi", "K")]), 0)

    def test_scanner_names_the_lane_pairs_closest_to_an_arb(self):
        from arb.scanner import Scanner
        sc = Scanner.__new__(Scanner)
        sc.trader = mock.Mock(venues={"kalshi": 1})
        sc.lane_active = lambda: True
        sc.lane_groups = lambda: {("G1", "v"): {}, ("G2", "v"): {}}
        cand = lambda g, k, p, edge: {"k": SimpleNamespace(game_key=g, var="v", market_id=k),
                                      "p": SimpleNamespace(market_id=p), "edge": edge}
        cands = [cand("G1", "K1", "P1", 0.02), cand("G9", "K9", "P9", 0.01),      # G9: not one Auto-trade takes
                 cand("G2", "K2", "P2", -0.005), cand("G1", "K3", "P3", -0.02)]   # K3: too far from an arb
        sc._prefetch_trade_info(cands)
        sc.trader.prefetch_info.assert_called_once_with(
            [("kalshi", "K1"), ("polymarket", "P1"), ("kalshi", "K2"), ("polymarket", "P2")])
        sc.trader.prefetch_info.reset_mock()
        sc.lane_active = lambda: False
        sc._prefetch_trade_info(cands)
        sc.trader.prefetch_info.assert_not_called()


# ---- after the orders ------------------------------------------------------------------------------

@mock.patch.object(trader_mod.config, "TRADES_LOG", new_callable=lambda: __import__("pathlib").Path(
    __import__("tempfile").gettempdir()) / "arb_test_trades.jsonl")
@mock.patch.object(trader_mod.config, "CLOSE_OUT_MAX_LOSS", 0.0)
class LiveBooksAfterAMissTests(unittest.TestCase):
    def test_close_out_reads_the_live_feeds_not_a_download(self, *_):
        k = Recording("kalshi", yes=[(0.40, 1000)], no=[(0.58, 100)])
        p = Recording("polymarket", no=[(0.50, 20)], yes=[(0.60, 100)])
        sc = scanner(streams_on=True, cash=10_000.0)
        sc.source[("kalshi", "K")].levels = k.levels("K")
        sc.source[("polymarket", "P")].levels = p.levels("P")
        t = Trader(sc, {"kalshi": k, "polymarket": p})
        plan = t.prepare(LEGS, order="thinner_first")
        k.book["yes"] = [(0.40, 5)]                        # the second leg only gets 5 of 20
        sc.source[("kalshi", "K")].levels = {"yes": [(0.40, 5)], "no": [(0.58, 100)]}
        k.calls.clear(); p.calls.clear()
        res = t.execute(plan["id"])
        self.assertEqual(res["status"], "partial")
        self.assertEqual([o[2] for o in p.orders if o[0] == "sell"], [15])    # sold back on the live bids
        self.assertNotIn("levels", k.calls + p.calls)      # no round trip before closing out, nor for the report
        self.assertIn("Kalshi", res["missed"][0]["exchange"])


class RetryWakeTests(unittest.TestCase):
    def test_the_stream_wakes_a_waiting_retry_at_once(self):
        st = streams.KalshiStream(None, {}, lambda *a: None, lambda *a: None)
        st.connected = True
        market = SimpleNamespace(levels={"yes": [(0.60, 100)], "no": []})
        sc = FakeScanner()
        sc.streams, sc.source = {"kalshi": st}, {("kalshi", "K"): market}
        since = time.time()

        def refill():
            time.sleep(0.05)
            market.levels = {"yes": [(0.41, 100)], "no": []}
            st._updated("K")
        threading.Thread(target=refill).start()
        t0 = time.monotonic()
        with mock.patch.object(trader_mod.config, "SECOND_LEG_RETRY_PAUSE", 2.0), \
                mock.patch.object(trader_mod.time, "sleep", side_effect=AssertionError("polled")):
            Trader(sc, {})._await_liquidity({"exchange": "kalshi", "market_id": "K", "side": "yes"}, 0.42, since)
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(st.waiters, {})                  # cleaned up


# ---- cash reads that keep the order connections warm ---------------------------------------------

class BalanceReadTests(unittest.TestCase):
    def test_both_sites_are_read_at_once(self):
        barrier = threading.Barrier(2, timeout=3)

        class HTTP:
            def __init__(self, reply):
                self.reply = reply

            def get(self, path, params=None):
                barrier.wait()
                return self.reply
        a = accounts.Accounts.__new__(accounts.Accounts)
        a.kalshi_http = HTTP({"balance": 1000})
        a.pm_http = HTTP({"balances": [{"currency": "USD", "buyingPower": 5}]})
        self.assertEqual(a.balances(), {"kalshi": 10.0, "polymarket": 5.0})

    def test_polymarket_reads_share_the_order_client(self):
        order_client = object()
        kc = SimpleNamespace(http=SimpleNamespace(signer=None))
        self.assertIs(accounts.Accounts(kc, pm_http=order_client).pm_http, order_client)

    def test_cash_is_read_more_often_while_auto_trade_runs(self):
        from arb.scanner import Scanner
        sc = Scanner.__new__(Scanner)
        sc.lane_active = lambda: True
        self.assertEqual(sc.balances_every(), config.BALANCES_REFRESH_AUTO_SECS)
        self.assertLess(config.BALANCES_REFRESH_AUTO_SECS, RateLimitedClient.POST_IDLE_MAX / 2)
        sc.lane_active = lambda: False
        self.assertEqual(sc.balances_every(), config.BALANCES_REFRESH_SECS)

    def test_turning_auto_trade_on_opens_the_order_connections(self):
        warmed = []
        sc = SimpleNamespace(trader=SimpleNamespace(venues={"kalshi": 1}, warm=lambda: warmed.append(1)),
                             log=lambda m: None)
        autotrade.AutoTrader(sc, run_async=False).set(True)
        self.assertEqual(warmed, [1])


if __name__ == "__main__":
    unittest.main()
