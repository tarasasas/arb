"""End-to-end timing of a trade on each exchange, with test orders that can't fill.

  python -m arb.latency          (or double-click latency-test.bat)

Times every step a trade goes through, on Kalshi and Polymarket, using the same code Make trade,
Fast trade and Auto-trade use: market info, live order book, cash, then a real order round trip on
each site and both at once. The test orders are 1 share at a 1 cent limit, immediate-or-cancel,
on markets where nobody is selling anywhere near 1 cent, so they go all the way through each
exchange's order system and cancel unfilled (worst case if one filled: about 2 cents). It asks
before sending any order. The report is printed and saved to latency-report.txt.
"""

import statistics
import sys
import time

from . import config
from .http import ApiError, LanePool

RUNS = 3
OUT = config.PROJECT_ROOT / "latency-report.txt"
TEST_PRICE = 0.01
MIN_ASK = 0.10           # only use a market whose cheapest seller asks at least this much


def timed(fn, *args):
    t = time.perf_counter()
    out = fn(*args)
    return (time.perf_counter() - t) * 1000, out


def summary(ms):
    return f"{statistics.median(ms):7.0f} ms   (min {min(ms):.0f}, max {max(ms):.0f})"


def pick_kalshi(kv, shard_cash):
    """An open Kalshi market on a shard holding cash, with no YES seller under MIN_ASK."""
    d = kv.client.http.get("/markets", {"status": "open", "limit": 200, "mve_filter": "exclude"})
    for m in d.get("markets") or []:
        shard = int(m.get("exchange_index") or 0)
        if shard_cash.get(shard, 0) < 0.05:
            continue
        try:
            yes = kv.levels(m["ticker"])["yes"]
        except Exception:
            continue
        if yes and yes[0][0] >= MIN_ASK and kv.market_info(m["ticker"]).get("open"):
            return m["ticker"], shard
    return None, None


def pick_polymarket(pv):
    d = pv.public.http.get("/markets", {"active": "true", "closed": "false", "limit": 100})
    for m in d.get("markets") or []:
        try:
            yes = pv.levels(m["slug"])["yes"]
        except Exception:
            continue
        if yes and yes[0][0] >= MIN_ASK and pv.market_info(m["slug"]).get("open"):
            return m["slug"]
    return None


def run(kv, pv, ask=input, runs=RUNS, log=print):
    lines = []

    def out(s=""):
        lines.append(s)
        log(s)

    out(f"Trade timing test, {time.strftime('%Y-%m-%d %H:%M:%S')}  (median of {runs} runs)")
    shards = kv.shard_balances()
    ticker, shard = pick_kalshi(kv, shards)
    slug = pick_polymarket(pv)
    if not ticker or not slug:
        out("Couldn't find a test market on " + ("Kalshi" if not ticker else "Polymarket") +
            " (needs an open market with no seller under 10 cents" + (", on a shard holding cash" if not ticker else "") + ").")
        return 1, lines
    out(f"Kalshi test market:     {ticker} (shard {shard})")
    out(f"Polymarket test market: {slug}")
    out("")

    steps = {
        "Kalshi      market info": lambda: kv.market_info(ticker),
        "Kalshi      order book": lambda: kv.levels(ticker),
        "Kalshi      cash on shard": lambda: kv.balance(shard),
        "Kalshi      cash per shard": kv.shard_balances,
        "Polymarket  market info": lambda: pv.market_info(slug),
        "Polymarket  order book": lambda: pv.levels(slug),
        "Polymarket  buying power": pv.balance,
    }
    out("Reading (what every trade does before ordering):")
    total = {"Kalshi": 0.0, "Polymarket": 0.0}
    for name, fn in steps.items():
        ms = [timed(fn)[0] for _ in range(runs)]
        out(f"  {name:28s} {summary(ms)}")
        if "per shard" not in name:
            total[name.split()[0]] += statistics.median(ms)
    out(f"  -> one trade's checks if run one after another: Kalshi {total['Kalshi']:.0f} ms + Polymarket "
        f"{total['Polymarket']:.0f} ms = {sum(total.values()):.0f} ms")
    out("")

    answer = ask(f"Send {runs * 3} test orders (1 share at 1 cent, immediate-or-cancel; they shouldn't fill)? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        out("Skipped the order tests.")
        return 0, lines

    fills = []

    def order_k():
        f = kv.buy(ticker, "yes", 1, TEST_PRICE, 0.07)
        fills.append(("Kalshi", f.qty))
        return f

    def order_p():
        f = pv.buy(slug, "yes", 1, TEST_PRICE, config.POLYMARKET_DEFAULT_COEF)
        fills.append(("Polymarket", f.qty))
        return f

    def both():
        with LanePool(2) as pool:
            return [j.result() for j in (pool.submit(order_k), pool.submit(order_p))]

    out("Placing orders (send -> exchange's final answer):")
    for name, fn in (("Kalshi      order round trip", order_k), ("Polymarket  order round trip", order_p),
                     ("Both at once (as trades now)", both)):
        ms, errors = [], []
        for _ in range(runs):
            try:
                ms.append(timed(fn)[0])
            except ApiError as e:
                errors.append(str(e))
            if any(q > 0 for _, q in fills):
                break
        out(f"  {name:28s} {summary(ms) if ms else 'failed'}" + (f"   errors: {errors[0]}" if errors else ""))
        if any(q > 0 for _, q in fills):
            out("  ! A test order filled (someone sold at 1 cent). Stopping; check that account.")
            break
    filled = [(ex, q) for ex, q in fills if q > 0]
    out("")
    out("No test order filled." if not filled else f"Filled: {filled}. Sell those shares or let them settle.")
    return 0, lines


def main(argv=None):
    from .kalshi import KalshiClient
    from .polymarket import PolymarketClient
    from .polymarket_auth import load_signer
    from .venues import KalshiVenue, PolymarketVenue
    k = KalshiClient()
    if not k.http.signer or not (config.POLYMARKET_KEY_ID and config.POLYMARKET_SECRET_KEY):
        print("This needs both API keys in .env (it uses the same trading code as Make trade).")
        return 1
    kv = KalshiVenue(k)
    pv = PolymarketVenue(PolymarketClient(), load_signer(config.POLYMARKET_KEY_ID, config.POLYMARKET_SECRET_KEY))
    yes = "--yes" in (argv if argv is not None else sys.argv[1:])
    code, lines = run(kv, pv, ask=(lambda _p: "y") if yes else input)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved to {OUT}")
    return code


if __name__ == "__main__":
    sys.exit(main())
