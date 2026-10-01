"""Background scan loop: refresh catalogs, match games, refresh prices, find arbs."""

import threading
import time
import traceback
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

from . import config, crypto, engine, kalshi, matching, nonsports, warmcache
from .kalshi import KalshiClient
from .matchstore import MatchStore
from .myarbs import MyArbs
from .polymarket import PolymarketClient

DEPTH_LEVELS = 25      # order-book price levels kept per leg for the dashboard


def _cand_key(cand):
    return (cand["k"].market_id, cand["sk"], cand["p"].market_id, cand["sp"])


def _row_key(row):
    k, p = row["legs"]
    return (k["market_id"], k["side"], p["market_id"], p["side"])


class Scanner:
    def __init__(self, log_to_console=True):
        self.kalshi = KalshiClient()
        self.pm = PolymarketClient()
        self.lock = threading.Lock()
        self.logs = deque(maxlen=200)
        self.log_to_console = log_to_console
        self.contracts, self.source, self.groups = [], {}, {}
        self.contract_index = {}
        self.hot_groups = {}            # (game_key, var) -> {(exchange, market_id)} near an arb
        self.catalog_time = 0.0
        self.store = MatchStore()
        self.my_arbs = MyArbs()
        self.accounts = None            # built on first sync (needs the API keys in .env)
        self.streams = {}               # live order-book streams, by exchange (need API keys)
        self.market_groups = {}         # (exchange, market id) -> pair groups it's in
        self.dirty, self.dirty_lock = set(), threading.Lock()
        self.merge_lock = threading.Lock()   # one pass at a time merges into the opportunities list
        from .alerts import Alerter
        self.alerter = Alerter(self.log)
        self.sports_cat, self.pairs_cat = ([], {}), ([], {})
        self.series_fees = {}
        self.fee_overrides = {}         # Kalshi per-event fee overrides (e.g. playoff games)
        self.suggestions, self.suggest_time = [], 0.0
        self.suggest_state = {"status": "waiting"}
        self._pairs_pending = False
        self.auto_pairs = []            # confident non-sports matches scanned without your approval
        self.crypto_cat = ([], {})      # crypto price markets on both sites, grouped by settlement instant
        self.trader, self.trading_status = self._make_trader()
        from .autotrade import AutoTrader
        self.autotrader = AutoTrader(self)  # off until you turn it on in the dashboard
        self.state = {"status": "starting", "opportunities": [], "near_misses": [], "stats": {},
                      "leagues": [], "unmatched": [], "tabs": [], "pair_conflicts": [], "last_catalog": None, "last_prices": None,
                      "scan_seconds": None, "logs": []}

    def _make_trader(self):
        """Trading needs a Kalshi key (already used for reads) and a Polymarket US key."""
        from .trader import Trader
        if not self.kalshi.http.signer:
            return Trader(self, None), "off: add a Kalshi API key"
        if not (config.POLYMARKET_KEY_ID and config.POLYMARKET_SECRET_KEY):
            return Trader(self, None), "off: add a Polymarket US API key"
        from .polymarket_auth import load_signer
        from .venues import KalshiVenue, PolymarketVenue
        try:
            pm_signer = load_signer(config.POLYMARKET_KEY_ID, config.POLYMARKET_SECRET_KEY)
        except Exception as e:
            return Trader(self, None), f"off: Polymarket key couldn't be loaded ({e})"
        venues = {"kalshi": KalshiVenue(self.kalshi), "polymarket": PolymarketVenue(self.pm, pm_signer)}
        return Trader(self, venues), f"on (cap ${config.MAX_TRADE_DOLLARS:.0f} per trade)"

    def find_contract(self, exchange, market_id):
        with self.lock:
            return self.contract_index.get((exchange, market_id))

    def start_message(self):
        self.log(f"Kalshi access: {self.kalshi.auth_info}")
        self.log(f"Trading: {self.trading_status}")
        self.log("Phone alerts: " + (f"{', '.join(self.alerter.channels())} for arbs of ${self.alerter.min_profit:g}+"
                                     if self.alerter.enabled else "off (see Alerts in the README)"))
        with self.lock:
            self.state["stats"]["alerts"] = {"channels": self.alerter.channels(), "min_profit": self.alerter.min_profit}

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append(line)
        if self.log_to_console:
            print(line, flush=True)

    # ---- catalog ----------------------------------------------------------------------

    def refresh_catalog(self):
        t0 = time.time()
        self.log("Loading sports markets from both exchanges...")
        with ThreadPoolExecutor(3) as pool:
            pm_job = pool.submit(self.pm.load_sports_markets, self.log)
            k_job = pool.submit(self.kalshi.load_sports_markets, self.log)
            fee_job = pool.submit(self.kalshi.event_fee_overrides)
            pmarkets, kmarkets = pm_job.result(), k_job.result()
            try:
                self.fee_overrides = fee_job.result()
            except Exception as e:           # keep the last known overrides rather than none
                self.log(f"Couldn't load Kalshi event fee overrides: {e!r}")
        kalshi.apply_fee_overrides(kmarkets, self.fee_overrides, engine.now_utc(), config.CATALOG_REFRESH_SECS + 60)
        self.log(f"Polymarket: {len(pmarkets)} spread/total/winner markets in configured leagues")
        self.log(f"Kalshi: {len(kmarkets)} spread/total/winner markets in configured leagues")

        kgames = matching.group_kalshi(kmarkets)
        pgames = matching.group_pm(pmarkets)
        matches, unmatched = matching.match_games(kgames, pgames, config.LEAGUES)
        contracts, source = matching.build_contracts(matches)
        groups = engine.group_pairs(contracts)
        paired_ids = {(c.exchange, c.market_id) for g in groups.values() for lst in g.values() for c in lst}
        contracts = [c for c in contracts if (c.exchange, c.market_id) in paired_ids]

        pm_games_by_league = Counter(k[0] for k in pgames)
        matched_by_league = Counter(k[0] for k, *_ in matches)
        k_games_by_league = Counter(g.league for g in kgames.values())
        leagues = []
        for pm_code, (k_code, sport) in config.LEAGUES.items():
            if pm_games_by_league[pm_code] or k_games_by_league[k_code]:
                leagues.append({"league": pm_code, "kalshi": k_code, "sport": sport,
                                "pm_games": pm_games_by_league[pm_code], "kalshi_games": k_games_by_league[k_code],
                                "matched": matched_by_league[pm_code]})
        self.sports_cat = (contracts, source)
        with self.lock:
            self.state.update({
                "leagues": sorted(leagues, key=lambda l: -l["matched"]),
                "unmatched": ["{} {} {} vs {}".format(*k) for k in unmatched[:100]],
                "last_catalog": engine.now_utc().isoformat(),
            })
            self.state["stats"].update({"kalshi_access": self.kalshi.auth_info, "trading": self.trading_status,
                                        "pm_markets": len(pmarkets), "kalshi_markets": len(kmarkets),
                                        "matched_games": len(matches)})
        self.refresh_pairs()
        self.catalog_time = time.time()
        self.log(f"Matched {len(matches)} games, {len(groups)} shared quantities, "
                 f"{len(contracts)} sports contracts to watch ({time.time() - t0:.0f}s)")

    # ---- non-sports: your approved pairs + suggestions -----------------------------------------

    def refresh_pairs(self):
        """Rebuild contracts for the non-sports pairs you approved (a few requests), then
        publish them together with the sports contracts."""
        approved = self.store.approved()
        manual = {(a["pm"], a["kalshi"]) for a in approved}
        with self.lock:
            approved += [a for a in self.auto_pairs if (a["pm"], a["kalshi"]) not in manual]
        contracts, source, conflicts = [], {}, []
        if approved:
            if not self.series_fees:
                self.series_fees = self.kalshi.series_fee_coefs()
            raw_k = self.kalshi.markets_by_ticker({a["kalshi"] for a in approved})
            raw_p = self.pm.markets_by_slug({a["pm"] for a in approved})
            kms = {t: nonsports.kalshi_market_obj(m, self.series_fees.get(m["event_ticker"].split("-")[0],
                                                                          config.KALSHI_TAKER_COEF))
                   for t, m in raw_k.items() if m.get("status") in ("active", "open")}
            pms = {s: nonsports.pm_market_obj(m, config.POLYMARKET_DEFAULT_COEF)
                   for s, m in raw_p.items() if m.get("active") and not m.get("closed")}
            kalshi.apply_fee_overrides(kms.values(), self.fee_overrides, engine.now_utc(),
                                       config.CATALOG_REFRESH_SECS + 60)
            contracts, source = nonsports.approved_contracts(approved, kms, pms, conflicts)
            for c in conflicts:
                self.log(f"Not scanning {c['pm']} ↔ {c['kalshi']}: {c['why']}. Remove it under Approved pairs.")
        with self.lock:
            self.state["pair_conflicts"] = conflicts
        self.pairs_cat = (contracts, source)
        self._publish()

    def _publish(self):
        s_contracts, s_source = self.sports_cat
        p_contracts, p_source = self.pairs_cat
        c_contracts, c_source = self.crypto_cat
        contracts = s_contracts + p_contracts + c_contracts
        source = {**s_source, **p_source, **c_source}
        groups = engine.group_pairs(contracts)
        with self.lock:
            self.contracts, self.source, self.groups = contracts, source, groups
            self.contract_index = {(c.exchange, c.market_id): c for c in contracts}
            idx = {}
            for g, by_ex in groups.items():
                for lst in by_ex.values():
                    for c in lst:
                        idx.setdefault((c.exchange, c.market_id), set()).add(g)
            self.market_groups = idx
        for ex, stream in self.streams.items():
            stream.markets = {mid: m for (e, mid), m in source.items() if e == ex}
        with self.lock:
            self.state["stats"].update({"paired_quantities": len(groups), "paired_contracts": len(contracts),
                                        "approved_pairs": len(p_contracts) // 2,
                                        "auto_pairs": sum(1 for c in p_contracts
                                                          if c.note == "auto" and c.exchange == "kalshi")})

    def refresh_crypto(self):
        """Crypto price markets that settle at the same instant on both sites (exact Up/Down twins and
        Kalshi's hourly ladder strikes). Republished only when the set of markets changes."""
        pm_raw = self.pm.raw_markets(("crypto",), self.log)
        k_raw = [m for series in crypto.kalshi_series_for(pm_raw) for m in self.kalshi.open_markets(series)]
        contracts, source = crypto.price_contracts(
            pm_raw, k_raw, lambda series: self.series_fees.get(series, config.KALSHI_TAKER_COEF),
            config.POLYMARKET_DEFAULT_COEF)
        kalshi.apply_fee_overrides([m for (ex, _), m in source.items() if ex == "kalshi"], self.fee_overrides,
                                   engine.now_utc(), config.CATALOG_REFRESH_SECS + 60)
        for c in contracts:
            if c.exchange == "kalshi":
                c.fee_coef = source[("kalshi", c.market_id)].fee_coef
        old_ids = {(c.exchange, c.market_id) for c in self.crypto_cat[0]}
        if {(c.exchange, c.market_id) for c in contracts} != old_ids:
            self.crypto_cat = (contracts, source)
            groups = {c.game_key for c in contracts}
            self.log(f"Crypto: {len(contracts)} price markets across {len(groups)} settlement time(s) on both sites")
            self._publish()

    def sync_positions(self):
        """Live position check: read both accounts, pair positions into arbs in My arbs."""
        from . import accounts
        if self.accounts is None:
            self.accounts = accounts.Accounts(self.kalshi)
        acc = self.accounts
        if len(acc.missing) == 2:
            self.my_arbs.sync_state = {"status": "off", "error": "Add your Kalshi and Polymarket API keys to .env"}
            return
        try:
            kpos, ppos = acc.positions()
        except Exception as e:
            self.my_arbs.sync_state = {"status": "error", "error": repr(e), "time": engine.now_utc().isoformat()}
            self.log(f"Position check failed: {e!r}")
            return
        pairs, unpaired = accounts.pair_positions(kpos, ppos, self.find_contract)
        self.my_arbs.sync_from_accounts(
            pairs, kpos, ppos, unpaired,
            lambda kc: {"game": kc.game_label or kc.game_key.split(":", 1)[-1], "tab": engine.row_tab(kc),
                        "closes": kc.close_time})
        self.my_arbs.sync_state = {"status": "ok", "time": engine.now_utc().isoformat(), "missing": acc.missing,
                                   "positions": len(kpos) + len(ppos), "paired": len(pairs)}

    def refresh_balances(self):
        """Read your cash on both sites (needs the API keys) for sizing opportunities to it."""
        from . import accounts
        if self.accounts is None:
            self.accounts = accounts.Accounts(self.kalshi)
        if len(self.accounts.missing) == 2:
            return
        try:
            bal = self.accounts.balances()
            info = {**bal, "time": engine.now_utc().isoformat(), "missing": self.accounts.missing}
        except Exception as e:
            with self.lock:
                old = dict(self.state.get("balances") or {})
            info = {**old, "error": repr(e)}            # keep the last known amounts
        with self.lock:
            self.state["balances"] = info

    def _balances_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            self.refresh_balances()
            time.sleep(config.BALANCES_REFRESH_SECS)

    def _positions_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            if self.contracts:                   # pairing needs the market catalog loaded
                try:
                    self.sync_positions()
                except Exception as e:
                    self.log(f"Position check error: {e!r}")
            time.sleep(config.POSITIONS_REFRESH_SECS)

    def _crypto_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            try:
                self.refresh_crypto()
            except Exception as e:
                self.log(f"Crypto pairing error: {e!r}")
            time.sleep(config.CRYPTO_REFRESH_SECS)

    def _suggest_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            if time.time() - self.suggest_time > config.SUGGEST_REFRESH_SECS:
                try:
                    self.refresh_suggestions()
                except Exception as e:
                    self.log(f"Suggestion error: {e!r}")
                    traceback.print_exc()
                    time.sleep(60)
                    continue
            time.sleep(5)

    def refresh_suggestions(self):
        t0 = time.time()
        with self.lock:
            self.suggest_state["status"] = "building"
        self.log("Building non-sports match suggestions...")
        with ThreadPoolExecutor(3) as pool:
            j_pm = pool.submit(self.pm.raw_markets, nonsports.PM_CATEGORIES, self.log)
            j_ev = pool.submit(self.kalshi.open_events)
            j_fee = pool.submit(self.kalshi.series_fee_coefs)
            pm_raw, events, self.series_fees = j_pm.result(), j_ev.result(), j_fee.result()
        pairs, rejected_events = self.store.decided()
        groups = nonsports.suggest(pm_raw, events, pairs, rejected_events)
        coverage = nonsports.tab_coverage(pm_raw, events, groups)
        auto = []
        if config.AUTO_ACCEPT_MATCHES:
            auto, groups = nonsports.split_auto(groups, config.AUTO_MIN_EVENT_SCORE, config.AUTO_MIN_OUTCOME_SCORE)
        with self.lock:
            self.suggestions, self.auto_pairs = groups, auto
            self.state["tabs"] = coverage
            self.suggest_state = {"status": "ready", "updated": engine.now_utc().isoformat(),
                                  "pm_markets": len(pm_raw),
                                  "kalshi_markets": sum(len(e.get("markets") or []) for e in events
                                                        if e.get("category") != "Sports")}
        self.suggest_time = time.time()
        self.log(f"Non-sports: {len(auto)} pairs auto-matched and scanned; {sum(len(g['pairs']) for g in groups)} "
                 f"held back for review (prices mirror or far apart, or different data providers) ({time.time() - t0:.0f}s)")
        self.refresh_pairs()

    def matching_snapshot(self, q="", category="", offset=0, limit=20):
        q = q.lower().strip()
        with self.lock:
            groups = self.suggestions
            st = dict(self.suggest_state)
        if category:
            groups = [g for g in groups if g["pm"]["category"] == category]
        if q:
            groups = [g for g in groups if q in (g["pm"]["question"] + " " + g["kalshi"]["title"] + " " +
                                                 " ".join(p["pm_label"] + " " + p["k_label"] for p in g["pairs"])).lower()]
        cats = sorted({g["pm"]["category"] for g in self.suggestions})
        return {**st, "total": len(groups), "groups": groups[offset:offset + limit], "categories": cats,
                "approved": self.store.approved(), "auto_count": len(self.auto_pairs)}

    def decide(self, pm, kalshi, relation, extra=None, pm_event=None, kalshi_event=None):
        if relation == "reject_event":
            self.store.reject_event(pm_event, kalshi_event)
            drop = lambda g: g["pm"]["key"] == pm_event and g["kalshi"]["key"] == kalshi_event
            with self.lock:
                self.suggestions = [g for g in self.suggestions if not drop(g)]
            return
        self.store.decide(pm, kalshi, relation, **(extra or {}))
        with self.lock:                       # your decision replaces any automatic match
            self.auto_pairs = [a for a in self.auto_pairs if (a["pm"], a["kalshi"]) != (pm, kalshi)]
        with self.lock:                       # hide it from the suggestion list right away
            for g in self.suggestions:
                g["pairs"] = [p for p in g["pairs"] if (p["pm"], p["kalshi"]) != (pm, kalshi)]
            self.suggestions = [g for g in self.suggestions if g["pairs"]]
        if relation in ("same", "opposite", "remove", "reject") and not self._pairs_pending:
            self._pairs_pending = True          # batch rapid approvals into one reload
            threading.Thread(target=self._refresh_pairs_safe, daemon=True).start()

    def _refresh_pairs_safe(self):
        time.sleep(1.5)
        self._pairs_pending = False
        try:
            self.refresh_pairs()
            self.log(f"Approved pairs updated: {len(self.pairs_cat[0]) // 2} being scanned")
        except Exception as e:
            self.log(f"Couldn't load approved pairs: {e!r}")

    # ---- prices -----------------------------------------------------------------------

    def _streamed(self):
        """(exchange, market id) pairs whose book is live on a connected stream right now."""
        return {(ex, mid) for ex, s in self.streams.items() if s.connected for mid in list(s.seen)}

    def refresh_prices(self, hot=False, stream_groups=None):
        """Full sweep (hot=False): every watched contract. Hot sweep: only the quantities that
        were within NEAR_MISS_EDGE of an arb on the last full sweep, so it takes ~1-2s.
        stream_groups: re-check just these pair groups on books the live streams already hold."""
        t0 = time.time()
        with self.lock:
            contracts, source, groups = self.contracts, self.source, self.groups
            hot_keys = self.hot_groups
        streamed = self._streamed()
        if stream_groups is not None:
            groups = {g: groups[g] for g in stream_groups if g in groups}
            contracts = [c for g in groups.values() for lst in g.values() for c in lst]
            hot = True
            if not contracts:
                return
        elif hot:
            # Only the markets that appear in near-arb pairs, grouped as before.
            groups = {g: {ex: [c for c in groups[g][ex] if (ex, c.market_id) in hot_keys[g]] for ex in groups[g]}
                      for g in hot_keys if g in groups}
            contracts = [c for g in groups.values() for lst in g.values() for c in lst]
            if not contracts:
                return
        # Streamed markets already have live books; only the rest are polled.
        kms = [source[("kalshi", c.market_id)] for c in contracts
               if c.exchange == "kalshi" and ("kalshi", c.market_id) not in streamed]
        pms = [source[("polymarket", c.market_id)] for c in contracts
               if c.exchange == "polymarket" and ("polymarket", c.market_id) not in streamed]
        if stream_groups is None and (kms or pms):
            with ThreadPoolExecutor(2) as pool:
                jobs = [pool.submit(self.kalshi.refresh_books, kms), pool.submit(self.pm.refresh_quotes, pms)]
                for j in jobs:
                    j.result()
        matching.sync_quotes(contracts, source)

        cands = engine.screen(groups, config.NEAR_MISS_EDGE)
        if not hot:
            hot_map = {}
            for c in cands:
                ids = hot_map.setdefault((c["k"].game_key, c["k"].var), set())
                ids.update({("kalshi", c["k"].market_id), ("polymarket", c["p"].market_id)})
            with self.lock:
                self.hot_groups = hot_map
            self._stream_wanted(cands)
        now = engine.now_utc()
        opportunities, near = [], []
        book_budget = 15 if hot else 60        # Polymarket book fetches per cycle
        fetched, unchecked = set(), set()
        for cand in cands:
            pm_slug = cand["p"].market_id
            if cand["edge"] > 0 and book_budget <= 0 and pm_slug not in fetched:
                unchecked.add(_cand_key(cand))  # out of book fetches: keep its last row rather than drop it
            if cand["edge"] > 0 and (book_budget > 0 or pm_slug in fetched):
                km = source[("kalshi", cand["k"].market_id)]
                pm = source[("polymarket", pm_slug)]
                if pm.slug not in fetched and ("polymarket", pm.slug) in streamed:
                    fetched.add(pm.slug)          # live book from the stream: no fetch needed
                if pm.slug not in fetched:
                    fetched.add(pm.slug)
                    book_budget -= 1
                    try:
                        self.pm.refresh_book(pm)
                    except Exception as e:
                        self.log(f"Book fetch failed for {pm.slug}: {e!r}")
                        pm.levels, pm.yes_ask, pm.no_ask = {}, None, None
                if not pm.levels:
                    continue
                # Re-read top of book after the fresh fetch, and the edge with it: the screen used the
                # quotes from before the fetch, which on a fast market (crypto windows) can be seconds old.
                cand["ap"] = pm.yes_ask if cand["sp"] == "yes" else pm.no_ask
                if cand["ap"] is None:
                    continue
                cand["edge"] = (cand["payout"] - cand["ak"] - cand["ap"]
                                - engine.fee_per_contract(cand["k"].fee_coef, cand["ak"])
                                - engine.fee_per_contract(cand["p"].fee_coef, cand["ap"]))
                levels_k, levels_p = km.levels.get(cand["sk"], []), pm.levels.get(cand["sp"], [])
                sizing = engine.size_opportunity(cand, levels_k, levels_p)
                cand["depth"] = {"kalshi": levels_k[:DEPTH_LEVELS], "polymarket": levels_p[:DEPTH_LEVELS]}
                if sizing and sizing["profit"] >= config.MIN_PROFIT_DOLLARS:
                    opportunities.append(engine.to_row(cand, sizing, now))
                    continue
            if len(near) < config.MAX_NEAR_MISSES and _cand_key(cand) not in unchecked:
                near.append(engine.to_row(cand, None, now))

        maker = self._maker_rows(cands, source, now) if stream_groups is None else None

        # A pass only replaces the rows it actually re-checked. The hot pass covers a few markets and
        # fetches at most 15 books, so without this the table dropped from ~50 rows to 15 between sweeps.
        covered = {(c.exchange, c.market_id) for c in contracts}
        self.merge_lock.acquire()
        with self.lock:
            previous = self.state["opportunities"]
        found = {_row_key(r) for r in opportunities}
        for r in previous:
            key = _row_key(r)
            if key in found or (r.get("trade_until") and r["trade_until"].replace("Z", "+00:00") <= now.isoformat()):
                continue
            if key in unchecked or (hot and not {("kalshi", key[0]), ("polymarket", key[2])} <= covered):
                opportunities.append(r)
        opportunities.sort(key=lambda r: -r["profit"])
        secs = round(time.time() - t0, 1)
        with self.lock:
            self.state.update({"opportunities": opportunities, "last_prices": now.isoformat(), "status": "running",
                               "hot_count": sum(len(v) for v in self.hot_groups.values()),
                               "streams": {ex: s.status() for ex, s in self.streams.items()}})
            if stream_groups is None:
                self.state.update({"near_misses": near, "hot_seconds" if hot else "scan_seconds": secs,
                                   "maker": maker})
            if not hot:
                self.state["last_full"] = now.isoformat()
        self.merge_lock.release()
        self.alerter.check(opportunities)
        self.autotrader.check(opportunities)
        if not hot:
            self.log(f"Full sweep in {secs:.0f}s: {len(opportunities)} opportunities, {len(cands)} pairs within "
                     f"{abs(config.NEAR_MISS_EDGE) * 100:.0f}c of breaking even (re-checked every ~2s until next sweep)")

    def _maker_rows(self, cands, source, now):
        """Near-misses that become profitable with the Polymarket leg resting as a maker order."""
        rows = []
        for cand in cands:
            if cand["edge"] > 0:
                continue                          # already an arb as a taker
            pm = source.get(("polymarket", cand["p"].market_id))
            km = source.get(("kalshi", cand["k"].market_id))
            if not pm or not km:
                continue
            mc = engine.maker_candidate(cand, pm, config.POLYMARKET_MAKER_REBATE, config.MAKER_MAX_SPREAD)
            if not mc or mc["edge"] < config.MAKER_MIN_EDGE:
                continue
            levels_k = km.levels.get(cand["sk"], []) if km.levels else []
            if not levels_k:
                continue
            cap = int(config.MAKER_MAX_CAPITAL / (mc["ak"] + mc["ap"]))
            sizing = engine.size_opportunity(mc, levels_k, [(mc["ap"], cap)])
            if not sizing or sizing["profit"] < config.MIN_PROFIT_DOLLARS:
                continue
            mc["depth"] = {"kalshi": levels_k[:DEPTH_LEVELS], "polymarket": [(mc["ap"], sizing["size"])]}
            row = engine.to_row(mc, sizing, now)
            m = mc["maker"]
            m["hedge_limit"] = engine.hedge_limit(mc["payout"], m["cost"], config.POLYMARKET_MAKER_REBATE,
                                                  cand["k"].fee_coef)
            row["maker"] = m
            row["legs"][1]["maker"] = True
            row["warnings"].insert(0, "MAKER MODE: your Polymarket order rests in the book and only fills when someone "
                                      "takes it, often just as the price moves against you. Buy the Kalshi leg as soon "
                                      "as any shares fill, at no more than the hedge limit, and cancel what's left if "
                                      "Kalshi moves past it.")
            rows.append(row)
        rows.sort(key=lambda r: -r["profit"])
        return rows

    def _stream_wanted(self, cands):
        """Stream the near-arb markets (closest first) plus every non-sports and crypto pair."""
        if not self.streams:
            return
        wanted = {"kalshi": [], "polymarket": []}
        for c in cands:
            wanted["kalshi"].append(c["k"].market_id)
            wanted["polymarket"].append(c["p"].market_id)
        for c in self.pairs_cat[0] + self.crypto_cat[0]:
            wanted[c.exchange].append(c.market_id)
        for ex, stream in self.streams.items():
            ids = list(dict.fromkeys(wanted[ex]))[:config.STREAM_MAX_MARKETS]
            stream.want(ids)

    def on_stream_update(self, exchange, market_id):
        with self.dirty_lock:
            self.dirty.add((exchange, market_id))

    def _stream_loop(self, stop_event):
        """Re-check the pairs a streamed price change touches, within ~0.1s of the change."""
        while not (stop_event and stop_event.is_set()):
            with self.dirty_lock:
                dirty, self.dirty = self.dirty, set()
            if dirty:
                with self.lock:
                    idx = self.market_groups
                gs = set().union(*(idx.get(d, set()) for d in dirty))
                try:
                    if gs:
                        self.refresh_prices(stream_groups=gs)
                except Exception as e:
                    self.log(f"Stream re-check error: {e!r}")
                    time.sleep(1)
            time.sleep(config.STREAM_EVAL_SECS)

    # ---- loop -------------------------------------------------------------------------

    def _catalog_loop(self, stop_event):
        """Market lists reload in the background so price updates never pause for it."""
        while not (stop_event and stop_event.is_set()):
            if time.time() - self.catalog_time > config.CATALOG_REFRESH_SECS:
                try:
                    self.refresh_catalog()
                except Exception as e:
                    self.log(f"Catalog error: {e!r}")
                    traceback.print_exc()
                    time.sleep(20)
                    continue
            time.sleep(2)

    def _loop(self, stop_event, hot):
        """Full sweeps and hot re-checks run in parallel threads, so the near-arb list keeps
        updating every couple of seconds while a full sweep is in progress."""
        while not (stop_event and stop_event.is_set()):
            try:
                if hot and not self.hot_groups:
                    time.sleep(1)
                    continue
                self.refresh_prices(hot=hot)
            except Exception as e:      # keep the dashboard alive through transient API errors
                self.log(f"Error: {e!r}")
                traceback.print_exc()
                with self.lock:
                    self.state["status"] = f"error: {e!r}"
                time.sleep(5)
                continue
            if not hot:
                self.save_warm()
            time.sleep(config.HOT_PAUSE_SECS if hot else config.FULL_SWEEP_SECS)

    # ---- warm start ---------------------------------------------------------------------

    WARM_STATE_KEYS = ("leagues", "unmatched", "tabs")

    def save_warm(self, min_interval=60):
        """Save the matched markets and near-arb list (at most once a minute) for a fast restart."""
        if time.time() - getattr(self, "_warm_saved", 0) < min_interval or not self.catalog_time:
            return
        self._warm_saved = time.time()
        try:
            with self.lock:
                data = {"sports_cat": self.sports_cat, "auto_pairs": self.auto_pairs,
                        "suggestions": self.suggestions, "hot_groups": self.hot_groups,
                        "series_fees": self.series_fees, "fee_overrides": self.fee_overrides,
                        "state": {k: self.state.get(k) for k in self.WARM_STATE_KEYS},
                        "stats": dict(self.state.get("stats") or {})}
            warmcache.save(data)
        except Exception as e:
            self.log(f"Couldn't save the warm-start cache: {e!r}")

    def load_warm(self):
        """Start from the last run's matches, if recent. Returns True if loaded."""
        d = warmcache.load()
        if not d or not d.get("sports_cat") or not d["sports_cat"][0]:
            return False
        try:
            self.sports_cat = d["sports_cat"]
            self.series_fees, self.fee_overrides = d.get("series_fees") or {}, d.get("fee_overrides") or {}
            with self.lock:
                self.auto_pairs, self.suggestions = d.get("auto_pairs") or [], d.get("suggestions") or []
                self.hot_groups = d.get("hot_groups") or {}
                self.state.update({k: v for k, v in (d.get("state") or {}).items() if v is not None})
                self.state["stats"].update({k: v for k, v in (d.get("stats") or {}).items()
                                            if k in ("pm_markets", "kalshi_markets", "matched_games")})
            self._publish()
        except Exception as e:
            self.log(f"Warm start skipped ({e!r}); loading everything fresh")
            self.sports_cat, self.hot_groups = ([], {}), {}
            return False
        try:
            self.refresh_pairs()           # a few requests: current terms for the non-sports pairs
        except Exception as e:
            self.log(f"Warm start: non-sports pairs wait for the fresh load ({e!r})")
        age = (time.time() - d["time"]) / 60
        self.log(f"Warm start: scanning {len(self.contracts)} contracts matched {age:.0f} min ago and "
                 f"{sum(len(v) for v in self.hot_groups.values())} near-arb markets right away; "
                 f"fresh market lists are loading in the background")
        return True

    def _stream_hot(self):
        """Point the live streams at the saved near-arb markets before the first full sweep."""
        wanted = {"kalshi": [], "polymarket": []}
        with self.lock:
            hot = list(self.hot_groups.values())
        for ids in hot:
            for ex, mid in ids:
                wanted[ex].append(mid)
        for c in self.pairs_cat[0] + self.crypto_cat[0]:
            wanted[c.exchange].append(c.market_id)
        for ex, stream in self.streams.items():
            stream.want(list(dict.fromkeys(wanted[ex]))[:config.STREAM_MAX_MARKETS])

    def run_forever(self, stop_event=None):
        self.start_message()
        try:
            self.load_warm()
        except Exception as e:
            self.log(f"Warm start skipped ({e!r})")
        threading.Thread(target=self._catalog_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._suggest_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._crypto_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._positions_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._balances_loop, args=(stop_event,), daemon=True).start()
        while not self.contracts and not (stop_event and stop_event.is_set()):
            time.sleep(1)               # first catalog load
        from . import streams
        self.streams = streams.build(self.kalshi, self.on_stream_update, self.log, {}, {})
        if self.streams:
            self._publish()             # hand the streams the market objects
            self._stream_hot()
            threading.Thread(target=self._stream_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._loop, args=(stop_event, False), daemon=True).start()
        self._loop(stop_event, True)

    def live_depth(self, exchange, market_id, side):
        """On-demand order book for buying one side of one market, fetched right now."""
        if side not in ("yes", "no"):
            raise ValueError("side must be yes or no")
        if exchange == "kalshi":
            levels = self.kalshi.live_levels(market_id)
        elif exchange == "polymarket":
            levels = self.pm.live_levels(market_id)
        else:
            raise ValueError("exchange must be kalshi or polymarket")
        return {"exchange": exchange, "market_id": market_id, "side": side,
                "levels": levels[side][:DEPTH_LEVELS], "fetched": engine.now_utc().isoformat()}

    def snapshot(self):
        with self.lock:
            s = dict(self.state)
        s["logs"] = list(self.logs)[-30:]
        s["autotrade"] = self.autotrader.status()
        return s
