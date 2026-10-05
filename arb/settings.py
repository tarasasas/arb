"""Trading settings you can change from the dashboard (Settings). A change applies right away and is
saved to .env, so it's still set after a restart; editing .env by hand keeps working too."""

import os
import re
import threading

from . import config

# (key, kind, label, help, default). kind: bool | money | number | int | percent | cents | choice:<a>,<b>,...
# percent and cents are stored in config as fractions / dollars (shown x100).
GROUPS = [
    ("What Auto-trade and Fast trade may take", [
        ("FAST_MAX_HOURS", "number", "Only arbs whose result is known within (hours)",
         "When the game ends or the event happens, even if a site pays out later (Polymarket's end dates often "
         "run weeks past the event). Arbs decided later need Make trade, or the long-dated return below.", 24),
        ("AUTO_TRADE_LONG_DAYS", "number", "Also arbs whose result is known within (days), at a higher return",
         "Long-dated arbs: the money is tied up until the result is known, so they need the return below. In "
         "Auto-trade mode their prices are checked about every 10 seconds (live-feed markets as soon as they move). "
         "0 = off.", 90),
        ("AUTO_TRADE_LONG_MIN_ROI", "percent", "Minimum return for those (%)",
         "On the trade itself: only as many shares as keep this return are bought, with no minimum size or profit. "
         "If Minimum return (Auto-trade) is higher, that counts.", 4),
        ("FAST_ALLOW_AUTO_MATCHED", "bool", "Pairs matched by wording you haven't checked",
         "Non-sports pairs the matcher paired automatically. A wrong match can lose on both sides.", True),
        ("FAST_ALLOW_TOO_GOOD", "bool", "Rows flagged too good to be true",
         "Prices are always re-checked live first, but a big gap usually means a stale quote or a different question.", True),
        ("FAST_ALLOW_PLAYER_PROPS", "bool", "Player props",
         "If the player doesn't play, each site settles at its own fair price, so the pair may not pay exactly $1.", True),
        ("AUTO_TRADE_LIVE_GAMES", "bool", "Games already in progress (Auto-trade and Auto maker)",
         "Live prices move between the two orders, so the second leg misses more often.", False),
        ("AUTO_TRADE_CRYPTO_WINDOWS", "bool", "Crypto Up/Down windows (Auto-trade)",
         "BTC, ETH and other coins' price at a set minute. The price moves every second, so the second leg often "
         "misses. Fast trade by hand can still take them.", False),
        ("AUTO_TRADE_MIN_ROI", "percent", "Minimum return (Auto-trade)", "Of the money put in.", 0.5),
        ("AUTO_TRADE_MIN_PROFIT", "money", "Minimum profit per trade (Auto-trade)", "0 = only the minimum return counts.", 0),
    ]),
    ("Speed", [
        ("FAST_LANE", "choice:auto,always,off", "Fast lane",
         "The pairs Auto-trade could take (paying out within the hours above) get their own price check about every "
         "half second and go first on the live streams, instead of waiting for the full sweep. auto: while Auto-trade "
         "or Auto maker is on. always: also for Fast trade by hand. off: everything waits for the normal scan.", "auto"),
        ("AUTO_TRADE_FOCUS", "bool", "Auto-trade mode: only refresh what Auto-trade can take",
         "While Auto-trade is on, the full scan, near-arb re-checks, non-sports suggestions and the My positions "
         "check pause, and the fast lane gets all the request slots. Market lists and your cash still refresh. Other "
         "rows on the dashboard stop updating until Auto-trade is off (Make trade still re-checks live prices).",
         True),
    ]),
    ("Auto-trade limits", [
        ("AUTO_TRADE_MAX_TRADE", "money", "Per trade", "Both legs together.", 25),
        ("AUTO_TRADE_DAILY_LIMIT", "money", "Per day", "Money Auto-trade may put in each day.", 100),
        ("AUTO_TRADE_MAX_DAILY_LOSS", "money", "Turn off after losing", "Net loss in one day.", 5),
        ("AUTO_TRADE_MAX_MISSES", "int", "Turn off after misses in a row", "On one site: rejected, unfilled or unhedged.", 3),
        ("AUTO_TRADE_HEDGE_DEPTH", "number", "Second leg's book must hold (x the shares)",
         "Within break-even; a thin book is what makes the second leg miss.", 2),
    ]),
    ("How Auto-trade trades", [
        ("AUTO_TRADE_DRY_RUN", "bool", "Paper trading: no real orders",
         "Auto-trade does everything but send the orders, then checks the real books at the moments they would have "
         "landed. Results show in the Auto-trade bar and paper_trades.jsonl; nothing is spent.", False),
        ("AUTO_TRADE_ORDER", "choice:smart,thinner_first,together,polymarket_first", "Order of the two legs",
         "smart: the stale side first when one site just moved and the other hasn't (it's about to reprice); both at "
         "once in fast markets or where second legs keep missing; else the thinner book first. thinner_first: the "
         "thinner book first, the other for what filled. together: both at once. polymarket_first: Polymarket first.",
         "smart"),
        ("AUTO_TRADE_FAST_EDGE", "cents", "Minimum edge in fast markets (¢ per pair)",
         "Crypto windows and games in progress move between the two orders.", 2),
        ("AUTO_TRADE_LEARN_BUFFER", "bool", "Learn each market type's buffer",
         "Also require the typical price move that type's trades met while the orders went out.", True),
        ("AUTO_TRADE_THROTTLE", "bool", "Pause market types that keep failing",
         "When most of a type's last trades missed on a site, or they lost money in total.", True),
        ("AUTO_TRADE_THROTTLE_MIN_FILL", "percent", "Pause below this fill rate (%)",
         "Share of its last 5 trades that filled on both sites.", 40),
        ("AUTO_TRADE_THROTTLE_HOURS", "number", "Pause for (hours)", "", 2),
        ("AUTO_TRADE_BOOK_SHARE", "percent", "Take at most this share of each book (%)",
         "Of the shares shown at the prices paid; shown shares are often gone by the time an order lands. 100 = all.",
         50),
    ]),
    ("EV bot (single bets, not hedged)", [
        ("EV_BOT_PROFILE", "choice:careful,normal,aggressive,custom", "Aggressiveness",
         "Fills in the settings below (you can still change any of them; it then shows custom). careful: bigger "
         "edges only, small bets, no games in progress. normal: the defaults. aggressive: smaller edges (1c), "
         "wider books, half-Kelly stakes up to $25 a bet and $200 a day, up to 3 bets per game, games in progress "
         "with little extra edge, a looser stale-quote window, and the cheap side of arbs Auto-trade won't take. "
         "More bets and bigger swings: smaller edges are likelier to be a wrong fair price.",
         "normal"),
        ("EV_BOT_PAPER", "bool", "Paper trading: no real orders",
         "Everything but the orders, filled against the real books; results show in the EV bot bar. Each real bet "
         "can lose: give paper trading a few hundred bets and check its closing value first.", True),
        ("EV_BOT_MIN_EDGE", "cents", "Minimum edge (¢ per share, after the fee)", "Below the fair price.", 2),
        ("EV_BOT_MIN_ROI", "percent", "Minimum edge (% of what the bet costs)", "", 4),
        ("EV_BOT_MAX_BET", "money", "Per bet", "", 10),
        ("EV_BOT_DAILY_LIMIT", "money", "Per day", "Money staked each day.", 50),
        ("EV_BOT_BANKROLL", "money", "Bankroll for sizing", "Bets are a fraction of the Kelly stake on this (or your "
         "cash on that site, if less).", 200),
        ("EV_BOT_KELLY", "number", "Fraction of the Kelly stake", "0.25 = quarter Kelly. Full Kelly swings hard when "
         "the fair price is off.", 0.25),
        ("EV_BOT_MAX_OPEN", "int", "Open bets at most", "Across all games (see bets per game below).", 10),
        ("EV_BOT_MAX_HOURS", "number", "Only games whose result is known within (hours)", "", 24),
        ("EV_BOT_LIVE_GAMES", "bool", "Games in progress too",
         "Stale quotes are most common in play, but prices jump and Polymarket can hold in-play orders a moment. Live "
         "bets need the extra edge below and quotes under 3 seconds old.", True),
        ("EV_BOT_LIVE_EXTRA_EDGE", "cents", "Extra edge for games in progress (¢ per share)",
         "On top of the minimum edge.", 1),
        ("EV_BOT_PER_GAME", "int", "Bets per game at most", "Open bets on one game.", 1),
        ("EV_BOT_TAKE_ARBS", "bool", "Bet the cheap side of arbs Auto-trade won't take",
         "When Auto-trade is off (or the game is in progress and Auto-trade skips those), an arb's cheap side is "
         "the biggest edge there is. Hedged by Auto-trade it's risk-free: turning Auto-trade on is better.", False),
        ("EV_BOT_PROPS", "bool", "Player props",
         "A player who sits out settles at a fair price on each site: about even for a single bet.", True),
        ("EV_BOT_STALE_FRESH_SECS", "number", "Stale quote: the other site moved within (seconds)", "", 2),
        ("EV_BOT_STALE_GAP_SECS", "number", "...and this site hadn't moved for at least (seconds) longer",
         "Smaller numbers catch more stale quotes, and more that weren't really stale.", 3),
        ("EV_BOT_MAX_SPREAD", "cents", "Widest book used for a fair price (¢)",
         "Between a market's YES ask and 1 - its NO ask. A wider book's middle isn't much of a price.", 4),
        ("EV_BOT_MAX_DISAGREE", "cents", "Most the two sites' prices may differ (¢)",
         "Further apart usually means a wrong match, or a move too big to call.", 8),
        ("EV_BOT_COOLDOWN_SECS", "number", "Wait before betting the same market again (seconds)", "", 600),
        ("EV_BOT_MIN_LEAD_SECS", "number", "No bets this close to the start (seconds)",
         "Prices jump at lineups and kickoff.", 300),
    ]),
    ("Every trade", [
        ("MAX_TRADE_DOLLARS", "money", "Hard cap per trade", "Make trade, Fast trade and Auto-trade, both legs together.", 100),
        ("FAST_MAX_TRADE", "money", "Fast trade, per click", "Both legs together.", 50),
        ("TRADE_ORDER", "choice:together,thinner_first,polymarket_first", "Order of the two legs (Make trade and Fast trade)",
         "together: both at once. thinner_first: the thinner book first. polymarket_first: Polymarket first.", "together"),
        ("CLOSE_OUT_MAX_LOSS", "money", "Hedge leftover shares up to ($ per share above break-even)",
         "When that loses less than selling them back. 0 = always sell back.", 0.05),
        ("KALSHI_SHARD_MODE", "choice:even,per_trade,manual", "Kalshi cash across exchange shards",
         "An order can only use its own market's shard's cash. even: every shard keeps an equal share (Kalshi "
         "rebalances about every 10 seconds), so no trade waits for cash. per_trade: a trade first moves the cash it "
         "needs to its shard, which takes seconds. manual: left as you set it (kalshi-shards.bat or kalshi.com).",
         "even"),
    ]),
    ("Auto maker", [
        ("MAKER_AUTO_MAX_ORDER", "money", "Per resting order", "Both legs together.", 25),
        ("MAKER_AUTO_MAX_RESTING", "money", "Resting at once, all orders", "", 100),
        ("MAKER_AUTO_MAX_ORDERS", "int", "Resting orders at once", "", 2),
        ("MAKER_AUTO_TTL_SECS", "number", "Cancel each order after (seconds)", "Polymarket expires it even if this app stops.", 120),
        ("MAKER_AUTO_DAILY_LIMIT", "money", "Filled per day", "", 200),
    ]),
]
SPEC = {key: (kind, label, help_, default) for _, items in GROUPS for key, kind, label, help_, default in items}

# What each EV bot aggressiveness sets, in the units the dashboard shows (cents, percent, dollars, seconds).
# The bankroll isn't part of it: that's your money, not a style.
EV_PROFILES = {
    "careful": {"EV_BOT_MIN_EDGE": 3, "EV_BOT_MIN_ROI": 6, "EV_BOT_MAX_BET": 5, "EV_BOT_DAILY_LIMIT": 25,
                "EV_BOT_KELLY": 0.15, "EV_BOT_MAX_OPEN": 5, "EV_BOT_MAX_HOURS": 24, "EV_BOT_LIVE_GAMES": False,
                "EV_BOT_LIVE_EXTRA_EDGE": 2, "EV_BOT_PER_GAME": 1, "EV_BOT_MAX_SPREAD": 3, "EV_BOT_MAX_DISAGREE": 6,
                "EV_BOT_COOLDOWN_SECS": 900, "EV_BOT_MIN_LEAD_SECS": 600, "EV_BOT_TAKE_ARBS": False,
                "EV_BOT_PROPS": False, "EV_BOT_STALE_FRESH_SECS": 2, "EV_BOT_STALE_GAP_SECS": 3},
    "normal": {"EV_BOT_MIN_EDGE": 2, "EV_BOT_MIN_ROI": 4, "EV_BOT_MAX_BET": 10, "EV_BOT_DAILY_LIMIT": 50,
               "EV_BOT_KELLY": 0.25, "EV_BOT_MAX_OPEN": 10, "EV_BOT_MAX_HOURS": 24, "EV_BOT_LIVE_GAMES": True,
               "EV_BOT_LIVE_EXTRA_EDGE": 1, "EV_BOT_PER_GAME": 1, "EV_BOT_MAX_SPREAD": 4, "EV_BOT_MAX_DISAGREE": 8,
               "EV_BOT_COOLDOWN_SECS": 600, "EV_BOT_MIN_LEAD_SECS": 300, "EV_BOT_TAKE_ARBS": False,
               "EV_BOT_PROPS": True, "EV_BOT_STALE_FRESH_SECS": 2, "EV_BOT_STALE_GAP_SECS": 3},
    "aggressive": {"EV_BOT_MIN_EDGE": 1, "EV_BOT_MIN_ROI": 2, "EV_BOT_MAX_BET": 25, "EV_BOT_DAILY_LIMIT": 200,
                   "EV_BOT_KELLY": 0.5, "EV_BOT_MAX_OPEN": 25, "EV_BOT_MAX_HOURS": 48, "EV_BOT_LIVE_GAMES": True,
                   "EV_BOT_LIVE_EXTRA_EDGE": 0.5, "EV_BOT_PER_GAME": 3, "EV_BOT_MAX_SPREAD": 6, "EV_BOT_MAX_DISAGREE": 12,
                   "EV_BOT_COOLDOWN_SECS": 120, "EV_BOT_MIN_LEAD_SECS": 60, "EV_BOT_TAKE_ARBS": True,
                   "EV_BOT_PROPS": True, "EV_BOT_STALE_FRESH_SECS": 5, "EV_BOT_STALE_GAP_SECS": 1},
}


def ev_profile():
    """The aggressiveness the EV bot's settings match right now, or "custom"."""
    for name, values in EV_PROFILES.items():
        if all(abs(float(_shown(k)) - float(v)) < 1e-9 for k, v in values.items()):
            return name
    return "custom"
_lock = threading.Lock()


def _shown(key):
    """config's value in the units the dashboard and .env use."""
    if key == "EV_BOT_PROFILE":
        return ev_profile()                # whatever the settings themselves add up to
    kind, v = SPEC[key][0], getattr(config, key)
    return round(v * 100, 4) if kind in ("percent", "cents") else v


def current():
    return {"profiles": {"EV_BOT_PROFILE": EV_PROFILES}, "groups": [{"title": title, "items": [
        {"key": key, "kind": kind.split(":")[0], "label": label, "help": help_, "default": default,
         "value": _shown(key), "options": kind.split(":", 1)[1].split(",") if kind.startswith("choice:") else None}
        for key, kind, label, help_, default in items]} for title, items in GROUPS]}


def _parse(key, raw):
    kind = SPEC[key][0]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if str(raw).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if str(raw).strip().lower() in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{SPEC[key][1]}: on or off")
    if kind.startswith("choice:"):
        if raw not in kind.split(":", 1)[1].split(","):
            raise ValueError(f"{SPEC[key][1]}: not one of the choices")
        return raw
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{SPEC[key][1]}: needs a number")
    if not 0 <= v <= 1_000_000 or v != v:
        raise ValueError(f"{SPEC[key][1]}: needs a number from 0 up")
    if kind == "int":
        if v < 1 or v != int(v):
            raise ValueError(f"{SPEC[key][1]}: needs a whole number from 1 up")
        return int(v)
    return v


def _env_text(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _apply(key, v):
    setattr(config, key, v / 100 if SPEC[key][0] in ("percent", "cents") else v)
    if key == "TRADE_ORDER":
        config.TRADE_LEGS_TOGETHER = v == "together"
    os.environ[key] = _env_text(v)


def write_env(values, path=None):
    """Set KEY=value lines in .env: an existing line is replaced in place (comments, keys and every
    other line are kept as they are); new keys go at the end. Written to a temp file, then swapped in."""
    path = path or config.PROJECT_ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], set()
    for raw in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", raw)
        if m and m[1] in values:
            if m[1] not in done:
                out.append(f"{m[1]}={values[m[1]]}")
                done.add(m[1])
            continue
        out.append(raw)
    new = [k for k in values if k not in done]
    if new:
        header = "# Set from the dashboard (Settings)"
        if header not in out:
            if out and out[-1].strip():
                out.append("")
            out.append(header)
        out += [f"{k}={values[k]}" for k in new]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def update(changes, path=None):
    """changes: {KEY: value}. All are checked before any is applied. Returns current()."""
    parsed = {}
    for key, raw in (changes or {}).items():
        if key not in SPEC:
            raise ValueError(f"Unknown setting {key}")
        parsed[key] = _parse(key, raw)
    profile = parsed.get("EV_BOT_PROFILE")
    if profile in EV_PROFILES:             # the profile's values, then anything set alongside it on top
        parsed = {**{k: _parse(k, v) for k, v in EV_PROFILES[profile].items()}, **parsed}
    if not parsed:
        return current()
    with _lock:
        write_env({k: _env_text(v) for k, v in parsed.items()}, path)
        for k, v in parsed.items():
            _apply(k, v)
    return current()
