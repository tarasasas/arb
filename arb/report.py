"""What happened to some trades: your app trade log plus Kalshi's own record of fills and settlements.

  python -m arb.report NPB          (or double-click trade-report.bat; default search: NPB)

Searches trades.jsonl (every Make trade / Fast trade / Auto-trade) and, with your Kalshi key, your
Kalshi fills and settlements from the last DAYS days, for markets whose ticker, slug or title
contains the search text. Prints a readable report and saves it to trade-report.txt (git-ignored).
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import config

DAYS = 14
OUT = config.PROJECT_ROOT / "trade-report.txt"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _match(text, q):
    return q.lower() in (text or "").lower()


def app_trades(q, path=config.TRADES_LOG):
    """Trades placed by the app whose legs match q, oldest first."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            t = json.loads(line)
        except ValueError:
            continue
        legs = (t.get("plan") or {}).get("legs") or {}
        if any(_match(l.get("market_id"), q) or _match(l.get("title"), q) for l in legs.values()):
            out.append(t)
    return out


def describe_app_trade(t):
    plan = t.get("plan") or {}
    legs = plan.get("legs") or {}
    lines = [f"{(t.get('started') or '')[:19]}  status: {t.get('status')}  planned {plan.get('size')} pairs, "
             f"${_f(plan.get('capital')):.2f} in, expected +${_f(plan.get('expected_profit')):.2f}"
             + (f"  (first: {plan.get('first')})" if plan.get("first") else "")]
    for ex, l in legs.items():
        lines.append(f"    plan {ex:10s} Buy {str(l.get('side')).upper():3s} ≤ {_f(l.get('limit')):.3f}  "
                     f"cash there ${_f(l.get('balance')):.2f}  {l.get('market_id')}  {l.get('title')}")
    for tr in plan.get("shard_transfers") or []:
        lines.append(f"    moved ${_f(tr.get('amount')):.2f} on Kalshi from shard {tr.get('from')} to {tr.get('to')}")
    for o in t.get("orders") or []:
        what = (f"filled {_f(o.get('qty')):g} for ${_f(o.get('amount')):.2f} + ${_f(o.get('fee')):.2f} fee"
                if not o.get("error") else f"ERROR {o.get('error')}")
        lines.append(f"    {o.get('kind', ''):8s} {o.get('exchange', ''):10s} {str(o.get('side')).upper():3s} {what}")
    for m in t.get("missed") or []:
        lines.append(f"    MISSED   {m.get('exchange', ''):10s} {m.get('why', '')}")
    return "\n".join(lines)


def _pages(http, path, key, params):
    cursor, out = None, []
    for _ in range(50):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        d = http.get(path, p)
        out += d.get(key) or []
        cursor = d.get("cursor")
        if not cursor or not d.get(key):
            break
    return out


def kalshi_record(http, q, days=DAYS):
    """{ticker: {"fills": [...], "settlement": {...}}} for tickers containing q."""
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    by = defaultdict(lambda: {"fills": [], "settlement": None})
    for f in _pages(http, "/portfolio/fills", "fills", {"limit": 1000, "min_ts": since}):
        t = f.get("ticker") or f.get("market_ticker") or ""
        if _match(t, q):
            by[t]["fills"].append(f)
    for s in _pages(http, "/portfolio/settlements", "settlements", {"limit": 1000, "min_ts": since}):
        if _match(s.get("ticker"), q):
            by[s["ticker"]]["settlement"] = s
    return dict(by)


def describe_kalshi(ticker, rec):
    lines = [ticker]
    cost = fees = 0.0
    for f in sorted(rec["fills"], key=lambda f: f.get("created_time") or ""):
        n, side, action = _f(f.get("count_fp") or f.get("count")), f.get("side"), f.get("action")
        price = _f(f.get("yes_price_dollars") if side == "yes" else f.get("no_price_dollars"))
        fee = _f(f.get("fee_cost"))
        cost += (1 if action == "buy" else -1) * n * price
        fees += fee
        lines.append(f"    {str(f.get('created_time'))[:19]}  {action} {n:g} {str(side).upper()} @ {price:.3f}  fee ${fee:.2f}"
                     f"  {'taker' if f.get('is_taker') else 'maker'}  shard {f.get('exchange_index', '?')}")
    s = rec["settlement"]
    if s:
        rev = _f(s.get("revenue")) / 100
        lines.append(f"    settled {str(s.get('settled_time'))[:19]}: result {s.get('market_result')}, paid out ${rev:.2f}"
                     f"  (held {_f(s.get('yes_count_fp')):g} YES / {_f(s.get('no_count_fp')):g} NO)")
        lines.append(f"    net on Kalshi: ${rev - cost - fees:+.2f}  (bought ${cost:.2f}, fees ${fees:.2f})")
    else:
        lines.append(f"    not settled yet; bought ${cost:.2f} net, fees ${fees:.2f}")
    return "\n".join(lines)


def build(q, http=None):
    parts = [f"Trade report for '{q}', {datetime.now():%Y-%m-%d %H:%M}", ""]
    trades = app_trades(q)
    parts.append(f"== Trades placed by the app (trades.jsonl): {len(trades)}")
    parts += [describe_app_trade(t) for t in trades] or ["    none"]
    parts.append("")
    if http is not None:
        rec = kalshi_record(http, q)
        parts.append(f"== Kalshi's record (fills and settlements, last {DAYS} days): {len(rec)} markets")
        parts += [describe_kalshi(t, r) for t, r in sorted(rec.items())] or ["    none"]
    else:
        parts.append("== Kalshi's record: skipped (no Kalshi API key in .env)")
    parts.append("")
    parts.append("Polymarket: check polymarket.us > Portfolio > History for the same markets.")
    return "\n".join(parts)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    q = (argv[0] if argv else "") or "NPB"
    from .kalshi import KalshiClient
    client = KalshiClient()
    text = build(q, client.http if client.http.signer else None)
    print(text)
    OUT.write_text(text, encoding="utf-8")
    print(f"\nSaved to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
