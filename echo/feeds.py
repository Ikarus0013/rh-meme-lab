"""Data feeds: the hot-ticker watchlist (other chains) and the RH firehose.

The asymmetry that makes this tractable: Robinhood Chain launches ~20k pools
a day, so searching every one against every other chain is hopeless. But the
set of tickers actually *running* elsewhere at any moment is small -- a few
hundred. So we maintain that small set and match the firehose against it.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memescan import config as C
from memescan.net import get_json

# GeckoTerminal's free tier throttles harder than its documented 30/min when
# called in bursts -- a 2.2s spacing still drew 429s during probing. A
# long-running collector has no reason to hurry.
GT_INTERVAL = 4.0
DS_INTERVAL = 0.30

# Chains worth watching for parents. Solana dominates meme launches; Base and
# BSC are the other two that reliably seed cross-chain copies.
PARENT_NETWORKS = ("solana", "base", "bsc")

# Never treat these as echo matches -- they are quote assets, infrastructure
# or equities, so a ticker collision carries no narrative signal at all.
NOISE_SYMBOLS = set(C.INFRA_SYMBOLS) | set(C.EQUITY_SYMBOLS) | {
    "WBNB", "SOL", "WSOL", "BNB", "MATIC", "AVAX", "ARB", "OP", "PEPE",
}


def _gt(path):
    return get_json(f"{C.GT_BASE}{path}", "gt", GT_INTERVAL)


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def norm_symbol(s):
    """Normalise a ticker for matching.

    Pool names arrive as 'SHREK / WETH'; token symbols as '$SHREK' or 'shrek'.
    Matching has to be case- and decoration-insensitive or the echo link is
    missed entirely.
    """
    if not s:
        return ""
    s = s.split("/")[0].strip().lstrip("$").upper()
    return "".join(ch for ch in s if ch.isalnum())


def _rows(payload):
    return (payload or {}).get("data", []) or []


def _base_symbol(pool_row, included):
    """Resolve a pool's base-token symbol via the response's `included` block.

    GT returns the pool's display name ('SHREK / WETH') but the authoritative
    symbol lives in the included token objects, keyed by relationship id.
    """
    rel = (pool_row.get("relationships") or {}).get("base_token", {}).get("data") or {}
    tid = rel.get("id")
    tok = included.get(tid)
    if tok:
        sym = (tok.get("attributes") or {}).get("symbol")
        if sym:
            return norm_symbol(sym)
    return norm_symbol((pool_row.get("attributes") or {}).get("name"))


def _addr(gt_id):
    return gt_id.split("_", 1)[1].lower() if gt_id and "_" in gt_id else None


def fetch_hot_tickers():
    """Tickers currently running on parent chains.

    Two sources per chain, because they catch different phases: `trending`
    surfaces a name while it is still climbing, `top-volume` confirms the ones
    that have already drawn real flow. A copycat launcher reads the same
    boards, so this approximates their input set.
    """
    out = []
    for net in PARENT_NETWORKS:
        for path, label in (
            (f"/networks/{net}/trending_pools?include=base_token", "trending"),
            (f"/networks/{net}/pools?include=base_token&sort=h24_volume_usd_desc", "volume"),
        ):
            payload = _gt(path)
            if not payload:
                continue
            included = {i["id"]: i for i in (payload.get("included") or [])}
            for p in _rows(payload):
                a = p.get("attributes") or {}
                sym = _base_symbol(p, included)
                if not sym or sym in NOISE_SYMBOLS or len(sym) < 2:
                    continue
                rel = (p.get("relationships") or {}).get("base_token", {}).get("data") or {}
                out.append({
                    "symbol": sym,
                    "chain": net,
                    "address": _addr(rel.get("id")) or "",
                    "mcap": _f(a.get("market_cap_usd")) or _f(a.get("fdv_usd")),
                    "liq": _f(a.get("reserve_in_usd")),
                    "vol24": _f((a.get("volume_usd") or {}).get("h24")),
                    "pct24": _f((a.get("price_change_percentage") or {}).get("h24")),
                    "pool_created_at": a.get("pool_created_at"),
                    "source": label,
                })
    return out


def fetch_rh_launches(pages=5):
    """The Robinhood Chain new-pool firehose.

    GT caps `new_pools` at 10 pages of 20. At the chain's observed launch rate
    that is roughly seven minutes of history, so the collector must poll well
    inside that window or it silently loses launches -- and a lost launch is
    an unobservable denominator, which is the one error this study cannot
    tolerate.
    """
    seen = {}
    for page in range(1, pages + 1):
        payload = _gt(f"/networks/robinhood/new_pools?include=base_token&page={page}")
        rows = _rows(payload)
        if not rows:
            break
        included = {i["id"]: i for i in (payload.get("included") or [])}
        for p in rows:
            a = p.get("attributes") or {}
            rel = (p.get("relationships") or {}).get("base_token", {}).get("data") or {}
            addr = _addr(rel.get("id"))
            if not addr or addr in seen:
                continue
            dex = ((p.get("relationships") or {}).get("dex", {}).get("data") or {}).get("id")
            seen[addr] = {
                "address": addr,
                "symbol": _base_symbol(p, included),
                "name": a.get("name") or "",
                "pool": (a.get("address") or "").lower(),
                "dex": dex or "",
                "pool_created_at": a.get("pool_created_at"),
                "init_liq": _f(a.get("reserve_in_usd")),
                "init_fdv": _f(a.get("fdv_usd")),
            }
    return list(seen.values())


def fetch_prices(addresses):
    """DexScreener batch quote, 30 tokens per call.

    Returns {address: {price, fdv, liq, vol_h1, buys_h1, sells_h1}}, summing
    liquidity across a token's pools and quoting from its deepest one -- the
    same convention memescan uses, so numbers stay comparable across tools.
    """
    out = {}
    addrs = [a for a in addresses if a]
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        rows = get_json(
            f"{C.DS_BASE}/tokens/v1/{C.DS_CHAIN}/{','.join(chunk)}", "ds", DS_INTERVAL
        )
        if not isinstance(rows, list):
            continue
        by_token = {}
        for pair in rows:
            base = ((pair.get("baseToken") or {}).get("address") or "").lower()
            if base:
                by_token.setdefault(base, []).append(pair)
        for addr, pairs in by_token.items():
            deepest = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
            tx1 = (deepest.get("txns") or {}).get("h1") or {}
            # Socials come free with the price call -- no extra request.
            inf = deepest.get("info") or {}
            kinds = {x.get("type", "").lower() for x in (inf.get("socials") or [])}
            out[addr] = {
                "tw": "twitter" in kinds, "tg": "telegram" in kinds,
                "web": bool(inf.get("websites")), "img": bool(inf.get("imageUrl")),
                "price": _f(deepest.get("priceUsd")),
                "fdv": _f(deepest.get("marketCap")) or _f(deepest.get("fdv")),
                "liq": sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs),
                "vol_h1": _f((deepest.get("volume") or {}).get("h1")),
                "buys_h1": int(tx1.get("buys") or 0),
                "sells_h1": int(tx1.get("sells") or 0),
            }
    return out
