"""Bundle detection and exit tracking.

A bundled launch is one where many wallets that look independent acquire the
float in the same instant -- one desk behind dozens of addresses. Measured on
four known tokens, BLISS-b had 82 wallets take 78% of float inside one second,
while legitimate ROBIN had 6 wallets on 1.4%.

The important finding is that **bundling is not a rug signal**. PENPE was
bundled (28 wallets, 52%) and went 50x; BLISS-a rugged while looking clean.
Bundling says one entity controls the float -- it does not say which way they
will go. What distinguishes the rug is the bundle EXITING, and that is what
this module tracks: identify the cohort at launch, then watch that specific
set of wallets and flag when they leave together.

Two measurement traps, both found the hard way:

  The mint recipient holds 100% of supply by construction. Leave it in and
  every token scores as maximally bundled -- the first version of this did
  exactly that and reported a 97% median "bundle share" across a clean cohort.

  ERC-721 mints and LP position NFTs pollute Transfer streams; callers should
  pass real ERC-20s only (see onchain.is_launch).
"""
import collections
import time

from memescan import config as C
from memescan.net import rpc

ZERO_TOPIC = "0x" + "0" * 64
BURN = "0x" + "0" * 40
CLUMP_BLOCKS = 10          # ~1 second on this chain
BUNDLE_MIN_WALLETS = 5
BUNDLE_MIN_SHARE = 30.0    # percent of non-mint float
EXIT_ALERT_PCT = 25.0      # cohort shedding this much of its peak = leaving


def _logs(addr, frm, to, depth=0):
    o = rpc("eth_getLogs", [{"address": addr, "topics": [C.TRANSFER_TOPIC],
                             "fromBlock": hex(frm), "toBlock": hex(to)}], C.RPC_URL)
    if not isinstance(o, dict):
        return o, 0
    m = str(o.get("__error__", "")).lower()
    if "429" in m and depth < 5:
        time.sleep(6)
        return _logs(addr, frm, to, depth + 1)
    if ("exceeds limit" in m or "timed out" in m) and to - frm > 200 and depth < 8:
        mid = (frm + to) // 2
        a, f1 = _logs(addr, frm, mid, depth + 1)
        b, f2 = _logs(addr, mid + 1, to, depth + 1)
        return a + b, f1 + f2
    return [], 1


def mint_info(addr):
    """(mint_block, {addresses the mint paid out to}) or None."""
    m = rpc("eth_getLogs", [{"address": addr, "topics": [C.TRANSFER_TOPIC, ZERO_TOPIC],
                             "fromBlock": "0x0", "toBlock": "latest"}], C.RPC_URL)
    if isinstance(m, dict) or not m:
        return None
    return (int(m[0]["blockNumber"], 16),
            {("0x" + x["topics"][2][-40:]).lower() for x in m})


def _replay(logs, skip):
    """-> (balances, first_seen_block) excluding `skip` addresses and the burn hole."""
    bal, first = collections.Counter(), {}
    for l in sorted(logs, key=lambda x: int(x["blockNumber"], 16)):
        if len(l.get("topics", [])) < 3:
            continue
        b = int(l["blockNumber"], 16)
        f = ("0x" + l["topics"][1][-40:]).lower()
        t = ("0x" + l["topics"][2][-40:]).lower()
        v = int(l["data"], 16) if l.get("data") and l["data"] != "0x" else 0
        bal[t] += v
        bal[f] -= v
        if t not in first:
            first[t] = b
    return {k: v for k, v in bal.items() if v > 0 and k != BURN and k not in skip}, first


def profile(addr, window_blocks=600):
    """Identify the bundle cohort from the token's first ~60 seconds."""
    mi = mint_info(addr)
    if not mi:
        return None
    mb, sinks = mi
    logs, failed = _logs(addr, mb, mb + window_blocks)
    if failed or not logs:
        return None
    pos, first = _replay(logs, sinks)
    if not pos:
        return {"mint_block": mb, "wallets": [], "share": 0.0, "holders": 0,
                "bundled": False, "cohort_amount": 0}
    tot = sum(pos.values()) or 1
    clump = collections.defaultdict(list)
    for a in pos:
        clump[(first[a] - mb) // CLUMP_BLOCKS].append(a)
    _, ws = max(clump.items(), key=lambda kv: len(kv[1]))
    amt = sum(pos[a] for a in ws)
    share = amt / tot * 100
    return {"mint_block": mb, "wallets": ws, "share": share, "holders": len(pos),
            "cohort_amount": amt,
            "bundled": len(ws) >= BUNDLE_MIN_WALLETS and share >= BUNDLE_MIN_SHARE}


def cohort_balance(addr, wallets, chunk=120):
    """Aggregate CURRENT balance of `wallets`, via one Multicall3 call per chunk.

    The obvious implementation -- replay Transfer logs from the mint to head --
    is O(token age): for a 450-hour-old token that is ~16M blocks and does not
    finish. balanceOf through Multicall3 answers the same question exactly, in
    one call per ~120 wallets, regardless of how old the token is.
    """
    from . import onchain
    total = 0
    ws = list(wallets)
    for i in range(0, len(ws), chunk):
        part = ws[i:i + chunk]
        calls = [(addr, "0x70a08231" + w[2:].rjust(64, "0")) for w in part]
        r = rpc("eth_call", [{"to": onchain.MULTICALL3,
                              "data": onchain._enc_aggregate3(calls)}, "latest"], C.RPC_URL)
        res = onchain._dec_aggregate3(r)
        if len(res) != len(part):
            return None
        for ok, data in res:
            if ok and data and data != "0x":
                total += int(data, 16)
    return total


def exit_state(addr, prof, to_block=None):
    """How much of the bundle's original position is still held.

    Returns {held_pct, exited_pct, alert}. `alert` fires once the cohort has
    shed EXIT_ALERT_PCT of what it started with -- the rug in progress, as
    opposed to the bundle merely existing.
    """
    if not prof or not prof["wallets"] or not prof["cohort_amount"]:
        return None
    now = cohort_balance(addr, prof["wallets"])
    if now is None:
        return None
    held = now / prof["cohort_amount"] * 100
    return {"held_pct": held, "exited_pct": max(0.0, 100 - held),
            "alert": (100 - held) >= EXIT_ALERT_PCT}
