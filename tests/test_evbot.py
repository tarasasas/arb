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

    def test_about_to_start_or_far_off_games_are_skipped(self):
        g, src = pair(start_in=0.05)                            # starts in 3 minutes
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])
        g, src = pair(decided_in=60)
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])

    def test_games_in_progress_need_the_extra_edge(self):
        g, src = pair(start_in=-0.5)                            # kicked off 30 min ago; 3.3c edge on the stale quote
        with mock.patch.object(config, "EV_BOT_LIVE_GAMES", False):
            self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])
        cands = evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0]
        self.assertEqual([(b["contract"].exchange, b["side"], b["live"]) for b in cands], [("kalshi", YES, True)])
        self.assertAlmostEqual(cands[0]["min_edge"], 0.03)
        with mock.patch.object(config, "EV_BOT_LIVE_EXTRA_EDGE", 0.02):     # 4c needed: 3.3c isn't enough live...
            self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])
            g2, src2 = pair()                                               # ...but is before the game
            self.assertEqual(len(evbot.candidates(g2, src2, NOW, moved=POLY_MOVED)[0]), 1)

    def test_live_quotes_must_be_seconds_old(self):
        ages = []
        g, src = pair(start_in=-0.5)
        evbot.candidates(g, src, NOW, fresh=lambda ex, mid, age=None: ages.append(age) or True, moved=POLY_MOVED)
        self.assertEqual(set(ages), {config.EV_BOT_LIVE_QUOTE_AGE})
        ages.clear()
        evbot.candidates(*pair(), NOW, fresh=lambda ex, mid, age=None: ages.append(age) or True, moved=POLY_MOVED)
        self.assertEqual(set(ages), {config.EV_BOT_MAX_QUOTE_AGE})

    def test_stale_quotes_are_never_trusted(self):
        g, src = pair()
        self.assertEqual(evbot.candidates(g, src, NOW, fresh=lambda ex, mid, age=None: ex != "kalshi", moved=POLY_MOVED)[0], [])

    def test_player_props_unless_turned_off(self):
        g, src = pair(var=("player", "pyd", "patrick mahomes"))
        self.assertEqual(len(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0]), 1)
        with mock.patch.object(config, "EV_BOT_PROPS", False):
            self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])

    def test_non_sports_are_left_alone(self):
        g, src = pair(var=("event", "x"))
        self.assertEqual(evbot.candidates(g, src, NOW, moved=POLY_MOVED)[0], [])

    def test_an_arbs_cheap_side_when_auto_trade_wont_take_it(self):
        g, src = pair(p_no=0.40, p_yes=0.62)                    # an arb: Kalshi YES 0.55 + Polymarket NO 0.40
        st = {}
        cands, _ = evbot.candidates(g, src, NOW, moved=POLY_MOVED, stats=st, arbs_ok=lambda live: True)
        self.assertEqual([(b["contract"].exchange, b["side"], b["arb"]) for b in cands], [("kalshi", YES, True)])
        self.assertEqual(st["arb taken"], 1)
        b = bot()
        b.scanner.autotrader = SimpleNamespace(on=False)
        with mock.patch.object(config, "EV_BOT_TAKE_ARBS", True):
            self.assertTrue(b._arbs_ok(False))                   # Auto-trade off
            b.scanner.autotrader.on = True
            self.assertFalse(b._arbs_ok(False))                  # Auto-trade takes it hedged
            with mock.patch.object(config, "AUTO_TRADE_LIVE_GAMES", False):
                self.assertTrue(b._arbs_ok(True))                # ...but skips games in progress
        self.assertFalse(b._arbs_ok(True))                       # setting off: never

    def test_the_ev_bot_has_its_own_stale_quote_window(self):
        from arb import execpolicy
        b = bot()
        del b._moved                                            # the real one
        seen = {}
        with mock.patch.object(execpolicy, "stale_side", lambda sc, legs, now=None, fresh_secs=None, gap_secs=None:
                               seen.update(fresh=fresh_secs, gap=gap_secs)), \
                mock.patch.object(config, "EV_BOT_STALE_FRESH_SECS", 5), mock.patch.object(config, "EV_BOT_STALE_GAP_SECS", 1):
            b._moved("KO45", "pm-o45")
        self.assertEqual(seen, {"fresh": 5, "gap": 1})


class WhyNoBetsTests(unittest.TestCase):
    def why(self, moved=lambda k, p: None, fresh=lambda ex, mid, age=None: True, **kw):
        g, src = pair(**kw)
        st = {}
        evbot.candidates(g, src, NOW, fresh=fresh, moved=moved, stats=st)
        return st

    def test_each_pair_is_counted_once_with_its_reason(self):
        self.assertEqual(self.why()["below edge"], 1)                        # tight books, consensus: no edge
        self.assertEqual(self.why(k_yes=0.58, k_no=0.48)["wide"], 1)        # Kalshi 6c wide
        self.assertEqual(self.why(p_yes=0.68, p_no=0.34)["apart"], 1)       # both 2c wide, mids 13c apart
        self.assertEqual(self.why(fresh=lambda ex, mid, age=None: ex != "kalshi")["not current"], 1)
        with mock.patch.object(config, "EV_BOT_LIVE_GAMES", False):
            self.assertEqual(self.why(start_in=-1)["started"], 1)
        self.assertEqual(self.why(start_in=-1)["live"], 1)                   # live games on: priced, not skipped
        self.assertEqual(self.why(start_in=0.05)["starting soon"], 1)
        self.assertEqual(self.why(p_no=0.40)["arb"], 1)
        st = self.why(moved=POLY_MOVED)
        self.assertEqual((st["qualified"], st["moves"]), (1, 1))

    def test_the_closest_miss_is_kept(self):
        best = self.why()["best"]                      # consensus 0.56: Polymarket NO at 0.42 is worth 0.44
        self.assertLess(best["closeness"], 1)
        self.assertEqual((best["exchange"], best["side"], best["source"]), ("Polymarket", NO, "consensus"))
        self.assertAlmostEqual(best["ev"], round(0.44 - 0.42 - fee_per_contract(0.0695, 0.42), 4), 4)
        self.assertGreaterEqual(self.why(moved=POLY_MOVED)["best"]["closeness"], 1)

    @mock.patch.object(config, "EV_BOT_PAPER", True)
    def test_bot_adds_it_up_since_turned_on_and_says_what_blocked_a_bet(self):
        b = bot()
        b.set(True)
        b._moved = lambda k, p: None
        for _ in range(3):
            b.observe(*pair(), NOW)
        w = b.status()["why"]
        self.assertEqual((w["passes"], w["checked"], w["counts"]["below edge"]), (3, 3, 3))
        self.assertIsNotNone(w["since"])
        b._moved = POLY_MOVED
        with mock.patch.object(config, "EV_BOT_DAILY_LIMIT", 0.5):
            b.observe(*pair(), NOW)
        self.assertEqual(b.status()["why"]["blocked"], {"daily limit reached": 1})
        b.set(False)
        b.set(True)                                                          # a fresh count each time it's turned on
        self.assertEqual(b.status()["why"]["checked"], 0)


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
    b._fresh = lambda ex, mid, age=None: True
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

    def test_bets_per_game(self):
        b = bot()
        b.set(True)
        g, src = pair()
        with mock.patch.object(config, "EV_BOT_COOLDOWN_SECS", 0):
            b.observe(g, src, NOW)
            self.assertIsNone(b.observe(g, src, NOW))                     # one per game by default
            self.assertEqual(b.status()["why"]["blocked"], {"already 1 bet on that game": 1})
            with mock.patch.object(config, "EV_BOT_PER_GAME", 3):
                b.observe(g, src, NOW)
                b.observe(g, src, NOW)
                self.assertIsNone(b.observe(g, src, NOW))
        self.assertEqual(len(b.bets), 3)

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

    def test_a_live_bet_is_scored_a_minute_later(self):
        b = bot()
        b.set(True)
        b.observe(*pair(start_in=-0.5), NOW)
        bet = b.bets[0]
        self.assertTrue(bet["live"])
        b.on = False
        b._moved = lambda k, p: None
        caught_up = dict(k_yes=0.61, k_no=0.41, p_yes=0.62, p_no=0.40, start_in=-0.5)   # Kalshi repriced to ~0.60
        b.observe(*pair(**caught_up), NOW + timedelta(seconds=30))
        self.assertIsNone(bet["clv"])                                       # not a minute yet
        b.observe(*pair(**caught_up), NOW + timedelta(seconds=61))
        self.assertTrue(bet["tracked"])
        self.assertAlmostEqual(bet["clv"], round(bet["closing"] - bet["cost"] / bet["qty"], 4), 4)
        s = b.status()["paper_results"]
        self.assertEqual((s["live_bets"], s["mark_n"], s["clv_n"]), (1, 1, 0))   # kept apart from closing value
        self.assertGreater(s["mark_avg"], 0)

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


# ---- dip trades: buy the scare, sell the bounce ------------------------------------------------------------------

def live_pair():
    g, src = pair(k_yes=0.55, k_no=0.47, p_yes=0.55, p_no=0.47, start_in=-0.5)      # kicked off 30 min ago
    return g, src, g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]


def asks(src, c, yes, no):
    c.ask = {YES: yes, NO: no}
    src[(c.exchange, c.market_id)].levels = {"yes": [(yes, 500)], "no": [(no, 500)]}


def panic(g, src, k, p, run, follow=False, frozen=False):
    """Kalshi's Over drops 10c (0.55 -> 0.45) while Polymarket keeps trading around 0.54; run(secs) after each
    change. follow: Polymarket drops with it. frozen: Polymarket's quotes never change."""
    run(0)
    if not frozen:
        asks(src, p, 0.56, 0.46)
    run(5)
    asks(src, k, 0.45, 0.57)
    if follow:
        asks(src, p, 0.46, 0.56)
    elif not frozen:
        asks(src, p, 0.55, 0.47)
    for secs in (20, 25, 31):
        run(secs)


class DipFindTests(unittest.TestCase):
    def found(self, **kw):
        g, src, k, p = live_pair()
        watch, seen = evbot.DipWatch(), []
        panic(g, src, k, p, lambda secs: seen.append(evbot.dip_candidates(g, src, NOW + timedelta(seconds=secs), watch)),
              **kw)
        return seen

    def test_a_panic_on_one_site_is_bought_there_once_it_holds(self):
        seen = self.found()
        self.assertEqual([len(x) for x in seen], [0, 0, 0, 0, 1])       # 20s and 25s: still confirming
        d = seen[-1][0]
        self.assertEqual((d["contract"].exchange, d["side"], d["ask"]), ("kalshi", YES, 0.45))
        self.assertAlmostEqual(d["q"], 0.54, 6)                          # Polymarket's mid
        self.assertEqual((d["dip"]["drop"], d["dip"]["anchor_ex"], d["dip"]["anchor_side"]), (0.10, "polymarket", YES))
        self.assertIn("Kalshi fell 10¢", d["fv"]["source"])

    def test_news_moves_both_sites_and_is_left_alone(self):
        self.assertEqual(self.found(follow=True)[-1], [])

    def test_a_frozen_other_site_proves_nothing(self):
        self.assertEqual(self.found(frozen=True)[-1], [])

    def test_too_small_a_drop_or_gap(self):
        with mock.patch.object(config, "EV_BOT_DIP_DROP", 0.12):
            self.assertEqual(self.found()[-1], [])
        with mock.patch.object(config, "EV_BOT_DIP_GAP", 0.08):
            self.assertEqual(self.found()[-1], [])

    def test_only_games_in_progress(self):
        g, src = pair(k_yes=0.55, k_no=0.47, p_yes=0.55, p_no=0.47)        # starts in 3 hours
        k, p = g[("NFL:x", TOTAL)]["kalshi"][0], g[("NFL:x", TOTAL)]["polymarket"][0]
        watch, seen = evbot.DipWatch(), []
        panic(g, src, k, p, lambda secs: seen.append(evbot.dip_candidates(g, src, NOW + timedelta(seconds=secs), watch)))
        self.assertEqual(seen[-1], [])

    def test_an_arb_goes_to_auto_trade_when_it_takes_games_in_progress(self):
        g, src, k, p = live_pair()
        watch, seen = evbot.DipWatch(), []
        panic(g, src, k, p, lambda secs: seen.append(evbot.dip_candidates(
            g, src, NOW + timedelta(seconds=secs), watch, arb_taken=lambda: True)))
        self.assertEqual(seen[-1], [])


class DipExitTests(unittest.TestCase):
    POS = {"qty": 10, "avg": 0.45, "cost": 10 * (0.45 + fee_per_contract(0.07, 0.45))}

    def exit(self, bid, anchor=0.54, held=30):
        return evbot.dip_exit(self.POS, bid, anchor, held, 0.07)

    def test_takes_the_profit_after_both_fees_at_the_lowest_price_that_makes_it(self):
        why, price = self.exit(0.53)
        self.assertIn("took the profit", why)
        self.assertEqual(price, 0.51)                     # 0.51 - fee clears 0.467 + 2c; 0.50 doesn't
        self.assertIsNone(self.exit(0.50))                # +1.5c after fees: not yet

    def test_cuts_it_when_it_keeps_falling_or_the_other_site_follows(self):
        self.assertIn("cut the loss", self.exit(0.40)[0])
        self.assertAlmostEqual(self.exit(0.40)[1], 0.37)
        self.assertIn("followed it down", self.exit(0.46, anchor=0.452)[0])

    def test_sells_at_the_best_price_after_the_time_limit(self):
        self.assertIn("no bounce in 10 min", self.exit(0.46, held=601)[0])
        self.assertIsNone(self.exit(0.46, held=599))


@mock.patch.object(config, "EV_BOT_PAPER", True)
class DipBotTests(unittest.TestCase):
    def buy(self, b=None):
        b = b or bot()
        b.set(True)
        b._moved = lambda k, p: None
        g, src, k, p = live_pair()
        panic(g, src, k, p, lambda secs: b.observe(g, src, NOW + timedelta(seconds=secs)))
        return b, g, src, k, p

    def test_buys_the_dip_and_sells_the_bounce(self):
        b, g, src, k, p = self.buy()
        self.assertEqual(len(b.bets), 1)
        d = b.bets[0]
        self.assertEqual((d["kind"], d["exchange"], d["side"], d["status"]), ("dip", "kalshi", YES, "open"))
        self.assertEqual(d["qty"], int(10 / (0.45 + fee_per_contract(0.07, 0.45))))      # $10 a dip trade
        b.observe(g, src, NOW + timedelta(seconds=45))                                   # nothing yet: hold
        self.assertEqual(d["status"], "open")
        asks(src, k, 0.53, 0.49)                                                         # bounced: bid 0.51
        b.observe(g, src, NOW + timedelta(seconds=60))
        self.assertEqual((d["status"], d["sold_qty"], d["exits"][0]["avg"]), ("sold", d["qty"], 0.51))
        self.assertAlmostEqual(d["pnl"], round(d["sold_amount"] - d["sold_fee"] - d["cost"], 2))
        self.assertGreater(d["pnl"], 0)
        r = b.status()["paper_results"]
        self.assertEqual((r["dips"]["closed"], r["dips"]["up"], r["bets"]), (1, 1, 0))  # apart from value bets
        self.assertIn("took the profit", b.status()["history"][0]["exit"])

    def test_keeps_selling_with_the_bot_off(self):
        b, g, src, k, p = self.buy()
        b.set(False)
        asks(src, k, 0.38, 0.64)                                                         # kept falling
        b.observe(g, src, NOW + timedelta(seconds=40))
        d = b.bets[0]
        self.assertEqual(d["status"], "sold")
        self.assertIn("cut the loss", d["exits"][0]["why"])
        self.assertLess(d["pnl"], 0)

    def test_a_loss_pauses_dip_trades_in_that_game(self):
        b, g, src, k, p = self.buy()
        asks(src, k, 0.38, 0.64)
        b.observe(g, src, NOW + timedelta(seconds=40))                                   # cut at a loss
        k2 = Contract("kalshi", "KO47", "NFL:x", TOTAL, ">", 47.5, "Over 47.5", fee_coef=0.07, close_time=k.close_time)
        p2 = Contract("polymarket", "pm-o47", "NFL:x", TOTAL, ">", 47.5, "O/U 47.5", fee_coef=0.0695,
                      close_time=p.close_time)
        for c in (k2, p2):
            src[(c.exchange, c.market_id)] = SimpleNamespace(levels={})
            asks(src, c, 0.55, 0.47)
        g2 = {("NFL:x", TOTAL): {"kalshi": [k2], "polymarket": [p2]}}                     # another line, same game
        panic(g2, src, k2, p2, lambda secs: b.observe(g2, src, NOW + timedelta(seconds=50 + secs)))
        self.assertEqual(len(b.bets), 1)
        self.assertIn("cut at a loss in that game", " ".join(b.status()["why"]["blocked"]))

    def test_the_other_site_following_ends_it(self):
        b, g, src, k, p = self.buy()
        asks(src, p, 0.46, 0.56)
        b.observe(g, src, NOW + timedelta(seconds=40))
        self.assertIn("followed it down", b.bets[0]["exits"][0]["why"])

    def test_a_sale_cut_short_by_a_restart_is_tried_again(self):
        b, g, src, k, p = self.buy()
        b.bets[0]["exiting"] = True
        b._save()
        reloaded = evbot.EVBot(Scanner(), path=b.path, run_async=False)
        reloaded._fresh = lambda ex, mid, age=None: True
        asks(src, k, 0.53, 0.49)
        reloaded.observe(g, src, NOW + timedelta(seconds=60))
        self.assertEqual(reloaded.bets[0]["status"], "sold")

    def test_a_dip_seen_long_ago_needs_confirming_again(self):
        w = evbot.DipWatch()
        self.assertEqual(w.held("x", 0, True), 0)
        self.assertEqual(w.held("x", 12, True), 12)
        self.assertEqual(w.held("x", 12 + config.EV_BOT_DIP_WINDOW_SECS + 1, True), 0)

    def test_off_in_settings_means_no_dip_trades(self):
        with mock.patch.object(config, "EV_BOT_DIPS", False):
            b, *_ = self.buy()
        self.assertEqual(b.bets, [])

    def test_a_game_ending_first_settles_what_is_left(self):
        b, g, src, k, p = self.buy()
        d = b.bets[0]
        src[("kalshi", "KO45")].levels = {"yes": [(0.53, 500)], "no": [(0.49, 5)]}       # only 5 to sell into
        asks(src, k, 0.53, 0.49)
        src[("kalshi", "KO45")].levels["no"] = [(0.49, 5)]
        b.observe(g, src, NOW + timedelta(seconds=60))
        self.assertEqual((d["status"], d["sold_qty"]), ("open", 5))
        b.scanner.kalshi = SimpleNamespace(markets_by_ticker=lambda ts: {"KO45": {"status": "finalized", "result": "yes",
                                                                                 "settlement_value_dollars": "1"}})
        b.scanner.pm = SimpleNamespace(markets_by_slug=lambda ss: {})
        b.settle(NOW + timedelta(hours=7), force=True)
        left = d["qty"] - 5
        self.assertEqual(d["status"], "won")
        self.assertAlmostEqual(d["pnl"], round(left + d["sold_amount"] - d["sold_fee"] - d["cost"], 2), 2)


@mock.patch.object(config, "EV_BOT_PAPER", False)
class LiveDipTests(unittest.TestCase):
    def test_a_real_dip_trade_sells_with_an_ioc_order(self):
        orders = []

        def buy(mid, side, n, limit, coef, expect=None):
            orders.append(("buy", side, n, limit))
            return Fill(qty=n, amount=n * 0.45, fee=0.37, order_id="b1")

        def sell(mid, side, n, min_price, coef, expect=None):
            orders.append(("sell", side, n, min_price))
            return Fill(qty=n, amount=n * 0.52, fee=0.37, order_id="s1")
        b = bot(FakeTrader(SimpleNamespace(buy=buy, sell=sell, balance=lambda shard=None: 500.0)))
        b, g, src, k, p = DipBotTests.buy(None, b)
        asks(src, k, 0.54, 0.48)
        b.observe(g, src, NOW + timedelta(seconds=60))
        self.assertEqual([o[0] for o in orders], ["buy", "sell"])
        self.assertEqual(orders[1][:3], ("sell", YES, orders[0][2]))
        self.assertEqual(orders[1][3], 0.51)                                             # the lowest price that clears it
        self.assertEqual((b.bets[0]["status"], b.bets[0]["paper"]), ("sold", False))
