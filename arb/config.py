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
def _env_num(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


MAX_TRADE_DOLLARS = _env_num("MAX_TRADE_DOLLARS", 100)    # hard cap per trade, both legs combined (fees included)
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
SECOND_LEG_RETRY_PAUSE = 0.25      # longest wait before a second-leg retry (a live stream ends it early)
PLAN_RECHECK_AFTER_SECS = 1.5      # a plan older than this (it sat in the confirm dialog) is re-checked first
# Both orders at once: share of the arb's profit given to the two orders as room above their planned limits
# (half each), so a small move on either site still fills instead of leaving the other leg to be sold back.
# 1.0 = all of it: if both fill at their raised limits the pair still breaks even. 0 = exactly the plan.
TOGETHER_HEADROOM = _env_num("TOGETHER_HEADROOM", 1.0)
TRADES_LOG = PROJECT_ROOT / "trades.jsonl"
# Kalshi keeps cash per exchange shard and an order can only use its market's shard. KALSHI_SHARD_MODE:
#   even:      every shard is kept stocked with an equal share. Kalshi's own rebalancing holds the split
#              (it moves cash about every 10 seconds, even while this app is off), so no trade waits for cash.
#   per_trade: a trade first moves the cash it needs onto its market's shard (your own money, same
#              account), then waits for it to arrive: seconds, while prices move.
#   manual:    the app leaves your shards alone (set a split with kalshi-shards.bat or at kalshi.com).
SHARD_MODES = ("even", "per_trade", "manual")


def _shard_mode(env=os.environ):
    mode = env.get("KALSHI_SHARD_MODE", "").strip().lower()
    return mode if mode in SHARD_MODES else "even"     # the older KALSHI_AUTO_SHARD_FUNDING is ignored


KALSHI_SHARD_MODE = _shard_mode()
SHARD_SPLIT_CHECK_SECS = 600       # even: re-check this often that Kalshi still keeps the even split
SHARD_EVEN_GRACE_SECS = 60         # even: if Kalshi hasn't evened the shards out this long after even mode
                                   # started, the app moves the cash itself (between trades, at most once a minute)
SHARD_TRANSFER_WAIT_SECS = 8.0     # per_trade: how long to wait for a shard transfer to show up before trading


# ---- Dashboard on your phone (python -m arb --phone, or DASHBOARD_PHONE=1) ----------------------
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "").strip()
DASHBOARD_PHONE = os.environ.get("DASHBOARD_PHONE", "").strip().lower() in ("1", "true", "yes", "on")

# ---- Fast trade (one click, no confirm) and Auto-trade (no click), for time-sensitive arbs ------
# Only rows that need no checking by you qualify: crypto (paired by contract terms) or anything whose
# result is known within FAST_MAX_HOURS (even if paid out later), never auto-matched pairs or rows with
# rule warnings.
FAST_MAX_HOURS = _env_num("FAST_MAX_HOURS", 24)
# Also allow pairs the matcher paired by wording (not verified by you). Set to 0 to require your check.
FAST_ALLOW_AUTO_MATCHED = os.environ.get("FAST_ALLOW_AUTO_MATCHED", "1").strip().lower() not in ("0", "false", "no", "off")
# Also allow rows flagged "too good to be true" (no upper limit on the return). Live prices are always
# re-checked before ordering. Set to 0 to skip them.
FAST_ALLOW_TOO_GOOD = os.environ.get("FAST_ALLOW_TOO_GOOD", "1").strip().lower() not in ("0", "false", "no", "off")
# Also allow player props (if the player doesn't play, each site settles at its own fair price, so the
# pair may pay a little more or less than $1). Set to 0 to skip them.
FAST_ALLOW_PLAYER_PROPS = os.environ.get("FAST_ALLOW_PLAYER_PROPS", "1").strip().lower() not in ("0", "false", "no", "off")
FAST_MAX_TRADE = _env_num("FAST_MAX_TRADE", 50)             # $ per Fast trade, both legs
AUTO_TRADE_MAX_TRADE = _env_num("AUTO_TRADE_MAX_TRADE", 25)  # $ per Auto-trade, both legs
AUTO_TRADE_DAILY_LIMIT = _env_num("AUTO_TRADE_DAILY_LIMIT", 100)   # $ spent by Auto-trade per day
AUTO_TRADE_MIN_PROFIT = _env_num("AUTO_TRADE_MIN_PROFIT", 0.0)     # $ floor (off: only the ROI minimum applies)
AUTO_TRADE_MIN_ROI = _env_num("AUTO_TRADE_MIN_ROI", 0.5) / 100     # % of the money put in
AUTO_TRADE_COOLDOWN_SECS = _env_num("AUTO_TRADE_COOLDOWN_SECS", 60)  # per pair of markets
# Rows kept from an earlier price pass (not re-checked, e.g. out of book downloads) carry old prices: trying
# one costs the pair its cooldown when the arb turns out gone. Auto-trade takes only rows checked this recently.
AUTO_TRADE_MAX_ROW_AGE = _env_num("AUTO_TRADE_MAX_ROW_AGE", 5)
# In-play games move between the two orders (and Polymarket can delay in-play orders), so the second
# leg often misses and the first is sold back at a loss. Off unless AUTO_TRADE_LIVE_GAMES=1.
AUTO_TRADE_LIVE_GAMES = os.environ.get("AUTO_TRADE_LIVE_GAMES", "0").strip().lower() in ("1", "true", "yes", "on")
# Crypto Up/Down price windows (BTC, ETH, ... at a set minute): the price moves every second, so the second
# leg often misses. Auto-trade leaves them alone unless AUTO_TRADE_CRYPTO_WINDOWS=1 (Fast trade still can).
AUTO_TRADE_CRYPTO_WINDOWS = os.environ.get("AUTO_TRADE_CRYPTO_WINDOWS", "0").strip().lower() in ("1", "true", "yes", "on")
AUTO_TRADE_GAME_COOLDOWN_SECS = _env_num("AUTO_TRADE_GAME_COOLDOWN_SECS", 600)   # whole game, after a miss
AUTO_TRADE_MAX_DAILY_LOSS = _env_num("AUTO_TRADE_MAX_DAILY_LOSS", 5)             # $ net loss that stops it
# Fail-safes. Auto-trade only sizes a trade so the second leg's book holds at least this many times the
# shares within break-even (a thin book is what makes the second leg miss), and it turns itself off
# after this many misses in a row on one site (rejections, unfilled orders, unhedged second legs).
AUTO_TRADE_HEDGE_DEPTH = _env_num("AUTO_TRADE_HEDGE_DEPTH", 2)
AUTO_TRADE_MAX_MISSES = int(_env_num("AUTO_TRADE_MAX_MISSES", 3))
# How Auto-trade's two orders go out (same choices as TRADE_ORDER, plus smart). smart (default), per
# trade: the stale side first when the live streams show one site just moved and the other hasn't (the
# stale one is about to reprice); both at once in fast markets (crypto windows, games in progress) or
# where second legs keep missing; otherwise the thinner book first. thinner_first: the book with less
# depth first (a miss there trades nothing), then the other site for exactly what filled.
AUTO_TRADE_ORDER = os.environ.get("AUTO_TRADE_ORDER", "").strip().lower() or "smart"
if AUTO_TRADE_ORDER not in ("smart", "polymarket_first", "together", "thinner_first"):
    AUTO_TRADE_ORDER = "smart"
STALE_FRESH_SECS = 2.0      # "just moved": a streamed price that changed within this many seconds
STALE_GAP_SECS = 3.0        # "stale": the other site's price unchanged for at least this much longer


def _env_on(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() not in ("0", "false", "no", "off")


# Edge Auto-trade needs, per pair: fast markets (crypto windows, games in progress) move between the two
# orders, so they need AUTO_TRADE_FAST_EDGE (cents). With AUTO_TRADE_LEARN_BUFFER every market type also
# needs the typical price move its own trades met while the orders went out.
AUTO_TRADE_FAST_EDGE = _env_num("AUTO_TRADE_FAST_EDGE", 2) / 100
AUTO_TRADE_LEARN_BUFFER = _env_on("AUTO_TRADE_LEARN_BUFFER", True)
# Pause a market type whose recent real trades mostly miss or lose: over its last AUTO_TRADE_THROTTLE_MIN_TRIES,
# fewer than AUTO_TRADE_THROTTLE_MIN_FILL (%) filled on both sites, or a net loss.
AUTO_TRADE_THROTTLE = _env_on("AUTO_TRADE_THROTTLE", True)
AUTO_TRADE_THROTTLE_MIN_FILL = _env_num("AUTO_TRADE_THROTTLE_MIN_FILL", 40) / 100
AUTO_TRADE_THROTTLE_MIN_TRIES = int(_env_num("AUTO_TRADE_THROTTLE_MIN_TRIES", 5))
AUTO_TRADE_THROTTLE_HOURS = _env_num("AUTO_TRADE_THROTTLE_HOURS", 2)
AUTO_TRADE_STATS_KEEP = 30                 # results kept per market type
EXEC_STATS_FILE = PROJECT_ROOT / "cache" / "exec_stats.json"
# Take at most this share (%) of the shares each book shows at the prices paid: shown shares are often
# gone, or pulled, by the time an order lands.
AUTO_TRADE_BOOK_SHARE = _env_num("AUTO_TRADE_BOOK_SHARE", 50) / 100
# Paper trading: Auto-trade does everything but send the orders, then checks the real books at the moments
# they would have landed to see what would have filled. Results go to paper_trades.jsonl.
AUTO_TRADE_DRY_RUN = _env_on("AUTO_TRADE_DRY_RUN", False)
# Fast lane: the pairs Auto-trade could take (result known within FAST_MAX_HOURS) get their own price check
# every FAST_LANE_PAUSE_SECS and go first on the live streams, instead of waiting for the full sweep.
# auto = while Auto-trade or Auto maker is on; always; off.
FAST_LANE = os.environ.get("FAST_LANE", "").strip().lower() or "auto"
if FAST_LANE not in ("auto", "always", "off"):
    FAST_LANE = "auto"
FAST_LANE_PAUSE_SECS = _env_num("FAST_LANE_PAUSE_SECS", 0.5)
# Auto-trade mode: while Auto-trade is on, only the markets it can take are refreshed (the fast lane), so it
# and its own pre-trade checks get the whole request budget. Off: everything keeps refreshing as usual.
AUTO_TRADE_FOCUS = os.environ.get("AUTO_TRADE_FOCUS", "1").strip().lower() not in ("0", "false", "no", "off")
# Paper trading (AUTO_TRADE_DRY_RUN): how long each site's order takes to land, in seconds, when no
# real trades have been timed yet.
PAPER_LATENCY = {"kalshi": 0.15, "polymarket": 0.7}
PAPER_LOG = PROJECT_ROOT / "paper_trades.jsonl"
# Unhedged first-leg shares may be hedged up to this far ($/share) above break-even when that loses less
# than selling them back. 0 = always sell back.
CLOSE_OUT_MAX_LOSS = _env_num("CLOSE_OUT_MAX_LOSS", 0.05)

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
    # Added after matching real games on both sites (Polymarket code -> Kalshi code).
    "epl": ("EPL", "soccer"),
    "lal": ("LALIGA", "soccer"),
    "bun": ("BUNDESLIGA", "soccer"),
    "sea": ("SERIEA", "soccer"),
    "lg1": ("LIGUE1", "soccer"),
    "ligpor": ("LIGAPORTUGAL", "soccer"),
    "j1": ("JLEAGUE", "soccer"),
    "wsl": ("EWSL", "soccer"),
    "cnl": ("CONCACAFNL", "soccer"),
    "uru1": ("URYPD", "soccer"),
    "svk2": ("SVK2L", "soccer"),
    "par1": ("APFDDH", "soccer"),
    "arg2": ("ARGNACB", "soccer"),
    "lpa": ("ARGPREMDIV", "soccer"),
    "lco": ("DIMAYOR", "soccer"),
    "serca": ("SERIEC", "soccer"),        # Serie C's three groups are one league on Kalshi
    "sercb": ("SERIEC", "soccer"),
    "sercc": ("SERIEC", "soccer"),
    "ahl": ("AHL", "hockey"),
    "snhl": ("NL", "hockey"),             # Swiss National League
    "ita2": ("BBSERIEA2", "basketball"),
    "lba": ("BBSERIEA", "basketball"),
    "autbl": ("AUTBSL", "basketball"),
    "lnb": ("LNBELITE", "basketball"),
    "fra2": ("LNBELITE2", "basketball"),
    "acb": ("ACB", "basketball"),
    # Tennis: every Kalshi tour's match series is one pool (TENNIS); games pair by date and player names.
    "atp": ("TENNIS", "tennis"),
    "wta": ("TENNIS", "tennis"),
    "itfme": ("TENNIS", "tennis"),
    "itfwo": ("TENNIS", "tennis"),
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
# A trade uses the live feed's book (no download) while the feed is alive: it heard from the exchange within
# LIVE_FEED_ALIVE_SECS and has this market's book from LIVE_BOOK_MAX_AGE or less ago. A book that hasn't
# changed is still current on a live feed: Kalshi's numbers every update and resyncs on a gap, Polymarket's
# sends the whole book each time, and a feed that drops clears its books.
LIVE_BOOK_MAX_AGE = 30.0
LIVE_FEED_ALIVE_SECS = 5.0
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
HOT_MAX_PAIRS = 400            # near-arb pairs re-checked between full sweeps (closest first)
GIL_SWITCH_SECS = 0.001        # a busy thread hands over to the others (stream re-checks, trades) this often
GC_FULL_EVERY_SECS = 1800      # full garbage collection at most this often (it pauses everything; see gctune)
GC_GEN2_THRESHOLD = 1000       # Python's automatic full collections: every this many middle ones (default 10)
MAKER_STREAM_BACKSTOP_SECS = 2.0   # with Polymarket's order stream, a resting order is still read this often
