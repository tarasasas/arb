"""'Make trade': size an arb from live books, then execute both legs safely.

Sequence (per the user's chosen policy):
  1. prepare(): fresh order books + balances -> size = min of both legs' profitable depth,
     capped by MAX_TRADE_DOLLARS, the dashboard's "Max to invest", and each account's cash.
     Returns a plan for the confirm dialog; nothing is sent.
  2. execute(): the thinner leg first as immediate-or-cancel at its planned limit. The other
     leg is then bought for exactly what filled, never above the break-even price, with
     SECOND_LEG_RETRIES more attempts on fresh prices. Any first-leg shares still unhedged
     are sold back immediately. Every order and result is appended to trades.jsonl.
"""

import json
import math
import threading
import time
import uuid
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

        info, levels, balance = {}, {}, {}
        for ex in EXCHANGES:
            v, mid = self.venues[ex], contracts[ex].market_id
            info[ex] = v.market_info(mid)
            if not info[ex]["open"]:
                raise TradeError(f"The {NAMES[ex]} market isn't open for trading.")
            levels[ex] = v.levels(mid)[sides[ex]]
            balance[ex] = v.balance()

        k, p = contracts["kalshi"], contracts["polymarket"]
        cand = {"k": SimpleNamespace(exchange="kalshi", fee_coef=k.fee_coef),
                "p": SimpleNamespace(exchange="polymarket", fee_coef=p.fee_coef), "payout": payout}
        full = engine.size_opportunity(cand, levels["kalshi"], levels["polymarket"])
        if not full:
            raise TradeError("Not profitable at live prices anymore; the books moved.")

        cap = min(config.MAX_TRADE_DOLLARS, max_invest or math.inf)
        step = max(info["kalshi"]["min_qty"], info["polymarket"]["min_qty"], 1.0)

        def cost_at(n):
            out = {}
            for ex in EXCHANGES:
                fills = engine._take(levels[ex], n)
                amt = sum(a * b for a, b in fills)
                out[ex] = {"fills": fills, "amount": amt, "fee": total_fee(ex, fills, contracts[ex].fee_coef),
                           "limit": fills[-1][0] if fills else None}
            return out

        def fits(n):
            c = cost_at(n)
            spend = {ex: c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES}
            return (sum(spend.values()) <= cap and all(spend[ex] <= balance[ex] for ex in EXCHANGES)
                    and payout * n - sum(spend.values()) > 0)

        n = floor_to(full["size"], step)
        if not fits(n):
            lo, hi = 0, int(n // step)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if fits(mid * step) else (lo, mid - 1)
            n = lo * step
        if n <= 0:
            limits = f"cap ${cap:.2f}, Kalshi cash ${balance['kalshi']:.2f}, Polymarket buying power ${balance['polymarket']:.2f}"
            raise TradeError(f"Can't fit even one profitable pair within your limits ({limits}).")

        c = cost_at(n)
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

    def _run(self, plan, info):
        A = plan["legs"][plan["first"]]
        B = plan["legs"]["polymarket" if plan["first"] == "kalshi" else "kalshi"]
        va, vb = self.venues[A["exchange"]], self.venues[B["exchange"]]
        log = {"plan": plan, "orders": [], "started": engine.now_utc().isoformat()}
        steps = []

        def record(kind, leg, fill=None, error=None):
            log["orders"].append({"kind": kind, "exchange": leg["exchange"], "market_id": leg["market_id"],
                                  "side": leg["side"], "error": error, "qty": fill.qty if fill else 0,
                                  "amount": fill.amount if fill else 0, "fee": fill.fee if fill else 0,
                                  "order_id": fill.order_id if fill else "", "request": fill.request if fill else None,
                                  "response": fill.response if fill else None})

        # 1. first leg
        try:
            fa = va.buy(A["market_id"], A["side"], plan["size"], A["limit"], A["fee_coef"])
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
            return self._result(plan, "no_fill", steps, fa, [], None, 0, "The first leg didn't fill (the price moved). Nothing was traded.")

        # 2. second leg, sized to what actually filled, never above break-even
        target = floor_to(fa.qty, B["min_qty"])
        a_cost_per = (fa.amount + fa.fee) / fa.qty
        tick_b = info[B["exchange"]]["tick"]
        fills_b, hedged, status, note = [], 0.0, "ok", ""
        for attempt in range(1 + config.SECOND_LEG_RETRIES):
            remaining = floor_to(target - hedged, B["min_qty"])
            if remaining <= 0:
                break
            # Each share hedged now must break even on its own: payout - first-leg cost per share.
            room = (plan["payout"] - a_cost_per) * remaining
            pmax = break_even_price(B["exchange"], remaining, room, B["fee_coef"], tick_b)
            limit = min(B["limit"], pmax) if attempt == 0 else pmax
            if limit <= 0:
                note = "No second-leg price can break even anymore."
                break
            try:
                fb = vb.buy(B["market_id"], B["side"], remaining, limit, B["fee_coef"])
            except ApiError as e:
                record("second", B, error=str(e))
                steps.append(f"{NAMES[B['exchange']]}: order rejected ({e.detail})")
                fb = Fill()
            except Exception as e:      # network error: the order may or may not exist, so stop here
                record("second", B, error=repr(e))
                self._write(log, status="unknown_second_leg")
                return self._result(plan, "unknown", steps, fa, fills_b, None, hedged,
                                    f"Couldn't confirm the {NAMES[B['exchange']]} order ({e!r}). Check both accounts "
                                    f"before doing anything else. Nothing was sold back.")
            else:
                record("second", B, fb)
                steps.append(f"{NAMES[B['exchange']]}: bought {fb.qty:g} {B['side'].upper()} at ≤ ${limit:.3f} "
                             f"for ${fb.amount:.2f} + ${fb.fee:.2f} fee" + (f" (retry {attempt})" if attempt else ""))
            fills_b.append(fb)
            hedged += fb.qty
            if hedged + 1e-9 < target and attempt < config.SECOND_LEG_RETRIES:
                time.sleep(0.4)
                try:        # refresh the planned limit from the live book for the retry
                    lv = vb.levels(B["market_id"])[B["side"]]
                    B["limit"] = lv[0][0] if lv else B["limit"]
                except Exception:
                    pass

        # 3. sell back whatever the second leg couldn't cover
        excess = fa.qty - hedged
        sold = None
        if excess > 1e-9:
            status = "partial"
            sell_qty = floor_to(excess, A["min_qty"])
            if sell_qty > 0:
                try:
                    lv = va.levels(A["market_id"])
                    other = "no" if A["side"] == YES else "yes"
                    bid = round(1 - lv[other][0][0], 6) if lv[other] else None   # best price we can sell our side at
                    if bid and bid > 0:
                        t = info[A["exchange"]]["tick"](bid)
                        min_price = max(t, round(bid - config.SELLBACK_SLIPPAGE_TICKS * t, 6))
                        sold = va.sell(A["market_id"], A["side"], sell_qty, min_price, A["fee_coef"])
                        record("sellback", A, sold)
                        steps.append(f"{NAMES[A['exchange']]}: sold back {sold.qty:g} unhedged {A['side'].upper()} "
                                     f"for ${sold.amount:.2f} − ${sold.fee:.2f} fee")
                    else:
                        steps.append(f"{NAMES[A['exchange']]}: no buyers to sell the unhedged shares back to")
                except Exception as e:
                    record("sellback", A, error=repr(e))
                    steps.append(f"{NAMES[A['exchange']]}: sell-back failed ({e})")
        self._write(log, status=status)
        return self._result(plan, status, steps, fa, fills_b, sold, hedged, note)

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
