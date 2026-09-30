"""Find cross-exchange arbitrage pairs and size them against real order-book depth."""

import copy
import math
import re
from collections import defaultdict
from datetime import datetime, timezone

from .model import YES, NO, fee_per_contract, guaranteed_payout, payout, sample_points, total_fee

SIDES = (YES, NO)
EXCHANGE_NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket"}


def group_pairs(contracts):
    """{(game_key, var): {"kalshi": [...], "polymarket": [...]}} for quantities both exchanges list."""
    groups = defaultdict(lambda: {"kalshi": [], "polymarket": []})
    for c in contracts:
        groups[(c.game_key, c.var)][c.exchange].append(c)
    return {k: v for k, v in groups.items() if v["kalshi"] and v["polymarket"]}


def screen(groups, min_edge):
    """Top-of-book screen. Returns candidates with per-contract edge > min_edge, best first.
    Edge = guaranteed payout - both prices - both unrounded taker fees."""
    out = []
    now = datetime.now(timezone.utc)
    for (game_key, var), g in groups.items():
        for k in g["kalshi"]:
            if _ended(k, now):
                continue
            for p in g["polymarket"]:
                if _ended(p, now):
                    continue
                for sk in SIDES:
                    ak = k.ask[sk]
                    if ak is None or ak <= 0 or ak >= 1:
                        continue
                    for sp in SIDES:
                        ap = p.ask[sp]
                        if ap is None or ap <= 0 or ap >= 1:
                            continue
                        if ak + ap >= 1 - min_edge:      # guaranteed payout is at most ~1
                            continue
                        pay = guaranteed_payout([(k, sk), (p, sp)])
                        if pay <= 0:
                            continue
                        edge = pay - ak - ap - fee_per_contract(k.fee_coef, ak) - fee_per_contract(p.fee_coef, ap)
                        if edge > min_edge:
                            out.append({"k": k, "sk": sk, "p": p, "sp": sp, "payout": pay, "edge": edge,
                                        "ak": ak, "ap": ap})
    out.sort(key=lambda c: -c["edge"])
    return out


def _ended(c, now):
    """True once a contract's trading window is over: its outcome is known and old quotes are stale."""
    t = _parse_time(c.trade_until) if c.trade_until else None
    return bool(t and t <= now)


def maker_quote(pm, side, max_spread=1.0):
    """Where to rest the Polymarket leg as a maker: one tick better than the best price on that
    side, or joining it when the spread is a single tick. Buy YES rests a bid; Buy NO rests an offer
    to sell YES (costing 1 - price). Returns (yes_price_to_post, cost_per_share) or None."""
    ask = pm.yes_ask
    bid = round(1 - pm.no_ask, 4) if pm.no_ask is not None else None
    if ask is None or bid is None or ask - bid > max_spread + 1e-9:
        return None
    t = getattr(pm, "tick", 0.01) or 0.01
    if side == YES:
        price = bid + t if bid + t < ask - 1e-9 else bid
        return round(price, 4), round(price, 4)
    price = ask - t if ask - t > bid + 1e-9 else ask
    return round(price, 4), round(1 - price, 4)


def maker_candidate(cand, pm, rebate, max_spread=1.0):
    """The same pair with the Polymarket leg resting as a maker: it earns `rebate` x p x (1-p)
    instead of paying the taker fee, at the price maker_quote picks. The Kalshi leg is still taken."""
    q = maker_quote(pm, cand["sp"], max_spread)
    if not q or not 0 < q[1] < 1:
        return None
    k = cand["k"]
    p = copy.copy(cand["p"])
    p.fee_coef = -rebate                      # a negative fee: the rebate
    edge = cand["payout"] - cand["ak"] - q[1] - fee_per_contract(k.fee_coef, cand["ak"]) + rebate * q[1] * (1 - q[1])
    return {**cand, "p": p, "ap": q[1], "edge": edge,
            "maker": {"post_yes_price": q[0], "cost": q[1], "tick": getattr(pm, "tick", 0.01)}}


def hedge_limit(payout, maker_cost, rebate, kalshi_coef):
    """Highest Kalshi price (whole cents) that still locks a profit once the maker leg filled at
    maker_cost per share."""
    room = payout - maker_cost + rebate * maker_cost * (1 - maker_cost)
    best = None
    for c in range(1, 100):
        price = c / 100
        if price + fee_per_contract(kalshi_coef, price) < room - 1e-9:
            best = price
    return best


def _take(levels, n):
    fills, left = [], n
    for price, qty in levels:
        if left <= 0:
            break
        q = min(qty, left)
        fills.append((price, q))
        left -= q
    return fills


def size_opportunity(cand, levels_k, levels_p):
    """Walk both books while each extra contract still has positive edge. Returns sizing
    dict or None. Sizes are whole contracts."""
    k, p, pay = cand["k"], cand["p"], cand["payout"]
    i = j = 0
    rem_k = levels_k[0][1] if levels_k else 0
    rem_p = levels_p[0][1] if levels_p else 0
    total = 0.0
    while i < len(levels_k) and j < len(levels_p):
        pk, pp = levels_k[i][0], levels_p[j][0]
        edge = pay - pk - pp - fee_per_contract(k.fee_coef, pk) - fee_per_contract(p.fee_coef, pp)
        if edge <= 0:
            break
        q = min(rem_k, rem_p)
        total += q
        rem_k -= q
        rem_p -= q
        if rem_k <= 1e-9:
            i += 1
            rem_k = levels_k[i][1] if i < len(levels_k) else 0
        if rem_p <= 1e-9:
            j += 1
            rem_p = levels_p[j][1] if j < len(levels_p) else 0
    n = math.floor(total + 1e-9)
    # Trim until profit after rounded fees is positive (rounding can bite on tiny sizes).
    while n > 0:
        fk, fp = _take(levels_k, n), _take(levels_p, n)
        cost_k, cost_p = sum(a * b for a, b in fk), sum(a * b for a, b in fp)
        fee_k, fee_p = total_fee(k.exchange, fk, k.fee_coef), total_fee(p.exchange, fp, p.fee_coef)
        profit = pay * n - cost_k - cost_p - fee_k - fee_p
        if profit > 0:
            return {"size": n, "cost_k": cost_k, "cost_p": cost_p, "fee_k": fee_k, "fee_p": fee_p,
                    "worst_k": fk[-1][0], "worst_p": fp[-1][0], "profit": profit,
                    "payout_total": pay * n, "fills_k": fk, "fills_p": fp}
        n -= 1 if n < 20 else max(1, n // 10)
    return None


# ---- presentation ---------------------------------------------------------------------

def _parse_time(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _ot_rule(text):
    t = text.lower()
    if re.search(r"overtime (is )?included|including overtime|includes overtime|extra innings", t):
        return "incl. OT"
    if re.search(r"regulation|excluding overtime|overtime (is )?not included", t):
        return "regulation only"
    return None


def _mid(c):
    """Implied YES probability from the two asks (YES ask and 1 - NO ask)."""
    y, n = c.ask.get(YES), c.ask.get(NO)
    if y is None or n is None or not (0 < y < 1 and 0 < n < 1):
        return None
    return (y + (1 - n)) / 2


# Where a market's number comes from. Two sites settling "Bitcoin above $X at 5pm" on different
# feeds (or a temperature on different stations) can land on opposite sides of the line.
SETTLEMENT_SOURCES = {
    "price": {"CF Benchmarks": r"cf benchmarks|\bbrti\b|real[- ]time index", "Binance": r"binance",
              "Coinbase": r"coinbase", "Chainlink": r"chainlink", "Kraken": r"kraken", "Pyth": r"\bpyth\b",
              "Bloomberg": r"bloomberg", "S&P Dow Jones Indices": r"s&p dow jones|spglobal"},
    "weather": {"National Weather Service": r"national weather service|\bnws\b|nowdata|climatological report",
                "Weather Underground": r"weather underground|wunderground", "AccuWeather": r"accuweather",
                "Meteostat": r"meteostat"},
    "chart": {"Spotify": r"spotify", "Luminate/Billboard": r"luminate|billboard", "Apple Music": r"apple music"},
    "wealth": {"Forbes": r"forbes", "Bloomberg Billionaires": r"bloomberg billionaires"},
    "ai leaderboard": {"LiveBench": r"livebench", "LMArena": r"\barena\b|lmarena", "Artificial Analysis":
                       r"artificial analysis"},
}


def settlement_sources(text):
    t = (text or "").lower()
    return {fam: {name for name, rx in names.items() if re.search(rx, t)} for fam, names in SETTLEMENT_SOURCES.items()}


def source_mismatch(k_rules, p_rules):
    """'Kalshi: CF Benchmarks; Polymarket: Binance' when the two rules name different sources for
    the same kind of number, else None."""
    sk, sp = settlement_sources(k_rules), settlement_sources(p_rules)
    for fam in SETTLEMENT_SOURCES:
        if sk[fam] and sp[fam] and not sk[fam] & sp[fam]:
            return f"Kalshi: {', '.join(sorted(sk[fam]))}; Polymarket: {', '.join(sorted(sp[fam]))}"
    return None


ANNOUNCE_K = re.compile(r"announce", re.I)
ANNOUNCE_EXCLUDED = re.compile(r"announcements?\b[^.]{0,60}\b(?:do not|does not|will not|won't) (?:qualify|count)", re.I)


def one_way_trap(k, sk, p):
    """Kalshi resolves YES on an announcement ("leave office or announce leaving") while Polymarket
    needs the real thing. Kalshi YES + Polymarket NO is then still safe; holding Kalshi NO is not:
    an announcement without the event loses both legs."""
    return (sk == NO and bool(ANNOUNCE_K.search(k.rules or "")) and bool(ANNOUNCE_EXCLUDED.search(p.rules or ""))
            and p.op == ">")


def exact_hedge(k, sk, p, sp):
    """True when the two legs are exact opposites: every possible result pays exactly $1 in
    total, never $2 (cross-line) and never $0 (push)."""
    nonneg = k.var[0] != "margin"
    pts = sample_points([k, p], nonneg)
    if not nonneg and (k.no_draw or p.no_draw):
        pts = [x for x in pts if x != 0]
    return {round(payout(k, sk, x) + payout(p, sp, x), 6) for x in pts} == {1.0}


def not_simple_reasons(cand):
    """Why a pair is not a plain two-leg "YES here, NO there" trade; empty list = simple."""
    k, p, sk, sp = cand["k"], cand["p"], cand["sk"], cand["sp"]
    why = []
    if k.integer_line or p.integer_line:
        why.append("whole-number line (push possible)")
    elif not exact_hedge(k, sk, p, sp):
        why.append("different lines (some results pay $2)")
    if cand["edge"] > SUSPICIOUS_EDGE:
        why.append("too good to be true")
    if k.var[0] == "event":
        if k.note == "auto":
            why.append("auto-matched, not verified")
        if source_mismatch(k.rules, p.rules):
            why.append("different settlement sources")
        if one_way_trap(k, sk, p):
            why.append("Kalshi also resolves YES on an announcement")
    else:
        ok, op = _ot_rule(k.rules), _ot_rule(p.rules)
        if ok and op and ok != op:
            why.append("overtime rules differ")
    return why


def trade_warnings(cand):
    """Warnings that depend on which sides the trade takes."""
    if cand["k"].var[0] == "event" and one_way_trap(cand["k"], cand["sk"], cand["p"]):
        return ["ONE-WAY RULES: Kalshi also resolves YES if they only ANNOUNCE it; Polymarket needs it to actually "
                "happen. This trade holds Kalshi NO, so an announcement without the event loses both legs. The reverse "
                "trade (Kalshi YES + Polymarket NO) would be safe."]
    return []


def warnings_for(k, p, now):
    w = []
    if k.var[0] == "event":
        if k.note == "structural":
            w.append("Paired by contract terms: same coin, same CF Benchmarks index, same window and same price "
                     "to beat on both exchanges. Quotes move fast in the last minutes of a window.")
        elif k.note == "auto":
            w.append("AUTO-MATCHED, NOT VERIFIED: the scanner paired these by wording. Open the math and read both "
                     "rules; if they aren't the same question, click Wrong match. A wrong match looks like a sure "
                     "arb but can lose on both sides.")
        else:
            w.append("You matched these markets. Before trading, re-read both rules: same event, same deadline "
                     "(including time zone), same resolution source.")
        km, pmid = _mid(k), _mid(p)
        if km is not None and pmid is not None:
            if p.op == "<" and abs(km - pmid) < 0.08 and abs(km - (1 - pmid)) > 0.2:
                w.append("PRICES CONTRADICT THIS MATCH: both sites price it about the same, which means Same, but "
                         "it's saved as Opposite. Remove it and re-approve as Same if it's the same outcome.")
            if p.op == ">" and abs(km - (1 - pmid)) < 0.08 and abs(km - pmid) > 0.2:
                w.append("PRICES CONTRADICT THIS MATCH: the prices mirror each other, which means Opposite (or "
                         "different questions), but it's saved as Same.")
        src = source_mismatch(k.rules, p.rules)
        if src:
            w.append(f"DIFFERENT SETTLEMENT SOURCES ({src}). The two feeds can differ at the deadline, so a "
                     f"result right at the line can lose both legs. Only safe when the line is far from the "
                     f"current value.")
        dk, dp = _parse_time(k.close_time), _parse_time(p.close_time)
        if dk and dp and abs((dk - dp).days) > 30:     # Polymarket end dates run ~15 days past the event
            w.append(f"Close dates differ by {abs((dk - dp).days)} days (Kalshi {dk:%b %d, %Y}, Polymarket "
                     f"{dp:%b %d, %Y}); money may be tied up until the later one.")
        return w
    if k.var[0] == "price":
        w.append("Both sites settle on the 60-second average of CF Benchmarks' BRTI at the close. Kalshi averages "
                 "the 60 seconds before the close and Polymarket the 60 prices ending at it, so the two can differ "
                 "by a second's move: only a close within a few dollars of a line could split them.")
        return w
    if k.integer_line or p.integer_line:
        w.append("Whole-number line: a push is assumed to pay $0 on both sides (conservative).")
    ok, op = _ot_rule(k.rules), _ot_rule(p.rules)
    if ok and op and ok != op:
        w.append(f"Overtime rules differ: Kalshi {ok}, Polymarket {op}.")
    start = _parse_time(p.close_time)
    if start and start <= now:
        w.append("Game already started: prices move fast, and the quotes may be seconds apart.")
    return w


def leg_text(c, side, price):
    if c.exchange == "polymarket":
        action = f"Buy {side.upper()}"       # Polymarket US: Buy No is a short of YES costing 1 - bid
    else:
        action = f"Buy {side.upper()}"
    return {"exchange": EXCHANGE_NAMES[c.exchange], "market_id": c.market_id, "title": c.title,
            "side": side, "action": action, "price": round(price, 4)}


def describe_var(var, note=""):
    if var[0] == "price":
        return f"{var[1].upper()} settlement price (CF Benchmarks 60-second average)"
    if var[0] == "event":
        return {"structural": "Paired by contract terms", "auto": "Auto-matched by wording"}.get(note, "Your approved match")
    kind, period = var[0], var[1]
    per = "" if period == "FG" else f" ({period})"
    if kind == "margin":
        return f"Winning margin{per}"
    if kind == "total":
        return f"Combined total{per}"
    return f"{var[2]} team total{per}"


def _range_text(lo, hi, unit_fmt):
    """Integer range [lo, hi] (None = unbounded) as text."""
    if lo is None and hi is None:
        return "any result"
    if lo is None:
        return f"{unit_fmt(hi)} or less"
    if hi is None:
        return f"{unit_fmt(lo)} or more"
    if lo == hi:
        return f"exactly {unit_fmt(lo)}"
    return f"{unit_fmt(lo)} to {unit_fmt(hi)}"


PERIOD_NAMES = {"1H": "1st half", "2H": "2nd half", "1Q": "1st quarter", "2Q": "2nd quarter",
                "3Q": "3rd quarter", "4Q": "4th quarter", "1P": "1st period", "2P": "2nd period",
                "3P": "3rd period", "F5": "First 5 innings"}


def _dollars(c):
    return f"${c / 100:,.2f}"


def outcome_text(var, lo, hi):
    if var[0] == "price":                   # x in cents
        coin = var[1].upper()
        if lo is None:
            return f"{coin} closes below {_dollars(hi + 1)}"
        if hi is None:
            return f"{coin} closes at {_dollars(lo)} or more"
        return f"{coin} closes {_dollars(lo)} to {_dollars(hi)}"
    if var[0] == "event":
        return "It happens (Kalshi market resolves YES)" if (lo or 0) >= 1 else "It doesn't happen (Kalshi resolves NO)"
    per = var[1]
    prefix = "" if per == "FG" else PERIOD_NAMES.get(per, f"Inning {per[1:]}" if per.startswith("I") else per) + ": "
    if var[0] in ("total", "tt"):
        who = "Combined score" if var[0] == "total" else f"{var[2]} scores"
        return f"{prefix}{who} {_range_text(None if lo == 0 and hi != 0 else lo, hi, str)}"
    a = var[2]
    if lo is not None and hi is not None and lo == hi == 0:
        return f"{prefix}Tie"
    if lo is not None and lo >= 1:
        return f"{prefix}{a} wins" + ("" if lo == 1 and hi is None else f" by {_range_text(lo, hi, str)}")
    if hi is not None and hi <= -1:
        lo_m, hi_m = -hi, None if lo is None else -lo
        return f"{prefix}{a} loses" + ("" if lo_m == 1 and hi_m is None else f" by {_range_text(lo_m, hi_m, str)}")
    # Range spanning zero.
    lose = "loses" if lo is None else f"loses by {-lo} or less" if lo < 0 else None
    win = "wins" if hi is None else f"wins by {hi} or less" if hi > 0 else None
    parts = [p for p in (lose, "ties", win) if p]
    return f"{prefix}{a} " + ", ".join(parts[:-1]) + " or " + parts[-1]


def outcome_table(k, sk, p, sp):
    """Every distinct outcome region with what each leg pays per contract."""
    var = k.var
    if var[0] == "price":
        # Prices in cents run to millions: evaluate one point per region between the lines.
        cuts = sorted({math.ceil(c.line) for c in (k, p)})
        starts = [None] + cuts
        regions = []
        for i, lo in enumerate(starts):
            x = cuts[0] - 1 if lo is None else lo
            hi = cuts[i] - 1 if i < len(cuts) else None
            pay = (payout(k, sk, x), payout(p, sp, x))
            if regions and regions[-1]["pay"] == pay:
                regions[-1]["hi"] = hi
            else:
                regions.append({"lo": lo, "hi": hi, "pay": pay})
        return [{"outcome": outcome_text(var, r["lo"], r["hi"]), "kalshi": r["pay"][0], "polymarket": r["pay"][1],
                 "total": r["pay"][0] + r["pay"][1]} for r in regions]
    nonneg = var[0] != "margin"
    pts = sample_points([k, p], nonneg)
    lo_x, hi_x = (0 if nonneg else min(pts) - 1), max(pts) + 1
    skip_zero = not nonneg and (k.no_draw or p.no_draw)
    regions = []
    for x in range(lo_x, hi_x + 1):
        if skip_zero and x == 0:
            continue
        pay = (payout(k, sk, x), payout(p, sp, x))
        if regions and regions[-1]["pay"] == pay and regions[-1]["hi"] in (x - 1, x - 2):
            regions[-1]["hi"] = x
        else:
            regions.append({"lo": x, "hi": x, "pay": pay})
    if regions:
        if not nonneg:
            regions[0]["lo"] = None
        regions[-1]["hi"] = None
    return [{"outcome": outcome_text(var, r["lo"], r["hi"]), "kalshi": r["pay"][0], "polymarket": r["pay"][1],
             "total": r["pay"][0] + r["pay"][1]} for r in regions]


# Dashboard tabs, from the Polymarket category of a non-sports pair.
TABS = {"politics": "Politics", "geopolitics": "Politics", "culture": "Culture", "macro": "Economics",
        "finance": "Finance", "technology": "Tech & science", "science": "Tech & science", "climate": "Weather",
        "crypto": "Crypto"}


def row_tab(k):
    if k.var[0] == "price":
        return "Crypto"
    if k.var[0] != "event":
        return "Sports"
    return TABS.get(k.game_key.split(":", 1)[0].lower(), "Other")


SUSPICIOUS_EDGE = 0.10    # per contract; real cross-exchange arbs are usually a cent or two


def explain_suspicious(cand, now):
    """Plain-English math for an arb that's too good to be true."""
    k, p = cand["k"], cand["p"]
    km, pm = _mid(k), _mid(p)
    lines = [f"TOO GOOD TO BE TRUE: {cand['edge'] * 100:.0f}¢ profit per contract. Real arbs between these sites are "
             f"usually 1-2¢."]
    if k.var[0] == "event" and km is not None and pm is not None:
        pm_same = pm if p.op == ">" else 1 - pm
        lines.append(f"The math: Kalshi prices its outcome at {km:.0%}; Polymarket prices the outcome it was matched to "
                     f"at {pm_same:.0%}. Two big exchanges almost never disagree by {abs(km - pm_same) * 100:.0f} points "
                     f"on the same question, so these are almost certainly DIFFERENT outcomes. Compare exactly: "
                     f"Kalshi \"{k.title}\" vs Polymarket \"{p.title}\". If they differ, click Wrong match.")
    elif km is not None and pm is not None:
        start = _parse_time(p.close_time)
        why = ("the game is in progress and one site's quote is lagging" if start and start <= now
               else "one of the quotes is stale or the market was just re-listed")
        lines.append(f"The math: Kalshi's YES implies {km:.0%}, Polymarket's YES implies {pm:.0%} for linked outcomes. "
                     f"Most likely {why}. Press Check live depth; if the gap is gone, it was never real.")
    return lines


def to_row(cand, sizing, now):
    k, p = cand["k"], cand["p"]
    too_good = cand["edge"] > SUSPICIOUS_EDGE
    # Money is tied up until the later of the two settles.
    closes = [t for t in (_parse_time(k.close_time), _parse_time(p.close_time)) if t]
    close = max(closes) if k.var[0] == "event" and closes else (closes[0] if closes else None)
    row = {
        "game": k.game_label or k.game_key.split(":", 1)[1], "league": k.game_key.split(":", 1)[0],
        "quantity": describe_var(k.var, k.note), "payout": cand["payout"], "edge_per_contract": round(cand["edge"], 4),
        "legs": [leg_text(k, cand["sk"], cand["ak"]), leg_text(p, cand["sp"], cand["ap"])],
        "rules": {"kalshi": k.rules, "polymarket": p.rules},
        "warnings": (explain_suspicious(cand, now) if too_good else []) + trade_warnings(cand) + warnings_for(k, p, now),
        "tab": row_tab(k),
        "pm_short": cand["sp"] == NO,
        "trade_until": k.trade_until or p.trade_until or None,
        "not_simple": not_simple_reasons(cand),
        "suspicious": too_good, "closes": close.isoformat() if close else None,
        "depth": cand.get("depth"),
        "fee_coef": {"kalshi": k.fee_coef, "polymarket": p.fee_coef},
    }
    if k.var[0] == "event":
        row["pair"] = {"pm": p.market_id, "kalshi": k.market_id, "auto": k.note == "auto"}
    if sizing:
        # Everything the dashboard needs to show bet sizes and re-size to a smaller budget
        # with the exchanges' exact fee rounding.
        row["book"] = {"kalshi": sizing["fills_k"], "polymarket": sizing["fills_p"],
                       "coef_kalshi": k.fee_coef, "coef_polymarket": p.fee_coef}
        row["scenarios"] = outcome_table(k, cand["sk"], p, cand["sp"])
        capital = sizing["cost_k"] + sizing["cost_p"] + sizing["fee_k"] + sizing["fee_p"]
        roi = sizing["profit"] / capital if capital else 0
        days = max((close - now).total_seconds() / 86400, 0.25) if close else None
        row.update({
            "size": sizing["size"], "capital": round(capital, 2), "fees": round(sizing["fee_k"] + sizing["fee_p"], 2),
            "profit": round(sizing["profit"], 2), "roi": round(roi, 4),
            "annualized": round(roi * 365 / days, 2) if days else None,
            "worst_prices": [sizing["worst_k"], sizing["worst_p"]],
        })
    return row


def now_utc():
    return datetime.now(timezone.utc)
