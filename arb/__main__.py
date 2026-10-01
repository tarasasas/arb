"""Entry point.

  python -m arb              run the scanner with a local dashboard (http://localhost:8791)
  python -m arb --once       run one scan and print results to the terminal
"""

import argparse
import json
import sys

from .scanner import Scanner


def print_once(scanner):
    s = scanner.snapshot()
    print("\n=== Summary ===")
    print(json.dumps(s["stats"], indent=1))
    print("\nLeagues (Polymarket games / Kalshi games / matched):")
    for l in s["leagues"]:
        print(f"  {l['league']:8s} {l['kalshi']:13s} {l['pm_games']:4d} / {l['kalshi_games']:4d} / {l['matched']:4d}")
    print(f"\nOpportunities: {len(s['opportunities'])}")
    for r in s["opportunities"][:20]:
        print(f"  ${r['profit']:.2f} on {r['size']} pairs, ${r['capital']:.2f} invested (ROI {r['roi']:.2%}) | "
              f"{r['league']} {r['game']} | {r['quantity']}")
        for leg, ex in zip(r["legs"], ("kalshi", "polymarket")):
            fills = r["book"][ex]
            cost = sum(p * q for p, q in fills)
            print(f"      {leg['exchange']:10s} {leg['action']:40s} {r['size']} @ avg {cost / r['size']:.4f} "
                  f"= ${cost:.2f}  {leg['title'][:70]}")
        print(f"      fees ${r['fees']:.2f}; outcomes:")
        for sc in r["scenarios"]:
            print(f"        {sc['outcome']:50s} Kalshi ${sc['kalshi']:.2f} + Polymarket ${sc['polymarket']:.2f}"
                  f" = ${sc['total']:.2f}/pair")
        for w in r["warnings"]:
            print(f"      ! {w}")
    print(f"\nClosest near misses (edge per contract):")
    for r in s["near_misses"][:15]:
        legs = " + ".join(f"{l['exchange']} {l['side'].upper()} {l['market_id']} @{l['price']:.3f}" for l in r["legs"])
        print(f"  {r['edge_per_contract']:+.4f} | {r['league']} {r['game']} | {legs}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arb", description="Kalshi vs Polymarket US sports arbitrage scanner")
    ap.add_argument("--once", action="store_true", help="run one scan, print results, exit")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    ap.add_argument("--phone", action="store_true",
                    help="also serve the dashboard on your local network for your phone (needs DASHBOARD_PASSWORD in .env); "
                         "on by default once DASHBOARD_PASSWORD is set")
    ap.add_argument("--local", action="store_true", help="this computer only, even if DASHBOARD_PASSWORD is set")
    args = ap.parse_args(argv)

    from . import gctune
    gctune.setup()
    scanner = Scanner()
    if args.once:
        scanner.start_message()
        scanner.refresh_catalog()
        scanner.refresh_prices()
        print_once(scanner)
        return 0

    from .server import serve
    from . import config
    phone = not args.local and (args.phone or config.DASHBOARD_PHONE or bool(config.DASHBOARD_PASSWORD))
    if not phone and not args.local:
        scanner.log("Phone access off: add DASHBOARD_PASSWORD=<at least 8 characters> to .env to open the "
                    "dashboard on your phone (see 'On your iPhone' in the README)")
    try:
        serve(scanner, args.port, open_browser=not args.no_browser, phone=phone, password=config.DASHBOARD_PASSWORD)
    finally:
        scanner.makerbot.stop_all()     # don't leave Auto maker orders resting (they'd expire anyway)
    return 0


if __name__ == "__main__":
    sys.exit(main())
