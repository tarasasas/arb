"""Keep cash on every Kalshi exchange shard, so orders on crypto (shard 2) and tennis, baseball and
basketball (shard 3) markets don't fail with "insufficient shard balance".

  python -m arb.shards        (or double-click kalshi-shards.bat)

Kalshi only lets an order use cash on its own market's shard. This shows your cash on each shard
and turns on Kalshi's automatic rebalancing with the split you choose: every 10 seconds Kalshi moves
cash between your shards to keep that split (POST /portfolio/target_balance_allocation). Run it
again to change the split; enter 0 for everything except the main shard to turn rebalancing off.
"""

import sys

from .http import ApiError
from .kalshi import KalshiClient

SHARDS = [(0, "main: politics, economics, football and everything else"),
          (2, "crypto and commodities"),
          (3, "tennis, baseball and basketball")]
DEFAULT = {0: 50, 2: 30, 3: 20}


def parse_split(text, default=DEFAULT):
    """'50 30 20' -> {0: 50, 2: 30, 3: 20}; empty -> default. Raises ValueError unless it sums to 100."""
    if not text.strip():
        return dict(default)
    nums = [int(x) for x in text.replace(",", " ").replace("/", " ").split()]
    if len(nums) != len(SHARDS) or any(n < 0 for n in nums) or sum(nums) != 100:
        raise ValueError(f"enter {len(SHARDS)} whole numbers that add up to 100, e.g. 50 30 20")
    return {idx: n for (idx, _), n in zip(SHARDS, nums)}


def allocation_body(split):
    nonzero = [{"exchange_index": i, "percent": p} for i, p in split.items() if p > 0]
    # Everything on the main shard = rebalancing off (an empty list disables it).
    return {"allocations": [] if [a["exchange_index"] for a in nonzero] == [0] else nonzero}


def main(ask=input):
    client = KalshiClient()
    if not client.http.signer:
        print("This needs your Kalshi API key in .env (KALSHI_API_KEY_ID and the private key).")
        return 1
    bal = client.http.get("/portfolio/balance")
    by = {int(b.get("exchange_index", 0)): float(b.get("balance") or 0) for b in bal.get("balance_breakdown") or []}
    print("Your Kalshi cash by exchange shard:")
    for idx, name in SHARDS:
        print(f"  shard {idx} ({name}): ${by.get(idx, 0.0):,.2f}")
    try:
        cur = client.http.get("/portfolio/target_balance_allocation").get("allocations") or []
        print("Automatic rebalancing now: " + (", ".join(f"shard {a['exchange_index']} {a['percent']}%" for a in cur) or "off"))
    except ApiError:
        pass
    print(f"\nSplit to keep, in % for shards {', '.join(str(i) for i, _ in SHARDS)} "
          f"(Enter for {' '.join(str(DEFAULT[i]) for i, _ in SHARDS)}; 100 0 0 turns rebalancing off):")
    while True:
        try:
            split = parse_split(ask("> "))
            break
        except ValueError as e:
            print(f"  {e}")
    try:
        client.http.post("/portfolio/target_balance_allocation", allocation_body(split))
    except ApiError as e:
        print(f"Kalshi refused it ({e}). You can also set it at kalshi.com/account/exchange-indexes.")
        return 1
    print("Done: " + ", ".join(f"shard {i} {p}%" for i, p in split.items()) +
          ". Kalshi rebalances every 10 seconds; restart the dashboard to see the new cash per shard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
