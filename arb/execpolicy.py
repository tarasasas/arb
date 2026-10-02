"""How Auto-trade decides what to try and how, from what its trades have shown so far.

  - Market types: every row gets one (e.g. "MLB", "Crypto windows", "NFL live"). Each type keeps its
    last AUTO_TRADE_STATS_KEEP results: real ones and paper ones (AUTO_TRADE_DRY_RUN) separately.
  - Minimum edge: fast markets (crypto windows, games in progress) need AUTO_TRADE_FAST_EDGE per pair;
    with AUTO_TRADE_LEARN_BUFFER, every type also needs the typical (75th percentile) price move its
    trades have met while the orders went out.
  - Throttle (AUTO_TRADE_THROTTLE): a type whose recent real trades mostly miss, or lose money, is
    paused for AUTO_TRADE_THROTTLE_HOURS.
  - Leg order ("smart"): the stale side first when the live streams show one site just moved and the
    other hasn't (the stale one is about to reprice); both at once in fast markets, or where second
    legs keep missing; otherwise the thinner book first.
"""

import json
import threading
import time
from collections import deque

from . import config

FAST_CATEGORIES = ("Crypto windows",)
MIN_MISSES = 3          # misses needed before "the site that misses more goes first" kicks in
STATUSES = ("ok", "partial", "no_fill", "rejected")      # attempts that reached an exchange (or would have)


def in_play(row):
    return any(w.startswith("Game already started") for w in row.get("warnings") or [])


def category(row):
    """A row's market type: the league for sports, the tab for the rest, crypto windows on their own,
    and games in progress apart from the same league before the start."""
    league, tab = str(row.get("league") or ""), row.get("tab") or ""
    if league.upper() == "CRYPTO":
        return "Crypto windows"
    base = league if tab == "Sports" and league else tab or league or "Other"
    return f"{base} live" if in_play(row) else base


def is_fast(cat):
    return cat in FAST_CATEGORIES or cat.endswith(" live")


def _p75(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(0.75 * len(xs)))] if xs else 0.0


class ExecStats:
    def __init__(self, path=None, now=time.time):
        self.path, self.now, self.lock = path, now, threading.Lock()
        self.results = {}          # category -> deque of {time, status, net, slip, mode, paper}
        self.paused = {}           # category -> (until, why)
        self._load()

    # ---- storage ----------------------------------------------------------------------------

    def _load(self):
        if not self.path:
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for cat, rs in (d.get("results") or {}).items():
            self.results[cat] = deque(rs, maxlen=int(config.AUTO_TRADE_STATS_KEEP))
        self.paused = {c: tuple(v) for c, v in (d.get("paused") or {}).items()}

    def _save(self):
        if not self.path:
            return
        try:
            self.path.parent.mkdir(exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"results": {c: list(r) for c, r in self.results.items()},
                                       "paused": self.paused}), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass

    # ---- results ----------------------------------------------------------------------------

    def record(self, cat, status, net=0.0, slip=None, mode=None, paper=False, missed=()):
        """One attempt's outcome. missed: the sites ("kalshi", "polymarket") whose order didn't fill.
        Returns a pause reason if this result paused the category."""
        if status not in STATUSES:
            return None
        with self.lock:
            rs = self.results.setdefault(cat, deque(maxlen=int(config.AUTO_TRADE_STATS_KEEP)))
            rs.append({"time": self.now(), "status": status, "net": round(net or 0.0, 4),
                       "slip": slip, "mode": mode, "paper": bool(paper), "missed": sorted(set(missed or ()))})
            why = None if paper else self._throttle(cat)
            self._save()
            return why

    def _recent(self, cat, paper=None):
        rs = list(self.results.get(cat) or [])
        return [r for r in rs if paper is None or r["paper"] == paper]

    def _throttle(self, cat):
        if not config.AUTO_TRADE_THROTTLE:
            return None
        rs = self._recent(cat, paper=False)[-int(config.AUTO_TRADE_THROTTLE_MIN_TRIES):]
        if len(rs) < int(config.AUTO_TRADE_THROTTLE_MIN_TRIES):
            return None
        filled = sum(r["status"] == "ok" for r in rs) / len(rs)
        net = sum(r["net"] for r in rs)
        why = None
        if filled < config.AUTO_TRADE_THROTTLE_MIN_FILL:
            why = f"only {filled:.0%} of its last {len(rs)} trades filled on both sites"
        elif net < 0:
            why = f"its last {len(rs)} trades lost ${-net:.2f} in total"
        if why:
            until = self.now() + config.AUTO_TRADE_THROTTLE_HOURS * 3600
            self.paused[cat] = (until, why)
            # start over once the pause ends: the old results would pause it again on the next miss
            self.results[cat] = deque((r for r in self.results[cat] if r["paper"]),
                                      maxlen=int(config.AUTO_TRADE_STATS_KEEP))
        return why

    def paused_why(self, cat):
        until, why = self.paused.get(cat, (0, ""))
        return why if until > self.now() else None

    def resume(self, cat=None):
        with self.lock:
            self.paused = {} if cat is None else {c: v for c, v in self.paused.items() if c != cat}
            self._save()

    # ---- what a category needs ----------------------------------------------------------------

    def learned_buffer(self, cat):
        """Typical adverse move ($/share) this type's trades met while the orders went out (75th
        percentile of the recorded ones, real and paper)."""
        slips = [r["slip"] for r in self._recent(cat) if r.get("slip") is not None]
        return round(_p75(slips), 4) if len(slips) >= 3 else 0.0

    def min_edge(self, cat):
        """Edge per pair this type needs before Auto-trade tries it, and why."""
        need, why = 0.0, []
        if is_fast(cat) and config.AUTO_TRADE_FAST_EDGE > 0:
            need = config.AUTO_TRADE_FAST_EDGE
            why.append(f"{config.AUTO_TRADE_FAST_EDGE * 100:g}¢ in fast markets")
        if config.AUTO_TRADE_LEARN_BUFFER:
            b = self.learned_buffer(cat)
            if b > need:
                need = b
                why = [f"{b * 100:.1f}¢: the typical price move its trades met"]
        return need, "; ".join(why)

    def second_leg_misses(self, cat):
        """Share of recent sequential attempts (real or paper) whose second leg missed."""
        rs = [r for r in self._recent(cat) if r["mode"] in ("thinner_first", "polymarket_first")
              and r["status"] in ("ok", "partial")][-10:]
        return (sum(r["status"] == "partial" for r in rs) / len(rs), len(rs)) if rs else (0.0, 0)

    def misses_by_site(self, cat):
        """{"kalshi": n, "polymarket": n}: orders that didn't fill over this type's last 20 attempts (real and
        paper); all types together when this one has fewer than MIN_MISSES."""
        def count(rs):
            out = {"kalshi": 0, "polymarket": 0}
            for r in rs[-20:]:
                for site in r.get("missed") or ():
                    if site in out:
                        out[site] += 1
            return out
        mine = count(self._recent(cat))
        if sum(mine.values()) >= MIN_MISSES:
            return mine, cat
        everything = [r for c in list(self.results) for r in self._recent(c)]
        everything.sort(key=lambda r: r["time"])
        return count(everything), "all types"

    def summary(self):
        out = []
        with self.lock:
            cats = set(self.results) | set(self.paused)
            for cat in sorted(cats):
                real, paper = self._recent(cat, False), self._recent(cat, True)
                need, why = self.min_edge(cat)
                out.append({"category": cat, "tries": len(real), "filled": sum(r["status"] == "ok" for r in real),
                            "net": round(sum(r["net"] for r in real), 2),
                            "paper_tries": len(paper), "paper_filled": sum(r["status"] == "ok" for r in paper),
                            "paper_net": round(sum(r["net"] for r in paper), 2),
                            "buffer": self.learned_buffer(cat), "min_edge": need, "min_edge_why": why,
                            "paused": self.paused_why(cat), "fast": is_fast(cat)})
        return out


def stale_side(scanner, legs, now=None):
    """The site whose price is stale while the other just moved, from the live streams: the other site
    changed within STALE_FRESH_SECS and this one not for STALE_GAP_SECS longer. None if unclear (either
    market not streamed, or both moved / both quiet)."""
    now = now or time.time()
    t = {}
    for leg in legs:
        ex, mid = leg["exchange"].lower(), leg["market_id"]
        s = (getattr(scanner, "streams", None) or {}).get(ex)
        if not s or not getattr(s, "connected", False) or mid not in getattr(s, "seen", ()):
            return None
        # when its best prices last changed (a change deeper in the book isn't a reprice); older stream
        # objects without that fall back to the last message of any kind
        changed = getattr(s, "top_changed_at", None)
        t[ex] = (changed if changed is not None else getattr(s, "updated_at", {})).get(mid)
        if not t[ex]:
            return None
    if len(t) != 2:
        return None
    fresh, stale = sorted(t, key=lambda ex: -t[ex])
    s = scanner.streams[fresh]
    mid = next(l["market_id"] for l in legs if l["exchange"].lower() == fresh)
    if (getattr(s, "first_seen", {}).get(mid) or 0) >= t[fresh]:
        return None                               # its only message is the snapshot after subscribing
    if now - t[fresh] <= config.STALE_FRESH_SECS and t[fresh] - t[stale] >= config.STALE_GAP_SECS:
        return stale
    return None


NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket"}


def choose_order(scanner, row, stats, cat):
    """(order mode, site to send first or None, why) for one Auto-trade attempt."""
    mode = config.AUTO_TRADE_ORDER
    if mode != "smart":
        return mode, None, "your setting"
    legs = [{"exchange": l["exchange"].lower(), "market_id": l["market_id"]} for l in row["legs"]]
    stale = stale_side(scanner, legs)
    if stale:
        other = "polymarket" if stale == "kalshi" else "kalshi"
        return "thinner_first", stale, (f"{NAMES[other]} just moved and {NAMES[stale]} hasn't yet: {NAMES[stale]} "
                                        f"first, before it reprices")
    # The site whose orders keep missing goes first: a first order that misses trades nothing, while a second
    # one that misses leaves the first leg to be sold back at a loss.
    misses, where = stats.misses_by_site(cat)
    worse = max(misses, key=misses.get)
    better = "polymarket" if worse == "kalshi" else "kalshi"
    if misses[worse] >= MIN_MISSES and misses[worse] >= 2 * misses[better]:
        return "thinner_first", worse, (f"{NAMES[worse]} missed {misses[worse]} times recently ({where}; "
                                        f"{NAMES[better]} {misses[better]}): {NAMES[worse]} first, so a miss there "
                                        f"trades nothing")
    if is_fast(cat):
        return "together", None, "fast market: both orders at once"
    rate, n = stats.second_leg_misses(cat)
    if n >= 3 and rate >= 0.5:
        return "together", None, f"second legs missed {rate:.0%} of the last {n} here: both orders at once"
    return "thinner_first", None, "thinner book first"
