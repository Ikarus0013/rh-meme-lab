# echo

A forward-collecting study of one question: **when a token runs on Solana or
Base, and a same-ticker clone launches on Robinhood Chain, is that clone
systematically tradeable?**

It exists because the data does not. Robinhood Chain launches roughly 20,000
pools a day; GeckoTerminal's `new_pools` returns at most 200, which at that
rate is about **seven minutes of history**, and the block explorer is behind
Cloudflare. Nothing can be backtested retrospectively here. The dataset has to
be recorded as it happens, or it is gone.

## Run it

```bash
cd ~/crypto              # repo root
python3 -m echo                      # collector, runs until stopped
python3 -c "import echo.analyze as a; a.report()"   # backtest so far
```

Stdlib only, no keys. Reuses `memescan.net` for throttling.

## What it records

| table | holds |
|---|---|
| `rh_launch` | every launch it witnesses, tagged `echo` or `control` |
| `hot_ticker` | tickers running on Solana/Base/BSC, snapshotted every 15min |
| `echo_link` | an RH launch matched to a live parent, with the parent's state *at detection* |
| `obs` | price/liquidity time series for both cohorts |
| `cycle_log` | every cycle, including failures — so a gap reads as a gap |

## The two design decisions that matter

**No lookahead, enforced by schema.** Nothing written to `rh_launch` or
`echo_link` is an outcome. Outcomes live only in `obs`, timestamped. A
backtest physically cannot read a fact that a live trader would not have had,
because that fact is not in the table it reads.

**A control cohort, always.** Every cycle samples random non-matching launches
and tracks them identically. "Echo clones average +80%" is not a finding if a
random Robinhood Chain launch also averages +80%. The edge is the *spread*
between cohorts, and without controls that spread is uncomputable.

## Two phenomena, not one

Ticker matching catches both, and they are not the same trade:

| | parent age | example | reading |
|---|---|---|---|
| **fresh-runner echo** | hours | DEED — parent 13h old, +1632%, $68M vol | live narrative propagating across chains |
| **brand squat** | months | PUMP — parent 14 months old, +2.5%, $2.1B | name-squatting, no narrative |

`echo_link.parent_age_h` separates them; `analyze.py` reports them as separate
cohorts (`FRESH_PARENT_H = 72`). Collapsing them would average a real signal
against noise.

## The backtest

`analyze.py` simulates entry at **+5, +15 and +60 minutes after detection** —
never at launch price, because you cannot get it. Exit is mechanical: +100%,
−50%, or 24h timeout, walked forward through the series. Peak return is
reported separately and labelled unrealizable so it is never mistaken for a
result.

It refuses to print a verdict below 20 observations per cohort, and warns
below 6 hours of collection.

## Known limits

- **Ticker matching is lexical.** A clone that renames itself is missed; a
  coincidental collision is a false positive. `feeds.NOISE_SYMBOLS` strips
  quote assets, equities and majors, which removes the worst of it.
- **Control is sampled, not exhaustive** (2/cycle). Unbiased, but it is a
  sample — confidence intervals widen accordingly.
- **The collector must actually stay running.** A sleeping Mac is a hole in
  the denominator. `cycle_log` makes holes visible; it cannot fill them.
- **Nothing here is predictive yet.** It is an instrument for answering the
  question, not an answer to it.

## Wash detection (`wash.py`)

`wash.check(pool)` scores a pool's recent trades for manufactured volume, using
structure rather than amounts — operators randomize sizes (288 distinct sizes
in 300 trades on the case this was built from), but not their scheduler:

- **round-trip symmetry** — buy USD ≈ sell USD per wallet means no position taken
- **cadence clustering** — one inter-trade gap shared to the second by 3+
  unrelated wallets is a single bot

Returns `clean` / `some` / `heavy` by flagged share of volume. **Limitation:**
GeckoTerminal serves only the ~300 most recent trades, so this says whether a
pool is being washed *now*, not whether a past pump was.

## Statistics: cluster by ticker, always

The echo cohort is **7x pseudo-replicated** — 63 launches across 9 tickers,
because a trending name gets mass-cloned (SI launched 31 times in 22h). Control
is 252 launches across 213 tickers. A per-token significance test assumes
independence and is invalid here. Aggregate to one row per ticker and use
Fisher's exact, which is valid at n=9 where a z-test is not.

## On-chain launch feed (`onchain.py`)

The GeckoTerminal feed caps at 10 pages × 20 rows — roughly **seven minutes**
of history at this chain's launch rate — so any stall loses launches
permanently. `onchain.py` reads launches straight from the chain instead.

**Every ERC-20 mint is a `Transfer` from the zero address**, whatever contract
created the token. That matters because sampling 8 launches turned up **four
different launchpad entry points** — watching launchpads means chasing a list
that keeps changing; watching mint events catches all of them by construction.

**Multicall3** is deployed at the canonical address, so symbol/name/decimals/
totalSupply for a whole window costs **one** rpc call instead of four per
token — at ~130 tokens per window, the difference between 520 calls and 1.

Measured coverage against GeckoTerminal at the same moment:

| feed | tokens |
|---|---|
| GeckoTerminal `new_pools` (5 pages) | 47 |
| on-chain mint feed (~10 min) | 117 |
| **on-chain only** | **77** ← launches GT never showed |
| GT only | 7 (older than the window) |

### Filtering: minting ≠ launching

The raw feed carries chain plumbing — every WETH deposit mints WETH, and
Uniswap v3/v4 and Algebra mint an **LP position NFT** on every add-liquidity.
ERC-721 has no `decimals`, which separates those from real ERC-20s cleanly
without maintaining an address list. Typically ~20% of a raw window is
rejected this way.

### A third RPC failure mode

Beyond HTTP 429 (back off) and `log query timed out`, a dense range returns
**`logs matched by query exceeds limit of 10000`**. Backing off does not help —
the range itself is too big — so `scan_mints_adaptive` halves it and recurses.
A fixed window cannot work: the same 5 minutes succeeded in one window and
blew the cap in the next. Unreadable sub-ranges are returned as an **error**
even when partial data came back, because a silent partial is exactly how the
denominator gets corrupted.

### Running alongside, not replacing

Both feeds run each cycle and every row carries `source` (`gt` | `onchain`),
so the switchover is visible in analysis rather than silently changing the
denominator mid-study. On-chain rows land as `cohort='unclassified'` until the
hot-ticker match is wired to them.
