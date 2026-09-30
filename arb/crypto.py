"""Crypto price markets paired by structure, not wording.

Polymarket US Up/Down windows, Kalshi's 15-minute Up/Down markets and Kalshi's hourly price ladders
all settle on the 60-second average of CF Benchmarks' BRTI at a fixed instant. Every one of them is a
threshold on that single number, so markets closing at the same instant are grouped and the engine
finds both exact twins and cross-strike pairs (e.g. Polymarket "Up from $83,642.70" + Kalshi "NOT
above $83,700": pays $1 either way, $2 if the close lands in between).
"""

from datetime import datetime, timezone

# Kalshi series listing 15-minute up/down windows, by coin symbol as Polymarket writes it.
# Kalshi hourly price ladders ("above $X at 5 pm"), settling on the same 60-second BRTI average.
KALSHI_LADDER = {"btc": "KXBTCD", "eth": "KXETHD", "sol": "KXSOLD", "xrp": "KXXRPD", "doge": "KXDOGED",
                 "bnb": "KXBNBD", "hype": "KXHYPED"}
KALSHI_UPDOWN_15M = {"btc": "KXBTC15M", "eth": "KXETH15M", "sol": "KXSOL15M", "xrp": "KXXRP15M",
                     "doge": "KXDOGE15M", "bnb": "KXBNB15M", "ada": "KXADA15M", "bch": "KXBCH15M",
                     "hype": "KXHYPE15M", "near": "KXNEAR15M", "ton": "KXTON15M", "zec": "KXZEC15M"}


def _time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError, AttributeError):
        return None


def pm_updown(pm_markets):
    """Open Polymarket Up/Down markets (any window) whose price to beat is already known."""
    out = []
    for m in pm_markets:
        t = m.get("assetPriceTerms") or {}
        if (t.get("marketType") == "ASSET_PRICE_MARKET_TYPE_UP_DOWN" and t.get("indexSymbol") == "BRTI"
                and _num(t.get("priceToBeat")) is not None and m.get("active") and not m.get("closed")):
            out.append(m)
    return out


def _coin(m):
    return ((m.get("assetPriceTerms") or {}).get("asset") or {}).get("symbol")


def kalshi_series_for(pm_markets):
    """Kalshi series worth fetching: the 15-minute and hourly-ladder series of each coin Polymarket lists."""
    coins = {_coin(m) for m in pm_updown(pm_markets)}
    return sorted(s for c in coins for s in (KALSHI_UPDOWN_15M.get(c), KALSHI_LADDER.get(c)) if s)


def price_contracts(pm_markets, kalshi_markets, kalshi_fee, pm_default_coef, now=None):
    """Every crypto market on both sites as a contract on one number: the coin's settlement value in
    cents (the 60-second CF Benchmarks average at the close). Markets that settle at the same instant
    share a group, so the engine pairs exact twins (Polymarket 15-min Up vs Kalshi 15-min) and
    different strikes (Polymarket Up from $83,642.70 vs Kalshi "above $83,700") alike.
    kalshi_fee(series) -> taker coefficient. Returns (contracts, source)."""
    from .model import Contract
    from .nonsports import kalshi_market_obj, pm_market_obj
    now = now or datetime.now(timezone.utc)
    series_coin = {v: k for d in (KALSHI_UPDOWN_15M, KALSHI_LADDER) for k, v in d.items()}
    sides = {}                                   # (coin, end) -> {"kalshi": [...], "polymarket": [...]}
    for m in pm_updown(pm_markets):
        t, coin = m["assetPriceTerms"], _coin(m)
        end = _time(t.get("windowEnd"))
        if not end or end <= now:
            continue
        beat = _num(t["priceToBeat"])
        # Up = close >= open: in cents, x >= beat, i.e. x > beat - 0.5.
        sides.setdefault((coin, end), {"kalshi": [], "polymarket": []})["polymarket"].append(
            (m, round(beat * 100) - 0.5, f"{coin.upper()} Up ({t.get('horizon')}): close ≥ ${beat:,.2f}"))
    for k in kalshi_markets:
        series = (k.get("event_ticker") or "").split("-")[0]
        coin, end = series_coin.get(series), _time(k.get("close_time"))
        strike, kind = _num(k.get("floor_strike")), k.get("strike_type")
        if not coin or not end or end <= now or strike is None or kind not in ("greater", "greater_or_equal"):
            continue
        if k.get("status") not in ("active", "open", None):
            continue
        cents = round(strike * 100)
        line = cents + 0.5 if kind == "greater" else cents - 0.5      # "above 83,699.99" = x >= 83,700.00
        shown = (cents + 1) / 100 if kind == "greater" else cents / 100
        sides.setdefault((coin, end), {"kalshi": [], "polymarket": []})["kalshi"].append(
            (k, line, f"{coin.upper()} close ≥ ${shown:,.2f}"))
    contracts, source = [], {}
    for (coin, end), by_ex in sides.items():
        if not by_ex["kalshi"] or not by_ex["polymarket"]:
            continue
        key = f"CRYPTO:{coin.upper()} {end:%Y-%m-%d %H:%M}"
        var = ("price", coin, end.isoformat())
        label = f"{coin.upper()} price at {end:%b %d %H:%M} UTC"
        until = end.isoformat().replace("+00:00", "Z")
        for k, line, title in by_ex["kalshi"]:
            km = kalshi_market_obj(k, kalshi_fee((k.get("event_ticker") or "").split("-")[0]))
            contracts.append(Contract("kalshi", k["ticker"], key, var, ">", line, title, km.rules, False, km.fee_coef,
                                      km.close_time, False, False, label, "structural", trade_until=until))
            source[("kalshi", k["ticker"])] = km
        for m, line, title in by_ex["polymarket"]:
            pm = pm_market_obj(m, pm_default_coef)
            contracts.append(Contract("polymarket", m["slug"], key, var, ">", line, title, pm.rules, False, pm.fee_coef,
                                      until, False, False, label, "structural", trade_until=until))
            source[("polymarket", m["slug"])] = pm
    return contracts, source
