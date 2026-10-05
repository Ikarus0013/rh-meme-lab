"""On-chain analysis over the public Robinhood Chain RPC.

Two jobs:
  1. Cheap contract sanity checks (eth_call).
  2. Full holder-set reconstruction by replaying ERC-20 Transfer logs.

(2) is the reason this tool can say anything real about "who is already in
there". The explorer sits behind Cloudflare and there is no free indexer, but
the RPC will serve eth_getLogs over ~2M-block windows, and this chain runs at
~0.1s/block -- so a token's entire life is a few dozen chunked calls.
"""
import time

from . import config as C
from .net import rpc

ZERO = "0x0000000000000000000000000000000000000000"

# 4-byte selectors
SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_DECIMALS = "0x313ce567"
SEL_OWNER = "0x8da5cb5b"


def _hex_int(h, default=0):
    if isinstance(h, dict):        # {"__error__": ...}
        return default
    try:
        return int(h, 16)
    except (TypeError, ValueError):
        return default


def head_block():
    return _hex_int(rpc("eth_blockNumber", [], C.RPC_URL))


def _call(to, selector):
    return rpc("eth_call", [{"to": to, "data": selector}, "latest"], C.RPC_URL)


def contract_facts(token):
    """Cheap per-token contract checks. All failures degrade to None."""
    facts = {"total_supply": None, "decimals": None,
             "owner": None, "renounced": None, "code_size": None}

    ts = _call(token, SEL_TOTAL_SUPPLY)
    if isinstance(ts, str):
        facts["total_supply"] = _hex_int(ts)

    dec = _call(token, SEL_DECIMALS)
    if isinstance(dec, str):
        facts["decimals"] = _hex_int(dec)

    own = _call(token, SEL_OWNER)
    if isinstance(own, str) and len(own) >= 66:
        addr = "0x" + own[-40:]
        facts["owner"] = addr
        # Zero owner == ownership renounced, the usual "cannot rug via admin"
        # signal. A live owner is not proof of malice, only of capability.
        facts["renounced"] = (addr.lower() == ZERO)

    code = rpc("eth_getCode", [token, "latest"], C.RPC_URL)
    if isinstance(code, str):
        facts["code_size"] = max(0, (len(code) - 2) // 2)

    return facts


def block_at_timestamp(ts_unix, head, head_ts):
    """Estimate a block number from a wall-clock timestamp.

    An estimate is fine here: it only sets the start of a log scan, and the
    scan is clamped with a safety margin below.
    """
    delta = head_ts - ts_unix
    est = head - int(delta / C.BLOCK_TIME_SEC)
    return max(0, est)


def head_timestamp(head):
    blk = rpc("eth_getBlockByNumber", [hex(head), False], C.RPC_URL)
    if isinstance(blk, dict):
        return _hex_int(blk.get("timestamp"))
    return None


ZERO_TOPIC = "0x" + "0" * 64


def find_mint_block(token, head):
    """Locate a token's first mint by querying only Transfer-from-zero events.

    Filtering on the `from` topic makes the result set tiny (usually a single
    log), so the node will serve a genesis-to-head range in well under a
    second -- where an unfiltered scan of the same range times out. This is
    what anchors a holder scan to actual deployment rather than to pool
    creation, which can be days later and silently truncates the replay.

    Returns (first_mint_block, total_minted) or (None, None).
    """
    res = rpc("eth_getLogs", [{
        "fromBlock": "0x0", "toBlock": hex(head), "address": token,
        "topics": [C.TRANSFER_TOPIC, ZERO_TOPIC],
    }], C.RPC_URL, tries=3, timeout=120)

    if isinstance(res, dict) or not res:
        return None, None
    blocks = [_hex_int(l.get("blockNumber")) for l in res]
    minted = sum(_hex_int(l.get("data")) for l in res)
    return (min(blocks) if blocks else None), minted


def _topic_to_addr(topic):
    return ("0x" + topic[-40:]).lower()


def fetch_transfer_logs(token, from_block, to_block, verbose=False,
                        start_span=None):
    """Chunked eth_getLogs over the Transfer topic, adapting the window size.

    Two distinct failures have to be told apart, or a busy token takes forever:

      * "log query timed out" -- the window holds too many logs. Halve it and
        retry immediately; retrying the same window is guaranteed to fail again.
      * "Too Many Requests" -- the window is fine, we are just going too fast.
        Sleep and retry the *same* window.

    A window that times out lowers a ceiling so the scan does not immediately
    oscillate back into it, but the ceiling lifts again after a sustained run
    of clean fetches -- launch regions are dense, the months afterwards are not.
    """
    logs = []
    skipped = []              # ranges we failed to read; these corrupt balances
    start = from_block
    span = start_span or C.MAX_LOG_RANGE
    consecutive_ok = 0
    rate_retries = 0          # per-window, so a hard rate limit cannot spin forever
    dense_retries = 0
    # Once a window size has proven too dense, never widen back into it --
    # each rediscovery costs a full server-side timeout (up to 90s).
    span_ceiling = C.MAX_LOG_RANGE

    while start <= to_block:
        end = min(start + span - 1, to_block)
        # tries=1: handle the retry policy here, where we know which failure
        # mode we are looking at.
        res = rpc("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end),
            "address": token, "topics": [C.TRANSFER_TOPIC],
        }], C.RPC_URL, tries=1, timeout=90)

        if isinstance(res, dict) and "__error__" in res:
            msg = res["__error__"].lower()
            if "too many" in msg or "rate" in msg or "429" in msg:
                rate_retries += 1
                if rate_retries > 8:
                    if verbose:
                        print(f"      ! rate limited persistently, skipping "
                              f"{start}-{end}")
                    skipped.append((start, end))
                    start = end + 1
                    rate_retries = 0
                    continue
                # Escalating backoff: the public RPC needs real breathing room.
                time.sleep(min(2.0 * rate_retries, 15.0))
                if verbose:
                    print(f"      rate limited, backoff #{rate_retries}")
                continue
            if span > C.MIN_LOG_RANGE:
                span_ceiling = max(C.MIN_LOG_RANGE, span // 2)
                span //= 2
                consecutive_ok = 0
                if verbose:
                    print(f"      window too dense, narrowing to {span:,} blocks")
                continue

            # Already at the narrowest window and still timing out. That is
            # usually the public node throttling us rather than genuine log
            # density, so back off and retry before giving up -- skipping a
            # range corrupts every balance downstream.
            dense_retries += 1
            if dense_retries <= 5:
                if verbose:
                    print(f"      min window still timing out, "
                          f"pausing {5*dense_retries}s (attempt {dense_retries}/5)")
                time.sleep(5.0 * dense_retries)
                continue

            if verbose:
                print(f"      ! skipping blocks {start}-{end} ({msg[:60]})")
            skipped.append((start, end))
            start = end + 1
            dense_retries = 0
            continue

        if res is None:
            # Transport-level failure; skip rather than stall the whole scan.
            if verbose:
                print(f"      ! no response for blocks {start}-{end}")
            skipped.append((start, end))
            start = end + 1
            continue

        logs.extend(res)
        rate_retries = 0
        dense_retries = 0
        if verbose:
            print(f"      blocks {start:,}-{end:,}: {len(res):,} transfers "
                  f"({len(logs):,} total)")
        start = end + 1

        # Widen cautiously after a run of clean fetches.
        # Widen after a sustained clean run. The ceiling rises with it: a
        # token's launch region is dense but the months after it are sparse,
        # so a permanently ratcheted-down window would crawl through the
        # quiet 95% of its history.
        consecutive_ok += 1
        if consecutive_ok >= 6:
            span_ceiling = min(C.MAX_LOG_RANGE, max(span_ceiling, span) * 2)
            span = min(span_ceiling, span * 2)
            consecutive_ok = 0

    return logs, skipped


def replay_balances(logs, exclude=(), total_supply=None, skipped=()):
    """Replay Transfer logs into balances, then partition out non-holders.

    Balances are built for *every* address first, so the positive balances sum
    to the full circulating supply. That sum is then checked against the
    contract's totalSupply to produce a `coverage` figure: if the log scan
    started after deployment, wallets that bought earlier and sold inside the
    window go net-negative and drop out, and coverage falls well below 1.0.
    Concentration stats computed on a partial replay are worse than no stats,
    so callers must check coverage before trusting them.

    exclude: pool/LP and contract addresses -- supply they hold is not a
    retail position and would otherwise dominate the top-holder table.
    """
    # Drop anything that is not a 20-byte address -- v4 pool IDs are 32 bytes
    # and can never match a holder key, so keeping them just hides the fact
    # that singleton liquidity is handled by detect_contract_holders instead.
    excl = {a.lower() for a in exclude if a and len(a) == 42}
    excl |= {a.lower() for a in C.BURN_ADDRESSES}
    balances = {}
    first_seen = {}
    n_transfers = 0

    for lg in logs:
        topics = lg.get("topics") or []
        # ERC-20 Transfer has exactly 3 topics; ERC-721 has 4 (tokenId indexed).
        if len(topics) != 3:
            continue
        val = _hex_int(lg.get("data"), 0)
        if val == 0:
            continue

        src = _topic_to_addr(topics[1])
        dst = _topic_to_addr(topics[2])
        blk = _hex_int(lg.get("blockNumber"))
        n_transfers += 1

        balances[src] = balances.get(src, 0) - val
        balances[dst] = balances.get(dst, 0) + val
        if dst not in first_seen:
            first_seen[dst] = blk

    # The zero address ends up hugely negative from mints; positive-only
    # filtering drops it and every other net-outflow address naturally.
    positive = {a: b for a, b in balances.items() if b > 0}
    reconstructed = sum(positive.values())

    coverage = None
    if total_supply and total_supply > 0:
        coverage = reconstructed / total_supply

    holders = {a: b for a, b in positive.items() if a not in excl}
    excluded_supply = reconstructed - sum(holders.values())

    return {
        "holders": holders,
        "first_seen": first_seen,
        "n_transfers": n_transfers,
        "coverage": coverage,
        "skipped": list(skipped),
        "reconstructed_supply": reconstructed,
        "excluded_supply": excluded_supply,
    }


# EIP-7702 delegation: an EOA whose code is exactly 0xef0100 || implementation.
# 23 bytes, and it means "still a user wallet", not "contract".
EIP7702_PREFIX = "0xef0100"
EIP7702_CODE_LEN = 23


def is_delegated_eoa(code):
    """True if this bytecode is an EIP-7702 delegation rather than a contract."""
    if not isinstance(code, str):
        return False
    return (code[:8].lower() == EIP7702_PREFIX
            and (len(code) - 2) // 2 == EIP7702_CODE_LEN)


def detect_contract_holders(holders, top_n=25, verbose=False):
    """Identify which of the largest holders are contracts, not wallets.

    This chain runs both pool architectures at once: v3-style DEXes expose a
    real 20-byte pool address (excludable by name), but Uniswap v4 and pons-v2
    are singletons whose pool "address" is a 32-byte pool ID -- the tokens
    actually sit in one shared PoolManager contract that no per-pool exclusion
    list will ever catch. Rather than enumerate singletons, routers, bridges
    and lockers, just ask the chain for bytecode.

    The subtlety is that "has bytecode" is NOT the same as "is a contract"
    here. Robinhood Chain is built around account abstraction -- the FOMO app
    uses it -- so a large share of ordinary retail wallets are EIP-7702
    delegated EOAs, which carry 23 bytes of delegation code. Those are exactly
    the holders we are trying to count. Excluding them would erase most of the
    retail base and badly overstate concentration, so they are kept.
    """
    top = sorted(holders.items(), key=lambda kv: -kv[1])[:top_n]
    contracts = set()
    for addr, _ in top:
        code = rpc("eth_getCode", [addr, "latest"], C.RPC_URL, tries=2)
        if not isinstance(code, str) or len(code) <= 2:
            continue                      # plain EOA
        if is_delegated_eoa(code):
            if verbose:
                print(f"        smart-account wallet (EIP-7702): {addr}")
            continue                      # user wallet, keep as a holder
        contracts.add(addr)
        if verbose:
            print(f"        contract holder: {addr} ({(len(code)-2)//2:,}B)")
    return contracts


def holder_metrics(replay, head, window_blocks, min_coverage=0.90,
                   contract_holders=()):
    """Concentration and growth statistics over the reconstructed holder set.

    Returns reliable=False when the replay did not recover enough of the
    supply to trust the numbers.
    """
    # Contracts among the holders are pools/routers/bridges, not participants;
    # leaving them in makes every token look whale-dominated.
    contracts = {a.lower() for a in contract_holders}
    all_holders = replay["holders"]
    contract_supply = sum(b for a, b in all_holders.items() if a in contracts)
    holders = {a: b for a, b in all_holders.items() if a not in contracts}
    first_seen = replay["first_seen"]
    coverage = replay.get("coverage")
    # Any unread block range means some transfers never landed, so balances
    # are wrong even if coverage happens to look acceptable.
    had_gaps = bool(replay.get("skipped"))
    reliable = (not had_gaps) and (coverage is not None
                                   and coverage >= min_coverage)

    if not holders:
        return {"holder_count": 0, "coverage": coverage, "reliable": False,
                "gaps": len(replay.get("skipped") or []),
                "contract_holders": len(contracts)}

    vals = sorted(holders.values(), reverse=True)
    total = sum(vals)
    if total <= 0:
        return {"holder_count": len(holders), "coverage": coverage,
                "reliable": False}

    def share(n):
        return sum(vals[:n]) / total

    # Gini over holder balances: 0 = perfectly even, 1 = one wallet owns it all.
    asc = sorted(vals)
    n = len(asc)
    cum = 0
    for i, v in enumerate(asc, 1):
        cum += i * v
    gini = (2 * cum) / (n * total) - (n + 1) / n if n > 1 else 1.0

    # Herfindahl index: sum of squared shares. >0.25 is a concentrated market.
    hhi = sum((v / total) ** 2 for v in vals)

    recent_cut = head - window_blocks
    new_holders = sum(1 for a in holders if first_seen.get(a, 0) >= recent_cut)

    return {
        "coverage": coverage,
        "reliable": reliable,
        "gaps": len(replay.get("skipped") or []),
        "contract_holders": len(contracts),
        "contract_supply_pct": (contract_supply / replay["reconstructed_supply"]
                                if replay.get("reconstructed_supply") else 0.0),
        "holder_count": n,
        "top1_share": share(1),
        "top10_share": share(10),
        "top50_share": share(50),
        "gini": gini,
        "hhi": hhi,
        "new_holders_window": new_holders,
        "new_holder_pct": new_holders / n,
    }
