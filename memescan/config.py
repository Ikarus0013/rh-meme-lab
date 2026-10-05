"""Static configuration: endpoints, chain constants, filters and scoring weights.

Everything tunable lives here so the scoring model can be iterated on without
touching the data-collection code.
"""

# ---------------------------------------------------------------- chain / apis
import os

CHAIN_ID = 4663

# The public RPC both rate-limits and times out on dense log queries, which
# makes --deep scans slow and sometimes impossible for very active tokens.
# Point this at a dedicated endpoint to make deep scans practical:
#   export MEMESCAN_RPC_URL="https://..."
PUBLIC_RPC = "https://rpc.mainnet.chain.robinhood.com"
RPC_URL = os.environ.get("MEMESCAN_RPC_URL", PUBLIC_RPC)

# DexScreener + GeckoTerminal both index Robinhood Chain under these slugs.
DS_CHAIN = "robinhood"
GT_NETWORK = "robinhood"

DS_BASE = "https://api.dexscreener.com"
GT_BASE = "https://api.geckoterminal.com/api/v2"

# Measured empirically against the public RPC (see README "Measured limits").
BLOCK_TIME_SEC = 0.10088          # ~10 blocks/sec
MAX_LOG_RANGE = 2_000_000         # 2M blocks OK; 10M times out server-side
MIN_LOG_RANGE = 2_000             # floor before backing off instead of narrowing

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Rate limits. GeckoTerminal's free tier is 30 calls/min -> 2.2s spacing.
GT_MIN_INTERVAL = 2.2
DS_MIN_INTERVAL = 0.25
RPC_MIN_INTERVAL = 0.30   # see net.RPC_MIN_INTERVAL

# ------------------------------------------------------------------ exclusions
# Quote / infrastructure assets. These show up as pool base tokens constantly
# and are never the thing we're screening for.
INFRA_SYMBOLS = {
    "WETH", "ETH", "USDG", "USDC", "USDT", "DAI", "WBTC", "BTC",
    "USDS", "FRAX", "LUSD", "WSTETH", "STETH", "RETH", "SUSDE", "USDE",
}

# Robinhood Chain hosts tokenized equities. They trade on the same DEXes and
# would otherwise sail through a "low mcap, real volume" filter. Excluded by
# ticker; extend via --exclude on the CLI.
EQUITY_SYMBOLS = {
    "AAPL", "MSFT", "NVDA", "META", "GOOG", "GOOGL", "AMZN", "TSLA", "NFLX",
    "AMD", "INTC", "COIN", "HOOD", "MSTR", "SPY", "QQQ", "IWM", "VOO", "GLD",
    "BRKB", "JPM", "V", "MA", "DIS", "BA", "PLTR", "SOFI", "GME", "AMC",
    "PYPL", "SQ", "SHOP", "UBER", "ABNB", "CRM", "ORCL", "ADBE", "AVGO",
    "MU", "SMCI", "ARM", "TSM", "BABA", "NIO", "F", "GM", "T", "VZ", "XOM",
    "CVX", "WMT", "COST", "TGT", "HD", "MCD", "SBUX", "NKE", "KO", "PEP",
}

# Addresses that hold supply but are not "holders" in any meaningful sense.
BURN_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0000000000000000000000000000000000000001",
}

# ------------------------------------------------------------------- screening
DEFAULTS = {
    "mcap_min": 20_000_000,
    "mcap_max": 100_000_000,
    "min_liquidity": 50_000,       # aggregate across all of a token's pools
    "min_vol24": 25_000,
    "min_age_hours": 24,           # anything younger is a coin-flip, not a screen
    "min_buyers24": 30,
    "position_usd": 1_000,         # what you intend to deploy
    "max_slippage": 0.02,          # 2% one-way price impact tolerance
}

# --------------------------------------------------------------------- scoring
# Pillar weights. Must sum to 1.0; asserted at import.
WEIGHTS = {
    "exit":      0.30,   # can I get my money back out
    "holders":   0.26,   # who is already in there, and is it real
    "momentum":  0.18,   # is it moving, and am I early or late
    "meme":      0.14,   # does it have the qualities memes need
    "safety":    0.12,   # contract + structure risk
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "pillar weights must sum to 1.0"

# Hard gates. A token failing any of these is reported but never ranked as
# investable -- they are disqualifiers, not score deductions.
GATES = {
    "min_liq_to_mcap": 0.010,     # <1% liquidity/mcap = you cannot exit
    "max_top10_share": 0.75,      # 10 wallets holding >75% = they decide, not you
    "min_unique_buyers": 15,
    "max_h24_gain": 900.0,        # already went 10x today; you are exit liquidity
}
