"""Your active arbs: trades you've placed, tracked until they settle. Saved in my_arbs.json
(git-ignored, so updates never touch it). Entries come from Make trade automatically, or from the
dashboard's "Track this arb" form for orders you placed on the sites yourself."""

import json
import threading
import time
import uuid
from datetime import datetime, timezone

from . import config

PATH = config.PROJECT_ROOT / "my_arbs.json"
LIVE_TTL_SECS = 30          # re-check market status and prices at most this often


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
    unhedged = [{"exchange": leg["exchange"], "side": leg["side"], "shares": leg["shares"] - pairs}
                for leg in legs if leg["shares"] - pairs > 1e-9]
    # Per pair at your real cost: what one share on each side cost, against what the pair pays.
    pair_cost = sum(leg["paid"] / leg["shares"] for leg in legs if leg["shares"] > 0) if pairs else 0.0
    return {"pairs": pairs, "paid": round(paid, 2), "guaranteed": round(guaranteed, 2),
            "profit": round(guaranteed - paid, 2), "roi": (guaranteed - paid) / paid if paid else 0,
            "unhedged": unhedged, "pair_cost": round(pair_cost, 4),
            "pair_edge": round(arb.get("payout", 1.0) - pair_cost, 4) if pairs else 0.0}


def kalshi_status(m):
    """Status and, once settled, the result of a Kalshi market; plus what selling now would get."""
    if not m:
        return {"state": "not found"}
    status = m.get("status") or ""
    result = (m.get("result") or "").lower()
    state = "settled" if result in ("yes", "no") else "open" if status in ("active", "open") else "closed"
    from .kalshi import settle_time
    return {"state": state, "result": result or None, "settles": settle_time(m),
            "bid": {"yes": _num(m.get("yes_bid_dollars")), "no": _num(m.get("no_bid_dollars"))}}


def polymarket_status(m):
    if not m:
        return {"state": "not found"}
    bid, ask = _num(m.get("bestBidQuote")), _num(m.get("bestAskQuote"))
    state = "open" if m.get("active") and not m.get("closed") else "closed"
    # Selling YES gets the bid; closing a NO (buying YES back) is worth 1 - ask.
    return {"state": state, "result": None, "settles": m.get("endDate") or m.get("gameStartTime"),
            "bid": {"yes": bid, "no": round(1 - ask, 4) if ask is not None else None}}


def pair_key(arb):
    """The two markets an arb holds: one entry per pair, whichever way it was recorded."""
    ids = {l["exchange"]: l["market_id"] for l in arb.get("legs") or []}
    return ids.get("kalshi"), ids.get("polymarket")


def merge_duplicates(items):
    """Collapse entries for the same pair of markets. An entry from your accounts wins (it counts
    every fill); otherwise repeat trades are added together into the earliest entry."""
    groups, order = {}, []
    for a in items:
        k = pair_key(a)
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
        out.append(base)
    return out


class MyArbs:
    def __init__(self, path=PATH):
        self.path, self.lock = path, threading.Lock()
        self.items = []
        self._live, self._live_time = {}, 0.0
        self.unpaired = []                          # live positions with no partner on the other site
        self.sync_state = {"status": "never"}
        if path.exists():
            try:
                self.items = json.loads(path.read_text(encoding="utf-8")).get("arbs", [])
            except (OSError, ValueError):
                pass
            merged = merge_duplicates(self.items)
            if len(merged) != len(self.items):      # clean up doubles saved by earlier versions
                self.items = merged
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
                same = next((a for a in self.items if pair_key(a) == pair_key({"legs": legs})), None)
                if same and arb.get("source") != "make_trade":
                    raise ValueError(f'You already track this pair ("{same["game"]}"): use Edit on it in My arbs')
            entry = {"id": arb.get("id") or uuid.uuid4().hex[:12],
                     "created": (old or {}).get("created") or datetime.now(timezone.utc).isoformat(),
                     "source": (old or {}).get("source") or arb.get("source") or "manual",
                     "game": str(arb.get("game") or legs[1]["title"]), "tab": arb.get("tab") or "",
                     "closes": arb.get("closes"), "payout": float(arb.get("payout") or 1.0),
                     "note": str(arb.get("note") or ""), "legs": legs,
                     "edited": bool(arb.get("edited") or (old or {}).get("edited"))}
            self.items = merge_duplicates([a for a in self.items if a["id"] != entry["id"]] + [entry])
            self._save()
            self._live_time = 0.0                   # fetch status for the new markets next time
        return entry

    def sync_from_accounts(self, pairs, kpos, ppos, unpaired, row_info):
        """Upsert an arb for every paired live position. Shares and cost follow the accounts unless
        you edited the entry yourself. row_info(kalshi contract) -> {game, tab, closes}."""
        for ticker, slug, kc, pc, payout in pairs:
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
        self.unpaired = unpaired

    def reconcile(self, kpos, ppos, read=("kalshi", "polymarket")):
        """Make every tracked leg hold what your account really holds, read through the sites' APIs.
        Fewer shares live (you sold some) cuts the leg, its cost pro rata; a leg sold out closes the arb.
        More shares live (you bought more, e.g. to hedge the short side by hand) raises the leg to the
        live count, the added shares at the account's average cost. A market held by two tracked arbs
        is only ever cut, since the extra shares can't be told apart. Only legs whose market is known
        to be still open are touched, because a market that settles also makes the position disappear.
        read: the exchanges whose positions were read. Returns the arbs that changed."""
        names = {"kalshi": "Kalshi", "polymarket": "Polymarket"}
        changed = []
        with self.lock:
            open_arbs = [a for a in self.items if not a.get("closed")]
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
                    a["closed"] = {"time": when, "why": f"you sold {' and '.join(sold)}"}
                    a["note"] = f"Closed: you sold {' and '.join(sold)}. " + (a.get("note") or "")
                else:
                    did = "; ".join(x for x in (f"you sold {' and '.join(sold)}" if sold else "",
                                                f"you bought {' and '.join(bought)} more" if bought else "") if x)
                    a["note"] = f"{did[0].upper()}{did[1:]}: shares updated from your account. " + (a.get("note") or "")
                changed.append(a)
            if changed:
                self._save()
        return changed

    def update_cost_basis(self, kpos, ppos, read=("kalshi", "polymarket")):
        """Set each tracked leg's cost to what your account says you really paid (fees included), at the
        account's average cost per share, so profit and ROI use real prices rather than planned ones.
        Legs whose account cost is uncertain (estimated) are left as recorded. Returns the arbs whose
        cost changed."""
        changed, dirty = [], False
        with self.lock:
            for a in self.items:
                if a.get("closed"):
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
                if not kcs or not pcs:
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
                        check = {"structure": "ok", "why": f"pays ${pay:g} per pair whatever happens"}
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
        except Exception as e:                  # keep showing the last known state
            self._live_error = repr(e)

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
            out.append({**a, "legs": legs, **s,
                        "worth_now": round(sum(worth), 2) if None not in worth else None,
                        "settled": bool(a.get("closed")) or all(l["state"] in ("settled", "closed") for l in legs)})
        return out

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
