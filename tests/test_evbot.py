import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from arb import config, evbot
from arb.http import ApiError
from arb.model import NO, YES, Contract, fee_per_contract
from arb.venues import Fill

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
TOTAL = ("total", "FG")


def pair(k_yes=0.55, k_no=0.47, p_yes=0.62, p_no=0.42, start_in=3, decided_in=6, var=TOTAL, p_line=45.5, p_op=">"):
    k = Contract("kalshi", "KO45", "NFL:x", var, ">", 45.5, "Over 45.5", fee_coef=0.07,
                 close_time=(NOW + timedelta(hours=decided_in)).isoformat(), game_label="KC vs BUF, Oct 3")
    p = Contract("polymarket", "pm-o45", "NFL:x", var, p_op, p_line, "O/U 45.5", fee_coef=0.0695,
                 close_time=(NOW + timedelta(hours=start_in)).isoformat(), game_label="KC vs BUF, Oct 3")
    k.ask, p.ask = {YES: k_yes, NO: k_no}, {YES: p_yes, NO: p_no}
    source = {("kalshi", "KO45"): SimpleNamespace(levels={"yes": [(k_yes, 500)], "no": [(k_no, 500)]}, shard=0),
              ("polymarket", "pm-o45"): SimpleNamespace(levels={"yes": [(p_yes, 500)], "no": [(p_no, 500)]})}
    return {("NFL:x", var): {"kalshi": [k], "polymarket": [p]}}, source


POLY_MOVED = lambda k, p: "polymarket"


class FairValueTests(unittest.TestCase):
    def test_consensus_leans_on_the_tighter_book(self):
        (g, _), = [pair()]
        k, p = g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]
        fv = evbot.fair_value(k, p, 1)
        # Kalshi mid 0.54 (2c wide), Polymarket mid 0.60 (4c wide): weights 2:1
        self.assertAlmostEqual(fv["fair"], (0.54 * 2 + 0.60) / 3, 6)
        self.assertEqual(fv["source"], "consensus")
        self.assertAlmostEqual(evbot.fair_value(k, p, 1, "polymarket")["fair"], 0.60, 9)

    def test_wide_books_or_big_disagreement_give_no_price(self):
        g, _ = pair(p_yes=0.70, p_no=0.36)                       # mids 0.54 vs 0.67: 13c apart
        k, p = g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]
        self.assertIsNone(evbot.fair_value(k, p, 1))
        g, _ = pair(k_yes=0.58, k_no=0.48)                       # Kalshi 6c wide
        k, p = g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]
        self.assertIsNone(evbot.fair_value(k, p, 1))

    def test_only_the_exact_same_question(self):
        g, _ = pair()
        k, p = g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]
        self.assertEqual(evbot.relation(k, p), 1)
        g, _ = pair(p_line=44.5)
        self.assertEqual(evbot.relation(*[g[("NFL:x", TOTAL)][ex][0] for ex in ("kalshi", "polymarket")]), 0)
        g, _ = pair(p_op="<")
        self.assertEqual(evbot.relation(*[g[("NFL:x", TOTAL)][ex][0] for ex in ("kalshi", "polymarket")]), -1)


class CandidateTests(unittest.TestCase):
    def test_consensus_on_tight_books_leaves_no_bet(self):
        g, src = pair()
        self.assertEqual(evbot.candidates(g, src, NOW)[0], [])

    def test_a_stale_quote_is_the_bet(self):
        g, src = pair()                                         # Polymarket moved to 0.60; Kalshi still asks 0.55
        cands, fairs = evbot.candidates(g, src, NOW, moved=POLY_MOVED)
        self.assertEqual([(b["contract"].exchange, b["side"]) for b in cands], [("kalshi", YES)])
        b = cands[0]
        cost = 0.55 + fee_per_contract(0.07, 0.55)
        self.assertAlmostEqual(b["ev"], 0.60 - cost, 6)
        self.assertEqual(b["fv"]["source"], "Polymarket just moved")
        self.assertAlmostEqual(fairs[("KO45", "pm-o45")], 0.60, 6)

    def test_an_arb_is_left_to_the_arb_side(self):
        g, src = pair(p_no=0.40, p_yes=0.62)                    # 0.55 + 0.40 + fees < $1
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])

    def test_started_or_far_off_games_are_skipped(self):
        g, src = pair(start_in=0.05)                            # starts in 3 minutes
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])
        g, src = pair(decided_in=60)
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])

    def test_stale_quotes_are_never_trusted(self):
        g, src = pair()
        self.assertEqual(evbot.candidates(g, src, NOW, fresh=lambda ex, mid: ex != "kalshi", moved=POLY_MOVED)[0], [])

    def test_props_and_non_sports_are_left_alone(self):
        g, src = pair(var=("player", "pyd", "patrick mahomes"))
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])


class MathTests(unittest.TestCase):
    def test_limit_keeps_the_edge(self):
        lim = evbot.limit_for(0.60, 0.07, 0.55, 0.02, 0.04)
        self.assertEqual(lim, 0.55)                              # 0.56 would leave under 2c and 4%
        self.assertGreaterEqual(0.60 - lim - fee_per_contract(0.07, lim), 0.02)
        self.assertIsNone(evbot.limit_for(0.56, 0.07, 0.55, 0.02, 0.04))

    def test_kelly(self):
        # q=0.6, cost 0.5: Kelly bets (0.6-0.5)/(1-0.5) = 20% of the bankroll
        self.assertAlmostEqual(evbot.kelly_shares(0.6, 0.5, 100, 1.0), 40.0)
        self.assertAlmostEqual(evbot.kelly_shares(0.6, 0.5, 100, 0.25), 10.0)
        self.assertEqual(evbot.kelly_shares(0.5, 0.5, 100, 1.0), 0.0)


class Scanner:
    def __init__(self, trader=None):
        self.lock, self.state, self.logs, self.trader, self.alerter = threading.Lock(), {}, [], trader, None
        self.trading_status = "on"

    def log(self, m):
        self.logs.append(m)


def bot(trader=None, **cfg):
    path = Path(tempfile.mkdtemp()) / "ev_bets.json"
    b = evbot.EVBot(Scanner(trader), path=path, run_async=False)
    b._moved = POLY_MOVED
    b._fresh = lambda ex, mid: True
    return b


@mock.patch.object(config, "EV_BOT_PAPER", True)
class PaperBotTests(unittest.TestCase):
    def test_off_by_default_and_bets_once_on(self):
        b = bot()
        g, src = pair()
        self.assertIsNone(b.observe(g, src, NOW))
        b.set(True)
        self.assertIsNotNone(b.observe(g, src, NOW))
        bet = b.bets[0]
        self.assertEqual((bet["paper"], bet["exchange"], bet["side"], bet["status"]), (True, "kalshi", YES, "open"))
        # quarter Kelly on $200: q 0.60, cost ~0.567 -> ~$6.6 of stake, under the $10 cap
        cost = 0.55 + fee_per_contract(0.07, 0.55)
        want = int(0.25 * (0.60 - cost) / (1 - cost) * 200 / cost)
        self.assertEqual(bet["qty"], want)
        self.assertIsNone(b.observe(g, src, NOW))               # cooldown, and one open bet per game

    def test_caps_per_bet_and_per_day(self):
        b = bot()
        b.set(True)
        g, src = pair()
        with mock.patch.object(config, "EV_BOT_KELLY", 1.0), mock.patch.object(config, "EV_BOT_MAX_BET", 3.0):
            b.observe(g, src, NOW)
        self.assertLessEqual(b.bets[0]["cost"], 3.0)
        b2 = bot()
        b2.set(True)
        with mock.patch.object(config, "EV_BOT_DAILY_LIMIT", 0.5):
            self.assertIsNone(b2.observe(*pair(), NOW))

    def test_closing_value_is_the_last_fair_price_before_the_start(self):
        b = bot()
        b.set(True)
        g, src = pair()
        b.observe(g, src, NOW)
        bet = b.bets[0]
        b.on = False
        g2, src2 = pair(k_yes=0.63, k_no=0.39, p_yes=0.63, p_no=0.39)   # by kickoff both sites priced it 0.62
        b._moved = lambda k, p: None
        b.observe(g2, src2, NOW + timedelta(hours=2))
        self.assertIsNone(bet["clv"])
        b.observe(g2, src2, NOW + timedelta(hours=3, minutes=1))  # the game started
        self.assertAlmostEqual(bet["closing"], 0.62, 2)
        self.assertAlmostEqual(bet["clv"], round(0.62 - bet["cost"] / bet["qty"], 4), 2)
        self.assertGreater(b.status()["paper_results"]["clv_avg"], 0)

    def test_no_reading_after_the_bet_means_no_closing_value(self):
        b = bot()
        b.set(True)
        b.observe(*pair(), NOW)
        b.on = False
        b.observe(*pair(), NOW + timedelta(hours=3, minutes=1))   # first reading is after the start
        self.assertTrue(b.bets[0]["tracked"])
        self.assertIsNone(b.bets[0]["clv"])
        self.assertIsNone(b.observe(*pair(), NOW + timedelta(hours=4)))   # nothing left to track: no work

    def test_closing_value_is_read_right_up_to_the_start(self):
        b = bot()
        b.set(True)
        b.observe(*pair(), NOW)
        b.on = False
        b._moved = lambda k, p: None
        b.observe(*pair(k_yes=0.63, k_no=0.39, p_yes=0.63, p_no=0.39), NOW + timedelta(hours=2, minutes=58))
        b.observe(*pair(k_yes=0.63, k_no=0.39, p_yes=0.63, p_no=0.39), NOW + timedelta(hours=3, minutes=1))
        self.assertAlmostEqual(b.bets[0]["closing"], 0.62, 2)

    def test_results_are_settled_from_the_market(self):
        b = bot()
        b.set(True)
        b.observe(*pair(), NOW)
        bet = b.bets[0]
        b.scanner.kalshi = SimpleNamespace(markets_by_ticker=lambda ts: {"KO45": {"status": "finalized", "result": "yes",
                                                                                 "settlement_value_dollars": "1"}})
        b.scanner.pm = SimpleNamespace(markets_by_slug=lambda ss: {})
        self.assertEqual(b.settle(NOW + timedelta(hours=1), force=True), [])          # not over yet
        done = b.settle(NOW + timedelta(hours=7), force=True)
        self.assertEqual([x["status"] for x in done], ["won"])
        self.assertAlmostEqual(bet["pnl"], round(bet["qty"] - bet["cost"], 2), 2)
        s = b.status()["paper_results"]
        self.assertEqual((s["settled"], s["won"]), (1, 1))
        reloaded = evbot.EVBot(Scanner(), path=b.path, run_async=False)       # kept across restarts
        self.assertEqual(reloaded.bets[0]["status"], "won")


class FakeTrader:
    def __init__(self, venue):
        self.venues, self.lock = {"kalshi": venue, "polymarket": venue}, threading.Lock()

    def _cached_cash(self, ex, shard=None):
        return 500.0

    def _cached_info(self, ex, mid):
        return {"open": True, "min_qty": 1.0, "shard": 0, "tick": lambda p: 0.01}

    def _fetch_info(self, ex, v, mid):
        return self._cached_info(ex, mid)


@mock.patch.object(config, "EV_BOT_PAPER", False)
class LiveBotTests(unittest.TestCase):
    def test_places_an_ioc_order_at_the_edge_keeping_limit(self):
        orders = []
        venue = SimpleNamespace(buy=lambda mid, side, n, limit, coef, expect=None: orders.append((mid, side, n, limit))
                                or Fill(qty=n, amount=n * 0.55, fee=n * fee_per_contract(0.07, 0.55)),
                                balance=lambda shard=None: 500.0)
        b = bot(FakeTrader(venue))
        b.set(True)
        b.observe(*pair(), NOW)
        self.assertEqual(len(orders), 1)
        mid, side, n, limit = orders[0]
        self.assertEqual((mid, side, limit), ("KO45", YES, 0.55))
        self.assertFalse(b.bets[0]["paper"])

    def test_stops_after_repeated_refusals(self):
        def refuse(*a, **kw):
            raise ApiError(400, "insufficient balance")
        b = bot(FakeTrader(SimpleNamespace(buy=refuse, balance=lambda shard=None: 500.0)))
        b.set(True)
        for i in range(config.AUTO_TRADE_MAX_MISSES):
            b.tried.clear()
            b.observe(*pair(), NOW)
        self.assertFalse(b.on)
        self.assertIn("refused", b.halted)
        self.assertEqual(b.bets, [])

    def test_live_needs_trading_set_up(self):
        b = bot(None)
        with self.assertRaises(ValueError):
            b.set(True)


if __name__ == "__main__":
    unittest.main()
