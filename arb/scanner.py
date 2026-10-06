"""Background scan loop: refresh catalogs, match games, refresh prices, find arbs."""

import json
import math
import threading
import time
import traceback
from collections import Counter, deque
from datetime import timedelta

from . import accounts, combos, config, crypto, engine, gctune, kalshi, matching, nonsports, warmcache
from .http import LanePool, priority
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
        self.my_arbs.fee_coef = self.fee_coef_for   # Worth now uses the fees Sell uses
        self.accounts = None            # built on first sync (needs the API keys in .env)
        self.streams = {}               # live order-book streams, by exchange (need API keys)
        self.market_groups = {}         # (exchange, market id) -> pair groups it's in
        self.dirty, self.dirty_lock = set(), threading.Lock()
        self._tick = threading.Event()
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
        from .combotrade import ComboTrader
        self.combo_trader = ComboTrader(self.trader)      # Make trade for the Combos tab
        self.load_focus()               # scan only markets settling soon, if you set Focus
        from .autotrade import AutoTrader
        self.autotrader = AutoTrader(self, stats_path=config.EXEC_STATS_FILE)   # off until you turn it on
        from .maker import MakerBot
        self.makerbot = MakerBot(self)      # Auto maker: off until you turn it on in Maker mode
        from .evbot import EVBot
        self.evbot = EVBot(self, path=config.EV_BETS_FILE)   # EV bot: off until you turn it on
        from .balance import Balancer
        self.balancer = Balancer(self)      # "Balance" in My arbs: even up legs with different share counts
        from .sellearly import EarlySeller
        self.seller = EarlySeller(self)     # "Sell" in My arbs: close an arb early when selling now is a profit
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

    def fee_coef_for(self, exchange, market_id):
        """A market's taker fee coefficient: its matched contract's, else its Kalshi series', else None."""
        c = self.find_any_contract(exchange, market_id)
        if c is not None:
            return c.fee_coef
        if exchange == "kalshi":
            return self.kalshi.series_fee_coefs().get(market_id.split("-")[0])
        return None

    def find_any_contract(self, exchange, market_id):
        """Like find_contract, but over every matched market, not just the ones Focus scans."""
        with self.lock:
            idx = getattr(self, "all_contract_index", None) or self.contract_index
            return idx.get((exchange, market_id))

    def find_matches(self, exchange, market_id):
        """Every matched pair's contract for this market (one per pair it's in), Focus aside."""
        with self.lock:
            hits = (getattr(self, "all_matches", None) or {}).get((exchange, market_id))
            if hits:
                return list(hits)
            c = (getattr(self, "all_contract_index", None) or self.contract_index).get((exchange, market_id))
            return [c] if c else []

    def find_contract(self, exchange, market_id):
        with self.lock:
            return self.contract_index.get((exchange, market_id))

    def note_latency(self, stages):
        self.__dict__.setdefault("_latency", deque(maxlen=50)).append(stages)

    def latency_summary(self):
        """p50 / p95 milliseconds per stage over the last 50 trades."""
        samples = list(getattr(self, "_latency", []))
        if not samples:
            return None
        out = {}
        for stage in dict.fromkeys(k for s in samples for k in s):
            vals = sorted(s[stage] for s in samples if stage in s)
            out[stage] = {"p50": vals[len(vals) // 2], "p95": vals[min(len(vals) - 1, int(len(vals) * 0.95))],
                          "n": len(vals)}
        return out

    def keep_shard_split(self, now=None):
        """Kalshi cash across exchange shards, per KALSHI_SHARD_MODE. even: Kalshi's own rebalancing keeps
        an equal share on every shard, so a trade never waits for cash to move. per_trade: that rebalancing
        is turned off, because the app moves cash as each trade needs it and Kalshi would move it back.
        manual: left alone. Runs at start, after the mode changes in Settings, and every
        SHARD_SPLIT_CHECK_SECS (someone may have changed the split at kalshi.com)."""
        from . import shards
        kv = (self.trader.venues or {}).get("kalshi") if self.trader else None
        mode, now = config.KALSHI_SHARD_MODE, now or time.time()
        if not kv or not hasattr(kv, "set_rebalancing"):
            return
        # even_out_shards steps in a minute after even mode starts, whether or not Kalshi took the split
        if mode != "even":
            self._even_since = None
        elif getattr(self, "_even_since", None) is None:
            self._even_since = now
        if mode == getattr(self, "_shard_mode_set", None) and (mode == "manual" or now - getattr(
                self, "_shard_checked", 0) < config.SHARD_SPLIT_CHECK_SECS):
            return
        if mode == "manual":
            self._shard_mode_set = mode
            self.log("Kalshi shards: manual (Settings > Kalshi cash across exchange shards): the app leaves them as they are")
            return
        self._shard_checked = now
        split = shards.even_split() if mode == "even" else {}
        try:
            was = kv.set_rebalancing(split)
        except Exception as e:
            self.log(f"Kalshi shards: couldn't set Kalshi's automatic rebalancing ({e!r}); will retry")
            return
        first = mode != getattr(self, "_shard_mode_set", None)
        self._shard_mode_set = mode
        text = ", ".join(f"shard {i} {p}%" for i, p in split.items())
        if was is not None:
            old = ", ".join(f"shard {a.get('exchange_index')} {a.get('percent')}%" for a in was) or "off"
            self.log(f"Kalshi shards: Kalshi now keeps an even split ({text}; was {old}), moving cash between your "
                     f"shards about every 10 seconds" if split else
                     f"Kalshi shards: turned off Kalshi's automatic rebalancing (was {old}); the app now moves cash "
                     f"onto a market's shard only when a trade needs it")
        elif first:
            self.log(f"Kalshi shards: even split ({text}) is set at Kalshi" if split else
                     "Kalshi shards: per trade: Kalshi's automatic rebalancing is off; the app moves cash as trades need it")

    def even_out_shards(self, now=None):
        """even mode, as a backstop to Kalshi's rebalancing: if the shards are still well off an equal
        share SHARD_EVEN_GRACE_SECS after even mode started (whether or not Kalshi accepted the split),
        move the surplus to the short ones directly.
        Same direction as Kalshi's own moves, so the two never fight. Only between trades, at most once a
        minute, and only moves above-share cash. Returns the moves made."""
        from . import shards
        kv = (self.trader.venues or {}).get("kalshi") if self.trader else None
        now = now or time.time()
        if config.KALSHI_SHARD_MODE != "even" or not kv or not hasattr(kv, "transfer"):
            return []
        since = getattr(self, "_even_since", None)
        if since is None or now - since < config.SHARD_EVEN_GRACE_SECS or now - getattr(self, "_evened_at", 0) < 60:
            return []
        if getattr(getattr(self.trader, "lock", None), "locked", lambda: False)() or getattr(self.autotrader, "busy", False):
            return []                              # a trade is sizing against this cash right now
        with self.lock:
            b = dict(self.state.get("balances") or {})
        cash = {int(k): float(v) for k, v in (b.get("kalshi_shards") or {}).items()}
        if not cash or b.get("stale") or b.get("error"):
            return []
        split = shards.even_split()
        total = sum(cash.values())
        target = {i: total * split.get(i, 0) / 100 for i in set(cash) | set(split)}
        short = sorted(((target[i] - cash.get(i, 0.0), i) for i in split
                        if cash.get(i, 0.0) < 0.9 * target[i] and target[i] - cash.get(i, 0.0) >= 1), reverse=True)
        moves = []
        for need, dst in short:
            for src in sorted((i for i in cash if cash[i] > target[i]), key=lambda i: target[i] - cash[i]):
                amount = math.floor(min(need, cash[src] - target[src]) * 100) / 100
                if amount < 1:
                    continue
                kv.transfer(src, dst, amount)
                cash[src] -= amount
                cash[dst] = cash.get(dst, 0.0) + amount
                need -= amount
                moves.append((src, dst, amount))
                if need < 1:
                    break
        self._evened_at = now
        for src, dst, amount in moves:
            self.log(f"Kalshi shards: moved ${amount:.2f} from shard {src} to shard {dst} toward an even split "
                     f"(Kalshi hadn't evened them out)")
        if moves:
            with self.lock:
                if self.state.get("balances"):
                    self.state["balances"] = {**self.state["balances"], "stale": True}
        return moves

    def start_message(self):
        self.log(f"Kalshi access: {self.kalshi.auth_info}")
        self.log(f"Trading: {self.trading_status}")
        self.log("Phone alerts: " + (f"{', '.join(self.alerter.channels())} for arbs of ${self.alerter.min_profit:g}+"
                                     if self.alerter.enabled else "off (see Alerts in the README)"))
        with self.lock:
            self.state["stats"]["alerts"] = {"channels": self.alerter.channels(), "min_profit": self.alerter.min_profit}
            # The trader is ready now; don't make the dashboard wait for the first market-list load.
            self.state["stats"].update({"kalshi_access": self.kalshi.auth_info, "trading": self.trading_status})

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append(line)
        if self.log_to_console:
            print(line, flush=True)

    # ---- catalog ----------------------------------------------------------------------

    def refresh_catalog(self):
        t0 = time.time()
        self.log("Loading sports markets from both exchanges...")
        with LanePool(3) as pool:
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
        self._gc_settle()

    def _gc_settle(self):
        """After a big reload: freeze the long-lived objects so garbage collection stops pausing
        everything (gctune). A full collection waits while a trade is in flight."""
        def busy():
            trader, auto = getattr(self, "trader", None), getattr(self, "autotrader", None)
            return bool((trader and trader.lock.locked()) or (auto and auto.busy))
        pause = gctune.settle(busy)
        if pause > 50:
            self.log(f"Memory cleanup paused the scanner for {pause:.0f}ms (runs at most every "
                     f"{config.GC_FULL_EVERY_SECS / 60:.0f} min)")

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

    # ---- focus: scan only markets settling soon -------------------------------------------------

    def load_focus(self):
        try:
            self.focus_days = float(json.loads(config.FOCUS_FILE.read_text(encoding="utf-8")).get("days") or 0)
        except (OSError, ValueError, AttributeError):
            self.focus_days = 0.0

    def set_focus(self, days):
        """Scan only pairs whose Kalshi market settles within `days` (0 = everything). Fewer markets means
        faster full sweeps, all of them on the live streams, and arbs that pay out sooner."""
        self.focus_days = max(0.0, float(days or 0))
        try:
            config.FOCUS_FILE.parent.mkdir(exist_ok=True)
            config.FOCUS_FILE.write_text(json.dumps({"days": self.focus_days}), encoding="utf-8")
        except OSError:
            pass
        self._publish()
        if self.streams:
            self._apply_stream_wants()
        self.log(f"Focus: {'markets settling within ' + format(self.focus_days, 'g') + ' days' if self.focus_days else 'all markets'}"
                 f" ({len(self.contracts)} contracts scanned)")
        return {"days": self.focus_days, "contracts": len(self.contracts)}

    @staticmethod
    def _in_focus(group, cutoff):
        """A pair group is in focus if a Kalshi market in it settles before the cutoff (no date: kept)."""
        times = [engine._parse_time(c.close_time) for c in group["kalshi"] if c.close_time]
        times = [t for t in times if t]
        return not times or min(times) <= cutoff

    def _publish(self):
        s_contracts, s_source = self.sports_cat
        p_contracts, p_source = self.pairs_cat
        c_contracts, c_source = self.crypto_cat
        contracts = s_contracts + p_contracts + c_contracts
        source = {**s_source, **p_source, **c_source}
        groups = engine.group_pairs(contracts)
        everything = {(c.exchange, c.market_id): c for c in contracts}   # Focus aside: for your positions
        every_match = {}               # a market can be in several matched pairs: keep each one's contract
        for c in contracts:
            every_match.setdefault((c.exchange, c.market_id), []).append(c)
        focus = getattr(self, "focus_days", 0)
        if focus:
            cutoff = engine.now_utc() + timedelta(days=focus)
            groups = {k: g for k, g in groups.items() if self._in_focus(g, cutoff)}
            keep = {(c.exchange, c.market_id) for g in groups.values() for lst in g.values() for c in lst}
            contracts = [c for c in contracts if (c.exchange, c.market_id) in keep]
        with self.lock:
            self.contracts, self.source, self.groups = contracts, source, groups
            self.contract_index = {(c.exchange, c.market_id): c for c in contracts}
            self.all_contract_index = everything
            self.all_matches = every_match
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

    def _make_accounts(self):
        """Signed reads of both accounts. Polymarket's go over the client its orders use (same host and key)."""
        pv = ((getattr(self, "trader", None) and self.trader.venues) or {}).get("polymarket")
        return accounts.Accounts(self.kalshi, pm_http=getattr(pv, "http", None))

    def sync_positions(self):
        """Live position check: read both accounts, pair positions into arbs in My arbs."""
        if self.accounts is None:
            self.accounts = self._make_accounts()
        acc = self.accounts
        if len(acc.missing) == 2:
            self.my_arbs.sync_state = {"status": "off", "error": "Add your Kalshi and Polymarket API keys to .env"}
            return
        read_at = time.time()             # a sale after this isn't in these positions yet
        try:
            kpos, ppos = acc.positions()
        except Exception as e:
            self.my_arbs.sync_state = {"status": "error", "error": repr(e), "time": engine.now_utc().isoformat()}
            self.log(f"Position check failed: {e!r}")
            return
        pairs, unpaired = accounts.pair_positions(kpos, ppos, self.find_matches)
        self.my_arbs.sync_from_accounts(
            pairs, kpos, ppos, unpaired,
            lambda kc: {"game": kc.game_label or kc.game_key.split(":", 1)[-1], "tab": engine.row_tab(kc),
                        "closes": kc.close_time}, read_at)
        try:                               # follow legs you sold yourself (needs each market's live state)
            self.my_arbs.snapshot(self.kalshi, self.pm)
            read = tuple(ex for ex, name in (("kalshi", "Kalshi"), ("polymarket", "Polymarket")) if name not in acc.missing)
            for a in self.my_arbs.reconcile(kpos, ppos, read, read_at):
                self.log(f"My arbs: {a['game']}: {a['note'].split('. ')[0]}")
            for a in self.my_arbs.update_cost_basis(kpos, ppos, read, read_at):
                self.log(f"My arbs: {a['game']}: cost updated from your accounts to "
                         f"${sum(l['paid'] for l in a['legs']):.2f}")
            if acc.kalshi_http:             # when each arb was really placed (first Kalshi fill), once per arb
                self.my_arbs.fill_placed_times(lambda t: min(
                    (f.get("created_time") for f in (acc.kalshi_http.get("/portfolio/fills", {"ticker": t, "limit": 200})
                                                     .get("fills") or []) if f.get("created_time")), default=None))
            for a in self.my_arbs.verify(self.find_matches):
                self.log(f"My arbs: {a['game']}: {a['check']['why']}")
        except Exception as e:
            self.log(f"My arbs: couldn't compare with your positions ({e!r})")
        self.my_arbs.sync_state = {"status": "ok", "time": engine.now_utc().isoformat(), "missing": acc.missing,
                                   "positions": len(kpos) + len(ppos),
                                   "paired": len(pairs) + self.my_arbs.known_pairs}

    def refresh_balances(self):
        """Read your cash on both sites (needs the API keys) for sizing opportunities to it."""
        if self.accounts is None:
            self.accounts = self._make_accounts()
        if len(self.accounts.missing) == 2:
            return
        try:
            bal = self.accounts.balances()
            info = {**bal, "time": engine.now_utc().isoformat(), "missing": self.accounts.missing,
                    "auto_shard_funding": config.KALSHI_SHARD_MODE == "per_trade"}
        except Exception as e:
            with self.lock:
                old = dict(self.state.get("balances") or {})
            info = {**old, "error": repr(e)}            # keep the last known amounts
        with self.lock:
            self.state["balances"] = info

    def balances_every(self):
        """Seconds between cash reads: more often while Auto-trade or Auto maker is on, which also keeps the
        connections orders go out on warm (see BALANCES_REFRESH_AUTO_SECS)."""
        return config.BALANCES_REFRESH_AUTO_SECS if self.lane_active() else config.BALANCES_REFRESH_SECS

    def _balances_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            started = time.time()
            self.refresh_balances()
            try:
                self.keep_shard_split()
                self.even_out_shards()
            except Exception as e:
                self.log(f"Kalshi shards: even split check failed ({e!r})")
            try:
                self.prefund_shards()
            except Exception as e:
                self.log(f"Kalshi shards: top-up failed ({e!r})")
            # in short steps, so turning Auto-trade on switches to its pace within a second
            while time.time() - started < self.balances_every() and not (stop_event and stop_event.is_set()):
                time.sleep(0.5)

    def prefund_shards(self, now=None):
        """Keep one trade's worth of cash on every Kalshi shard that has an opportunity right now, so
        a trade there doesn't wait for a transfer. Moves from your richest other shard (never below
        what that shard needs itself); at most once a minute per shard. Returns the moves made."""
        kv = (self.trader.venues or {}).get("kalshi") if self.trader else None
        if not (config.KALSHI_SHARD_MODE == "per_trade" and kv and hasattr(kv, "transfer")):
            return []
        with self.lock:
            b = dict(self.state.get("balances") or {})
            rows = list(self.state.get("opportunities") or [])
        cash = {int(k): float(v) for k, v in (b.get("kalshi_shards") or {}).items()}
        if not cash or b.get("stale") or b.get("error"):
            return []
        target = max(config.FAST_MAX_TRADE, config.AUTO_TRADE_MAX_TRADE)
        needed = {int(r.get("kalshi_shard") or 0) for r in rows}
        now = now or time.time()
        last = self.__dict__.setdefault("_prefund_at", {})
        moves = []
        for shard in sorted(needed):
            short = target - cash.get(shard, 0.0)
            if short < 1 or now - last.get(shard, 0) < 60:
                continue
            for src in sorted((i for i in cash if i != shard), key=lambda i: -cash[i]):
                spare = cash[src] - (target if src in needed else 0.0)
                amount = math.floor(min(short, spare) * 100) / 100
                if amount < 1:
                    continue
                kv.transfer(src, shard, amount)
                cash[src] -= amount
                cash[shard] = cash.get(shard, 0.0) + amount
                short -= amount
                moves.append((src, shard, amount))
                if short < 1:
                    break
            last[shard] = now
        for src, dst, amount in moves:
            self.log(f"Kalshi shards: moved ${amount:.2f} from shard {src} to shard {dst} ahead of trades there")
        if moves:
            with self.lock:
                if self.state.get("balances"):
                    self.state["balances"] = {**self.state["balances"], "stale": True}
        return moves

    def _positions_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            if self.contracts and not self.auto_mode():   # pairing needs the market catalog loaded
                try:
                    self.sync_positions()
                except Exception as e:
                    self.log(f"Position check error: {e!r}")
            time.sleep(config.POSITIONS_REFRESH_SECS)

    def _evbot_loop(self, stop_event):
        """The EV bot's open bets: their results, once their games are over."""
        while not (stop_event and stop_event.is_set()):
            try:
                self.evbot.settle()
            except Exception as e:
                self.log(f"EV bot: settling failed ({e!r})")
            time.sleep(30)

    def _crypto_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            if self.auto_mode() and not config.AUTO_TRADE_CRYPTO_WINDOWS:
                time.sleep(1)                    # Auto-trade leaves crypto windows alone
                continue
            try:
                self.refresh_crypto()
            except Exception as e:
                self.log(f"Crypto pairing error: {e!r}")
            time.sleep(config.CRYPTO_REFRESH_SECS)

    def _suggest_loop(self, stop_event):
        while not (stop_event and stop_event.is_set()):
            if time.time() - self.suggest_time > config.SUGGEST_REFRESH_SECS and not self.auto_mode():
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
        with LanePool(3) as pool:
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
        self._gc_settle()

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
        """(exchange, market id) pairs with a recent book from a connected stream. Only these skip
        polling, so a stream that goes quiet can't freeze prices."""
        return {(ex, mid) for ex, s in self.streams.items() for mid in s.fresh(config.STREAM_FRESH_SECS)}

    def refresh_prices(self, hot=False, stream_groups=None, lane=False):
        """Full sweep (hot=False): every watched contract. Hot sweep: only the quantities that
        were within NEAR_MISS_EDGE of an arb on the last full sweep, so it takes ~1-2s.
        stream_groups: re-check just these pair groups on books the live streams already hold.
        lane: the fast lane, every pair Auto-trade could take (see lane_groups); "long": its long-dated pairs."""
        if hot or stream_groups is not None or lane:
            with priority():               # near-arb and stream re-checks go ahead of background loads
                return self._refresh_prices(hot, stream_groups, lane)
        return self._refresh_prices(hot, stream_groups)

    # ---- fast lane: what Auto-trade can take, checked on its own, faster -------------------------

    def lane_active(self):
        """FAST_LANE: "auto" runs it while Auto-trade or Auto maker is on, "always" all the time, "off" never."""
        mode = getattr(config, "FAST_LANE", "auto")
        if mode == "always":
            return True
        return mode == "auto" and bool(getattr(self.autotrader, "on", False) or getattr(self.makerbot, "on", False)
                                       or getattr(getattr(self, "evbot", None), "on", False))

    def lane_groups(self, now=None, long=False):
        """The pair groups Auto-trade could take: result known within FAST_MAX_HOURS, soonest first. long: the
        long-dated ones instead (known after that, within AUTO_TRADE_LONG_DAYS; Auto-trade needs a higher return
        there). Games already under way are left out unless AUTO_TRADE_LIVE_GAMES is on (Auto-trade skips them).
        Re-worked out at most every 30s, or when the matched markets or the hours change."""
        now = now or engine.now_utc()
        with self.lock:
            groups = self.groups
        skip_windows = config.FAST_LANE == "auto" and not config.AUTO_TRADE_CRYPTO_WINDOWS
        # games in progress: when Auto-trade takes them, or the EV bot is on and takes them (bets or dip trades)
        live_ok = bool(config.AUTO_TRADE_LIVE_GAMES or ((config.EV_BOT_LIVE_GAMES or config.EV_BOT_DIPS)
                                                       and getattr(getattr(self, "evbot", None), "on", False)))
        key = (id(groups), config.FAST_MAX_HOURS, config.AUTO_TRADE_LONG_DAYS, live_ok, skip_windows)
        hit = getattr(self, "_lane_cache", None)
        if hit and hit[0] == key and time.time() - hit[1] < 30:
            return hit[2][1 if long else 0]
        cutoff = now + timedelta(hours=config.FAST_MAX_HOURS)
        last = max(cutoff, now + timedelta(days=config.AUTO_TRADE_LONG_DAYS))
        picked = []
        for g, by_ex in groups.items():
            def first(ex):
                ts = [t for t in (engine._parse_time(c.close_time) for c in by_ex[ex] if c.close_time) if t]
                return min(ts) if ts else None
            k_close, p_close = first("kalshi"), first("polymarket")
            # Same date fast_check uses: when the result is known. Non-sports: the earlier site (Polymarket's
            # end date often runs weeks past the event); sports: Kalshi's.
            close = min((t for t in (k_close, p_close) if t), default=None) if g[1][0] == "event" else k_close
            if close is None or close > last:
                continue
            if (g[1][0] not in ("event", "price") and not live_ok
                    and p_close is not None and p_close <= now):
                continue                    # in play: nothing here takes it
            if skip_windows and g[1][0] == "price":
                continue                    # crypto Up/Down windows: Auto-trade leaves them alone
            picked.append((close, g))
        picked.sort(key=lambda t: t[0])
        out = ({g: groups[g] for t, g in picked if t <= cutoff}, {g: groups[g] for t, g in picked if t > cutoff})
        self._lane_cache = (key, time.time(), out)
        return out[1 if long else 0]

    def auto_groups(self):
        """Every pair group Auto-trade could take: the fast lane's and the long-dated ones."""
        return {**self.lane_groups(), **self.lane_groups(long=True)}

    def long_lane_on(self):
        """Auto-trade mode with long-dated arbs on: the full sweep that would otherwise find them is paused,
        so the lane checks them too, every AUTO_TRADE_LONG_RECHECK_SECS."""
        return bool(config.AUTO_TRADE_LONG_DAYS > 0 and config.AUTO_TRADE_FOCUS
                    and getattr(getattr(self, "autotrader", None), "on", False))

    def _lane_loop(self, stop_event):
        """While the lane is active, re-check its pairs every FAST_LANE_PAUSE_SECS (streamed markets from
        memory, the rest polled), so Auto-trade sees a new arb in about a second, not at the next full sweep."""
        was = False
        while not (stop_event and stop_event.is_set()):
            active = self.lane_active() and bool(self.groups)
            if active != was:               # streams: lane markets first while it runs
                was = active
                self._apply_stream_wants()
                self.log(f"Fast lane {'on' if active else 'off'}" + (
                    f": {len(self.lane_groups())} pairs decided within {config.FAST_MAX_HOURS:g}h, re-checked "
                    f"about every {config.FAST_LANE_PAUSE_SECS:g}s and first on the live streams" if active else ""))
            if not active:
                with self.lock:
                    self.state["lane"] = {"active": False}
                time.sleep(1)
                continue
            try:
                self.refresh_prices(lane=True)
            except Exception as e:
                self.log(f"Fast lane error: {e!r}")
                time.sleep(2)
                continue
            time.sleep(config.FAST_LANE_PAUSE_SECS)

    def _long_loop(self, stop_event):
        """While long_lane_on: re-check the long-dated pairs every AUTO_TRADE_LONG_RECHECK_SECS, on a thread of
        their own so the fast lane keeps its half-second pace (live-feed markets are re-checked as they move)."""
        was = False
        while not (stop_event and stop_event.is_set()):
            on = self.long_lane_on() and bool(self.groups)
            if on != was:                   # streams: long-dated markets after the fast lane's
                was = on
                self._apply_stream_wants()
                if on:
                    self.log(f"Long-dated arbs: {len(self.lane_groups(long=True))} pairs decided within "
                             f"{config.AUTO_TRADE_LONG_DAYS:g} days, re-checked about every "
                             f"{config.AUTO_TRADE_LONG_RECHECK_SECS:g}s")
            if not on:
                with self.lock:
                    self.state.pop("long_lane", None)
                time.sleep(1)
                continue
            try:
                self.refresh_prices(lane="long")
            except Exception as e:
                self.log(f"Long-dated check error: {e!r}")
                time.sleep(2)
                continue
            time.sleep(config.AUTO_TRADE_LONG_RECHECK_SECS)

    def _refresh_prices(self, hot=False, stream_groups=None, lane=False):
        t0 = time.time()
        t0_wall = t0                       # polled prices: the pass's start is the tick
        complete = bool(getattr(self, "catalog_time", 0) and getattr(self, "suggest_time", 0))   # covers every matched market
        with self.lock:
            contracts, source, groups = self.contracts, self.source, self.groups
            hot_keys = self.hot_groups
        if lane:
            groups = self.lane_groups(long=lane == "long")
            contracts = [c for g in groups.values() for lst in g.values() for c in lst]
            hot = True                      # a partial pass: keeps the rows it didn't re-check
            if not contracts:
                with self.lock:
                    if lane == "long":
                        self._long_t0 = t0
                        self.state["long_lane"] = {"pairs": 0, "markets": 0, "seconds": 0,
                                                   "days": config.AUTO_TRADE_LONG_DAYS}
                    else:
                        self.state["lane"] = {"active": True, "pairs": 0, "markets": 0, "seconds": 0}
                return
        # Full sweeps poll every market (a backstop for the streams); near-arb passes skip markets
        # with a fresh streamed book.
        streamed = self._streamed() if (hot or stream_groups is not None) else set()
        if stream_groups is not None:
            groups = {g: groups[g] for g in stream_groups if g in groups}
            contracts = [c for g in groups.values() for lst in g.values() for c in lst]
            hot = True
            if not contracts:
                return
        elif hot and not lane:
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
        failed = set()                     # markets whose request failed: their rows are kept, not dropped
        if stream_groups is None and (kms or pms):
            # Full sweeps read Kalshi's best prices from the market list (half the requests); the pairs
            # that need depth get their order books below. Near-arb passes fetch the books directly.
            kalshi_read = self.kalshi.refresh_books if hot and not lane else self.kalshi.refresh_tops
            with LanePool(2) as pool:
                jobs = [pool.submit(kalshi_read, kms), pool.submit(self.pm.refresh_quotes, pms)]
                failed_k, failed_p = (j.result() or set() for j in jobs)
            failed = {("kalshi", t) for t in failed_k} | {("polymarket", sl) for sl in failed_p}
        matching.sync_quotes(contracts, source)

        cands = engine.screen(groups, config.NEAR_MISS_EDGE)
        self._prefetch_trade_info(cands)
        evbot = getattr(self, "evbot", None)
        if evbot is not None:
            try:
                evbot.observe(groups, source)
            except Exception as e:
                self.log(f"EV bot error: {e!r}")
        # Combos (3-way dutches, same-site line arbs) on full sweeps and near-arb passes; see combos.py.
        with_combos = stream_groups is None and not lane
        combo_cands = combos.screen(groups, config.NEAR_MISS_EDGE, engine.now_utc()) if with_combos else []
        if not hot:
            # The closest pairs only: a long hot list re-checks slowly, and arbs come from the closest.
            hot_map = {}
            for c in cands[:config.HOT_MAX_PAIRS]:
                ids = hot_map.setdefault((c["k"].game_key, c["k"].var), set())
                ids.update({("kalshi", c["k"].market_id), ("polymarket", c["p"].market_id)})
            for cc in combo_cands[:config.COMBO_HOT_MAX]:          # near combos are re-checked with them
                for c, _ in cc["legs"]:
                    hot_map.setdefault((c.game_key, c.var), set()).add((c.exchange, c.market_id))
            with self.lock:
                self.hot_groups = hot_map
            self._stream_wanted(cands)
        now = engine.now_utc()
        opportunities, near = [], []
        book_budget = 15 if hot else 60        # Polymarket book fetches per cycle
        fetched, unchecked = set(), set()
        # Fetch the books this pass will need all at once, in parallel, best edges first.
        want = list(dict.fromkeys(c["p"].market_id for c in cands if c["edge"] > 0
                                  and ("polymarket", c["p"].market_id) not in streamed))[:book_budget]
        book_errors = {}

        def get_book(slug):
            try:
                self.pm.refresh_book(source[("polymarket", slug)])
            except Exception as e:
                book_errors[slug] = e
        # Kalshi depth for the same pairs, fetched at the same moment so both legs' books match in time
        # (a full sweep read only Kalshi's best prices; its maker rows need depth for near-arbs too).
        want_k = list(dict.fromkeys(c["k"].market_id for c in cands if (c["edge"] > 0 or not hot)
                                    and ("kalshi", c["k"].market_id) not in streamed))
        if stream_groups is not None:
            want_k = []                     # a stream pass: Kalshi books are live already
        if want or want_k:
            with LanePool(min(8, len(want)) + 1) as pool:
                k_job = pool.submit(self.kalshi.refresh_books, [source[("kalshi", t)] for t in want_k]) if want_k else None
                list(pool.map(get_book, want))
                if k_job:
                    failed |= {("kalshi", t) for t in (k_job.result() or set())}
        prefetched = set(want)
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
                        if pm.slug in book_errors:
                            raise book_errors[pm.slug]
                        if pm.slug not in prefetched:
                            self.pm.refresh_book(pm)
                    except Exception as e:
                        self.log(f"Book fetch failed for {pm.slug}: {e!r}")
                        pm.levels, pm.yes_ask, pm.no_ask = {}, None, None
                        failed.add(("polymarket", pm.slug))
                if not pm.levels:
                    continue
                # Re-read top of book after the fresh fetch, and the edge with it: the screen used the
                # quotes from before the fetch, which on a fast market (crypto windows) can be seconds old.
                cand["ap"] = pm.yes_ask if cand["sp"] == "yes" else pm.no_ask
                cand["ak"] = km.yes_ask if cand["sk"] == "yes" else km.no_ask
                if cand["ap"] is None or cand["ak"] is None:
                    continue
                cand["edge"] = (cand["payout"] - cand["ak"] - cand["ap"]
                                - engine.fee_per_contract(cand["k"].fee_coef, cand["ak"])
                                - engine.fee_per_contract(cand["p"].fee_coef, cand["ap"]))
                levels_k, levels_p = km.levels.get(cand["sk"], []), pm.levels.get(cand["sp"], [])
                sizing = engine.size_opportunity(cand, levels_k, levels_p)
                cand["depth"] = {"kalshi": levels_k[:DEPTH_LEVELS], "polymarket": levels_p[:DEPTH_LEVELS]}
                if sizing and sizing["profit"] >= config.MIN_PROFIT_DOLLARS:
                    row = engine.to_row(cand, sizing, now)
                    row["kalshi_shard"] = getattr(km, "shard", 0)   # Kalshi cash is held per shard
                    # Tick-to-trade timing: when the price that made this row arrived, and when it was found.
                    ticks = [getattr(s, "updated_at", {}).get(mid) for ex, mid in (("kalshi", cand["k"].market_id),
                                                                   ("polymarket", pm_slug))
                             for s in [self.streams.get(ex)] if s is not None]
                    ticks = [t for t in ticks if t]
                    row["tick_ts"] = max(ticks) if ticks else t0_wall
                    row["detected_ts"] = time.time()
                    opportunities.append(row)
                    continue
            if len(near) < config.MAX_NEAR_MISSES and _cand_key(cand) not in unchecked:
                near.append(engine.to_row(cand, None, now))

        maker = self._maker_rows(cands, source, now) if stream_groups is None and not lane else None
        combo_rows = (self._combo_rows(combo_cands, source, streamed | {("polymarket", sl) for sl in fetched}
                                       | {("kalshi", t) for t in want_k}, now) if with_combos else None)

        # A pass only replaces the rows it actually re-checked. The hot pass covers a few markets and
        # fetches at most 15 books, so without this the table dropped from ~50 rows to 15 between sweeps.
        covered = {(c.exchange, c.market_id) for c in contracts}
        self.merge_lock.acquire()
        with self.lock:
            previous = self.state["opportunities"]
        found = {_row_key(r) for r in opportunities}
        with self.lock:
            index = self.contract_index
        for r in previous:
            key = _row_key(r)
            if key in found or (r.get("trade_until") and r["trade_until"].replace("Z", "+00:00") <= now.isoformat()):
                continue
            legs = {("kalshi", key[0]), ("polymarket", key[2])}
            if not all(leg in index for leg in legs):
                continue                   # a market left the scanner's list (closed, or the match was removed)
            if key in unchecked or legs & failed or (hot and not legs <= covered):
                opportunities.append(r)    # not re-checked this pass (or its request failed): keep it
        opportunities.sort(key=lambda r: -r["profit"])
        secs = round(time.time() - t0, 1)
        with self.lock:
            self.state.update({"opportunities": opportunities, "last_prices": now.isoformat(), "status": "running",
                               "hot_count": sum(len(v) for v in self.hot_groups.values()),
                               "streams": {ex: s.status() for ex, s in self.streams.items()}})
            if lane == "long":
                last, self._long_t0 = getattr(self, "_long_t0", None), t0
                self.state["long_lane"] = {"pairs": len(groups), "seconds": secs, "days": config.AUTO_TRADE_LONG_DAYS,
                                           "every": round(t0 - last, 1) if last and t0 - last < 300 else None,
                                           "markets": len({(c.exchange, c.market_id) for c in contracts}),
                                           "polled": len(kms) + len(pms)}
            elif lane:
                last, self._lane_t0 = getattr(self, "_lane_t0", None), t0
                self.state["lane"] = {"active": True, "pairs": len(groups), "seconds": secs,
                                      "every": round(t0 - last, 1) if last and t0 - last < 60 else None,
                                      "markets": len({(c.exchange, c.market_id) for c in contracts}),
                                      "polled": len(kms) + len(pms), "hours": config.FAST_MAX_HOURS}
            elif stream_groups is None:
                self.state.update({"near_misses": near, "hot_seconds" if hot else "scan_seconds": secs,
                                   "maker": maker})
            if combo_rows is not None:
                self.state["combos"] = self._merge_combos(combo_rows, covered, index, now)
            if not hot:
                self.state["last_full"] = now.isoformat()
                self._warm_ready = complete
        self.merge_lock.release()
        self.alerter.check(opportunities)
        self.autotrader.check(opportunities)
        if maker is not None:
            self.makerbot.check(maker)
        if not hot:
            self.log(f"Full sweep in {secs:.0f}s: {len(opportunities)} opportunities, {len(cands)} pairs within "
                     f"{abs(config.NEAR_MISS_EDGE) * 100:.0f}c of breaking even (re-checked every ~2s until next sweep)")

    def _prefetch_trade_info(self, cands):
        """While Auto-trade (or Auto maker) is on: the market details (tick, minimum size, shard, open) of the
        pairs closest to an arb that it could take, loaded in the background (see Trader.prefetch_info). When one
        turns into an arb, its trade's checks find them cached instead of waiting a round trip for them."""
        trader = getattr(self, "trader", None)
        if not (trader and trader.venues and self.lane_active()):
            return
        lane, ids = self.lane_groups(), []
        for c in cands:                     # best edge first
            if c["edge"] < config.INFO_PREFETCH_EDGE:
                break
            if (c["k"].game_key, c["k"].var) in lane:
                ids += [("kalshi", c["k"].market_id), ("polymarket", c["p"].market_id)]
                if len(ids) >= 2 * config.INFO_PREFETCH_PAIRS:
                    break
        if ids:
            trader.prefetch_info(ids)

    def _combo_rows(self, cands, source, have_books, now):
        """Rows for the combos with a top-of-book edge: their books fetched (Kalshi in one batch, Polymarket up
        to COMBO_BOOK_BUDGET), then sized on real depth across every leg. have_books: (exchange, market id)
        whose book is already current this pass (streamed, or fetched for a pair)."""
        pos = [c for c in cands if c["edge"] > 0][:config.COMBO_MAX_ROWS]
        if not pos:
            return []
        legs = {(c.exchange, c.market_id) for cand in pos for c, _ in cand["legs"]} - have_books
        want_k = [source[k] for k in legs if k[0] == "kalshi" and k in source]
        want_p = [source[k] for k in legs if k[0] == "polymarket" and k in source][:config.COMBO_BOOK_BUDGET]

        def pm_book(pm):
            try:
                self.pm.refresh_book(pm)
            except Exception:
                pm.levels = {}                 # no current book: that combo isn't sized this pass
        with LanePool(min(8, len(want_p)) + 1) as pool:
            k_job = pool.submit(self.kalshi.refresh_books, want_k) if want_k else None
            list(pool.map(pm_book, want_p))
            if k_job:
                k_job.result()
        rows = []
        for cand in pos:
            levels = [((getattr(source.get((c.exchange, c.market_id)), "levels", None) or {}).get(s) or [])
                      for c, s in cand["legs"]]
            if any(not lv for lv in levels):
                continue
            # prices from the books just read, not from the screen's quotes
            asks = [lv[0][0] for lv in levels]
            edge = cand["payout"] - sum(a + engine.fee_per_contract(c.fee_coef, a) for a, (c, _) in zip(asks, cand["legs"]))
            sizing = combos.size(cand["legs"], levels, cand["payout"]) if edge > 0 else None
            if sizing and sizing["profit"] >= config.MIN_PROFIT_DOLLARS:
                shards = {c.market_id: getattr(source.get(("kalshi", c.market_id)), "shard", 0)
                          for c, _ in cand["legs"] if c.exchange == "kalshi"}
                rows.append(combos.to_row({**cand, "asks": asks, "edge": edge}, sizing, now, shards))
        rows.sort(key=lambda r: -r["profit"])
        return rows

    def _merge_combos(self, rows, covered, index, now):
        """This pass's combos, plus earlier ones it didn't re-check (not all their markets were in this pass),
        while their markets are still listed and they're no older than COMBO_MAX_AGE_SECS."""
        found = {r["key"] for r in rows}
        out = list(rows)
        for r in self.state.get("combos") or []:
            legs = {(l["exchange"].lower(), l["market_id"]) for l in r["legs"]}
            age = (now - engine._parse_time(r["checked"])).total_seconds() if r.get("checked") else math.inf
            if (r["key"] not in found and not legs <= covered and all(leg in index for leg in legs)
                    and age <= config.COMBO_MAX_AGE_SECS):
                out.append(r)
        out.sort(key=lambda r: -r["profit"])
        return out

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
        self._stream_pairs = [(c["k"].market_id, c["p"].market_id) for c in cands]
        self._apply_stream_wants()

    def _apply_stream_wants(self):
        """While the fast lane runs, its markets go first (near-arb ones, then the rest, soonest payout
        first), so the STREAM_MAX_MARKETS slots go to what Auto-trade can take."""
        if not self.streams:
            return
        pairs = getattr(self, "_stream_pairs", [])
        wanted = {"kalshi": [], "polymarket": []}
        # the fast lane's markets first, then (Auto-trade mode with long-dated arbs on) the long-dated ones
        for lane in (self.lane_groups() if self.lane_active() else {},
                     self.lane_groups(long=True) if self.long_lane_on() else {}):
            ids = {(c.exchange, c.market_id) for g in lane.values() for lst in g.values() for c in lst}
            for k, p in pairs:
                if ("kalshi", k) in ids or ("polymarket", p) in ids:
                    wanted["kalshi"].append(k)
                    wanted["polymarket"].append(p)
            for g in lane.values():
                for ex, lst in g.items():
                    wanted[ex] += [c.market_id for c in lst]
        for k, p in pairs:
            wanted["kalshi"].append(k)
            wanted["polymarket"].append(p)
        for c in self.pairs_cat[0] + self.crypto_cat[0]:
            wanted[c.exchange].append(c.market_id)
        if getattr(self, "focus_days", 0):          # focused: stream every scanned market that fits
            with self.lock:
                focused = list(self.contracts)
            for c in focused:
                wanted[c.exchange].append(c.market_id)
        for ex, stream in self.streams.items():
            ids = list(dict.fromkeys(wanted[ex]))[:config.STREAM_MAX_MARKETS]
            stream.want(ids)

    def on_stream_update(self, exchange, market_id):
        with self.dirty_lock:
            self.dirty.add((exchange, market_id))
        self._tick.set()                    # wake the re-check now, not on the next poll

    def _stream_loop(self, stop_event):
        """Re-check the pairs a streamed price change touches, as soon as it arrives. Ticks that
        come in while a re-check runs are batched into the next one."""
        while not (stop_event and stop_event.is_set()):
            self._tick.wait(1.0)
            self._tick.clear()
            with self.dirty_lock:
                dirty, self.dirty = self.dirty, set()
            if dirty:
                with self.lock:
                    idx = self.market_groups
                gs = set().union(*(idx.get(d, set()) for d in dirty))
                if self.auto_mode():
                    gs &= set(self.auto_groups())        # only what Auto-trade can take
                try:
                    if gs:
                        self.refresh_prices(stream_groups=gs)
                except Exception as e:
                    self.log(f"Stream re-check error: {e!r}")
                    time.sleep(1)

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

    def auto_mode(self):
        """Auto-trade mode (AUTO_TRADE_FOCUS, while Auto-trade is on): only the markets Auto-trade can take
        refresh (the fast lane), so it and Auto-trade's own checks get the request budget. Paused meanwhile:
        full sweeps, near-arb re-checks, live-feed re-checks of other markets, non-sports suggestions, the My
        arbs position check, and crypto pairing unless Auto-trade takes crypto windows. Market lists, cash
        and shards still refresh. Work already under way when it starts finishes."""
        on = bool(config.AUTO_TRADE_FOCUS and getattr(getattr(self, "autotrader", None), "on", False))
        if on != getattr(self, "_auto_mode_was", False):
            self._auto_mode_was = on
            self.log("Auto-trade mode: only the markets Auto-trade can take are refreshed; the full scan and other "
                     "checks wait until it's off" if on else "Regular mode: every market is scanned again")
            with self.lock:
                self.state["mode"] = "auto" if on else "regular"
        return on

    def _loop(self, stop_event, hot):
        """Full sweeps and hot re-checks run in parallel threads, so the near-arb list keeps
        updating every couple of seconds while a full sweep is in progress."""
        while not (stop_event and stop_event.is_set()):
            try:
                if (hot and not self.hot_groups) or self.auto_mode():
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

    def save_warm(self, min_interval=300):
        """Save the matched markets and near-arb list (at most every 5 minutes) for a fast restart."""
        if time.time() - getattr(self, "_warm_saved", 0) < min_interval or not getattr(self, "_warm_ready", False):
            return                         # only after a full sweep over every freshly matched market
        self._warm_saved = time.time()
        try:
            with self.lock:
                data = {"sports_cat": self.sports_cat, "pairs_cat": self.pairs_cat, "auto_pairs": self.auto_pairs,
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
            self.pairs_cat = d.get("pairs_cat") or ([], {})
            self.series_fees, self.fee_overrides = d.get("series_fees") or {}, d.get("fee_overrides") or {}
            with self.lock:
                self.auto_pairs, self.suggestions = d.get("auto_pairs") or [], d.get("suggestions") or []
                self.hot_groups = d.get("hot_groups") or {}
                self.state.update({k: v for k, v in (d.get("state") or {}).items() if v is not None})
                self.state["stats"].update({k: v for k, v in (d.get("stats") or {}).items()
                                            if k in ("pm_markets", "kalshi_markets", "matched_games")})
            self._publish()
            self._gc_settle()
        except Exception as e:
            self.log(f"Warm start skipped ({e!r}); loading everything fresh")
            self.sports_cat, self.hot_groups = ([], {}), {}
            return False
        def pairs():                       # current terms for the non-sports pairs, without holding up the start
            try:
                self.refresh_pairs()
            except Exception as e:
                self.log(f"Warm start: non-sports pairs wait for the fresh load ({e!r})")
        threading.Thread(target=pairs, daemon=True).start()
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
        threading.Thread(target=self._evbot_loop, args=(stop_event,), daemon=True).start()
        while not self.contracts and not (stop_event and stop_event.is_set()):
            time.sleep(1)               # first catalog load
        from . import streams
        self.streams = streams.build(self.kalshi, self.on_stream_update, self.log, {}, {})
        pv = (self.trader.venues or {}).get("polymarket")
        if pv is not None and streams.available():   # your orders and buying power, pushed instead of polled
            self.private_stream = streams.PolymarketPrivateStream(pv.http.signer, self.log)
            self.private_stream.start()
            pv.private = self.private_stream
        if self.streams:
            self._publish()             # hand the streams the market objects
            self._stream_hot()
            threading.Thread(target=self._stream_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._loop, args=(stop_event, False), daemon=True).start()
        threading.Thread(target=self._lane_loop, args=(stop_event,), daemon=True).start()
        threading.Thread(target=self._long_loop, args=(stop_event,), daemon=True).start()
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
        s["makerbot"] = self.makerbot.status()
        s["combo_history"] = list(getattr(getattr(self, "combo_trader", None), "history", []))
        s["evbot"] = self.evbot.status() if getattr(self, "evbot", None) else None
        s["focus"] = {"days": getattr(self, "focus_days", 0), "contracts": len(self.contracts)}
        s["latency"] = self.latency_summary()
        return s
