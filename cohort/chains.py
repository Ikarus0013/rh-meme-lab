"""Per-trader chain attribution for the FOMO top-150.

FOMO ranks traders by account-wide PnL across every chain. This module answers
the question that ranking hides: *where* does each trader actually earn?

Three sources, deliberately kept separate because they measure different
things and disagree in informative ways:

  topTokens   (free, in the leaderboard)  -- current holdings, so mostly
              UNREALISED pnl, attributed by networkId.
  relay       (/swaps?source=relay)       -- actual cross-chain legs. Covers
              Relay-routed flow only, so a trader who never bridges is
              under-counted.
  trades      (/trades?status=closed)     -- REALISED pnl per closed position.
              The cleanest measure, and the least available: FOMO's upstream
              returns 503 for it most of the time.

Where realised and unrealised disagree, that gap is the finding, not an error
to reconcile away -- a trader whose paper gains are large and whose closed
trades are negative has not yet proven anything.
"""
import collections
import json
import time

from . import client

NET = {1399811149: "solana", 792703809: "solana", 4663: "robinhood",
       8453: "base", 56: "bsc", 1: "ethereum", 42161: "arbitrum"}


def leaderboard(window="30d", limit=150):
    d = client.get(f"/v2/leaderboard/{window}", {"limit": limit}, cost_key="leaderboard")
    return d.get("traders") or []


def toptoken_split(row):
    """{chain: pnl} from the leaderboard row -- free, no extra call."""
    per = collections.Counter()
    for t in (row.get("topTokens") or []):
        per[NET.get(t.get("networkId"), str(t.get("networkId")))] += float(t.get("pnl") or 0)
    return per


def rank_by_rh(rows):
    """Re-rank the board by Robinhood-Chain pnl -- the ranking FOMO omits."""
    out = []
    for r in rows:
        per = toptoken_split(r)
        tot = sum(abs(v) for v in per.values()) or 1.0
        out.append({
            "rank": r["rank"], "handle": r["handle"],
            "evm": (r.get("wallets") or {}).get("evm"),
            "pnl_total": r.get("pnlUsd") or 0,
            "pnl_rh": per.get("robinhood", 0.0),
            "rh_share": per.get("robinhood", 0.0) / tot,
            "split": dict(per),
        })
    out.sort(key=lambda x: -x["pnl_rh"])
    return out


def relay_chains(handle):
    """{chain: leg_count} from actual Relay-routed legs, or None on failure."""
    d = client.get(f"/v2/users/{handle}/swaps", {"source": "relay"}, quiet=True)
    if "__error__" in d:
        return None
    return d.get("chains")


def sweep(handles, pause=1.0):
    out = {}
    for h in handles:
        out[h] = relay_chains(h)
        time.sleep(pause)
    return out
