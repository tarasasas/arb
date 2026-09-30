"""Check every opportunity the running dashboard shows: re-fetch both books live, recompute the
profit, and compare the two markets' rules for signs of a wrong match.

    python -m arb.verify [port]

Verdicts: LEGIT (still profitable on live books, no rule problems found), GONE (not profitable any
more), SUSPECT (too good to be true), WRONG (different settlement source, year or time), TRAP (one
side's rules are wider, so this direction can lose both legs). LEGIT is not a guarantee: read both
rules before trading."""
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import nonsports
from .engine import source_mismatch
from .model import kalshi_fee, polymarket_fee

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
BASE = f"http://127.0.0.1:{PORT}"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def live(ex, mid, side):
    return [tuple(l) for l in get(f"/api/depth?exchange={ex}&id={urllib.parse.quote(mid)}&side={side}")["levels"]]


def best_profit(lk, lp, ck, cp, pay):
    """Walk both fresh books; return (pairs, profit) at the most profitable size."""
    best, fk, fp, n, i, j = (0, 0.0), [], [], 0.0, 0, 0
    rk, rp = (lk[0][1] if lk else 0), (lp[0][1] if lp else 0)
    while i < len(lk) and j < len(lp):
        q = min(rk, rp, 50)
        fk.append((lk[i][0], q))
        fp.append((lp[j][0], q))
        n += q
        prof = pay * n - sum(p * q for p, q in fk) - sum(p * q for p, q in fp) - kalshi_fee(fk, ck) - polymarket_fee(fp, cp)
        if prof > best[1]:
            best = (n, prof)
        elif prof < best[1] - 0.5:
            break                                  # past the profitable depth
        rk, rp = rk - q, rp - q
        if rk <= 1e-9:
            i += 1
            rk = lk[i][1] if i < len(lk) else 0
        if rp <= 1e-9:
            j += 1
            rp = lp[j][1] if j < len(lp) else 0
    return best


def rule_flags(r):
    """Reasons to doubt a non-sports pair, from its rules and the scanner's own checks."""
    flags = [w for w in r.get("not_simple") or [] if "settlement sources" in w or "announcement" in w]
    if not nonsports.times_compatible(r["rules"]["kalshi"], r["rules"]["polymarket"]):
        flags.append("rules give different times of day")
    return flags


def verdict_for(r, prof):
    why = rule_flags(r) if r.get("pair") else [w for w in r["warnings"] if "Overtime" in w or "Whole-number" in w]
    if r.get("pair", {}).get("auto"):
        why.append("auto-matched")
    if not prof or prof <= 0:
        return "GONE", why
    if any("source" in w or "times" in w for w in why):
        return "WRONG", why
    if any("announcement" in w for w in why):
        return "TRAP", why
    if r.get("suspicious"):
        return "SUSPECT", why + ["too good to be true"]
    return "LEGIT", why


def check(r):
    kleg, pleg = r["legs"]
    try:
        n, prof = best_profit(live("kalshi", kleg["market_id"], kleg["side"]),
                              live("polymarket", pleg["market_id"], pleg["side"]),
                              r["fee_coef"]["kalshi"], r["fee_coef"]["polymarket"], r["payout"])
    except Exception as e:                          # a book fetch failed; say so rather than guess
        return "ERROR", 0, 0.0, [repr(e)]
    verdict, why = verdict_for(r, prof)
    return verdict, n, prof, why


def main():
    s = get("/api/state")
    opps = s["opportunities"]
    print(f"{datetime.now(timezone.utc):%H:%M:%S} UTC  {len(opps)} opportunities on the dashboard")
    for r in opps:
        verdict, n, prof, why = check(r)
        kleg, pleg = r["legs"]
        live_txt = f"live now: {n:g} pairs, ${prof:.2f}" if prof > 0 else "live now: gone"
        print(f"  {verdict:7} ${r['profit']:8.2f} ({live_txt})  {r['league']}  {r['game'][:70]}")
        print(f"          Kalshi {kleg['side'].upper()} @ {kleg['price']}: {kleg['title'][:80]}")
        print(f"          Polymarket {pleg['side'].upper()} @ {pleg['price']}: {pleg['title'][:80]}")
        if why:
            print(f"          {'; '.join(why)}")


if __name__ == "__main__":
    main()
