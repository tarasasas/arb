"""Sell early: close an arb you hold (My arbs) before it pays out, when selling both legs now makes money.

Held to the end, an arb pays its payout per pair. Before then each leg can be sold back into its site's bids.
What it's worth now: both legs' shares sold into the live order books, each site's fee taken off. Against what
those pairs cost you (fees included) that's the profit if you sell now; against the payout, what selling early
gives up to have the money back now (or gains, when the two sites' prices have crossed the other way).

The preview walks both books and sells the number of pairs that makes the most: deeper bids can pay less than a
pair cost, and those pairs stay held. A sale is offered only when it's a profit. The orders: the thinner book
first, immediate-or-cancel at no less than the previewed price, so a moved book sells fewer shares, never
cheaper (none sold: nothing happened); then the other leg for exactly what sold, at no less than SECOND_LEG_SLIP
under its previewed price and never below where the whole sale breaks even, retried if short. Shares still unsold
on that leg stay in My arbs without their pair, where Balance offers to sell them or buy their pair back.
"""

import time
import uuid

from . import config, engine
from .balance import ArbOrders
from .http import LanePool, priority, trading
from .model import total_fee
from .trader import NAMES, TradeError
from .venues import floor_to

OTHER = {"yes": "no", "no": "yes"}
SECOND_LEG_SLIP = 0.05     # $/share the second leg may sell under its previewed price (never below break-even)
SELL_ALL_TTL_SECS = 120    # a Sell all preview stays usable this long: choosing which to sell takes a while


def bids_for(levels, side):
    """What one share of side sells for, best first: 1 - each ask of the other side."""
    return [(round(1 - p, 6), q) for p, q in levels.get(OTHER[side]) or [] if q]


def sale_value(a, levels, coef, step=1.0):
    """Selling an arb's pairs into the books now (levels and fee coefs by exchange). Returns the sale that makes
    the most: {"n", "legs": {ex: {"qty", "amount", "fee", "limit"}}, "proceeds", "cost", "profit"} with "pairs"
    (held), "pair_cost" and "all" (the same for every pair the books take), or None if a book has no buyers."""
    legs = {l["exchange"]: l for l in a["legs"]}
    pairs = floor_to(min(l["shares"] for l in legs.values()), step)
    if pairs <= 0:
        return None
    pair_cost = sum(l["paid"] / l["shares"] for l in legs.values())
    bids = {ex: bids_for(levels[ex], l["side"]) for ex, l in legs.items()}
    top = floor_to(min([pairs] + [sum(q for _, q in b) for b in bids.values()]), step)
    if top <= 0:
        return None

    def at(n):
        out = {}
        for ex in legs:
            fills = engine._take(bids[ex], n)
            out[ex] = {"qty": n, "amount": sum(p * q for p, q in fills), "fee": total_fee(ex, fills, coef[ex]),
                       "limit": fills[-1][0]}
        proceeds = sum(o["amount"] - o["fee"] for o in out.values())
        return {"n": n, "legs": out, "proceeds": proceeds, "cost": n * pair_cost, "profit": proceeds - n * pair_cost}

    # Within one price level of each book every pair sells for the same, so the best size is where a level ends.
    sizes = {top, step}
    for b in bids.values():
        depth = 0.0
        for _, q in b:
            depth += q
            if floor_to(min(depth, top), step) > 0:
                sizes.add(floor_to(min(depth, top), step))
    tried = [at(n) for n in sorted(sizes)]
    best = max(tried, key=lambda v: v["profit"])
    best.update(pairs=pairs, pair_cost=pair_cost, all=tried[-1])
    return best


def floor_price(ex, need, qty, coef):
    """The lowest whole-cent price at which selling qty shares brings in at least need after ex's fee."""
    x = max(0.01, round(need / qty, 2) if qty else 0.01)
    while x > 0.01 and x * qty - total_fee(ex, [(x, qty)], coef) >= need:
        x = round(x - 0.01, 2)                 # (rounded up from a cent below) step down to the edge
    while x < 0.99 and x * qty - total_fee(ex, [(x, qty)], coef) < need - 1e-9:
        x = round(x + 0.01, 2)
    return x


class EarlySeller(ArbOrders):
    WHAT = "Selling early"

    def _read(self, a):
        """Both legs' market details and order books, read now: (legs, info, levels, coef, step)."""
        if a.get("closed") or a.get("paid_out"):
            raise TradeError("That arb is already closed.")
        legs = {l["exchange"]: l for l in a["legs"]}
        if set(legs) != {"kalshi", "polymarket"}:
            raise TradeError("Selling early needs one Kalshi leg and one Polymarket leg.")
        if min(l["shares"] for l in legs.values()) <= 0:
            raise TradeError("No pairs held: one leg has no shares (Balance sells what's left).")
        venues = self._venues()
        info, levels = {}, {}

        def read(ex):
            v, mid = venues[ex], legs[ex]["market_id"]
            info[ex], levels[ex] = v.market_info(mid), v.levels(mid)
        with priority(), LanePool(2) as pool:
            for job in [pool.submit(read, ex) for ex in legs]:
                job.result()
        coef = {ex: self._coef(ex, legs[ex]["market_id"], info[ex]) for ex in legs}
        return legs, info, levels, coef, max(1.0, *(info[ex]["min_qty"] for ex in legs))

    def _plans(self, a, legs, info, levels, coef, step):
        """(best, everything): selling the pairs that make the most (offered only at a profit), and selling every pair
        the books take whatever it makes (Sell all → everything). Each a plan for _run; best always has why/ok."""
        payout = float(a.get("payout") or 1.0)
        held = min(x["shares"] for x in legs.values())
        base = {"created": time.time(), "arb_id": a["id"], "game": a.get("game"), "closes": a.get("closes"),
                "payout": payout, "ok": False,
                "extra": [{"exchange": NAMES[ex], "side": l["side"], "shares": round(l["shares"] - held, 4)}
                          for ex, l in legs.items() if l["shares"] - held > 0.005]}
        shut = [NAMES[ex] for ex in legs if not info[ex]["open"]]
        v = None if shut else sale_value(a, levels, coef, step)
        if v is None:
            empty = [NAMES[ex] for ex in legs if not bids_for(levels[ex], legs[ex]["side"])]
            why = (f"the {' and '.join(shut)} market isn't open for trading" if shut
                   else f"no buyers on {' or '.join(empty) or 'one site'} right now")
            return {**base, "id": uuid.uuid4().hex, "why": why}, None

        def plan(sale, pick):
            n = sale["n"]
            out = {**base, "id": uuid.uuid4().hex, "pairs": v["pairs"], "n": n, "kept": round(v["pairs"] - n, 4),
                   "pair_cost": round(v["pair_cost"], 4), "proceeds": round(sale["proceeds"], 2),
                   "cost": round(sale["cost"], 2), "profit": round(sale["profit"], 2),
                   "roi": sale["profit"] / sale["cost"] if sale["cost"] else None,
                   "hold_payout": round(payout * n, 2), "hold_profit": round(payout * n - sale["cost"], 2), "pick": pick,
                   "legs": {ex: {"exchange": ex, "market_id": legs[ex]["market_id"], "side": legs[ex]["side"],
                                 "title": legs[ex].get("title"), "qty": n, "limit": o["limit"],
                                 "amount": round(o["amount"], 4), "fee": round(o["fee"], 4), "fee_coef": coef[ex],
                                 "min_qty": info[ex]["min_qty"]} for ex, o in sale["legs"].items()}}
            # the thinner book first: if it doesn't sell, nothing has
            spare = {ex: sum(q for p, q in bids_for(levels[ex], legs[ex]["side"]) if p >= o["limit"] - 1e-9) - n
                     for ex, o in sale["legs"].items()}
            out["first"] = min(spare, key=lambda ex: spare[ex])
            return out
        best, everything = plan(v, "best"), plan(v["all"], "everything")
        best["all"] = {"n": everything["n"], "proceeds": everything["proceeds"], "profit": everything["profit"]}
        if v["profit"] < 0.01:
            best["why"] = (f"selling now would {'lose' if v['profit'] < 0 else 'make'} ${abs(v['profit']):.2f} "
                           f"against what the pairs cost (best bids: " + ", ".join(
                               f"{NAMES[ex]} {legs[ex]['side'].upper()} {o['limit'] * 100:.1f}¢"
                               for ex, o in v["legs"].items()) + ")")
        else:
            best["ok"] = True
        everything["ok"] = True                     # chosen knowing what it makes or loses
        return best, everything

    def preview(self, arb_id):
        a = self._arb(arb_id)
        best, _ = self._plans(a, *self._read(a))
        self._keep(best)
        return best

    def execute(self, plan_id):
        plan = self._take_plan(plan_id, "Sell")
        if not plan.get("ok"):
            raise TradeError(f"Not selling: {plan.get('why') or 'not a profit right now'}.")
        return self._run(plan)

    # ---- Sell all ------------------------------------------------------------------------------------------

    def preview_all(self):
        """Every open arb priced on the live books: what selling the profitable ones would make, and what selling
        everything would make or lose, against what those pairs cost and what holding them pays."""
        mine = self.scanner.my_arbs
        with mine.lock:
            ids = [a["id"] for a in mine.items if not a.get("closed") and not a.get("paid_out")]
        self._venues()

        def one(arb_id):
            try:
                a = self._arb(arb_id)
                best, everything = self._plans(a, *self._read(a))
                return {"arb_id": arb_id, "game": a.get("game"), "best": best, "everything": everything,
                        "why": None if everything else best.get("why")}
            except Exception as e:
                return {"arb_id": arb_id, "game": None, "best": None, "everything": None,
                        "why": getattr(e, "detail", None) or str(e)}
        with LanePool(min(4, max(1, len(ids)))) as pool:
            rows = list(pool.map(one, ids))

        def total(plans):
            return {"arbs": len(plans), "pairs": round(sum(p["n"] for p in plans), 4),
                    "proceeds": round(sum(p["proceeds"] for p in plans), 2), "cost": round(sum(p["cost"] for p in plans), 2),
                    "profit": round(sum(p["profit"] for p in plans), 2),
                    "hold_profit": round(sum(p["hold_profit"] for p in plans), 2)}
        batch = {"id": uuid.uuid4().hex, "created": time.time(), "ttl": SELL_ALL_TTL_SECS, "arbs": rows,
                 "profitable": total([r["best"] for r in rows if r["best"] and r["best"]["ok"]]),
                 "everything": total([r["everything"] for r in rows if r["everything"]]),
                 "unsellable": sum(1 for r in rows if not r["everything"])}
        self._keep(batch)
        return batch

    def execute_all(self, batch_id, which=None, picks=None):
        """Sell the arbs a Sell all preview priced. picks: the ones you chose, [{"arb_id", "sale": "best" (the pairs
        that make money, as its own Sell would) or "everything" (every pair the books take)}]; or which =
        "profitable" / "everything" for all of them. One arb at a time; one failing doesn't stop the rest. A choice
        the preview can't do is refused before anything is sent, and the preview stays usable."""
        with self.lock:
            batch = self.plans.get(batch_id)
        if batch is None:
            self._take_plan(batch_id, "Sell all")          # (raises: used, expired or unknown)
        plans = self._picked(batch, which, picks)
        self._take_plan(batch_id, "Sell all")
        results = []
        for plan in plans:
            try:
                res = self._run(plan)
            except TradeError as e:
                res = {"status": "error", "text": str(e)}
            results.append({"game": plan["game"], **res})
        sold = [r for r in results if r["status"] in ("ok", "partial")]
        proceeds = round(sum(r.get("proceeds") or 0 for r in sold), 2)
        profit = round(sum(r.get("profit") or 0 for r in sold if r["status"] == "ok"), 2)
        bad = len(results) - len([r for r in results if r["status"] == "ok"])
        return {"status": "ok" if not bad else "partial", "results": results, "proceeds": proceeds, "profit": profit,
                "text": f"Sold {len(sold)} of {len(plans)} arbs for ${proceeds:.2f} after fees"
                        + (f": {'+' if profit >= 0 else '-'}${abs(profit):.2f} against what they cost" if sold else "")
                        + (f". {bad} didn't fully sell: see below." if bad else ".")}

    @staticmethod
    def _picked(batch, which, picks):
        rows = {r["arb_id"]: r for r in batch["arbs"]}
        if picks is None:
            if which not in ("profitable", "everything"):
                raise TradeError("Choose which arbs to sell.")
            plans = [r["best"] for r in rows.values() if r["best"] and r["best"]["ok"]] if which == "profitable" \
                else [r["everything"] for r in rows.values() if r["everything"]]
        else:
            plans, seen = [], set()
            for p in picks:
                r = rows.get(str(p.get("arb_id") or ""))
                if r is None:
                    raise TradeError("An arb you chose isn't in this preview any more: press Refresh prices.")
                if r["arb_id"] in seen:
                    continue
                plan = {"best": r["best"], "everything": r["everything"]}.get(p.get("sale"))
                if not plan or not plan.get("ok"):
                    raise TradeError(f"{r['game'] or r['arb_id']}: can't sell that way now "
                                     f"({(r['best'] or {}).get('why') or r.get('why') or 'no price'}).")
                plans.append(plan)
                seen.add(r["arb_id"])
        if not plans:
            raise TradeError("Nothing to sell: choose at least one arb.")
        return plans

    # ---- the orders ----------------------------------------------------------------------------------------

    def _run(self, plan):
        venues = self._venues()
        a_ex = plan["first"]
        b_ex = "polymarket" if a_ex == "kalshi" else "kalshi"
        la, lb = plan["legs"][a_ex], plan["legs"][b_ex]
        mine = self.scanner.my_arbs
        arb = self._arb(plan["arb_id"])
        log = {"kind": "sell_early", "arb_id": plan["arb_id"], "game": plan["game"], "plan": plan, "orders": [],
               "started": engine.now_utc().isoformat()}
        sold, fa, got = {}, None, [0.0, 0.0, 0.0]

        def order(leg, qty, price):
            f = venues[leg["exchange"]].sell(leg["market_id"], leg["side"], qty, price, leg["fee_coef"])
            log["orders"].append({"exchange": leg["exchange"], "side": leg["side"], "qty": qty, "min_price": price,
                                  "filled": f.qty, "amount": f.amount, "fee": f.fee, "order_id": f.order_id})
            return f
        mine.hold(arb)                      # the position sync leaves this pair alone while the orders are out
        try:
            with trading():
                fa = order(la, la["qty"], la["limit"])
                if fa.qty <= 0:
                    log["status"] = "no_fill"
                    return {"status": "no_fill",
                            "text": f"Nothing sold on {NAMES[a_ex]} (the price moved). Nothing changed: press Sell again."}
                sold[a_ex] = (fa.qty, fa.amount, fa.fee)
                # The other leg, for exactly what sold: down to SECOND_LEG_SLIP under its preview price if the book
                # moved, and a sale that makes money never below where it still covers what these pairs cost.
                need = fa.qty * plan["pair_cost"] - (fa.amount - fa.fee)
                even, slip = floor_price(b_ex, need, fa.qty, lb["fee_coef"]), round(lb["limit"] - SECOND_LEG_SLIP, 2)
                floor = max(even, slip) if even <= lb["limit"] + 1e-9 else max(0.01, slip)   # (a sale at a loss)
                for attempt in range(1 + config.SECOND_LEG_RETRIES):
                    left = floor_to(fa.qty - got[0], lb["min_qty"])
                    if left <= 0:
                        break
                    if attempt:
                        time.sleep(config.SECOND_LEG_RETRY_PAUSE)
                    fb = order(lb, left, floor)
                    got = [got[0] + fb.qty, got[1] + fb.amount, got[2] + fb.fee]
                if got[0] > 0:
                    sold[b_ex] = tuple(got)
        except Exception as e:
            log.update(status="error", error=repr(e))
            detail = getattr(e, "detail", None) or str(e)
            done = " and ".join(f"{q:g} on {NAMES[ex]}" for ex, (q, _, _) in sold.items())
            raise TradeError(f"Sell failed on {NAMES[b_ex if sold else a_ex]}: {detail}. "
                             + (f"Sold so far ({done}) is recorded in My arbs; " if sold else "Nothing sold; ")
                             + "check that account.")
        finally:
            if sold:
                mine.apply_sale(plan["arb_id"], sold)
            mine.release(arb)
            log.setdefault("status", "ok" if got[0] >= (fa.qty if fa else 0) - 1e-6 else "partial")
            self._write(log)
        proceeds = sum(x[1] - x[2] for x in sold.values())
        short = round(fa.qty - got[0], 4)
        name_b, side_b = NAMES[b_ex], lb["side"].upper()
        if short < lb["min_qty"] - 1e-9:      # all sold, or a fraction that site can't sell (shown as leftover)
            profit = proceeds - fa.qty * plan["pair_cost"]
            return {"status": "ok", "pairs": fa.qty, "proceeds": round(proceeds, 2), "profit": round(profit, 2),
                    "text": f"Sold {fa.qty:g} pairs for ${proceeds:.2f} after fees: "
                            f"{'+' if profit >= 0 else '-'}${abs(profit):.2f} against what they cost."
                            + (f" {plan['kept']:g} pairs are still held." if plan.get("kept") else "")}
        return {"status": "partial", "pairs": got[0], "proceeds": round(proceeds, 2),
                "text": f"Sold {fa.qty:g} {la['side'].upper()} on {NAMES[a_ex]} but only {got[0]:g} {side_b} on {name_b} "
                        f"(its buyers left). {short:g} {side_b} on {name_b} are now held without their pair: Balance "
                        f"them in My arbs (sell them, or buy their pair back)."}
