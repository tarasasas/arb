"""Background scan loop: refresh catalogs, match games, refresh prices, find arbs."""

import threading
import time
import traceback
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

from . import config, crypto, engine, matching, nonsports
from .kalshi import KalshiClient
from .matchstore import MatchStore
from .polymarket import PolymarketClient

DEPTH_LEVELS = 25      # order-book price levels kept per leg for the dashboard


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
        self.sports_cat, self.pairs_cat = ([], {}), ([], {})
        self.series_fees = {}
        self.suggestions, self.suggest_time = [], 0.0
        self.suggest_state = {"status": "waiting"}
        self._pairs_pending = False
        self.auto_pairs = []            # confident non-sports matches scanned without your approval
        self.crypto_pairs = []          # identical crypto Up/Down windows, paired by contract terms
        self.trader, self.trading_status = self._make_trader()
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

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append(line)
        if self.log_to_console:
            print(line, flush=True)

    # ---- catalog ----------------------------------------------------------------------

    def refresh_catalog(self):
        t0 = time.time()
        self.log("Loading sports markets from both exchanges...")
        with ThreadPoolExecutor(2) as pool:
            pm_job = pool.submit(self.pm.load_sports_markets, self.log)
            k_job = pool.submit(self.kalshi.load_sports_markets, self.log)
            pmarkets, kmarkets = pm_job.result(), k_job.result()
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
            approved += [a for a in self.auto_pairs + self.crypto_pairs if (a["pm"], a["kalshi"]) not in manual]
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
        contracts = s_contracts + p_contracts
        source = {**s_source, **p_source}
        groups = engine.group_pairs(contracts)
        with self.lock:
            self.contracts, self.source, self.groups = contracts, source, groups
            self.contract_index = {(c.exchange, c.market_id): c for c in contracts}
            self.state["stats"].update({"paired_quantities": len(groups), "paired_contracts": len(contracts),
                                        "approved_pairs": len(p_contracts) // 2,
                                        "auto_pairs": sum(1 for c in p_contracts
                                                          if c.note == "auto" and c.exchange == "kalshi")})

    def refresh_crypto(self):
        """Pair the current crypto Up/Down windows; reload pairs only when the set changes."""
        pm_raw = self.pm.raw_markets(("crypto",), self.log)
        k_raw = [m for series in crypto.kalshi_series_for(pm_raw) for m in self.kalshi.open_markets(series)]
        new = crypto.pairs(pm_raw, k_raw)
        with self.lock:
            changed = {(a["pm"], a["kalshi"]) for a in new} != {(a["pm"], a["kalshi"]) for a in self.crypto_pairs}
            self.crypto_pairs = new
        if changed:
            self.log(f"Crypto Up/Down: {len(new)} identical window(s) on both exchanges")
            self.refresh_pairs()

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
                 f"lower-confidence pairs left for review ({time.time() - t0:.0f}s)")
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

    def refresh_prices(self, hot=False):
        """Full sweep (hot=False): every watched contract. Hot sweep: only the quantities that
        were within NEAR_MISS_EDGE of an arb on the last full sweep, so it takes ~1-2s."""
        t0 = time.time()
        with self.lock:
            contracts, source, groups = self.contracts, self.source, self.groups
            hot_keys = self.hot_groups
        if hot:
            # Only the markets that appear in near-arb pairs, grouped as before.
            groups = {g: {ex: [c for c in groups[g][ex] if (ex, c.market_id) in hot_keys[g]] for ex in groups[g]}
                      for g in hot_keys if g in groups}
            contracts = [c for g in groups.values() for lst in g.values() for c in lst]
            if not contracts:
                return
        kms = [source[("kalshi", c.market_id)] for c in contracts if c.exchange == "kalshi"]
        pms = [source[("polymarket", c.market_id)] for c in contracts if c.exchange == "polymarket"]
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
        now = engine.now_utc()
        opportunities, near = [], []
        book_budget = 15 if hot else 60        # Polymarket book fetches per cycle
        fetched = set()
        for cand in cands:
            if cand["edge"] > 0 and book_budget > 0:
                km = source[("kalshi", cand["k"].market_id)]
                pm = source[("polymarket", cand["p"].market_id)]
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
                # Re-read top of book after the fresh fetch.
                cand["ap"] = pm.yes_ask if cand["sp"] == "yes" else pm.no_ask
                if cand["ap"] is None:
                    continue
                levels_k, levels_p = km.levels.get(cand["sk"], []), pm.levels.get(cand["sp"], [])
                sizing = engine.size_opportunity(cand, levels_k, levels_p)
                cand["depth"] = {"kalshi": levels_k[:DEPTH_LEVELS], "polymarket": levels_p[:DEPTH_LEVELS]}
                if sizing and sizing["profit"] >= config.MIN_PROFIT_DOLLARS:
                    opportunities.append(engine.to_row(cand, sizing, now))
                    continue
            if len(near) < config.MAX_NEAR_MISSES:
                near.append(engine.to_row(cand, None, now))

        opportunities.sort(key=lambda r: -r["profit"])
        secs = round(time.time() - t0, 1)
        with self.lock:
            self.state.update({"opportunities": opportunities, "near_misses": near,
                               "last_prices": now.isoformat(), "status": "running",
                               "hot_seconds" if hot else "scan_seconds": secs,
                               "hot_count": sum(len(v) for v in self.hot_groups.values())})
            if not hot:
                self.state["last_full"] = now.isoformat()
        if not hot:
            self.log(f"Full sweep in {secs:.0f}s: {len(opportunities)} opportunities, {len(cands)} pairs within "
                     f"{abs(config.NEAR_MISS_EDGE) * 100:.0f}c of breaking even (re-checked every ~2s until next sweep)")

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
            time.sleep(config.HOT_PAUSE_SECS if hot else config.FULL_SWEEP_SECS)

    def run_forever(self, stop_event=None):
        self.start_message()
        threading.Thread(target=self._catalog_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._suggest_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._crypto_loop, args=(stop_event,), daemon=True).start()
        while not self.contracts and not (stop_event and stop_event.is_set()):
            time.sleep(1)               # first catalog load
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
        return s
