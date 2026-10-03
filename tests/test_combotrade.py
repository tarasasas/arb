import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from arb import trader as trader_mod
from arb.combotrade import ComboTrader
from arb.http import ApiError
from arb.model import NO, YES, Contract, total_fee
from arb.trader import Trader, TradeError
from arb.venues import Fill

MARGIN, TOTAL = ("margin", "FG", "ARS"), ("total", "FG")
LOG = Path(tempfile.gettempdir()) / "arb_test_combo_trades.jsonl"


class Venue:
    """Books per market, in cost space per side; IOC buys fill from levels at or below the limit."""

    def __init__(self, name, books, balance=10_000.0, min_qty=1.0, reject=None, broken=None):
        self.name, self.bal, self.min_qty, self.reject, self.broken = name, balance, min_qty, reject, broken
        self.books = {m: {s: list(b.get(s, [])) for s in ("yes", "no")} for m, b in books.items()}
        self.orders, self.refills = [], {}

    def levels(self, mid):
        return {s: [lv for lv in self.books[mid][s] if lv[1] > 0] for s in ("yes", "no")}

    def market_info(self, mid):
        return {"open": True, "tick": lambda _p: 0.01, "min_qty": self.min_qty, "shard": 0}

    def balance(self, shard=None):
        return self.bal

    def buy(self, mid, side, qty, limit, coef, expect=None):
        self.orders.append(("buy", mid, side, qty, round(limit, 4)))
        if self.reject == mid:
            raise ApiError(400, "insufficient balance")
        if self.broken == mid:
            raise TimeoutError("read timed out")
        fills, left, book = [], qty, self.books[mid][side]
        for i, (p, q) in enumerate(book):
            if left <= 1e-9 or p > limit + 1e-9:
                break
            t = min(q, left)
            fills.append((p, t))
            book[i] = (p, q - t)
            left -= t
        if self.refills.get(mid):                     # liquidity that shows up after this order
            book.append(self.refills[mid].pop(0))
            book.sort()
        return Fill(qty=sum(q for _, q in fills), amount=sum(p * q for p, q in fills), fee=total_fee(self.name, fills, coef))

    def sell(self, mid, side, qty, min_price, coef, expect=None):
        self.orders.append(("sell", mid, side, qty, round(min_price, 4)))
        return Fill(qty=qty, amount=qty * min_price, fee=total_fee(self.name, [(min_price, qty)], coef))


def contract(ex, mid, var, op, line):
    return Contract(ex, mid, "EPL:26OCT03ARSCHE", var, op, line, f"{ex} {mid}",
                    fee_coef=0.07 if ex == "kalshi" else 0.0695, winner=var[0] == "margin")


class Scanner:
    def __init__(self, contracts):
        self.by = {(c.exchange, c.market_id): c for c in contracts}
        self.lock, self.state = threading.Lock(), {}

    def find_matches(self, ex, mid):
        c = self.by.get((ex, mid))
        return [c] if c else []

    def log(self, _m):
        pass


THREE = [contract("kalshi", "KHOME", MARGIN, ">", 0.0), contract("kalshi", "KTIE", MARGIN, "==", 0.0),
         contract("polymarket", "pm-away", MARGIN, "<", 0.0)]
THREE_LEGS = [{"exchange": "kalshi", "market_id": "KHOME", "side": "yes"},
              {"exchange": "kalshi", "market_id": "KTIE", "side": "yes"},
              {"exchange": "polymarket", "market_id": "pm-away", "side": "yes"}]


def three(home=((0.40, 100),), tie=((0.30, 100),), away=((0.25, 100),), **kw):
    k = Venue("kalshi", {"KHOME": {"yes": list(home)}, "KTIE": {"yes": list(tie)}}, **kw.get("k", {}))
    p = Venue("polymarket", {"pm-away": {"yes": list(away), "no": [(0.76, 100)]}}, **kw.get("p", {}))
    k.books["KHOME"]["no"] = [(0.61, 100)]
    k.books["KTIE"]["no"] = [(0.71, 100)]
    return ComboTrader(Trader(Scanner(THREE), {"kalshi": k, "polymarket": p})), k, p


@mock.patch.object(trader_mod.config, "TRADES_LOG", LOG)
@mock.patch.object(trader_mod.time, "sleep", lambda _s: None)
class ComboTradeTests(unittest.TestCase):
    def test_three_way_fills_every_result(self):
        ct, k, p = three()
        plan = ct.prepare(THREE_LEGS)
        self.assertEqual((plan["size"], plan["payout"], plan["strategy"]), (100, 1.0, "3-way"))
        res = ct.execute(plan["id"])
        self.assertEqual((res["status"], res["hedged_sets"], res["unhedged_shares"]), ("ok", 100, 0))
        self.assertEqual(sorted(o[1] for o in k.orders + p.orders), ["KHOME", "KTIE", "pm-away"])
        self.assertAlmostEqual(res["net"], round(plan["expected_profit"], 2), 2)
        self.assertGreater(res["net"], 0)

    def test_same_site_pair_goes_to_one_exchange(self):
        cs = [contract("kalshi", "O45", TOTAL, ">", 4.5), contract("kalshi", "O55", TOTAL, ">", 5.5)]
        k = Venue("kalshi", {"O45": {"yes": [(0.40, 50)], "no": [(0.62, 50)]}, "O55": {"yes": [(0.52, 50)], "no": [(0.50, 50)]}})
        ct = ComboTrader(Trader(Scanner(cs), {"kalshi": k, "polymarket": Venue("polymarket", {})}))
        plan = ct.prepare([{"exchange": "kalshi", "market_id": "O45", "side": "yes"},
                           {"exchange": "kalshi", "market_id": "O55", "side": "no"}])
        self.assertEqual((plan["strategy"], plan["size"]), ("same-site", 50))
        res = ct.execute(plan["id"])
        self.assertEqual((res["status"], res["hedged_sets"]), ("ok", 50))
        self.assertEqual({(o[1], o[2]) for o in k.orders}, {("O45", "yes"), ("O55", "no")})

    def test_a_short_leg_is_topped_up_within_break_even(self):
        ct, k, p = three(home=((0.39, 100),), tie=((0.30, 60), (0.31, 1000)))    # 100 sets, 40 of them at 0.31
        plan = ct.prepare(THREE_LEGS)
        self.assertEqual(plan["size"], 100)
        k.books["KTIE"]["yes"] = [(0.30, 60)]                 # ...but only 60 are there when the order lands
        k.refills["KTIE"] = [(0.31, 500)]                    # then sellers come back
        res = ct.execute(plan["id"])
        tie_buys = [o for o in k.orders if o[1] == "KTIE"]
        self.assertEqual(len(tie_buys), 2)
        self.assertEqual(tie_buys[1][3], plan["size"] - 60)  # topped up exactly the missing shares
        self.assertEqual((res["status"], res["hedged_sets"]), ("ok", plan["size"]))
        self.assertGreaterEqual(res["net"], -0.01)           # every set still pays back what it cost

    def test_what_cant_be_matched_is_sold_back(self):
        ct, k, p = three(tie=((0.30, 100),))
        plan = ct.prepare(THREE_LEGS)
        k.books["KTIE"]["yes"] = [(0.30, 40)]                 # 40 fill, and nothing near break-even after
        res = ct.execute(plan["id"])
        sells = sorted((o[1], o[3]) for o in k.orders + p.orders if o[0] == "sell")
        self.assertEqual(sells, [("KHOME", 60), ("pm-away", 60)])
        self.assertEqual((res["status"], res["hedged_sets"], res["unhedged_shares"]), ("partial", 40, 0))

    def test_a_refused_leg_isnt_retried(self):
        ct, k, p = three(p={"reject": "pm-away"})
        plan = ct.prepare(THREE_LEGS)
        res = ct.execute(plan["id"])
        self.assertEqual(len([o for o in p.orders if o[0] == "buy"]), 1)
        self.assertEqual(sorted(o[1] for o in k.orders if o[0] == "sell"), ["KHOME", "KTIE"])
        self.assertEqual((res["status"], res["hedged_sets"]), ("partial", 0))
        self.assertTrue(any("rejected" in s for s in res["steps"]))

    def test_an_unconfirmed_order_stops_everything(self):
        ct, k, p = three(p={"broken": "pm-away"})
        plan = ct.prepare(THREE_LEGS)
        with self.assertRaises(TradeError) as e:
            ct.execute(plan["id"])
        self.assertIn("Couldn't confirm", str(e.exception))
        self.assertEqual([o for o in k.orders if o[0] == "sell"], [])     # nothing sold back blindly

    def test_cash_limits_the_size(self):
        ct, k, p = three(k={"balance": 20.0})                  # Kalshi pays home + draw: ~$0.73 a set
        plan = ct.prepare(THREE_LEGS)
        kalshi = sum(l["amount"] + l["fee"] for l in plan["legs"] if l["exchange"] == "kalshi")
        self.assertLessEqual(kalshi, 20.0)
        self.assertLess(plan["size"], 100)

    def test_legs_from_different_games_are_refused(self):
        other = Contract("kalshi", "OTHER", "EPL:26OCT04XXXYYY", MARGIN, "==", 0.0, "x", fee_coef=0.07)
        ct = ComboTrader(Trader(Scanner(THREE[:2] + [other]), {"kalshi": Venue("kalshi", {}), "polymarket": Venue("polymarket", {})}))
        with self.assertRaises(TradeError):
            ct.prepare(THREE_LEGS[:2] + [{"exchange": "kalshi", "market_id": "OTHER", "side": "yes"}])

    def test_gone_at_live_prices_sends_nothing(self):
        ct, k, p = three(home=((0.45, 100),))
        with self.assertRaises(TradeError):
            ct.prepare(THREE_LEGS)
        self.assertEqual(k.orders + p.orders, [])

    def test_confirm_dialog_wait_resizes_never_grows(self):
        ct, k, p = three()
        plan = ct.prepare(THREE_LEGS)
        ct.plans[plan["id"]][0]["created"] -= 5
        k.books["KTIE"]["yes"] = [(0.30, 30), (0.40, 100)]   # only 30 still profitable
        res = ct.execute(plan["id"])
        self.assertEqual((res["status"], res["hedged_sets"]), ("ok", 30))
        self.assertIn("Live re-check", res["steps"][0])

    def test_plan_is_single_use(self):
        ct, k, p = three()
        plan = ct.prepare(THREE_LEGS)
        ct.execute(plan["id"])
        with self.assertRaises(TradeError):
            ct.execute(plan["id"])


if __name__ == "__main__":
    unittest.main()
