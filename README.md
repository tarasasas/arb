# Kalshi × Polymarket US arbitrage scanner

Finds guaranteed-profit pairs across Kalshi and Polymarket US sports markets: moneylines,
spreads, totals, team totals, and period markets (halves, quarters, hockey periods, MLB
first 5 innings and innings), plus tennis match winners, first-inning runs, both teams to
score, soccer exact scores, and MLB/NFL/NHL player props. Scanning works without API keys, and a Kalshi key makes it
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

- **Hot list:** the 400 pairs closest to an arb (within 3¢, `HOT_MAX_PAIRS`) are re-checked every
  ~2 seconds.
- **Full sweep:** every watched contract is re-checked continuously. It reads Kalshi's best prices
  from the market list (200 markets per request), then fetches order books only for the pairs within
  3¢ of an arb, at the same moment as their Polymarket books. A pass takes ~40 seconds without a
  Kalshi key, less with one.

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

Questions about different time windows aren't paired at all: "How low will BTC get in October?" is
not "How low will Bitcoin get this year?" (a dip in November loses both legs). The window comes from
the wording ("this week", "in October", "this year", "in 2026"), because both sites' end dates are too
loose to compare (Polymarket's run ~2 weeks late, Kalshi's up to a year).

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

Some arbs last only seconds, so these skip the confirm screen. Auto-trade runs this sequence
(`AUTO_TRADE_ORDER=thinner_first`, the default):
1. Check live order books and balances.
2. Buy the side with the thinner order book first. If it misses, nothing is traded.
3. Buy the other side for exactly what filled, with its limit at break-even. Immediate-or-cancel orders
   fill at the resting prices, so this costs nothing when the book held still, and still hedges when it
   moved a tick. Up to 2 retries, each sent as soon as the live stream shows shares back at break-even
   (at most 0.25s later).
4. Close any shares that still couldn't be hedged, whichever way loses less: sell them back, or hedge
   them up to 5¢ a share above break-even (`CLOSE_OUT_MAX_LOSS`; `0` = always sell back).

⚡ Fast trade sends its orders per `TRADE_ORDER` (below), with the same steps 3 and 4.

**Which rows qualify:** anything whose result is known within `FAST_MAX_HOURS` (24 hours by
default), crypto included. That's when the game ends or the event happens, even if a site pays out
later: for a non-sports pair it's the earlier of the two sites' dates, because Polymarket's end dates
often run weeks past the event. That includes pairs auto-matched by wording that you haven't checked
(`FAST_ALLOW_AUTO_MATCHED`), and rows flagged too good to be true, so any return of at least
`AUTO_TRADE_MIN_ROI` qualifies, with no upper limit (`FAST_ALLOW_TOO_GOOD`). Live prices are always
re-checked before ordering. Rows with a one-way-rules, different-settlement-source or prices-contradict
warning never qualify. A wrong auto-match can lose on both sides, so set either setting to `0` to
require your check.

- **⚡ Fast trade** (a button on qualifying rows): one click places both orders, up to `FAST_MAX_TRADE`
  (and your **Max to invest**, if you set one).
- **Auto-trade** (the bar above the tabs): places qualifying arbs by itself, one at a time.
  - It's **off every time the scanner starts**, and you're asked once when you turn it on.
  - It only trades when the return at live prices is at least `AUTO_TRADE_MIN_ROI` (0.5%), at the size
    it can actually take (capped per trade). `AUTO_TRADE_MIN_PROFIT` adds an optional dollar floor (off).
  - It never spends more than `AUTO_TRADE_MAX_TRADE` per trade or `AUTO_TRADE_DAILY_LIMIT` per day.
    Today's spend and net are kept in `cache/auto_trade_day.json`, so the daily limit and the daily loss
    stop hold across restarts.
  - It waits `AUTO_TRADE_COOLDOWN_SECS` before trying the same pair again, and only takes rows the
    scanner checked in the last `AUTO_TRADE_MAX_ROW_AGE` seconds (5).
  - It **turns itself off** (and alerts your phone, if alerts are set up) if a trade leaves a whole share
    or more unhedged (`AUTO_TRADE_STOP_UNHEDGED`; a smaller leftover is a fraction no site lets you sell)
    or an order can't be confirmed. An order whose answer was lost (a timeout or dropped connection) is
    looked up first: on Kalshi by its client order ID, on Polymarket among the orders your private stream
    reports since, matched on market, intent, quantity, price and time in force. Only an exact single
    match counts, so a lost answer alone no longer stops it.
  - If `trades.jsonl` can't be written (Excel locks a file it has open), the trade goes to
    `trades.pending.jsonl` instead and the log says so.
  - It skips games already in play (`AUTO_TRADE_LIVE_GAMES=1` to allow them). In play, prices move
    between the two orders and Polymarket can delay in-play orders, so the second leg often misses. The
    first leg is then closed at a small loss: those are the **partial** lines in *Last auto-trades*.
  - It skips crypto Up/Down windows (BTC, ETH and other coins' price at a set minute;
    `AUTO_TRADE_CRYPTO_WINDOWS=1` to allow them). The price moves every second, so the second leg often
    misses there too. Fast trade by hand can still take them.
  - After a partial or no-fill it leaves that whole game alone for 10 minutes
    (`AUTO_TRADE_GAME_COOLDOWN_SECS`), and it turns itself off once it has lost
    `AUTO_TRADE_MAX_DAILY_LOSS` ($5) net in a day.
  - Fail-safes against one site always missing: it only trades a size the second
    leg's book can cover twice over within break-even (`AUTO_TRADE_HEDGE_DEPTH`=2),
    the second leg's first order already allows up to break-even
    (`SECOND_LEG_AT_BREAKEVEN`=1; IOC still fills at the best prices), every miss is
    logged with the site and the reason (rejection text, or the price there now vs
    the order's limit), and it turns itself off after 3 misses in a row on one site
    (`AUTO_TRADE_MAX_MISSES`). `trade-report.bat` shows the same `MISSED` lines.
  - Every trade goes to My arbs and `trades.jsonl`.

**How Auto-trade keeps failed trades down** (all in ⚙ Settings → How Auto-trade trades):

- **Stale side first** (leg order `smart`, the default). A cross-site arb usually exists because one site
  hasn't repriced yet, and it's about to. When the live streams show one site's best price just moved
  (within 2s) and the other's hasn't for 3s more, the stale one goes first, before it reprices. Changes
  deeper in a book, and the first book after subscribing, don't count as moves.
- **The site that keeps missing goes first.** A first order that misses trades nothing; a second one that
  misses leaves the first leg to be sold back at a loss. So once one site has missed at least 3 times and
  twice as often as the other (over that market type's last 20 tries, or all types' while it has fewer),
  that site's order goes first. Fast markets
  (crypto windows, games in progress), and market types whose second legs keep missing, send both orders at
  once instead; anything else sends the thinner book first. Each trade's history line says which and why,
  and where both planned prices came from ("Kalshi NO ≤ $0.690 (live feed, last changed 0.3s before)"),
  so a miss shows whether the quote it relied on was old.
- **More edge where prices move fast.** Crypto windows and games in progress need `AUTO_TRADE_FAST_EDGE`
  (2¢) a pair. Every market type also learns its own buffer: the typical (75th percentile) price move its
  trades met while the orders went out (`AUTO_TRADE_LEARN_BUFFER`).
- **Market types that keep failing pause themselves.** Results are kept per type (MLB, NFL live, Crypto
  windows, Politics, ...). When fewer than 40% of a type's last 5 real trades filled on both sites, or they
  lost money in total, that type pauses for 2 hours. **By market type** in the Auto-trade bar shows each
  type's record, what edge it needs and why, and a Resume button. Kept in `cache/exec_stats.json`.
- **Never the whole book.** A trade takes at most half of the shares each book shows at the prices paid
  (`AUTO_TRADE_BOOK_SHARE`): shown shares are often gone, or pulled, by the time an order lands.
- **Paper trading** (`AUTO_TRADE_DRY_RUN=1`). Auto-trade picks and plans trades as usual but sends
  nothing. It reads the real books at the moments each order would have landed (your measured order
  times, else 0.15s Kalshi and 0.7s Polymarket) and applies the same rules: second leg at break-even with
  retries, leftovers closed the cheaper way. Results show as "paper" in the bar, in their own column
  under By market type, and in `paper_trades.jsonl`. They teach the buffers but never pause a type.
  Paper fills assume the shown shares were really there, so real fills can only be the same or worse.

**Fast lane.** A full sweep of every market takes 30-90 seconds, too slow for arbs that last
seconds. While Auto-trade or Auto maker is on, the pairs Auto-trade could take (result known within
`FAST_MAX_HOURS`, games in progress left out unless `AUTO_TRADE_LIVE_GAMES=1`) get their own price
check about every half second (`FAST_LANE_PAUSE_SECS`), and their markets get the first live-stream
slots. That's a tenth or less of all markets (about 600 pairs on a typical day), so with an
Advanced-tier Kalshi key a pass takes a second or two. The Auto-trade bar shows "Fast lane: N pairs
decided within 24h, checked every Xs". `FAST_LANE=always` runs it all the time (for Fast trade by
hand too), `off` never; also in ⚙ Settings.

**Auto-trade mode.** While Auto-trade is on, only the markets it can take are refreshed: the full
sweep, near-arb re-checks, live-feed re-checks of other markets, non-sports suggestions and the My
positions check pause, so the fast lane and Auto-trade's own checks get the whole request budget.
Market lists, your cash and Kalshi shards keep refreshing. The header says "Auto-trade mode". Turn it
off under ⚙ Settings (`AUTO_TRADE_FOCUS=0`) to keep everything refreshing. A trade's own requests
always go ahead of every other request.

**⚙ Settings** (top right, and next to the Auto-trade switch) changes all of these from the dashboard:
what Auto-trade and Fast trade may take (auto-matched pairs, too-good-to-be-true rows, player props,
games in progress), the minimum return, the per-trade and per-day limits, the hard cap per trade
(`MAX_TRADE_DOLLARS`, default $100), leg order, and Auto maker's limits. A change applies at once and
is saved to `.env`, the same lines you can still edit by hand.

Defaults (in `.env`):

```
FAST_MAX_HOURS=24
FAST_ALLOW_AUTO_MATCHED=1       # 0 = only pairs you or the sports matcher verified
FAST_ALLOW_TOO_GOOD=1           # 0 = skip rows flagged too good to be true
FAST_ALLOW_PLAYER_PROPS=1       # 0 = skip player props (a player who doesn't play settles at fair prices)
FAST_MAX_TRADE=50
AUTO_TRADE_MAX_TRADE=25
AUTO_TRADE_DAILY_LIMIT=100
AUTO_TRADE_MIN_PROFIT=0         # optional dollar floor; ROI minimum below is what counts
AUTO_TRADE_MIN_ROI=0.5          # percent
AUTO_TRADE_COOLDOWN_SECS=60
AUTO_TRADE_ORDER=smart          # or thinner_first / together / polymarket_first
AUTO_TRADE_FAST_EDGE=2          # cents a pair in crypto windows and games in progress
AUTO_TRADE_LEARN_BUFFER=1       # plus each market type's typical price move
AUTO_TRADE_THROTTLE=1           # pause a type below 40% filled, or losing, over its last 5
AUTO_TRADE_BOOK_SHARE=50        # percent of each book's shown shares a trade may take
AUTO_TRADE_DRY_RUN=0            # 1 = paper trading
CLOSE_OUT_MAX_LOSS=0.05         # $/share above break-even a leftover may be hedged at; 0 = sell back
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
- **Real cost basis.** Each position check also sets every tracked leg's cost to what your account says
  you actually paid for those shares, fees included, at the account's average cost per share. Profit and
  ROI then use the real prices rather than the planned or typed ones. A leg updated this way shows
  "(real cost, was $X)". Legs whose account cost is uncertain (some Polymarket Buy No positions) keep
  the recorded cost.
- **Is each one really an arb?** Every check also re-verifies each tracked arb.
  - **Structure:** from the two markets' rules, your positions must pay out whatever happens. Two bets
    that can both lose are flagged "Not an arb: …". The payout per pair is refreshed from the rules.
  - **Economics:** at your real cost basis, the payout per pair must beat what you paid per pair.
    Otherwise it's flagged "Not an arb at your cost: you paid $1.03 per pair for a $1.00 payout".
  - Good ones show "Arb at your cost: $0.03 per pair locked". The Active arbs card counts the ones that
    aren't.
  - Pairing your positions uses every matched market, whatever Focus is set to.
- **Balance** (on an arb whose legs hold different share counts): checks both live books and shows the
  two ways to even it up, selling the extra shares or buying the missing ones on the other site,
  with what each leaves you after fees. It recommends whichever leaves more money (shares an option
  can't cover count as $0) and sends nothing until you pick one. The order is immediate-or-cancel at
  no worse than the price shown. A sale's gain or loss against what those shares cost is kept in the
  arb's profit. Extra shares under one share (Polymarket fills fractions on a buy by dollar amount)
  can be balanced too, as long as the site takes an order that small.
- **Sales you make yourself are followed.** If your live position in a tracked leg is smaller than
  recorded (you sold some or all of it on the site), the leg is cut to what you still hold, with its
  cost cut pro rata. An arb with a leg sold out moves to **Closed early**, with a note saying what
  you sold, and the shares you still hold show under **Only on one site**. Only legs whose market is
  still open are changed: when a market settles its position disappears too, and that isn't a sale.
- **Payouts by day:** a chart at the top shows how much your active arbs pay out on each calendar day
  over the next 7, 30 or 90 days, then by week for 6 months and by month for 1 year or All. It counts
  the guaranteed payout, on the day the later of the two
  markets closes. Hover over (or tab to) a day to see its arbs and how much of it is profit. The note
  under the chart covers arbs past their close date that are still waiting to settle, and payouts beyond
  the range. **Show as table** lists the same numbers.
- **Paid out:** once both markets of an arb have paid out (Kalshi finalized, Polymarket resolved), the
  arb moves to the **Paid out** section with what it really paid. Each leg's payout comes from its
  market's result, including any extra shares held on one side, and the time comes from Kalshi's
  settlement. Cards show the total paid out, the profit made and its return on the money put in, and the
  last 30 days. The payout is saved in `my_arbs.json`, so it stays even after the sites stop listing the
  markets. An arb whose markets are over but hasn't paid yet stays with the active ones, marked
  "waiting for the payout".
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
3. **Re-checks if you took a moment to confirm.** Both books and balances are read again: the
   trade can shrink, never grow, and nothing is sent if the arb is gone.
4. **Places the orders** per `TRADE_ORDER` (below). In `thinner_first`, the first leg goes on the
   thinner book as an immediate-or-cancel limit order, and the second leg is bought for exactly the
   shares that filled, limited at the break-even price. It retries twice.
5. **Closes** any first-leg shares that still aren't hedged, whichever way loses less: sold back, or
   hedged up to `CLOSE_OUT_MAX_LOSS` (5¢) a share above break-even.
6. **Shows the result:**
   - hedged pairs;
   - locked profit;
   - sell-back gain or loss;
   - any shares left unhedged, in red.

Every order and response is appended to `trades.jsonl` in this folder.

Trade requests need a token that only the dashboard page receives, so other websites can't
trigger orders. The server listens on localhost only.

**Order of the two orders** (`TRADE_ORDER` in `.env`):
- `together` (default): both orders go out at the same moment, so neither waits for the other site's
  answer and both land on the prices the scanner saw. If one side fills less, the short side is bought
  again for the difference (never above break-even) and anything still unmatched is sold back.
- `polymarket_first`: Polymarket goes first, then Kalshi is bought for exactly what filled. A Polymarket
  miss trades nothing, but Kalshi's price has the whole Polymarket round trip (often a second) to move.
- `thinner_first`: the book with less depth goes first.

The checks before ordering (market info, order book and cash on both sites) run all at once, and
connections to both sites are kept open between requests, which saves a TLS handshake on every call.

**Pauses inside the app.** Only one thread runs Python at a time, so anything long blocks the stream
re-check and a trade in flight too. Measured with a probe thread on the full app: Python's garbage
collector used to pause everything for 100-560ms several times a minute, and saving the warm-start
file froze it for 1.4s every 5 minutes. Now (`arb/gctune.py`) the long-lived market data is frozen out
of collection after each reload, automatic full collections are rare (`GC_GEN2_THRESHOLD`), a full
one runs at most every 30 minutes and never during a trade, threads swap every 1ms instead of 5ms, and
the warm-start file is written in small pieces. 99.9% of the time the hot path now waits under 5ms
for its turn (was 57-70ms).

**How fast is a trade?** Double-click `latency-test.bat` (or run `python -m arb.latency`). It times each
step a trade goes through on both sites (market info, order book, cash) using the trading code itself.
Then, if you answer `y`, it sends real test orders on each site alone and on both at once: 1 share at a
1¢ limit, immediate-or-cancel, on markets nobody is selling anywhere near 1¢. They cancel unfilled
(worst case about 2¢). The report is saved to `latency-report.txt`.

## How it works

1. **Load markets.** Loads every open sports market in the configured leagues
   (`arb/config.py`). From Polymarket it uses the slug, market type and line. From Kalshi it
   uses the series name, ticker and strike.
2. **Match games.** Matches games by league, date, and team codes. When codes differ, it
   falls back to team names, e.g. Kalshi "Los Angeles R" = Polymarket "Los Angeles Rams".
3. **Normalize contracts.** Rewrites every contract as a condition on one game number:
   winning margin, combined total, or one team's total, each per period. Exact scores and
   both-teams-score are yes/no conditions; a player prop is a condition on that player's stat
   (paired only when both sites name the same player the same way; soccer player goals are left
   out, since the sites' rules differ on substitutes and extra time). For example:
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

**Your Polymarket orders and buying power are streamed too** (`wss://api.polymarket.us/v1/ws/private`),
as Polymarket's rate-limit guide asks instead of polling. A fill on any of your orders is pushed the
moment it happens: an order confirmation is read right then instead of being polled every 50-250ms,
and Auto maker hedges a resting order's fill on Kalshi as soon as it's pushed (it used to find out by
reading the order every 0.5s; with the stream it reads it on each push and every 2s as a backstop,
while still checking Kalshi's price every 0.5s). Buying power comes from the stream as well, so a trade
doesn't have to download it. If this stream is down, everything falls back to polling as before. The
window says "Polymarket order stream on" once it's connected.

**Polymarket's public gateway caches.** Its CDN keeps every reply (order books, quotes, market lists)
for 30 seconds per URL and ignores no-cache headers, so reading the same book twice within 30 seconds
returned the same old copy: arbs that were already gone, and liquidity that wasn't there. Every
read now carries a parameter no other request has, so it always comes from the exchange. Kalshi's
market list (the prices full sweeps and the fast lane read) allows 15 seconds of caching too, so it gets
the same treatment; its order books aren't cached.

If a stream goes quiet (no message for 90 seconds) it reconnects by itself. Every full sweep still
polls every market as a backstop, and a streamed market is only trusted without polling while it has
updated in the last minute, so a silent stream can't freeze prices. A polled price never replaces a
newer streamed one: each quote remembers when it was requested, and an older answer is ignored. The header shows each stream's
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
- **Even split (the default):** every shard keeps an equal share of your Kalshi cash (34% main, 33%
  crypto and commodities, 33% tennis, baseball and basketball). The app sets this up through Kalshi's
  own automatic rebalancing, which moves cash between your shards about every 10 seconds, even while the
  app is off. The app checks the split at start-up and every 10 minutes, and says what it found in the
  log (lines starting `Kalshi shards:`). If your shards are still well off an equal share a minute after
  the split was set, the app moves the extra cash to the short shards itself, between trades. Trades
  never wait for cash to move. Right after a big trade drains a shard, the next trade there is sized to what's left until
  Kalshi refills it.
- **Other modes** are under *Kalshi cash across exchange shards* in Settings, or `KALSHI_SHARD_MODE`
  in `.env`:
  - `per_trade`: before a trade on a shard that's short, the app moves just the cash that trade needs
    onto it from your other shards (richest first). It waits up to 8 seconds for the cash to arrive,
    then sizes the trade to what's there. The confirm screen shows what was moved. Arbs are sized to
    your total Kalshi cash. In this mode the app turns Kalshi's own rebalancing off, because it would
    move the cash back.
  - `manual`: the app leaves your shards alone. Set your own split with `kalshi-shards.bat` (or
    `python -m arb.shards`): it shows your cash per shard, asks for a split (Enter = even), and turns
    on Kalshi's automatic rebalancing with it. `100 0 0` turns rebalancing off. You can also move cash
    by hand at kalshi.com/account/exchange-indexes.
- **Make trade**, **Fast trade** and **Auto-trade** check the market's shard before ordering. If it's
  empty they tell you, and nothing is traded.

## Faster Kalshi scanning (free Advanced tier)

Kalshi's free Basic tier allows 20 requests a second. The Advanced tier allows 30, and it's free and
permanent once at least 1 of your last 100 Kalshi orders was placed through the API (a **Make trade**
counts; orders placed on kalshi.com don't). Double-click `upgrade-kalshi.bat` (or run
`python -m arb.upgrade`) once. It prints your tier before and after. Then restart the dashboard:
it reads your tier at start-up and uses the higher limit by itself.

## Focus: scan only markets settling soon

**Filters & settings → Focus** limits scanning to pairs whose Kalshi market settles within 1, 3, 7 or
30 days, or **All markets**. Sports games carry dozens of lines each, so as a rough guide:

| Focus | Share of contracts scanned |
| - | - |
| 1 day | ~5% (about 2,000) |
| 3 days | ~57% |
| 7 days | ~86% |

With fewer markets each full sweep is much faster, and when focused every scanned market also goes on
the live price streams (up to 2,000 per site), so prices update within a fraction of a second. Every arb
found also pays out within that window, so your cash comes back sooner. The setting is a scanner setting:
it applies right away, and the scanner keeps it in `cache/focus.json` across restarts. The
"Contracts watched" tile shows the focus.

## Latency: tick to trade

Every trade records a timeline in milliseconds, and the result screen and **Last auto-trades** show it:
- **tick to detected:** the price change arriving, to the arb being found;
- **detected to decided:** the arb being found, to Auto-trade deciding to take it (or you clicking);
- **checks:** the pre-trade checks;
- **each order:** from being sent to the exchange's final answer;
- **total:** from the tick to done.

**Auto-trade → Speed** shows typical (p50) and slow-case (p95) times over the last 50 trades.

What keeps it short:
- **Streamed ticks:** a price change is re-checked the moment it arrives (no polling interval).
- **Checks without downloads:** in the common case the pre-trade checks download nothing. They use the
  live feed's book while the feed is alive (it heard from the exchange in the last 5 seconds) and has the
  book from the last 30 seconds: a book that hasn't changed is still current on a live feed. They also use
  the market's details cached for a minute and the scanner's cash reading (refreshed every 15 seconds, and
  downloaded fresh after any trade). Whatever must be downloaded loads at the same time.
- **Trades first:** a trade's own requests go ahead of every other request, the fast lane's included,
  and may burst without waiting for the steady pace; orders never wait for a slot.
- **Auto-trade mode:** while Auto-trade is on, only the markets it can take are refreshed (see Fast lane).
- **Leg order decided last:** Auto-trade's "smart" order (which site goes first) is decided once the books
  are read, not when the arb was spotted, so "the stale site first" is still true when the orders go out.
- **Kept-alive connections:** connections to both sites stay open, which saves a TLS handshake per request.
- **Polymarket order confirmation:** checked after 50 ms, then backing off (it was every 250 ms).
- **Kalshi shard cash:** every shard is kept at an equal share (see Kalshi exchange shards), so a trade never
  waits for a transfer.
- **Polymarket goes first** (see Make trade), so a slow Polymarket miss trades nothing.

The floor is the two exchanges' own response times over your internet connection. Run
`latency-test.bat` to see yours.

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
orders; **Auto maker** does.

**Auto maker** (switch at the top of the Maker mode tab, off every time the scanner starts) does the
three steps above by itself for the best Maker mode row settling within `FAST_MAX_HOURS`:
- rests a post-only Polymarket order (it can never fill as a taker) that Polymarket expires after
  `MAKER_AUTO_TTL_SECS` (120) even if this app stops; sized so Kalshi holds twice the shares at or
  below the hedge limit, and to the cash on both sites;
- reads the order every `MAKER_AUTO_POLL_SECS` (0.5s) and buys every newly filled share on Kalshi at
  once, never above the hedge limit for what those shares really cost;
- cancels when Kalshi moves past the hedge limit, the row leaves the Maker mode list, a hedge misses,
  time runs out, you press Cancel, or you turn it off; then hedges any last fills and sells back what
  it couldn't hedge;
- adds hedged pairs to My arbs and logs every order to `trades.jsonl`;
- turns itself off after `AUTO_TRADE_MAX_MISSES` orders in a row leave shares to sell back, after
  `AUTO_TRADE_MAX_DAILY_LOSS` lost in a day, or if an order can't be read.

Limits in `.env`: `MAKER_AUTO_MAX_ORDER` ($25 per order), `MAKER_AUTO_MAX_ORDERS` (2 at once),
`MAKER_AUTO_MAX_RESTING` ($100 resting in total), `MAKER_AUTO_DAILY_LIMIT` ($200 filled per day),
`MAKER_AUTO_COOLDOWN_SECS` (300, per pair).

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
