"""Balance: even up a tracked arb whose two legs hold different share counts (My arbs).

The extra shares on one side are worth $0 in the worst case. Two ways to fix that, priced on live books
with each site's fees:
  - sell them: the extra shares go back into that site's bids;
  - buy the difference: the same number of the other leg's side on the other site, so the extra shares
    become pairs that pay the arb's payout.
The preview shows both and recommends the one that leaves more money (shares an option can't cover
count as $0). Nothing is sent until you pick one; its order is immediate-or-cancel at no worse than the
previewed price, so it fills less (never at a worse price) if the book moved.
"""

import json
import threading
import time
import uuid

from . import config, engine
from .http import LanePool, priority
from .model import total_fee
from .myarbs import summarize
from .trader import NAMES, TradeError
from .venues import floor_to

PLAN_TTL_SECS = 30
MIN_EXTRA = 0.005          # less than this is rounding, not an imbalance


class ArbOrders:
    """Orders on an arb you hold (My arbs): Balance, and Sell early (sellearly.py)."""
    WHAT = "Balancing"

    def __init__(self, scanner):
        self.scanner, self.plans, self.lock = scanner, {}, threading.Lock()

    def _venues(self):
        venues = getattr(getattr(self.scanner, "trader", None), "venues", None)
        if not venues:
            raise TradeError(f"{self.WHAT} places orders, so it needs both API keys (trading is off).")
        return venues

    def _keep(self, plan):
        with self.lock:
            now = time.time()
            self.plans = {k: p for k, p in self.plans.items() if now - p["created"] < p.get("ttl", PLAN_TTL_SECS)}
            self.plans[plan["id"]] = plan

    def _take_plan(self, plan_id, again):
        with self.lock:
            plan = self.plans.pop(plan_id, None)
        if plan is None:
            raise TradeError(f"That preview was already used or doesn't exist. Press {again} again.")
        if time.time() - plan["created"] > plan.get("ttl", PLAN_TTL_SECS):
            raise TradeError(f"That preview expired (prices move). Press {again} again for fresh numbers.")
        return plan

    def _arb(self, arb_id):
        mine = self.scanner.my_arbs
        with mine.lock:
            a = next((a for a in mine.items if a["id"] == arb_id), None)
            if a is None:
                raise TradeError("That arb isn't in My arbs any more.")
            return json.loads(json.dumps(a))          # a copy: the sync may change it meanwhile

    def _coef(self, ex, mid, info):
        c = self.scanner.find_any_contract(ex, mid) if hasattr(self.scanner, "find_any_contract") else None
        if c is not None:
            return c.fee_coef
        if ex == "polymarket":
            return info.get("fee_coef") or config.POLYMARKET_DEFAULT_COEF
        try:
            return self.scanner.kalshi.series_fee_coefs().get(mid.split("-")[0], config.KALSHI_TAKER_COEF)
        except Exception:
            return config.KALSHI_TAKER_COEF

    @staticmethod
    def _write(log):
        log["finished"] = engine.now_utc().isoformat()
        with open(config.TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log, default=str) + "\n")


class Balancer(ArbOrders):
    def preview(self, arb_id):
        a = self._arb(arb_id)
        legs = a["legs"]
        if len(legs) != 2:
            raise TradeError("Balancing needs one Kalshi leg and one Polymarket leg.")
        big, small = sorted(legs, key=lambda l: -l["shares"])
        extra = round(big["shares"] - small["shares"], 4)
        if extra < MIN_EXTRA:
            raise TradeError("Both legs already hold the same number of shares.")
        venues = self._venues()
        info, levels, cash = {}, {}, {}

        def read(leg):
            ex, v = leg["exchange"], venues[leg["exchange"]]
            info[ex] = v.market_info(leg["market_id"])
            levels[ex] = v.levels(leg["market_id"])
            cash[ex] = v.balance(info[ex].get("shard")) if ex == small["exchange"] else None
        with priority(), LanePool(2) as pool:
            for job in [pool.submit(read, leg) for leg in legs]:
                job.result()
        payout = float(a.get("payout") or 1.0)
        options = {"sell": self._sell_option(big, extra, info, levels),
                   "buy": self._buy_option(small, big, extra, payout, info, levels, cash)}
        ok = [k for k, o in options.items() if o["ok"]]
        best = max(ok, key=lambda k: options[k]["value"]) if ok else None
        plan = {"id": uuid.uuid4().hex, "created": time.time(), "arb_id": arb_id, "game": a.get("game"),
                "extra": extra, "extra_exchange": NAMES[big["exchange"]], "extra_side": big["side"],
                "payout": payout, "options": options, "recommended": best}
        self._keep(plan)
        return plan

    def _sell_option(self, leg, extra, info, levels):
        ex, name, side = leg["exchange"], NAMES[leg["exchange"]], leg["side"]
        coef = self._coef(ex, leg["market_id"], info[ex])
        base = {"action": "sell", "exchange": ex, "market_id": leg["market_id"], "side": side, "fee_coef": coef}
        if not info[ex]["open"]:
            return {**base, "ok": False, "why": f"the {name} market isn't open for trading"}
        qty = floor_to(extra, info[ex]["min_qty"])
        if qty <= 0:
            return {**base, "ok": False, "why": f"{extra:g} is less than {name}'s smallest order ({info[ex]['min_qty']:g})"}
        other = "no" if side == "yes" else "yes"
        bids = [(round(1 - p, 6), q) for p, q in levels[ex][other]]       # what one of these shares sells for
        fills = engine._take(bids, qty)
        got = round(sum(q for _, q in fills), 4)
        if got <= 0:
            return {**base, "ok": False, "why": f"no buyers on {name} right now"}
        amount = sum(p * q for p, q in fills)
        fee = total_fee(ex, fills, coef)
        value = amount - fee
        return {**base, "ok": True, "qty": got, "limit": fills[-1][0], "amount": round(amount, 2), "fee": round(fee, 2),
                "value": round(value, 2), "left": round(extra - got, 4),
                "text": f"Sell the {got:g} extra {side.upper()} on {name} for ${value:.2f} after fees"}


    def _buy_option(self, leg, big, extra, payout, info, levels, cash):
        ex, name, side = leg["exchange"], NAMES[leg["exchange"]], leg["side"]
        coef = self._coef(ex, leg["market_id"], info[ex])
        base = {"action": "buy", "exchange": ex, "market_id": leg["market_id"], "side": side, "fee_coef": coef}
        if not info[ex]["open"]:
            return {**base, "ok": False, "why": f"the {name} market isn't open for trading"}
        qty = floor_to(extra, info[ex]["min_qty"])
        if qty <= 0:
            return {**base, "ok": False, "why": f"{extra:g} is less than {name}'s smallest order ({info[ex]['min_qty']:g})"}
        fills = engine._take(levels[ex][side], qty)
        amount, fee = sum(p * q for p, q in fills), total_fee(ex, fills, coef)
        capped = False
        for _ in range(30):                        # never more than the hard cap per trade (Settings)
            if amount + fee <= config.MAX_TRADE_DOLLARS + 1e-9:
                break
            qty = floor_to(qty * config.MAX_TRADE_DOLLARS / (amount + fee) * 0.98, info[ex]["min_qty"])
            fills = engine._take(levels[ex][side], qty)
            amount, fee, capped = sum(p * q for p, q in fills), total_fee(ex, fills, coef), True
        got = round(sum(q for _, q in fills), 4)
        if got <= 0:
            return {**base, "ok": False, "why": (f"even one share is over your ${config.MAX_TRADE_DOLLARS:g} cap per trade"
                                                 if capped else f"no sellers on {name} right now")}
        if cash.get(ex) is not None and amount + fee > cash[ex] + 1e-9:
            return {**base, "ok": False, "why": f"not enough cash on {name} (${cash[ex]:.2f})"}
        value = payout * got - amount - fee           # the new pairs pay out; this is what's left after buying
        return {**base, "ok": True, "qty": got, "limit": fills[-1][0], "amount": round(amount, 2), "fee": round(fee, 2),
                "value": round(value, 2), "left": round(extra - got, 4),
                "text": (f"Buy {got:g} {side.upper()} on {name} for ${amount + fee:.2f}: with your extra "
                         f"{NAMES[big['exchange']]} shares they make {got:g} more pairs paying ${payout * got:.2f}"
                         + (f" (as many as your ${config.MAX_TRADE_DOLLARS:g} cap per trade allows)" if capped else ""))}

    def execute(self, plan_id, choice):
        plan = self._take_plan(plan_id, "Balance")
        opt = plan["options"].get(choice)
        if not opt or not opt["ok"]:
            raise TradeError("That option isn't available.")
        v = self._venues()[opt["exchange"]]
        name, side = NAMES[opt["exchange"]], opt["side"].upper()
        log = {"kind": "balance", "arb_id": plan["arb_id"], "game": plan["game"], "option": opt,
               "started": engine.now_utc().isoformat()}
        try:
            with priority():
                if opt["action"] == "sell":
                    fill = v.sell(opt["market_id"], opt["side"], opt["qty"], opt["limit"], opt["fee_coef"])
                else:
                    fill = v.buy(opt["market_id"], opt["side"], opt["qty"], opt["limit"], opt["fee_coef"])
        except Exception as e:
            log.update(status="error", error=repr(e))
            self._write(log)
            detail = getattr(e, "detail", None) or str(e)
            raise TradeError(f"{name} order failed: {detail}. Check that account; My arbs updates from it within a minute.")
        log.update(status="ok" if fill.qty else "no_fill", qty=fill.qty, amount=fill.amount, fee=fill.fee,
                   order_id=fill.order_id, response=fill.response)
        self._write(log)
        if fill.qty <= 0:
            return {"status": "no_fill", "text": f"Nothing filled on {name} (the price moved). Nothing changed."}
        self.scanner.my_arbs.apply_balance(plan["arb_id"], opt["exchange"], opt["action"], fill.qty, fill.amount, fill.fee)
        verb = "Sold" if opt["action"] == "sell" else "Bought"
        money = fill.amount - fill.fee if opt["action"] == "sell" else fill.amount + fill.fee
        short = round(opt["qty"] - fill.qty, 4)
        return {"status": "ok" if short <= 0 else "partial", "qty": fill.qty, "amount": round(fill.amount, 2),
                "fee": round(fill.fee, 2),
                "text": f"{verb} {fill.qty:g} {side} on {name} for ${money:.2f}"
                        + (f" ({short:g} didn't fill: the book moved; press Balance again)" if short > 0 else "") + "."}


def needs_balance(arb):
    """True when one leg holds more shares than the other (a Balance button is worth showing)."""
    s = summarize(arb)
    return bool(s["unhedged"] or s["leftover"])
