# memescan

A screener for meme coins on **Robinhood Chain** (chain ID 4663), built for
sizing an actual entry rather than admiring a chart.

It answers a narrow question: *of the tokens currently in my market-cap band,
which ones can I put money into and get it back out of, and who is already
holding them?*

## Install

None. Python 3.9+ stdlib only — no `pip install`, no API keys.

```bash
cd ~/crypto              # repo root
python3 -m memescan --help
```

## Usage

```bash
# default screen: $20M-$100M mcap, $1k position, 2% max slippage
python3 -m memescan

# size the screen to the money you're actually deploying
python3 -m memescan --position 5000 --max-slippage 0.015

# widen the net and look deeper
python3 -m memescan --pages 10 --limit 25 --detail 8

# reconstruct real holder sets for the top 3 (slow: minutes per token)
python3 -m memescan --deep 3

# machine-readable output
python3 -m memescan --json scan.json

# deep scans are far more practical on a dedicated RPC than the public one
export MEMESCAN_RPC_URL="https://your-endpoint"
python3 -m memescan --deep 3            # or: --rpc-url https://...
```

## How it works

```
GeckoTerminal pools ──┐
  top-by-volume       ├─→ dedupe to TOKENS ─→ band filter ─→ DexScreener enrich
  trending + new    ──┘      (not pools)                          │
                                                                  ▼
        score ←── RPC contract checks ←── GeckoTerminal token quality
          │
          └─→ optional: replay ERC-20 Transfer logs → real holder set
```

Two things this gets right that a naive screener does not:

**Tokens, not pools.** A single token routinely has five or more pools on this
chain (PONS had five in the top 100 by volume). Ranking pools double-counts.
Liquidity and volume are summed across a token's pools; price and valuation are
taken from its deepest one.

**Robinhood Chain carries tokenized equities.** NVDA, META and friends trade on
the same DEXes and sail through every "low mcap, real volume" filter. They're
excluded by ticker (`config.EQUITY_SYMBOLS`), alongside quote assets like WETH
and USDG. Extend with `--exclude`.

## The score

Five pillars, weighted (`config.WEIGHTS`). Every sub-factor prints with the
input that produced it, so a ranking can always be interrogated.

| Pillar | Weight | Asks |
|---|---|---|
| **exit** | 0.30 | liquidity/mcap, turnover, slippage on *your* position size, routes out |
| **holders** | 0.26 | unique buyers, buy pressure, wallet diversity, top-10 share, HHI |
| **momentum** | 0.18 | sustained trend, volume acceleration, and whether you're late |
| **meme** | 0.14 | socials, branding, ticker, lineage, meme-vs-utility intent |
| **safety** | 0.12 | ownership renounced, liquidity floor, sell pressure, whale risk |

Exit is weighted highest on purpose. In this band the binding constraint is
almost never "was it a good meme" — it's that a $60M-mcap token with $80k of
liquidity cannot absorb your exit at any price you'd accept.

**Hard gates** are kept out of the weighted score entirely. A token failing one
is marked `X` and never presented as a candidate no matter how well it scores:
liquidity under 1% of mcap, fewer than 15 unique buyers, already up >900% on the
day, top-10 wallets over 75%, or younger than `--min-age-hours`.

### Why HHI and not Gini

Gini is the obvious concentration metric and it is the wrong one here. Over
token balances it is dominated by the long tail of dust wallets, so it tracks
holder *count* rather than whale risk. Two tokens with verified 100%-coverage
holder data:

| | holders | top-1 | top-10 | Gini | HHI |
|---|---|---|---|---|---|
| CHUMP | 10,913 | 1.4% | 12.9% | **0.988** | 0.0035 |
| ORBIO | 3,301 | 5.9% | 31.0% | **0.955** | 0.0153 |

CHUMP is better distributed on every measure that matters, yet scores *worse*
on Gini. Scoring it punished the better token. HHI squares the shares, so it
responds to large holders and ignores dust — it ranks them correctly. Gini is
still computed and reported as context; it is just not scored.

### Calibration

Curve thresholds were fitted to the chain's actual distributions (sampled over
80 tokens), not to intuition carried over from other chains:

| Metric | p10 | median | p90 |
|---|---|---|---|
| liquidity / mcap | 1.2% | 5.4% | 53% |
| wallet diversity (buyers/buys) | 0.145 | 0.282 | 0.505 |
| pool count | 1 | 1 | 3 |

Wallet diversity matters here: Robinhood Chain supports gasless ERC-4337
account abstraction, so wallets legitimately trade many times a day and the
organic-vs-wash threshold sits far lower than it would on an L1. A curve tuned
for Ethereum scores every token on this chain at zero.

## The deep holder scan (`--deep N`)

The block explorer is behind Cloudflare and there is no free indexer for this
chain, so holder data is reconstructed directly: every ERC-20 `Transfer` log
from the token's mint to head is replayed into a balance map. That yields real
numbers — holder count, top-1/top-10/top-50 share, Gini, HHI, new-holder growth
— rather than a vendor's opaque score.

**Anchored to the mint, not the pool.** A token is often deployed days before
its first pool exists, so starting a scan at pool-creation time silently
truncates it: wallets that bought earlier and sold inside the window go
net-negative and vanish from the balance map. The scanner instead finds the
true mint block by querying only `Transfer`-from-zero events. Filtering on the
`from` topic makes the result set tiny (usually a single log), so the node
serves a genesis-to-head range in under a second — where the same range
unfiltered times out. This was found the hard way: a pool-anchored scan of ZZZ
reported 58% supply coverage.

**Integrity check.** Reconstructed supply is compared against the contract's
`totalSupply`. Stats are marked `UNRELIABLE` — and excluded from both scoring
and the gates — if coverage falls below 90% *or* if any block range failed to
read. Partial concentration numbers are worse than none, so the tool reports
nothing rather than something wrong.

**It is slow and best-effort on the public RPC.** That endpoint both
rate-limits (HTTP 429) and returns `log query timed out` on dense windows. The
scanner adapts — halving its window on a timeout from 2M blocks down to a 2k
floor, backing off on rate limits, and widening again after a clean run — but a
token's launch region can be dense enough that even 2k-block windows time out.
In testing, a very active token (ZZZ, ~$13M daily volume) could not be scanned
to completion this way and correctly reported `UNRELIABLE` rather than
guessing.

Set `MEMESCAN_RPC_URL` (or `--rpc-url`) to a dedicated endpoint and this
becomes routine. On the public RPC, treat `--deep` as an investigation you run
on one or two finalists, and expect it to take minutes.

**Pools are excluded by asking the chain, not by listing them.** Robinhood
Chain runs both pool architectures simultaneously: v3-style DEXes
(`uniswap-v3`, `ramses-v3`) expose a real 20-byte pool address, while
`uniswap-v4` and `pons-v2` are *singletons* whose pool "address" is a 32-byte
pool ID — the tokens actually sit in one shared PoolManager contract that no
per-pool exclusion list can catch. So instead of enumerating singletons,
routers, bridges and lockers, the scanner runs one `eth_getCode` per top holder:
an address with bytecode is not a retail wallet. Reported concentration is
therefore wallet concentration, with contract-held supply broken out
separately.

**But bytecode alone does not mean "contract" on this chain.** Robinhood Chain
is built around account abstraction — FOMO uses it — so a large share of
ordinary retail wallets are **EIP-7702 delegated EOAs**, carrying exactly 23
bytes of `0xef0100 || implementation` delegation code. Those are precisely the
holders worth counting. On ORBIO, 13 of the 15 flagged "contracts" were
delegated user wallets; only two were real (a 24KB PoolManager and a 2KB
router). Excluding smart accounts would have erased most of the retail base and
badly overstated concentration, so they are kept.

Both bugs surfaced from implausible numbers rather than failures: first a scan
reporting LP at 0.0% of supply, which no live pool can do; then a "contract"
holder list where 13 entries were suspiciously all the same 23 bytes.

## Measured RPC limits

Probed against `https://rpc.mainnet.chain.robinhood.com`:

- `eth_getLogs` serves **2,000,000-block** windows; 10M times out server-side.
  The scanner halves its window on a timeout and retries.
- Block time is **~0.10088s** (~10 blocks/sec), so a 40-day-old token is
  ~34M blocks ≈ 18 chunked calls.
- **No archive state.** Historical `eth_getCode` returns
  `metadata is not found`, so binary-searching for a deploy block is
  impossible — hence the mint-event approach above.
- The RPC rate-limits aggressively (HTTP 429, *not* a JSON-RPC error), and
  returns `log query timed out` independently. The two need opposite responses
  — back off vs. narrow the window — so they are handled separately.

## Tests

```bash
python3 tests.py     # from the repo root: 58 offline checks, no network
```

Covers the scoring curves, the slippage math, gate behaviour, and the
Transfer-log replay — including that a truncated scan is detected, that unread
block ranges force `UNRELIABLE`, that ERC-721 transfers are ignored, and that
burn and pool addresses are excluded from holder stats — and that a v4
singleton PoolManager is excluded from concentration while its supply is still
reported, and that EIP-7702 delegated EOAs are classified as wallets rather
than contracts.

## Limits worth knowing

- **Market cap is FDV-ish.** Most memes are fully circulating so the two agree,
  but a token with a vesting treasury will look cheaper than it is.
- **Slippage is a constant-product approximation** (`X = t·Q/(1−t)` with
  `Q ≈ liquidity/2`). Uniswap v3 concentrated liquidity can absorb more or less
  depending on range placement. Treat `MAX POS` as an order of magnitude.
- **No honeypot simulation.** Ownership-renounced and code-size checks are done,
  but nothing here simulates a sell to prove the token is sellable.
- **`buyers` is summed across pools**, so a wallet trading two pools counts twice.
- **The meme/utility classifier is lexical.** Tokens whose description reads
  as a product (yield, revenue share, index) are flagged `U` in the output —
  during testing it correctly caught "The Index", a revenue-share token that
  passed every structural filter. It is a keyword heuristic and can be wrong,
  so it flags rather than excludes. Drop them with `--exclude`.
- **Nothing here is predictive.** Every factor is descriptive of current
  structure. The tool ranks tokens against each other; it has no view on
  whether any of them go up.

## Reality check

On Robinhood Chain's FOMO app, 95.2% of 375,740 meme traders lost money or made
under $100. The median trader lost about $120; aggregate losses were $1.26B,
while the platform collected eight figures in fees in August alone. This is a
negative-sum game before you make a single decision, and a screener does not
change that arithmetic — it only helps you avoid the positions you provably
cannot exit. Size accordingly.
