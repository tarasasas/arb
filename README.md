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
| `start-dashboard.bat` | Starts the scanner and opens the dashboard. Close the window to stop it. |
| `scan-once.bat` | Runs one scan and prints the results in the window. |
| `run-tests.bat` | Runs the unit tests. |
| `setup.bat` | One-time setup for the Kalshi API key: installs `cryptography` and creates `.env`. |
| `update.bat` | Gets the latest code from GitHub. Keeps your `.env`, `kalshi.key` and trade log, and throws away any other edits to the code. |

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

- **Hide Polymarket shorts:** hides any trade that shorts on Polymarket. Polymarket US has no NO
  shares, so taking NO means shorting YES. That costs the same as buying NO: you receive the bid, $1
  is held as margin, and your buying power drops by 1 − bid.
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

## Crypto Up/Down (paired automatically, no approval needed)

Polymarket US "BTC Up or Down: 15 min" and Kalshi `KXBTC15M` "BTC price up in next 15 mins?" are the
same contract. Both settle on the 60-second average of CF Benchmarks' BRTI at the window's open and
close, and a tie counts as Up/Yes on both.

Every 20 seconds the scanner pairs windows with the same coin, the same start and end to the second,
and the same price to beat. These rows are marked as paired by contract terms, count as simple
trades, and settle within 15 minutes. Near 50/50 both taker fees add up to about 3.5¢, so an arb
appears only when one site lags the other during a fast move.

Other coins pair automatically as soon as Polymarket lists them. Kalshi already runs ETH, SOL, XRP,
DOGE and more. Polymarket's 60-minute windows have no Kalshi twin, because Kalshi only runs
15-minute ones.

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

## Things the numbers assume

- **Taker fees only.**
  - Kalshi: `0.07 × series fee_multiplier × C × P × (1−P)`.
  - Polymarket: `feeCoefficient × C × p × (1−p)` (currently 0.0695).
- **Shorting on Polymarket US is the NO side.** You receive the bid and $1 is held as margin, so
  buying power drops by `1 − bid`, the same as buying NO. The capital figure uses `1 − bid`.
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
