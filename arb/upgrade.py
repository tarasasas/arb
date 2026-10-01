"""One-time: ask Kalshi for the free Advanced API tier (30 requests a second instead of 20).

  python -m arb.upgrade        (or double-click upgrade-kalshi.bat)

Kalshi grants it permanently when at least 1 of your last 100 Kalshi orders was placed through the
API (Make trade counts; orders placed on kalshi.com don't). The scanner reads your tier at start-up
and speeds up on its own, so just restart the dashboard afterwards.
POST /account/api_usage_level/upgrade, see docs.kalshi.com.
"""

import sys

from .http import ApiError
from .kalshi import KalshiClient


def describe(limits):
    read = (limits.get("read") or {}).get("refill_rate")
    return f"{limits.get('usage_tier', '?')} tier" + (f", read budget {float(read):g} tokens/s" if read else "")


def main():
    client = KalshiClient()
    if not client.http.signer:
        print("This needs your Kalshi API key in .env (KALSHI_API_KEY_ID and the private key).")
        return 1
    before = client.http.get("/account/limits")
    print(f"Now: {describe(before)}")
    try:
        client.http.post("/account/api_usage_level/upgrade", {})
    except ApiError as e:
        if e.status == 403:
            print("Kalshi said no: none of your last 100 Kalshi orders was placed through the API.\n"
                  "Place one small trade with the dashboard's Make trade button, then run this again.")
        else:
            print(f"Kalshi refused the upgrade ({e}).")
        return 1
    after = client.http.get("/account/limits")
    print(f"After: {describe(after)}")
    print("Done. Restart the dashboard and it will use the higher limit automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
