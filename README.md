# rh-meme-lab

A research toolkit for meme coins on **Robinhood Chain** (chain ID 4663).

It is built around three questions, in the order they matter when real money is
involved:

1. **Can I get out?** Screen tokens by exit liquidity, not by chart.
2. **Is there an edge at all?** Record the data needed to test one thesis
   honestly, against a control group, with no lookahead.
3. **How much, and when do I sell?** Size the position from the exit
   backwards, before entering.

Everything is Python standard library. There is no `pip install`, no
framework, and no build step. Two optional features need API keys; the core
screener, collector, dashboard and exit planner need none.

> **Not financial advice.** Nothing here predicts price. On Robinhood Chain's
> FOMO app, 95.2% of 375,740 meme traders lost money or made under $100. This
> is a negative-sum game before you make a single decision. The tools help you
> avoid positions you provably cannot exit and claims you cannot support.

---

## Contents

- [What is in the repo](#what-is-in-the-repo)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [memescan: the screener](#memescan-the-screener)
- [echo: the forward study](#echo-the-forward-study)
- [dash.py: the live dashboard](#dashpy-the-live-dashboard)
- [exitplan.py: the exit planner](#exitplanpy-the-exit-planner)
- [cohort: FOMO leaderboard analysis](#cohort-fomo-leaderboard-analysis)
- [jev: typed LLM judgments](#jev-typed-llm-judgments)
- [API keys](#api-keys)
- [Running the collector as a service](#running-the-collector-as-a-service)
- [Data and what is not in the repo](#data-and-what-is-not-in-the-repo)
- [Tests](#tests)
- [What Robinhood Chain taught us](#what-robinhood-chain-taught-us)
- [Design principles](#design-principles)
- [Known limits](#known-limits)
- [License](#license)
- [Repository layout](#repository-layout)

---

## What is in the repo

| Component | What it does | Needs a key | Docs |
|---|---|---|---|
| **`memescan/`** | CLI screener. Ranks tokens in a market-cap band on exit, holders, momentum, meme and safety. Can reconstruct the real holder set from chain logs. | no | [memescan/README.md](memescan/README.md) |
| **`echo/`** | Always-on collector and backtest for the "cross-chain echo" thesis, plus wash-trade, bundle and on-chain launch detection. | no (Jev optional) | [echo/README.md](echo/README.md) |
| **`dash.py`** | Local web dashboard over the collector's database, with the exit planner built in. | no | below |
| **`exitplan.py`** | Position sizing from exit liquidity: max safe size, tranche ladder, profit ladder. | no | below |
| **`cohort/`** | FOMO API client. Re-ranks the trader leaderboard by Robinhood Chain PnL. | FOMO | below |
| **`jev/`** | TypeSafe/Jev client used to classify launches as live narrative vs brand squat. | TypeSafe | below |
| **`setup.sh`** | Stores API keys outside the repo and verifies each one against the live API. | | |
| **`tests.py`** | 58 offline checks on scoring, slippage, gates and the holder replay. | no | |
| **`COMMANDS.md`** | Operator cheat sheet for the machine the collector runs on. | | [COMMANDS.md](COMMANDS.md) |

---

## Quick start

Requirements: Python 3.9 or newer, and network access. macOS is only required
for the launchd service; everything else runs anywhere.

```bash
git clone https://github.com/Ikarus0013/rh-meme-lab.git ~/crypto
cd ~/crypto

python3 tests.py                 # 58 offline checks, should end with ALL PASS

python3 -m memescan              # screen $20M-$100M mcap tokens
python3 -m echo                  # start the collector (runs until stopped)
python3 dash.py                  # dashboard at http://localhost:8799
```

Cloning to `~/crypto` is recommended: a few error messages point at
`~/crypto/setup.sh`, and macOS will not let a background service read from
`~/Desktop` (see [the service section](#running-the-collector-as-a-service)).

---

## Architecture

```
                         OTHER CHAINS                    ROBINHOOD CHAIN
                  GeckoTerminal trending           GeckoTerminal new_pools
                  (Solana / Base / BSC)            + on-chain mint events (RPC)
                           │                                  │
                           ▼                                  ▼
                    hot_ticker table  ── ticker match ──▶  rh_launch table
                    (refreshed 15 min)                    echo │ control
                                                               │
                              DexScreener prices ──▶  obs (price / liq series)
                                                               │
              ┌────────────────────────────────────────────────┤
              ▼                         ▼                      ▼
       echo/analyze.py             dash.py               echo/backfill.py
       delayed-entry backtest      live dashboard        on-chain case-control
       echo vs control             + exit planner


   memescan (independent CLI)
   GeckoTerminal pools ─▶ dedupe to tokens ─▶ band filter ─▶ DexScreener enrich
        ─▶ RPC contract checks ─▶ 5-pillar score + hard gates
        ─▶ optional: replay Transfer logs ─▶ real holder set
```

Shared plumbing lives in `memescan/net.py` (per-host rate limiting, retries,
backoff) and `memescan/config.py` (endpoints, symbol filters, weights). `echo`
imports both.

**Data sources**

| Source | Used for | Key |
|---|---|---|
| GeckoTerminal public API | pool discovery, trending boards, recent trades, OHLCV | none |
| DexScreener public API | price, liquidity, market cap, socials | none |
| Robinhood Chain public RPC | contract checks, Transfer logs, mint events, Multicall3 | none |
| FOMO API | trader leaderboard and swap history | yes |
| TypeSafe (Jev) | typed probability judgments on launches | yes |

---

## memescan: the screener

Answers: *of the tokens in my market-cap band, which can I put money into and
get back out of, and who is already holding them?*

```bash
python3 -m memescan                                   # $20M-$100M, $1k position, 2% slippage
python3 -m memescan --position 5000 --max-slippage 0.015
python3 -m memescan --pages 10 --limit 25 --detail 8  # wider and deeper
python3 -m memescan --deep 3                          # real holder sets for the top 3 (slow)
python3 -m memescan --json scan.json                  # machine-readable
MEMESCAN_RPC_URL=https://your-endpoint python3 -m memescan --deep 3
```

| Flag | Default | Meaning |
|---|---|---|
| `--mcap-min` / `--mcap-max` | $20M / $100M | market-cap band |
| `--min-liquidity`, `--min-vol24`, `--min-age-hours` | see `config.DEFAULTS` | floor filters |
| `--position` | 1000 | the dollars you are actually deploying; drives the slippage score |
| `--max-slippage` | 0.02 | price-impact budget |
| `--pages` | 5 | GeckoTerminal pages to pull |
| `--limit` / `--detail` | 15 / 5 | rows shown / rows with the full factor breakdown |
| `--deep N` | 0 | reconstruct holders for the top N from Transfer logs |
| `--exclude SYM ...` | | drop tickers |
| `--no-chain` | | skip RPC contract checks |
| `--rpc-url` | public RPC | or set `MEMESCAN_RPC_URL` |
| `--json PATH`, `--no-color`, `--verbose` | | output control |

**The score.** Five pillars, each 0-100 from named sub-factors, combined by
`config.WEIGHTS`:

| Pillar | Weight | Asks |
|---|---|---|
| exit | 0.30 | liquidity/mcap, turnover, slippage on *your* position size, routes out |
| holders | 0.26 | unique buyers, buy pressure, wallet diversity, top-10 share, HHI |
| momentum | 0.18 | sustained trend, volume acceleration, whether you are late |
| meme | 0.14 | socials, branding, ticker, lineage, meme-vs-utility intent |
| safety | 0.12 | ownership renounced, liquidity floor, sell pressure, whale risk |

**Hard gates** sit outside the weighted score. A token failing one is marked
`X` and is never presented as a candidate, however well it scores: liquidity
under 1% of mcap, fewer than 15 unique buyers, up more than 900% on the day,
top-10 wallets over 75%, or younger than `--min-age-hours`.

**Deep holder scan.** There is no free indexer for this chain and the explorer
is behind Cloudflare, so `--deep` replays every ERC-20 `Transfer` log from the
token's mint to head into a balance map. The result is checked against
`totalSupply`; below 90% coverage, or with any unread block range, the stats
are marked `UNRELIABLE` and excluded from scoring.

The full write-up, including why HHI is scored and Gini is not, how thresholds
were calibrated, and the measured RPC limits, is in
[memescan/README.md](memescan/README.md).

---

## echo: the forward study

One thesis: **when a token runs on Solana, Base or BSC and a same-ticker clone
launches on Robinhood Chain, is that clone systematically tradeable?**

It has to be a *forward* study. The chain launches roughly 20,000 pools a day
and GeckoTerminal's `new_pools` reaches back about seven minutes at that rate,
so the history cannot be fetched later. It is recorded as it happens or it is
gone.

```bash
python3 -m echo                                      # collector; runs until stopped
python3 -c "import echo.analyze as a; a.report()"    # backtest so far
python3 -m echo.backfill                             # retrospective on-chain case-control
```

### Collector (`echo/collect.py`)

Every 180 seconds:

1. Refresh the **hot-ticker** watchlist (every 15 min): what is running on
   Solana, Base and BSC right now.
2. Ingest new Robinhood Chain launches from two feeds, GeckoTerminal and
   on-chain mint events. Every row carries its `source`.
3. Tag each launch **echo** (ticker matches a live parent) or **control**
   (random sample of launches that matched nothing).
4. Track price and liquidity for both cohorts for 48 hours: every cycle for
   the first 2 hours, then every 15 minutes.
5. Log the cycle, including failures and empty cycles, so a gap reads as a gap.

### Schema (`echo/db.py`, SQLite)

| Table | Holds |
|---|---|
| `rh_launch` | every launch witnessed, with cohort and source |
| `hot_ticker` | tickers running on other chains, snapshotted |
| `echo_link` | an RH launch matched to a parent, with the parent's state *at detection* |
| `obs` | price / FDV / liquidity time series for both cohorts |
| `onchain_seen` | addresses the on-chain feed already emitted, so a re-mint is not a new launch |
| `jev_score` | Jev classifications for on-chain launches |
| `social_snapshot` | socials as they were at detection, since socials get added after attention arrives |
| `cycle_log` | every collector cycle: `ok`, `error` or `empty` |
| `hist_token` | checkpointed state for the retrospective backfill |

The rule that governs the schema: every column written at detection time must
be knowable at detection time. Outcomes live only in `obs`, timestamped.
Lookahead bias is prevented by the table layout, not by discipline.

### Backtest (`echo/analyze.py`)

- Entry is simulated at **+5, +15 and +60 minutes after detection**, never at
  the launch price, because you cannot get it.
- Exit is mechanical: +100%, -50%, or a 24h timeout, walked forward through
  the series. Peak return is reported separately and labelled unrealizable.
- Echo launches are split into **fresh** (parent under 72h old: a live
  narrative) and **squat** (older parent: name-squatting). They are different
  trades and averaging them hides both.
- Every figure is shown against the **control** cohort. The edge is the spread.
- No verdict below 20 observations per cohort. The significance bar is
  Bonferroni-corrected (0.05 / 9) for the 9 comparisons the report makes.
- `simulate_costed()` re-runs the same rules with entry slippage, exit
  slippage and fees charged. These tokens launch around $5k of liquidity,
  where a $500 order is a sixth of the pool.

### Supporting modules

| Module | Purpose |
|---|---|
| `feeds.py` | GeckoTerminal and DexScreener feeds, symbol normalisation, noise-ticker filter |
| `onchain.py` | Launch feed read straight from the chain: every ERC-20 mint is a `Transfer` from the zero address, whatever launchpad created it. Metadata comes through Multicall3, one RPC call per window instead of four per token. Filters out WETH deposits and LP-position NFTs. |
| `classify.py` | Uses Jev to make the fresh/squat call for on-chain launches, as a reject filter only |
| `wash.py` | `wash.check(pool)` scores recent trades for manufactured volume by structure: per-wallet round-trip symmetry and inter-trade cadence shared across wallets |
| `bundle.py` | Detects bundled launches (many wallets taking float in the same instant) and tracks whether that specific wallet set later exits together |
| `backfill.py` | Three checkpointed stages (sample, features, outcome) for the question that *can* be answered from chain history: among tokens that got traction, what separated winners from the ones that went to zero |

More detail, including measured feed coverage and the statistics note on
clustering by ticker, is in [echo/README.md](echo/README.md).

---

## dash.py: the live dashboard

```bash
python3 dash.py                       # http://localhost:8799
ECHO_DASH_PORT=9100 python3 dash.py   # pick a port
```

A single-file threaded HTTP server over the collector's SQLite database. It
binds to `127.0.0.1` only, refreshes every 30 seconds, and if the port is busy
it walks up to the next free one and prints it.

Panels:

- **Collector**: launch and observation counts, cycle health, time since the
  last cycle, gaps surfaced as a status chip
- **Backtest**: the cohort table at each entry delay
- **Echo detections**: matched launches with parent state at detection
- **FOMO cohort**: top traders re-ranked by Robinhood Chain PnL (shown only if
  `cohort/cohort_rh.json` exists)
- **Watchlist**: parents currently running on other chains
- **Exit planner** at `/exit`: paste a token address, get the full plan

The page follows one rule: a number that is not yet trustworthy must *look*
untrustworthy. Cohorts below the sample floor render greyed with their `n`
shown, and the verdict banner states what is missing instead of showing a
figure. Each column has an inline glossary.

Routes: `/` (dashboard), `/data` (state as JSON), `/exit` (planner page),
`/exit/plan` (planner JSON).

---

## exitplan.py: the exit planner

Sizes a position by what you can get **out**, not what you want in.

On a constant-product pool, selling `X` against liquidity `L` moves the price
by `X / (L/2 + X)`. The damage is superlinear: a position worth 5% of the pool
costs about 9% to exit, and one worth 50% costs about 50%.

The planner answers three questions:

1. **How much can I put in and still leave?** `max_position(liquidity, max_slip)`
2. **What does getting out look like?** `tranche_plan(...)`: a ladder of sells
   that each stay inside the impact budget. Liquidity is held constant, which
   flatters the result, so treat the tranche count as a floor.
3. **When do I take money off the table?** `profit_ladder(...)`: pre-committed
   sells at 3x / 10x / 30x, flagging the rung where cost basis is recovered.

```python
import exitplan
p = exitplan.plan(address="0x...", position=2500, max_slip=0.02)
p["safe_position"], p["one_shot_slip"], len(p["tranches"])
```

`lookup()` resolves the chain automatically (Robinhood, Solana, Base, BSC,
Ethereum) through DexScreener and sums liquidity across the token's pools. The
same functions back the `/exit` page in the dashboard.

---

## cohort: FOMO leaderboard analysis

FOMO publishes a trader leaderboard by total PnL. `cohort/chains.py` re-ranks
it by **Robinhood Chain PnL**, the ranking FOMO does not show, using the
per-token breakdown already present in each leaderboard row, so it costs no
extra calls.

```python
from cohort import chains
rows = chains.leaderboard("30d", limit=150)
ranked = chains.rank_by_rh(rows)          # sorted by RH-chain pnl, with rh_share
chains.relay_chains("some_handle")        # chains a trader actually bridges through
```

`cohort/client.py` wraps the API with two properties that matter on a free
tier of about 1,000 calls a month: every response is **cached to disk** keyed
by the full request, and every call is **logged with its credit cost** in
`cohort/credits.jsonl`. `client.spend()` reports the total.

---

## jev: typed LLM judgments

`jev/client.py` is a small client for TypeSafe's System One endpoint. One call
carries a `state` object and several typed questions, and returns
probabilities and scores. Responses are cached to disk by request hash, so
re-running an evaluation is free and reproducible.

It is used in `echo/classify.py` for one job: deciding whether an echo
candidate's parent is in a **live run** or is an old brand being squatted. On
28 launches with known outcomes, the `live_narrative` question separated the
two groups far better than the 72-hour age threshold did (mean 0.79 vs 0.27).

Jev is used as a **reject filter, never a picker**. Its confidence ran
0.45-0.76 on tokens it scored low and 0.00-0.24 on the ones that went on to
double. It knows when to say no and not when to say yes, so nothing in this
repo treats a high score as a buy signal.

---

## API keys

Only `cohort/` and `jev/` (and therefore `echo/classify.py`) need keys.

```bash
bash setup.sh          # set up whatever is missing
bash setup.sh fomo     # FOMO only      (https://fomoapi.io/dashboard)
bash setup.sh jev      # TypeSafe only  (https://typesafe.ai)
```

The script reads each key without echoing it, strips whitespace, writes it to
`~/.config/memescan/` with mode `600`, and then proves it works with a live
call. Keys never enter the repo, the shell history or the environment.

| File | Used by |
|---|---|
| `~/.config/memescan/fomo_key` | `cohort/client.py` |
| `~/.config/memescan/jev_key` | `jev/client.py` |

Optional environment variables:

| Variable | Effect |
|---|---|
| `MEMESCAN_RPC_URL` | use a dedicated RPC endpoint instead of the public one |
| `ECHO_DASH_PORT` | dashboard port (default 8799) |

---

## Running the collector as a service

The study is only as good as its uptime, so on macOS the collector runs as a
launchd agent that starts at login and restarts on crash.

```bash
# edit the paths inside the file first
cp deploy/com.ron.echo.plist.example ~/Library/LaunchAgents/com.ron.echo.plist
launchctl load ~/Library/LaunchAgents/com.ron.echo.plist

launchctl list | awk 'NR==1 || /com.ron.echo/'       # running? status 0 is healthy
tail -f echo/logs/collector.log                      # watch it
launchctl kickstart -k gui/$(id -u)/com.ron.echo     # restart after a code change
```

A healthy log line:

```
[16:11:02] seen= 91 new= 2 echo= 0 tracked=  12/  30 hot=93
```

Two things to know:

- **Do not keep the repo under `~/Desktop` or `~/Documents`.** macOS TCC
  blocks launchd agents from reading there. The failure shows up only as exit
  code 78, and creating files still works, which makes it confusing.
- **`seen= 0` is a problem, not a quiet chain.** Those cycles are logged as
  `empty` and count as gaps. A sleeping Mac is a hole in the denominator;
  `caffeinate -s` avoids it.

[COMMANDS.md](COMMANDS.md) has the full operator cheat sheet, including a
health-check snippet.

---

## Data and what is not in the repo

The repo contains code and documentation only. These are git-ignored:

| Path | Why |
|---|---|
| `echo/echo.db` (+ `-wal`, `-shm`) | the live dataset; about 190 MB and growing, above GitHub's file limit |
| `echo/logs/` | collector and dashboard logs |
| `cohort/cache/`, `jev/cache/` | API response caches, regenerated on demand |
| `cohort/credits.jsonl` | local API spend log |
| `cohort/cohort_rh.json` | saved leaderboard snapshot containing third-party trader handles and wallets |

A fresh clone therefore starts with an empty database. `python3 -m echo`
creates it on first run, and the backtest becomes meaningful after roughly 48
hours of uninterrupted collection.

For scale: the original instance has been collecting since 2026-09-22 and, as
of 2026-10-05, holds about 110,000 witnessed launches and 920,000 price
observations.

---

## Tests

```bash
python3 tests.py     # 58 offline checks, no network
```

Covers the scoring curves, slippage math, gate behaviour and the Transfer-log
replay, including regressions for bugs found in real data: a truncated scan is
detected, unread block ranges force `UNRELIABLE`, ERC-721 transfers are
ignored, burn and pool addresses are excluded from holder stats, a v4
singleton PoolManager is excluded from concentration while its supply is still
reported, and EIP-7702 delegated EOAs count as wallets.

The `echo`, `cohort` and `jev` packages and `dash.py` have no automated tests.

---

## What Robinhood Chain taught us

Things that were measured rather than assumed, and that shaped the code:

- **Tokens, not pools.** One token routinely has five or more pools. Ranking
  pools double-counts, so liquidity and volume are summed per token.
- **Tokenized equities trade on the same DEXes.** NVDA, META and others pass
  every "low mcap, real volume" filter and are excluded by ticker.
- **Bytecode does not mean "contract".** The chain is built around account
  abstraction, so many retail wallets are EIP-7702 delegated EOAs carrying 23
  bytes of delegation code. On one token, 13 of 15 flagged "contracts" were
  ordinary users.
- **Two pool architectures at once.** v3-style pools have real addresses;
  Uniswap v4 and pons-v2 are singletons where all tokens sit in one shared
  PoolManager. Pools are excluded by asking the chain for code, not by a list.
- **Gini is the wrong concentration metric.** It tracks the dust tail, so it
  ranked a better-distributed token as worse. HHI is scored instead.
- **Wallets trade many times a day** because of gasless ERC-4337, so the
  organic-vs-wash threshold sits far lower than on an L1.
- **The public RPC has three distinct failure modes** needing different
  responses: HTTP 429 (back off), `log query timed out` (narrow the window),
  and `logs matched by query exceeds limit of 10000` (halve and recurse).
- **No archive state.** Historical `eth_getCode` fails, so deploy blocks are
  found through mint events instead of binary search.
- **Launchpads keep changing.** Sampling 8 launches found four different entry
  points. Watching mint events catches all of them by construction.
- **Wash trading hides in structure, not size.** One case had 23 wallets on an
  identical buy/buy/buy/sell cycle with 288 distinct trade sizes in 300 trades.
- **Bundling is not a rug signal.** A bundled token went 50x while a
  clean-looking one rugged. What matters is the bundle *exiting* together.
- **Echo launches are heavily pseudo-replicated.** A trending name gets
  mass-cloned (one ticker launched 31 times in 22 hours), so per-token
  significance tests are invalid. Aggregate to one row per ticker.

---

## Design principles

- **Standard library only.** Nothing to install, nothing to break on upgrade.
- **Report nothing rather than something wrong.** Partial holder data, thin
  cohorts and unread ranges are flagged or withheld, not averaged in.
- **Every score is interrogable.** Each sub-factor is stored with the input
  that produced it.
- **Gates are not scores.** "You cannot trade this" conditions are kept out of
  the weighted sum so a strong meme score cannot paper over an impossible exit.
- **Always a control group.** An absolute return is not a finding.
- **Cache and account for every paid call.**

---

## Known limits

- Market cap is FDV-like; a token with a vesting treasury looks cheaper than
  it is.
- Slippage is a constant-product approximation. Concentrated liquidity can
  absorb more or less. Treat max-position figures as an order of magnitude.
- There is no honeypot simulation: nothing proves a token is sellable.
- Ticker matching is lexical. Renamed clones are missed and coincidental
  collisions are false positives.
- The control cohort is a sample, so confidence intervals are wide.
- Wash detection sees only the ~300 most recent trades, so it describes now,
  not a past pump.
- Deep holder scans on the public RPC are slow and can fail on very active
  tokens; they report `UNRELIABLE` when they do.
- The collector's service setup is macOS-specific.
- Nothing here is predictive.

---

## License

[MIT](LICENSE)

---

## Repository layout

```
.
├── README.md                 this file
├── COMMANDS.md               operator cheat sheet
├── LICENSE                   MIT
├── setup.sh                  store and verify API keys
├── tests.py                  58 offline checks
├── dash.py                   local dashboard + exit planner UI
├── exitplan.py               exit-first position sizing
├── deploy/
│   └── com.ron.echo.plist.example   launchd agent template
├── memescan/                 the screener
│   ├── cli.py                argument parsing and terminal output
│   ├── sources.py            GeckoTerminal discovery, DexScreener enrichment
│   ├── chain.py              RPC contract checks, Transfer-log holder replay
│   ├── score.py              five pillars, hard gates, slippage math
│   ├── net.py                rate limiting, retries, JSON-RPC
│   └── config.py             endpoints, filters, weights
├── echo/                     the forward study
│   ├── collect.py            the collector loop
│   ├── db.py                 SQLite schema
│   ├── feeds.py              hot tickers, RH launches, prices
│   ├── onchain.py            mint-event launch feed via Multicall3
│   ├── classify.py           fresh vs squat, using Jev
│   ├── analyze.py            delayed-entry backtest, costed simulation
│   ├── backfill.py           retrospective on-chain case-control
│   ├── wash.py               wash-trade detector
│   └── bundle.py             bundle detection and exit tracking
├── cohort/                   FOMO API client and leaderboard re-ranking
└── jev/                      TypeSafe / Jev client
```
