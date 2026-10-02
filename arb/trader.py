"""'Make trade': size an arb from live books, then execute both legs safely.

Sequence (per the user's chosen policy):
  1. prepare(): fresh order books + balances -> size = min of both legs' profitable depth,
     capped by MAX_TRADE_DOLLARS, the dashboard's "Max to invest", and each account's cash.
     Returns a plan for the confirm dialog; nothing is sent.
  2. execute():
     a. A plan that waited in the confirm dialog is re-sized on books read right then: it can
        shrink, never grow, and nothing is sent if the arb is gone.
     b. The orders, per TRADE_ORDER (Auto-trade: AUTO_TRADE_ORDER). thinner_first: the thinner
        leg first as immediate-or-cancel; the other leg is then bought for exactly what filled,
        limited at break-even, with SECOND_LEG_RETRIES more attempts as soon as its book refills.
     c. First-leg shares still unhedged are closed whichever way loses less: sold back, or hedged
        up to CLOSE_OUT_MAX_LOSS per share above break-even.
     Every order and result is appended to trades.jsonl.
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
    def __init__(self, message="", exchange=None):
        super().__init__(message)
        self.exchange = exchange           # the site that rejected an order, when one did


def _fee(exchange, qty, price, coef):
    return total_fee(exchange, [(price, qty)], coef)


ORDER_MODES = ("together", "polymarket_first", "thinner_first")


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


def fit_size(levels, coefs, payout, cap, cash, step, max_n=math.inf):
    """Most pairs (a multiple of step, at most max_n) profitable on these books within cap and each
    site's cash; 0 if not even one."""
    cand = {"k": SimpleNamespace(exchange="kalshi", fee_coef=coefs["kalshi"]),
            "p": SimpleNamespace(exchange="polymarket", fee_coef=coefs["polymarket"]), "payout": payout}
    full = engine.size_opportunity(cand, levels["kalshi"], levels["polymarket"])
    if not full:
        return 0

    def fits(n):
        c = cost_at(levels, coefs, n)
        if any(sum(q for _, q in c[ex]["fills"]) + 1e-9 < n for ex in EXCHANGES):
            return False
        spend = {ex: c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES}
        return (sum(spend.values()) <= cap and all(spend[ex] <= cash[ex] for ex in EXCHANGES)
                and payout * n - sum(spend.values()) > 0)

    n = floor_to(min(full["size"], max_n), step)
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
        """Cash from the scanner's balance reading if it's fresh (and not marked stale by a trade).
        Polymarket buying power pushed by its private stream wins: it's current even right after a trade."""
        if ex == "polymarket":
            live = getattr((self.venues or {}).get("polymarket"), "stream_buying_power", lambda: None)()
            if live is not None:
                return live
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

    def prepare(self, legs, max_invest=None, timeline=None, hedge_depth=1.0, order=None, first=None, dry=False,
                book_share=None):
        """hedge_depth > 1: shrink the size until the second leg's book holds that many times the shares
        at or below break-even, so a small move between the two orders can't leave the first leg unhedged.
        order: how the two orders go out (ORDER_MODES); TRADE_ORDER if not given. first: which site goes
        first in a one-after-the-other order (else the thinner book). dry: a paper trade (simulate()): no
        cash limits, nothing moved between Kalshi shards. book_share: take at most this share of the shares
        each book shows at the prices paid."""
        tl = dict(timeline or {})
        tl.setdefault("decided", time.time())      # Make trade: the click is the decision
        with priority():                   # trades go ahead of background market loads
            plan = self._prepare(legs, max_invest, hedge_depth, order if order in ORDER_MODES else config.TRADE_ORDER,
                                 first=first, dry=dry, book_share=book_share)
        tl["checks_done"] = time.time()
        plan["timeline"] = tl
        return plan

    def _prepare(self, legs, max_invest=None, hedge_depth=1.0, mode=None, first=None, dry=False, book_share=None):
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
            if live is not None:                  # how long since the live feed last changed this book
                t = getattr((getattr(self.scanner, "streams", None) or {}).get(ex), "updated_at", {}).get(mid)
                checks[f"{ex}_book_age"] = round(time.time() - t, 2) if t else None
            checks[f"{ex}_cash"] = "cached" if cached is not None else "download"
        with LanePool(2) as pool:
            for job in [pool.submit(read, ex) for ex in EXCHANGES]:
                job.result()
        for ex in EXCHANGES:
            if not info[ex]["open"]:
                raise TradeError(f"The {NAMES[ex]} market isn't open for trading.")
        if dry:                                    # paper trade: what the books allow, whatever the cash
            balance = {ex: math.inf for ex in EXCHANGES}

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
        if config.KALSHI_AUTO_SHARD_FUNDING and not dry and hasattr(self.venues["kalshi"], "fund_shard"):   # any shard, 0 too
            want = largest(kalshi_cash=math.inf)
            c = cost_at(want)
            short = c["kalshi"]["amount"] + c["kalshi"]["fee"] - balance["kalshi"]
            if want > 0 and short > 0.005:
                try:
                    moves, balance["kalshi"] = self.venues["kalshi"].fund_shard(shard, math.ceil((short + 0.05) * 100) / 100)
                    transfers = [{"from": src, "to": shard, "amount": amt} for src, amt in moves]
                except ApiError as e:          # trade with what's there; say why it's small
                    funding_error = f"Kalshi refused moving cash to shard {shard}: {e.detail}"
        if transfers:
            # Waiting for the transfer took seconds: size on books read now, not on the ones from before it.
            def book(ex):
                live = self._live_book(ex, contracts[ex].market_id)
                return (live if live is not None else self.venues[ex].levels(contracts[ex].market_id))[sides[ex]]
            with LanePool(2) as pool:
                fresh = {ex: pool.submit(book, ex) for ex in EXCHANGES}
                levels.update({ex: job.result() for ex, job in fresh.items()})
            checks["kalshi_book"] = checks["polymarket_book"] = "re-read after shard transfer"
            full = engine.size_opportunity(cand, levels["kalshi"], levels["polymarket"])
            if not full:
                raise TradeError(f"The arb was gone by the time the cash reached Kalshi shard {shard} (moved "
                                 f"${sum(t['amount'] for t in transfers):.2f}; it stays there for next time). "
                                 f"Nothing was traded.")
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

        if book_share and 0 < book_share < 1:
            # Never the whole book: shares shown at a price are often gone (or pulled) by the time an order
            # lands, so take at most this share of what each book shows at the prices being paid.
            def within_share(n):
                cn = cost_at(n)
                return all(n <= book_share * sum(q for pr, q in levels[ex] if pr <= cn[ex]["limit"] + 1e-9) + 1e-9
                           for ex in EXCHANGES)
            if not within_share(n):
                lo, hi = 0, int(n // step)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    lo, hi = (mid, hi) if within_share(mid * step) else (lo, mid - 1)
                if lo <= 0:
                    raise TradeError(f"The books are too thin: even one pair would take more than {book_share:.0%} of "
                                     f"the shares shown at these prices. Nothing was traded.")
                n = lo * step
        c = cost_at(n)
        spare = {ex: sum(q for pr, q in levels[ex] if pr <= c[ex]["limit"] + 1e-9) - n for ex in EXCHANGES}
        mode = mode or config.TRADE_ORDER
        # polymarket_first: the slower site goes first and Kalshi (fast) is bought for exactly what filled,
        # so a Polymarket miss trades nothing. thinner_first: the book with less spare depth goes first.
        # first: the caller's pick (Auto-trade sends the stale side first: it's the one about to reprice).
        if first in EXCHANGES and mode != "together":
            pass
        elif mode == "polymarket_first":
            first = "polymarket"
        else:
            first = min(EXCHANGES, key=lambda ex: spare[ex])
        second = "kalshi" if first == "polymarket" else "polymarket"

        # Legs that may have to hedge the other: the second one, or either when both go out together.
        hedgers = [(first, second), (second, first)] if mode == "together" else [(first, second)]

        def hedge_room(n, a, b):
            """Shares leg b's book holds at or below the price where n pairs break even, given leg a's cost."""
            cn = cost_at(n)
            room = (payout - (cn[a]["amount"] + cn[a]["fee"]) / n) * n
            pmax = break_even_price(b, n, room, contracts[b].fee_coef, info[b]["tick"])
            return sum(q for pr, q in levels[b] if pr <= pmax + 1e-9)

        def deep_enough(n):
            return all(hedge_room(n, a, b) >= hedge_depth * n for a, b in hedgers)

        if hedge_depth > 1 and n > 0 and not deep_enough(n):
            lo, hi = 0, int(n // step)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if deep_enough(mid * step) else (lo, mid - 1)
            if lo <= 0:
                thin = " and ".join(NAMES[b] for a, b in hedgers if hedge_room(step, a, b) < hedge_depth * step)
                raise TradeError(f"The {thin or NAMES[second]} book is too thin to hedge safely (it needs {hedge_depth:g}x "
                                 f"the shares within break-even). Nothing was traded.")
            n = lo * step
            c = cost_at(n)
            spare = {ex: sum(q for pr, q in levels[ex] if pr <= c[ex]["limit"] + 1e-9) - n for ex in EXCHANGES}
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
        plan["dry"] = dry
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

        # 0. A plan that sat in the confirm dialog: re-size it on books read now.
        if time.time() - plan["created"] > config.PLAN_RECHECK_AFTER_SECS:
            try:
                still = self._recheck(plan, info, steps)
            except Exception as e:
                self._write(log, status="recheck_failed")
                raise TradeError(f"Couldn't re-check live prices ({e}). Nothing was traded.")
            if not still:
                self._write(log, status="moved")
                return self._result(plan, "moved", steps, Fill(), [], None, 0,
                                    "Prices moved after you confirmed and the arb is gone at live prices. "
                                    "Nothing was traded.")

        fills_b, hedged, first_attempt = [], 0.0, 0
        if plan.get("together"):
            # 1. both legs at once at their planned limits
            A, B, fa, fb = self._send_both(plan, record, steps, log)
            va, vb = self.venues[A["exchange"]], self.venues[B["exchange"]]
            if fa.qty <= 0:
                errs = {o["exchange"]: o["error"] for o in log["orders"] if o.get("error")}
                missed = [self._miss(leg, plan["size"], leg["limit"], errs.get(leg["exchange"])) for leg in (A, B)]
                log["missed"] = missed
                self._write(log, status="no_fill")
                return self._result(plan, "no_fill", steps, fa, [], None, 0,
                                    "Neither order filled (the prices moved). Nothing was traded.", missed)
            fills_b, hedged, first_attempt = [fb], fb.qty, 1     # B's planned-limit try is done
            return self._finish(plan, info, A, B, va, vb, fa, fills_b, hedged, first_attempt, steps, log, record)

        # 1. first leg (meanwhile the second site gets a connection ready for its order)
        warm = getattr(vb, "warm", None)
        if warm:
            threading.Thread(target=self._quiet, args=(warm,), daemon=True).start()
        try:
            fa = self._timed_buy(plan, "first", A, plan["size"], A["limit"])
        except ApiError as e:
            record("first", A, error=str(e))
            self._write(log, status="failed_first_leg")
            hint = (" Kalshi keeps cash separately per exchange shard and this market's shard has none: run "
                    "kalshi-shards.bat once, or move cash at kalshi.com/account/exchange-indexes."
                    if "shard" in str(e.detail).lower() else "")
            raise TradeError(f"{NAMES[A['exchange']]} rejected the first order, nothing was traded: {e.detail}{hint}",
                             exchange=NAMES[A["exchange"]])
        except Exception as e:          # sent, but the outcome couldn't be confirmed
            record("first", A, error=repr(e))
            self._write(log, status="unknown_first_leg")
            raise TradeError(f"Couldn't confirm the {NAMES[A['exchange']]} order ({e}). Check that account before "
                             f"doing anything else; the second leg was NOT placed.")
        record("first", A, fa)
        steps.append(f"{NAMES[A['exchange']]}: bought {fa.qty:g} {A['side'].upper()} for ${fa.amount:.2f} + ${fa.fee:.2f} fee")
        if fa.qty <= 0:
            log["missed"] = [self._miss(A, plan["size"], A["limit"])]
            self._write(log, status="no_fill")
            return self._result(plan, "no_fill", steps, fa, [], None, 0, "The first leg didn't fill (the price moved). "
                                "Nothing was traded.", log["missed"])

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
        last = {"limit": B["limit"], "error": None}
        for attempt in range(first_attempt, 1 + config.SECOND_LEG_RETRIES):
            remaining = floor_to(target - hedged, B["min_qty"])
            if remaining <= 0:
                break
            # Each share hedged now must break even on its own: payout - first-leg cost per share.
            room = (plan["payout"] - a_cost_per) * remaining
            pmax = break_even_price(B["exchange"], remaining, room, B["fee_coef"], tick_b)
            limit = pmax if attempt or config.SECOND_LEG_AT_BREAKEVEN else min(B["limit"], pmax)
            if limit <= 0:
                note = "No second-leg price can break even anymore."
                break
            try:
                fb = self._timed_buy(plan, "second" if attempt == 0 else f"retry {attempt}", B, remaining, limit)
            except ApiError as e:
                record("second", B, error=str(e))
                steps.append(f"{NAMES[B['exchange']]}: order rejected ({e.detail})")
                last.update(limit=limit, error=e.detail)
                fb = Fill()
            except Exception as e:      # network error: the order may or may not exist, so stop here
                record("second", B, error=repr(e))
                self._write(log, status="unknown_second_leg")
                return self._result(plan, "unknown", steps, fa, fills_b, None, hedged,
                                    f"Couldn't confirm the {NAMES[B['exchange']]} order ({e!r}). Check both accounts "
                                    f"before doing anything else. Nothing was sold back.")
            else:
                last.update(limit=limit, error=None)
                record("second", B, fb)
                steps.append(f"{NAMES[B['exchange']]}: bought {fb.qty:g} {B['side'].upper()} at ≤ ${limit:.3f} "
                             f"for ${fb.amount:.2f} + ${fb.fee:.2f} fee" + (f" (retry {attempt})" if attempt else ""))
            fills_b.append(fb)
            hedged += fb.qty
            if hedged + 1e-9 < target and attempt < config.SECOND_LEG_RETRIES:
                self._await_liquidity(B, last["limit"], since=time.time())

        # 3. close whatever the second leg couldn't cover at break-even, the cheaper way
        excess = fa.qty - hedged
        sold, missed, at_break_even = None, None, hedged
        if excess > 1e-9:
            status = "partial"
            cash_b = (B.get("balance") or math.inf) - sum(f.amount + f.fee for f in fills_b)
            extra, sold = self._close_out(plan, info, A, B, va, vb, excess, a_cost_per, cash_b, record, steps)
            fills_b += extra
            hedged += sum(f.qty for f in extra)
        if status == "partial":          # after the close-out: looking at the book can wait, that can't
            missed = [self._miss(B, target, last["limit"], last["error"], at_break_even)]
        log["missed"] = missed
        self._write(log, status=status)
        return self._result(plan, status, steps, fa, fills_b, sold, hedged, note, missed)

    @staticmethod
    def _quiet(fn):
        try:
            fn()
        except Exception:
            pass

    def _recheck(self, plan, info, steps):
        """Re-size a confirmed plan on books and cash read right now: never more pairs than you
        confirmed nor more than its cap. Returns False when not even one pair is profitable."""
        legs, levels, cash = plan["legs"], {}, {}

        def read(ex):
            leg, v = legs[ex], self.venues[ex]
            live = self._live_book(ex, leg["market_id"])
            levels[ex] = (live if live is not None else v.levels(leg["market_id"]))[leg["side"]]
            cash[ex] = v.balance(info[ex].get("shard") if ex == "kalshi" else None)
        with LanePool(2) as pool:
            for job in [pool.submit(read, ex) for ex in EXCHANGES]:
                job.result()
        coefs = {ex: legs[ex]["fee_coef"] for ex in EXCHANGES}
        step = max(legs["kalshi"]["min_qty"], legs["polymarket"]["min_qty"], 1.0)
        n = fit_size(levels, coefs, plan["payout"], plan["cap"], cash, step, max_n=plan["size"])
        plan["recheck"] = {"size": n, "after_secs": round(time.time() - plan["created"], 1)}
        if n <= 0:
            return False
        c = cost_at(levels, coefs, n)
        before_n, before = plan["size"], plan["expected_profit"]
        for ex in EXCHANGES:
            legs[ex].update(limit=c[ex]["limit"], amount=c[ex]["amount"], fee=c[ex]["fee"], balance=cash[ex])
        plan["size"], plan["capital"] = n, sum(c[ex]["amount"] + c[ex]["fee"] for ex in EXCHANGES)
        plan["expected_profit"] = plan["payout"] * n - plan["capital"]
        if n != before_n or abs(plan["expected_profit"] - before) >= 0.005:
            steps.append(f"Live re-check: {n:g} pairs, expected +${plan['expected_profit']:.2f} "
                         f"(you confirmed {before_n:g} pairs, +${before:.2f})")
        return True

    def _await_liquidity(self, leg, limit, since):
        """Pause before a second-leg retry so the book can refill. With a live stream, go as soon as it
        shows a fresh book with shares at or below the limit; at most SECOND_LEG_RETRY_PAUSE either way."""
        deadline = time.monotonic() + config.SECOND_LEG_RETRY_PAUSE
        stream = (getattr(self.scanner, "streams", None) or {}).get(leg["exchange"])
        m = (getattr(self.scanner, "source", None) or {}).get((leg["exchange"], leg["market_id"]))
        if not stream or m is None or not getattr(stream, "connected", False):
            time.sleep(config.SECOND_LEG_RETRY_PAUSE)
            return
        while time.monotonic() < deadline:
            if (getattr(stream, "updated_at", {}).get(leg["market_id"]) or 0) > since and any(
                    p <= limit + 1e-9 for p, _ in (getattr(m, "levels", None) or {}).get(leg["side"]) or []):
                return
            time.sleep(0.02)

    def _close_out(self, plan, info, A, B, va, vb, excess, a_cost_per, cash_b, record, steps):
        """First-leg shares the second leg couldn't hedge at break-even. Close them whichever way gets
        more back per share: sell them back into the first site's bids, or hedge them on the second
        site a little above break-even (at most CLOSE_OUT_MAX_LOSS per share). Returns (extra
        second-leg fills, sell-back fill or None)."""
        try:
            with LanePool(2) as pool:
                ja, jb = pool.submit(va.levels, A["market_id"]), pool.submit(vb.levels, B["market_id"])
                lv_a, lv_b = ja.result(), jb.result()
        except Exception as e:
            record("sellback", A, error=repr(e))
            steps.append(f"Couldn't read the books to close {excess:g} unhedged shares ({e})")
            return [], None
        other = "no" if A["side"] == YES else "yes"
        bids_a = [(round(1 - p, 6), q) for p, q in lv_a[other]]          # what one of our shares sells for
        worst_b = plan["payout"] - a_cost_per + config.CLOSE_OUT_MAX_LOSS
        hedge_fills = engine._take([(p, q) for p, q in lv_b[B["side"]] if p <= worst_b + 1e-9],
                                   floor_to(excess, B["min_qty"]))
        sell_fills = engine._take(bids_a, floor_to(excess, A["min_qty"]))
        hedge_q, sell_q = sum(q for _, q in hedge_fills), sum(q for _, q in sell_fills)
        hedge_cost = sum(p * q for p, q in hedge_fills) + total_fee(B["exchange"], hedge_fills, B["fee_coef"])
        hedge_per = (plan["payout"] * hedge_q - hedge_cost) / hedge_q if hedge_q else -math.inf
        sell_per = ((sum(p * q for p, q in sell_fills) - total_fee(A["exchange"], sell_fills, A["fee_coef"])) / sell_q
                    if sell_q else -math.inf)
        extra, sold = [], None
        if hedge_q and hedge_per > sell_per and hedge_cost <= cash_b:
            try:
                fb = self._timed_buy(plan, "close hedge", B, hedge_q, hedge_fills[-1][0])
                record("close_hedge", B, fb)
                extra.append(fb)
                if fb.qty:
                    pnl = plan["payout"] * fb.qty - a_cost_per * fb.qty - fb.amount - fb.fee
                    steps.append(f"{NAMES[B['exchange']]}: hedged {fb.qty:g} more {B['side'].upper()} above break-even "
                                 f"for ${fb.amount:.2f} + ${fb.fee:.2f} fee ({'+' if pnl >= 0 else '-'}${abs(pnl):.2f} on "
                                 f"them), cheaper than selling back")
            except Exception as e:
                record("close_hedge", B, error=repr(e))
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
            try:
                sold = va.sell(A["market_id"], A["side"], sell_qty, min_price, A["fee_coef"])
                record("sellback", A, sold)
                steps.append(f"{NAMES[A['exchange']]}: sold back {sold.qty:g} unhedged {A['side'].upper()} "
                             f"for ${sold.amount:.2f} − ${sold.fee:.2f} fee")
            except Exception as e:
                record("sellback", A, error=repr(e))
                steps.append(f"{NAMES[A['exchange']]}: sell-back failed ({e})")
        return extra, sold

    # ---- paper trading ------------------------------------------------------------------------

    def simulate(self, plan_id, latency=None, sleep=None):
        """A paper trade: the plan's orders played against the real books, each read at the moment that
        order would have landed (the site's typical order time after the one before), with the same
        rules as a real trade: second leg at break-even with retries, leftovers closed the cheaper way.
        Nothing is sent. Returns a result shaped like execute()'s, with "paper": True. It assumes the
        shares shown were really there and that taking them wouldn't have moved anyone, so real fills
        can only be the same or worse."""
        with self.lock:
            entry = self.plans.pop(plan_id, None)
        if not entry:
            raise TradeError("That plan was already used or doesn't exist.")
        plan, info = entry
        lat = {**config.PAPER_LATENCY, **(latency or {})}
        sleep = sleep or time.sleep
        steps, missed, payout = [], [], plan["payout"]

        def book(leg):
            live = self._live_book(leg["exchange"], leg["market_id"])
            return live if live is not None else self.venues[leg["exchange"]].levels(leg["market_id"])

        def take(leg, qty, limit, lv, kind):
            fills = engine._take([(p, q) for p, q in lv[leg["side"]] if p <= limit + 1e-9], floor_to(qty, leg["min_qty"]))
            f = Fill(qty=sum(q for _, q in fills), amount=sum(p * q for p, q in fills), order_id="paper")
            f.fee = total_fee(leg["exchange"], fills, leg["fee_coef"]) if fills else 0.0
            steps.append(f"{NAMES[leg['exchange']]}: would have bought {f.qty:g} {leg['side'].upper()} at ≤ ${limit:.3f} "
                         f"for ${f.amount:.2f} + ${f.fee:.2f} fee ({kind})")
            if f.qty + 1e-9 < qty:
                best = lv[leg["side"]][0][0] if lv[leg["side"]] else None
                missed.append({"exchange": NAMES[leg["exchange"]], "best": best,
                               "gap": max(0.0, best - limit) if best is not None else None,
                               "why": f"would have filled {f.qty:g} of {qty:g} at ≤ ${limit:.3f}" + (
                                   f"; best price then ${best:.3f}" if best is not None else "; no sellers then")})
            return f

        legs = plan["legs"]
        if plan["together"]:                       # both sent at once: each lands after its own site's time
            order = sorted(EXCHANGES, key=lambda ex: lat[ex])
            sleep(lat[order[0]])
            lv0 = book(legs[order[0]])
            sleep(max(0.0, lat[order[1]] - lat[order[0]]))
            lv1 = book(legs[order[1]])
            got = {order[0]: take(legs[order[0]], plan["size"], legs[order[0]]["limit"], lv0, "together"),
                   order[1]: take(legs[order[1]], plan["size"], legs[order[1]]["limit"], lv1, "together")}
            plan["first"] = max(EXCHANGES, key=lambda ex: got[ex].qty)
        A = legs[plan["first"]]
        B = legs["polymarket" if plan["first"] == "kalshi" else "kalshi"]
        if plan["together"]:
            fa, fills_b = got[A["exchange"]], [got[B["exchange"]]]
            hedged, attempt = fills_b[0].qty, 1
        else:
            sleep(lat[A["exchange"]])
            fa = take(A, plan["size"], A["limit"], book(A), "first")
            fills_b, hedged, attempt = [], 0.0, 0
        if fa.qty <= 0:
            return {**self._result(plan, "no_fill", steps, fa, [], None, 0, "Paper: the first leg wouldn't have filled.",
                                   missed), "paper": True}
        a_cost = (fa.amount + fa.fee) / fa.qty
        tick_b = info[B["exchange"]]["tick"]
        for attempt in range(attempt, 1 + config.SECOND_LEG_RETRIES):
            remaining = floor_to(fa.qty - hedged, B["min_qty"])
            if remaining <= 0:
                break
            limit = break_even_price(B["exchange"], remaining, (payout - a_cost) * remaining, B["fee_coef"], tick_b)
            if limit <= 0:
                break
            sleep(lat[B["exchange"]] + (config.SECOND_LEG_RETRY_PAUSE if attempt else 0.0))
            fb = take(B, remaining, limit, book(B), "second" if attempt == 0 else f"retry {attempt}")
            fills_b.append(fb)
            hedged += fb.qty
        missed = [m for m in missed if m["exchange"] == NAMES[B["exchange"]]] or missed
        excess, sold, status = fa.qty - hedged, None, "ok"
        if excess > 1e-9:                          # leftovers: hedge a little above break-even, or sell back
            status = "partial"
            lv_a, lv_b = book(A), book(B)
            worst = payout - a_cost + config.CLOSE_OUT_MAX_LOSS
            hedge = engine._take([(p, q) for p, q in lv_b[B["side"]] if p <= worst + 1e-9], floor_to(excess, B["min_qty"]))
            other = "no" if A["side"] == YES else "yes"
            sell = engine._take([(round(1 - p, 6), q) for p, q in lv_a[other]], floor_to(excess, A["min_qty"]))
            hq, sq = sum(q for _, q in hedge), sum(q for _, q in sell)
            hcost = sum(p * q for p, q in hedge) + (total_fee(B["exchange"], hedge, B["fee_coef"]) if hedge else 0)
            h_per = (payout * hq - hcost) / hq if hq else -math.inf
            s_per = ((sum(p * q for p, q in sell) - total_fee(A["exchange"], sell, A["fee_coef"])) / sq) if sq else -math.inf
            if hq and h_per > s_per:
                fb = Fill(qty=hq, amount=sum(p * q for p, q in hedge), order_id="paper")
                fb.fee = hcost - fb.amount
                fills_b.append(fb)
                hedged += hq
                steps.append(f"{NAMES[B['exchange']]}: would have hedged {hq:g} more above break-even")
            elif sq:
                sold = Fill(qty=sq, amount=sum(p * q for p, q in sell), order_id="paper")
                sold.fee = total_fee(A["exchange"], sell, A["fee_coef"])
                steps.append(f"{NAMES[A['exchange']]}: would have sold back {sq:g} for ${sold.amount:.2f}")
        return {**self._result(plan, status, steps, fa, fills_b, sold, hedged, "", missed), "paper": True}

    def _miss(self, leg, qty, limit, error=None, filled=0.0):
        """Why an order didn't (fully) fill, for the Auto-trade fail-safe and history: the site's
        rejection, or the price on that book now next to the limit the order carried."""
        why, gap_dollars, best = f"rejected: {error}" if error else "", None, None
        if not error:
            why = f"filled {filled:g} of {qty:g} at ≤ ${limit:.3f}"
            try:
                lv = self.venues[leg["exchange"]].levels(leg["market_id"])[leg["side"]]
                if lv:
                    best, gap_dollars = lv[0][0], max(0.0, lv[0][0] - limit)
                    gap = (lv[0][0] - limit) * 100
                    why += (f"; best price there now ${lv[0][0]:.3f}" +
                            (f" ({gap:+.1f}¢ vs the limit: the price moved)" if gap > 0.05 else
                             f", {sum(q for p, q in lv if p <= limit + 1e-9):g} shares at the limit now (taken before ours arrived)"))
                else:
                    why += "; no sellers there now"
            except Exception:
                pass
        return {"exchange": NAMES[leg["exchange"]], "why": why, "gap": gap_dollars, "best": best}

    def _result(self, plan, status, steps, fa, fills_b, sold, hedged, note, missed=None):
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
        # How far prices moved against the plan while the orders went out ($/share): the second leg's average
        # fill over its planned price, or, on an order that missed, the best price then over the planned one
        # (not over the limit: that already includes the arb's edge). Auto-trade learns from this how much
        # edge each kind of market needs.
        slips, planned = [], {}
        if plan.get("size"):
            planned = {ex: leg["amount"] / plan["size"] for ex, leg in plan["legs"].items()}
        by_name = {v: k for k, v in NAMES.items()}
        for m in missed or []:
            ex = by_name.get(m.get("exchange"))
            if m.get("best") is not None and ex in planned:
                slips.append(max(0.0, m["best"] - planned[ex]))
        qb = sum(f.qty for f in fills_b)
        if qb > 0 and planned:
            slips.append(max(0.0, sum(f.amount for f in fills_b) / qb - planned[B["exchange"]]))
        return {"status": status, "steps": steps, "note": note, "hedged_pairs": hedged, "legs_filled": legs_filled,
                "first": NAMES[A["exchange"]], "missed": missed or [],
                "plan": {"payout": plan["payout"], "legs": plan["legs"]},
                "locked_profit": round(locked, 2), "sellback_pnl": round(sellback_pnl, 2),
                "net": round(locked + sellback_pnl, 2), "unhedged_shares": round(unhedged, 4),
                "unhedged_exchange": NAMES[A["exchange"]] if unhedged > 1e-9 else None,
                "unhedged_side": A["side"] if unhedged > 1e-9 else None, "payout": plan["payout"],
                "slip": round(max(slips), 4) if slips else None, "order_mode": plan.get("order_mode"),
                "first_exchange": A["exchange"], "checks": plan.get("checks")}

    def _write(self, log, status):
        log["status"], log["finished"] = status, engine.now_utc().isoformat()
        with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log, default=str) + "\n")
