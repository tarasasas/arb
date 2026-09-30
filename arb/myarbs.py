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
    return {"pairs": pairs, "paid": round(paid, 2), "guaranteed": round(guaranteed, 2),
            "profit": round(guaranteed - paid, 2), "roi": (guaranteed - paid) / paid if paid else 0,
            "unhedged": unhedged}


def kalshi_status(m):
    """Status and, once settled, the result of a Kalshi market; plus what selling now would get."""
    if not m:
        return {"state": "not found"}
    status = m.get("status") or ""
    result = (m.get("result") or "").lower()
    state = "settled" if result in ("yes", "no") else "open" if status in ("active", "open") else "closed"
    return {"state": state, "result": result or None,
            "bid": {"yes": _num(m.get("yes_bid_dollars")), "no": _num(m.get("no_bid_dollars"))}}


def polymarket_status(m):
    if not m:
        return {"state": "not found"}
    bid, ask = _num(m.get("bestBidQuote")), _num(m.get("bestAskQuote"))
    state = "open" if m.get("active") and not m.get("closed") else "closed"
    # Selling YES gets the bid; closing a NO (buying YES back) is worth 1 - ask.
    return {"state": state, "result": None,
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
            tickers = {l["market_id"] for a in items for l in a["legs"] if l["exchange"] == "kalshi"}
            slugs = {l["market_id"] for a in items for l in a["legs"] if l["exchange"] == "polymarket"}
            live = {}
            try:
                for t, m in kalshi_client.markets_by_ticker(tickers).items():
                    live[("kalshi", t)] = kalshi_status(m)
                for s, m in pm_client.markets_by_slug(slugs).items():
                    live[("polymarket", s)] = polymarket_status(m)
                self._live, self._live_time = live, time.time()
            except Exception as e:                  # keep showing the last known state
                self._live_error = repr(e)
        out = []
        for a in items:
            legs = []
            for l in a["legs"]:
                st = self._live.get((l["exchange"], l["market_id"]), {"state": "checking…"})
                bid = (st.get("bid") or {}).get(l["side"])
                legs.append({**l, "state": st["state"], "result": st.get("result"),
                             "worth_now": round(bid * l["shares"], 2) if bid is not None else None})
            s = summarize(a)
            worth = [l["worth_now"] for l in legs]
            out.append({**a, "legs": legs, **s,
                        "worth_now": round(sum(worth), 2) if None not in worth else None,
                        "settled": all(l["state"] in ("settled", "closed") for l in legs)})
        return out

    def state(self):
        return {"unpaired": self.unpaired, "sync": self.sync_state}
