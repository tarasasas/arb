"""EV bot: single bets that cost less than they're worth at the price both exchanges agree on.

Unlike an arb, a bet here is NOT hedged: each one can lose. The bet is +EV only if the fair price is right.

Fair price: where Kalshi and Polymarket list the exact same question (same quantity, same line, no push),
each book's mid (between its YES ask and 1 - its NO ask) is an estimate of the YES probability.
  - One site just repriced and the other hasn't (the live feeds show it, as for Auto-trade's leg order):
    the fresh site's mid. The stale quote is the bet: it's about to move the same way.
  - Otherwise the two mids weighted by how tight each book is (consensus). With both books tight this
    almost never leaves a bet: a gap that big is already an arb, and arbs are left to the arb side.
Books wider than EV_BOT_MAX_SPREAD, or mids further apart than EV_BOT_MAX_DISAGREE (a wrong match, or a
move too big to call), give no fair price.

A bet: one side of one of the two markets whose price + taker fee is at least EV_BOT_MIN_EDGE below
that side's fair price (and EV_BOT_MIN_ROI of what it costs). A pair that's an arb is left to the arb
side (hedged is better). Sports games whose result is known within EV_BOT_MAX_HOURS; games in progress too
(EV_BOT_LIVE_GAMES) with EV_BOT_LIVE_EXTRA_EDGE more edge and quotes under EV_BOT_LIVE_QUOTE_AGE old.

Size: EV_BOT_KELLY x the Kelly stake for that edge on EV_BOT_BANKROLL, at most EV_BOT_MAX_BET a bet and
EV_BOT_DAILY_LIMIT a day, EV_BOT_MAX_OPEN open bets, one open bet per game; never more than the book
shows within the edge.

How it judges itself: every bet keeps the last fair price before its game started (closing value: the
standard early test of whether bets have an edge, known hours before the results) and, once its market
settles, what it really paid. Paper mode (EV_BOT_PAPER, on by default) does everything but send orders,
filling against the real books. Off every time the scanner starts.
"""

import json
import math
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import config, engine
from .http import ApiError, trading
from .model import NO, YES, fee_per_contract, total_fee
from .venues import floor_to

SIDES = (YES, NO)
GAME_VARS = ("margin", "total", "tt", "btts")        # game quantities; props settle oddly if a player sits out
NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket"}


def quote(c):
    """(mid, spread) of a contract's YES from its two asks, or None."""
    y, n = c.ask.get(YES), c.ask.get(NO)
    if y is None or n is None or not (0 < y < 1 and 0 < n < 1):
        return None
    return (y + 1 - n) / 2, max(y + n - 1, 0.0)


def relation(k, p):
    """+1 when p's YES is the same question as k's YES, -1 when it's k's NO, 0 otherwise (different lines,
    a possible push). Cached: it depends only on terms."""
    cache = getattr(k, "pay_cache", None)
    if cache is None:
        cache = k.pay_cache = {}
    key = ("rel", p.market_id, p.var, p.op, p.line)
    if key not in cache:
        cache[key] = 1 if engine.exact_hedge(k, YES, p, NO) else -1 if engine.exact_hedge(k, YES, p, YES) else 0
    return cache[key]


def fair_value(k, p, rel, fresh=None):
    """The fair YES probability of k ({"fair", "source", "k_mid", "p_mid", ...}) or None. fresh: the site that
    just repriced while the other didn't ("kalshi" / "polymarket"), whose mid is then the fair price."""
    qk, qp = quote(k), quote(p)
    if not qk or not qp:
        return None
    (mk, sk), (mp, sp) = qk, qp
    if rel < 0:
        mp = 1 - mp
    if max(sk, sp) > config.EV_BOT_MAX_SPREAD + 1e-9 or abs(mk - mp) > config.EV_BOT_MAX_DISAGREE + 1e-9:
        return None
    if fresh in ("kalshi", "polymarket"):
        fair, source = (mk, "Kalshi just moved") if fresh == "kalshi" else (mp, "Polymarket just moved")
    else:
        wk, wp = 1 / max(sk, 0.005), 1 / max(sp, 0.005)
        fair, source = (mk * wk + mp * wp) / (wk + wp), "consensus"
    return {"fair": fair, "source": source, "k_mid": mk, "p_mid": mp, "k_spread": sk, "p_spread": sp}


def _hedged_arb(k, p, rel):
    """True if the pair is an arb as it stands (k's side + the opposite side of p for under $1)."""
    for sk in SIDES:
        sp = (NO if sk == YES else YES) if rel > 0 else sk
        ak, ap = k.ask.get(sk), p.ask.get(sp)
        if ak and ap and 0 < ak < 1 and 0 < ap < 1 and \
                1 - ak - ap - fee_per_contract(k.fee_coef, ak) - fee_per_contract(p.fee_coef, ap) > 0:
            return True
    return False


def limit_for(q, coef, ask, min_edge, min_roi):
    """Highest whole-cent price at or above the ask where one share is still worth the edge; None if none."""
    best, p = None, ask
    while p < 1:
        cost = p + fee_per_contract(coef, p)
        if q - cost < min_edge - 1e-12 or (q - cost) / cost < min_roi - 1e-12:
            break
        best = round(p, 4)
        p = round(math.floor(p * 100 + 1e-6) / 100 + 0.01, 4)
    return best


def kelly_shares(q, cost, bankroll, fraction):
    """Shares for a fraction of the Kelly stake on a $1-payout contract won with probability q."""
    if cost <= 0 or cost >= 1 or q <= cost:
        return 0.0
    return fraction * (q - cost) / (1 - cost) * bankroll / cost


def _no_price_reason(k, p, rel):
    """Why a same-question pair has no fair price ("no quote" / "wide" / "apart"), or None if it has one."""
    qk, qp = quote(k), quote(p)
    if not qk or not qp:
        return "no quote"
    mp = qp[0] if rel > 0 else 1 - qp[0]
    if max(qk[1], qp[1]) > config.EV_BOT_MAX_SPREAD + 1e-9:
        return "wide"
    if abs(qk[0] - mp) > config.EV_BOT_MAX_DISAGREE + 1e-9:
        return "apart"
    return None


def _closeness(ev, cost, min_edge=None):
    """How close a bet is to qualifying: 1 = exactly at both minimums (edge and return), below 1 = short."""
    min_edge = config.EV_BOT_MIN_EDGE if min_edge is None else min_edge
    return min(ev / min_edge if min_edge > 0 else math.inf,
               (ev / cost) / config.EV_BOT_MIN_ROI if config.EV_BOT_MIN_ROI > 0 else math.inf)


def candidates(groups, source, now, fresh=lambda ex, mid, age=None: True, moved=lambda k, p: None, stats=None):
    """(bets worth taking now, best edge first; {(k id, p id): fair YES of k} for every pair priced).
    fresh(exchange, market id, max age in seconds): whether that market's quote is current. moved(k id, p id):
    the site that just repriced while the other didn't, or None. stats: a dict that gets, per same-question
    pair in the window, the reason it was passed over (or "qualified"), "moves" (stale-quote moments), "live"
    (pairs whose game is in progress) and "best" (the bet that came closest to qualifying)."""
    out, fairs = [], {}
    st = stats if stats is not None else {}
    count = lambda why: st.__setitem__(why, st.get(why, 0) + 1)
    horizon = now + timedelta(hours=config.EV_BOT_MAX_HOURS)
    lead = now + timedelta(seconds=config.EV_BOT_MIN_LEAD_SECS)
    for (_gk, var), g in groups.items():
        if var[0] not in GAME_VARS:
            continue
        for k in g.get("kalshi") or []:
            if engine._ended(k, now):
                continue
            decided = engine._parse_time(k.close_time)
            if decided is None or decided > horizon:
                continue
            for p in g.get("polymarket") or []:
                rel = relation(k, p)
                if not rel:
                    continue
                start = engine._parse_time(p.close_time)           # a Polymarket game's date is its start
                if start is None:
                    count("no start time")
                    continue
                live = start <= now
                if live and not config.EV_BOT_LIVE_GAMES:
                    count("started")                              # in play, and live games are off
                    continue
                age = config.EV_BOT_LIVE_QUOTE_AGE if live else config.EV_BOT_MAX_QUOTE_AGE
                if not (fresh("kalshi", k.market_id, age) and fresh("polymarket", p.market_id, age)):
                    count("not current")
                    continue
                why = _no_price_reason(k, p, rel)
                if why:
                    count(why)
                    continue
                fv = fair_value(k, p, rel, moved(k.market_id, p.market_id))
                fairs[(k.market_id, p.market_id)] = fv["fair"]       # for closing value and live marks
                if not live and start <= lead:
                    count("starting soon")
                    continue
                if _hedged_arb(k, p, rel):
                    count("arb")                                  # take it hedged instead
                    continue
                if fv["source"] != "consensus":
                    count("moves")
                if live:
                    count("live")
                min_edge = config.EV_BOT_MIN_EDGE + (config.EV_BOT_LIVE_EXTRA_EDGE if live else 0.0)
                found = False
                for c, yes_q in ((k, fv["fair"]), (p, fv["fair"] if rel > 0 else 1 - fv["fair"])):
                    if fv["source"] != "consensus" and fv["source"].startswith(NAMES[c.exchange]):
                        continue                                  # the site that moved is the price, not the bet
                    for side in SIDES:
                        a = c.ask.get(side)
                        if a is None or not 0 < a < 1:
                            continue
                        q = yes_q if side == YES else 1 - yes_q
                        cost = a + fee_per_contract(c.fee_coef, a)
                        ev = q - cost
                        close = _closeness(ev, cost, min_edge)
                        if st.get("best") is None or close > st["best"]["closeness"]:
                            st["best"] = {"closeness": round(close, 3), "ev": round(ev, 4), "roi": round(ev / cost, 4),
                                          "game": k.game_label or k.game_key.split(":", 1)[1],
                                          "quantity": engine.describe_var(k.var), "exchange": NAMES[c.exchange],
                                          "side": side, "ask": a, "fair": round(q, 4), "source": fv["source"],
                                          "live": live, "min_edge": round(min_edge, 4),
                                          "time": now.isoformat(timespec="seconds")}
                        if close < 1 - 1e-9:
                            continue
                        found = True
                        m = source.get((c.exchange, c.market_id))
                        out.append({"contract": c, "side": side, "ask": a, "q": q, "ev": ev, "cost": cost,
                                    "k": k.market_id, "p": p.market_id, "rel": rel, "fv": fv,
                                    "game": k.game_key, "label": k.game_label or k.game_key.split(":", 1)[1],
                                    "quantity": engine.describe_var(k.var), "start": start.isoformat(),
                                    "decided": decided.isoformat(), "market": m, "live": live, "min_edge": min_edge,
                                    "levels": list(((getattr(m, "levels", None) or {}).get(side)) or [])
                                    or ([(a, getattr(m, f"{side}_ask_size", None))]
                                        if getattr(m, f"{side}_ask_size", None) else [])})
                count("qualified" if found else "below edge")
    out.sort(key=lambda b: -b["ev"])
    return out, fairs


class EVBot:
    def __init__(self, scanner, path=None, run_async=True):
        self.scanner, self.path = scanner, path
        self.on, self.busy, self.halted = False, False, None
        self.lock = threading.Lock()
        self.bets = self._load()
        self.tried = {}                    # market id -> time of the last bet attempt
        self.rejects = 0                   # orders refused in a row
        self.top = []                      # the best bets seen on the last pass, for the dashboard
        self._reset_why()
        self._last_settle = 0.0
        self._worker = ThreadPoolExecutor(1, thread_name_prefix="ev-bot") if run_async else None

    # ---- storage ------------------------------------------------------------------------------

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) if self.path else []
        except (OSError, ValueError):
            return []

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.bets[-2000:], default=str), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- switch and state ---------------------------------------------------------------------

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    def _reset_why(self):
        """'Why no bets?': since it was turned on, why each same-question pair was passed over, the bet that came
        closest to qualifying, and why bets that did qualify weren't placed."""
        self.funnel, self.blocked, self.best = {}, {}, None
        self.why_since, self.passes = None, 0

    def _block(self, why):
        self.blocked[why] = self.blocked.get(why, 0) + 1

    def set(self, on):
        t = getattr(self.scanner, "trader", None)
        if on and not config.EV_BOT_PAPER and not (t and t.venues):
            raise ValueError(f"Trading is {getattr(self.scanner, 'trading_status', 'off')}; the EV bot can still "
                             f"paper trade (EV_BOT_PAPER=1).")
        with self.lock:
            if on and not self.on:
                self._reset_why()
                self.why_since = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.on, self.halted = bool(on), None
            self.rejects = 0
        self.scanner.log(f"EV bot turned {'on' if on else 'off'}" + (
            f" ({'PAPER: no real orders' if config.EV_BOT_PAPER else 'LIVE: real orders'}; up to "
            f"${config.EV_BOT_MAX_BET:g} a bet, ${config.EV_BOT_DAILY_LIMIT:g} a day)" if on else ""))
        return self.status()

    def spent_today(self, paper=None):
        day = self._today()
        return round(sum(b["cost"] for b in self.bets if b["day"] == day
                         and (paper is None or b["paper"] == paper)), 2)

    def open_bets(self, paper=None):
        return [b for b in self.bets if b["status"] == "open" and (paper is None or b["paper"] == paper)]

    def summary(self, paper):
        """Settled results and closing value for real (paper=False) or paper bets."""
        mine = [b for b in self.bets if b["paper"] == paper]
        settled = [b for b in mine if b["status"] in ("won", "lost", "void")]
        staked = sum(b["cost"] for b in settled)
        pnl = sum(b["pnl"] for b in settled)
        clv = [b["clv"] for b in mine if b.get("clv") is not None and not b.get("live")]
        mark = [b["clv"] for b in mine if b.get("clv") is not None and b.get("live")]
        opened = [b for b in mine if b["status"] == "open"]
        return {"bets": len(mine), "open": len(opened), "open_staked": round(sum(b["cost"] for b in opened), 2),
                "open_ev": round(sum(b["ev"] * b["qty"] for b in opened), 2),
                "settled": len(settled), "won": sum(b["status"] == "won" for b in settled),
                "lost": sum(b["status"] == "lost" for b in settled), "pnl": round(pnl, 2),
                "roi": round(pnl / staked, 4) if staked else None,
                "expected": round(sum(b["ev"] * b["qty"] for b in settled), 2),
                "clv_n": len(clv), "clv_avg": round(sum(clv) / len(clv), 4) if clv else None,
                "clv_positive": round(sum(x > 0 for x in clv) / len(clv), 3) if clv else None,
                "live_bets": sum(bool(b.get("live")) for b in mine),
                "mark_n": len(mark), "mark_avg": round(sum(mark) / len(mark), 4) if mark else None,
                "mark_positive": round(sum(x > 0 for x in mark) / len(mark), 3) if mark else None}

    def status(self):
        return {"on": self.on, "busy": self.busy, "halted": self.halted, "paper": config.EV_BOT_PAPER,
                "spent_today": self.spent_today(config.EV_BOT_PAPER), "daily_limit": config.EV_BOT_DAILY_LIMIT,
                "max_bet": config.EV_BOT_MAX_BET, "min_edge": config.EV_BOT_MIN_EDGE, "min_roi": config.EV_BOT_MIN_ROI,
                "kelly": config.EV_BOT_KELLY, "bankroll": config.EV_BOT_BANKROLL, "max_open": config.EV_BOT_MAX_OPEN,
                "max_hours": config.EV_BOT_MAX_HOURS, "live_games": config.EV_BOT_LIVE_GAMES,
                "live_extra_edge": config.EV_BOT_LIVE_EXTRA_EDGE, "live_mark_secs": config.EV_BOT_LIVE_MARK_SECS,
                "real": self.summary(False), "paper_results": self.summary(True),
                "history": [{k: b.get(k) for k in ("time", "paper", "game", "quantity", "exchange", "side", "title",
                                                   "qty", "avg", "cost", "fair", "fair_source", "ev", "status", "pnl", "clv",
                                                   "live")}
                            for b in reversed(self.bets[-20:])],
                "top": self.top if self.on else [],
                "why": {"since": self.why_since, "passes": self.passes, "counts": dict(self.funnel),
                        "checked": sum(v for k, v in self.funnel.items() if k not in ("moves", "live")), "best": self.best,
                        "blocked": dict(self.blocked)}}

    # ---- every price pass -------------------------------------------------------------------------

    def _fresh(self, ex, mid, max_age=None):
        """A market's quote is current: its live feed is alive and holds it, or it was read within max_age
        seconds (EV_BOT_MAX_QUOTE_AGE if not given)."""
        s = (getattr(self.scanner, "streams", None) or {}).get(ex)
        now = time.time()
        if s and getattr(s, "connected", False) and now - (getattr(s, "last_msg", 0) or 0) <= config.LIVE_FEED_ALIVE_SECS \
                and mid in getattr(s, "seen", ()):
            return True
        m = (getattr(self.scanner, "source", None) or {}).get((ex, mid))
        age = config.EV_BOT_MAX_QUOTE_AGE if max_age is None else max_age
        return bool(m and now - (getattr(m, "quoted_at", 0) or 0) <= age)

    def _moved(self, k, p):
        """The site that just repriced while the other hasn't (execpolicy.stale_side), or None."""
        from .execpolicy import stale_side
        stale = stale_side(self.scanner, [{"exchange": "kalshi", "market_id": k}, {"exchange": "polymarket", "market_id": p}])
        return None if stale is None else "polymarket" if stale == "kalshi" else "kalshi"

    def observe(self, groups, source, now=None):
        """After a price pass: keep each open bet's latest pre-game fair price (its closing value once the game
        starts), and when on, place the best bet worth taking. Returns the bet started, if any."""
        if not self.on:                    # off: only the games it still holds bets in, for their closing value
            games = {b["game_key"] for b in self.bets if b["status"] == "open" and not b.get("tracked")}
            if not games:
                return None
            groups = {g: v for g, v in groups.items() if g[0] in games}
        now = now or engine.now_utc()
        stats = {}
        cands, fairs = candidates(groups, source, now, self._fresh, self._moved, stats)
        self._track_closing(fairs, now)
        if self.on:
            with self.lock:
                self.passes += 1
                best = stats.pop("best", None)
                for why, n in stats.items():
                    self.funnel[why] = self.funnel.get(why, 0) + n
                if best and (self.best is None or best["closeness"] > self.best["closeness"]):
                    self.best = best
        self.top = [{"game": b["label"], "quantity": b["quantity"], "exchange": NAMES[b["contract"].exchange],
                     "side": b["side"], "title": b["contract"].title, "ask": b["ask"], "fair": round(b["q"], 4),
                     "source": b["fv"]["source"], "ev": round(b["ev"], 4)} for b in cands[:5]]
        if not self.on:
            return None
        with self.lock:
            if self.halted or self.busy:
                return None
            pick = self._pick(cands, now)
            if pick is None:
                return None
            self.busy = True
            self.tried[pick["contract"].market_id] = time.time()
            pick["now"] = now
        if self._worker:
            self._worker.submit(self._run, pick)
        else:
            self._run(pick)
        return pick

    def _pick(self, cands, now):
        if not cands:
            return None
        opened = self.open_bets(config.EV_BOT_PAPER)
        if len(opened) >= config.EV_BOT_MAX_OPEN:
            self._block("open-bet limit reached")
            return None
        if config.EV_BOT_DAILY_LIMIT - self.spent_today(config.EV_BOT_PAPER) < 1:
            self._block("daily limit reached")
            return None
        busy_games = {b["game_key"] for b in opened}
        t = time.time()
        for b in cands:
            if b["game"] in busy_games:
                self._block("already a bet on that game")
                continue
            if t - self.tried.get(b["contract"].market_id, 0) < config.EV_BOT_COOLDOWN_SECS:
                self._block("tried that market in the last 10 min")
                continue
            return b
        return None

    def _track_closing(self, fairs, now):
        """Each open bet's latest fair price after it was placed; once its game starts, that's its closing value
        (none if no reading came in between: the fair price at the bet itself would only repeat its edge)."""
        with self.lock:                    # passes run on several threads at once
            self._track(fairs, now)

    def _track(self, fairs, now):
        changed = False
        for b in self.bets:
            if b["status"] != "open" or b.get("tracked"):
                continue
            start = engine._parse_time(b.get("start") or "")
            f = fairs.get((b["k"], b["p"]))
            if b.get("live"):
                # placed during the game: its score is the fair price a minute later (gave up after five)
                placed = engine._parse_time(b.get("placed") or "")
                if placed is None or now < placed + timedelta(seconds=config.EV_BOT_LIVE_MARK_SECS):
                    continue
                if f is not None:
                    yes = f if b["yes_is_k"] else 1 - f
                    q = yes if b["side"] == YES else 1 - yes
                    b["closing"], b["clv"] = round(q, 4), round(q - b["cost"] / b["qty"], 4)
                    b["tracked"] = changed = True
                elif now >= placed + timedelta(seconds=5 * config.EV_BOT_LIVE_MARK_SECS):
                    b["tracked"] = changed = True
                continue
            if f is not None and (start is None or now < start):    # (this pass's bet is placed after this)
                b["last_fair"] = f if b["yes_is_k"] else 1 - f
                b["last_fair_at"] = now.isoformat()
            if start and start <= now:
                b["tracked"] = changed = True
                if b.get("last_fair") is not None:
                    q = b["last_fair"] if b["side"] == YES else 1 - b["last_fair"]
                    b["closing"] = round(q, 4)
                    b["clv"] = round(q - b["cost"] / b["qty"], 4)      # closing fair - what a share cost
        if changed:
            self._save()

    # ---- placing a bet ------------------------------------------------------------------------

    def _size(self, b, bankroll):
        c = b["contract"]
        limit = limit_for(b["q"], c.fee_coef, b["ask"], b.get("min_edge", config.EV_BOT_MIN_EDGE), config.EV_BOT_MIN_ROI)
        if limit is None:
            return 0, None, []
        stake_cap = min(config.EV_BOT_MAX_BET, config.EV_BOT_DAILY_LIMIT - self.spent_today(config.EV_BOT_PAPER))
        want = min(kelly_shares(b["q"], b["cost"], bankroll, config.EV_BOT_KELLY), stake_cap / b["cost"])
        usable = [(p, q) for p, q in b["levels"] if q and p <= limit + 1e-9]
        n = math.floor(min(want, sum(q for _, q in usable)) + 1e-9)
        fills = engine._take(usable, n)
        while n > 0 and sum(p * q for p, q in fills) + total_fee(c.exchange, fills, c.fee_coef) > stake_cap + 1e-9:
            n -= 1                         # deeper levels cost more than the top: stay within the caps
            fills = engine._take(usable, n)
        return n, limit, fills

    def _run(self, b):
        c, side = b["contract"], b["side"]
        entry = None
        try:
            t = getattr(self.scanner, "trader", None)
            cash = None
            if t is not None and t.venues and not config.EV_BOT_PAPER:
                cash = t._cached_cash(c.exchange, getattr(b["market"], "shard", 0) if c.exchange == "kalshi" else None)
            bankroll = min(config.EV_BOT_BANKROLL, cash) if cash is not None else config.EV_BOT_BANKROLL
            n, limit, fills = self._size(b, bankroll)
            if n < 1 or sum(p * q for p, q in fills) < 1:
                self._block("under $1 at the edge (thin book, or a small Kelly stake)")
                return
            if config.EV_BOT_PAPER:
                qty, amount = sum(q for _, q in fills), sum(p * q for p, q in fills)
                fee, order_id = total_fee(c.exchange, fills, c.fee_coef), "paper"
            else:
                got = self._place(t, c, side, n, limit, b)
                if got is None:
                    return
                qty, amount, fee, order_id = got.qty, got.amount, got.fee, got.order_id
                if qty <= 0:
                    self._block("order didn't fill (the price moved)")
                    self.scanner.log(f"EV bot: {NAMES[c.exchange]} {side.upper()} {c.market_id} didn't fill at ≤ ${limit:.2f}")
                    return
            entry = self._record(b, qty, amount, fee, limit, order_id)
            self.scanner.log(f"EV bot {'paper ' if entry['paper'] else ''}bet: {entry['game']} · {NAMES[c.exchange]} "
                             f"{side.upper()} {qty:g} @ {entry['avg']:.3f} (fair {entry['fair']:.3f}, "
                             f"+${entry['ev'] * qty:.2f} expected)")
        except Exception as e:
            self._halt(f"unexpected error ({e!r}): check your {NAMES.get(c.exchange, '')} account")
        finally:
            self.busy = False

    def _place(self, t, c, side, n, limit, b):
        """A real IOC order. Returns its Fill, or None when nothing was sent / it was refused."""
        if not t.lock.acquire(blocking=False):
            self._block("an arb trade was going out")
            return None                     # an arb trade is going out: it goes first
        try:
            with trading():
                v = t.venues[c.exchange]
                info = t._cached_info(c.exchange, c.market_id) or t._fetch_info(c.exchange, v, c.market_id)
                if not info.get("open"):
                    self._block("market not open")
                    return None
                shard = info.get("shard") if c.exchange == "kalshi" else None
                cash = t._cached_cash(c.exchange, shard)
                cash = v.balance(shard) if cash is None else cash
                n = floor_to(min(n, cash / (limit + fee_per_contract(c.fee_coef, limit))), info["min_qty"])
                if n < info["min_qty"]:
                    self._block("not enough cash on that site")
                    return None
                try:
                    fill = v.buy(c.market_id, side, n, limit, c.fee_coef, expect=b["ask"])
                except ApiError as e:
                    self._block("order refused")
                    self.rejects += 1
                    self.scanner.log(f"EV bot: {NAMES[c.exchange]} refused the order ({e.detail})")
                    if self.rejects >= config.AUTO_TRADE_MAX_MISSES:
                        self._halt(f"{NAMES[c.exchange]} refused {self.rejects} orders in a row (last: {e.detail})")
                    return None
                self.rejects = 0
                with self.scanner.lock:            # cash changed
                    if self.scanner.state.get("balances"):
                        self.scanner.state["balances"] = {**self.scanner.state["balances"], "stale": True}
                return fill
        finally:
            t.lock.release()

    def _record(self, b, qty, amount, fee, limit, order_id):
        c = b["contract"]
        entry = {"id": uuid.uuid4().hex, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "day": self._today(), "paper": config.EV_BOT_PAPER, "game": b["label"], "game_key": b["game"],
                 "quantity": b["quantity"], "exchange": c.exchange, "market_id": c.market_id, "title": c.title,
                 "side": b["side"], "qty": qty, "amount": round(amount, 4), "fee": round(fee, 4),
                 "cost": round(amount + fee, 4), "avg": round(amount / qty, 4), "limit": limit, "order_id": order_id,
                 "fair": round(b["q"], 4), "fair_source": b["fv"]["source"], "ev": round(b["q"] - (amount + fee) / qty, 4),
                 "k": b["k"], "p": b["p"], "yes_is_k": c.exchange == "kalshi" or b["rel"] > 0,
                 "mids": {"kalshi": round(b["fv"]["k_mid"], 4), "polymarket": round(b["fv"]["p_mid"], 4)},
                 "start": b["start"], "decided": b["decided"], "status": "open", "pnl": None, "clv": None,
                 "live": bool(b.get("live")), "placed": (b.get("now") or engine.now_utc()).isoformat()}
        with self.lock:
            self.bets.append(entry)
            self._save()
        return entry

    def _halt(self, why):
        with self.lock:
            self.on, self.halted = False, why
        self.scanner.log(f"EV bot stopped: {why}")
        alerter = getattr(self.scanner, "alerter", None)
        if alerter and alerter.enabled:
            threading.Thread(target=alerter._safe_send, args=(f"EV bot stopped: {why}",), daemon=True).start()

    # ---- results --------------------------------------------------------------------------------

    def settle(self, now=None, force=False):
        """Read the result of every open bet whose game should be over (at most every EV_BOT_SETTLE_SECS).
        Returns the bets settled now."""
        from .myarbs import kalshi_status, polymarket_status
        t = time.time()
        if not force and t - self._last_settle < config.EV_BOT_SETTLE_SECS:
            return []
        self._last_settle = t
        now = now or engine.now_utc()
        due = [b for b in self.bets if b["status"] == "open"
               and (engine._parse_time(b.get("decided") or "") or now) <= now]
        if not due:
            return []
        sc, done = self.scanner, []
        st = {}
        try:
            tickers = [b["market_id"] for b in due if b["exchange"] == "kalshi"]
            slugs = [b["market_id"] for b in due if b["exchange"] == "polymarket"]
            if tickers:
                st.update({("kalshi", k): kalshi_status(m) for k, m in sc.kalshi.markets_by_ticker(tickers).items()})
            if slugs:
                st.update({("polymarket", s): polymarket_status(m) for s, m in sc.pm.markets_by_slug(slugs).items()})
        except Exception as e:
            sc.log(f"EV bot: couldn't read bet results ({e!r}); will retry")
            return []
        with self.lock:
            for b in due:
                s = st.get((b["exchange"], b["market_id"])) or {}
                if not s.get("paid"):
                    continue
                pays = s["pays_yes"] if b["side"] == YES else 1 - s["pays_yes"]
                b["payout"] = round(b["qty"] * pays, 2)
                b["pnl"] = round(b["payout"] - b["cost"], 2)
                b["status"] = "won" if pays >= 0.999 else "lost" if pays <= 0.001 else "void"
                b["settled_at"] = now.isoformat()
                done.append(b)
            if done:
                self._save()
        for b in done:
            sc.log(f"EV bot {'paper ' if b['paper'] else ''}bet {b['status']}: {b['game']} · {NAMES[b['exchange']]} "
                   f"{b['side'].upper()} {b['qty']:g}: {'+' if b['pnl'] >= 0 else '-'}${abs(b['pnl']):.2f}")
        return done
