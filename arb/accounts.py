"""Your live positions on both exchanges, read with the API keys in .env, paired into arbs.

Kalshi: GET /portfolio/positions (position_fp > 0 is YES, < 0 is NO; cost = the position's cost basis plus
the fees on the shares held, the "Cost" Kalshi's app shows).
Polymarket US: GET /v1/portfolio/positions (netPosition > 0 is YES; < 0 is a short, i.e. Buy No).
"""

from . import config
from .http import RateLimitedClient
from .model import as_list, best_match, guaranteed_payout


def _f(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError, AttributeError):
        return None


_fee_cache = {}       # (ticker, position, lifetime fees) -> fee on the shares held


def held_fee(fills, position):
    """Fees on the contracts still held, from a market's fills (any order): each buy adds its fee, a sale
    takes out its share of the fees at the average (Kalshi's app shows the same "includes fee of").
    position: signed contracts now (YES > 0). None if the fills don't add up to it."""
    pos = fee = 0.0
    for f in sorted(fills, key=lambda f: (f.get("ts") or 0, f.get("created_time") or "")):
        n = _f(f.get("count_fp") if f.get("count_fp") is not None else f.get("count")) or 0.0
        side = f.get("outcome_side") or ((f.get("side") if f.get("action") == "buy" else
                                          {"yes": "no", "no": "yes"}.get(f.get("side"))) if f.get("action") else None)
        if side not in ("yes", "no") or n <= 0:
            return None
        d, cost = (1 if side == "yes" else -1) * n, _f(f.get("fee_cost")) or 0.0
        if pos == 0 or (pos > 0) == (d > 0):             # opening more
            pos, fee = pos + d, fee + cost
            continue
        closed = min(n, abs(pos))                          # selling: its own fee is spent, not held
        fee -= fee * closed / abs(pos)
        pos += d
        if n > closed:                                     # flipped to the other side
            fee = cost * (n - closed) / n
    return round(fee, 4) if abs(pos - position) < 0.005 else None


def _position_fee(http, p, qty):
    """The fee on a position's held contracts. Kalshi's positions list only gives lifetime fees for the
    market (sold shares included), so the held part comes from the fills, once per position change."""
    lifetime = _f(p.get("fees_paid_dollars")) or 0.0
    direct = _f(p.get("position_fee_cost_dollars"))
    if direct is not None:
        return direct
    if lifetime <= 0:
        return 0.0
    key = (p["ticker"], qty, lifetime)
    if key not in _fee_cache:
        fills, cursor = [], None
        try:
            while True:
                params = {"ticker": p["ticker"], "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                d = http.get("/portfolio/fills", params)
                fills += d.get("fills") or []
                cursor = d.get("cursor")
                if not cursor or not d.get("fills"):
                    break
        except Exception:
            return lifetime                                     # tried again next read
        fee = held_fee(fills, qty)
        _fee_cache[key] = lifetime if fee is None else fee    # fills don't add up: lifetime (never too low)
    return _fee_cache[key]


def kalshi_positions(http):
    """{ticker: {side, shares, paid, fees}} for every open Kalshi position."""
    out, cursor = {}, None
    while True:
        params = {"limit": 1000, "count_filter": "position"}
        if cursor:
            params["cursor"] = cursor
        d = http.get("/portfolio/positions", params)
        rows = d.get("market_positions") or []
        for p in rows:
            qty = _f(p.get("position_fp") if p.get("position_fp") is not None else p.get("position"))
            if not qty:
                continue
            fees = _position_fee(http, p, qty)
            cost = (_f(p.get("market_exposure_dollars")) or 0) + fees
            out[p["ticker"]] = {"side": "yes" if qty > 0 else "no", "shares": abs(qty), "paid": round(cost, 2),
                                "fees": round(fees, 2), "title": p["ticker"]}
        cursor = d.get("cursor")
        if not cursor or not rows:
            return out


def polymarket_positions(http):
    """{slug: {side, shares, paid}} for every open Polymarket US position. A short (Buy No) costs
    1 - price per share; if the API reports its cost as the (negative) sale proceeds, convert it."""
    out, cursor = {}, None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        d = http.get("/v1/portfolio/positions", params)
        for key, p in (d.get("positions") or {}).items():
            if p.get("expired"):
                continue
            net = _f(p.get("netPositionDecimal") if p.get("netPositionDecimal") is not None else p.get("netPosition"))
            if not net:
                continue
            meta = p.get("marketMetadata") or {}
            slug = meta.get("slug") or meta.get("marketSlug") or key
            shares, cost = abs(net), _f(p.get("cost"))
            if cost is None:
                paid, estimated = None, True
            elif net < 0 and cost < 0:
                paid, estimated = shares - abs(cost), False     # proceeds received -> 1 - price per share
            else:
                paid, estimated = abs(cost), net < 0
            out[slug] = {"side": "yes" if net > 0 else "no", "shares": shares,
                         "paid": round(paid, 2) if paid is not None else None, "paid_estimated": estimated,
                         "title": meta.get("title") or meta.get("question") or slug}
        cursor = d.get("nextCursor")
        if d.get("eof") or not cursor:
            return out


def pair_positions(kpos, ppos, lookup):
    """Pair Kalshi and Polymarket positions that together are a known arb.
    lookup(exchange, market_id) -> Contract or None (the scanner's watched contracts).
    Returns (arbs, unpaired): arbs as [(ticker, slug, kalshi contract, pm contract, payout)]."""
    arbs, used_p = [], set()
    for ticker, kp in sorted(kpos.items()):
        kcs = as_list(lookup("kalshi", ticker))
        if not kcs:
            continue
        for slug, pp in sorted(ppos.items()):
            if slug in used_p:
                continue
            m = best_match(kcs, lookup("polymarket", slug), kp["side"], pp["side"])
            if not m:
                continue
            kc, pc, pay = m
            if pay > 0:
                arbs.append((ticker, slug, kc, pc, pay))
                used_p.add(slug)
                break
    used_k = {a[0] for a in arbs}
    unpaired = ([{"exchange": "kalshi", "market_id": t, **p} for t, p in sorted(kpos.items()) if t not in used_k] +
                [{"exchange": "polymarket", "market_id": s, **p} for s, p in sorted(ppos.items()) if s not in used_p])
    return arbs, unpaired


class Accounts:
    """Signed read access to both accounts, built from the keys in .env (either may be missing)."""

    def __init__(self, kalshi_client):
        self.kalshi_http = kalshi_client.http if kalshi_client.http.signer else None
        self.pm_http = None
        if config.POLYMARKET_KEY_ID and config.POLYMARKET_SECRET_KEY:
            try:
                from .polymarket_auth import load_signer
                self.pm_http = RateLimitedClient(config.POLYMARKET_TRADE_BASE, 5.0,
                                                 signer=load_signer(config.POLYMARKET_KEY_ID,
                                                                    config.POLYMARKET_SECRET_KEY))
            except Exception:
                self.pm_http = None

    @property
    def missing(self):
        return [name for name, h in (("Kalshi", self.kalshi_http), ("Polymarket", self.pm_http)) if not h]

    def balances(self):
        """Cash you can trade with right now, per exchange ({} for a site without a key).
        Kalshi: balance (balance_dollars, or balance in cents). Polymarket: buying power, which is
        what a Buy No (1 - price per share) or a Buy Yes draws on."""
        out = {}
        if self.kalshi_http:
            d = self.kalshi_http.get("/portfolio/balance")
            dollars = _f(d.get("balance_dollars"))
            out["kalshi"] = dollars if dollars is not None else (_f(d.get("balance")) or 0) / 100
            # Kalshi splits cash by exchange shard; an order only uses its market's shard's cash.
            shards = {str(b.get("exchange_index", 0)): _f(b.get("balance")) or 0.0
                      for b in d.get("balance_breakdown") or []}
            if shards:
                out["kalshi_shards"] = shards
        if self.pm_http:
            bals = self.pm_http.get("/v1/account/balances").get("balances") or []
            usd = next((b for b in bals if b.get("currency") in (None, "", "USD")), bals[0] if bals else {})
            out["polymarket"] = _f(usd.get("buyingPower")) or 0.0
        return out

    def positions(self):
        return (kalshi_positions(self.kalshi_http) if self.kalshi_http else {},
                polymarket_positions(self.pm_http) if self.pm_http else {})
