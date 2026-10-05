"""SQLite store for the cross-chain echo study.

One table per concern, and one rule that governs the whole schema: every
column written at detection time must be knowable at detection time. No
outcome data ever lands in `echo_link` or `rh_launch` -- outcomes live in
`obs`, timestamped, so a backtest can only ever read what a live trader
would have had in hand. Lookahead bias is a schema property here, not a
discipline you have to remember.
"""
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), "echo.db")

SCHEMA = """
-- Every Robinhood Chain launch we witness. This is the DENOMINATOR: without
-- the clones that died, "echo clones pump" is an unfalsifiable claim.
CREATE TABLE IF NOT EXISTS rh_launch (
    address         TEXT PRIMARY KEY,
    symbol          TEXT,
    name            TEXT,
    pool            TEXT,
    dex             TEXT,
    pool_created_at TEXT,
    first_seen      INTEGER,
    init_liq        REAL,
    init_fdv        REAL,
    cohort          TEXT,     -- 'echo' | 'control'
    source          TEXT      -- 'gt' | 'onchain'  (which feed found it)
);
CREATE INDEX IF NOT EXISTS ix_launch_symbol ON rh_launch(symbol);
CREATE INDEX IF NOT EXISTS ix_launch_cohort ON rh_launch(cohort, first_seen);
CREATE INDEX IF NOT EXISTS ix_launch_source ON rh_launch(source);

-- Addresses the on-chain feed has already emitted, so a token that mints
-- again is not recounted as a new launch.
CREATE TABLE IF NOT EXISTS onchain_seen (
    address   TEXT PRIMARY KEY,
    first_block INTEGER,
    seen_at   INTEGER
);

-- Tickers currently running on other chains. Refreshed on a slow cadence;
-- we keep every snapshot rather than upserting, so a parent's state at the
-- moment of any given detection is recoverable.
CREATE TABLE IF NOT EXISTS hot_ticker (
    symbol          TEXT,
    chain           TEXT,
    address         TEXT,
    seen_at         INTEGER,
    mcap            REAL,
    liq             REAL,
    vol24           REAL,
    pct24           REAL,
    pool_created_at TEXT,
    PRIMARY KEY (symbol, chain, address, seen_at)
);
CREATE INDEX IF NOT EXISTS ix_hot_symbol ON hot_ticker(symbol, seen_at);

-- An RH launch whose ticker matched a hot ticker elsewhere. Every field is
-- information a trader would have had at `detected_at`.
CREATE TABLE IF NOT EXISTS echo_link (
    rh_address      TEXT,
    parent_chain    TEXT,
    parent_address  TEXT,
    symbol          TEXT,
    detected_at     INTEGER,
    parent_mcap     REAL,
    parent_liq      REAL,
    parent_vol24    REAL,
    parent_pct24    REAL,
    parent_age_h    REAL,
    lag_h           REAL,     -- RH launch time minus parent launch time
    PRIMARY KEY (rh_address, parent_chain, parent_address)
);

-- What Jev said about a candidate, kept so a classification can always be
-- interrogated. Scores are written once, at classification time, from
-- information available then -- never refreshed with hindsight.
CREATE TABLE IF NOT EXISTS jev_score (
    address         TEXT PRIMARY KEY,
    live_narrative  REAL,     -- noul: is the parent mid-run right now
    attention       REAL,     -- score 0-4: likely to draw real buying
    confidence      REAL,     -- Jev's own certainty on `attention`
    parent_symbol   TEXT,
    scored_at       INTEGER
);

-- Outcome time series, for echo and control alike. Sampled densely enough
-- that entry can be simulated at +5/+15/+60min after detection.
CREATE TABLE IF NOT EXISTS obs (
    address   TEXT,
    ts        INTEGER,
    price     REAL,
    fdv       REAL,
    liq       REAL,
    vol_h1    REAL,
    buys_h1   INTEGER,
    sells_h1  INTEGER,
    PRIMARY KEY (address, ts)
);

-- Socials over time. DexScreener profiles are submitted by teams, usually
-- AFTER a token draws attention -- so "winners have Twitter" is worthless
-- unless we know the Twitter was there BEFORE the run. One row per change,
-- so the first row is the state at detection and later rows date any addition.
CREATE TABLE IF NOT EXISTS social_snapshot (
    address  TEXT,
    ts       INTEGER,
    tw       INTEGER,
    tg       INTEGER,
    web      INTEGER,
    img      INTEGER,
    PRIMARY KEY (address, ts)
);

-- Operational log, so a gap in the data is visible as a gap rather than
-- silently looking like "no launches happened".
CREATE TABLE IF NOT EXISTS cycle_log (
    ts          INTEGER PRIMARY KEY,
    kind        TEXT,
    n_seen      INTEGER,
    n_new       INTEGER,
    n_echo      INTEGER,
    n_tracked   INTEGER,
    note        TEXT
);
"""


def connect(path=DB_PATH):
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # WAL lets the analysis CLI read while the collector is mid-write.
    con.execute("PRAGMA journal_mode=WAL")
    return con


def now():
    return int(time.time())
