"""Polymarket US public market data: sports market parsing, batched quotes, order books."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import config
from .http import RateLimitedClient

PAGE = 500
WORKERS = 6

SLUG_RE = re.compile(r"^(?P<prefix>aec|asc|tsc|atc)-(?P<league>[a-z0-9]+)-(?P<t1>[a-z0-9]+)-(?P<t2>[a-z0-9]+)-"
                     r"(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<rest>.+))?$")
TYPE_RE = re.compile(r"^(?P<sport>football|basketball|baseball|hockey|soccer)_(?P<scope>team|game)_(?P<mid>.+?)_"
                     r"(?P<kind>winner|spread|total|total_runs|total_goals)$")
PERIOD_WORDS = {
    "full_game": "FG", "full_time": "FG", "points_full_game": "FG", "": "FG",
    "first_half": "1H", "second_half": "2H",
    "first_quarter": "1Q", "second_quarter": "2Q", "third_quarter": "3Q", "fourth_quarter": "4Q",
    "first_period": "1P", "second_period": "2P", "third_period": "3P",
    "first_five": "F5",
}


@dataclass
class PMMarket:
    slug: str
    league: str
    sport: str
    date: str            # YYYY-MM-DD (event-local date, as in the slug)
    t1: str
    t2: str
    kind: str            # GAME | SPREAD | TOTAL | TEAMTOTAL
    period: str
    team: str | None     # PM team abbreviation, "draw", or None
    op: str
    line: float
    tie_half: bool
    title: str
    rules: str
    start_time: str
    fee_coef: float
    team_names: dict     # abbreviation -> set of names seen on this market
    yes_ask: float | None = None
    no_ask: float | None = None
    levels: dict = field(default_factory=dict)
    state: str = ""
    quoted_at: float = 0.0   # time.time() when the request behind the current quote was sent


def _q(v):
    try:
        return float((v or {}).get("value"))
    except (TypeError, ValueError):
        return None


def _period(mid):
    if mid in PERIOD_WORDS:
        return PERIOD_WORDS[mid]
    m = re.match(r"^inning(\d)$", mid)
    return f"I{m[1]}" if m else None


def parse_market(m):
    slug = m.get("slug") or ""
    sm = SLUG_RE.match(slug)
    if not sm or sm["league"] not in config.LEAGUES:
        return None
    if m.get("closed") or not m.get("active") or m.get("status") not in (None, "MARKET_STATUS_OPEN"):
        return None
    stype = m.get("sportsMarketType") or ""
    rest = sm["rest"] or ""
    team_total = re.match(r"^tt(?:1h|2h)?-([a-z0-9]+)-", rest)

    # Special-cased names that don't follow sport_scope_period_kind.
    if stype == "football_team_points_full_game_total":
        sport, mid, kind = "football", "full_game", "total"
    elif stype in ("baseball_team_total_runs", "hockey_team_total_goals", "soccer_team_total_goals"):
        sport, mid, kind = stype.split("_")[0], "full_game", "total"
    else:
        tm = TYPE_RE.match(stype)
        if not tm:
            return None
        sport, mid, kind = tm["sport"], tm["mid"], tm["kind"]
    period = _period(mid)
    if period is None:
        return None

    sides = m.get("marketSides") or []
    long_side = next((s for s in sides if s.get("long")), {})
    long_team = ((long_side.get("team") or {}).get("abbreviation") or "").lower() or None
    names = {}
    for s in sides:
        t = s.get("team") or {}
        if t.get("abbreviation"):
            names.setdefault(t["abbreviation"].lower(), set()).update(
                n for n in (t.get("name"), t.get("alias"), t.get("safeName")) if n)

    line = m.get("line")
    tie_half = False
    if kind == "winner":
        if sm["prefix"] == "aec":            # two-way moneyline, long side = long team
            team = long_team or sm["t1"]
            kind_c, op, line = "GAME", ">", 0.0
            tie_half = sport == "football" and period == "FG"
        else:                                # atc-...-<team|draw>: one market per outcome
            team = rest.split("-")[-1]
            kind_c = "GAME"
            op, line = ("==", 0.0) if team == "draw" else (">", 0.0)
    elif kind == "spread":
        if line is None or not long_team:
            return None
        # "Will LONG cover L" <=> margin(LONG) + L > 0 <=> margin(LONG) > -L
        team, kind_c, op, line = long_team, "SPREAD", ">", -float(line)
    else:
        if line is None:
            return None
        if team_total:
            team, kind_c = team_total[1], "TEAMTOTAL"
        else:
            team, kind_c = None, "TOTAL"
        op, line = ">", float(line)

    pm = PMMarket(
        slug=slug, league=sm["league"], sport=sport, date=sm["date"], t1=sm["t1"], t2=sm["t2"],
        kind=kind_c, period=period, team=team, op=op, line=float(line), tie_half=tie_half,
        title=m.get("question") or slug, rules=m.get("description") or "",
        start_time=m.get("gameStartTime") or m.get("endDate") or "",
        fee_coef=float(m.get("feeCoefficient") or config.POLYMARKET_DEFAULT_COEF), team_names=names,
    )
    set_quotes(pm, m)
    return pm


def set_quotes(pm, m):
    bid, ask = _q(m.get("bestBidQuote")), _q(m.get("bestAskQuote"))
    pm.yes_ask = ask
    pm.no_ask = round(1 - bid, 4) if bid is not None else None   # short at the bid, $1 margin


class PolymarketClient:
    def __init__(self):
        self.http = RateLimitedClient(config.POLYMARKET_BASE, config.POLYMARKET_RPS)

    def _page(self, offset):
        return self.http.get("/markets", {"active": "true", "closed": "false", "categories": "sports",
                                          "limit": PAGE, "offset": offset}).get("markets") or []

    def load_sports_markets(self, log=print):
        """Pages are fetched in parallel waves; the listing ends at the first short page."""
        out, offset = [], 0
        with ThreadPoolExecutor(WORKERS) as pool:
            while True:
                pages = list(pool.map(self._page, [offset + i * PAGE for i in range(WORKERS)]))
                for ms in pages:
                    out += [pm for pm in map(parse_market, ms) if pm]
                offset += WORKERS * PAGE
                if any(len(ms) < PAGE for ms in pages):
                    return out
                if offset % 12000 == 0:
                    log(f"  polymarket: scanned {offset} markets, {len(out)} usable")

    def raw_markets(self, categories, log=print):
        """Active markets in the given categories, as returned by the API, each market once.
        A category slug the API rejects is logged and skipped, so one bad slug can't stop the rest."""
        out = {}
        for cat in categories:
            offset = 0
            while True:
                try:
                    ms = self.http.get("/markets", {"active": "true", "closed": "false", "categories": cat,
                                                    "limit": PAGE, "offset": offset}).get("markets") or []
                except Exception as e:
                    log(f"  polymarket: category {cat!r} not loaded ({e!r})")
                    break
                for m in ms:
                    out.setdefault(m.get("slug"), m)
                if len(ms) < PAGE:
                    break
                offset += PAGE
        return list(out.values())

    def markets_by_slug(self, slugs):
        out = {}
        slugs = list(slugs)
        for i in range(0, len(slugs), 100):
            d = self.http.get("/markets", [("slug", s) for s in slugs[i:i + 100]] + [("limit", 200)])
            out.update({m["slug"]: m for m in d.get("markets") or []})
        return out

    def refresh_quotes(self, markets, high=False):
        """Refresh top of book for many markets, 100 slugs per request, in parallel. A quote is
        only applied if it was requested after the one the market already holds."""
        by_slug = {m.slug: m for m in markets}
        slugs = list(by_slug)
        chunks = [slugs[i:i + 100] for i in range(0, len(slugs), 100)]

        def fetch(chunk):
            sent = time.time()
            try:
                return chunk, sent, self.http.get("/markets", [("slug", s) for s in chunk] + [("limit", 200)], high=high)
            except Exception:
                return chunk, sent, {}        # unpriced this cycle (handled below as "not seen")

        with ThreadPoolExecutor(WORKERS) as pool:
            for chunk, sent, d in pool.map(fetch, chunks):
                seen = set()
                for m in d.get("markets") or []:
                    pm = by_slug.get(m.get("slug"))
                    if pm:
                        seen.add(pm.slug)
                        if sent < pm.quoted_at:
                            continue
                        if m.get("closed") or not m.get("active"):
                            pm.yes_ask = pm.no_ask = None
                        else:
                            set_quotes(pm, m)
                        pm.quoted_at = sent
                for s in chunk:
                    pm = by_slug[s]
                    if s not in seen and sent >= pm.quoted_at:
                        pm.yes_ask = pm.no_ask = None
                        pm.quoted_at = sent

    def live_levels(self, slug):
        """Current depth for buying each side: {"yes": [...], "no": [...], "state": ...}.
        Buying YES lifts offers; buying NO = shorting into bids at cost (1 - bid)."""
        d = self.http.get(f"/markets/{slug}/book", high=True).get("marketData") or {}
        bids = [(_q(l.get("px")), float(l.get("qty") or 0)) for l in d.get("bids") or []]
        offers = [(_q(l.get("px")), float(l.get("qty") or 0)) for l in d.get("offers") or []]
        bids = sorted(((p, q) for p, q in bids if p is not None and q > 0), key=lambda t: -t[0])
        offers = sorted(((p, q) for p, q in offers if p is not None and q > 0), key=lambda t: t[0])
        return {"yes": offers, "no": [(round(1 - p, 4), q) for p, q in bids], "state": d.get("state")}

    def refresh_book(self, pm):
        sent = time.time()
        lv = self.live_levels(pm.slug)
        pm.levels = {"yes": lv["yes"], "no": lv["no"]}
        pm.yes_ask = lv["yes"][0][0] if lv["yes"] else None
        pm.no_ask = lv["no"][0][0] if lv["no"] else None
        pm.state = lv["state"]
        pm.quoted_at = max(pm.quoted_at, sent)
