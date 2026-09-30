"""Crypto Up/Down markets paired by structure, not wording.

Polymarket US "BTC Up or Down: 15 min" and Kalshi KXBTC15M "BTC price up in next 15 mins?" are the
same contract: both settle on the 60-second average of CF Benchmarks' BRTI at the window's open and
close, and a tie counts as Up/Yes on both. A pair needs the same coin, the same window to the second
and the same price to beat (the open value), so there is nothing to approve by hand.
"""

from datetime import datetime

# Kalshi series listing 15-minute up/down windows, by coin symbol as Polymarket writes it.
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


def pm_updown_15m(pm_markets):
    """Open Polymarket 15-minute Up/Down markets whose window has started (price to beat known)."""
    out = []
    for m in pm_markets:
        t = m.get("assetPriceTerms") or {}
        if (t.get("marketType") == "ASSET_PRICE_MARKET_TYPE_UP_DOWN" and t.get("horizon") == "15m"
                and t.get("indexSymbol") == "BRTI" and _num(t.get("priceToBeat")) is not None
                and m.get("active") and not m.get("closed")):
            out.append(m)
    return out


def kalshi_series_for(pm_markets):
    """Kalshi series worth fetching: one per coin Polymarket currently lists."""
    coins = {((m.get("assetPriceTerms") or {}).get("asset") or {}).get("symbol") for m in pm_updown_15m(pm_markets)}
    return sorted(KALSHI_UPDOWN_15M[c] for c in coins if c in KALSHI_UPDOWN_15M)


def pairs(pm_markets, kalshi_markets):
    """Rows in the approved-pair format for every identical Up/Down window on both exchanges."""
    by_window = {}
    for k in kalshi_markets:
        if k.get("strike_type") != "greater_or_equal" or k.get("status") not in ("active", "open", None):
            continue
        series = k.get("event_ticker", "").split("-")[0]
        by_window[(series, _time(k.get("open_time")), _time(k.get("close_time")))] = k
    out = []
    for m in pm_updown_15m(pm_markets):
        t = m["assetPriceTerms"]
        coin = (t.get("asset") or {}).get("symbol")
        k = by_window.get((KALSHI_UPDOWN_15M.get(coin), _time(t.get("windowStart")), _time(t.get("windowEnd"))))
        if not k:
            continue
        beat, strike = _num(t.get("priceToBeat")), _num(k.get("floor_strike"))
        if strike is None or abs(beat - strike) > 0.005:
            continue                         # different open value: not the same window after all
        start, end = _time(t["windowStart"]), _time(t["windowEnd"])
        out.append({"pm": m["slug"], "kalshi": k["ticker"], "relation": "same", "structural": True,
                    "label": f"{coin.upper()} Up or Down 15 min, {start:%b %d %H:%M}–{end:%H:%M} UTC",
                    "question": m.get("question") or "", "pm_label": f"Up from ${beat:,.2f}",
                    "k_label": k.get("yes_sub_title") or "", "k_title": k.get("title") or ""})
    return out
