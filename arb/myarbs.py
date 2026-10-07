"""Your active arbs: trades you've placed, tracked until they settle. Saved in my_arbs.json
(git-ignored, so updates never touch it). Entries come from Make trade automatically, or from the
dashboard's "Track this arb" form for orders you placed on the sites yourself."""

import json
import math
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config
from .http import LanePool

PATH = config.PROJECT_ROOT / "my_arbs.json"
LIVE_TTL_SECS = 30          # re-check market status and prices at most this often
SALE_GRACE_SECS = 120       # a site's position list can lag its fills: reads this soon after a sale are ignored


def _num(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError, AttributeError):
        return None


def summarize(arb):
    """Numbers for one arb: pairs held on both sides, what they pay whatever happens, and profit.
    Shares held on one side only are counted as worth $0 (worst case)."""
    legs = arb["legs"]
    pairs = min(leg["shares"] for leg in legs) if legs else 0
    paid = sum(leg["paid"] for leg in legs)
    guaranteed = arb.get("payout", 1.0) * pairs
    realized = arb.get("realized") or 0.0       # what selling extra shares made or lost (Balance)
    # Extra shares on one side. Less than one share (Polymarket fills fractions: a buy by dollar amount gets
    # e.g. 12.04 or 8.8) risks under $1 and is evened up on Polymarket: listed as leftover, not unhedged.
    extra = [{"exchange": leg["exchange"], "side": leg["side"], "shares": round(leg["shares"] - pairs, 4)}
             for leg in legs if leg["shares"] - pairs > 1e-6]
    unhedged = [x for x in extra if x["shares"] >= 1 - 1e-6]
    leftover = [x for x in extra if x["shares"] < 1 - 1e-6]
    # Per pair at your real cost: what one share on each side cost, against what the pair pays.
    pair_cost = sum(leg["paid"] / leg["shares"] for leg in legs if leg["shares"] > 0) if pairs else 0.0
    return {"pairs": pairs, "paid": round(paid, 2), "guaranteed": round(guaranteed, 2),
            "profit": round(guaranteed - paid + realized, 2), "roi": (guaranteed - paid + realized) / paid if paid else 0,
            "unhedged": unhedged, "leftover": leftover, "pair_cost": round(pair_cost, 4),
            "pair_edge": round(arb.get("payout", 1.0) - pair_cost, 4) if pairs else 0.0}


def kalshi_status(m):
    """Status and, once settled, the result of a Kalshi market; plus what selling now would get.
    paid: the market has paid out (finalized); pays_yes: what one YES share paid ($1 or $0, or a
    fraction for a void or scalar result); paid_at: when Kalshi settled it."""
    if not m:
        return {"state": "not found"}
    status = m.get("status") or ""
    result = (m.get("result") or "").lower()
    state = "settled" if result in ("yes", "no") else "open" if status in ("active", "open") else "closed"
    from .kalshi import settle_time
    out = {"state": state, "result": result or None, "settles": settle_time(m),
           "bid": {"yes": _num(m.get("yes_bid_dollars")), "no": _num(m.get("no_bid_dollars"))}}
    if status in ("finalized", "settled") and result:
        value = _num(m.get("settlement_value_dollars"))
        out.update(paid=True, pays_yes=value if value is not None else (1.0 if result == "yes" else 0.0),
                   paid_at=m.get("settlement_ts") or None)
    return out


def polymarket_status(m):
    """As kalshi_status. A resolved Polymarket market prices its winning side at 1: the long (YES) side's
    price is what one YES share paid."""
    if not m:
        return {"state": "not found"}
    bid, ask = _num(m.get("bestBidQuote")), _num(m.get("bestAskQuote"))
    state = "open" if m.get("active") and not m.get("closed") else "closed"
    # Selling YES gets the bid; closing a NO (buying YES back) is worth 1 - ask.
    out = {"state": state, "result": None, "settles": m.get("endDate") or m.get("gameStartTime"),
           "bid": {"yes": bid, "no": round(1 - ask, 4) if ask is not None else None},
           "fee_coef": _num(m.get("feeCoefficient"))}
    if m.get("status") == "MARKET_STATUS_RESOLVED":
        long = next((s for s in m.get("marketSides") or [] if s.get("long")), {})
        pays_yes = _num(long.get("price"))
        if pays_yes is not None:
            out.update(state="settled", result="yes" if pays_yes >= 0.5 else "no", paid=True, pays_yes=pays_yes,
                       paid_at=None)
    return out


def leg_payout(leg, st):
    """Dollars a settled leg paid: its shares times what one share of its side paid."""
    yes = st["pays_yes"]
    return leg["shares"] * (yes if leg["side"] == "yes" else 1 - yes)


def pair_key(arb):
    """The two markets an arb holds: one entry per pair, whichever way it was recorded."""
    ids = {l["exchange"]: l["market_id"] for l in arb.get("legs") or []}
    return ids.get("kalshi"), ids.get("polymarket")


def merge_duplicates(items):
    """Collapse entries for the same pair of markets. An entry from your accounts wins (it counts
    every fill); otherwise repeat trades are added together into the earliest entry. What sales of
    their shares made (realized) adds up. Closed and paid-out arbs are history: never merged."""
    groups, order = {}, []
    for a in items:
        k = ("done", a.get("id")) if a.get("closed") or a.get("paid_out") else pair_key(a)
        if k not in groups:
            order.append(k)
        groups.setdefault(k, []).append(a)
    out = []
    for k in order:
        group = groups[k]
        if len(group) == 1:
            out.append(group[0])
            continue
        acct = [a for a in group if a.get("source") == "account"]
        base = dict(acct[0] if acct else min(group, key=lambda a: a.get("created") or ""))
        others = [a for a in group if a["id"] != base["id"]]
        if not acct:                               # separate trades on the same pair: add them up
            legs = {l["exchange"]: dict(l) for l in base["legs"]}
            for a in others:
                for l in a["legs"]:
                    if l["exchange"] in legs and l["side"] == legs[l["exchange"]]["side"]:
                        legs[l["exchange"]]["shares"] += l["shares"]
                        legs[l["exchange"]]["paid"] = round(legs[l["exchange"]]["paid"] + l["paid"], 2)
            base["legs"] = [legs["kalshi"], legs["polymarket"]]
        base["created"] = min(a.get("created") or "" for a in group) or base.get("created")
        base["note"] = base.get("note") or next((a["note"] for a in others if a.get("note")), "")
        if any(a.get("realized") for a in group):
            base["realized"] = round(sum(a.get("realized") or 0.0 for a in group), 2)
        sales = [x for a in group for x in a.get("sales") or []]
        if sales:
            base["sales"] = sales
        out.append(base)
    return out


class MyArbs:
    def __init__(self, path=PATH):
        self.path, self.lock = path, threading.Lock()
        self.items = []
        self._live, self._live_time = {}, 0.0
        self.unpaired = []                          # live positions with no partner on the other site
        self.known_pairs = 0                        # pairs kept from My arbs that the scanner can't match now
        self.sync_state = {"status": "never"}
        self.touched = {}                           # pair -> when a sale changed it (inf while its orders are out)
        self._books = {}                            # (exchange, market id) -> order book, for Worth now
        self.fee_coef = None                        # (exchange, market id) -> taker fee coefficient (the scanner's)
        if path.exists():
            try:
                self.items = json.loads(path.read_text(encoding="utf-8")).get("arbs", [])
            except (OSError, ValueError):
                pass
            merged = merge_duplicates(self.items)
            if len(merged) != len(self.items):      # clean up doubles saved by earlier versions
                self.items = merged
                self._save()
            if self._close_sold_out():
                self._save()

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"arbs": self.items}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def save(self, arb):
        """Add a new arb (no id) or replace an existing one (same id). Returns the stored entry.
        A new manual entry for a pair you already track is refused (edit that one instead); a new
        Make trade fill on a tracked pair is added into the existing entry."""
        legs = [{"exchange": str(l["exchange"]).lower(), "market_id": str(l["market_id"]),
                 "side": str(l["side"]).lower(), "title": str(l.get("title") or ""),
                 "shares": float(l["shares"]), "paid": float(l["paid"])} for l in arb["legs"]]
        if len(legs) != 2 or {l["exchange"] for l in legs} != {"kalshi", "polymarket"}:
            raise ValueError("An arb needs one Kalshi leg and one Polymarket leg")
        if any(l["shares"] < 0 or l["paid"] < 0 for l in legs):
            raise ValueError("Shares and amounts can't be negative")
        with self.lock:
            old = next((a for a in self.items if a["id"] == arb.get("id")), None)
            if old is None and arb.get("source") != "account":
                same = next((a for a in self.items if pair_key(a) == pair_key({"legs": legs})
                             and not a.get("closed") and not a.get("paid_out")), None)
                if same and arb.get("source") != "make_trade":
                    raise ValueError(f'You already track this pair ("{same["game"]}"): use Edit on it in My arbs')
            entry = {"id": arb.get("id") or uuid.uuid4().hex[:12],
                     "created": (old or {}).get("created") or datetime.now(timezone.utc).isoformat(),
                     "source": (old or {}).get("source") or arb.get("source") or "manual",
                     "game": str(arb.get("game") or legs[1]["title"]), "tab": arb.get("tab") or "",
                     "closes": arb.get("closes"), "payout": float(arb.get("payout") or 1.0),
                     "note": str(arb.get("note") or ""), "legs": legs,
                     "edited": bool(arb.get("edited") or (old or {}).get("edited"))}
            for k in ("realized", "sales", "placed", "check"):        # an account sync or edit keeps these
                if (old or {}).get(k) is not None:
                    entry[k] = old[k]
            self.items = merge_duplicates([a for a in self.items if a["id"] != entry["id"]] + [entry])
            self._save()
            self._live_time = 0.0                   # fetch status for the new markets next time
        return entry

    def hold(self, a):
        """A sale's orders are going out for this arb: the position sync leaves the pair alone until release()."""
        with self.lock:
            self.touched[pair_key(a)] = math.inf

    def release(self, a):
        with self.lock:
            self.touched[pair_key(a)] = time.time()

    def _stale(self, key, read_at):
        """Positions read at read_at may not show a sale on this pair yet: it was under way or finished after the
        read began, or finished under SALE_GRACE_SECS ago (the sites' position lists can lag their fills)."""
        t = self.touched.get(key, 0)
        return read_at is not None and (t >= read_at or time.time() - t < SALE_GRACE_SECS)

    def _close(self, a, why, when):
        """Close an arb that holds no pairs any more. What its sales made (Sell) stays with it, and an id from your
        accounts is freed, so the same pair bought again becomes a new arb rather than reopening this one."""
        sales = a.get("sales") or []
        a["closed"] = {"time": when, "why": why}
        if sales:
            a["closed"].update(sold_for=round(sum(x["proceeds"] for x in sales), 2),
                               cost=round(sum(x["cost"] for x in sales), 2), profit=a.get("realized") or 0.0)
        if str(a.get("id", "")).startswith("acct-"):
            a["id"] = f"{a['id']}-closed-{uuid.uuid4().hex[:6]}"

    def _close_sold_out(self):
        """Repairs for arbs saved by earlier versions. Close open arbs whose sales (Sell) left no pairs and at most a
        fraction of a share. Give closed arbs with recorded sales what they sold for (an arb the position check put
        back and then closed again lost it). Sold-out legs carry no fees. Returns how many changed."""
        n, now = 0, datetime.now(timezone.utc).isoformat()
        for a in self.items:
            c, sales = a.get("closed"), a.get("sales") or []
            if c and sales and c.get("sold_for") is None:
                c.update(sold_for=round(sum(x["proceeds"] for x in sales), 2),
                         cost=round(sum(x["cost"] for x in sales), 2), profit=a.get("realized") or 0.0)
                n += 1
            for leg in a.get("legs") or []:
                if leg.get("fees") and leg["shares"] <= 1e-6:
                    leg["fees"] = 0.0
                    n += 1
            legs = a.get("legs") or []
            if (not a.get("closed") and not a.get("paid_out") and a.get("sales") and legs
                    and min(l["shares"] for l in legs) <= 1e-6 and max(l["shares"] for l in legs) < 1 - 1e-6):
                self._close(a, "sold early", now)
                n += 1
        return n

    def sync_from_accounts(self, pairs, kpos, ppos, unpaired, row_info, read_at=None):
        """Upsert an arb for every paired live position. Shares and cost follow the accounts unless
        you edited the entry yourself. row_info(kalshi contract) -> {game, tab, closes}. read_at: when the
        positions were read (pairs sold from since are left for the next read)."""
        for ticker, slug, kc, pc, payout in pairs:
            if self._stale((ticker, slug), read_at):
                continue
            kp, pp = kpos[ticker], ppos[slug]
            arb_id = f"acct-{ticker}-{slug}"
            with self.lock:
                old = next((a for a in self.items if a["id"] == arb_id), None)
            if old and old.get("edited"):
                with self.lock:                     # still drop other entries for this pair
                    self.items = merge_duplicates(self.items)
                    self._save()
                continue
            legs = [{"exchange": "kalshi", "market_id": ticker, "side": kp["side"], "title": kc.title,
                     "shares": kp["shares"], "paid": kp["paid"]},
                    {"exchange": "polymarket", "market_id": slug, "side": pp["side"], "title": pc.title,
                     "shares": pp["shares"], "paid": pp["paid"] if pp["paid"] is not None else 0.0}]
            note = (old or {}).get("note") or (
                "Polymarket cost estimated from the account: check it" if pp.get("paid_estimated") else "")
            self.save({"id": arb_id, "source": "account", "payout": payout, "legs": legs, "note": note,
                       **row_info(kc)})
        self.unpaired, self.known_pairs = self.claim(unpaired)

    def claim(self, unpaired):
        """Positions the scanner couldn't pair right now (usually because one of the markets left its list,
        e.g. that site stopped trading it) that an open tracked arb already pairs: they're still that arb,
        not one-sided. Returns (still unpaired, number of tracked arbs that claimed positions)."""
        have = {(u["exchange"], u["market_id"], u.get("side")) for u in unpaired}
        claimed, n = set(), 0
        with self.lock:
            for a in self.items:
                if a.get("closed"):
                    continue
                keys = [(l["exchange"], l["market_id"], l["side"]) for l in a["legs"]]
                if len(keys) == 2 and all(k in have and k not in claimed for k in keys):
                    claimed.update(keys)
                    n += 1
        return [u for u in unpaired if (u["exchange"], u["market_id"], u.get("side")) not in claimed], n

    def reconcile(self, kpos, ppos, read=("kalshi", "polymarket"), read_at=None):
        """Make every tracked leg hold what your account really holds, read through the sites' APIs.
        Fewer shares live (you sold some) cuts the leg, its cost pro rata; a leg sold out closes the arb.
        More shares live (you bought more, e.g. to hedge the short side by hand) raises the leg to the
        live count, the added shares at the account's average cost. A market held by two tracked arbs
        is only ever cut, since the extra shares can't be told apart. Only legs whose market is known
        to be still open are touched, because a market that settles also makes the position disappear.
        read: the exchanges whose positions were read; read_at: when. Returns the arbs that changed."""
        names = {"kalshi": "Kalshi", "polymarket": "Polymarket"}
        changed = []
        with self.lock:
            open_arbs = [a for a in self.items if not a.get("closed") and not self._stale(pair_key(a), read_at)]
            uses = {}
            for a in open_arbs:
                for leg in a["legs"]:
                    k = (leg["exchange"], leg["market_id"], leg["side"])
                    uses[k] = uses.get(k, 0) + 1
            for a in open_arbs:
                sold, bought = [], []
                for leg in a["legs"]:
                    if leg["exchange"] not in read:
                        continue
                    st = self._live.get((leg["exchange"], leg["market_id"])) or {}
                    if st.get("state") != "open":
                        continue
                    pos = (kpos if leg["exchange"] == "kalshi" else ppos).get(leg["market_id"])
                    live = pos["shares"] if pos and pos.get("side") == leg["side"] else 0.0
                    if live + 1e-6 < leg["shares"]:
                        if leg["shares"] > 0:
                            leg["paid"] = round(leg["paid"] * live / leg["shares"], 2)
                            if leg.get("fees"):
                                leg["fees"] = round(leg["fees"] * live / leg["shares"], 2)
                        sold.append(f"{leg['shares'] - live:g} {names[leg['exchange']]} {leg['side'].upper()}")
                        leg["shares"] = live
                    elif live > leg["shares"] + 1e-6 and uses[(leg["exchange"], leg["market_id"], leg["side"])] == 1:
                        extra = live - leg["shares"]
                        if pos.get("paid") is not None and not pos.get("paid_estimated"):
                            leg["paid"] = round(pos["paid"], 2)            # the account's cost for all of them
                        elif leg["shares"] > 0:
                            leg["paid"] = round(leg["paid"] * live / leg["shares"], 2)
                        bought.append(f"{extra:g} {names[leg['exchange']]} {leg['side'].upper()}")
                        leg["shares"] = live
                    leg["shares_from"] = "account"
                if not sold and not bought:
                    continue
                when = datetime.now(timezone.utc).isoformat()
                if any(leg["shares"] <= 1e-9 for leg in a["legs"]):
                    a["note"] = f"Closed: you sold {' and '.join(sold)}. " + (a.get("note") or "")
                    self._close(a, f"you sold {' and '.join(sold)}", when)
                else:
                    did = "; ".join(x for x in (f"you sold {' and '.join(sold)}" if sold else "",
                                                f"you bought {' and '.join(bought)} more" if bought else "") if x)
                    a["note"] = f"{did[0].upper()}{did[1:]}: shares updated from your account. " + (a.get("note") or "")
                changed.append(a)
            if changed:
                self._save()
        return changed

    def update_cost_basis(self, kpos, ppos, read=("kalshi", "polymarket"), read_at=None):
        """Set each tracked leg's cost to what your account says you really paid (fees included), at the
        account's average cost per share, so profit and ROI use real prices rather than planned ones.
        Legs whose account cost is uncertain (estimated) are left as recorded. Returns the arbs whose
        cost changed."""
        changed, dirty = [], False
        with self.lock:
            for a in self.items:
                if a.get("closed") or self._stale(pair_key(a), read_at):
                    continue
                moved = False
                for leg in a["legs"]:
                    if leg["exchange"] not in read or leg["shares"] <= 0:
                        continue
                    pos = (kpos if leg["exchange"] == "kalshi" else ppos).get(leg["market_id"])
                    if (not pos or pos.get("side") != leg["side"] or not pos.get("shares")
                            or pos.get("paid") is None or pos.get("paid_estimated")):
                        continue
                    real = round(pos["paid"] / pos["shares"] * leg["shares"], 2)
                    if pos.get("fees") is not None:
                        fees = round(pos["fees"] / pos["shares"] * leg["shares"], 2)    # part of `real`
                        if leg.get("fees") != fees:
                            leg["fees"], dirty = fees, True
                    if abs(real - leg["paid"]) >= 0.01:
                        leg.setdefault("paid_recorded", leg["paid"])     # what was recorded at the time
                        leg["paid"] = real
                        moved = True
                    if leg.get("cost_from") != "account":
                        leg["cost_from"] = "account"
                        dirty = True
                if moved:
                    changed.append(a)
            if changed or dirty:
                self._save()
        return changed

    def fill_placed_times(self, first_fill):
        """Set each arb's "placed" time (when you actually traded it) from your first Kalshi fill in its
        market. Arbs found through the position check otherwise only know when the app first saw them.
        first_fill(ticker) -> ISO time or None. Looked up once per arb. Returns how many were set."""
        with self.lock:
            todo = [(a, l["market_id"]) for a in self.items if not a.get("placed")
                    for l in a["legs"] if l["exchange"] == "kalshi"]
        done = 0
        for a, ticker in todo:
            try:
                t = first_fill(ticker)
            except Exception:
                continue
            with self.lock:
                a["placed"] = min(x for x in (t, a.get("created")) if x) if (t or a.get("created")) else None
                done += 1
        if done:
            with self.lock:
                self._save()
        return done

    def verify(self, lookup):
        """Re-check that each tracked arb really is one: from the two markets' rules, the positions must
        pay out whatever happens (the payout per pair is refreshed from that). lookup(exchange,
        market id) -> Contract or None. Stores a["check"] = {"structure": ok | broken | unknown, "why"}.
        Returns the arbs whose check changed."""
        from .model import as_list, best_match
        changed = []
        with self.lock:
            for a in self.items:
                if a.get("closed"):
                    continue
                legs = {l["exchange"]: l for l in a["legs"]}
                kl, pl = legs.get("kalshi"), legs.get("polymarket")
                kcs = as_list(lookup("kalshi", kl["market_id"])) if kl else []
                pcs = as_list(lookup("polymarket", pl["market_id"])) if pl else []
                m = best_match(kcs, pcs, kl["side"], pl["side"]) if kcs and pcs else None
                prev = a.get("check") or {}
                if (not kcs or not pcs) and (prev.get("structure") == "ok" or a.get("source") == "account"):
                    # Matched before (positions from your accounts are only paired through a match), but a
                    # market has left the scanner's list, usually because that site stopped trading it.
                    # The shares and the rules haven't changed, so the earlier check stands.
                    gone = " and ".join(n for n, c in (("Kalshi", kcs), ("Polymarket", pcs)) if not c)
                    check = {"structure": "ok", "why": f"matched when last checked; the {gone} market isn't "
                                                       f"trading right now, so it can't be re-checked",
                             "checked": prev.get("checked")}
                elif not kcs or not pcs:
                    check = {"structure": "unknown", "why": "can't re-check the match: a market is no longer listed"}
                elif not m:
                    check = {"structure": "unknown",
                             "why": "these two markets aren't matched to each other in the scanner right now"}
                else:
                    kc, pc, pay = m
                    if pay <= 0:
                        check = {"structure": "broken",
                                 "why": "these positions don't pay out in every outcome: one result loses both"}
                    else:
                        check = {"structure": "ok", "why": f"pays ${pay:g} per pair whatever happens",
                                 "checked": datetime.now(timezone.utc).isoformat()}
                        a["payout"] = pay
                    # Payout date from the markets' current settle times (the later of the two).
                    from .engine import _parse_time
                    times = [t for t in (_parse_time(kc.close_time), _parse_time(pc.close_time)) if t]
                    if times:
                        closes = max(times).isoformat()
                        if closes != a.get("closes"):
                            a["closes"] = closes
                            changed.append(a) if a not in changed else None
                if (a.get("check") or {}).get("structure") != check["structure"]:
                    changed.append(a)
                a["check"] = check
            if changed:
                self._save()
        return changed

    def apply_balance(self, arb_id, exchange, action, qty, amount, fee):
        """Record a Balance order: a buy adds shares and what they cost to that leg; a sale takes shares
        out at the leg's average cost and keeps what it made or lost over that cost (in `realized`)."""
        names = {"kalshi": "Kalshi", "polymarket": "Polymarket"}
        with self.lock:
            a = next((a for a in self.items if a["id"] == arb_id), None)
            leg = next((l for l in (a or {}).get("legs", []) if l["exchange"] == exchange), None)
            if leg is None or qty <= 0:
                return None
            side = leg["side"].upper()
            if action == "buy":
                leg["shares"], leg["paid"] = round(leg["shares"] + qty, 4), round(leg["paid"] + amount + fee, 2)
                did = f"bought {qty:g} {side} on {names[exchange]} for ${amount + fee:.2f}"
            else:
                cost = leg["paid"] / leg["shares"] * qty if leg["shares"] else 0.0
                leg["shares"], leg["paid"] = round(leg["shares"] - qty, 4), round(max(0.0, leg["paid"] - cost), 2)
                pnl = amount - fee - cost
                a["realized"] = round((a.get("realized") or 0.0) + pnl, 2)
                did = (f"sold {qty:g} extra {side} on {names[exchange]} for ${amount - fee:.2f} "
                       f"({'+' if pnl >= 0 else '-'}${abs(pnl):.2f} vs what they cost)")
            a["note"] = f"Balanced: {did}. " + (a.get("note") or "")
            self._save()
            return a

    def apply_sale(self, arb_id, sold):
        """Record selling an arb's shares early (Sell): sold = {exchange: (shares, amount, fee)}. Each leg loses
        those shares at its average cost; what the sale made over that cost goes to `realized`, and the sale is
        kept in `sales`. An arb with no shares left closes, with what all its sales made."""
        names = {"kalshi": "Kalshi", "polymarket": "Polymarket"}
        with self.lock:
            a = next((a for a in self.items if a["id"] == arb_id), None)
            if a is None or not sold:
                return None
            proceeds = cost = 0.0
            legs = {}
            for ex, (qty, amount, fee) in sold.items():
                leg = next((l for l in a["legs"] if l["exchange"] == ex), None)
                if leg is None or qty <= 0:
                    continue
                c = leg["paid"] / leg["shares"] * qty if leg["shares"] else 0.0
                if leg.get("fees") and leg["shares"]:     # the buy fees are part of `paid`: they go with the shares
                    leg["fees"] = round(leg["fees"] * max(0.0, leg["shares"] - qty) / leg["shares"], 2)
                leg["shares"], leg["paid"] = round(max(0.0, leg["shares"] - qty), 4), round(max(0.0, leg["paid"] - c), 2)
                proceeds, cost = proceeds + amount - fee, cost + c
                legs[ex] = {"shares": qty, "amount": round(amount, 2), "fee": round(fee, 2)}
            gain = proceeds - cost
            now = datetime.now(timezone.utc).isoformat()
            a["realized"] = round((a.get("realized") or 0.0) + gain, 2)
            a.setdefault("sales", []).append({"time": now, "legs": legs, "proceeds": round(proceeds, 2),
                                              "cost": round(cost, 2), "profit": round(gain, 2)})
            did = " and ".join(f"{v['shares']:g} {names[ex]} {next(l for l in a['legs'] if l['exchange'] == ex)['side'].upper()}"
                               for ex, v in legs.items())
            a["note"] = (f"Sold early: {did} for ${proceeds:.2f} after fees ({'+' if gain >= 0 else '-'}${abs(gain):.2f} "
                         f"vs what they cost). " + (a.get("note") or ""))
            self.touched[pair_key(a)] = time.time()
            # No pairs left: closed. A fraction of a share left on one side (no site sells less than its minimum) is
            # listed under Only on one site; a whole share or more stays here, for Balance.
            left = [l for l in a["legs"] if l["shares"] > 1e-6]
            if min(l["shares"] for l in a["legs"]) <= 1e-6 and all(l["shares"] < 1 - 1e-6 for l in left):
                total = sum(x["proceeds"] for x in a["sales"])
                self._close(a, f"sold early for ${total:.2f}" + "".join(
                    f"; {l['shares']:g} {names[l['exchange']]} {l['side'].upper()} left over (a fraction)" for l in left),
                    now)
            self._save()
            return a

    def delete(self, arb_id):
        with self.lock:
            self.items = [a for a in self.items if a["id"] != arb_id]
            self._save()

    def add_from_trade(self, plan, result, row=None):
        """Record what Make trade actually hedged (nothing if no pairs were hedged)."""
        filled = result.get("legs_filled") or {}
        if not result.get("hedged_pairs") or len(filled) != 2:
            return None
        return self.save({"source": "make_trade", "game": (row or {}).get("game"), "tab": (row or {}).get("tab"),
                          "closes": (row or {}).get("closes"), "payout": plan["payout"],
                          "legs": [{**{k: plan["legs"][ex][k] for k in ("exchange", "market_id", "side", "title")},
                                    "shares": filled[ex]["shares"], "paid": filled[ex]["paid"]}
                                   for ex in ("kalshi", "polymarket")]})

    def snapshot(self, kalshi_client, pm_client):
        """Every tracked arb with its numbers and the live state of both markets."""
        with self.lock:
            items = [dict(a) for a in self.items]
        if items and time.time() - self._live_time > LIVE_TTL_SECS:
            self._refresh_live(items, kalshi_client, pm_client)
            with self.lock:                    # copy again: the refresh may have corrected payout dates
                items = [dict(a) for a in self.items]
        return self._rows(items)

    def _refresh_live(self, items, kalshi_client, pm_client):
        """Market status, best bids and settle times for every tracked market, from both sites."""
        tickers = {l["market_id"] for a in items for l in a["legs"] if l["exchange"] == "kalshi"}
        slugs = {l["market_id"] for a in items for l in a["legs"] if l["exchange"] == "polymarket"}
        live = {}
        try:
            for t, m in kalshi_client.markets_by_ticker(tickers).items():
                live[("kalshi", t)] = kalshi_status(m)
            for s, m in pm_client.markets_by_slug(slugs).items():
                live[("polymarket", s)] = polymarket_status(m)
            self._live, self._live_time = live, time.time()
            self._refresh_payout_dates()
            self._record_payouts()
        except Exception as e:                  # keep showing the last known state
            self._live_error = repr(e)
        self._refresh_books(items, kalshi_client, pm_client)

    def _refresh_books(self, items, kalshi_client, pm_client):
        """Both order books of every open arb, so Worth now walks the depth exactly as Sell does: the best
        price alone can hold a fraction of a share, the rest sitting cents lower."""
        want = {(l["exchange"], l["market_id"]) for a in items if not a.get("closed") and not a.get("paid_out")
                for l in a["legs"] if (self._live.get((l["exchange"], l["market_id"])) or {}).get("state") == "open"}
        books = {}
        tickers = [mid for ex, mid in want if ex == "kalshi"]
        slugs = [mid for ex, mid in want if ex == "polymarket"]

        def pm_book(slug):
            try:
                return slug, pm_client.live_levels(slug)
            except Exception:
                return slug, None
        try:
            if tickers:
                books.update({("kalshi", t): lv for t, lv in kalshi_client.books_by_ticker(tickers).items()})
        except Exception:
            pass
        if slugs:
            with LanePool(min(8, len(slugs))) as pool:
                books.update({("polymarket", s): lv for s, lv in pool.map(pm_book, slugs) if lv is not None})
        self._books = books

    def _coef(self, ex, mid):
        """A market's taker fee coefficient: the scanner's (its matched contract, else its Kalshi series), else
        the one Polymarket lists, else the default. The same Sell uses."""
        c = None
        if self.fee_coef:
            try:
                c = self.fee_coef(ex, mid)
            except Exception:
                c = None
        if c is None and ex == "polymarket":
            c = (self._live.get((ex, mid)) or {}).get("fee_coef")
        return c if c is not None else config.KALSHI_TAKER_COEF if ex == "kalshi" else config.POLYMARKET_DEFAULT_COEF

    def _sale_now(self, a):
        """Selling the arb's pairs into the order books now (sellearly.sale_value, as the Sell dialog does), or
        None without both books."""
        from .sellearly import sale_value
        levels = {l["exchange"]: self._books.get((l["exchange"], l["market_id"])) for l in a["legs"]}
        if len(levels) != 2 or None in levels.values() or a.get("closed") or a.get("paid_out"):
            return None
        try:
            return sale_value(a, levels, {l["exchange"]: self._coef(l["exchange"], l["market_id"]) for l in a["legs"]})
        except (ZeroDivisionError, KeyError, ValueError):
            return None

    def _rows(self, items):
        out = []
        for a in items:
            legs = []
            for l in a["legs"]:
                st = self._live.get((l["exchange"], l["market_id"]), {"state": "checking…"})
                bid = (st.get("bid") or {}).get(l["side"])
                legs.append({**l, "state": st["state"], "result": st.get("result"), "settles": st.get("settles"),
                             "worth_now": round(bid * l["shares"], 2) if bid is not None else None})
            s = summarize(a)
            worth = [l["worth_now"] for l in legs]
            v = self._sale_now(a)              # what Sell would do with the books as last read
            sold = a.get("closed") or {}
            if sold.get("cost"):               # sold with Sell: what the sold shares cost, and the sale's return
                s.update(paid=sold["cost"], roi=sold["profit"] / sold["cost"])
            settled = bool(a.get("closed") or a.get("paid_out")) or all(l["state"] in ("settled", "closed") for l in legs)
            # active: still trading; awaiting: markets over, payout not in yet; paid: paid out; sold: you sold out early
            phase = ("paid" if a.get("paid_out") else "sold" if a.get("closed") else "awaiting" if settled else "active")
            out.append({**a, "legs": legs, **s,
                        "worth_now": round(sum(worth), 2) if None not in worth else None,
                        "sell_now": round(v["proceeds"], 2) if v else None,
                        "sell_profit": round(v["profit"], 2) if v else None, "sell_pairs": v["n"] if v else None,
                        # every pair the books take (Sell all → everything), and what holding the sold pairs pays
                        "sell_all_now": round(v["all"]["proceeds"], 2) if v else None,
                        "sell_all_profit": round(v["all"]["profit"], 2) if v else None,
                        "sell_all_pairs": v["all"]["n"] if v else None,
                        "hold_profit_sold": round(float(a.get("payout") or 1.0) * v["n"] - v["cost"], 2) if v else None,
                        "hold_all_profit": round(float(a.get("payout") or 1.0) * v["all"]["n"] - v["all"]["cost"], 2)
                        if v else None,
                        "settled": settled, "phase": phase})
        return out

    def _record_payouts(self):
        """Once both markets of an arb have paid out, save what it actually paid (from each market's
        result) and when, so the Paid out list never depends on the sites still listing the markets.
        Returns the arbs recorded now."""
        done = []
        now = datetime.now(timezone.utc).isoformat()
        with self.lock:
            for a in self.items:
                if a.get("closed") or a.get("paid_out"):
                    continue
                sts = [self._live.get((l["exchange"], l["market_id"])) or {} for l in a["legs"]]
                if not a["legs"] or not all(st.get("paid") for st in sts):
                    continue
                legs = [{"exchange": l["exchange"], "side": l["side"], "result": st.get("result"),
                         "shares": l["shares"], "paid_out": round(leg_payout(l, st), 2)} for l, st in zip(a["legs"], sts)]
                amount = round(sum(l["paid_out"] for l in legs), 2)
                cost = round(sum(l["paid"] for l in a["legs"]), 2)
                times = [st.get("paid_at") for st in sts if st.get("paid_at")]
                # Kalshi says exactly when it settled; Polymarket doesn't, and both settle on the same event.
                a["paid_out"] = {"time": max(times) if times else now, "amount": amount,
                                 "profit": round(amount - cost, 2), "cost": cost, "legs": legs, "recorded": now}
                done.append(a)
            if done:
                self._save()
        return done

    def _refresh_payout_dates(self):
        """Each tracked arb pays out when the later of its two markets settles: take that from the
        markets' live data on both sites (works even if the pair is no longer in the scanner's list)."""
        from .engine import _parse_time
        changed = False
        with self.lock:
            for a in self.items:
                if a.get("closed"):
                    continue
                times = [_parse_time((self._live.get((l["exchange"], l["market_id"])) or {}).get("settles") or "")
                         for l in a["legs"]]
                if not times or not all(times):
                    continue
                closes = max(times).isoformat()
                if closes != a.get("closes"):
                    a["closes"], changed = closes, True
            if changed:
                self._save()

    def state(self):
        return {"unpaired": self.unpaired, "sync": self.sync_state}
