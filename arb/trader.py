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
from .http import ApiError, LanePool, priority
from .model import YES, guaranteed_payout, total_fee
from .venues import Fill, floor_to

EXCHANGES = ("kalshi", "polymarket")
_TL_LOCK = threading.Lock()


def timeline_stages(tl):
    """Milliseconds per stage of one trade's timeline (stages it didn't have are left out)."""
    def ms(a, b):
        return round((tl[b] - tl[a]) * 1000) if tl.get(a) and tl.get(b) else None
    out = {"tick to detected": ms("tick", "detected"), "detected to decided": ms("detected", "decided"),
           "checks": ms("decided", "checks_done")}
    for o in tl.get("orders") or []:
        name = f"{NAMES[o['exchange']]} order"
        if name not in out:
            out[name] = round((o["acked"] - o["sent"]) * 1000)
    out["total (tick to done)"] = ms("tick", "done") if tl.get("tick") else ms("decided", "done")
    return {k: v for k, v in out.items() if v is not None}
SHARD_NAMES = {0: "main", 1: "combos", 2: "crypto and commodities", 3: "tennis, baseball and basketball"}
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
        self._info_cache = {}            # (exchange, market id) -> (time, market info)

    # ---- tick-to-trade timeline -------------------------------------------------------------

    def _timed_buy(self, plan, kind, leg, qty, limit):
        """venue.buy, recording when the order went out and when its final answer came back."""
        sent = time.time()
        try:
            return self.venues[leg["exchange"]].buy(leg["market_id"], leg["side"], qty, limit, leg["fee_coef"])
        finally:
            with _TL_LOCK:
                plan.setdefault("timeline", {}).setdefault("orders", []).append(
                    {"exchange": leg["exchange"], "kind": kind, "sent": sent, "acked": time.time()})

    # ---- fast paths for the pre-trade checks -------------------------------------------------

    def _market_info(self, ex, venue, mid):
        hit = self._info_cache.get((ex, mid))
        if hit and time.time() - hit[0] < config.MARKET_INFO_TTL:
            return hit[1], "cached"
        info = venue.market_info(mid)
        self._info_cache[(ex, mid)] = (time.time(), info)
        return info, "download"

    def _live_book(self, ex, mid):
        """The stream's book for this market if it updated within LIVE_BOOK_MAX_AGE, else None."""
        stream = (getattr(self.scanner, "streams", None) or {}).get(ex)
        source = getattr(self.scanner, "source", None) or {}
        m = source.get((ex, mid))
        if not stream or m is None or not getattr(stream, "connected", False):
            return None
        t = getattr(stream, "updated_at", {}).get(mid)
        if not t or time.time() - t > config.LIVE_BOOK_MAX_AGE or not getattr(m, "levels", None):
            return None
        return {"yes": list(m.levels.get("yes") or []), "no": list(m.levels.get("no") or [])}

    def _cached_cash(self, ex, shard=None):
        """Cash from the scanner's balance reading if it's fresh (and not marked stale by a trade)."""
        state = getattr(self.scanner, "state", None) or {}
        b = state.get("balances") or {}
        if not b.get("time") or b.get("stale") or b.get("error"):
            return None
        try:
            age = (engine.now_utc() - engine._parse_time(b["time"])).total_seconds()
        except Exception:
            return None
        if age > config.CASH_MAX_AGE:
            return None
        if ex == "kalshi":
            shards = b.get("kalshi_shards")
            if shards is not None:
                return shards.get(str(shard or 0))
            return b.get("kalshi") if not shard else None
        return b.get("polymarket")

    # ---- planning ----------------------------------------------------------------------

    def prepare(self, legs, max_invest=None, timeline=None):
        tl = dict(timeline or {})
        tl.setdefault("decided", time.time())      # Make trade: the click is the decision
        with priority():                   # trades go ahead of background market loads
            plan = self._prepare(legs, max_invest)
        tl["checks_done"] = time.time()
        plan["timeline"] = tl
        return plan

    def _prepare(self, legs, max_invest=None):
        if not self.venues:
            raise TradeError("Trading needs both API keys. Add POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY to .env.")
        by_ex = {l["exchange"]: l for l in legs}
        if set(by_ex) != set(EXCHANGES):
            raise TradeError("A trade needs one Kalshi leg and one Polymarket leg.")
        contracts = {ex: self.scanner.find_contract(ex, by_ex[ex]["market_id"]) for ex in EXCHANGES}
        if not all(contracts.values()):
            gone = " and ".join(NAMES[ex] for ex in EXCHANGES if not contracts[ex])
            raise TradeError(f"The {gone} market is no longer in the scanner's list: it closed, or its match was removed "
                             f"(e.g. an auto-match held back for review). Nothing was traded; the row will drop off the list.")
        sides = {ex: by_ex[ex]["side"] for ex in EXCHANGES}
        payout = guaranteed_payout([(contracts[ex], sides[ex]) for ex in EXCHANGES])
        if payout <= 0:
            raise TradeError("This pair doesn't guarantee a payout.")

        # Every check on both sites at once (prices move while we look): market info, order book and
        # cash per site; Kalshi's cash depends on the market's shard, so it follows its market info.
        info, levels, balance = {}, {}, {}

        # In the common case none of this downloads anything: the stream's book, cached market details
        # and the scanner's recent cash reading are used, and only what's missing is fetched.
        checks = {}

        def read(ex):
            v, mid = self.venues[ex], contracts[ex].market_id
            info[ex], checks[f"{ex}_info"] = self._market_info(ex, v, mid)
            shard = info[ex].get("shard") if ex == "kalshi" else None
            with LanePool(2) as pool:
                live = self._live_book(ex, mid)
                book = None if live is not None else pool.submit(v.levels, mid)
                cached = self._cached_cash(ex, shard)
                cash = None if cached is not None else pool.submit(v.balance, shard)
                levels[ex] = (live if live is not None else book.result())[sides[ex]]
                balance[ex] = cached if cached is not None else cash.result()
            checks[f"{ex}_book"] = "stream" if live is not None else "download"
            checks[f"{ex}_cash"] = "cached" if cached is not None else "download"
        with LanePool(2) as pool:
            for job in [pool.submit(read, ex) for ex in EXCHANGES]:
                job.result()
        for ex in EXCHANGES:
            if not info[ex]["open"]:
                raise TradeError(f"The {NAMES[ex]} market isn't open for trading.")

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

        def fits(n, kalshi_cash=None, pm_cash=None):
            c = cost_at(n)
            spend = {ex: c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES}
            cash = {"kalshi": balance["kalshi"] if kalshi_cash is None else kalshi_cash,
                    "polymarket": balance["polymarket"] if pm_cash is None else pm_cash}
            return (sum(spend.values()) <= cap and all(spend[ex] <= cash[ex] for ex in EXCHANGES)
                    and payout * n - sum(spend.values()) > 0)

        def largest(kalshi_cash=None, pm_cash=None):
            n = floor_to(full["size"], step)
            if fits(n, kalshi_cash, pm_cash):
                return n
            lo, hi = 0, int(n // step)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if fits(mid * step, kalshi_cash, pm_cash) else (lo, mid - 1)
            return lo * step

        # If even unlimited Kalshi cash wouldn't make one pair fit, Kalshi isn't the limit: say what is.
        if largest(kalshi_cash=math.inf) <= 0 and checks.get("polymarket_cash") == "cached":
            balance["polymarket"] = self.venues["polymarket"].balance(None)    # don't fail on a stale reading
            checks["polymarket_cash"] = "download"
        if largest(kalshi_cash=math.inf) <= 0:
            one = cost_at(step)
            if largest(kalshi_cash=math.inf, pm_cash=math.inf) > 0:
                need = one["polymarket"]["amount"] + one["polymarket"]["fee"]
                raise TradeError(f"Polymarket buying power is ${balance['polymarket']:.2f}, not enough for even one pair "
                                 f"(about ${need:.2f} on Polymarket). Nothing was traded.")
            raise TradeError(f"Not profitable at live prices within your ${cap:.2f} limit any more (the books moved). "
                             f"Nothing was traded.")

        # Kalshi cash is held per exchange shard. If this market's shard is short, move what the trade
        # needs onto it from your other shards first (same account), then size against what arrived.
        shard = info["kalshi"].get("shard") or 0
        transfers, funding_error = [], ""
        if config.KALSHI_AUTO_SHARD_FUNDING and hasattr(self.venues["kalshi"], "fund_shard"):   # any shard, 0 too
            want = largest(kalshi_cash=math.inf)
            c = cost_at(want)
            short = c["kalshi"]["amount"] + c["kalshi"]["fee"] - balance["kalshi"]
            if want > 0 and short > 0.005:
                try:
                    moves, balance["kalshi"] = self.venues["kalshi"].fund_shard(shard, math.ceil((short + 0.05) * 100) / 100)
                    transfers = [{"from": src, "to": shard, "amount": amt} for src, amt in moves]
                except ApiError as e:          # trade with what's there; say why it's small
                    funding_error = f"Kalshi refused moving cash to shard {shard}: {e.detail}"
        n = largest()
        if n <= 0 and balance["kalshi"] < 1:
            moved = f" (moved ${sum(t['amount'] for t in transfers):.2f} there, not arrived yet)" if transfers else ""
            try:
                others = {i: b for i, b in self.venues["kalshi"].shard_balances().items() if i != shard and b >= 0.01}
            except Exception:
                others = {}
            elsewhere = (" You have " + ", ".join(f"${b:.2f} on shard {i}" for i, b in sorted(others.items())) + "."
                         if others else "")
            fix = (funding_error + "." if funding_error else
                   "Automatic shard funding is off (KALSHI_AUTO_SHARD_FUNDING=0): move cash at kalshi.com/account/exchange-indexes, "
                   "or run kalshi-shards.bat to keep every shard funded."
                   if not config.KALSHI_AUTO_SHARD_FUNDING else
                   "Move cash at kalshi.com/account/exchange-indexes, or run kalshi-shards.bat to keep every shard funded.")
            raise TradeError(f"This Kalshi market trades on exchange shard {shard} ({SHARD_NAMES.get(shard, 'another shard')}) "
                             f"and you have ${balance['kalshi']:.2f} there{moved}.{elsewhere} Kalshi only lets an order use "
                             f"cash on its own shard. {fix} Nothing was traded.")
        if n <= 0:
            where = f" on shard {shard}"
            limits = f"cap ${cap:.2f}, Kalshi cash{where} ${balance['kalshi']:.2f}, Polymarket buying power ${balance['polymarket']:.2f}"
            raise TradeError(f"Can't fit even one profitable pair within your limits ({limits}).")

        c = cost_at(n)
        spare = {ex: sum(q for pr, q in levels[ex] if pr <= c[ex]["limit"] + 1e-9) - n for ex in EXCHANGES}
        mode = config.TRADE_ORDER
        # polymarket_first: the slower site goes first and Kalshi (fast) is bought for exactly what filled,
        # so a Polymarket miss trades nothing. thinner_first: the book with less spare depth goes first.
        first = "polymarket" if mode == "polymarket_first" else min(EXCHANGES, key=lambda ex: spare[ex])
        plan = {
            "id": uuid.uuid4().hex, "created": time.time(), "size": n, "payout": payout, "first": first,
            "legs": {ex: {"exchange": ex, "market_id": contracts[ex].market_id, "side": sides[ex],
                          "title": contracts[ex].title, "limit": c[ex]["limit"], "amount": c[ex]["amount"],
                          "fee": c[ex]["fee"], "fee_coef": contracts[ex].fee_coef, "min_qty": info[ex]["min_qty"],
                          "available_at_limit": n + spare[ex], "balance": balance[ex]} for ex in EXCHANGES},
        }
        plan["capital"] = sum(c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES)
        plan["shard_transfers"] = transfers
        plan["checks"] = checks
        plan["together"] = mode == "together"
        plan["order_mode"] = mode
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
            with priority():
                res = self._run(plan, info)
        tl = plan.setdefault("timeline", {})
        tl["done"] = time.time()
        res["timeline"] = {"stages": timeline_stages(tl), "checks": plan.get("checks")}
        return res

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

        fills_b, hedged, first_attempt = [], 0.0, 0
        if plan.get("together"):
            # 1. both legs at once at their planned limits
            A, B, fa, fb = self._send_both(plan, record, steps, log)
            va, vb = self.venues[A["exchange"]], self.venues[B["exchange"]]
            if fa.qty <= 0:
                self._write(log, status="no_fill")
                return self._result(plan, "no_fill", steps, fa, [], None, 0,
                                    "Neither order filled (the prices moved). Nothing was traded.")
            fills_b, hedged, first_attempt = [fb], fb.qty, 1     # B's planned-limit try is done
            return self._finish(plan, info, A, B, va, vb, fa, fills_b, hedged, first_attempt, steps, log, record)

        # 1. first leg
        try:
            fa = self._timed_buy(plan, "first", A, plan["size"], A["limit"])
        except ApiError as e:
            record("first", A, error=str(e))
            self._write(log, status="failed_first_leg")
            hint = (" Kalshi keeps cash separately per exchange shard and this market's shard has none: run "
                    "kalshi-shards.bat once, or move cash at kalshi.com/account/exchange-indexes."
                    if "shard" in str(e.detail).lower() else "")
            raise TradeError(f"{NAMES[A['exchange']]} rejected the first order, nothing was traded: {e.detail}{hint}")
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

        return self._finish(plan, info, A, B, va, vb, fa, fills_b, hedged, first_attempt, steps, log, record)

    def _send_both(self, plan, record, steps, log):
        """Send both orders at the same moment. Returns (A, B, fill A, fill B) with A the side that
        filled more (plan["first"] is set to it). A rejected order counts as no fill; an order whose
        outcome can't be confirmed stops everything."""
        legs = [plan["legs"]["kalshi"], plan["legs"]["polymarket"]]

        def send(leg):
            try:
                return self._timed_buy(plan, "together", leg, plan["size"], leg["limit"]), None
            except Exception as e:
                return None, e
        with LanePool(2) as pool:
            out = list(pool.map(send, legs))
        fills, unknown = {}, []
        for leg, (fill, err) in zip(legs, out):
            name = NAMES[leg["exchange"]]
            if isinstance(err, ApiError):
                record("together", leg, error=str(err))
                hint = (" (Kalshi keeps cash per exchange shard and this market's shard is short)"
                        if "shard" in str(err.detail).lower() else "")
                steps.append(f"{name}: order rejected ({err.detail}){hint}")
                fill = Fill()
            elif err is not None:
                record("together", leg, error=repr(err))
                unknown.append(f"{name} ({err})")
                fill = Fill()
            else:
                record("together", leg, fill)
                steps.append(f"{name}: bought {fill.qty:g} {leg['side'].upper()} for ${fill.amount:.2f} + ${fill.fee:.2f} fee")
            fills[leg["exchange"]] = fill
        if unknown:
            self._write(log, status="unknown_together")
            got = ", ".join(f"{NAMES[ex]} filled {f.qty:g}" for ex, f in fills.items() if f.qty)
            raise TradeError(f"Couldn't confirm the {' and '.join(unknown)} order. {got + '. ' if got else ''}"
                             f"Check that account and the other before doing anything else; nothing was sold back.")
        first = max(("kalshi", "polymarket"), key=lambda ex: fills[ex].qty)
        plan["first"] = first
        second = "polymarket" if first == "kalshi" else "kalshi"
        return plan["legs"][first], plan["legs"][second], fills[first], fills[second]

    def _finish(self, plan, info, A, B, va, vb, fa, fills_b, hedged, first_attempt, steps, log, record):
        # 2. second leg, sized to what actually filled, never above break-even
        target = floor_to(fa.qty, B["min_qty"])
        a_cost_per = (fa.amount + fa.fee) / fa.qty
        tick_b = info[B["exchange"]]["tick"]
        status, note = "ok", ""
        for attempt in range(first_attempt, 1 + config.SECOND_LEG_RETRIES):
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
                fb = self._timed_buy(plan, "second" if attempt == 0 else f"retry {attempt}", B, remaining, limit)
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
        B = plan["legs"]["polymarket" if plan["first"] == "kalshi" else "kalshi"]
        kept_a = fa.qty - sold_qty
        legs_filled = {A["exchange"]: {"shares": kept_a, "paid": round(a_per * kept_a, 2)},
                       B["exchange"]: {"shares": sum(f.qty for f in fills_b), "paid": round(b_spent, 2)}}
        return {"status": status, "steps": steps, "note": note, "hedged_pairs": hedged, "legs_filled": legs_filled,
                "plan": {"payout": plan["payout"], "legs": plan["legs"]},
                "locked_profit": round(locked, 2), "sellback_pnl": round(sellback_pnl, 2),
                "net": round(locked + sellback_pnl, 2), "unhedged_shares": round(unhedged, 4),
                "unhedged_exchange": NAMES[A["exchange"]] if unhedged > 1e-9 else None,
                "unhedged_side": A["side"] if unhedged > 1e-9 else None, "payout": plan["payout"]}

    def _write(self, log, status):
        log["status"], log["finished"] = status, engine.now_utc().isoformat()
        with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log, default=str) + "\n")
