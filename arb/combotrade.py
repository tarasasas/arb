"""Make trade for combos (the Combos tab): 2 or 3 legs, on one site or both (see combos.py).

  1. prepare(): live books (the live feeds' while alive), market details and cash, all at once; the size
     is the most whole sets that are profitable on those books within MAX_TRADE_DOLLARS, your Max to invest
     and the cash on each site (Kalshi: on each leg's exchange shard). Nothing is sent.
  2. execute():
     a. A plan that waited in the confirm dialog is re-sized on books read right then (shrinks, never
        grows; nothing is sent if the combo is gone).
     b. Every leg at once, immediate-or-cancel, each limit raised by its share of TOGETHER_HEADROOM x the
        profit, so a one-tick move still fills while every set still at least breaks even.
     c. Legs that filled short are topped up to the leg that filled most, never above break-even per set,
        up to SECOND_LEG_RETRIES more times.
     d. Shares that still don't make a whole set are sold back.
     Every order goes to trades.jsonl ("kind": "combo").
"""

import itertools
import math
import threading
import time
import uuid
from collections import deque

from . import combos, config, engine
from .http import ApiError, trading
from .model import YES, guaranteed_payout
from .trader import EXCHANGES, NAMES, _IO, TradeError, break_even_price, timeline_stages
from .venues import Fill, floor_to


class ComboTrader:
    def __init__(self, trader):
        self.trader = trader               # venues, the trade lock, live books, cached details and cash
        self.plans = {}
        self.history = deque(maxlen=20)

    @property
    def scanner(self):
        return self.trader.scanner

    # ---- planning ------------------------------------------------------------------------------

    @staticmethod
    def _check_legs(legs):
        out = []
        for l in legs or []:
            ex, side = str(l.get("exchange", "")).lower(), str(l.get("side", "")).lower()
            if ex not in EXCHANGES or side not in ("yes", "no") or not l.get("market_id"):
                raise TradeError("Each leg needs an exchange, a market and a side.")
            out.append({"exchange": ex, "market_id": str(l["market_id"]), "side": side})
        if not 2 <= len(out) <= 3:
            raise TradeError("A combo has 2 or 3 legs.")
        if len({l["market_id"] for l in out}) != len(out):
            raise TradeError("A combo's legs must be different markets.")
        return out

    def _contracts(self, legs):
        """Each leg's contract, all on the same game's quantity, and what they're guaranteed to pay together.
        A market matched in several pairs has a contract per pair; the combination that pays most is used."""
        sc = self.scanner
        find = getattr(sc, "find_matches", None) or (lambda ex, mid: [c for c in [sc.find_contract(ex, mid)] if c])
        options = [find(l["exchange"], l["market_id"]) for l in legs]
        if not all(options):
            raise TradeError("A market in this combo is no longer in the scanner's list (it closed, or its match was "
                             "removed). Nothing was traded.")
        best = None
        for cs in itertools.product(*options):
            if len({(c.game_key, c.var) for c in cs}) != 1:
                continue
            pay = guaranteed_payout([(c, l["side"]) for c, l in zip(cs, legs)])
            if best is None or pay > best[1]:
                best = (list(cs), pay)
        if best is None or best[1] <= 0:
            raise TradeError("These legs don't guarantee a payout together. Nothing was traded.")
        return best

    def _read(self, legs, need_info=True):
        """Every leg's book and market details, and the cash each site/shard needs, all at once.
        Returns (books, infos, cash {(exchange, shard): dollars}, checks)."""
        t = self.trader
        info_jobs, infos = {}, [None] * len(legs)
        for i, l in enumerate(legs):
            hit = t._cached_info(l["exchange"], l["market_id"]) if need_info else None
            if hit is None and need_info:
                info_jobs[i] = _IO.submit(t._fetch_info, l["exchange"], t.venues[l["exchange"]], l["market_id"])
            infos[i] = hit
        pm_cash = None
        if any(l["exchange"] == "polymarket" for l in legs) and t._cached_cash("polymarket") is None:
            pm_cash = _IO.submit(t.venues["polymarket"].balance, None)
        books = t._books(legs)
        for i, job in info_jobs.items():
            infos[i] = job.result()
        cash, jobs = {}, {}
        for i, l in enumerate(legs):
            key = (l["exchange"], int((infos[i] or {}).get("shard") or 0) if l["exchange"] == "kalshi" else None)
            if key in cash or key in jobs:
                continue
            if key[0] == "polymarket" and pm_cash is not None:
                jobs[key] = pm_cash
                continue
            hit = t._cached_cash(*key)
            if hit is None:
                jobs[key] = _IO.submit(t.venues[key[0]].balance, key[1])
            else:
                cash[key] = hit
        for key, job in jobs.items():
            cash[key] = job.result()
        checks = {"info_downloaded": len(info_jobs), "cash_downloaded": len(jobs),
                  "books_live": sum(t._live_book(l["exchange"], l["market_id"]) is not None for l in legs)}
        return books, infos, cash, checks

    @staticmethod
    def _shard_key(leg, info):
        return leg["exchange"], int((info or {}).get("shard") or 0) if leg["exchange"] == "kalshi" else None

    def _fit(self, pairs, levels, pay, cap, cash, keys, step, max_n=math.inf):
        """Most whole sets (a multiple of step, at most max_n) profitable on these books within cap and the cash
        on each site/shard; 0 if not even one."""
        full = combos.size(pairs, levels, pay)
        if not full:
            return 0

        def fits(n):
            c = combos.cost_at(pairs, levels, n)
            if any(sum(q for _, q in x["fills"]) + 1e-9 < n for x in c):
                return False
            spend = {}
            for key, x in zip(keys, c):
                spend[key] = spend.get(key, 0.0) + x["cost"] + x["fee"]
            total = sum(spend.values())
            return total <= cap and all(v <= cash[k] + 1e-9 for k, v in spend.items()) and pay * n - total > 0

        n = floor_to(min(full["size"], max_n), step)
        if n > 0 and not fits(n):
            lo, hi = 0, int(n // step)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if fits(mid * step) else (lo, mid - 1)
            n = lo * step
        return n

    def prepare(self, legs, max_invest=None):
        t = self.trader
        if not t.venues:
            raise TradeError("Trading needs both API keys. Add POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY to .env.")
        legs = self._check_legs(legs)
        contracts, pay = self._contracts(legs)
        tl = {"decided": time.time()}
        t.warm()                            # order connections ready while the checks run
        with trading():
            books, infos, cash, checks = self._read(legs)
        tl["checks_done"] = time.time()
        for l, info in zip(legs, infos):
            if not info.get("open"):
                raise TradeError(f"The {NAMES[l['exchange']]} market {l['market_id']} isn't open for trading.")
        pairs = [(c, l["side"]) for c, l in zip(contracts, legs)]
        levels = [b[l["side"]] for b, l in zip(books, legs)]
        cap = min(config.MAX_TRADE_DOLLARS, max_invest or math.inf)
        step = max([i["min_qty"] for i in infos] + [1.0])
        keys = [self._shard_key(l, i) for l, i in zip(legs, infos)]
        if not combos.size(pairs, levels, pay):
            raise TradeError("Not profitable at live prices anymore; the books moved. Nothing was traded.")
        n = self._fit(pairs, levels, pay, cap, cash, keys, step)
        if n <= 0:
            have = ", ".join(f"{NAMES[ex]}{f' shard {sh}' if sh is not None else ''} ${v:.2f}" for (ex, sh), v in cash.items())
            raise TradeError(f"Can't fit even one profitable set within your limits (cap ${cap:.2f}; cash: {have}). "
                             f"Nothing was traded.")
        c = combos.cost_at(pairs, levels, n)
        plan = {"id": uuid.uuid4().hex, "kind": "combo", "created": time.time(), "size": n, "payout": pay, "cap": cap,
                "strategy": "3-way" if len(legs) == 3 else "same-site" if len({l["exchange"] for l in legs}) == 1 else "pair",
                "legs": [{**l, "title": k.title, "limit": x["limit"], "amount": x["cost"], "fee": x["fee"],
                          "fee_coef": k.fee_coef, "min_qty": i["min_qty"], "shard": key[1],
                          "available_at_limit": sum(q for p, q in lv if p <= x["limit"] + 1e-9), "balance": cash[key]}
                         for l, k, x, i, key, lv in zip(legs, contracts, c, infos, keys, levels)],
                "checks": checks, "timeline": tl}
        plan["capital"] = sum(x["cost"] + x["fee"] for x in c)
        plan["expected_profit"] = pay * n - plan["capital"]
        self.plans[plan["id"]] = (plan, infos, contracts)
        return plan

    # ---- execution ---------------------------------------------------------------------------

    def execute(self, plan_id):
        t = self.trader
        with t.lock:                        # one trade at a time, pairs and combos alike
            entry = self.plans.pop(plan_id, None)
            if not entry:
                raise TradeError("That plan was already used or doesn't exist. Press Make trade again.")
            plan, infos, contracts = entry
            if time.time() - plan["created"] > config.TRADE_PLAN_TTL_SECS:
                raise TradeError("That plan expired (prices move fast). Press Make trade again for fresh numbers.")
            with trading():
                res = self._run(plan, infos, contracts)
        plan["timeline"]["done"] = time.time()
        res["timeline"] = {"stages": timeline_stages(plan["timeline"])}
        return res

    def _send(self, plan, orders, kind):
        """{leg index: (qty, limit)} all at once: the first on this thread, the rest on waiting workers.
        Returns {leg index: (Fill or None, error or None)}."""
        legs = plan["legs"]

        def one(i):
            qty, limit = orders[i]
            try:
                return self.trader._timed_buy(plan, kind, legs[i], qty, limit), None
            except Exception as e:
                return None, e
        idx = list(orders)
        jobs = {i: _IO.submit(one, i) for i in idx[1:]}
        out = {idx[0]: one(idx[0])} if idx else {}
        out.update({i: job.result() for i, job in jobs.items()})
        return out

    def _limits(self, plan, infos):
        """Each leg's planned limit, raised by its share of TOGETHER_HEADROOM x the profit (see trader.together_limits)."""
        n, legs = plan["size"], plan["legs"]
        share = max(0.0, plan["payout"] * n - plan["capital"]) * config.TOGETHER_HEADROOM / len(legs)
        return [max(l["limit"], break_even_price(l["exchange"], n, l["amount"] + l["fee"] + share, l["fee_coef"], i["tick"]))
                for l, i in zip(legs, infos)]

    def _run(self, plan, infos, contracts):
        t, legs, pay = self.trader, plan["legs"], plan["payout"]
        log, steps = {"kind": "combo", "plan": plan, "orders": [], "started": engine.now_utc().isoformat()}, []
        qty, amount, fee = [0.0] * len(legs), [0.0] * len(legs), [0.0] * len(legs)

        def record(kind, leg, fill=None, error=None):
            log["orders"].append({"kind": kind, "exchange": leg["exchange"], "market_id": leg["market_id"],
                                  "side": leg["side"], "error": error, "qty": fill.qty if fill else 0,
                                  "amount": fill.amount if fill else 0, "fee": fill.fee if fill else 0,
                                  "order_id": fill.order_id if fill else "", "request": fill.request if fill else None,
                                  "response": fill.response if fill else None})

        def apply(results, kind):
            """Add each order's fill; returns the legs whose site refused the order outright (a 4xx)."""
            unknown, refused = [], set()
            for i, (fill, err) in sorted(results.items()):
                leg, name = legs[i], NAMES[legs[i]["exchange"]]
                if isinstance(err, ApiError):
                    record(kind, leg, error=str(err))
                    steps.append(f"{name}: {leg['side'].upper()} order rejected ({err.detail})")
                    if 400 <= err.status < 500:
                        refused.add(i)
                elif err is not None:
                    record(kind, leg, error=repr(err))
                    unknown.append(f"{name} {leg['market_id']} ({err})")
                else:
                    record(kind, leg, fill)
                    qty[i], amount[i], fee[i] = qty[i] + fill.qty, amount[i] + fill.amount, fee[i] + fill.fee
                    steps.append(f"{name}: bought {fill.qty:g} {leg['side'].upper()} {leg['market_id']} for "
                                 f"${fill.amount:.2f} + ${fill.fee:.2f} fee" + (f" ({kind})" if kind != "combo" else ""))
            if unknown:
                t._write(log, status="unknown_combo")
                got = ", ".join(f"{NAMES[l['exchange']]} {l['market_id']} filled {q:g}" for l, q in zip(legs, qty) if q)
                raise TradeError(f"Couldn't confirm the {' and '.join(unknown)} order. {got + '. ' if got else ''}Check "
                                 f"both accounts before doing anything else; nothing was sold back.")
            return refused

        # 0. A plan that sat in the confirm dialog: re-size it on books read now.
        if time.time() - plan["created"] > config.PLAN_RECHECK_AFTER_SECS:
            try:
                still = self._recheck(plan, infos, contracts, steps)
            except Exception as e:
                t._write(log, status="recheck_failed")
                raise TradeError(f"Couldn't re-check live prices ({e}). Nothing was traded.")
            if not still:
                t._write(log, status="moved")
                return self._result(plan, "moved", steps, qty, amount, fee, 0.0, [None] * len(legs),
                                    "Prices moved after you confirmed and the combo is gone at live prices. Nothing was traded.")

        # 1. every leg at once
        n = plan["size"]
        limits = self._limits(plan, infos)
        refused = apply(self._send(plan, {i: (n, limits[i]) for i in range(len(legs))}, "combo"), "combo")
        if max(qty) <= 0:
            t._write(log, status="no_fill")
            return self._result(plan, "no_fill", steps, qty, amount, fee, 0.0, [None] * len(legs),
                                "No order filled (the prices moved). Nothing was traded.")

        # 2. top up the legs that filled short, never above break-even for a whole set
        step = max([l["min_qty"] for l in legs] + [1.0])
        target = floor_to(max(qty), step)
        for attempt in range(config.SECOND_LEG_RETRIES):
            short = [i for i in range(len(legs)) if qty[i] + 1e-9 < target and i not in refused]
            if not short:
                break
            # cost per share so far (planned, for a leg that got nothing): every set must still pay back its cost,
            # so the short legs share what's left of the payout between them
            ref = [(amount[i] + fee[i]) / qty[i] if qty[i] > 0 else (legs[i]["amount"] + legs[i]["fee"]) / n
                   for i in range(len(legs))]
            slack = pay - sum(ref)
            if slack < 0:
                steps.append("No top-up price can break even any more.")
                break
            orders = {}
            for j in short:
                d = floor_to(target - qty[j], legs[j]["min_qty"])
                lim = break_even_price(legs[j]["exchange"], d, d * (ref[j] + slack / len(short)), legs[j]["fee_coef"],
                                       infos[j]["tick"]) if d > 0 else 0
                if lim > 0:
                    orders[j] = (d, lim)
            if not orders:
                break
            if attempt:
                first = next(iter(orders))
                t._await_liquidity(legs[first], orders[first][1], since=time.time())
            refused |= apply(self._send(plan, orders, f"top-up {attempt + 1}"), f"top-up {attempt + 1}")

        # 3. sell back whatever doesn't make a whole set
        sets = floor_to(min(qty), step)
        sold = self._sell_back(plan, infos, [q - sets for q in qty], record, steps)
        status = "ok" if all(q - sets <= 1e-9 for q in qty) else "partial"
        t._write(log, status=status)
        note = "" if status == "ok" else "Not every leg filled the same number of shares; the extra shares were sold back."
        return self._result(plan, status, steps, qty, amount, fee, sets, sold, note)

    def _sell_back(self, plan, infos, excess, record, steps):
        """Sell each leg's extra shares back into its bids (at most SELLBACK_SLIPPAGE_TICKS below the best).
        Returns each leg's sale Fill (None where nothing was sold)."""
        t, legs = self.trader, plan["legs"]
        idx = [i for i, e in enumerate(excess) if floor_to(e, legs[i]["min_qty"]) > 0]
        out = [None] * len(legs)
        if not idx:
            return out
        try:
            books = t._books([legs[i] for i in idx])
        except Exception as e:
            steps.append(f"Couldn't read the books to sell back the extra shares ({e})")
            return out
        jobs = {}
        for i, lv in zip(idx, books):
            leg = legs[i]
            other = "no" if leg["side"] == YES else "yes"
            bids = [(round(1 - p, 6), q) for p, q in lv[other]]
            n = floor_to(excess[i], leg["min_qty"])
            if not bids:
                steps.append(f"{NAMES[leg['exchange']]}: no buyers to sell {n:g} extra {leg['side'].upper()} back to")
                continue
            tick = infos[i]["tick"](bids[0][0])
            low = max(tick, round(bids[0][0] - config.SELLBACK_SLIPPAGE_TICKS * tick, 6))
            fills = engine._take(bids, n)
            expect = sum(p * q for p, q in fills) / sum(q for _, q in fills)
            jobs[i] = _IO.submit(t.venues[leg["exchange"]].sell, leg["market_id"], leg["side"], n, low, leg["fee_coef"],
                                 expect=expect)
        for i, job in jobs.items():
            leg = legs[i]
            try:
                out[i] = job.result()
                record("sellback", leg, out[i])
                steps.append(f"{NAMES[leg['exchange']]}: sold back {out[i].qty:g} extra {leg['side'].upper()} for "
                             f"${out[i].amount:.2f} − ${out[i].fee:.2f} fee")
            except Exception as e:
                record("sellback", leg, error=repr(e))
                steps.append(f"{NAMES[leg['exchange']]}: sell-back failed ({e})")
        return out

    def _recheck(self, plan, infos, contracts, steps):
        """Re-size a confirmed plan on books and cash read now; never more sets than confirmed. False if none."""
        legs = plan["legs"]
        t = self.trader
        cash_jobs = {}
        for l, i in zip(legs, infos):
            key = self._shard_key(l, i)
            if key not in cash_jobs:
                cash_jobs[key] = _IO.submit(t.venues[key[0]].balance, key[1])
        books = t._books(legs)
        cash = {k: j.result() for k, j in cash_jobs.items()}
        pairs = [(c, l["side"]) for c, l in zip(contracts, legs)]
        levels = [b[l["side"]] for b, l in zip(books, legs)]
        keys = [self._shard_key(l, i) for l, i in zip(legs, infos)]
        step = max([l["min_qty"] for l in legs] + [1.0])
        n = self._fit(pairs, levels, plan["payout"], plan["cap"], cash, keys, step, max_n=plan["size"])
        plan["recheck"] = {"size": n, "after_secs": round(time.time() - plan["created"], 1)}
        if n <= 0:
            return False
        before_n, before = plan["size"], plan["expected_profit"]
        for l, x, key in zip(legs, combos.cost_at(pairs, levels, n), keys):
            l.update(limit=x["limit"], amount=x["cost"], fee=x["fee"], balance=cash[key])
        plan["size"], plan["capital"] = n, sum(l["amount"] + l["fee"] for l in legs)
        plan["expected_profit"] = plan["payout"] * n - plan["capital"]
        if n != before_n or abs(plan["expected_profit"] - before) >= 0.005:
            steps.append(f"Live re-check: {n:g} sets, expected +${plan['expected_profit']:.2f} "
                         f"(you confirmed {before_n:g}, +${before:.2f})")
        return True

    def _result(self, plan, status, steps, qty, amount, fee, sets, sold, note):
        legs, pay = plan["legs"], plan["payout"]
        avg = [(a + f) / q if q else 0.0 for q, a, f in zip(qty, amount, fee)]
        locked = pay * sets - sum(a * sets for a in avg)
        sold_q = [s.qty if s else 0.0 for s in sold]
        sellback = sum(s.amount - s.fee - a * s.qty for s, a in zip(sold, avg) if s)
        left = [max(0.0, q - sets - sq) for q, sq in zip(qty, sold_q)]
        return {"kind": "combo", "status": status, "steps": steps, "note": note, "strategy": plan.get("strategy"),
                "hedged_sets": sets, "hedged_pairs": sets, "payout": pay,
                "legs_filled": [{"exchange": l["exchange"], "market_id": l["market_id"], "side": l["side"],
                                 "shares": q - sq, "paid": round(a * (q - sq), 2)}
                                for l, q, sq, a in zip(legs, qty, sold_q, avg)],
                "locked_profit": round(locked, 2), "sellback_pnl": round(sellback, 2), "net": round(locked + sellback, 2),
                "unhedged_shares": round(sum(left), 4),
                "unhedged": [{"exchange": NAMES[l["exchange"]], "market_id": l["market_id"], "side": l["side"],
                              "shares": round(x, 4)} for l, x in zip(legs, left) if x > 1e-9]}


def record_combo(scanner, result, row=None):
    """After a combo trade: cash marked stale and refreshed, the history and the log."""
    with scanner.lock:
        if scanner.state.get("balances"):
            scanner.state["balances"] = {**scanner.state["balances"], "stale": True}
    threading.Thread(target=scanner.refresh_balances, daemon=True).start()
    ct = getattr(scanner, "combo_trader", None)
    if ct is not None:
        ct.history.appendleft({"time": engine.now_utc().isoformat(), "game": (row or {}).get("game"),
                               "strategy": result.get("strategy"), "status": result["status"],
                               "sets": result["hedged_sets"], "net": result["net"],
                               "unhedged": result["unhedged_shares"]})
    scanner.log(f"Combo trade {result['status']}: {result['hedged_sets']:g} sets, net ${result['net']:.2f}" +
                (f", {result['unhedged_shares']:g} UNHEDGED" if result["unhedged_shares"] else ""))
