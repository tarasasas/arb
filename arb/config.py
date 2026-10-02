"""Static configuration: API hosts, rate limits, league mapping, scan settings."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_env_file(path=PROJECT_ROOT / ".env"):
    """Minimal .env support: KEY=VALUE lines; real environment variables take precedence."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()

# Optional Kalshi API key: raises the read limit from ~4 req/s (public) to the account's
# tier budget (Basic = 200 tokens/s = 20 market-data requests/s at 10 tokens each).
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "").strip()
_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
KALSHI_PRIVATE_KEY_PATH = str((PROJECT_ROOT / _key_path).resolve()) if _key_path else ""
KALSHI_BUDGET_FRACTION = 0.85      # leave headroom below the account's read budget

# Optional Polymarket US API key (polymarket.us/developer). Needed only for "Make trade".
POLYMARKET_KEY_ID = os.environ.get("POLYMARKET_KEY_ID", "").strip()
POLYMARKET_SECRET_KEY = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
POLYMARKET_TRADE_BASE = "https://api.polymarket.us"

# ---- trading ("Make trade" button) ----------------------------------------------------------
MAX_TRADE_DOLLARS = 100.0          # hard cap per trade, both legs combined (fees included)
TRADE_PLAN_TTL_SECS = 20           # a confirmed plan must be executed within this window
SECOND_LEG_RETRIES = 3             # extra hedge attempts, each sent only once the book shows shares under the ceiling
HEDGE_WINDOW_SECS = 1.5            # how long the second leg keeps trying before the first leg is sold back
HEDGE_POLL_SECS = 0.1              # book re-check interval while the second leg waits for liquidity
# The second leg may pay up to this much per share past break-even, but only when selling the first leg
# back would lose more (spread + a second round of fees, usually 3-5c a share). 0 = never past break-even.
HEDGE_MAX_LOSS_PER_SHARE = 0.02
SELLBACK_SLIPPAGE_TICKS = 3        # sell-back accepts up to this many ticks below the best bid
TRADES_LOG = PROJECT_ROOT / "trades.jsonl"

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# external-api.kalshi.com is Kalshi's recommended host, but it rejects Python's urllib
# (403) while api.elections.kalshi.com, also officially supported, accepts it.
POLYMARKET_BASE = "https://gateway.polymarket.us/v1"

# Requests per second. Unauthenticated Kalshi reads start returning 429 around 4-5/s;
# Polymarket US allows 20/s per IP on the public gateway.
KALSHI_RPS = 4.0
POLYMARKET_RPS = 15.0

# Polymarket league code (from slugs, e.g. "asc-nfl-...") -> (Kalshi league code, sport).
# Kalshi series are named KX + league + optional period + kind, e.g. KXNFL1HSPREAD.
LEAGUES = {
    "nfl": ("NFL", "football"),
    "cfb": ("NCAAF", "football"),
    "mlb": ("MLB", "baseball"),
    "nhl": ("NHL", "hockey"),
    "nba": ("NBA", "basketball"),
    "wnba": ("WNBA", "basketball"),
    "kbo": ("KBO", "baseball"),
    "npb": ("NPB", "baseball"),
    "eurolg": ("EUROLEAGUE", "basketball"),
    "eurocup": ("EUROCUP", "basketball"),
    "vtb": ("VTB", "basketball"),
    "nbl": ("NBL", "basketball"),
    "lnbp": ("LNBP", "basketball"),
    "bsl": ("BSL", "basketball"),
    "bbl": ("BBL", "basketball"),
    "khl": ("KHL", "hockey"),
    "shl": ("SHL", "hockey"),
    "del": ("DEL", "hockey"),
    "liiga": ("LIIGA", "hockey"),
    "unl": ("UEFANL", "soccer"),
    "intf": ("INTLFRIENDLY", "soccer"),
    "mls": ("MLS", "soccer"),
    "uslc": ("USL", "soccer"),
    "lexp": ("LIGAEXP", "soccer"),
    "engnl": ("ENGNL", "soccer"),
    "nwsl": ("NWSL", "soccer"),
    "j2": ("J2LEAGUE", "soccer"),
    "lal2": ("LALIGA2", "soccer"),
    "efl1": ("EFLL1", "soccer"),
    "bra": ("BRASILEIRO", "soccer"),
    "brb": ("BRASILEIROB", "soccer"),
    "brc": ("BRASILEIROC", "soccer"),
    "uwcl": ("UCLW", "soccer"),
}

# Fees (taker). Kalshi: 0.07 x series fee_multiplier x C x P x (1-P), rounded up to the cent.
# Polymarket US: feeCoefficient (0.0695 today) x C x p x (1-p), banker's rounding to the cent.
KALSHI_TAKER_COEF = 0.07
POLYMARKET_DEFAULT_COEF = 0.0695

# Scanner settings.
CATALOG_REFRESH_SECS = 300     # full market lists + game matching
SUGGEST_REFRESH_SECS = 1800    # rebuild non-sports match suggestions
CRYPTO_REFRESH_SECS = 20       # look for new 15-minute crypto Up/Down windows
AUTO_ACCEPT_MATCHES = True     # scan confident non-sports matches without waiting for approval
AUTO_MIN_EVENT_SCORE = 0.5     # question-level match score needed to auto-accept
AUTO_MIN_OUTCOME_SCORE = 0.3   # outcome-level match score needed to auto-accept
FULL_SWEEP_SECS = 5            # pause between full sweeps of every watched contract (a sweep takes 10-40s)
HOT_PAUSE_SECS = 0.5           # pause between re-checks of the near-arb "hot list" (a re-check takes ~1-2s)
MIN_PROFIT_DOLLARS = 0.01      # hide opportunities below this guaranteed profit
NEAR_MISS_EDGE = -0.03         # also track pairs within 3 cents of breaking even (per contract)
MAX_NEAR_MISSES = 50
