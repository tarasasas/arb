"""'Make trade': size an arb from live books, then execute both legs safely.

Sequence (per the user's chosen policy):
  1. prepare(): fresh order books + balances -> size = min of both legs' profitable depth,
     capped by MAX_TRADE_DOLLARS, the dashboard's "Max to invest", and each account's cash.
     Returns a plan for the confirm dialog; nothing is sent.
  2. execute():
     a. Re-reads both books and balances (the dialog can sit open for up to 20s) and re-sizes:
        never more pairs than you confirmed nor more than your cap, and nothing is sent at all
        if the arb is gone.
     b. The thinner leg first, immediate-or-cancel at its live limit.
     c. The other leg for exactly what filled, limited at break-even. IOC orders fill at the
        resting prices, so the high limit costs nothing when the book is as planned, and still
        hedges when it moved a little. SECOND_LEG_RETRIES more attempts on fresh books.
     d. First-leg shares still unhedged are closed whichever way loses less: sold back, or
        hedged a little above break-even (at most CLOSE_OUT_MAX_LOSS per share).
     Every order, its round-trip time and the result are appended to trades.jsonl.
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
from .model import YES, guaranteed_payout, total_fee
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


def cost_at(levels, coefs, n):
    """Cost of n pairs walking each leg's book: {exchange: {fills, amount, fee, limit}}."""
    out = {}
    for ex in EXCHANGES:
        fills = engine._take(levels[ex], n)
        out[ex] = {"fills": fills, "amount": sum(a * b for a, b in fills), "fee": total_fee(ex, fills, coefs[ex]),
                   "limit": fills[-1][0] if fills else None}
    return out


def profitable_size(levels, coefs, payout):
    """Pairs profitable on these books, ignoring money limits (0 if none)."""
    cand = {"k": SimpleNamespace(exchange="kalshi", fee_coef=coefs["kalshi"]),
            "p": SimpleNamespace(exchange="polymarket", fee_coef=coefs["polymarket"]), "payout": payout}
    full = engine.size_opportunity(cand, levels["kalshi"], levels["polymarket"])
    return full["size"] if full else 0


def fit_size(levels, coefs, payout, cap, balance, step, max_n=math.inf):
    """Most pairs (a multiple of step, at most max_n) that are profitable on these books and fit
    within cap and each account's cash."""
    def fits(n):
        c = cost_at(levels, coefs, n)
        if any(sum(q for _, q in c[ex]["fills"]) + 1e-9 < n for ex in EXCHANGES):
            return False                                      # not enough depth
        spend = {ex: c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES}
        return (sum(spend.values()) <= cap and all(spend[ex] <= balance[ex] for ex in EXCHANGES)
                and payout * n - sum(spend.values()) > 0)

    n = floor_to(min(profitable_size(levels, coefs, payout), max_n), step)
    if n > 0 and not fits(n):
        lo, hi = 0, int(n // step)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            lo, hi = (mid, hi) if fits(mid * step) else (lo, mid - 1)
        n = lo * step
    return n


class Trader:
    def __init__(self, scanner, venues):
        """venues: {"kalshi": KalshiVenue, "polymarket": PolymarketVenue} or None if not configured."""
        self.scanner, self.venues = scanner, venues
        self.plans, self.lock = {}, threading.Lock()

    def _live(self, market_ids, sides, with_info):
        """Books (the side being bought), balances and optionally market info for both legs,
        all fetched at once so the two books are read at the same moment."""
        with ThreadPoolExecutor(6) as pool:
            jobs = {}
            for ex in EXCHANGES:
                v = self.venues[ex]
                jobs[("levels", ex)] = pool.submit(v.levels, market_ids[ex])
                jobs[("balance", ex)] = pool.submit(v.balance)
                if with_info:
                    jobs[("info", ex)] = pool.submit(v.market_info, market_ids[ex])
            got = {key: job.result() for key, job in jobs.items()}
        levels = {ex: got[("levels", ex)][sides[ex]] for ex in EXCHANGES}
        balance = {ex: got[("balance", ex)] for ex in EXCHANGES}
        info = {ex: got[("info", ex)] for ex in EXCHANGES} if with_info else None
        return levels, balance, info

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

        levels, balance, info = self._live({ex: contracts[ex].market_id for ex in EXCHANGES}, sides, with_info=True)
        for ex in EXCHANGES:
            if not info[ex]["open"]:
                raise TradeError(f"The {NAMES[ex]} market isn't open for trading.")

        coefs = {ex: contracts[ex].fee_coef for ex in EXCHANGES}
        if not profitable_size(levels, coefs, payout):
            raise TradeError("Not profitable at live prices anymore; the books moved.")
        cap = min(config.MAX_TRADE_DOLLARS, max_invest or math.inf)
        step = max(info["kalshi"]["min_qty"], info["polymarket"]["min_qty"], 1.0)
        n = fit_size(levels, coefs, payout, cap, balance, step)
        if n <= 0:
            limits = f"cap ${cap:.2f}, Kalshi cash ${balance['kalshi']:.2f}, Polymarket buying power ${balance['polymarket']:.2f}"
            raise TradeError(f"Can't fit even one profitable pair within your limits ({limits}).")

        c = cost_at(levels, coefs, n)
        spare = {ex: sum(q for pr, q in levels[ex] if pr <= c[ex]["limit"] + 1e-9) - n for ex in EXCHANGES}
        first = min(EXCHANGES, key=lambda ex: spare[ex])          # the thinner book goes first
        plan = {
            "id": uuid.uuid4().hex, "created": time.time(), "size": n, "payout": payout, "first": first,
            "legs": {ex: {"exchange": ex, "market_id": contracts[ex].market_id, "side": sides[ex],
                          "title": contracts[ex].title, "limit": c[ex]["limit"], "amount": c[ex]["amount"],
                          "fee": c[ex]["fee"], "fee_coef": contracts[ex].fee_coef, "min_qty": info[ex]["min_qty"],
                          "available_at_limit": n + spare[ex], "balance": balance[ex]} for ex in EXCHANGES},
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

    def _recheck(self, plan, log, steps):
        """Re-size the confirmed plan on books read right now. Returns the second leg's cash, or
        None when nothing should be sent (the arb is gone at live prices)."""
        legs = plan["legs"]
        levels, balance, _ = self._live({ex: legs[ex]["market_id"] for ex in EXCHANGES},
                                        {ex: legs[ex]["side"] for ex in EXCHANGES}, with_info=False)
        coefs = {ex: legs[ex]["fee_coef"] for ex in EXCHANGES}
        step = max(legs["kalshi"]["min_qty"], legs["polymarket"]["min_qty"], 1.0)
        n = fit_size(levels, coefs, plan["payout"], plan["cap"], balance, step, max_n=plan["size"])
        log["recheck"] = {"size": n, "levels": {ex: levels[ex][:10] for ex in EXCHANGES}, "balance": balance}
        if n <= 0:
            return None
        c = cost_at(levels, coefs, n)
        before = plan["expected_profit"]
        for ex in EXCHANGES:
            legs[ex].update(limit=c[ex]["limit"], amount=c[ex]["amount"], fee=c[ex]["fee"], balance=balance[ex])
        plan["size"], plan["capital"] = n, sum(c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES)
        plan["expected_profit"] = plan["payout"] * n - plan["capital"]
        if abs(plan["expected_profit"] - before) >= 0.005 or n != log["plan_confirmed"]["size"]:
            steps.append(f"Live re-check: {n:g} pairs, expected +${plan['expected_profit']:.2f} "
                         f"(you confirmed {log['plan_confirmed']['size']:g} pairs, +${before:.2f})")
        return balance

    def _run(self, plan, info):
        A = plan["legs"][plan["first"]]
        B = plan["legs"]["polymarket" if plan["first"] == "kalshi" else "kalshi"]
        va, vb = self.venues[A["exchange"]], self.venues[B["exchange"]]
        log = {"plan_confirmed": json.loads(json.dumps(plan)), "plan": plan, "orders": [],
               "started": engine.now_utc().isoformat()}
        steps = []

        def record(kind, leg, fill=None, error=None, ms=None):
            log["orders"].append({"kind": kind, "exchange": leg["exchange"], "market_id": leg["market_id"],
                                  "side": leg["side"], "error": error, "qty": fill.qty if fill else 0,
                                  "amount": fill.amount if fill else 0, "fee": fill.fee if fill else 0,
                                  "ms": ms, "order_id": fill.order_id if fill else "",
                                  "request": fill.request if fill else None,
                                  "response": fill.response if fill else None})

        # 0. re-check both books: the confirm dialog may have been open for a while
        try:
            balance = self._recheck(plan, log, steps)
        except Exception as e:
            self._write(log, status="recheck_failed")
            raise TradeError(f"Couldn't re-check live prices ({e}). Nothing was traded.")
        if balance is None:
            self._write(log, status="moved")
            return self._result(plan, "moved", steps, Fill(), [], None, 0,
                                "Prices moved after you confirmed and the arb is gone at live prices. "
                                "Nothing was traded.")

        # 1. first leg; meanwhile make sure the second leg's exchange has a connection ready
        with ThreadPoolExecutor(1) as pool:
            pool.submit(getattr(vb, "warm", lambda: None))
            t0 = time.monotonic()
            try:
                fa = va.buy(A["market_id"], A["side"], plan["size"], A["limit"], A["fee_coef"])
            except ApiError as e:
                record("first", A, error=str(e), ms=_ms(t0))
                self._write(log, status="failed_first_leg")
                raise TradeError(f"{NAMES[A['exchange']]} rejected the first order, nothing was traded: {e.detail}")
            except Exception as e:          # sent, but the outcome couldn't be confirmed
                record("first", A, error=repr(e), ms=_ms(t0))
                self._write(log, status="unknown_first_leg")
                raise TradeError(f"Couldn't confirm the {NAMES[A['exchange']]} order ({e}). Check that account before "
                                 f"doing anything else; the second leg was NOT placed.")
            record("first", A, fa, ms=_ms(t0))
        steps.append(f"{NAMES[A['exchange']]}: bought {fa.qty:g} {A['side'].upper()} for ${fa.amount:.2f} + ${fa.fee:.2f} fee")
        if fa.qty <= 0:
            self._write(log, status="no_fill")
            return self._result(plan, "no_fill", steps, fa, [], None, 0, "The first leg didn't fill (the price moved). Nothing was traded.")

        # 2. second leg, sized to what actually filled, limited at break-even
        target = floor_to(fa.qty, B["min_qty"])
        a_cost_per = (fa.amount + fa.fee) / fa.qty
        tick_b = info[B["exchange"]]["tick"]
        cash_b = balance[B["exchange"]]
        fills_b, hedged, note = [], 0.0, ""
        for attempt in range(1 + config.SECOND_LEG_RETRIES):
            remaining = floor_to(target - hedged, B["min_qty"])
            if remaining <= 0:
                break
            # Each share hedged must break even on its own (payout - first-leg cost per share), and
            # the order must fit the account's cash.
            spent_b = sum(f.amount + f.fee for f in fills_b)
            room = min((plan["payout"] - a_cost_per) * remaining, cash_b - spent_b)
            limit = break_even_price(B["exchange"], remaining, room, B["fee_coef"], tick_b)
            if limit <= 0:
                note = "No second-leg price can break even anymore."
                break
            t0 = time.monotonic()
            try:
                fb = vb.buy(B["market_id"], B["side"], remaining, limit, B["fee_coef"])
            except ApiError as e:
                record("second", B, error=str(e), ms=_ms(t0))
                steps.append(f"{NAMES[B['exchange']]}: order rejected ({e.detail})")
                fb = Fill()
            except Exception as e:      # network error: the order may or may not exist, so stop here
                record("second", B, error=repr(e), ms=_ms(t0))
                self._write(log, status="unknown_second_leg")
                return self._result(plan, "unknown", steps, fa, fills_b, None, hedged,
                                    f"Couldn't confirm the {NAMES[B['exchange']]} order ({e!r}). Check both accounts "
                                    f"before doing anything else. Nothing was sold back.")
            else:
                record("second", B, fb, ms=_ms(t0))
                steps.append(f"{NAMES[B['exchange']]}: bought {fb.qty:g} {B['side'].upper()} at ≤ ${limit:.3f} "
                             f"for ${fb.amount:.2f} + ${fb.fee:.2f} fee" + (f" (retry {attempt})" if attempt else ""))
            fills_b.append(fb)
            hedged += fb.qty
            if hedged + 1e-9 < target and attempt < config.SECOND_LEG_RETRIES:
                time.sleep(config.SECOND_LEG_RETRY_PAUSE)        # let the book refill

        # 3. close whatever the second leg couldn't cover, the cheaper way
        sold = None
        if fa.qty - hedged > 1e-9:
            extra, sold = self._close_out(plan, info, A, B, va, vb, fa.qty - hedged, a_cost_per,
                                          cash_b - sum(f.amount + f.fee for f in fills_b), record, steps)
            fills_b += extra
            hedged += sum(f.qty for f in extra)
        status = "ok" if fa.qty - hedged <= 1e-9 else "partial"
        self._write(log, status=status)
        return self._result(plan, status, steps, fa, fills_b, sold, hedged, note)

    def _close_out(self, plan, info, A, B, va, vb, excess, a_cost_per, cash_b, record, steps):
        """First-leg shares the second leg couldn't hedge at break-even. Close them whichever way
        gets more back per share: sell them back into the first exchange's bids, or hedge them on
        the second exchange slightly above break-even (never more than CLOSE_OUT_MAX_LOSS per
        share). Returns (extra second-leg fills, sell-back fill or None)."""
        try:
            with ThreadPoolExecutor(2) as pool:
                ja, jb = pool.submit(va.levels, A["market_id"]), pool.submit(vb.levels, B["market_id"])
                lv_a, lv_b = ja.result(), jb.result()
        except Exception as e:
            steps.append(f"Couldn't read the books to close {excess:g} unhedged shares ({e})")
            return [], None
        other = "no" if A["side"] == YES else "yes"
        bids_a = [(round(1 - p, 6), q) for p, q in lv_a[other]]        # what one of our shares sells for
        extra, sold = [], None

        # Hedge candidate: B's asks up to the loss limit, valued at payout - cost per share.
        worst_b = (plan["payout"] - a_cost_per) + config.CLOSE_OUT_MAX_LOSS
        asks_b = [(p, q) for p, q in lv_b[B["side"]] if p <= worst_b + 1e-9]
        hedge_fills = engine._take(asks_b, floor_to(excess, B["min_qty"]))
        hedge_q = sum(q for _, q in hedge_fills)
        sell_fills = engine._take(bids_a, floor_to(excess, A["min_qty"]))
        sell_q = sum(q for _, q in sell_fills)
        hedge_per = ((plan["payout"] * hedge_q - sum(p * q for p, q in hedge_fills)
                      - total_fee(B["exchange"], hedge_fills, B["fee_coef"])) / hedge_q) if hedge_q else -math.inf
        sell_per = ((sum(p * q for p, q in sell_fills) - total_fee(A["exchange"], sell_fills, A["fee_coef"])) / sell_q
                    if sell_q else -math.inf)
        hedge_cost = sum(p * q for p, q in hedge_fills) + total_fee(B["exchange"], hedge_fills, B["fee_coef"])
        if hedge_q and hedge_per > sell_per and hedge_cost <= cash_b:
            t0 = time.monotonic()
            try:
                fb = vb.buy(B["market_id"], B["side"], hedge_q, hedge_fills[-1][0], B["fee_coef"])
                record("close_hedge", B, fb, ms=_ms(t0))
                extra.append(fb)
                if fb.qty:
                    loss = (a_cost_per * fb.qty + fb.amount + fb.fee) - plan["payout"] * fb.qty
                    steps.append(f"{NAMES[B['exchange']]}: hedged {fb.qty:g} more {B['side'].upper()} above break-even "
                                 f"(${fb.amount:.2f} + ${fb.fee:.2f} fee, {'-' if loss > 0 else '+'}${abs(loss):.2f}), "
                                 f"cheaper than selling back")
            except Exception as e:
                record("close_hedge", B, error=repr(e), ms=_ms(t0))
                steps.append(f"{NAMES[B['exchange']]}: closing hedge failed ({e})")
            excess -= sum(f.qty for f in extra)

        sell_qty = floor_to(excess, A["min_qty"])
        if sell_qty > 0:
            if not bids_a:
                steps.append(f"{NAMES[A['exchange']]}: no buyers to sell the unhedged shares back to")
                return extra, None
            bid = bids_a[0][0]
            t = info[A["exchange"]]["tick"](bid)
            min_price = max(t, round(bid - config.SELLBACK_SLIPPAGE_TICKS * t, 6))
            t0 = time.monotonic()
            try:
                sold = va.sell(A["market_id"], A["side"], sell_qty, min_price, A["fee_coef"])
                record("sellback", A, sold, ms=_ms(t0))
                steps.append(f"{NAMES[A['exchange']]}: sold back {sold.qty:g} unhedged {A['side'].upper()} "
                             f"for ${sold.amount:.2f} − ${sold.fee:.2f} fee")
            except Exception as e:
                record("sellback", A, error=repr(e), ms=_ms(t0))
                steps.append(f"{NAMES[A['exchange']]}: sell-back failed ({e})")
        return extra, sold

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


def _ms(t0):
    return round((time.monotonic() - t0) * 1000)
