"""Market-data sources: GeckoTerminal (discovery) + DexScreener (enrichment).

Discovery is pool-level; everything downstream is token-level. The bridge
between the two is `pools_to_tokens`, which aggregates a token's many pools
into one row -- important on this chain, where a single token routinely has
five or more pools and naive pool-level ranking double-counts it.
"""
import time

from . import config as C
from .net import get_json


# ------------------------------------------------------------------ discovery
def _gt(path):
    return get_json(f"{C.GT_BASE}{path}", "gt", C.GT_MIN_INTERVAL)


def _pool_rows(payload):
    return (payload or {}).get("data", []) or []


def discover_pools(pages=5, verbose=True):
    """Pull the pool universe from several GT endpoints and de-duplicate.

    Top-by-volume finds established names; new_pools and trending catch things
    before they show up in volume rankings.
    """
    seen = {}

    def absorb(rows, label):
        added = 0
        for p in rows:
            addr = p.get("attributes", {}).get("address")
            if addr and addr not in seen:
                seen[addr] = p
                added += 1
        if verbose:
            print(f"    {label}: +{added} new ({len(seen)} total)")

    for page in range(1, pages + 1):
        rows = _pool_rows(_gt(f"/networks/{C.GT_NETWORK}/pools"
                              f"?page={page}&sort=h24_volume_usd_desc"))
        if not rows:
            break
        absorb(rows, f"top-volume p{page}")

    absorb(_pool_rows(_gt(f"/networks/{C.GT_NETWORK}/trending_pools")), "trending")
    absorb(_pool_rows(_gt(f"/networks/{C.GT_NETWORK}/new_pools")), "new")
    return list(seen.values())


def _addr_from_gt_id(gt_id):
    """'robinhood_0xabc...' -> '0xabc...'"""
    if not gt_id or "_" not in gt_id:
        return None
    return gt_id.split("_", 1)[1].lower()


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pools_to_tokens(pools):
    """Aggregate pool rows into one record per base token.

    Liquidity and volume sum across pools; FDV/mcap and price are taken from
    the deepest pool (the most reliable quote); pool_count and the age of the
    oldest pool are kept as structural signals.
    """
    tokens = {}
    for p in pools:
        a = p.get("attributes", {}) or {}
        rel = p.get("relationships", {}) or {}
        base = _addr_from_gt_id((rel.get("base_token", {}).get("data") or {}).get("id"))
        if not base:
            continue

        liq = _f(a.get("reserve_in_usd"))
        vol24 = _f((a.get("volume_usd") or {}).get("h24"))
        txns = a.get("transactions") or {}

        t = tokens.setdefault(base, {
            "address": base, "liquidity": 0.0, "vol24": 0.0, "vol6": 0.0,
            "pool_count": 0, "deepest_liq": -1.0, "fdv": 0.0, "price": 0.0,
            "name": "", "created_at": None, "pct": {}, "buyers24": 0,
            "sellers24": 0, "buys24": 0, "sells24": 0, "buyers6": 0,
            "top_pool": None, "dex_set": set(), "pool_addresses": set(),
        })
        if a.get("address"):
            t["pool_addresses"].add(a["address"].lower())

        t["liquidity"] += liq
        t["vol24"] += vol24
        t["vol6"] += _f((a.get("volume_usd") or {}).get("h6"))
        t["pool_count"] += 1

        dex = ((rel.get("dex", {}) or {}).get("data") or {}).get("id")
        if dex:
            t["dex_set"].add(dex)

        h24 = txns.get("h24") or {}
        h6 = txns.get("h6") or {}
        t["buyers24"] += int(h24.get("buyers") or 0)
        t["sellers24"] += int(h24.get("sellers") or 0)
        t["buys24"] += int(h24.get("buys") or 0)
        t["sells24"] += int(h24.get("sells") or 0)
        t["buyers6"] += int(h6.get("buyers") or 0)

        # Deepest pool wins for price/valuation/percent-change quotes.
        if liq > t["deepest_liq"]:
            t["deepest_liq"] = liq
            t["fdv"] = _f(a.get("fdv_usd"))
            mc = _f(a.get("market_cap_usd"))
            t["mcap_gt"] = mc if mc > 0 else t["fdv"]
            t["price"] = _f(a.get("base_token_price_usd"))
            t["name"] = a.get("name") or ""
            t["pct"] = {k: _f(v) for k, v in (a.get("price_change_percentage") or {}).items()}
            t["top_pool"] = a.get("address")

        created = a.get("pool_created_at")
        if created and (t["created_at"] is None or created < t["created_at"]):
            t["created_at"] = created

    for t in tokens.values():
        t["dex_count"] = len(t["dex_set"])
        del t["dex_set"]
        t["pool_addresses"] = sorted(t["pool_addresses"])
    return tokens


# ----------------------------------------------------------------- enrichment
def ds_token_batch(addresses):
    """DexScreener pairs for up to 30 tokens per request.

    Returns {token_address_lower: [pair, ...]}.
    """
    out = {}
    for i in range(0, len(addresses), 30):
        chunk = addresses[i:i + 30]
        url = f"{C.DS_BASE}/tokens/v1/{C.DS_CHAIN}/{','.join(chunk)}"
        rows = get_json(url, "ds", C.DS_MIN_INTERVAL)
        if not isinstance(rows, list):
            continue
        for pair in rows:
            base = ((pair.get("baseToken") or {}).get("address") or "").lower()
            if base:
                out.setdefault(base, []).append(pair)
    return out


def merge_dexscreener(token, pairs):
    """Fold DexScreener pair data into a token record.

    DexScreener is the better source for market cap, socials and fine-grained
    price buckets; GT is the better source for unique buyer/seller counts.
    """
    if not pairs:
        return token

    deepest = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")))
    token["symbol"] = (deepest.get("baseToken") or {}).get("symbol") or ""
    token["token_name"] = (deepest.get("baseToken") or {}).get("name") or ""

    mc = _f(deepest.get("marketCap")) or _f(deepest.get("fdv"))
    if mc > 0:
        token["mcap"] = mc

    ds_liq = sum(_f((p.get("liquidity") or {}).get("usd")) for p in pairs)
    if ds_liq > 0:
        token["liquidity_ds"] = ds_liq

    # Socials / branding completeness -> meme pillar.
    info = deepest.get("info") or {}
    socials = {s.get("type", "").lower() for s in (info.get("socials") or [])}
    token["has_twitter"] = "twitter" in socials
    token["has_telegram"] = "telegram" in socials
    token["has_discord"] = "discord" in socials
    token["has_website"] = bool(info.get("websites"))
    token["has_image"] = bool(info.get("imageUrl"))
    token["has_header"] = bool(info.get("header"))
    token["boosts"] = int((deepest.get("boosts") or {}).get("active") or 0)

    # DexScreener knows about pools GeckoTerminal's ranked pages missed; every
    # one of them holds LP supply that must be excluded from holder stats.
    known = set(token.get("pool_addresses") or [])
    for pr in pairs:
        pa = (pr.get("pairAddress") or "").lower()
        if pa:
            known.add(pa)
    token["pool_addresses"] = sorted(known)

    created_ms = deepest.get("pairCreatedAt")
    if created_ms:
        token["created_ms"] = created_ms

    pc = deepest.get("priceChange") or {}
    for k in ("m5", "h1", "h6", "h24"):
        if k in pc:
            token["pct"][k] = _f(pc[k])

    tx = deepest.get("txns") or {}
    token["ds_buys24"] = int((tx.get("h24") or {}).get("buys") or 0)
    token["ds_sells24"] = int((tx.get("h24") or {}).get("sells") or 0)
    return token


def gt_token_info(address):
    """GeckoTerminal token metadata, including its gt_score breakdown.

    gt_score_details carries a `holders` sub-score, which is the only free
    holder-quality signal available without an indexer.
    """
    d = _gt(f"/networks/{C.GT_NETWORK}/tokens/{address}/info")
    a = ((d or {}).get("data") or {}).get("attributes") or {}
    details = a.get("gt_score_details") or {}
    return {
        "gt_score": _f(a.get("gt_score")),
        "gt_holders_score": _f(details.get("holders")),
        "gt_pool_score": _f(details.get("pool")),
        "gt_tx_score": _f(details.get("transaction")),
        "gt_info_score": _f(details.get("info")),
        "gt_creation_score": _f(details.get("creation")),
        "gt_verified": bool(a.get("gt_verified")),
        "description": a.get("description") or "",
        "twitter": a.get("twitter_handle") or "",
        "telegram": a.get("telegram_handle") or "",
        "categories": a.get("categories") or [],
    }
