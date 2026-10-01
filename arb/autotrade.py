"""Fast trade (one click, no confirm screen) and Auto-trade (no click at all) for time-sensitive arbs.

Both run the same safe sequence as Make trade (fresh books and balances, thinner leg first, the other
leg never above break-even, unhedged shares sold back), just without stopping to show you the plan.
Only rows the scanner marks "fast" qualify (engine.fast_check): crypto pairs matched by contract terms
or anything settling within FAST_MAX_HOURS, never auto-matched pairs or rows with rule warnings.

Auto-trade is off every time the scanner starts; you turn it on in the dashboard. It places one trade
at a time, at most AUTO_TRADE_MAX_TRADE each and AUTO_TRADE_DAILY_LIMIT per day, and turns itself
off if a trade leaves shares unhedged or can't be confirmed.
"""

import threading
import time
from collections import deque
from datetime import datetime

from . import config
from .trader import TradeError


def legs_of(row):
    return [{"exchange": l["exchange"].lower(), "market_id": l["market_id"], "side": l["side"]} for l in row["legs"]]


def pair_id(legs):
    return tuple(sorted(f"{l['exchange'].lower()}:{l['market_id']}:{l['side']}" for l in legs))


def in_play(row):
    """A game that has already started (the scanner flags it in the row's warnings)."""
    return any(w.startswith("Game already started") for w in row.get("warnings") or [])


def row_info(row):
    return {"game": row.get("game"), "tab": row.get("tab"), "closes": row.get("closes")} if row else None


def spent(result):
    """Money a trade actually used: the shares still held on both sites (sold-back shares excluded)."""
    return round(sum(l.get("paid") or 0 for l in (result.get("legs_filled") or {}).values()), 2)


def record_trade(scanner, result, row, label="Trade"):
    """After any trade: refresh cash, add it to My arbs, and log it."""
    threading.Thread(target=scanner.refresh_balances, daemon=True).start()
    try:
        if result.get("plan"):
            scanner.my_arbs.add_from_trade(result["plan"], result, row)
    except Exception as e:
        scanner.log(f"Couldn't add the trade to My arbs: {e!r}")
    scanner.log(f"{label} {result['status']}: {result['hedged_pairs']:g} pairs hedged, net ${result['net']:.2f}" +
                (f", {result['unhedged_shares']:g} UNHEDGED" if result["unhedged_shares"] else ""))


def current_row(scanner, legs):
    """The scanner's latest row for these legs, or None if it isn't listed any more."""
    want = pair_id(legs)
    with scanner.lock:
        rows = list(scanner.state.get("opportunities") or [])
    return next((r for r in rows if pair_id(r["legs"]) == want), None)


def fast_trade(scanner, legs, max_invest=None, label="Fast trade", min_profit=0.0, min_roi=0.0, cap=None):
    """Plan and place a trade in one step, for an arb the scanner currently lists as fast."""
    row = current_row(scanner, legs)
    if row is None:
        raise TradeError("This arb isn't on the list any more (the prices moved). Nothing was traded.")
    fast = row.get("fast") or {}
    if not fast.get("ok"):
        raise TradeError(f"{label} isn't allowed for this arb ({fast.get('why', 'not checked')}). Use Make trade.")
    cap = min(x for x in (cap or config.FAST_MAX_TRADE, max_invest) if x)
    trader = scanner.trader
    plan = trader.prepare(legs, cap)
    roi = plan["expected_profit"] / plan["capital"] if plan["capital"] else 0
    if plan["expected_profit"] < min_profit or roi < min_roi:
        trader.plans.pop(plan["id"], None)
        raise TradeError(f"Only ${plan['expected_profit']:.2f} profit ({roi * 100:.2f}%) at live prices within "
                         f"${cap:.2f}. Nothing was traded.")
    result = trader.execute(plan["id"])
    result["capital"] = round(plan["capital"], 2)
    record_trade(scanner, result, row_info(row), label)
    return result


class AutoTrader:
    def __init__(self, scanner, run_async=True):
        self.scanner, self.run_async = scanner, run_async
        self.on, self.busy, self.halted = False, False, None
        self.lock = threading.Lock()
        self.spend = {}                 # local date -> dollars used by Auto-trade
        self.net = {}                   # local date -> net profit locked by Auto-trade
        self.tried = {}                 # pair id -> time of the last attempt
        self.game_pause = {}            # game -> time until which it's skipped (after a miss)
        self.history = deque(maxlen=20)

    @staticmethod
    def _today():
        return datetime.now().strftime("%Y-%m-%d")

    def spent_today(self):
        return round(self.spend.get(self._today(), 0.0), 2)

    def set(self, on):
        if on and not self.scanner.trader.venues:
            raise TradeError(f"Trading is {self.scanner.trading_status}.")
        with self.lock:
            self.on, self.halted = bool(on), None
        self.scanner.log(f"Auto-trade turned {'on' if on else 'off'}" + (
            f": up to ${config.AUTO_TRADE_MAX_TRADE:g} per trade, ${config.AUTO_TRADE_DAILY_LIMIT:g} per day, "
            f"profit at least ${config.AUTO_TRADE_MIN_PROFIT:g}" if on else ""))
        return self.status()

    def status(self):
        return {"on": self.on, "busy": self.busy, "halted": self.halted, "spent_today": self.spent_today(),
                "net_today": round(self.net.get(self._today(), 0.0), 2),
                "daily_limit": config.AUTO_TRADE_DAILY_LIMIT, "max_trade": config.AUTO_TRADE_MAX_TRADE,
                "min_profit": config.AUTO_TRADE_MIN_PROFIT, "min_roi": config.AUTO_TRADE_MIN_ROI,
                "live_games": config.AUTO_TRADE_LIVE_GAMES, "max_daily_loss": config.AUTO_TRADE_MAX_DAILY_LOSS,
                "fast_max_trade": config.FAST_MAX_TRADE, "fast_max_hours": config.FAST_MAX_HOURS,
                "allow_auto_matched": config.FAST_ALLOW_AUTO_MATCHED, "allow_too_good": config.FAST_ALLOW_TOO_GOOD,
                "history": list(self.history)}

    def pick(self, rows, now=None):
        """The most profitable fast row worth trading that wasn't tried in the last cooldown."""
        now = now or time.time()
        ok = [r for r in rows if (r.get("fast") or {}).get("ok")
              and (r.get("profit") or 0) >= config.AUTO_TRADE_MIN_PROFIT
              and (r.get("roi") or 0) >= config.AUTO_TRADE_MIN_ROI
              and (config.AUTO_TRADE_LIVE_GAMES or not in_play(r))
              and self.game_pause.get(r.get("game"), 0) <= now
              and now - self.tried.get(pair_id(r["legs"]), 0) >= config.AUTO_TRADE_COOLDOWN_SECS]
        return max(ok, key=lambda r: r["profit"], default=None)

    def check(self, rows):
        """Called after every price pass. Starts at most one trade; returns the row picked."""
        with self.lock:
            if not self.on or self.halted or self.busy:
                return None
            room = config.AUTO_TRADE_DAILY_LIMIT - self.spent_today()
            if room < 1:
                return None
            row = self.pick(rows)
            if row is None:
                return None
            self.busy = True
            self.tried[pair_id(row["legs"])] = time.time()
        args = (row, min(config.AUTO_TRADE_MAX_TRADE, room))
        if self.run_async:
            threading.Thread(target=self._run, args=args, daemon=True).start()
        else:
            self._run(*args)
        return row

    def _run(self, row, cap):
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "game": row.get("game"),
                 "tab": row.get("tab"), "legs": [f"{l['exchange']} Buy {l['side'].upper()}" for l in row["legs"]]}
        try:
            res = fast_trade(self.scanner, legs_of(row), label="Auto-trade", cap=cap,
                             min_profit=config.AUTO_TRADE_MIN_PROFIT, min_roi=config.AUTO_TRADE_MIN_ROI)
            used = spent(res)
            with self.lock:
                self.spend[self._today()] = self.spend.get(self._today(), 0.0) + used
                self.net[self._today()] = self.net.get(self._today(), 0.0) + res["net"]
            entry.update({"status": res["status"], "pairs": res["hedged_pairs"], "spent": used, "net": res["net"],
                          "unhedged": res["unhedged_shares"]})
            if res["status"] in ("partial", "no_fill"):
                # The second leg missed (or the first didn't fill): leave this game alone for a while.
                self.game_pause[row.get("game")] = time.time() + config.AUTO_TRADE_GAME_COOLDOWN_SECS
                entry["note"] = "; ".join(res.get("steps") or []) + (f" {res['note']}" if res.get("note") else "")
            if self.net.get(self._today(), 0.0) <= -config.AUTO_TRADE_MAX_DAILY_LOSS:
                self._halt(f"net loss today is ${-self.net[self._today()]:.2f} (limit ${config.AUTO_TRADE_MAX_DAILY_LOSS:g}): "
                           f"check what's happening before turning it back on", notify=False)
            if res["status"] == "unknown" or res["unhedged_shares"] > 0:
                self._halt(f"last trade left {res['unhedged_shares']:g} shares unhedged or unconfirmed: check "
                           f"both accounts, then turn Auto-trade back on", notify=False)
            if res["hedged_pairs"] > 0 or self.halted:
                self._notify(f"Auto-trade {res['status']}: {row.get('game')}: {res['hedged_pairs']:g} pairs, "
                             f"${used:.2f} in, net ${res['net']:+.2f}" + (f"\n⚠ {self.halted}" if self.halted else ""))
        except TradeError as e:           # books moved, not enough cash, ...: nothing was traded
            entry.update({"status": "skipped", "note": str(e)})
            if "Check that account" in str(e):
                self._halt(str(e))
        except Exception as e:
            entry.update({"status": "error", "note": repr(e)})
            self._halt(f"unexpected error ({e!r}): check both accounts")
        finally:
            self.history.appendleft(entry)
            self.busy = False

    def _halt(self, why, notify=True):
        with self.lock:
            self.on, self.halted = False, why
        self.scanner.log(f"Auto-trade stopped: {why}")
        if notify:
            self._notify(f"Auto-trade stopped: {why}")

    def _notify(self, text):
        alerter = getattr(self.scanner, "alerter", None)
        if alerter and alerter.enabled:
            threading.Thread(target=alerter._safe_send, args=(text,), daemon=True).start()
