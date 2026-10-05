"""Wash-trading detector.

Built from a live case (ROBIN, 2026-09-23) where 23 wallets ran an identical
buy/buy/buy/sell cycle at 185s/186s/191s gaps, each sell dumping exactly the
three preceding buys. Trade *sizes* were randomized -- 288 distinct sizes in
300 trades -- so amount-fingerprinting finds nothing. What gives these away is
structure, not size:

  round-trip   a wallet whose buy USD and sell USD match within a few percent
               has taken no position; it has manufactured volume.
  cadence      unrelated wallets sharing an inter-trade gap to the second are
               driven by one scheduler.

Both are cheap and run off GeckoTerminal's free trades endpoint. Neither is
proof on its own -- a market maker also round-trips -- so the score is
reported as a share of volume, not a boolean, and the caller decides.
"""
import collections
import datetime

from memescan import config as C
from memescan.net import get_json

GT_INTERVAL = 4.0
ROUND_TRIP_TOL = 0.05      # buys within 5% of sells -> no net position
CADENCE_TOL = 3.0          # seconds; gaps this close across wallets = one clock
MIN_TRADES = 20            # below this the structure tests are meaningless


def fetch_trades(pool, network="robinhood"):
    d = get_json(f"{C.GT_BASE}/networks/{network}/pools/{pool}/trades", "gt", GT_INTERVAL)
    return [r["attributes"] for r in ((d or {}).get("data") or [])]


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def score(trades):
    """Return a dict of wash indicators for one pool's recent trades."""
    if len(trades) < MIN_TRADES:
        return {"n_trades": len(trades), "verdict": "insufficient", "flagged_vol_share": None}

    by = collections.defaultdict(list)
    for t in trades:
        by[t.get("tx_from_address")].append(t)

    flagged, gaps_by_wallet = set(), {}
    for a, v in by.items():
        b = sum(float(t.get("volume_in_usd") or 0) for t in v if t.get("kind") == "buy")
        s = sum(float(t.get("volume_in_usd") or 0) for t in v if t.get("kind") == "sell")
        if b > 0 and s > 0 and abs(b - s) / max(b, s) < ROUND_TRIP_TOL:
            flagged.add(a)
        if len(v) >= 3:
            ts = sorted(_ts(t["block_timestamp"]) for t in v)
            gaps_by_wallet[a] = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]

    # cadence clustering: a gap value shared by 3+ unrelated wallets is a clock
    buckets = collections.defaultdict(set)
    for a, gaps in gaps_by_wallet.items():
        for g in gaps:
            buckets[round(g / CADENCE_TOL)].add(a)
    synced = set()
    for _, ws in buckets.items():
        if len(ws) >= 3:
            synced |= ws
    flagged |= synced

    tot = sum(float(t.get("volume_in_usd") or 0) for t in trades) or 1.0
    fv = sum(float(t.get("volume_in_usd") or 0)
             for t in trades if t.get("tx_from_address") in flagged)
    sizes = {round(float(t.get("volume_in_usd") or 0), 4) for t in trades}

    share = fv / tot
    return {
        "n_trades": len(trades), "n_wallets": len(by),
        "flagged_wallets": len(flagged), "synced_wallets": len(synced),
        "flagged_vol_share": share,
        "size_diversity": len(sizes) / len(trades),
        "verdict": "heavy" if share >= 0.40 else "some" if share >= 0.15 else "clean",
    }


def check(pool, network="robinhood"):
    return score(fetch_trades(pool, network))
