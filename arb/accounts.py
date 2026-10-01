"""Your live positions on both exchanges, read with the API keys in .env, paired into arbs.

Kalshi: GET /portfolio/positions (position_fp > 0 is YES, < 0 is NO; cost = market exposure + fees).
Polymarket US: GET /v1/portfolio/positions (netPosition > 0 is YES; < 0 is a short, i.e. Buy No).
"""

from . import config
from .http import RateLimitedClient
from .model import guaranteed_payout


def _f(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError, AttributeError):
        return None


def kalshi_positions(http):
    """{ticker: {side, shares, paid}} for every open Kalshi position."""
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
            cost = (_f(p.get("market_exposure_dollars")) or 0) + (_f(p.get("fees_paid_dollars")) or 0)
            out[p["ticker"]] = {"side": "yes" if qty > 0 else "no", "shares": abs(qty), "paid": round(cost, 2),
                                "title": p["ticker"]}
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
        kc = lookup("kalshi", ticker)
        if not kc:
            continue
        for slug, pp in sorted(ppos.items()):
            if slug in used_p:
                continue
            pc = lookup("polymarket", slug)
            if not pc or (pc.game_key, pc.var) != (kc.game_key, kc.var):
                continue
            pay = guaranteed_payout([(kc, kp["side"]), (pc, pp["side"])])
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
        if self.pm_http:
            bals = self.pm_http.get("/v1/account/balances").get("balances") or []
            usd = next((b for b in bals if b.get("currency") in (None, "", "USD")), bals[0] if bals else {})
            out["polymarket"] = _f(usd.get("buyingPower")) or 0.0
        return out

    def positions(self):
        return (kalshi_positions(self.kalshi_http) if self.kalshi_http else {},
                polymarket_positions(self.pm_http) if self.pm_http else {})
