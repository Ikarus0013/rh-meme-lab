"""On-chain launch feed: every token mint, read straight from the chain.

Replaces the GeckoTerminal `new_pools` feed, which caps at 10 pages x 20 rows
-- about seven minutes of history at this chain's launch rate -- and so loses
launches permanently whenever the collector stalls or falls behind.

Two facts make this tractable:

  Every ERC-20 mint is a `Transfer` from the zero address, whatever contract
  created the token. Sampling 8 launches turned up FOUR different launchpad
  entry points, so watching launchpads means chasing a list that keeps
  changing; watching mint events catches all of them by construction.

  Multicall3 is deployed at the canonical address, so symbol/name/decimals for
  a whole window of tokens costs ONE rpc call instead of three per token. At
  ~129 tokens per 5-minute window that is the difference between 387 calls and
  1, which is the difference between working and being rate-limited.

Window sizing is empirical: a 5-minute window returns ~8.7k logs and serves in
under 2s; 15 minutes exceeds the node's log cap and fails outright.
"""
import time

from memescan import config as C
from memescan.net import rpc

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
ZERO_TOPIC = "0x" + "0" * 64
SEL = {"symbol": "0x95d89b41", "name": "0x06fdde03",
       "decimals": "0x313ce567", "totalSupply": "0x18160ddd"}

# 5 min of blocks. Larger windows exceed the node's returned-log limit.
WINDOW_SEC = 300
MAX_WINDOW_BLOCKS = int(WINDOW_SEC / C.BLOCK_TIME_SEC)


# ------------------------------------------------------------------ abi utils
def _pad(h):
    h = h[2:] if h.startswith("0x") else h
    return h.rjust(64, "0")


def _enc_aggregate3(calls):
    """aggregate3((address,bool,bytes)[]) -> calldata."""
    tuples = []
    for target, data in calls:
        cd = data[2:]
        tuples.append(_pad(hex(int(target, 16))) + _pad("1") + _pad(hex(96))
                      + _pad(hex(len(cd) // 2))
                      + cd.ljust(((len(cd) + 63) // 64) * 64, "0"))
    offs, cur = "", 32 * len(tuples)
    for t in tuples:
        offs += _pad(hex(cur))
        cur += len(t) // 2
    return ("0x82ad56cb" + _pad(hex(32)) + _pad(hex(len(tuples)))
            + offs + "".join(tuples))


def _dec_aggregate3(ret):
    """-> [(success, returndata_hex), ...]"""
    if not isinstance(ret, str) or len(ret) < 130:
        return []
    b = ret[2:]
    w = lambda i: b[i * 64:(i + 1) * 64]
    base = int(w(0), 16) // 32
    n = int(w(base), 16)
    out = []
    for i in range(n):
        off = base + 1 + int(w(base + 1 + i), 16) // 32
        success = int(w(off), 16) == 1
        doff = off + int(w(off + 1), 16) // 32
        ln = int(w(doff), 16)
        out.append((success, "0x" + b[(doff + 1) * 64:(doff + 1) * 64 + ln * 2]))
    return out


def _dec_string(h):
    """Decode an ABI string, falling back to bytes32 (older tokens use it)."""
    if not h or h == "0x":
        return ""
    b = h[2:]
    try:
        if len(b) >= 128:
            ln = int(b[64:128], 16)
            if 0 < ln <= 256:
                return bytes.fromhex(b[128:128 + ln * 2]).decode("utf-8", "replace").strip()
        return bytes.fromhex(b[:64]).decode("utf-8", "replace").replace("\x00", "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------- reads
def head_block(rpc_url=None):
    r = rpc("eth_blockNumber", [], rpc_url or C.RPC_URL)
    return int(r, 16) if isinstance(r, str) else None


def scan_mints(from_block, to_block, rpc_url=None):
    """Distinct token addresses minted in a block range.

    Filtering on `from == zero` keeps the result set small enough that the node
    answers a 5-minute window in under two seconds. Returns
    {address: earliest_block_seen} or {"__error__": msg}.
    """
    logs = rpc("eth_getLogs", [{"topics": [C.TRANSFER_TOPIC, ZERO_TOPIC],
                                "fromBlock": hex(from_block), "toBlock": hex(to_block)}],
               rpc_url or C.RPC_URL)
    if isinstance(logs, dict):
        return {"__error__": logs.get("__error__", "unknown")}
    out = {}
    for l in logs:
        a = l["address"].lower()
        b = int(l["blockNumber"], 16)
        if a not in out or b < out[a]:
            out[a] = b
    return out


def batch_metadata(addresses, rpc_url=None, chunk=120):
    """symbol/name/decimals/totalSupply for many tokens via Multicall3.

    One rpc call per `chunk` tokens rather than four per token. allowFailure is
    set, so a token missing an optional method degrades to a blank field
    instead of reverting the whole batch.
    """
    out = {}
    addrs = list(addresses)
    for i in range(0, len(addrs), chunk):
        part = addrs[i:i + chunk]
        calls = [(a, SEL[f]) for a in part
                 for f in ("symbol", "name", "decimals", "totalSupply")]
        r = rpc("eth_call", [{"to": MULTICALL3, "data": _enc_aggregate3(calls)}, "latest"],
                rpc_url or C.RPC_URL)
        res = _dec_aggregate3(r)
        if len(res) != len(calls):
            continue
        for j, a in enumerate(part):
            sym_ok, sym = res[j * 4]
            nam_ok, nam = res[j * 4 + 1]
            dec_ok, dec = res[j * 4 + 2]
            sup_ok, sup = res[j * 4 + 3]
            out[a] = {
                "symbol": _dec_string(sym) if sym_ok else "",
                "name": _dec_string(nam) if nam_ok else "",
                "decimals": int(dec, 16) if dec_ok and dec != "0x" else None,
                "total_supply": int(sup, 16) if sup_ok and sup != "0x" else None,
            }
    return out


MIN_SPLIT_BLOCKS = 200          # floor before we stop subdividing


def scan_mints_adaptive(frm, to, rpc_url=None, _depth=0):
    """scan_mints over [frm, to], splitting the range when the node refuses it.

    This chain has a THIRD failure mode beyond the two already known: besides
    HTTP 429 (back off, same request will work) and "log query timed out",
    a dense range returns "logs matched by query exceeds limit of 10000".
    Backing off does not help -- the range itself is too big -- so the fix is
    to halve it. A fixed window cannot work because launch density varies:
    the same 5 minutes succeeded in one window and blew the cap in the next.

    Returns ({address: block}, n_failed_ranges). A non-zero failure count means
    the caller is missing launches and must not treat the window as complete.
    """
    out = rpc("eth_getLogs", [{"topics": [C.TRANSFER_TOPIC, ZERO_TOPIC],
                               "fromBlock": hex(frm), "toBlock": hex(to)}],
              rpc_url or C.RPC_URL)
    if not isinstance(out, dict):
        acc = {}
        for l in out:
            a = l["address"].lower()
            b = int(l["blockNumber"], 16)
            if a not in acc or b < acc[a]:
                acc[a] = b
        return acc, 0

    msg = str(out.get("__error__", "")).lower()
    splittable = ("exceeds limit" in msg or "timed out" in msg or "too large" in msg)
    if not splittable or to - frm <= MIN_SPLIT_BLOCKS or _depth > 8:
        return {}, 1
    mid = (frm + to) // 2
    a, f1 = scan_mints_adaptive(frm, mid, rpc_url, _depth + 1)
    b, f2 = scan_mints_adaptive(mid + 1, to, rpc_url, _depth + 1)
    for k, v in b.items():
        if k not in a or v < a[k]:
            a[k] = v
    return a, f1 + f2


def poll(since_block=None, rpc_url=None):
    """One pass: mints since `since_block` (default: one window back), enriched.

    Returns (tokens, head, error). `tokens` maps address -> metadata + block.
    """
    head = head_block(rpc_url)
    if head is None:
        return {}, None, "head unavailable"
    frm = max(0, since_block if since_block is not None else head - MAX_WINDOW_BLOCKS)
    if head - frm > MAX_WINDOW_BLOCKS:
        frm = head - MAX_WINDOW_BLOCKS      # never exceed the node's log cap
    mints, failed = scan_mints_adaptive(frm, head, rpc_url)
    meta = batch_metadata(list(mints), rpc_url)
    tokens = {a: {**meta.get(a, {}), "block": b} for a, b in mints.items()}
    # A failed sub-range is a hole in the denominator, so it is reported as an
    # error even though partial data came back -- silently returning the
    # partial set is exactly how this study would get quietly corrupted.
    return tokens, head, (f"{failed} sub-range(s) unreadable" if failed else None)


# ------------------------------------------------------------------ filtering
# Minting is not the same as launching. Three things pollute the raw feed:
#   - wrapped assets (every WETH deposit mints WETH)
#   - LP position NFTs (Uniswap v3/v4, Algebra) minted on every add-liquidity
#   - tokens that simply mint again later
# ERC-721 has no `decimals`, which separates position NFTs from real ERC-20s
# cleanly and without a maintained address list.
POSITION_HINTS = ("-POS", "POSM", "POSITION", "LP-", "-LP")


def is_launch(meta):
    """Does this mint look like a new ERC-20 token rather than chain plumbing?"""
    if meta.get("decimals") is None:
        return False                      # ERC-721 / non-standard -> not a token launch
    sym = (meta.get("symbol") or "").upper()
    if not sym or len(sym) > 24:
        return False
    if sym in C.INFRA_SYMBOLS or sym in C.EQUITY_SYMBOLS:
        return False
    if any(h in sym for h in POSITION_HINTS):
        return False
    if not meta.get("total_supply"):
        return False
    return True


def poll_launches(seen, since_block=None, rpc_url=None):
    """poll() filtered to plausible launches and deduped against `seen`.

    `seen` is a set of addresses already recorded; mutated in place. Returns
    (new_launches, head, error, stats).
    """
    tokens, head, err = poll(since_block, rpc_url)
    if err:
        return {}, head, err, {}
    kept, rejected_nft, rejected_other, dup = {}, 0, 0, 0
    for a, m in tokens.items():
        if a in seen:
            dup += 1
            continue
        if not is_launch(m):
            if m.get("decimals") is None:
                rejected_nft += 1
            else:
                rejected_other += 1
            continue
        seen.add(a)
        kept[a] = m
    return kept, head, None, {"raw": len(tokens), "kept": len(kept),
                              "nft": rejected_nft, "other": rejected_other, "dup": dup}
