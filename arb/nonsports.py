"""Non-sports markets: suggest likely Kalshi <-> Polymarket pairs for you to approve, and turn
approved pairs into contracts the normal scanner, math and trader already handle.

Matching is two-step:
  1. Questions: each Polymarket question (its outcome markets grouped) against Kalshi events,
     scored by rarity-weighted word overlap (TF-IDF cosine) plus shared numbers.
  2. Outcomes within a matched question: candidate names / number ranges paired by label.
Nothing is scanned until you approve a pair (same or opposite meaning).
"""

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .kalshi import KalshiMarket
from .model import Contract
from .polymarket import PMMarket

PM_CATEGORIES = ("politics", "culture", "macro", "finance", "technology", "climate", "crypto",
                 "geopolitics", "science")
MONTHS = {"jan": "january", "feb": "february", "mar": "march", "apr": "april", "jun": "june", "jul": "july",
          "aug": "august", "sep": "september", "sept": "september", "oct": "october", "nov": "november",
          "dec": "december"}
SYNONYMS = {"d": "democratic", "dem": "democratic", "democrat": "democratic", "democrats": "democratic",
            "r": "republican", "rep": "republican", "gop": "republican", "republicans": "republican",
            "govenor": "governor", "gubernatorial": "governor", "pres": "president", "presidential": "president",
            "btc": "bitcoin", "eth": "ethereum", "fomc": "fed", "hikes": "hike", "cuts": "cut", "wins": "win",
            "winner": "win", "senator": "senate", "elections": "election"}
STOP = set("the a an of in on by be to for what which who how is at will does do did with and or from this that "
           "than more less before after end next per its it as are was were has have any get".split())
STATES = {"AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
          "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
          "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
          "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
          "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
          "NH": "newhampshire", "NJ": "newjersey", "NM": "newmexico", "NY": "newyork", "NC": "northcarolina",
          "ND": "northdakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
          "RI": "rhodeisland", "SC": "southcarolina", "SD": "southdakota", "TN": "tennessee", "TX": "texas",
          "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "westvirginia",
          "WI": "wisconsin", "WY": "wyoming", "DC": "districtofcolumbia"}
STATE_NAMES = {v for v in STATES.values()}
MULTIWORD_STATES = {"new hampshire": "newhampshire", "new jersey": "newjersey", "new mexico": "newmexico",
                    "new york": "newyork", "north carolina": "northcarolina", "north dakota": "northdakota",
                    "rhode island": "rhodeisland", "south carolina": "southcarolina", "south dakota": "southdakota",
                    "west virginia": "westvirginia", "district of columbia": "districtofcolumbia"}
# Words that change what a market measures; one side having it and the other not is a red flag.
CONCEPTS = {"margin", "turnout", "percent", "nomination", "primary", "seats", "control", "approval", "runoff",
            "popular", "mention", "say", "price", "score", "rank", "place", "global", "county", "state"}
MONTH_NAMES = set(MONTHS.values()) | {"may"}
OFFICES = {"house", "senate", "governor", "mayor", "president", "parliament", "minister"}
RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[%°$A-Za-z]{0,4}\s*(?:-|–|to)\s*\$?\d")   # "20-25%", "85° to 86°"
THRESHOLD_RE = re.compile(r"\+|≥|≤|>|<|\b(?:or more|or above|or higher|or less|or below|or lower|above|below|"
                          r"at least|at most|over|under|more than|less than)\b", re.I)
MAX_AUTO_PRICE_GAP = 0.25     # sites pricing "the same" outcome 25+ points apart are almost never the same
EVENT_SCORE_MIN = 0.40
OUTCOME_SCORE_MIN = 0.20


def years(text, ticker=""):
    """Years mentioned in a title, plus the 2-digit year Kalshi puts in event tickers
    (KXNOBELPEACE-27, KXHOUSERACE-CA39-26, KXBIGBROTHER-26DEC31)."""
    ys = {int(y) for y in re.findall(r"\b(20[2-4]\d)\b", text or "")}
    m = re.search(r"-(\d{2})(?:[A-Z]{3}(?:\d{2})?)?$", ticker or "")        # -27, -26DEC, -26DEC31
    if m:
        ys.add(2000 + int(m[1]))
    return ys


def _offices(toks):
    s = set(toks) & OFFICES
    if "district" in toks:           # expanded House district codes / "Ohio's 6th District"
        s.add("house")
    return s


DIRECTION_WORDS = {"increase": "up", "increases": "up", "hike": "up", "hikes": "up", "raise": "up", "raises": "up",
                   "rise": "up", "higher": "up", "above": "up", "over": "up", "up": "up",
                   "decrease": "down", "decreases": "down", "cut": "down", "cuts": "down", "lower": "down",
                   "reduce": "down", "fall": "down", "below": "down", "under": "down", "down": "down"}


def _direction(label):
    """'up', 'down', or None: which way a numeric outcome points (hike vs cut, above vs below)."""
    dirs = {DIRECTION_WORDS[w] for w in re.findall(r"[a-z]+", (label or "").lower()) if w in DIRECTION_WORDS}
    return dirs.pop() if len(dirs) == 1 else None


def _month_days(label):
    """{(month, day)} for dates written like 'December 31' / 'Dec 1'."""
    out = set()
    for mon, day in re.findall(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})\b", label or ""):
        key = mon.lower()[:3]
        full = MONTHS.get(key, key)          # "dec"/"december" -> "december"; "may" stays "may"
        if full in MONTH_NAMES:
            out.add((full, int(day)))
    return out


def outcomes_compatible(pm_label, k_label):
    """Numbers alone aren't enough: '25 bps Increase' is not 'Hike >25bps' (shape differs),
    not '25 bps Decrease' (direction differs), and 'By December 31' is not 'Before Dec 1'."""
    dp_, dk_ = _month_days(pm_label), _month_days(k_label)
    if dp_ and dk_ and not dp_ & dk_:
        return False
    if numbers(pm_label) and numbers(k_label):
        if _shape(pm_label) != _shape(k_label):
            return False
        dp, dk = _direction(pm_label), _direction(k_label)
        if dp and dk and dp != dk:
            return False
    return True


def _shape(label):
    """'range' ('20-25%'), 'threshold' ('25+', 'Above $5'), or None (names, dates)."""
    if RANGE_RE.search(label or ""):
        return "range"
    if THRESHOLD_RE.search(label or ""):
        return "threshold"
    return None


def _expand(text):
    """'CA-39' / 'NY25' -> 'california district 39'; multi-word state names -> one token."""
    text = re.sub(r"\b([A-Z]{2})-?(\d{1,2})\b",
                  lambda m: f" {STATES[m[1]]} district {int(m[2])} " if m[1] in STATES else m[0], text or "")
    low = text.lower()
    for k, v in MULTIWORD_STATES.items():
        low = low.replace(k, v)
    return low


def tokens(text):
    out = []
    for t in re.findall(r"[a-z]+|\d+(?:\.\d+)?", _expand(text).replace("&", " and ")):
        if t.isdigit():
            t = str(int(t))                      # "09" -> "9" (district numbers)
        t = MONTHS.get(t, t)
        t = SYNONYMS.get(t, t)
        if t not in STOP and len(t) > 0:
            out.append(t)
    return out


def numbers(text):
    return {float(x) for x in re.findall(r"\d+(?:\.\d+)?", (text or "").replace(",", ""))}


class Tfidf:
    def __init__(self, docs):
        df = Counter(t for d in docs for t in set(d))
        self.n, self.df = len(docs), df
        self.idf = {t: math.log((self.n + 1) / (c + 1)) + 1 for t, c in df.items()}

    def vec(self, toks):
        tf = Counter(toks)
        v = {t: c * self.idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        return {t: x / norm for t, x in v.items()}


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _date(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _q(v):
    try:
        return float((v or {}).get("value"))
    except (TypeError, ValueError):
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- grouping raw API data into questions -------------------------------------------------------

def pm_questions(pm_markets):
    """Group Polymarket outcome markets into questions by (question text, end date)."""
    groups = defaultdict(list)
    for m in pm_markets:
        if m.get("closed") or not m.get("active") or m.get("category") == "sports":
            continue
        groups[(m.get("question") or m["slug"], str(m.get("endDate"))[:10])].append(m)
    out = []
    for (question, end), ms in groups.items():
        out.append({"key": f"{question}|{end}", "question": question, "end": end, "category": ms[0].get("category"),
                    "description": ms[0].get("description") or "", "markets": ms})
    return out


def kalshi_questions(events):
    out = []
    for e in events:
        if e.get("category") == "Sports":
            continue
        ms = [m for m in e.get("markets") or [] if m.get("status") in ("active", "open", None)]
        if ms:
            out.append({"key": e["event_ticker"], "title": e.get("title") or "", "sub_title": e.get("sub_title") or "",
                        "category": e.get("category"), "series": e.get("series_ticker") or e["event_ticker"].split("-")[0],
                        "close": str(ms[0].get("close_time") or "")[:10], "markets": ms})
    return out


def _pm_label(m):
    return m.get("title") or m.get("question") or ""


def _k_label(m):
    return f"{m.get('yes_sub_title') or ''} {m.get('title') or ''}"


def _mid(bid, ask):
    return (bid + ask) / 2 if bid is not None and ask is not None and 0 < bid <= ask < 1 else None


def _hint(pm_mid, k_mid):
    if pm_mid is None or k_mid is None:
        return None
    same, opp = abs(pm_mid - k_mid), abs(pm_mid - (1 - k_mid))
    if same <= 0.08 and opp >= same + 0.1:
        return "same"
    if opp <= 0.08 and same >= opp + 0.1:
        return "opposite"
    return None


# ---- suggestions --------------------------------------------------------------------------------

def suggest(pm_markets, kalshi_events, decided_pairs, rejected_events, max_groups=5000):
    pq, kq = pm_questions(pm_markets), kalshi_questions(kalshi_events)
    pm_docs = [tokens(q["question"]) for q in pq]
    k_docs = [tokens(f"{q['title']} {q['sub_title']}") for q in kq]
    tf = Tfidf(pm_docs + k_docs)
    k_vecs = [tf.vec(d) for d in k_docs]
    index = defaultdict(list)
    rare = max(3, 0.2 * tf.n)                    # only rare-ish words generate candidates
    for i, d in enumerate(k_docs):
        for t in set(d):
            if tf.df[t] <= rare:
                index[t].append(i)

    groups = []
    for qi, q in enumerate(pq):
        v = tf.vec(pm_docs[qi])
        cands = Counter()
        for t in set(pm_docs[qi]):
            for i in index.get(t, ()):
                cands[i] += 1
        scored = []
        p_set = set(pm_docs[qi])
        p_dist = set(re.findall(r"district (\d+)", _expand(q["question"])))
        p_years = years(q["question"])
        if not p_years:
            # No year in the title: Polymarket end dates run ~15 days past the event, so back off 20 days.
            end = _date(q["end"])
            if end:
                p_years = {(end - timedelta(days=20)).year}
        for i, _ in cands.most_common(60):
            k_set = set(k_docs[i])
            k_dist = set(re.findall(r"district (\d+)", _expand(f"{kq[i]['title']} {kq[i]['sub_title']}")))
            if p_dist and k_dist and not p_dist & k_dist:
                continue                                        # PA-01 is never PA-03
            k_years = years(f"{kq[i]['title']} {kq[i]['sub_title']}", kq[i]["key"])
            if p_years and k_years and not p_years & k_years:
                continue                                        # 2026 Nobel is never the 2027 Nobel
            s = cosine(v, k_vecs[i])
            pm_months, k_months = p_set & MONTH_NAMES, k_set & MONTH_NAMES
            if pm_months and k_months and not pm_months & k_months:
                s -= 0.4                                        # September jobs report vs October
            nums_p, nums_k = numbers(_expand(q["question"])), numbers(_expand(f"{kq[i]['title']} {kq[i]['sub_title']}"))
            if nums_p and nums_k:
                overlap = len(nums_p & nums_k) / len(nums_p | nums_k)
                s += 0.1 * overlap if overlap else -0.3         # e.g. district 3 vs district 1
            sp, sk = p_set & STATE_NAMES, k_set & STATE_NAMES
            if sp and sk and not sp & sk:
                s -= 0.4                                        # different states
            elif bool(sp) != bool(sk):
                s -= 0.2                                        # a US state on one side only
            op, ok = _offices(pm_docs[qi]), _offices(k_docs[i])
            if op and ok and not op & ok:
                s -= 0.3                                        # House vs Governor, etc.
            s -= 0.25 * len((p_set ^ k_set) & CONCEPTS)         # "margin" on one side only, etc.
            dp, dk = _date(q["end"]), _date(kq[i]["close"])
            if dp and dk and abs((dp - dk).days) > 400:
                s -= 0.15                         # very different years: probably a different contest
            if s >= EVENT_SCORE_MIN and (q["key"], kq[i]["key"]) not in rejected_events:
                scored.append((s, i))
        ranked = sorted(scored, reverse=True)
        for s, i in ranked[:2]:
            if s < ranked[0][0] - 0.1:
                break                                           # a runner-up must be nearly as good
            g = _pair_outcomes(q, kq[i], s, decided_pairs)
            if g:
                groups.append(g)
    groups.sort(key=lambda g: -g["score"])
    return groups[:max_groups]


def _pair_outcomes(q, k, score, decided):
    pms, kms = q["markets"], k["markets"]
    cand = []
    if len(pms) == 1 and len(kms) == 1:
        if outcomes_compatible(_pm_label(pms[0]), kms[0].get("yes_sub_title") or ""):
            cand = [(score, pms[0], kms[0])]
    else:
        tf = Tfidf([tokens(_pm_label(m)) for m in pms] + [tokens(_k_label(m)) for m in kms])
        kv = [(m, tf.vec(tokens(_k_label(m))), numbers(m.get("yes_sub_title"))) for m in kms]
        for pm in pms:
            pv, pn = tf.vec(tokens(_pm_label(pm))), numbers(_pm_label(pm))
            p_shape = _shape(_pm_label(pm))
            for km, vec, kn in kv:
                k_shape = _shape(km.get("yes_sub_title"))
                if {p_shape, k_shape} == {"range", "threshold"}:
                    continue                    # "20-25%" bucket is not the same market as "25+"
                if not outcomes_compatible(_pm_label(pm), km.get("yes_sub_title") or km.get("title") or ""):
                    continue
                s = cosine(pv, vec)
                if pn and kn:
                    s = 0.5 * s + 0.5 * (len(pn & kn) / len(pn | kn))
                if s >= OUTCOME_SCORE_MIN:
                    cand.append((s, pm, km))
    used_p, used_k, pairs = set(), set(), []
    for s, pm, km in sorted(cand, key=lambda c: -c[0]):
        if pm["slug"] in used_p or km["ticker"] in used_k or (pm["slug"], km["ticker"]) in decided:
            continue
        used_p.add(pm["slug"]); used_k.add(km["ticker"])
        pb, pa = _q(pm.get("bestBidQuote")), _q(pm.get("bestAskQuote"))
        kb, ka = _f(km.get("yes_bid_dollars")), _f(km.get("yes_ask_dollars"))
        pairs.append({"pm": pm["slug"], "pm_label": _pm_label(pm), "pm_bid": pb, "pm_ask": pa,
                      "kalshi": km["ticker"], "k_label": km.get("yes_sub_title") or km.get("title") or km["ticker"],
                      "k_title": km.get("title") or "", "k_bid": kb, "k_ask": ka, "score": round(s, 3),
                      "hint": _hint(_mid(pb, pa), _mid(kb, ka)),
                      "pm_rules": pm.get("description") or "",
                      "k_rules": ((km.get("rules_primary") or "") + "\n\n" + (km.get("rules_secondary") or "")).strip()})
    if not pairs:
        return None
    return {"id": f"{q['key']}||{k['key']}", "score": round(score, 3),
            "pm": {"key": q["key"], "question": q["question"], "category": q["category"], "end": q["end"],
                   "outcomes": len(q["markets"])},
            "kalshi": {"key": k["key"], "title": k["title"], "sub_title": k["sub_title"], "category": k["category"],
                       "close": k["close"], "outcomes": len(k["markets"])},
            "pairs": sorted(pairs, key=lambda p: -p["score"])}


def split_auto(groups, min_event, min_outcome):
    """Split suggestions into (auto-accepted pairs, groups still needing review). Confident
    pairs are accepted as 'same'; low-confidence ones and ones whose prices look mirrored
    (which can't be 'same' as labelled) stay for manual review."""
    auto, review = [], []
    for g in groups:
        keep = []
        for p in g["pairs"]:
            pm_mid, k_mid = _mid(p.get("pm_bid"), p.get("pm_ask")), _mid(p.get("k_bid"), p.get("k_ask"))
            wild = pm_mid is not None and k_mid is not None and abs(pm_mid - k_mid) > MAX_AUTO_PRICE_GAP
            if g["score"] >= min_event and p["score"] >= min_outcome and p["hint"] != "opposite" and not wild:
                auto.append({"pm": p["pm"], "kalshi": p["kalshi"], "relation": "same", "auto": True,
                             "question": g["pm"]["question"], "pm_label": p["pm_label"], "k_label": p["k_label"],
                             "k_title": p["k_title"]})
            else:
                keep.append(p)
        if keep:
            review.append({**g, "pairs": keep})
    return auto, review


# ---- approved pairs -> contracts ------------------------------------------------------------------

def kalshi_market_obj(m, fee_coef):
    ev = m["event_ticker"]
    km = KalshiMarket(
        ticker=m["ticker"], event_ticker=ev, series=ev.split("-")[0], league="", pm_league="", sport="",
        body=ev, date_code="", teams_str="", kind="EVENT", period="", team=None, op=">", line=0.5,
        title=m.get("title") or m["ticker"], name=m.get("yes_sub_title") or "",
        rules=((m.get("rules_primary") or "") + "\n\n" + (m.get("rules_secondary") or "")).strip(),
        close_time=m.get("expected_expiration_time") or m.get("close_time") or "", fee_coef=fee_coef)
    km.yes_ask, km.no_ask = _f(m.get("yes_ask_dollars")), _f(m.get("no_ask_dollars"))
    return km


def pm_market_obj(m, default_coef):
    pm = PMMarket(slug=m["slug"], league=m.get("category") or "", sport="", date="", t1="", t2="", kind="EVENT",
                  period="", team=None, op=">", line=0.5, tie_half=False,
                  title=f"{m.get('question') or ''} — {m.get('title') or ''}".strip(" —"),
                  rules=m.get("description") or "", start_time=m.get("endDate") or "",
                  fee_coef=float(m.get("feeCoefficient") or default_coef), team_names={})
    bid, ask = _q(m.get("bestBidQuote")), _q(m.get("bestAskQuote"))
    pm.yes_ask, pm.no_ask = ask, (round(1 - bid, 4) if bid is not None else None)
    return pm


def approved_contracts(approved, kalshi_markets, pm_markets):
    """approved: store rows; *_markets: id -> market object. Returns (contracts, source)."""
    contracts, source = [], {}
    for a in approved:
        km, pm = kalshi_markets.get(a["kalshi"]), pm_markets.get(a["pm"])
        if not km or not pm:
            continue                               # closed/settled or not open right now
        pid = f"{a['pm']}|{a['kalshi']}"
        var, key = ("event", pid), f"{(pm.league or 'other').upper()}:{pid}"
        label = pm.title if len(pm.title) < 90 else pm.title[:87] + "…"
        k_op = ">"                                  # Kalshi YES = the event
        p_op = ">" if a["relation"] == "same" else "<"
        note = "auto" if a.get("auto") else ""
        contracts.append(Contract("kalshi", km.ticker, key, var, k_op, 0.5, km.title, km.rules, False, km.fee_coef,
                                  km.close_time, False, True, label, note))
        contracts.append(Contract("polymarket", pm.slug, key, var, p_op, 0.5, pm.title, pm.rules, False, pm.fee_coef,
                                  pm.start_time, False, True, label, note))
        source[("kalshi", km.ticker)], source[("polymarket", pm.slug)] = km, pm
    return contracts, source
