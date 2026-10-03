"""Combos: risk-free sports trades the pair scan (one Kalshi leg + one Polymarket leg) can't see.

  - 3-way dutch: a game that can end in a draw has three results (the anchor team wins, draw, it loses).
    Buying each result's YES, each on whichever site is cheaper, pays $1 whatever happens; buying each
    result's NO pays $2 (two of the three always win). Either is an arb when it costs less than that.
  - Same-site line arbs: two contracts on ONE site whose own prices disagree, e.g. Kalshi Over 4.5 YES at
    40c with Kalshi Over 5.5 NO at 55c: a total of 5 pays both, anything else pays one, so $1 is
    guaranteed for 95c. Both orders go to the same exchange.

Every combo's legs are on the same quantity of the same game (the pair groups' (game, quantity) keys),
and what it's guaranteed to pay comes from model.guaranteed_payout, the same math the pairs use.
"""

import math

from .engine import _ended, _parse_time, _take, describe_var, leg_text, outcome_text, row_tab, _ot_rule
from .model import NO, YES, fee_per_contract, guaranteed_payout, payout, sample_points, total_fee

SIDES = (YES, NO)
EXCHANGES = ("kalshi", "polymarket")
SPORTS_VARS = ("margin", "total", "tt", "btts", "player")    # combos are for sports quantities only
XS = range(-4, 5)                                          # enough integer margins to tell the results apart
REGIONS = {"win": lambda x: x > 0, "draw": lambda x: x == 0, "lose": lambda x: x < 0}
STRATEGIES = {"3-way": "3-way dutch", "same-site": "Same-site line arb"}


def _cost(c, side):
    """Price + taker fee for one share at the top of the book, or None if there's no seller."""
    a = c.ask.get(side)
    if a is None or a <= 0 or a >= 1:
        return None
    return a + fee_per_contract(c.fee_coef, a)


def _unique(contracts, now):
    """One contract per market (a market matched in several pairs appears once), live ones only."""
    out, seen = [], set()
    for c in contracts:
        if c.market_id not in seen and not _ended(c, now):
            seen.add(c.market_id)
            out.append(c)
    return out


def _pay(a, sa, b, sb):
    """Guaranteed payout of two legs, cached on the first contract (it depends only on terms)."""
    cache = getattr(a, "pay_cache", None)
    if cache is None:
        cache = a.pay_cache = {}
    key = ("same", sa, b.market_id, b.var, b.op, b.line, sb)
    pay = cache.get(key)
    if pay is None:
        pay = cache[key] = guaranteed_payout([(a, sa), (b, sb)])
    return pay


def _grid(contracts):
    """Every distinct result of the group's quantity, as the integer points guaranteed_payout checks
    (between two lines the payout doesn't change, so these cover every result)."""
    var = contracts[0].var
    nonneg = var[0] != "margin"
    pts = sample_points(contracts, nonneg)
    if not nonneg and any(c.no_draw for c in contracts):
        pts = [x for x in pts if x != 0]
    return tuple(pts)


def _vector(c, side, grid):
    """What one share of this side pays at each point of the grid (cached while the grid is the same)."""
    cache = getattr(c, "pay_cache", None)
    if cache is None:
        cache = c.pay_cache = {}
    hit = cache.get(("vec", side))
    if hit is None or hit[0] != grid:
        hit = cache[("vec", side)] = (grid, tuple(payout(c, side, x) for x in grid))
    return hit[1]


def region_of(c, side):
    """("win" | "draw" | "lose", exact) when this side of a winning-margin contract pays $1 on exactly one
    of the three results (exact=True) or on exactly the other two (exact=False); None otherwise (a
    spread line other than ±0.5, a moneyline paying half on a tie, ...). Cached: it depends only on terms."""
    cache = getattr(c, "pay_cache", None)
    if cache is None:
        cache = c.pay_cache = {}
    key = ("region", side)
    if key not in cache:
        cache[key] = _region(c, side)
    return cache[key]


def _region(c, side):
    if c.var[0] != "margin":
        return None
    pattern = tuple(payout(c, side, x) for x in XS)
    for name, hit in REGIONS.items():
        only = tuple(1.0 if hit(x) else 0.0 for x in XS)
        if pattern == only:
            return name, True
        if pattern == tuple(1.0 - v for v in only):
            return name, False
    return None


def _cand(strategy, legs, pay, hints):
    asks = [c.ask[s] for c, s in legs]
    edge = pay - sum(_cost(c, s) for c, s in legs)
    return {"strategy": strategy, "legs": legs, "payout": pay, "edge": edge, "asks": asks,
            "close_hint": hints[0], "start_hint": hints[1]}


def _hints(g):
    """(when the game's result is known, when it starts): the earliest Kalshi settle time and the earliest
    Polymarket date in the group (a Polymarket sports contract's date is the game's start)."""
    def first(ex):
        ts = [t for t in (_parse_time(c.close_time) for c in g.get(ex) or [] if c.close_time) if t]
        return min(ts).isoformat() if ts else None
    return first("kalshi"), first("polymarket")


def screen(groups, min_edge, now):
    """Combos on top-of-book prices with edge per set > min_edge, best first. groups: the pair groups
    {(game_key, var): {"kalshi": [...], "polymarket": [...]}}."""
    out = []
    for (_game, var), g in groups.items():
        if var[0] not in SPORTS_VARS:
            continue
        hints = None
        # Same-site line arbs: two different markets on one site. Two legs pay at most $1, so only pairs
        # costing under 1 - min_edge can qualify: cheapest first, and stop as soon as a pair costs more.
        limit, grid = 1 - min_edge, None
        for ex in EXCHANGES:
            legs = sorted(((cost, c, side) for c in _unique(g.get(ex) or [], now) for side in SIDES
                           for cost in (_cost(c, side),) if cost is not None), key=lambda t: t[0])
            for i, (ca, a, sa) in enumerate(legs):
                if 2 * ca >= limit:
                    break                      # every later pair costs at least twice this one
                for cb, b, sb in legs[i + 1:]:
                    if ca + cb >= limit:
                        break
                    if b.market_id == a.market_id:
                        continue               # both sides of one market: always costs $1 or more
                    grid = grid or _grid((g.get("kalshi") or []) + (g.get("polymarket") or []))
                    # the least the pair pays over every result, from per-share vectors (no per-pair setup)
                    if min(x + y for x, y in zip(_vector(a, sa, grid), _vector(b, sb, grid))) - ca - cb <= min_edge:
                        continue
                    pay = _pay(a, sa, b, sb)   # the exact math decides
                    if pay <= 0 or pay - ca - cb <= min_edge:
                        continue
                    hints = hints or _hints(g)
                    out.append(_cand("same-site", [(a, sa), (b, sb)], pay, hints))
        # 3-way dutch: each result on the cheaper site, all YES ($1) or all NO ($2).
        if var[0] != "margin":
            continue
        everyone = _unique(g.get("kalshi") or [], now) + _unique(g.get("polymarket") or [], now)
        if not everyone or any(c.no_draw for c in everyone):
            continue                                   # no draw possible: two results, the pairs cover it
        best = {}
        for c in everyone:
            for s in SIDES:
                r, cost = region_of(c, s), _cost(c, s)
                if r is None or cost is None:
                    continue
                if r not in best or cost < best[r][2]:
                    best[r] = (c, s, cost)
        for exact in (True, False):
            picks = [best.get((name, exact)) for name in REGIONS]
            if None in picks:
                continue
            legs = [(c, s) for c, s, _ in picks]
            pay = guaranteed_payout(legs)
            if pay <= 0 or pay - sum(cost for *_, cost in picks) <= min_edge:
                continue
            hints = hints or _hints(g)
            out.append(_cand("3-way", legs, pay, hints))
    out.sort(key=lambda c: -c["edge"])
    return out


def size(legs, levels, pay, max_n=math.inf):
    """Walk every leg's book together while each extra set still makes money. levels: each leg's
    [(price, qty)] best first. Returns {"size", "legs": [{"fills", "cost", "fee", "limit"}], "capital",
    "profit", "payout_total"} or None. Whole sets only."""
    if not levels or any(not lv for lv in levels):
        return None
    idx, rem, total = [0] * len(levels), [lv[0][1] for lv in levels], 0.0
    while all(i < len(lv) for i, lv in zip(idx, levels)) and total < max_n:
        prices = [lv[i][0] for i, lv in zip(idx, levels)]
        if pay - sum(p + fee_per_contract(c.fee_coef, p) for p, (c, _) in zip(prices, legs)) <= 0:
            break
        q = min(min(rem), max_n - total)
        total += q
        for k in range(len(levels)):
            rem[k] -= q
            if rem[k] <= 1e-9:
                idx[k] += 1
                rem[k] = levels[k][idx[k]][1] if idx[k] < len(levels[k]) else 0
    n = math.floor(total + 1e-9)
    while n > 0:                                       # rounded fees can bite on small sizes
        out = cost_at(legs, levels, n)
        capital = sum(l["cost"] + l["fee"] for l in out)
        if pay * n - capital > 0:
            return {"size": n, "legs": out, "capital": capital, "profit": pay * n - capital, "payout_total": pay * n}
        n -= 1 if n < 20 else max(1, n // 10)
    return None


def cost_at(legs, levels, n):
    """Each leg's fills, cost, rounded fee and limit for n sets."""
    out = []
    for (c, _), lv in zip(legs, levels):
        fills = _take(lv, n)
        out.append({"fills": fills, "cost": sum(p * q for p, q in fills), "fee": total_fee(c.exchange, fills, c.fee_coef),
                    "limit": fills[-1][0] if fills else None})
    return out


def scenarios(legs):
    """Every distinct result with what each leg pays per set: [{"outcome", "pays": [...], "total"}]."""
    cs = [c for c, _ in legs]
    var = cs[0].var
    nonneg = var[0] != "margin"
    pts = sample_points(cs, nonneg)
    lo_x, hi_x = (0 if nonneg else min(pts) - 1), max(pts) + 1
    skip_zero = not nonneg and any(c.no_draw for c in cs)
    regions = []
    for x in range(lo_x, hi_x + 1):
        if skip_zero and x == 0:
            continue
        pays = tuple(payout(c, s, x) for c, s in legs)
        if regions and regions[-1]["pays"] == pays and regions[-1]["hi"] in (x - 1, x - 2):
            regions[-1]["hi"] = x
        else:
            regions.append({"lo": x, "hi": x, "pays": pays})
    if regions:
        if not nonneg:
            regions[0]["lo"] = None
        regions[-1]["hi"] = None
    return [{"outcome": outcome_text(var, r["lo"], r["hi"]), "pays": list(r["pays"]), "total": sum(r["pays"])}
            for r in regions]


def warnings(cand, now):
    cs = [c for c, _ in cand["legs"]]
    sites = {c.exchange for c in cs}
    w = []
    if any(c.integer_line for c in cs):
        w.append("Whole-number line: a push is assumed to pay $0 (conservative).")
    if cs[0].var[0] == "player":
        w.append("Player prop: if the player doesn't play, each site settles at its own fair price, so the legs may "
                 "not add up to the guaranteed payout.")
    rules = {c.exchange: _ot_rule(c.rules) for c in cs if _ot_rule(c.rules)}
    if len(set(rules.values())) > 1:
        w.append("Overtime rules differ: " + ", ".join(f"{ex.capitalize()} {r}" for ex, r in sorted(rules.items())) + ".")
    start = _parse_time(cand.get("start_hint") or "")
    if start and start <= now:
        w.append("Game already started: prices move fast, and the quotes may be seconds apart.")
    if len(cs) > 2:
        w.append(f"{len(cs)} orders" + (" on both sites" if len(sites) > 1 else f" on {next(iter(sites)).capitalize()}")
                 + ": they go out at once; a leg that fills short is topped up (never above break-even) and anything "
                 "left over is sold back.")
    return w


def row_key(legs):
    return "|".join(f"{c.exchange}:{c.market_id}:{s}" for c, s in legs)


def to_row(cand, sizing, now, shards=None):
    """A dashboard row (state["combos"]). shards: {kalshi market id: exchange shard}."""
    legs, first = cand["legs"], cand["legs"][0][0]
    close = _parse_time(cand.get("close_hint") or "") if cand.get("close_hint") else None
    sites = sorted({c.exchange for c, _ in legs})
    row = {
        "kind": "combo", "strategy": cand["strategy"], "strategy_name": STRATEGIES[cand["strategy"]],
        "key": row_key(legs), "game": first.game_label or first.game_key.split(":", 1)[1],
        "league": first.game_key.split(":", 1)[0], "quantity": describe_var(first.var, first.note),
        "tab": row_tab(first), "sites": sites, "payout": cand["payout"], "edge_per_contract": round(cand["edge"], 4),
        "legs": [{**leg_text(c, s, a), "fee_coef": c.fee_coef, "shard": (shards or {}).get(c.market_id)
                  if c.exchange == "kalshi" else None} for (c, s), a in zip(legs, cand["asks"])],
        "warnings": warnings(cand, now), "scenarios": scenarios(legs),
        "closes": close.isoformat() if close else None, "decided": close.isoformat() if close else None,
        "checked": now.isoformat(),
    }
    if sizing:
        for leg, s in zip(row["legs"], sizing["legs"]):
            leg.update({"fills": s["fills"], "limit": s["limit"], "cost": round(s["cost"], 4), "fee": round(s["fee"], 4)})
        capital = sizing["capital"]
        roi = sizing["profit"] / capital if capital else 0
        days = max((close - now).total_seconds() / 86400, 0.25) if close else None
        row.update({"size": sizing["size"], "capital": round(capital, 2),
                    "fees": round(sum(s["fee"] for s in sizing["legs"]), 2), "profit": round(sizing["profit"], 2),
                    "roi": round(roi, 4), "annualized": round(roi * 365 / days, 2) if days else None})
    return row
