"""Match Polymarket games to Kalshi games and build comparable contracts."""

import re
import unicodedata
from collections import defaultdict
from datetime import date

from .model import Contract, YES, NO

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def kalshi_date_code(iso):
    d = date.fromisoformat(iso)
    return f"{d.year % 100:02d}{MONTHS[d.month - 1]}{d.day:02d}"


def _tokens(name):
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("st.", "state"))
    toks = name.split()
    return ["state" if t == "st" else t for t in toks]


# Club-name filler that one site writes and the other doesn't ("CA Lanús" / "Lanus", "Levallois" /
# "Levallois Basketball", "Gimnasia y Esgrima de La Plata" / "Gimnasia La Plata").
FILLER = {"fc", "cf", "sc", "ac", "ad", "ca", "cs", "csyd", "cd", "club", "de", "del", "da", "do", "y", "e",
          "the", "basketball", "basket", "bc", "afc"}


def _plain_tokens(name):
    """Lowercase words without accents, punctuation or club filler."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch)).lower()
    return [t for t in re.sub(r"[^a-z0-9 ]", " ", name).split() if t not in FILLER]


def _same_word(a, b):
    return a == b or (min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)))


def _words_within(small, big):
    return bool(small) and all(any(_same_word(a, b) for b in big) for a in small)


def name_matches(kalshi_name, pm_names):
    """Kalshi short names ("Los Angeles R", "Jacksonville St.") vs Polymarket full names
    ("Los Angeles Rams"): every Kalshi token must equal the Polymarket token in the same
    position, except the last which may be a prefix. Failing that, one name's words (accents,
    punctuation and club filler ignored) must all be in the other's: "Lanus" / "CA Lanús",
    "Roanne Chorale" / "Roanne". The game matcher still needs both teams of one game to match, each
    to a different team, on the same date, so a loose name can't pair two different games."""
    kt = _tokens(kalshi_name)
    if not kt:
        return False
    for n in pm_names:
        pt = _tokens(n)
        if len(pt) < len(kt):
            continue
        if all(a == b for a, b in zip(kt[:-1], pt)) and pt[len(kt) - 1].startswith(kt[-1]):
            return True
    kw = _plain_tokens(kalshi_name)
    for n in pm_names:
        pw = _plain_tokens(n)
        if _words_within(kw, pw) or _words_within(pw, kw):
            return True
    return False


class KalshiGame:
    def __init__(self, league, body, date_code, teams_str):
        self.league, self.body, self.date_code, self.teams_str = league, body, date_code, teams_str
        self.markets = []
        self.names = {}          # team code -> display name

    @property
    def teams(self):
        return {m.team for m in self.markets if m.team and m.team != "TIE"}


def group_kalshi(kmarkets):
    games = {}
    for m in kmarkets:
        g = games.get((m.league, m.body))
        if g is None:
            g = games[(m.league, m.body)] = KalshiGame(m.league, m.body, m.date_code, m.teams_str)
        g.markets.append(m)
        if m.kind == "GAME" and m.team and m.team != "TIE" and m.period == "FG":
            g.names[m.team] = m.name
    return games


def group_pm(pmarkets):
    games = defaultdict(list)
    for m in pmarkets:
        games[(m.league, m.date, m.t1, m.t2)].append(m)
    return games


def match_games(kgames, pgames, league_map):
    """Returns list of (pm_key, pm_markets, kalshi_game, team_map pm->kalshi) and a list
    of unmatched Polymarket game keys that had same-day Kalshi games in the league."""
    by_day = defaultdict(list)
    for g in kgames.values():
        by_day[(g.league, g.date_code)].append(g)

    matches, unmatched = [], []
    for key, pms in pgames.items():
        league, day, t1, t2 = key
        kleague = league_map[league][0]
        cands = by_day.get((kleague, kalshi_date_code(day)), [])
        if not cands:
            continue
        T1, T2 = t1.upper(), t2.upper()
        exact = [g for g in cands if g.teams_str in (T1 + T2, T2 + T1)]
        if len(exact) == 1:
            matches.append((key, pms, exact[0], {t1: T1, t2: T2}))
            continue
        # Fall back to names: map each PM team to a Kalshi code by code or display name.
        pm_names = defaultdict(set)
        for m in pms:
            for ab, ns in m.team_names.items():
                pm_names[ab] |= ns
        found = []
        for g in cands:
            codes = g.teams
            if len(codes) != 2:
                continue
            tm = {}
            for t in (t1, t2):
                hits = [c for c in codes if c == t.upper() or
                        (g.names.get(c) and name_matches(g.names[c], pm_names.get(t, set())))]
                if len(hits) == 1:
                    tm[t] = hits[0]
            if len(tm) == 2 and len(set(tm.values())) == 2:
                found.append((g, tm))
        if len(found) == 1:
            matches.append((key, pms, found[0][0], found[0][1]))
        else:
            unmatched.append(key)
    return matches, unmatched


def _orient(kind, team, op, line, anchor):
    """Express a contract on a team-specific quantity relative to the anchor team.
    margin(X) > t  <=>  margin(anchor) < -t  when X is the other team."""
    if kind in ("GAME", "SPREAD") and team not in (None, "TIE", "draw") and team != anchor:
        flip = {">": "<", "<": ">", "==": "=="}[op]
        return flip, -line
    return op, line


def _var(kind, period, team, anchor):
    if kind in ("GAME", "SPREAD"):
        return ("margin", period, anchor)
    if kind == "TOTAL":
        return ("total", period)
    if kind == "BTTS":
        return ("btts", period)          # 1 if both teams score in the period, else 0
    return ("tt", period, team)


def _no_draw(league, sport, var):
    """Full games that can't finish level: OT/shootouts/extra innings decide them.
    (NFL can tie, NPB/KBO baseball can tie, soccer can draw.)"""
    return var[0] == "margin" and var[1] == "FG" and (
        sport in ("basketball", "hockey") or league in ("MLB", "NCAAF"))


def build_contracts(matches):
    """Turn matched games into Contract objects keyed for pairing. Returns
    (contracts, source) where source maps contract id -> underlying market object."""
    contracts, source = [], {}
    for key, pms, kg, team_map in matches:
        anchor = sorted(team_map.values())[0]
        game_key = f"{kg.league}:{kg.body}"
        sport = kg.markets[0].sport
        d = date.fromisoformat(key[1])
        label = f"{key[2].upper()} vs {key[3].upper()}, {d:%b} {d.day}"
        for km in kg.markets:
            if km.team == "TIE":
                op, line = "==", 0.0
            else:
                op, line = _orient(km.kind, km.team, km.op, km.line, anchor)
            tie_half = km.kind == "GAME" and km.sport == "football" and km.period == "FG" and km.team != "TIE"
            var = _var(km.kind, km.period, km.team, anchor)
            c = Contract("kalshi", km.ticker, game_key, var, op, line, km.title, km.rules, tie_half,
                         km.fee_coef, km.close_time, _no_draw(kg.league, sport, var), km.kind == "GAME", label)
            contracts.append(c)
            source[("kalshi", km.ticker)] = km
        for pm in pms:
            team = pm.team
            if team not in (None, "draw"):
                team = team_map.get(team)
                if team is None:
                    continue
            if team == "draw":
                op, line = "==", 0.0
            else:
                op, line = _orient(pm.kind, team, pm.op, pm.line, anchor)
            var = _var(pm.kind, pm.period, team, anchor)
            c = Contract("polymarket", pm.slug, game_key, var, op, line, pm.title, pm.rules, pm.tie_half,
                         pm.fee_coef, pm.start_time, _no_draw(kg.league, sport, var), pm.kind == "GAME", label)
            contracts.append(c)
            source[("polymarket", pm.slug)] = pm
    return contracts, source


def sync_quotes(contracts, source):
    for c in contracts:
        m = source[(c.exchange, c.market_id)]
        c.ask = {YES: m.yes_ask, NO: m.no_ask}
