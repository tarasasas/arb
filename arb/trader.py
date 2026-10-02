"""'Make trade': size an arb from live books, then execute both legs safely.

Sequence (per the user's chosen policy):
  1. prepare(): fresh order books + balances (fetched in parallel) -> size = min of both legs'
     profitable depth, capped by MAX_TRADE_DOLLARS, the dashboard's "Max to invest", and each
     account's cash. Returns a plan for the confirm dialog; nothing is sent.
  2. execute(): both books are re-read the moment you confirm, and the trade shrinks to what they
     still hold at the confirmed limits (or is cancelled with nothing sent), so the first leg never
     buys shares the second leg can no longer cover. The thinner leg then goes first as
     immediate-or-cancel. The other leg is sent at once for exactly what filled, at up to its hedge
     ceiling: break-even, or up to HEDGE_MAX_LOSS_PER_SHARE past it when that is still cheaper than
     selling the first leg back. If it falls short it retries, but only when the live book shows
     shares under the ceiling, for up to HEDGE_WINDOW_SECS. Any first-leg shares still unhedged are
     sold back. Every order and result is appended to trades.jsonl.

Every venue request made here skips the scanner's rate-limit queue, so a background sweep never
delays an order.
"""

import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from . import config, engine
from .http import ApiError
from .model import YES, fee_per_contract, guaranteed_payout, total_fee
from .venues import Fill, floor_to

EXCHANGES = ("kalshi", "polymarket")
NAMES = {"kalshi": "Kalshi", "polymarket": "Polymarket"}


class TradeError(Exception):
    pass


def _fee(exchange, qty, price, coef):
    return total_fee(exchange, [(price, qty)], coef)


def break_even_price(exchange, qty, room, coef, tick):
    """Highest price p (on the tick grid) with qty*p + fee <= room."""
    if qty <= 0 or room <= 0:
        return 0.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if qty * mid + _fee(exchange, qty, mid, coef) <= room else (lo, mid)
    return round(floor_to(lo, tick(lo)), 6)


def _other(exchange):
    return "polymarket" if exchange == "kalshi" else "kalshi"


def _bids(levels, side):
    """Prices `side` can be sold at right now, best first: selling YES hits the bids that buying NO
    would lift, at 1 - their price (and vice versa)."""
    return [(round(1 - p, 6), q) for p, q in levels["no" if side == YES else "yes"]]


def _unwind_value(leg, levels):
    """Net per-share proceeds of selling leg's side back at the best bid now (0 if nobody bids)."""
    bids = _bids(levels, leg["side"])
    return bids[0][0] - fee_per_contract(leg["fee_coef"], bids[0][0]) if bids else 0.0


def _parallel(calls):
    """Run {key: zero-arg callable} at once and return {key: result, or the exception it raised}."""
    with ThreadPoolExecutor(len(calls)) as pool:
        futures = {k: pool.submit(fn) for k, fn in calls.items()}
        return {k: f.exception() or f.result() for k, f in futures.items()}


def _raise_first(results):
    for r in results.values():
        if isinstance(r, Exception):
            raise r


def size_trade(levels, coefs, payout, cap, balance, step, max_n=math.inf):
    """Largest size (a multiple of step, at most max_n) that both books cover profitably and that fits
    the cap and each account's cash. Returns (n, costs per exchange); n is None when no size is
    profitable at all, and 0 when only the cap or cash gets in the way."""
    cand = {"k": SimpleNamespace(exchange="kalshi", fee_coef=coefs["kalshi"]),
            "p": SimpleNamespace(exchange="polymarket", fee_coef=coefs["polymarket"]), "payout": payout}
    full = engine.size_opportunity(cand, levels["kalshi"], levels["polymarket"])
    if not full:
        return None, None

    def cost_at(n):
        out = {}
        for ex in EXCHANGES:
            fills = engine._take(levels[ex], n)
            amt = sum(a * b for a, b in fills)
            out[ex] = {"fills": fills, "amount": amt, "fee": total_fee(ex, fills, coefs[ex]),
                       "limit": fills[-1][0] if fills else None}
        return out

    def fits(n):
        c = cost_at(n)
        spend = {ex: c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES}
        return (sum(spend.values()) <= cap and all(spend[ex] <= balance[ex] for ex in EXCHANGES)
                and payout * n - sum(spend.values()) > 0)

    n = floor_to(min(full["size"], max_n), step)
    if not fits(n):
        lo, hi = 0, int(n // step)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(mid * step) else (lo, mid - 1)
        n = lo * step
    return n, (cost_at(n) if n > 0 else None)


def _spare(levels, costs, n):
    """Shares each book holds at or under its limit beyond the n we need; the thinner one goes first."""
    return {ex: sum(q for pr, q in levels[ex] if pr <= costs[ex]["limit"] + 1e-9) - n for ex in EXCHANGES}


class Trader:
    def __init__(self, scanner, venues):
        """venues: {"kalshi": KalshiVenue, "polymarket": PolymarketVenue} or None if not configured."""
        self.scanner, self.venues = scanner, venues
        self.plans, self.lock = {}, threading.Lock()

    # ---- planning ----------------------------------------------------------------------

    def prepare(self, legs, max_invest=None):
        if not self.venues:
            raise TradeError("Trading needs both API keys. Add POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY to .env.")
        by_ex = {l["exchange"]: l for l in legs}
        if set(by_ex) != set(EXCHANGES):
            raise TradeError("A trade needs one Kalshi leg and one Polymarket leg.")
        contracts = {ex: self.scanner.find_contract(ex, by_ex[ex]["market_id"]) for ex in EXCHANGES}
        if not all(contracts.values()):
            raise TradeError("These markets are no longer in the scanner's list (closed or delisted).")
        sides = {ex: by_ex[ex]["side"] for ex in EXCHANGES}
        payout = guaranteed_payout([(contracts[ex], sides[ex]) for ex in EXCHANGES])
        if payout <= 0:
            raise TradeError("This pair doesn't guarantee a payout.")

        calls = {}
        for ex in EXCHANGES:
            v, mid = self.venues[ex], contracts[ex].market_id
            calls[ex, "info"] = lambda v=v, mid=mid: v.market_info(mid)
            calls[ex, "levels"] = lambda v=v, mid=mid: v.levels(mid)
            calls[ex, "balance"] = v.balance
        got = _parallel(calls)
        for ex in EXCHANGES:                # a closed market explains any other failure, so report it first
            if not isinstance(got[ex, "info"], Exception) and not got[ex, "info"]["open"]:
                raise TradeError(f"The {NAMES[ex]} market isn't open for trading.")
        _raise_first(got)
        info = {ex: got[ex, "info"] for ex in EXCHANGES}
        levels = {ex: got[ex, "levels"][sides[ex]] for ex in EXCHANGES}
        balance = {ex: got[ex, "balance"] for ex in EXCHANGES}

        cap = min(config.MAX_TRADE_DOLLARS, max_invest or math.inf)
        step = max(info["kalshi"]["min_qty"], info["polymarket"]["min_qty"], 1.0)
        n, c = size_trade(levels, {ex: contracts[ex].fee_coef for ex in EXCHANGES}, payout, cap, balance, step)
        if n is None:
            raise TradeError("Not profitable at live prices anymore; the books moved.")
        if n <= 0:
            limits = f"cap ${cap:.2f}, Kalshi cash ${balance['kalshi']:.2f}, Polymarket buying power ${balance['polymarket']:.2f}"
            raise TradeError(f"Can't fit even one profitable pair within your limits ({limits}).")

        spare = _spare(levels, c, n)
        first = min(EXCHANGES, key=lambda ex: spare[ex])          # the thinner book goes first
        plan = {
            "id": uuid.uuid4().hex, "created": time.time(), "size": n, "payout": payout, "first": first,
            "legs": {ex: {"exchange": ex, "market_id": contracts[ex].market_id, "side": sides[ex],
                          "title": contracts[ex].title, "limit": c[ex]["limit"], "amount": c[ex]["amount"],
                          "fee": c[ex]["fee"], "fee_coef": contracts[ex].fee_coef, "min_qty": info[ex]["min_qty"],
                          "available_at_limit": n + spare[ex], "balance": balance[ex]} for ex in EXCHANGES},
            "hedge": {"max_loss_per_share": config.HEDGE_MAX_LOSS_PER_SHARE, "window_secs": config.HEDGE_WINDOW_SECS},
        }
        plan["capital"] = sum(c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES)
        plan["expected_profit"] = payout * n - plan["capital"]
        plan["cap"] = cap
        self.plans[plan["id"]] = (plan, info)
        return plan

    # ---- execution ---------------------------------------------------------------------

    def execute(self, plan_id):
        with self.lock:
            entry = self.plans.pop(plan_id, None)
            if not entry:
                raise TradeError("That plan was already used or doesn't exist. Press Make trade again.")
            plan, info = entry
            if time.time() - plan["created"] > config.TRADE_PLAN_TTL_SECS:
                raise TradeError("That plan expired (prices move fast). Press Make trade again for fresh numbers.")
            return self._run(plan, info)

    def _preflight(self, plan, info, log):
        """Re-read both books right before the first order. The plan is as old as the confirm dialog,
        and a first leg that fills against a second-leg book that has since thinned is what forces a
        sell-back. Returns (run, fresh levels) where run is the plan resized to what both books still
        hold at the confirmed limits (size 0: nothing to trade)."""
        legs = plan["legs"]
        try:
            fresh = _parallel({ex: (lambda ex=ex: self.venues[ex].levels(legs[ex]["market_id"])) for ex in EXCHANGES})
            _raise_first(fresh)
        except Exception as e:
            log["preflight"] = {"error": repr(e)}
            self._write(log, status="failed_preflight")
            raise TradeError(f"Couldn't re-check the order books ({e}); nothing was traded. Press Make trade again.")
        capped = {ex: [lv for lv in fresh[ex][legs[ex]["side"]] if lv[0] <= legs[ex]["limit"] + 1e-9]
                  for ex in EXCHANGES}
        step = max(info["kalshi"]["min_qty"], info["polymarket"]["min_qty"], 1.0)
        n, c = size_trade(capped, {ex: legs[ex]["fee_coef"] for ex in EXCHANGES}, plan["payout"], plan["cap"],
                          {ex: legs[ex]["balance"] for ex in EXCHANGES}, step, plan["size"])
        log["preflight"] = {"size": n or 0, "books": {ex: fresh[ex][legs[ex]["side"]][:10] for ex in EXCHANGES}}
        if not n:
            return {**plan, "size": 0}, fresh
        spare = _spare(capped, c, n)
        first = min(EXCHANGES, key=lambda ex: spare[ex])
        log["preflight"].update(first=first, limits={ex: c[ex]["limit"] for ex in EXCHANGES})
        run = {**plan, "size": n, "first": first,
               "legs": {ex: {**legs[ex], "limit": c[ex]["limit"]} for ex in EXCHANGES}}
        return run, fresh

    def _wait_for_liquidity(self, venue, leg, ceiling, deadline):
        """Re-read leg's book until it offers shares at or under ceiling. Returns those levels, or None
        once the hedge window closes. An IOC sent with nothing to hit can't fill; it just burns time."""
        for _ in range(max(1, round(config.HEDGE_WINDOW_SECS / config.HEDGE_POLL_SECS))):
            try:
                lv = venue.levels(leg["market_id"])[leg["side"]]
            except Exception:
                lv = []
            if lv and lv[0][0] <= ceiling + 1e-9:
                return lv
            if time.monotonic() + config.HEDGE_POLL_SECS > deadline:
                return None
            time.sleep(config.HEDGE_POLL_SECS)
        return None

    def _run(self, plan, info):
        log = {"plan": plan, "orders": [], "started": engine.now_utc().isoformat()}
        steps = []

        def record(kind, leg, fill=None, error=None):
            log["orders"].append({"kind": kind, "exchange": leg["exchange"], "market_id": leg["market_id"],
                                  "side": leg["side"], "error": error, "qty": fill.qty if fill else 0,
                                  "amount": fill.amount if fill else 0, "fee": fill.fee if fill else 0,
                                  "order_id": fill.order_id if fill else "", "request": fill.request if fill else None,
                                  "response": fill.response if fill else None})

        # 0. pre-flight: resize to the books as they are now
        run, fresh = self._preflight(plan, info, log)
        if run["size"] <= 0:
            steps.append("Re-checked both order books when you confirmed: the prices had moved past your limits.")
            self._write(log, status="moved")
            return self._result(plan, "no_fill", steps, Fill(), [], None, 0,
                                "Prices moved after you confirmed, so no order was sent. Press Make trade again "
                                "for fresh numbers.")
        if run["size"] < plan["size"]:
            steps.append(f"Re-checked both order books: trimmed to {run['size']:g} pairs (from {plan['size']:g}), "
                         f"what both still hold at your limits.")
        A, B = run["legs"][run["first"]], run["legs"][_other(run["first"])]
        va, vb = self.venues[A["exchange"]], self.venues[B["exchange"]]
        book_a = [lv for lv in fresh[A["exchange"]][A["side"]] if lv[0] <= A["limit"] + 1e-9]

        # 1. first leg
        try:
            fa = va.buy(A["market_id"], A["side"], run["size"], A["limit"], A["fee_coef"], book=book_a)
        except ApiError as e:
            record("first", A, error=str(e))
            self._write(log, status="failed_first_leg")
            raise TradeError(f"{NAMES[A['exchange']]} rejected the first order, nothing was traded: {e.detail}")
        except Exception as e:          # sent, but the outcome couldn't be confirmed
            record("first", A, error=repr(e))
            self._write(log, status="unknown_first_leg")
            raise TradeError(f"Couldn't confirm the {NAMES[A['exchange']]} order ({e}). Check that account before "
                             f"doing anything else; the second leg was NOT placed.")
        record("first", A, fa)
        steps.append(f"{NAMES[A['exchange']]}: bought {fa.qty:g} {A['side'].upper()} for ${fa.amount:.2f} + ${fa.fee:.2f} fee")
        if fa.qty <= 0:
            self._write(log, status="no_fill")
            return self._result(run, "no_fill", steps, fa, [], None, 0, "The first leg didn't fill (the price moved). Nothing was traded.")

        # 2. second leg, sized to what actually filled, sent at once at the hedge ceiling. An IOC fills
        #    at the best prices on the book, so a higher limit only costs more if the book moved, and
        #    then hedging still loses less than selling back.
        target = floor_to(fa.qty, B["min_qty"])
        a_cost_per = (fa.amount + fa.fee) / fa.qty
        # Past break-even only by less than selling back would lose: spread plus a second round of fees.
        allowance = min(config.HEDGE_MAX_LOSS_PER_SHARE, max(0.0, a_cost_per - _unwind_value(A, fresh[A["exchange"]])))
        room_per = plan["payout"] - a_cost_per + allowance
        tick_b = info[B["exchange"]]["tick"]
        book_b = fresh[B["exchange"]][B["side"]]
        deadline = time.monotonic() + config.HEDGE_WINDOW_SECS
        fills_b, hedged, status, note = [], 0.0, "ok", ""
        for attempt in range(1 + config.SECOND_LEG_RETRIES):
            remaining = floor_to(target - hedged, B["min_qty"])
            if remaining <= 0:
                break
            limit = break_even_price(B["exchange"], remaining, room_per * remaining, B["fee_coef"], tick_b)
            if limit <= 0:
                note = "No second-leg price can break even anymore."
                break
            if attempt:
                book_b = self._wait_for_liquidity(vb, B, limit, deadline)
                if book_b is None:
                    note = (f"{NAMES[B['exchange']]} showed no shares at or under ${limit:.3f} within "
                            f"{config.HEDGE_WINDOW_SECS:g}s.")
                    break
            try:
                fb = vb.buy(B["market_id"], B["side"], remaining, limit, B["fee_coef"], book=book_b)
            except ApiError as e:
                record("second", B, error=str(e))
                steps.append(f"{NAMES[B['exchange']]}: order rejected ({e.detail})")
                if 400 <= e.status < 500:   # the same order would be refused again; unwind now
                    break
                continue
            except Exception as e:      # network error: the order may or may not exist, so stop here
                record("second", B, error=repr(e))
                self._write(log, status="unknown_second_leg")
                return self._result(run, "unknown", steps, fa, fills_b, None, hedged,
                                    f"Couldn't confirm the {NAMES[B['exchange']]} order ({e!r}). Check both accounts "
                                    f"before doing anything else. Nothing was sold back.")
            record("second", B, fb)
            steps.append(f"{NAMES[B['exchange']]}: bought {fb.qty:g} {B['side'].upper()} at ≤ ${limit:.3f} "
                         f"for ${fb.amount:.2f} + ${fb.fee:.2f} fee" + (f" (retry {attempt})" if attempt else ""))
            fills_b.append(fb)
            hedged += fb.qty

        # 3. sell back whatever the second leg couldn't cover
        excess = fa.qty - hedged
        sold = None
        if excess > 1e-9:
            status = "partial"
            sell_qty = floor_to(excess, A["min_qty"])
            if sell_qty > 0:
                try:
                    bids = _bids(va.levels(A["market_id"]), A["side"])
                    if bids and bids[0][0] > 0:
                        bid = bids[0][0]
                        t = info[A["exchange"]]["tick"](bid)
                        min_price = max(t, round(bid - config.SELLBACK_SLIPPAGE_TICKS * t, 6))
                        sold = va.sell(A["market_id"], A["side"], sell_qty, min_price, A["fee_coef"], book=bids)
                        record("sellback", A, sold)
                        steps.append(f"{NAMES[A['exchange']]}: sold back {sold.qty:g} unhedged {A['side'].upper()} "
                                     f"for ${sold.amount:.2f} − ${sold.fee:.2f} fee")
                    else:
                        steps.append(f"{NAMES[A['exchange']]}: no buyers to sell the unhedged shares back to")
                except Exception as e:
                    record("sellback", A, error=repr(e))
                    steps.append(f"{NAMES[A['exchange']]}: sell-back failed ({e})")
        self._write(log, status=status)
        return self._result(run, status, steps, fa, fills_b, sold, hedged, note)

    def _result(self, plan, status, steps, fa, fills_b, sold, hedged, note):
        A = plan["legs"][plan["first"]]
        a_per = (fa.amount + fa.fee) / fa.qty if fa.qty else 0
        b_spent = sum(f.amount + f.fee for f in fills_b)
        locked = plan["payout"] * hedged - a_per * hedged - b_spent
        sold_qty = sold.qty if sold else 0
        sellback_pnl = (sold.amount - sold.fee - a_per * sold_qty) if sold else 0
        unhedged = max(0.0, fa.qty - hedged - sold_qty)
        return {"status": status, "steps": steps, "note": note, "hedged_pairs": hedged,
                "locked_profit": round(locked, 2), "sellback_pnl": round(sellback_pnl, 2),
                "net": round(locked + sellback_pnl, 2), "unhedged_shares": round(unhedged, 4),
                "unhedged_exchange": NAMES[A["exchange"]] if unhedged > 1e-9 else None,
                "unhedged_side": A["side"] if unhedged > 1e-9 else None, "payout": plan["payout"]}

    def _write(self, log, status):
        log["status"], log["finished"] = status, engine.now_utc().isoformat()
        with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log, default=str) + "\n")
