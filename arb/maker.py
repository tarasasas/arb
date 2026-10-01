"""Auto maker: runs Maker mode rows by itself.

For a Maker mode row it rests one post-only Polymarket buy at the row's price (it can't fill as a
taker, and Polymarket expires it after MAKER_AUTO_TTL_SECS even if this app stops). Then, every
MAKER_AUTO_POLL_SECS, it reads the order: whatever filled since the last look is bought on Kalshi
right away, immediate-or-cancel, never above the hedge limit (the Kalshi price where the pair still
profits at what the Polymarket shares really cost). It cancels the rest when Kalshi's price moves
past the hedge limit, when the arb leaves the Maker mode list, when time runs out, or when a hedge
misses; then it hedges any last fills and sells back Polymarket shares it couldn't hedge.

Off every time the scanner starts. It turns itself off after AUTO_TRADE_MAX_MISSES orders in a row
leave shares to sell back, after AUTO_TRADE_MAX_DAILY_LOSS lost in a day, or if an order's state
can't be read.
"""

import json
import math
import threading
import time
from collections import deque
from datetime import datetime

from . import config, engine
from .autotrade import in_play, pair_id
from .http import ApiError, priority
from .model import YES, fee_per_contract
from .venues import floor_to

TERMINAL = {"ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED",
            "ORDER_STATE_REPLACED"}


def _today():
    return datetime.now().strftime("%Y-%m-%d")


class MakerBot:
    def __init__(self, scanner, run_async=True, sleep=time.sleep, clock=time.time):
        self.scanner, self.run_async, self.sleep, self.clock = scanner, run_async, sleep, clock
        self.on, self.halted = False, None
        self.lock = threading.Lock()
        self.active = {}                 # pair id -> live order record (shown on the dashboard)
        self.tried = {}                  # pair id -> time the last order ended
        self.history = deque(maxlen=20)
        self.misses = 0                  # orders in a row that left shares to sell back
        self.filled = {}                 # date -> dollars filled (both legs)
        self.net = {}                    # date -> net profit locked

    # ---- switch and status -----------------------------------------------------------------

    def set(self, on):
        from .trader import TradeError
        if on and not self.scanner.trader.venues:
            raise TradeError(f"Trading is {self.scanner.trading_status}.")
        with self.lock:
            self.on, self.halted = bool(on), None
            if on:
                self.misses = 0
        self.scanner.log(f"Auto maker turned {'on' if on else 'off'}" + (
            f": up to ${config.MAKER_AUTO_MAX_ORDER:g} per order, {config.MAKER_AUTO_MAX_ORDERS} at once, "
            f"{config.MAKER_AUTO_TTL_SECS:g}s each" if on else ": cancelling resting orders"))
        return self.status()

    def status(self):
        with self.lock:
            active = [{k: v for k, v in o.items() if not k.startswith("_")} for o in self.active.values()]
        return {"on": self.on, "halted": self.halted, "active": active, "history": list(self.history),
                "misses": self.misses, "max_misses": config.AUTO_TRADE_MAX_MISSES,
                "filled_today": round(self.filled.get(_today(), 0.0), 2), "net_today": round(self.net.get(_today(), 0.0), 2),
                "max_order": config.MAKER_AUTO_MAX_ORDER, "max_resting": config.MAKER_AUTO_MAX_RESTING,
                "max_orders": config.MAKER_AUTO_MAX_ORDERS, "ttl": config.MAKER_AUTO_TTL_SECS,
                "daily_limit": config.MAKER_AUTO_DAILY_LIMIT, "fast_max_hours": config.FAST_MAX_HOURS}

    def cancel(self, order_id):
        """Ask one order's watcher to cancel it (dashboard button)."""
        with self.lock:
            for o in self.active.values():
                if o["id"] == order_id:
                    o["_stop"] = "you cancelled it"
                    return True
        return False

    def stop_all(self, why="the scanner is stopping"):
        """Cancel every resting order now (on shutdown); the watchers then finish up."""
        with self.lock:
            orders = list(self.active.values())
            for o in orders:
                o["_stop"] = why
        pv = (self.scanner.trader.venues or {}).get("polymarket")
        for o in orders:
            try:
                pv.cancel(o["id"], o["slug"])
            except Exception:
                pass

    # ---- picking ---------------------------------------------------------------------------

    def _resting_dollars(self):
        return sum(o["capital"] for o in self.active.values())

    def pick(self, rows, now=None):
        now = now or self.clock()
        hours = config.FAST_MAX_HOURS * 3600
        ok = []
        for r in rows:
            m = r.get("maker") or {}
            if not m.get("hedge_limit") or (r.get("profit") or 0) <= 0:
                continue
            if r.get("suspicious") or (not config.AUTO_TRADE_LIVE_GAMES and in_play(r)):
                continue
            closes = engine._parse_time(r.get("closes")) if r.get("closes") else None
            if not closes or (closes - engine.now_utc()).total_seconds() > hours:
                continue                       # same rule as Auto-trade: settles within FAST_MAX_HOURS
            pid = pair_id(r["legs"])
            if pid in self.active or now - self.tried.get(pid, 0) < config.MAKER_AUTO_COOLDOWN_SECS:
                continue
            ok.append(r)
        return max(ok, key=lambda r: r.get("edge_per_contract") or 0, default=None)

    def check(self, rows):
        """Called after each price pass with the Maker mode rows. Starts at most one order."""
        with self.lock:
            if not self.on or self.halted or len(self.active) >= config.MAKER_AUTO_MAX_ORDERS:
                return None
            room = min(config.MAKER_AUTO_MAX_RESTING - self._resting_dollars(),
                       config.MAKER_AUTO_DAILY_LIMIT - self.filled.get(_today(), 0.0), config.MAKER_AUTO_MAX_ORDER)
            if room < 1:
                return None
            row = self.pick(rows or [])
            if row is None:
                return None
            pid = pair_id(row["legs"])
            rec = {"id": "", "pair": pid, "game": row.get("game"), "slug": row["legs"][1]["market_id"],
                   "ticker": row["legs"][0]["market_id"], "state": "placing", "posted": None, "capital": room,
                   "size": 0, "filled": 0, "hedged": 0, "cost": row["maker"]["cost"],
                   "hedge_limit": row["maker"]["hedge_limit"], "started": self.clock()}
            self.active[pid] = rec
        if self.run_async:
            threading.Thread(target=self._guard, args=(row, rec, room), daemon=True).start()
        else:
            self._guard(row, rec, room)
        return row

    # ---- one order, start to finish --------------------------------------------------------

    def _guard(self, row, rec, room):
        entry = {"time": datetime.now().isoformat(timespec="seconds"), "game": row.get("game")}
        try:
            with priority():
                entry.update(self._run(row, rec, room))
        except Exception as e:
            entry.update({"status": "error", "note": repr(e)})
            self._halt(f"unexpected error ({e!r}): check your Polymarket open orders and both accounts")
        finally:
            with self.lock:
                self.active.pop(rec["pair"], None)
                self.tried[rec["pair"]] = self.clock()
            self.history.appendleft(entry)

    def _run(self, row, rec, room):
        tr = self.scanner.trader
        kv, pv = tr.venues["kalshi"], tr.venues["polymarket"]
        kl, pl = row["legs"][0], row["legs"][1]
        ticker, slug, sk, sp = kl["market_id"], pl["market_id"], kl["side"], pl["side"]
        kcoef = (row.get("fee_coef") or {}).get("kalshi", config.KALSHI_TAKER_COEF)
        payout, cost, hl0 = row["payout"], row["maker"]["cost"], row["maker"]["hedge_limit"]
        log = {"kind": "maker", "row": {k: row.get(k) for k in ("game", "maker", "legs", "payout")},
               "orders": [], "started": engine.now_utc().isoformat()}

        # Size: what fits the budget, with Kalshi holding twice the shares at or below the hedge limit now.
        kinfo = kv.market_info(ticker)
        if not kinfo.get("open"):
            return {"status": "skipped", "note": "Kalshi market not open"}
        levels = kv.levels(ticker)[sk]
        depth = sum(q for p, q in levels if p <= hl0 + 1e-9)
        per_pair = cost + hl0 + fee_per_contract(kcoef, hl0)
        size = math.floor(min(row.get("size") or 0, room / per_pair, depth / config.AUTO_TRADE_HEDGE_DEPTH))
        if size < 1:
            return {"status": "skipped", "note": f"Kalshi has {depth:g} shares at or below the hedge limit "
                                                 f"${hl0:.2f}: too few to hedge safely"}
        # Cash on both sides first: a fill we can't hedge for lack of Kalshi cash is the worst outcome.
        shard = kinfo.get("shard") or 0
        need_k = size * (hl0 + fee_per_contract(kcoef, hl0))
        k_cash = tr._cached_cash("kalshi", shard)
        k_cash = kv.balance(shard) if k_cash is None else k_cash
        if k_cash < need_k and config.KALSHI_AUTO_SHARD_FUNDING and hasattr(kv, "fund_shard"):
            try:
                _, k_cash = kv.fund_shard(shard, math.ceil((need_k - k_cash + 0.05) * 100) / 100)
            except ApiError:
                pass
        if k_cash < need_k:
            size = math.floor(k_cash / (need_k / size))
        p_cash = tr._cached_cash("polymarket")
        p_cash = pv.balance() if p_cash is None else p_cash
        size = min(size, math.floor(p_cash / cost))
        if size < 1:
            return {"status": "skipped", "note": f"not enough cash (Kalshi shard {shard} ${k_cash:.2f}, "
                                                 f"Polymarket ${p_cash:.2f})"}

        try:
            oid, req, resp = pv.post_maker(slug, sp, size, cost, config.MAKER_AUTO_TTL_SECS)
        except ApiError as e:            # e.g. it would have crossed (price moved): nothing rests
            log["orders"].append({"kind": "post", "error": str(e)})
            self._write(log, "rejected")
            return {"status": "skipped", "note": f"Polymarket refused the order: {e.detail}"}
        log["orders"].append({"kind": "post", "order_id": oid, "request": req, "response": resp})
        with self.lock:
            rec.update(id=oid, state="resting", posted=self.clock(), size=size, capital=round(size * per_pair, 2))
        self.scanner.log(f"Auto maker: resting {size} {sp.upper()} at ${cost:.3f} on {row.get('game')} "
                         f"(hedge on Kalshi at ≤ ${hl0:.2f})")

        filled = p_paid = p_fee = 0.0     # Polymarket side, from the order's state
        hedged = k_paid = k_fee = 0.0     # Kalshi side
        why, deadline, errors, hedge_misses = None, rec["posted"] + config.MAKER_AUTO_TTL_SECS, 0, 0
        after_cancel = None               # reads left once cancelled, waiting for the final state
        while True:
            try:
                o = pv.order(oid)
                errors = 0
            except Exception as e:
                errors += 1
                if errors >= 5:
                    why = why or f"couldn't read the order ({e!r})"
                    self._halt(f"couldn't read Polymarket order {oid}: cancel it on polymarket.us if it's still open")
                    break
                self.sleep(config.MAKER_AUTO_POLL_SECS)
                continue
            filled, p_paid, p_fee = pv.maker_fills(o, cost, None)
            # Hedge whatever filled and isn't hedged yet, at the limit for what those shares really cost.
            todo = floor_to(filled - hedged, kinfo.get("min_qty") or 1)
            if todo > 0:
                per = (p_paid + p_fee) / filled
                limit = engine.hedge_limit(payout, per, 0.0, kcoef)
                f = None
                if limit:
                    try:
                        f = kv.buy(ticker, sk, todo, limit, kcoef)
                        log["orders"].append({"kind": "hedge", "qty": f.qty, "amount": f.amount, "fee": f.fee,
                                              "limit": limit, "order_id": f.order_id})
                    except ApiError as e:
                        log["orders"].append({"kind": "hedge", "error": str(e)})
                hedged += f.qty if f else 0
                k_paid += f.amount if f else 0
                k_fee += f.fee if f else 0
                if not f or f.qty < todo:
                    hedge_misses += 1
                    why = why or "Kalshi didn't fill the hedge at the limit"
            with self.lock:
                rec.update(filled=filled, hedged=hedged, state=o.get("state", "").replace("ORDER_STATE_", "").lower())
            if o.get("state") in TERMINAL or after_cancel == 0:
                break
            if after_cancel is not None:
                after_cancel -= 1
                self.sleep(config.MAKER_AUTO_POLL_SECS)
                continue
            # Reasons to stop resting.
            why = why or rec.get("_stop") or (None if self.on else "Auto maker was turned off")
            if not why and self.clock() >= deadline:
                why = f"time's up ({config.MAKER_AUTO_TTL_SECS:g}s)"
            if not why and not self._still_listed(rec["pair"]):
                why = "the arb left the Maker mode list (prices moved)"
            if not why:
                best = self._kalshi_best(kv, ticker, sk)
                if best is not None and best > hl0 + 1e-9:
                    why = f"Kalshi moved to ${best:.2f}, past the hedge limit ${hl0:.2f}"
            if why:
                try:
                    pv.cancel(oid, slug)
                    log["orders"].append({"kind": "cancel", "why": why})
                except Exception as e:
                    log["orders"].append({"kind": "cancel", "error": repr(e)})
                after_cancel = 6         # keep reading until it's final: fills before the cancel get hedged
                continue
            self.sleep(config.MAKER_AUTO_POLL_SECS)

        # Sell back Polymarket shares that couldn't be hedged.
        sold_qty = sold_net = 0.0
        left = filled - hedged
        if left > 1e-9:
            try:
                lv = pv.levels(slug)
                other = "no" if sp == YES else "yes"
                bid = round(1 - lv[other][0][0], 6) if lv.get(other) else None
                if bid and bid > 0:
                    t = 0.01
                    s = pv.sell(slug, sp, floor_to(left, 0.01), max(t, round(bid - config.SELLBACK_SLIPPAGE_TICKS * t, 6)),
                                config.POLYMARKET_DEFAULT_COEF)
                    sold_qty, sold_net = s.qty, s.amount - s.fee
                    log["orders"].append({"kind": "sellback", "qty": s.qty, "amount": s.amount, "fee": s.fee})
            except Exception as e:
                log["orders"].append({"kind": "sellback", "error": repr(e)})

        per_p = (p_paid + p_fee) / filled if filled else 0
        locked = payout * hedged - per_p * hedged - (k_paid + k_fee)
        sell_pnl = sold_net - per_p * sold_qty
        net = round(locked + sell_pnl, 2)
        unhedged = round(max(0.0, filled - hedged - sold_qty), 4)
        status = "no_fill" if filled <= 0 else "ok" if left <= 1e-9 else "partial"
        self._write(log, status)
        today = _today()
        with self.lock:
            self.filled[today] = self.filled.get(today, 0.0) + per_p * filled + k_paid + k_fee
            self.net[today] = self.net.get(today, 0.0) + net
        if hedged > 0:
            self._record(row, hedged, per_p * hedged, k_paid + k_fee)
        if status == "partial":
            self.misses += 1
            if self.misses >= config.AUTO_TRADE_MAX_MISSES:
                self._halt(f"{self.misses} orders in a row left shares to sell back (last: {why})")
        elif status == "ok":
            self.misses = 0
        if unhedged > 0:
            self._halt(f"{unhedged:g} Polymarket shares on {row.get('game')} are unhedged and couldn't be sold back: "
                       f"check your Polymarket account")
        if self.net.get(today, 0.0) <= -config.AUTO_TRADE_MAX_DAILY_LOSS:
            self._halt(f"net loss today is ${-self.net[today]:.2f} (limit ${config.AUTO_TRADE_MAX_DAILY_LOSS:g})")
        if filled > 0:
            self.scanner.log(f"Auto maker {status}: {row.get('game')}: {filled:g} filled, {hedged:g} hedged, net ${net:+.2f}")
            self._refresh_cash()
        return {"status": status, "filled": filled, "pairs": hedged, "net": net, "unhedged": unhedged,
                "note": f"{size} rested at ${cost:.3f}; ended: {why or 'filled'}"}

    # ---- helpers ---------------------------------------------------------------------------

    def _still_listed(self, pid):
        with self.scanner.lock:
            rows = list(self.scanner.state.get("maker") or [])
        return any(pair_id(r["legs"]) == pid for r in rows)

    def _kalshi_best(self, kv, ticker, side):
        """Cheapest Kalshi price for the hedge side: from the live stream if fresh, else downloaded."""
        live = self.scanner.trader._live_book("kalshi", ticker)
        try:
            lv = (live or kv.levels(ticker))[side]
        except Exception:
            return None
        return lv[0][0] if lv else 1.0

    def _record(self, row, pairs, p_cost, k_cost):
        plan = {"payout": row["payout"],
                "legs": {"kalshi": {"exchange": "kalshi", "market_id": row["legs"][0]["market_id"],
                                    "side": row["legs"][0]["side"], "title": row["legs"][0].get("title", "")},
                         "polymarket": {"exchange": "polymarket", "market_id": row["legs"][1]["market_id"],
                                        "side": row["legs"][1]["side"], "title": row["legs"][1].get("title", "")}}}
        result = {"hedged_pairs": pairs, "legs_filled": {"kalshi": {"shares": pairs, "paid": round(k_cost, 2)},
                                                         "polymarket": {"shares": pairs, "paid": round(p_cost, 2)}}}
        try:
            self.scanner.my_arbs.add_from_trade(plan, result, {"game": row.get("game"), "tab": row.get("tab"),
                                                               "closes": row.get("closes")})
        except Exception as e:
            self.scanner.log(f"Couldn't add the maker fill to My arbs: {e!r}")

    def _refresh_cash(self):
        with self.scanner.lock:
            if self.scanner.state.get("balances"):
                self.scanner.state["balances"] = {**self.scanner.state["balances"], "stale": True}
        threading.Thread(target=self.scanner.refresh_balances, daemon=True).start()

    def _write(self, log, status):
        log["status"], log["finished"] = status, engine.now_utc().isoformat()
        try:
            with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(log, default=str) + "\n")
        except OSError:
            pass

    def _halt(self, why):
        with self.lock:
            self.on, self.halted = False, why
        self.scanner.log(f"Auto maker stopped: {why}")
        alerter = getattr(self.scanner, "alerter", None)
        if alerter and getattr(alerter, "enabled", False):
            threading.Thread(target=alerter._safe_send, args=(f"Auto maker stopped: {why}",), daemon=True).start()
