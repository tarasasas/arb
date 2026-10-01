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
# Or the key itself (PEM text) in one variable, for hosts where a file is awkward, e.g. a cloud
# environment's settings. Line breaks may be real newlines or written as \n.
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "").strip().replace("\\n", "\n")
KALSHI_BUDGET_FRACTION = 0.85      # leave headroom below the account's read budget

# Optional Polymarket US API key (polymarket.us/developer). Needed only for "Make trade".
POLYMARKET_KEY_ID = os.environ.get("POLYMARKET_KEY_ID", "").strip()
POLYMARKET_SECRET_KEY = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()
POLYMARKET_TRADE_BASE = "https://api.polymarket.us"

# ---- trading ("Make trade" button) ----------------------------------------------------------
MAX_TRADE_DOLLARS = 100.0          # hard cap per trade, both legs combined (fees included)
TRADE_PLAN_TTL_SECS = 20           # a confirmed plan must be executed within this window
SECOND_LEG_RETRIES = 2             # extra attempts to hedge the second leg before selling back
# The second leg's first try goes out with its limit at break-even (not at the price seen when planning).
# IOC orders fill at the best prices in the book, so this costs nothing when the book held still, and
# when it moved a tick it still hedges (at less profit) instead of missing and selling back at a loss.
SECOND_LEG_AT_BREAKEVEN = os.environ.get("SECOND_LEG_AT_BREAKEVEN", "1").strip().lower() not in ("0", "false", "no", "off")
# How a trade's two orders go out:
#   together (default): both at the same moment, so neither waits on the other site's answer. Uneven
#     fills are evened up on the short side (never above break-even), and anything left is sold back.
#   polymarket_first: Polymarket (the slower site) first, then Kalshi for exactly what filled. A
#     Polymarket miss trades nothing, but Kalshi's price has Polymarket's whole answer time to move.
#   thinner_first: the book with less depth first, the other sized to its fill.
TRADE_ORDER = os.environ.get("TRADE_ORDER", "").strip().lower() or "together"
if TRADE_ORDER not in ("polymarket_first", "together", "thinner_first"):
    TRADE_ORDER = "together"
TRADE_LEGS_TOGETHER = TRADE_ORDER == "together"
SELLBACK_SLIPPAGE_TICKS = 3        # sell-back accepts up to this many ticks below the best bid
TRADES_LOG = PROJECT_ROOT / "trades.jsonl"
# Kalshi keeps cash per exchange shard and an order can only use its market's shard. With this on, a
# trade first moves the cash it needs onto that shard from your other shards (your own money, same
# account). Set KALSHI_AUTO_SHARD_FUNDING=0 in .env to turn it off.
KALSHI_AUTO_SHARD_FUNDING = os.environ.get("KALSHI_AUTO_SHARD_FUNDING", "1").strip().lower() not in ("0", "false", "no", "off")
SHARD_TRANSFER_WAIT_SECS = 8.0     # how long to wait for a shard transfer to show up before trading


def _env_num(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---- Dashboard on your phone (python -m arb --phone, or DASHBOARD_PHONE=1) ----------------------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_PHONE = os.environ.get("DASHBOARD_PHONE", "").strip().lower() in ("1", "true", "yes", "on")

# ---- Fast trade (one click, no confirm) and Auto-trade (no click), for time-sensitive arbs ------
# Only rows that need no checking by you qualify: crypto (paired by contract terms) or anything
# settling within FAST_MAX_HOURS, never auto-matched pairs or rows with rule warnings.
FAST_MAX_HOURS = _env_num("FAST_MAX_HOURS", 24)
# Also allow pairs the matcher paired by wording (not verified by you). Set to 0 to require your check.
FAST_ALLOW_AUTO_MATCHED = os.environ.get("FAST_ALLOW_AUTO_MATCHED", "1").strip().lower() not in ("0", "false", "no", "off")
# Also allow rows flagged "too good to be true" (no upper limit on the return). Live prices are always
# re-checked before ordering. Set to 0 to skip them.
FAST_ALLOW_TOO_GOOD = os.environ.get("FAST_ALLOW_TOO_GOOD", "1").strip().lower() not in ("0", "false", "no", "off")
FAST_MAX_TRADE = _env_num("FAST_MAX_TRADE", 50)             # $ per Fast trade, both legs
AUTO_TRADE_MAX_TRADE = _env_num("AUTO_TRADE_MAX_TRADE", 25)  # $ per Auto-trade, both legs
AUTO_TRADE_DAILY_LIMIT = _env_num("AUTO_TRADE_DAILY_LIMIT", 100)   # $ spent by Auto-trade per day
AUTO_TRADE_MIN_PROFIT = _env_num("AUTO_TRADE_MIN_PROFIT", 0.0)     # $ floor (off: only the ROI minimum applies)
AUTO_TRADE_MIN_ROI = _env_num("AUTO_TRADE_MIN_ROI", 0.5) / 100     # % of the money put in
AUTO_TRADE_COOLDOWN_SECS = _env_num("AUTO_TRADE_COOLDOWN_SECS", 60)  # per pair of markets
# In-play games move between the two orders (and Polymarket can delay in-play orders), so the second
# leg often misses and the first is sold back at a loss. Off unless AUTO_TRADE_LIVE_GAMES=1.
AUTO_TRADE_LIVE_GAMES = os.environ.get("AUTO_TRADE_LIVE_GAMES", "0").strip().lower() in ("1", "true", "yes", "on")
AUTO_TRADE_GAME_COOLDOWN_SECS = _env_num("AUTO_TRADE_GAME_COOLDOWN_SECS", 600)   # whole game, after a miss
AUTO_TRADE_MAX_DAILY_LOSS = _env_num("AUTO_TRADE_MAX_DAILY_LOSS", 5)             # $ net loss that stops it
# Fail-safes. Auto-trade only sizes a trade so the second leg's book holds at least this many times the
# shares within break-even (a thin book is what makes the second leg miss), and it turns itself off
# after this many misses in a row on one site (rejections, unfilled orders, unhedged second legs).
AUTO_TRADE_HEDGE_DEPTH = _env_num("AUTO_TRADE_HEDGE_DEPTH", 2)
AUTO_TRADE_MAX_MISSES = int(_env_num("AUTO_TRADE_MAX_MISSES", 3))

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
POLYMARKET_MAKER_REBATE = 0.0125   # paid to resting orders: 0.0125 x C x p x (1-p) (docs.polymarket.us/fees)
MAKER_MAX_CAPITAL = 1000.0         # size maker-mode rows to at most this much money (a resting order
                                   # can be any size, so Kalshi depth alone would suggest millions of shares)
MAKER_MIN_EDGE = 0.005             # per pair: below half a cent a maker fill isn't worth the waiting
MAKER_MAX_SPREAD = 0.03            # only where Polymarket's bid-ask gap is this tight: in a wide gap a
                                   # resting order fills only when the price jumps, and Kalshi jumps too
# Maker automation ("Auto maker" switch in Maker mode; off every time the scanner starts). It rests one
# post-only Polymarket order per arb, buys the Kalshi side for every share that fills (never above the
# hedge limit), and cancels when Kalshi moves past the hedge limit, the arb disappears, or time runs out.
MAKER_AUTO_MAX_ORDER = _env_num("MAKER_AUTO_MAX_ORDER", 25)         # $ per resting order, both legs
MAKER_AUTO_MAX_RESTING = _env_num("MAKER_AUTO_MAX_RESTING", 100)    # $ resting at once, all orders
MAKER_AUTO_MAX_ORDERS = int(_env_num("MAKER_AUTO_MAX_ORDERS", 2))   # resting orders at once
MAKER_AUTO_TTL_SECS = _env_num("MAKER_AUTO_TTL_SECS", 120)          # cancel (and Polymarket expires it) after this
MAKER_AUTO_POLL_SECS = _env_num("MAKER_AUTO_POLL_SECS", 0.5)        # how often each order is checked for fills
MAKER_AUTO_COOLDOWN_SECS = _env_num("MAKER_AUTO_COOLDOWN_SECS", 300) # per pair, after an order ends
MAKER_AUTO_DAILY_LIMIT = _env_num("MAKER_AUTO_DAILY_LIMIT", 200)    # $ filled per day

# Scanner settings.
CATALOG_REFRESH_SECS = 300     # full market lists + game matching
SUGGEST_REFRESH_SECS = 1800    # rebuild non-sports match suggestions
CRYPTO_REFRESH_SECS = 20       # look for new 15-minute crypto Up/Down windows
POSITIONS_REFRESH_SECS = 60    # live position check for My arbs (needs your API keys)
BALANCES_REFRESH_SECS = 15     # your cash on each site, used to size opportunities you can afford
STREAM_MAX_MARKETS = 2000      # per exchange: near-arb markets and non-sports/crypto pairs streamed live
STREAM_EVAL_SECS = 0.1         # how often streamed price changes are re-checked for arbs
FOCUS_FILE = PROJECT_ROOT / "cache" / "focus.json"   # the dashboard's Focus setting, kept across restarts
LIVE_BOOK_MAX_AGE = 1.0      # a trade uses the stream's book (no download) if it updated this recently
MARKET_INFO_TTL = 60.0       # a trade reuses a market's details (tick, min size, shard) this long
CASH_MAX_AGE = 20.0          # a trade uses the scanner's cash reading if it's this fresh
STREAM_FRESH_SECS = 60         # a streamed market is trusted (not polled) only if updated this recently
STREAM_QUIET_SECS = 90         # a stream with no message at all for this long is reconnected
AUTO_ACCEPT_MATCHES = True     # scan non-sports matches without waiting for approval
# Match scores needed to auto-accept. 0 accepts every suggestion; pairs whose prices mirror each other,
# sit 25+ points apart, or settle on different data providers still wait for review (always fake arbs).
AUTO_MIN_EVENT_SCORE = 0.0
AUTO_MIN_OUTCOME_SCORE = 0.0
FULL_SWEEP_SECS = 5            # pause between full sweeps of every watched contract (a sweep takes 10-40s)
HOT_PAUSE_SECS = 0.5           # pause between re-checks of the near-arb "hot list" (a re-check takes ~1-2s)
MIN_PROFIT_DOLLARS = 0.01      # hide opportunities below this guaranteed profit
NEAR_MISS_EDGE = -0.03         # also track pairs within 3 cents of breaking even (per contract)
MAX_NEAR_MISSES = 50
