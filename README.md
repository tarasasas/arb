# Kalshi × Polymarket US arbitrage scanner

Finds guaranteed-profit pairs across Kalshi and Polymarket US sports markets: moneylines,
spreads, totals, team totals, and period markets (halves, quarters, hockey periods, MLB
first 5 innings and innings). Scanning works without API keys, and a Kalshi key makes it
faster. With both a Kalshi and a Polymarket key, the **Make trade** button can place both
legs of an arb for you after you confirm (see below).

## Run

Requires Python 3.10+ and no packages.

On Windows, double-click these files in this folder:

| File | What it does |
|---|---|
| `start-dashboard.bat` | Gets the latest code (see `update.bat`), then starts the scanner and opens the dashboard. Close the window to stop it. |
| `scan-once.bat` | Runs one scan and prints the results in the window. |
| `run-tests.bat` | Runs the unit tests. |
| `setup.bat` | One-time setup for the Kalshi API key: installs `cryptography` and creates `.env`. |
| `update.bat` | Gets the latest code from GitHub by running `update.sh` in Git Bash. `start-dashboard.bat` runs it for you every time. Keeps your `.env`, `kalshi.key` and trade log, and throws away any other edits to the code. |

Or run from a terminal:

```
python -m arb              # dashboard at http://localhost:8791 (opens automatically)
python -m arb --once       # one scan, results printed to the terminal
python -m unittest discover -s tests -t .
```

The first market load takes about 30 seconds with a Kalshi key, or about 70 without.
After that, two loops run in parallel:

- **Hot list:** the ~3,000 markets within 3¢ of an arb are re-checked every ~5 seconds.
- **Full sweep:** every watched contract is re-checked continuously, taking ~25 seconds
  per pass.

Market lists reload in the background every 5 minutes. Use `--port N` if 8791 is taken.

For each opportunity, the dashboard shows how much to bet on each side: contracts,
average price, cash needed, and fee. Click a row, or tick **Show math for all**, to see
the math:
- cost per pair after fees;
- the guaranteed payout;
- a table of every possible outcome showing what each leg pays and your profit or loss.

**Max to invest per arb** re-sizes every opportunity to your stake, using the same
order-book depth and fee rounding.

More filters above the table (the browser remembers them):

- **Hide Polymarket Buy NO:** hides any trade that buys NO on Polymarket. In the app that's
  **Buy → No**. Under the hood Polymarket US has no NO shares: it sells YES for you at the bid and
  holds $1 as margin, so each share costs 1 − bid, the price the dashboard shows. There's no extra
  risk compared with buying NO on Kalshi.
- **Hide too-good-to-be-true:** hides any opportunity over 10¢ per contract.
  - These rows sort to the bottom and carry an automatic explanation, for example "Kalshi
    prices its outcome at 3%, Polymarket at 71%".
  - The explanation shows both exact outcome texts, so a wrong match is easy to spot.
- **Simple trades only:** keeps only plain two-leg trades, YES on one site and NO on the other,
  where every result pays exactly $1. Hover a row's "Not simple" note to see what rules it out:
  - different lines, where some results pay $2;
  - a whole-number line, where a push is possible;
  - overtime rules or settlement sources that differ;
  - an auto-matched pair you haven't verified;
  - too good to be true.
- **Settles within N days:** hides anything that ties money up longer.
- **Tabs above the table** (All, Sports, Politics, Economics, Crypto, Culture, Weather, Tech &
  science, Finance) split the opportunities by category and show a count on each. The browser
  remembers the tab you picked.

The opportunity size is the smaller of the two legs' available shares at profitable prices.
Each leg shows how many shares are available at its best price. The details panel adds:

- **Order book right now:** price levels for both legs. **Check live depth** fetches them
  fresh from both exchanges.
- **Match your fill:** after the first leg fills, enter the shares that actually filled and
  the total you paid, fees included. You get:
  - how many shares to buy on the other exchange;
  - the break-even price;
  - how many shares are available at or below it;
  - the limit price to use and your locked-in profit.

  A row you've started entering stays pinned on screen until you press **Done – unpin**,
  even if the arb disappears.

### Optional: Kalshi API key (faster scans)

Without a key, Kalshi allows about 4 requests per second. With a key, the scanner reads
your tier's budget from `GET /account/limits` and uses 85% of it. The Basic tier allows
about 17 market-data requests per second.

1. `pip install cryptography`
2. Copy `.env.example` to `.env`.
3. Set `KALSHI_API_KEY_ID` to your key ID.
4. Put the downloaded private key file in this folder as `kalshi.key`. Alternatively, point
   `KALSHI_PRIVATE_KEY_PATH` at wherever the file lives.

`.env` and `*.key` are git-ignored. The key is only used to sign read requests, and the
scanner never places orders. The dashboard header shows which access mode is active.

## Politics, economics, culture & more (matched automatically, you check them)

Non-sports markets have no team codes, so the scanner matches them by wording. Every match
it finds is scanned right away. When one shows up as an opportunity it's marked
**Auto-matched** with **Correct match** / **Wrong match** buttons: read both rules there
before trading.

Three kinds of pair are never scanned automatically, because they are always fake arbs:

- prices that mirror each other;
- prices 25 or more points apart;
- rules that name different data providers.

These wait for you in the review list instead. To go back to approving only confident
matches, raise `AUTO_MIN_EVENT_SCORE` and `AUTO_MIN_OUTCOME_SCORE` in `arb/config.py`
(0.5 and 0.3 were the old values).

1. **Suggestions.** Every 30 minutes, every open Polymarket non-sports question is compared
   with Kalshi's events. Matching uses rare shared words, numbers, and state and district
   codes. It penalizes different districts, months, offices (House vs Governor), and market
   types ("margin" vs "winner").
2. **Outcome pairing.** Within a matched question, outcomes such as candidates or price
   thresholds are paired. A range ("20–25%") is never paired with a threshold ("25+").
3. **Review on the dashboard.** The *Politics, economics, culture & more* section shows the
   pairs held back for review, with both prices, a hint, and both rules. The hint says "prices agree → likely Same"
   or "prices mirror → likely Opposite". For each pair, click one of:
   - **Same:** YES on both sites is the same outcome.
   - **Opposite:** Polymarket YES is Kalshi NO.
   - **Reject.**
   - **Not the same question:** hides the whole card.
4. **Scanning.** Auto-matched and approved pairs join the normal scan within seconds. The opportunities
   table, the math, "Match your fill" and Make trade all work on them.

Decisions are saved in `matches.json`. Remove an approved pair from the *Approved pairs*
list on the dashboard.

Safety checks on every match, including pairs approved earlier:

- **Different years:** a pair whose years differ is never scanned, for example the 2026 Nobel
  on Polymarket against `KXNOBELPEACE-27` on Kalshi. The dashboard lists any such saved pair so
  you can remove it.
- **Different times of day:** hourly crypto and index markets pair only when the times match,
  so "5pm ET" never pairs with "12pm ET". A UTC time counts as either its EDT or EST equivalent.
- **Different settlement sources:** a row whose two rules name different price feeds or weather
  stations gets a warning, for example CF Benchmarks on Kalshi and Binance on Polymarket. The
  two feeds can disagree right at the line, and then both legs can lose.

- **Different data providers:** a pair is never auto-matched when the two rules name different
  providers for the same kind of number: price feeds, weather stations, music charts (Spotify vs
  Luminate/Billboard), wealth rankings (Forbes vs Bloomberg), or AI leaderboards (LiveBench vs
  LMArena).
- **"90+" vs "Above 90":** these never pair. On a whole-number score such as Rotten Tomatoes or a
  seat count, a result of exactly 90 loses both legs.
- **One-way rules:** some Kalshi markets also resolve YES on an *announcement*, for example "leave
  office or announce leaving", while Polymarket needs the event itself. Kalshi YES with Polymarket NO
  is safe. A trade holding Kalshi NO gets a ONE-WAY RULES warning, because an announcement alone
  loses both legs.

The **Non-sports tabs** table shows, for each category tab on each site, how many markets are
open and how many outcome pairs the matcher found. Tabs with pairs on both sites are where
non-sports arbs can show up.

## Crypto (paired automatically, no approval needed)

These markets all settle on the same number: the 60-second average of CF Benchmarks' Bitcoin index
(BRTI) at a fixed instant.

- Polymarket US **Up/Down windows** (15-minute and 60-minute). "Up" means the close is at or above
  the window's opening price.
- Kalshi's **15-minute Up/Down** markets (`KXBTC15M`).
- Kalshi's **hourly price ladder** (`KXBTCD`: "above $83,700 at 8 pm").

Every 20 seconds the scanner loads them for each coin Polymarket lists, and groups markets that
settle at the same instant. Each becomes a threshold on the close in cents, so the engine finds:

- **Exact twins:** Polymarket 15-minute Up vs Kalshi 15-minute Up, same window and opening price.
  One leg pays whatever happens.
- **Cross-strike pairs:** for example Polymarket "Up from $83,642.70" plus Kalshi "NOT above
  $83,699.99". That pays $1 whichever way it goes, and $2 if the close lands between the two.

Rows show under the **Crypto** tab, with the outcome table in dollars. A window drops out the moment
it closes. Kalshi averages the 60 seconds before the close and Polymarket the 60 prices ending at it,
so the two can differ by one second's move; that only matters if the close lands within a few
dollars of a line. ETH, SOL, XRP, DOGE, BNB and HYPE pair the same way as soon as Polymarket lists
them.

## ⚡ Fast trade and Auto-trade (crypto and other time-sensitive arbs)

Some arbs last only seconds, so these skip the confirm screen. They run the same safe sequence as
**Make trade**:
1. Check live order books and balances.
2. Buy the side with the thinner order book first.
3. Buy the other side for exactly what filled, never above break-even.
4. Sell back right away any shares that couldn't be hedged.

**Which rows qualify:** anything settling within `FAST_MAX_HOURS` (24 hours by default), crypto
included. That includes pairs auto-matched by wording that you haven't checked
(`FAST_ALLOW_AUTO_MATCHED`), and rows flagged too good to be true, so any return of at least
`AUTO_TRADE_MIN_ROI` qualifies, with no upper limit (`FAST_ALLOW_TOO_GOOD`). Live prices are always
re-checked before ordering. Rows with a one-way-rules, different-settlement-source or prices-contradict
warning never qualify. A wrong auto-match can lose on both sides, so set either setting to `0` to
require your check.

- **⚡ Fast trade** (a button on qualifying rows): one click places both orders, up to `FAST_MAX_TRADE`
  (and your **Max to invest**, if you set one).
- **Auto-trade** (the bar above the tabs): places qualifying arbs by itself, one at a time.
  - It's **off every time the scanner starts**, and you're asked once when you turn it on.
  - It only trades when the profit at live prices is at least `AUTO_TRADE_MIN_PROFIT`.
  - It never spends more than `AUTO_TRADE_MAX_TRADE` per trade or `AUTO_TRADE_DAILY_LIMIT` per day.
  - It waits `AUTO_TRADE_COOLDOWN_SECS` before trying the same pair again.
  - It **turns itself off** (and alerts your phone, if alerts are set up) if a trade leaves shares
    unhedged or an order can't be confirmed.
  - It skips games already in play (`AUTO_TRADE_LIVE_GAMES=1` to allow them). In play, prices move
    between the two orders and Polymarket can delay in-play orders, so the second leg often misses. The
    first leg is then sold back at a small loss: those are the **partial** lines in *Last auto-trades*.
  - After a partial or no-fill it leaves that whole game alone for 10 minutes
    (`AUTO_TRADE_GAME_COOLDOWN_SECS`), and it turns itself off once it has lost
    `AUTO_TRADE_MAX_DAILY_LOSS` ($5) net in a day.
  - Every trade goes to My arbs and `trades.jsonl`.

Limits, in `.env` (defaults shown):

```
FAST_MAX_HOURS=24
FAST_ALLOW_AUTO_MATCHED=1       # 0 = only pairs you or the sports matcher verified
FAST_ALLOW_TOO_GOOD=1           # 0 = skip rows flagged too good to be true
FAST_MAX_TRADE=50
AUTO_TRADE_MAX_TRADE=25
AUTO_TRADE_DAILY_LIMIT=100
AUTO_TRADE_MIN_PROFIT=0.50
AUTO_TRADE_MIN_ROI=0.5          # percent
AUTO_TRADE_COOLDOWN_SECS=60
```

Crypto twins settle on the same CF Benchmarks index, but Kalshi averages the 60 seconds *before* the
close and Polymarket the 60 seconds *ending at* it. A close within a few dollars of the line could, rarely,
split them.

## Sizing to your cash

With your API keys in `.env`, the scanner reads the cash on each site every 15 seconds (and right
after **Make trade**). The header shows it as **Cash: Kalshi $X · Polymarket $Y**. With
**Size to my cash** ticked (the default), each opportunity is sized to the most shares you can
actually buy. The Kalshi leg has to fit your Kalshi balance, and the Polymarket leg has to fit your
Polymarket buying power, fees included. Your **Max to invest** still applies on top of that.

- A row that was cut down says why under its share count, for example
  *of 1,400 · limited by your Kalshi cash*.
- Arbs you can't afford even one share of are hidden, with a count above the table
  (*3 more arbs hidden: not enough cash*).
- Untick **Size to my cash** to size by the order books alone.

## My arbs (your active trades)

The **My arbs** tab, at the right end of the tabs, tracks the arbs you've actually placed:

- **Live position check:** about once a minute, and when you click **Sync now**, it reads your open
  positions on both sites with the API keys in `.env`. Kalshi and Polymarket positions that together
  form an arb the scanner knows about are paired and added automatically, with the shares held and
  what they cost. Positions with no partner on the other site are listed under **Only on one site**.
  If you edit an arb found this way, the sync stops changing it.
- **Make trade** adds each trade that hedged at least one pair, with the shares and costs that filled.
- **Track this arb** on any opportunity opens a form filled in with the dashboard's sizing. Change
  it to what actually filled on each site (shares, and the total paid including fees), then save.
  **Add an arb by hand** does the same for anything else.

For each arb it shows:

- your two orders and what each cost;
- what the pairs pay back whatever happens, and your locked profit;
- any shares held on one side only (counted as worth $0);
- what both legs would sell for right now;
- each market's live state: open, closed, or settled with Kalshi's result.

Arbs move to **Settled or closed** once both markets are done. Edit or delete them any time.
Everything is saved on your computer in `my_arbs.json`, which updates never touch.

## Checking what the dashboard found

With the dashboard running, run this in a second window:

```
python -m arb.verify
```

For every opportunity it:

- re-fetches both order books live;
- recomputes the profit at today's depth;
- sorts the row as **LEGIT**, **GONE**, **SUSPECT** (too good to be true), **WRONG** (different
  source, year or time), or **TRAP** (this direction can lose both legs).

LEGIT means no problem was found, not that there is none, so still read both rules before trading.

## Make trade (places real orders)

Trading turns on when both a Kalshi key and a Polymarket US key are set in `.env`. The
dashboard header shows `Trading: on`. Clicking **Make trade** on an opportunity:

1. **Plans (nothing is sent yet).**
   - Re-fetches both order books and both account balances live.
   - Sizes the trade to the smaller leg's depth at profitable prices.
   - Caps the size at **$100 per trade** (`MAX_TRADE_DOLLARS` in `arb/config.py`), at
     "Max to invest", and at each account's cash.
2. **Shows a confirm dialog** with both orders, limit prices, costs, and expected profit.
   The prices are valid for 20 seconds.
3. **Places the first leg** on the thinner book, as an immediate-or-cancel limit order.
4. **Places the second leg** for exactly the shares that filled, capped at the break-even
   price. It retries twice on fresh prices.
5. **Sells back** any first-leg shares that still aren't hedged, straight away.
6. **Shows the result:**
   - hedged pairs;
   - locked profit;
   - sell-back gain or loss;
   - any shares left unhedged, in red.

Every order and response is appended to `trades.jsonl` in this folder.

Trade requests need a token that only the dashboard page receives, so other websites can't
trigger orders. The server listens on localhost only.

## How it works

1. **Load markets.** Loads every open sports market in the configured leagues
   (`arb/config.py`). From Polymarket it uses the slug, market type and line. From Kalshi it
   uses the series name, ticker and strike.
2. **Match games.** Matches games by league, date, and team codes. When codes differ, it
   falls back to team names, e.g. Kalshi "Los Angeles R" = Polymarket "Los Angeles Rams".
3. **Normalize contracts.** Rewrites every contract as a condition on one game number:
   winning margin, combined total, or one team's total, each per period. For example:
   - Polymarket "PIT +1.5" becomes *margin(PIT) > −1.5*.
   - Kalshi "CLE wins by over 7.5" becomes *margin(PIT) < −7.5*.
4. **Find guaranteed pairs.** For each pair of positions, one per exchange, it checks every
   possible final score and takes the worst-case total payout. This covers two cases:
   - same-line arbs, such as YES on one exchange and NO on the other;
   - cross-line arbs. For example, "wins by more than 3.5" plus "NOT wins by more than 6.5"
     always pays at least $1, and pays $2 when the margin lands between.
5. **Size against depth.** It screens pairs on top-of-book prices after fees. For
   promising pairs it fetches both order books and adds contracts while each extra one
   still makes money. The reported profit uses each exchange's rounding rules:
   - Kalshi rounds the fee up to the cent.
   - Polymarket uses banker's rounding.

## Live prices (WebSocket streams)

With your API keys in `.env`, the scanner opens a live stream to each exchange and subscribes to up
to 2,000 markets on each: every near-arb found by the full sweep, plus every non-sports and crypto
pair. Each price change re-checks just the pairs that market is in, within about 0.1 seconds,
instead of waiting for the next poll. The header shows **Live prices: Kalshi ● N mkts ·
Polymarket ● N mkts**. The full sweep keeps polling everything else and adds new near-arbs to the
streams as it finds them.

Streams need the `websocket-client` package; `start-dashboard.bat` installs it the first time. With
no keys or no package, the scanner polls as before. A dropped stream reconnects by itself, and a
missed Kalshi update forces a fresh book.

If a stream goes quiet (no message for 90 seconds) it reconnects by itself. Every full sweep still
polls every market as a backstop, and a streamed market is only trusted without polling while it has
updated in the last minute, so a silent stream can't freeze prices. The header shows each stream's
state, for example "Kalshi ● 640 of 1,900 live", or "quiet 95s (polling)". Hover over it for its
subscriptions, reconnects and last error.

## On your iPhone

The dashboard works on a phone. On a narrow screen opportunities show as cards, the filters fold
under **Filters & settings**, and every button is big enough to tap.

1. Add a password to `.env` (at least 8 characters). It's needed because the dashboard can place trades:
   ```
   DASHBOARD_PASSWORD=pick-something-long
   ```
2. Start the dashboard as usual (`start-dashboard.bat`). With a password set, it serves your phone
   too. The console prints the address to open, like `http://192.168.1.20:8791`.
   (`start-dashboard.bat --local` keeps it on this computer only.)
3. On the iPhone, on the same Wi-Fi, open that address in Safari and log in with the password.
4. In Safari, tap **Share → Add to Home Screen**. It then opens full-screen like an app.

If the page doesn't load, Windows Firewall is blocking it: allow Python on **Private networks** when
Windows asks, or under *Windows Security → Firewall → Allow an app through firewall*.

On this computer the dashboard still opens at `http://localhost:8791` with no login. Every other
device has to log in, and the login lasts 90 days, or until you change the password. After 10 wrong
passwords from a device, that device has to wait 5 minutes.

Away from home: phone mode only works on your own Wi-Fi. Don't open the port on your router.
Instead install [Tailscale](https://tailscale.com) (free) on the PC and the phone, and use the PC's
Tailscale address in place of the Wi-Fi one. The connection is then encrypted and private to your
devices.

## Kalshi exchange shards (crypto and some sports)

Kalshi now runs some markets on separate exchange "shards": crypto and commodities on shard 2, and
tennis, baseball and basketball on shard 3. Everything else is on the main shard 0. **An order can only
use cash held on its own market's shard.** With all your cash on shard 0, a crypto order fails with
`insufficient shard balance`.

- The header shows your Kalshi cash per shard (e.g. `Kalshi $505.00 (#0 $500.00, #2 $5.00)`). Each arb
  is sized to the cash on its own shard, and a row cut down by it says *limited by your Kalshi cash
  on shard 2*.
- **Automatic (on by default):** before a trade on a shard that's short, the app moves just the cash
  that trade needs onto it from your other shards (richest first; it's your own money on the same
  account, nothing leaves Kalshi). It waits up to 8 seconds for the cash to arrive, then sizes the trade
  to what's there. The confirm screen shows what was moved. Because of this, arbs are sized to your
  total Kalshi cash. To turn it off, put `KALSHI_AUTO_SHARD_FUNDING=0` in `.env`.
- **Or keep a fixed split:** double-click `kalshi-shards.bat` (or run `python -m arb.shards`). It shows your
  cash per shard, asks for a split (Enter = 50% main, 30% crypto, 20% tennis/baseball/basketball), and
  turns on Kalshi's automatic rebalancing. Kalshi then moves cash between your shards every 10
  seconds to keep that split. Run it again to change the split; `100 0 0` turns it off. You can also
  move cash by hand at kalshi.com/account/exchange-indexes.
- **Make trade**, **Fast trade** and **Auto-trade** check the market's shard before ordering. If it's
  empty they tell you, and nothing is traded.

## Faster Kalshi scanning (free Advanced tier)

Kalshi's free Basic tier allows 20 requests a second. The Advanced tier allows 30, and it's free and
permanent once at least 1 of your last 100 Kalshi orders was placed through the API (a **Make trade**
counts; orders placed on kalshi.com don't). Double-click `upgrade-kalshi.bat` (or run
`python -m arb.upgrade`) once. It prints your tier before and after. Then restart the dashboard:
it reads your tier at start-up and uses the higher limit by itself.

## Fast restarts (warm start)

Matching every market on both sites takes a few minutes. So every 5 minutes the scanner saves its
matches and its near-arb list to `cache/warm.pkl` (git-ignored). On the next start it begins checking
prices on those saved matches within seconds, and the live streams pick up the saved near-arb markets
straight away, while fresh market lists load in the background.

Only the matching is reused. Every price is fetched live before anything is shown, and markets that
closed in the meantime just drop out. A cache more than 12 hours old is ignored. To force a completely
fresh start, delete the `cache` folder.

## Maker mode

The **Maker mode** tab lists pairs that aren't arbs when you take both prices, but become profitable
if you post the Polymarket leg as a resting limit order instead. A resting (maker) order earns a
rebate of 0.0125 × p × (1 − p) per share instead of paying the 0.0695 taker fee, and the scanner posts
it one tick better than the current best price. Each row's details say exactly what to do:

1. On Polymarket, post the Buy Yes/No limit order shown, Good 'til canceled.
2. As soon as any shares fill, buy the same number on Kalshi at no more than the hedge limit.
   **Match your fill** works out the exact numbers.
3. If Kalshi moves past the hedge limit first, cancel the Polymarket order.

The risk: a resting order tends to fill just as the price moves against you, and Kalshi may have
moved too by then. So maker mode only looks at Polymarket markets whose bid–ask gap is 3¢ or less,
needs at least 0.5¢ profit per pair, and sizes each row to at most $1,000 (`MAKER_MAX_CAPITAL`,
`MAKER_MAX_SPREAD` and `MAKER_MIN_EDGE` in `arb/config.py`). **Make trade** doesn't place maker
orders: place them yourself.

## Alerts

**On the dashboard:** above the table, set **Alert at $** (default 5). When a new arb at or above
that profit appears, the dashboard beeps, puts "(1 new)" in the tab title and, with **Desktop
notification** ticked, shows a Windows notification. An arb that stays on screen, or drops off and
comes back within 10 minutes, doesn't alert again.

**On your phone, with the dashboard closed or minimized:** add either of these to `.env`, restart,
then press **Test phone alert**:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   # channel settings → Integrations → Webhooks
TELEGRAM_BOT_TOKEN=123456:ABC...                          # create a bot with @BotFather
TELEGRAM_CHAT_ID=123456789                                # message your bot, then open
                                                          # https://api.telegram.org/bot<token>/getUpdates
ALERT_MIN_PROFIT=5            # optional, dollars
ALERT_COOLDOWN_MINS=30        # optional: repeat an arb only after this long, or once its profit grows by half
```

Each message says what to buy on each site, with shares and limit prices. Rows marked too good to
be true, or with ONE-WAY RULES, different settlement sources or contradicting prices, never alert.

## Things the numbers assume

- **Taker fees only.**
  - Kalshi: `0.07 × fee_multiplier × C × P × (1−P)`. The multiplier is the series' own, unless
    Kalshi overrides it for one event (playoff games often go from 0.5× to 1×). The scanner loads
    these overrides and uses the higher rate if one starts before the next reload. Kalshi rounds
    the order's cost + fee up to the cent, which matters on sub-cent prices such as 12.3¢.
  - Polymarket: `feeCoefficient × C × p × (1−p)` (currently 0.0695).
- **Buy NO on Polymarket US is a short of YES.** You receive the bid and $1 is held as margin, so
  each share costs `1 − bid`, the same as buying NO. The dashboard shows it as Buy NO at `1 − bid`.
- **Whole-number lines assume a push pays nothing on either side.** This is conservative.
  Such rows carry a warning.
- **Ties and draws.** An NFL moneyline tie pays $0.50 on both exchanges. Basketball,
  hockey, MLB and college football full games cannot end level. Soccer, NPB and KBO can.
- **Settlement rules can differ.** A postponed game settles at "fair price" after 48 hours
  on Kalshi, but only after two weeks on Polymarket. Rows flag differing overtime wording,
  and each row shows both exchanges' full rules.
- **Quotes from the two exchanges are seconds apart.** Re-check both books before trading.

## Layout

| File | Purpose |
|---|---|
| `arb/config.py` | Hosts, rate limits, league mapping, fees, scan intervals |
| `arb/kalshi.py` / `arb/polymarket.py` | Fetch and parse each exchange |
| `arb/matching.py` | Game matching and conversion to the shared contract model |
| `arb/model.py` | Contract model, payout math, fee rounding |
| `arb/engine.py` | Pair screening, order-book sizing, dashboard rows |
| `arb/scanner.py` / `arb/server.py` | Background loop and local dashboard |

To add a league, add its Polymarket slug code and Kalshi series code to `LEAGUES` in
`arb/config.py`. The Coverage table on the dashboard shows how many games matched.
