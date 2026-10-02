"""One-time: ask Kalshi for the free Advanced API usage level (a bigger read budget, faster scanning).

  python -m arb.upgrade        (or double-click upgrade-kalshi.bat)

Kalshi grants it permanently when at least 1 of your last 100 Kalshi orders was placed through the
API (Make trade, Auto-trade and latency-test.bat count; orders placed on kalshi.com don't).
POST /account/api_usage_level/upgrade (docs.kalshi.com), sent exactly like Kalshi's example: no body,
to external-api.kalshi.com first, then to the host the scanner uses.

Note: the upgrade shows up as a grant and a bigger read budget. GET /account/limits keeps reporting
"usage_tier": "basic" either way, so that label alone doesn't say whether it worked.
"""

import sys

from .http import ApiError, RateLimitedClient
from .kalshi import KalshiClient

HOSTS = ("https://external-api.kalshi.com/trade-api/v2", "https://api.elections.kalshi.com/trade-api/v2")
PATH = "/account/api_usage_level/upgrade"


def describe(limits):
    """One readable line per thing that matters: tier label, read and write budgets, grants."""
    read, write = limits.get("read") or {}, limits.get("write") or {}
    lines = [f"  usage_tier:  {limits.get('usage_tier', '?')}",
             f"  read budget: {read.get('refill_rate', '?')} tokens/s (bucket {read.get('bucket_capacity', '?')})"
             f" = about {float(read.get('refill_rate') or 0) / 10:g} market-data requests/s",
             f"  write budget: {write.get('refill_rate', '?')} tokens/s (bucket {write.get('bucket_capacity', '?')})"]
    grants = limits.get("grants") or []
    if grants:
        for g in grants:
            lines.append(f"  grant: {g.get('level')} on {g.get('exchange_instance')} (source {g.get('source')}"
                         + (f", expires {g['expires_ts']}" if g.get("expires_ts") else ", permanent") + ")")
    else:
        lines.append("  grants: none")
    return "\n".join(lines)


def read_budget(limits):
    return float((limits.get("read") or {}).get("refill_rate") or 0)


def main():
    client = KalshiClient()
    if not client.http.signer:
        print("This needs your Kalshi API key in .env (KALSHI_API_KEY_ID and the private key).")
        return 1
    before = client.http.get("/account/limits")
    print("Before:\n" + describe(before))
    if before.get("usage_tier", "basic") != "basic" or before.get("grants"):
        print(f"\nAlready upgraded: you're on {before.get('usage_tier')} with a read budget of "
              f"{read_budget(before):g} tokens/s. Nothing to do; the dashboard already uses it.")
        return 0

    answer, refused = None, None
    for host in HOSTS:
        http = RateLimitedClient(host, 2.0, signer=client.http.signer)
        try:
            answer = http.post(PATH, None)
            print(f"\nKalshi accepted the upgrade request ({host.split('/')[2]}): {answer or 'OK'}")
            break
        except ApiError as e:
            blocked = e.detail.lstrip().startswith("<")     # a web page, not Kalshi's API answering
            print(f"\n{host.split('/')[2]} answered HTTP {e.status}" + (
                " (the host's front door blocked the request; trying the other host)" if blocked
                else f": {e.detail[:300]}"))
            if e.status == 403 and not blocked:
                refused = e
                break                       # a real "no": the other host would say the same
        except Exception as e:              # host unreachable from here: try the other one
            print(f"\n{host.split('/')[2]} couldn't be reached ({e!r})")
    if refused:
        print("\nKalshi said no: none of your last 100 Kalshi orders was placed through the API.\n"
              "Place one small trade with the dashboard's Make trade button (or run latency-test.bat and\n"
              "answer y), then run this again.")
        return 1
    if answer is None:
        print("\nThe upgrade request didn't go through on either host. Copy everything above and send it over.")
        return 1

    after = client.http.get("/account/limits")
    print("\nAfter:\n" + describe(after))
    if read_budget(after) > read_budget(before) or (after.get("grants") or []) != (before.get("grants") or []):
        print("\nUpgraded. Restart the dashboard and it will use the bigger budget automatically.")
    elif after.get("grants"):
        print("\nYou already have the grant shown above; the read budget is what Kalshi gives it.\n"
              "\"usage_tier\" stays \"basic\" on Kalshi's side: that label doesn't change with a grant.")
    else:
        print("\nKalshi accepted the request but your limits look the same. It may take a few minutes to apply;\n"
              "run this again later. If it still looks the same, copy everything above and send it over.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
