"""Retrospective case-control over the past N weeks, read from chain history.

The echo thesis cannot be backtested this way -- it needs to know what was
trending on Solana/Base at each past moment, and we only began recording that
three days ago. What CAN be reconstructed is the case-control question, which
depends only on facts that are permanently on chain:

    among tokens that actually got traction, what separated the winners
    from the ones that went to zero?

Three stages, each checkpointed into `hist_token` so the job resumes after a
rate-limit wall or a restart rather than starting over:

  1. sample   pick windows across the period, read mint events, store tokens
  2. features replay each token's first 3h of Transfers -> holders, xfers,
              concentration, whether the deployer still holds
  3. outcome  OHLCV peak vs open, but ONLY for tokens that showed traction --
              the expensive call is spent where it can change an answer

Stage 3 is gated on stage 2 because ~95% of launches never trade at all.
Running outcomes on those would cost hours and tell us only that dead tokens
are dead -- the comparison that matters is winners vs tokens that DID trade
and still failed.
"""
import collections
import random
import time

from memescan import config as C
from memescan.net import get_json, rpc

from . import db, onchain

ZERO = "0x" + "0" * 64
BPH = int(3600 / C.BLOCK_TIME_SEC)
TRACTION_HOLDERS = 50       # below this a token never really traded

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_token (
    address     TEXT PRIMARY KEY,
    symbol      TEXT,
    name        TEXT,
    mint_block  INTEGER,
    sampled_at  INTEGER,
    holders_3h  INTEGER,
    xfers_3h    INTEGER,
    top1_3h     REAL,
    top10_3h    REAL,
    dev_3h      REAL,
    peak_x      REAL,
    open_mcap   REAL,
    peak_mcap   REAL,
    stage       INTEGER DEFAULT 1   -- 1 sampled, 2 featured, 3 outcome, -1 failed
);
CREATE INDEX IF NOT EXISTS ix_hist_stage ON hist_token(stage);
"""

ALIGN_SCHEMA = """
ALTER TABLE hist_token ADD COLUMN entry_px REAL;
ALTER TABLE hist_token ADD COLUMN ret_aligned REAL;
ALTER TABLE hist_token ADD COLUMN peak_aligned REAL;
ALTER TABLE hist_token ADD COLUMN exit_reason TEXT;
"""


def init(con):
    con.executescript(SCHEMA)
    con.commit()


def _logs_adaptive(addr, frm, to, depth=0):
    out = rpc("eth_getLogs", [{"address": addr, "topics": [C.TRANSFER_TOPIC],
                               "fromBlock": hex(frm), "toBlock": hex(to)}], C.RPC_URL)
    if not isinstance(out, dict):
        return out, 0
    m = str(out.get("__error__", "")).lower()
    if "429" in m and depth < 6:
        time.sleep(8)
        return _logs_adaptive(addr, frm, to, depth + 1)
    if ("exceeds limit" in m or "timed out" in m) and to - frm > 300 and depth < 10:
        mid = (frm + to) // 2
        a, f1 = _logs_adaptive(addr, frm, mid, depth + 1)
        time.sleep(0.3)
        b, f2 = _logs_adaptive(addr, mid + 1, to, depth + 1)
        return a + b, f1 + f2
    return [], 1


def stage1_sample(con, days=28, windows=40, seed=5):
    """Read mint events from `windows` moments spread across the period."""
    head = onchain.head_block()
    if not head:
        return 0
    span = int(days * 86400 / C.BLOCK_TIME_SEC)
    rnd = random.Random(seed)
    picks = sorted(rnd.sample(range(head - span, head - BPH * 4), windows))
    added = 0
    for frm in picks:
        to = frm + int(300 / C.BLOCK_TIME_SEC)
        mints, failed = onchain.scan_mints_adaptive(frm, to)
        if failed or not mints:
            continue
        meta = onchain.batch_metadata(list(mints))
        rows = [(a, (meta.get(a, {}).get("symbol") or "")[:24],
                 (meta.get(a, {}).get("name") or "")[:48], b, db.now())
                for a, b in mints.items() if onchain.is_launch(meta.get(a, {}))]
        con.executemany("INSERT OR IGNORE INTO hist_token"
                        " (address,symbol,name,mint_block,sampled_at) VALUES (?,?,?,?,?)", rows)
        con.commit()
        added += len(rows)
        time.sleep(1.0)
    return added


def stage2_features(con, limit=400):
    """Replay each token's first 3 hours of Transfer logs."""
    rows = con.execute("SELECT address, mint_block FROM hist_token"
                       " WHERE stage=1 LIMIT ?", (limit,)).fetchall()
    done = 0
    for r in rows:
        logs, failed = _logs_adaptive(r["address"], r["mint_block"],
                                      r["mint_block"] + 3 * BPH)
        if failed:
            con.execute("UPDATE hist_token SET stage=-1 WHERE address=?", (r["address"],))
            continue
        bal, dev = collections.Counter(), None
        for l in logs:
            if len(l.get("topics", [])) < 3:
                continue
            f = ("0x" + l["topics"][1][-40:]).lower()
            t = ("0x" + l["topics"][2][-40:]).lower()
            v = int(l["data"], 16) if l.get("data") and l["data"] != "0x" else 0
            if dev is None and f == "0x" + "0" * 40:
                dev = t
            bal[t] += v
            bal[f] -= v
        pos = {k: v for k, v in bal.items() if v > 0 and k != "0x" + "0" * 40}
        tot = sum(pos.values()) or 1
        top = sorted(pos.values(), reverse=True)
        con.execute(
            "UPDATE hist_token SET holders_3h=?, xfers_3h=?, top1_3h=?, top10_3h=?,"
            " dev_3h=?, stage=2 WHERE address=?",
            (len(pos), len(logs), (top[0] / tot * 100) if top else 0,
             (sum(top[:10]) / tot * 100) if top else 0,
             (pos.get(dev, 0) / tot * 100) if dev else 0, r["address"]))
        con.commit()
        done += 1
        time.sleep(0.4)
    return done


def stage3_outcome(con, limit=200):
    """OHLCV peak, only for tokens that showed traction."""
    rows = con.execute("SELECT address FROM hist_token WHERE stage=2"
                       " AND holders_3h >= ? LIMIT ?", (TRACTION_HOLDERS, limit)).fetchall()
    done = 0
    for r in rows:
        d = get_json(f"{C.GT_BASE}/networks/{C.GT_NETWORK}/tokens/{r['address']}/pools",
                     "gt", 4.0)
        pools = (d or {}).get("data") or []
        if not pools:
            con.execute("UPDATE hist_token SET stage=3, peak_x=0 WHERE address=?", (r["address"],))
            con.commit()
            continue
        p = max(pools, key=lambda x: float(x["attributes"].get("reserve_in_usd") or 0))
        o = get_json(f"{C.GT_BASE}/networks/{C.GT_NETWORK}/pools/"
                     f"{p['attributes']['address']}/ohlcv/hour?aggregate=1&limit=720", "gt", 4.0)
        ol = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not ol:
            con.execute("UPDATE hist_token SET stage=3, peak_x=0 WHERE address=?", (r["address"],))
            con.commit()
            continue
        ol.sort(key=lambda x: x[0])
        op = ol[0][1] or 0
        hi = max(x[2] for x in ol)
        fdv = float(p["attributes"].get("fdv_usd") or 0)
        px = float(p["attributes"].get("base_token_price_usd") or 0)
        sup = (fdv / px) if px else 0
        con.execute("UPDATE hist_token SET stage=3, peak_x=?, open_mcap=?, peak_mcap=?"
                    " WHERE address=?",
                    ((hi / op) if op else 0, op * sup, hi * sup, r["address"]))
        con.commit()
        done += 1
    return done


# The decision point for the traction filter is 3h after mint (that is when
# holders_3h is knowable), so the earliest honest entry is the NEXT hourly bar.
ENTRY_HOUR = 4
TP, SL, TIMEOUT_H = 1.00, -0.50, 24


def stage4_align(con, limit=250):
    """Re-score outcomes under the prospective study's exit rule.

    `peak_x` is lifetime peak from OHLCV: unrealizable, unbounded in time, and
    not comparable to anything the live pipeline measures. This replays the
    same bars under delayed entry and a mechanical exit (+100% / -50% / 24h),
    so the traction filter and the echo signal can finally be read on one
    scale. Hourly bars are the finest granularity history offers, so entry is
    a whole hour late -- if anything this UNDERSTATES what a live trader with
    minute data could do.
    """
    head = onchain.head_block()
    now = time.time()
    rows = con.execute("SELECT address, mint_block FROM hist_token"
                       " WHERE stage=3 AND holders_3h>=? AND ret_aligned IS NULL"
                       " LIMIT ?", (TRACTION_HOLDERS, limit)).fetchall()
    done = 0
    for r in rows:
        mint_ts = now - (head - r["mint_block"]) * C.BLOCK_TIME_SEC
        d = get_json(f"{C.GT_BASE}/networks/{C.GT_NETWORK}/tokens/{r['address']}/pools",
                     "gt", 4.0)
        pools = (d or {}).get("data") or []
        if not pools:
            con.execute("UPDATE hist_token SET ret_aligned=0, exit_reason='nopool'"
                        " WHERE address=?", (r["address"],)); con.commit(); continue
        p = max(pools, key=lambda x: float(x["attributes"].get("reserve_in_usd") or 0))
        o = get_json(f"{C.GT_BASE}/networks/{C.GT_NETWORK}/pools/"
                     f"{p['attributes']['address']}/ohlcv/hour?aggregate=1&limit=720", "gt", 4.0)
        ol = (((o or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        if not ol:
            con.execute("UPDATE hist_token SET ret_aligned=0, exit_reason='nodata'"
                        " WHERE address=?", (r["address"],)); con.commit(); continue
        ol.sort(key=lambda x: x[0])
        entry_ts = mint_ts + ENTRY_HOUR * 3600
        bars = [b for b in ol if b[0] >= entry_ts]
        if not bars:
            con.execute("UPDATE hist_token SET ret_aligned=0, exit_reason='late'"
                        " WHERE address=?", (r["address"],)); con.commit(); continue
        p0 = bars[0][1] or 0
        if p0 <= 0:
            con.execute("UPDATE hist_token SET ret_aligned=0, exit_reason='nopx'"
                        " WHERE address=?", (r["address"],)); con.commit(); continue
        deadline = bars[0][0] + TIMEOUT_H * 3600
        peak, ret, why = 0.0, None, "open"
        for b in bars:
            hi, lo, close = b[2], b[3], b[4]
            peak = max(peak, hi / p0 - 1.0)
            if lo / p0 - 1.0 <= SL:      # stop checked first: worst case within the bar
                ret, why = SL, "sl"; break
            if hi / p0 - 1.0 >= TP:
                ret, why = TP, "tp"; break
            if b[0] >= deadline:
                ret, why = close / p0 - 1.0, "timeout"; break
        if ret is None:
            ret, why = bars[-1][4] / p0 - 1.0, "open"
        con.execute("UPDATE hist_token SET entry_px=?, ret_aligned=?, peak_aligned=?,"
                    " exit_reason=? WHERE address=?", (p0, ret, peak, why, r["address"]))
        con.commit(); done += 1
    return done


def run(days=28, windows=40):
    con = db.connect()
    init(con)
    print(f"[stage 1] sampling {windows} windows across {days} days...", flush=True)
    n = stage1_sample(con, days, windows)
    print(f"[stage 1] {n} launches recorded", flush=True)
    while True:
        d = stage2_features(con)
        left = con.execute("SELECT COUNT(*) FROM hist_token WHERE stage=1").fetchone()[0]
        print(f"[stage 2] featured {d}, {left} remaining", flush=True)
        if not d:
            break
    while True:
        d = stage3_outcome(con)
        left = con.execute("SELECT COUNT(*) FROM hist_token WHERE stage=2"
                           " AND holders_3h>=?", (TRACTION_HOLDERS,)).fetchone()[0]
        print(f"[stage 3] outcomes {d}, {left} remaining", flush=True)
        if not d:
            break
    print("[done]", flush=True)


if __name__ == "__main__":
    run()
