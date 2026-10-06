"""Kalshi public market data: series discovery, sports market parsing, batch order books."""

import os
import re
import threading
import time
import urllib.error
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import config
from .http import LanePool, RateLimitedClient, unique_token
from .kalshi_auth import load_signer

DEFAULT_TOKEN_COST = 10   # all market-data GETs; see GET /account/endpoint_costs

PERIODS = ("1H", "2H", "1Q", "2Q", "3Q", "4Q", "1P", "2P", "3P", "F5")
SERIES_REST_RE = re.compile(r"^(?P<period>1H|2H|1Q|2Q|3Q|4Q|1P|2P|3P|F5)?(?P<kind>GAME|SPREAD|TOTAL|TEAMTOTAL|INNINGTOTAL|RFI|BTTS|SCORE)?$")
TEAM_STRIKE_RE = re.compile(r"^(?P<team>[A-Z0-9]*?[A-Z])(?P<num>\d+)$")
SCORE_STRIKE_RE = re.compile(r"^(?P<a>[A-Z]+)(?P<x>\d+)(?P<b>[A-Z]+)(?P<y>\d+)$")     # AZE5LTU2: AZE 5, LTU 2

# Player props: Kalshi series -> stat, the same names polymarket.PLAYER_PROPS uses. Only stats whose
# rules match on both sites (same participation rule: starting lineup / starting pitcher, a snap, time
# on ice). Soccer goals are left out: Kalshi counts a substitute's goals and extra time, Polymarket
# settles a non-starter at a fair price and stops at 90 minutes.
PLAYER_PROPS = {
    "KXMLBHIT": "hits", "KXMLBHR": "hr", "KXMLBHRR": "hrr", "KXMLBKS": "k", "KXMLBTB": "tb", "KXMLBRBI": "rbi",
    "KXMLBSB": "sb", "KXMLBHA": "ha", "KXMLBOUTS": "outs",
    "KXNFLRECYDS": "recyd", "KXNFLRSHYDS": "ryd", "KXNFLPASSYDS": "pyd", "KXNFLPASSTDS": "ptd", "KXNFLREC": "rec",
    "KXNFLTD": "td", "KXNFLPASSCOMP": "pcmp", "KXNFLPASSATT": "patt", "KXNFLRSHATT": "ratt", "KXNFLPASSINT": "int",
    "KXNFLRRYDS": "scrim",
    "KXNHLPTS": "pts", "KXNHLGOAL": "goals", "KXNHLAST": "ast",
}

# Kalshi league code -> (Polymarket league code, sport); longest code first so that
# e.g. BRASILEIROB is tried before BRASILEIRO.
_BY_KALSHI = sorted(((k, pm, sport) for pm, (k, sport) in config.LEAGUES.items()), key=lambda t: -len(t[0]))


@dataclass
class KalshiMarket:
    ticker: str
    event_ticker: str
    series: str
    league: str          # Kalshi league code, e.g. NFL
    pm_league: str
    sport: str
    body: str            # event body, e.g. 26OCT04INDWAS
    date_code: str       # 26OCT04
    teams_str: str       # INDWAS
    kind: str            # GAME | SPREAD | TOTAL | TEAMTOTAL | BTTS | SCORE | PROP
    period: str          # FG, 1H, ..., F5, I1..I9
    team: str | None     # team code, "TIE", or None
    op: str
    line: float
    title: str
    name: str            # team display name for GAME markets (yes_sub_title)
    rules: str
    close_time: str
    fee_coef: float
    yes_ask: float | None = None
    no_ask: float | None = None
    yes_ask_size: float | None = None
    no_ask_size: float | None = None
    # Depth for buying each side: [(price, qty)] best first.
    levels: dict = field(default_factory=dict)
    quoted_at: float = 0.0   # when the request behind the current quote was sent (or the stream update arrived)
    # Kalshi exchange shard the market trades on (0 default, 2 crypto/commodities, 3 some sports).
    # Orders only use cash held on that shard.
    shard: int = 0
    score: tuple | None = None   # SCORE: ((team code, goals), (team code, goals))
    player: str = ""             # PROP: the player's name, as Kalshi writes it
    stat: str = ""               # PROP: key in PLAYER_PROPS


def settle_time(m):
    """When a Kalshi market is expected to settle (pay out). expected_expiration_time can be a placeholder
    for a whole event (e.g. Dec 2028 on a "before 2027" market), so it's capped at the market's own
    latest_expiration_time; close_time is the fallback."""
    from datetime import datetime
    def t(s):
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            return None
    exp, latest = m.get("expected_expiration_time"), m.get("latest_expiration_time")
    if exp and latest and t(exp) and t(latest) and t(latest) < t(exp):
        return latest
    return exp or latest or m.get("close_time") or ""


# Tennis match-winner series on every tour, read as one league (see config.LEAGUES "atp", "wta", ...).
TENNIS_SERIES = {"KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH", "KXWTACHALLENGERMATCH", "KXCHALLENGERMATCH",
                 "KXITFMATCH", "KXITFWMATCH"}


def parse_series(series_ticker):
    """KXNFL1HSPREAD -> (NFL, nfl, football, 1H, SPREAD) or None."""
    if not series_ticker.startswith("KX"):
        return None
    if series_ticker in TENNIS_SERIES:
        return "TENNIS", "atp", "tennis", "FG", "GAME"
    rest_all = series_ticker[2:]
    for code, pm_code, sport in _BY_KALSHI:
        if rest_all.startswith(code) and series_ticker in PLAYER_PROPS:
            return code, pm_code, sport, "FG", "PROP"
        if rest_all.startswith(code):
            m = SERIES_REST_RE.match(rest_all[len(code):])
            if not m or not (m["period"] or m["kind"]):
                continue
            return code, pm_code, sport, m["period"] or "FG", m["kind"] or "GAME"
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_market(m, series_info, fee_coef):
    code, pm_code, sport, period, kind = series_info
    ev = m["event_ticker"]
    if "-" not in ev or not (m["ticker"].startswith(ev + "-") or m["ticker"] == ev):   # KXMLBRFI: ticker = event
        return None
    body = ev.split("-", 1)[1]
    date_code, teams_str = body[:7], re.sub(r"^\d{4}", "", body[7:])
    suffix = m["ticker"][len(ev) + 1:]
    strike_type = m.get("strike_type")
    team, op, line, score, player = None, None, None, None, ""

    if kind == "GAME":
        if strike_type != "structured":
            return None
        team = suffix
        op, line = ("==", 0.0) if suffix == "TIE" else (">", 0.0)
    elif kind == "RFI":                     # "1st inning: Over 0.5 runs" = 1st-inning total of 1 or more
        if strike_type != "greater_or_equal" or _f(m.get("floor_strike")) is None:
            return None
        period, kind, op, line = "I1", "TOTAL", ">", _f(m.get("floor_strike")) - 0.5
    elif kind == "BTTS":                    # both teams score (in the period): a yes/no event
        op, line = ">", 0.5
    elif kind == "SCORE":                   # one exact score (after 90 minutes + stoppage): a yes/no event
        mm = SCORE_STRIKE_RE.match(suffix)
        if not mm or teams_str not in (mm["a"] + mm["b"], mm["b"] + mm["a"]):     # both teams of this game
            return None
        score = ((mm["a"], int(mm["x"])), (mm["b"], int(mm["y"])))
        op, line = ">", 0.5
    elif kind == "PROP":                    # "Gerrit Cole: 7+" strikeouts = count > 6.5
        if strike_type != "greater" or _f(m.get("floor_strike")) is None:
            return None
        player = (m.get("yes_sub_title") or "").split(":")[0].strip()
        if not player:
            return None
        op, line = ">", _f(m.get("floor_strike"))
    elif kind == "INNINGTOTAL":
        mm = re.match(r"^(\d+)-\d+$", suffix)
        if not mm or strike_type != "greater":
            return None
        period = "I" + mm[1]
        kind, op, line = "TOTAL", ">", _f(m.get("floor_strike"))
    else:
        if strike_type == "greater":
            op, line = ">", _f(m.get("floor_strike"))
        elif strike_type == "less":
            op, line = "<", _f(m.get("cap_strike"))
        else:
            return None
        if kind in ("SPREAD", "TEAMTOTAL"):
            mm = TEAM_STRIKE_RE.match(suffix)
            if not mm:
                return None
            team = mm["team"]
    if line is None:
        return None

    km = KalshiMarket(
        ticker=m["ticker"], event_ticker=ev, series=ev.split("-")[0], league=code, pm_league=pm_code,
        sport=sport, body=body, date_code=date_code, teams_str=teams_str, kind=kind, period=period,
        team=team, op=op, line=line, title=m.get("title") or m["ticker"], name=m.get("yes_sub_title") or "",
        rules=((m.get("rules_primary") or "") + "\n\n" + (m.get("rules_secondary") or "")).strip(),
        close_time=settle_time(m), fee_coef=fee_coef,
        shard=int(m.get("exchange_index") or 0), score=score, player=player,
        stat=PLAYER_PROPS.get(ev.split("-")[0], "") if kind == "PROP" else "",
    )
    km.yes_ask, km.no_ask = _f(m.get("yes_ask_dollars")), _f(m.get("no_ask_dollars"))
    km.yes_ask_size = _f(m.get("yes_ask_size_fp"))
    return km


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def effective_multiplier(series_mult, changes, now, horizon_secs):
    """Fee multiplier for one event: the series' own, replaced by the latest event override already
    in effect (None clears it). Overrides scheduled within `horizon_secs` also count, and the higher
    rate wins, because fees are only re-read that often (e.g. a playoff game going from 0.5x to 1x)."""
    current, soon = series_mult, []
    for ts, mult in sorted(changes, key=lambda c: c[0] or datetime.min.replace(tzinfo=timezone.utc)):
        value = series_mult if mult is None else float(mult)
        if ts is None or ts <= now:
            current = value
        elif ts <= now + timedelta(seconds=horizon_secs):
            soon.append(value)
    return max([current] + soon)


def apply_fee_overrides(markets, overrides, now, horizon_secs):
    """Set each market's taker coefficient from its event's fee override, if it has one."""
    for m in markets:
        changes = overrides.get(m.event_ticker)
        if changes:
            series_mult = m.fee_coef / config.KALSHI_TAKER_COEF
            m.fee_coef = config.KALSHI_TAKER_COEF * effective_multiplier(series_mult, changes, now, horizon_secs)


def workers_for(rps, reply_secs=1.5, cap=32):
    """Enough parallel requests to keep `rps` going when each reply takes ~reply_secs."""
    return max(2, min(cap, round(rps * reply_secs)))


class KalshiClient:
    def __init__(self):
        signer = None
        self.auth_info = "public data, no API key"
        if config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY:
            signer = load_signer(config.KALSHI_API_KEY_ID, key_pem=config.KALSHI_PRIVATE_KEY)
        elif config.KALSHI_API_KEY_ID and config.KALSHI_PRIVATE_KEY_PATH:
            if not os.path.exists(config.KALSHI_PRIVATE_KEY_PATH):
                raise SystemExit(f"KALSHI_PRIVATE_KEY_PATH not found: {config.KALSHI_PRIVATE_KEY_PATH}")
            signer = load_signer(config.KALSHI_API_KEY_ID, config.KALSHI_PRIVATE_KEY_PATH)
        self.http = RateLimitedClient(config.KALSHI_BASE, config.KALSHI_RPS, signer=signer)
        # Requests in flight. The rate limiter sets the pace; this only has to cover the reply time
        # (~1s for a 100-market order-book batch), or the queue runs below the allowed rate.
        self.workers = workers_for(config.KALSHI_RPS)
        if signer:
            self._apply_account_limits()

    def _apply_account_limits(self):
        try:
            limits = self.http.get("/account/limits")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("Kalshi rejected the API key (HTTP %d). Check that KALSHI_API_KEY_ID matches "
                                 "the private key file and that the key is for production, not demo." % e.code)
            raise
        refill = float((limits.get("read") or {}).get("refill_rate") or 0)
        if refill <= 0:
            return
        rps = refill / DEFAULT_TOKEN_COST * config.KALSHI_BUDGET_FRACTION
        self.http.set_rate(rps)
        self.workers = workers_for(rps)
        grants = [g.get("level") for g in limits.get("grants") or [] if g.get("level")]
        # Kalshi keeps usage_tier at "basic" after an upgrade; the grant and the read budget show it.
        tier = limits.get("usage_tier", "?") + (f" + {'/'.join(grants)} grant" if grants else "")
        self.auth_info = f"API key, {tier}, {rps:.0f} req/s"

    SERIES_TTL = 1800      # the full series list is 18 MB (a ~200ms pause to read): fetch it once per half hour

    def _all_series(self):
        """Every Kalshi series, shared by sports_series() and series_fee_coefs()."""
        lock = self.__dict__.setdefault("_series_lock", threading.Lock())
        with lock:
            hit = getattr(self, "_series_cache", None)
            if hit and time.time() - hit[0] < self.SERIES_TTL:
                return hit[1]
            series = self.http.get("/series").get("series", [])
            self._series_cache = (time.time(), series)
            return series

    def sports_series(self):
        """Return {series_ticker: (series_info, fee_coef)} for configured leagues."""
        out = {}
        for s in self._all_series():
            info = parse_series(s["ticker"])
            if not info:
                continue
            mult = s.get("fee_multiplier")
            mult = 1.0 if mult is None else float(mult)
            out[s["ticker"]] = (info, config.KALSHI_TAKER_COEF * mult)
        return out

    def series_fee_coefs(self):
        """{series_ticker: taker coefficient} for every series (non-sports included)."""
        out = {}
        for s in self._all_series():
            mult = s.get("fee_multiplier")
            out[s["ticker"]] = config.KALSHI_TAKER_COEF * (1.0 if mult is None else float(mult))
        return out

    def event_fee_overrides(self):
        """{event_ticker: [(scheduled time, multiplier or None)]}: per-event fee overrides layered on
        top of the series fee (GET /events/fee_changes)."""
        out, cursor = defaultdict(list), None
        while True:
            params = {"limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = self.http.get("/events/fee_changes", params)
            changes = d.get("event_fee_changes") or []
            for c in changes:
                out[c["event_ticker"]].append((_ts(c.get("scheduled_ts")), c.get("fee_multiplier_override")))
            cursor = d.get("cursor")
            if not cursor or not changes:
                return dict(out)

    def open_events(self, log=print):
        """Every open event with its markets nested (the endpoint excludes combos)."""
        events, cursor = [], None
        while True:
            params = {"status": "open", "limit": 200, "with_nested_markets": "true"}
            if cursor:
                params["cursor"] = cursor
            d = self.http.get("/events", params)
            events += d.get("events", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("events"):
                return events

    def markets_by_ticker(self, tickers):
        out = {}
        tickers = list(tickers)
        for i in range(0, len(tickers), 100):
            d = self.http.get("/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 1000})
            out.update({m["ticker"]: m for m in d.get("markets", [])})
        return out

    def open_markets(self, series_ticker):
        markets, cursor = [], None
        while True:
            params = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            d = self.http.get("/markets", params)
            markets += d.get("markets", [])
            cursor = d.get("cursor")
            if not cursor or not d.get("markets"):
                return markets

    def load_sports_markets(self, log=print):
        series = sorted(self.sports_series().items())

        def fetch(item):
            ticker, (info, coef) = item
            try:
                return [km for km in (parse_market(m, info, coef) for m in self.open_markets(ticker)) if km]
            except Exception as e:          # one bad series shouldn't sink the whole catalog
                log(f"  kalshi: skipped {ticker} ({e!r})")
                return []

        result = []
        with LanePool(self.workers) as pool:
            for i, kms in enumerate(pool.map(fetch, series)):
                result += kms
                if (i + 1) % 50 == 0:
                    log(f"  kalshi: {i + 1}/{len(series)} series, {len(result)} markets")
        return result

    def refresh_books(self, markets):
        """Fetch order books (100 tickers per request) and set levels + top of book.
        Returns the tickers whose request failed (unpriced this cycle, but not known to be gone).
        A book is applied only if its request went out after the market's current quote, so a slow
        poll never replaces a newer streamed book."""
        by_ticker = {m.ticker: m for m in markets}
        tickers = list(by_ticker)
        chunks = [tickers[i:i + 100] for i in range(0, len(tickers), 100)]

        def fetch(chunk):
            sent = time.time()
            try:
                return chunk, sent, self.http.get("/markets/orderbooks", [("tickers", t) for t in chunk])
            except Exception:
                return chunk, sent, None

        with LanePool(self.workers) as pool:
            results = list(pool.map(fetch, chunks))
        failed = set()
        for chunk, sent, d in results:
            if d is None:
                # Leave these markets unpriced this cycle rather than acting on stale books.
                for t in chunk:
                    _unprice(by_ticker[t], sent)
                failed.update(chunk)
                continue
            seen = set()
            for ob in d.get("orderbooks", []):
                m = by_ticker.get(ob.get("ticker"))
                if not m:
                    continue
                seen.add(m.ticker)
                if sent < getattr(m, "quoted_at", 0.0):
                    continue
                m.levels = buy_levels(ob.get("orderbook_fp") or {})
                buy_yes, buy_no = m.levels["yes"], m.levels["no"]
                m.yes_ask, m.yes_ask_size = buy_yes[0] if buy_yes else (None, None)
                m.no_ask, m.no_ask_size = buy_no[0] if buy_no else (None, None)
                m.quoted_at = sent
            for t in chunk:
                if t not in seen:            # not in the reply (closed, halted): don't keep last cycle's book
                    _unprice(by_ticker[t], sent)
        return failed

    def refresh_tops(self, markets):
        """Top of book only, from the market list: 200 tickers per request where order books take
        100, so a full sweep needs half the requests. Depth comes from refresh_books() on the pairs
        that need it. Same freshness rule and return value as refresh_books()."""
        by_ticker = {m.ticker: m for m in markets}
        chunks, cur, size = [], [], 0
        for t in by_ticker:
            if cur and (len(cur) == 200 or size + len(t) + 1 > 6000):     # 400 tickers -> HTTP 414
                chunks.append(cur)
                cur, size = [], 0
            cur.append(t)
            size += len(t) + 1
        if cur:
            chunks.append(cur)

        def fetch(chunk):
            sent = time.time()
            try:
                # The market list allows 15s of CDN caching (cache-control max-age=15): ask for a fresh copy.
                return chunk, sent, self.http.get("/markets", {"tickers": ",".join(chunk), "limit": 1000,
                                                               "_": unique_token()})
            except Exception:
                return chunk, sent, None

        with LanePool(self.workers) as pool:
            results = list(pool.map(fetch, chunks))
        failed = set()
        for chunk, sent, d in results:
            if d is None:
                failed.update(chunk)
            got = {raw.get("ticker"): raw for raw in (d or {}).get("markets") or []}
            for t in chunk:
                m, raw = by_ticker[t], got.get(t)
                if raw is None or raw.get("status") not in ("active", "open"):
                    _unprice(m, sent)
                    continue
                if sent < getattr(m, "quoted_at", 0.0):
                    continue
                yes, no = _ask(raw.get("yes_ask_dollars")), _ask(raw.get("no_ask_dollars"))
                book_top = tuple((m.levels.get(s) or [(None,)])[0][0] for s in ("yes", "no")) if m.levels else None
                if book_top != (yes, no):
                    m.levels = {}            # depth from before no longer matches; refetched when needed
                m.yes_ask, m.no_ask = yes, no
                m.yes_ask_size, m.no_ask_size = _f(raw.get("yes_ask_size_fp")), _f(raw.get("no_ask_size_fp"))
                m.quoted_at = sent
        return failed

    def live_levels(self, ticker):
        """Current depth for buying each side of one market: {"yes": [...], "no": [...]}."""
        return buy_levels(self.http.get(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {})

    def books_by_ticker(self, tickers):
        """{ticker: depth for buying each side} for many markets, 100 order books per request."""
        out, tickers = {}, list(tickers)
        for i in range(0, len(tickers), 100):
            d = self.http.get("/markets/orderbooks", [("tickers", t) for t in tickers[i:i + 100]])
            for ob in d.get("orderbooks") or []:
                out[ob.get("ticker")] = buy_levels(ob.get("orderbook_fp") or {})
        return out


def _ask(v):
    """A listed ask, or None when there's nobody selling (listed as 0 or 1)."""
    p = _f(v)
    return p if p is not None and 0 < p < 1 else None


def _unprice(m, sent):
    """No usable book from a request sent at `sent` (failed, or the market wasn't in the reply)."""
    if sent >= getattr(m, "quoted_at", 0.0):
        m.levels, m.yes_ask, m.no_ask, m.quoted_at = {}, None, None, sent


def buy_levels(book):
    """Kalshi books list bids only. Buying YES lifts NO bids (YES price = 1 - NO bid) and
    vice versa. Returns [(price, qty)] best first for each side."""
    yes_bids = [(float(p), float(q)) for p, q in book.get("yes_dollars") or []]
    no_bids = [(float(p), float(q)) for p, q in book.get("no_dollars") or []]
    return {"yes": [(round(1 - p, 4), q) for p, q in reversed(no_bids) if q > 0],
            "no": [(round(1 - p, 4), q) for p, q in reversed(yes_bids) if q > 0]}
