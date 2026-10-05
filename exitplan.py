"""Exit planner: size a position by what you can get OUT, not what you want in.

The arithmetic nobody runs before entering. On a constant-product pool the
impact of selling X against liquidity L is X/(L/2 + X), so the damage is
superlinear in position size -- a position worth 5% of a pool does not cost
5% to exit, it costs ~9%, and one worth 50% costs ~50%.

Measured live: STONK carries a $271M market cap on $3.2M of liquidity. A
$12.4M holding in it -- 4.6% of the whole market cap -- would move the price
88% on the way out. That holder's balance is a mark, not money.

So the planner answers three questions in the order they actually matter:
  1. how much can I put in and still leave?    (max position for a slip budget)
  2. what does getting out look like?          (tranche ladder)
  3. when do I take money off the table?       (profit ladder, cost basis first)
"""
import json
import urllib.request

DS = "https://api.dexscreener.com"


def est_slippage(position, liquidity):
    """Constant-product price impact of trading `position` against `liquidity`."""
    if liquidity <= 0:
        return 1.0
    return position / (liquidity / 2.0 + position)


def max_position(liquidity, max_slip):
    """Largest single trade that stays inside a price-impact budget."""
    if liquidity <= 0 or not (0 < max_slip < 1):
        return 0.0
    return max_slip * (liquidity / 2.0) / (1.0 - max_slip)


def tranche_plan(position_usd, liquidity, max_slip=0.02, cap=40):
    """Ladder a full exit into trades that each stay under the impact budget.

    Liquidity is held constant, which flatters the result: every sale also
    removes value from the pool, so a real exit is worse than this. Treat the
    tranche count as a floor.
    """
    out, remaining, n = [], float(position_usd), 0
    step = max_position(liquidity, max_slip)
    if step <= 0:
        return out, remaining
    while remaining > 1 and n < cap:
        t = min(step, remaining)
        slip = est_slippage(t, liquidity)
        out.append({"n": n + 1, "sell": t, "slip": slip, "net": t * (1 - slip)})
        remaining -= t
        n += 1
    return out, max(0.0, remaining)


def profit_ladder(entry_usd, rungs=((3, 0.25), (10, 0.25), (30, 0.25))):
    """Pre-committed sells by multiple. Flags where cost basis is recovered."""
    rows, sold, recovered = [], 0.0, False
    for mult, frac in rungs:
        value_then = entry_usd * mult
        take = value_then * frac * (1 - sold)
        sold += frac * (1 - sold)
        cum = sum(r["proceeds"] for r in rows) + take
        row = {"mult": mult, "frac": frac, "proceeds": take, "cum": cum,
               "recovered": cum >= entry_usd and not recovered}
        if row["recovered"]:
            recovered = True
        rows.append(row)
    return rows


def lookup(address):
    """Live market cap + summed liquidity for a token.

    One search call resolves the chain instead of probing five endpoints in
    series -- that serial probe took up to 100s on a miss and, on a
    single-threaded server, froze the whole UI while it ran.
    """
    addr = (address or "").strip()
    if not addr:
        return {"ok": False, "error": "no address given"}
    chains = ["robinhood", "solana", "base", "bsc", "ethereum"]
    # cheap hint: 0x-prefixed is EVM, otherwise Solana-style base58
    if not addr.startswith("0x"):
        chains = ["solana"]
    try:
        req = urllib.request.Request(f"{DS}/latest/dex/search?q={addr}",
                                     headers={"User-Agent": "exitplan/1.0"})
        pairs = json.load(urllib.request.urlopen(req, timeout=8)).get("pairs") or []
        hit = [p for p in pairs
               if ((p.get("baseToken") or {}).get("address") or "").lower() == addr.lower()]
        if hit:
            chains = [hit[0].get("chainId")] + [c for c in chains if c != hit[0].get("chainId")]
    except Exception:
        pass

    for chain in chains[:3]:
        try:
            req = urllib.request.Request(f"{DS}/tokens/v1/{chain}/{addr}",
                                         headers={"User-Agent": "exitplan/1.0"})
            rows = json.load(urllib.request.urlopen(req, timeout=8))
        except Exception:
            continue
        if not rows:
            continue
        base = rows[0].get("baseToken") or {}
        liq = sum((p.get("liquidity") or {}).get("usd", 0) or 0 for p in rows)
        mc = max((p.get("marketCap") or p.get("fdv") or 0) for p in rows)
        pools = sorted(
            ({"dex": p.get("dexId"), "liq": (p.get("liquidity") or {}).get("usd", 0) or 0}
             for p in rows), key=lambda x: -x["liq"])
        return {"ok": True, "chain": chain, "symbol": base.get("symbol"),
                "name": base.get("name"), "mcap": mc, "liquidity": liq,
                "deepest": pools[0]["liq"] if pools else 0,
                "pools": pools[:6], "price": float(rows[0].get("priceUsd") or 0)}
    return {"ok": False, "error": "token not found on any indexed chain"}


def plan(address=None, liquidity=None, mcap=None, position=None, max_slip=0.02):
    """Everything the page needs, computed server-side."""
    info = None
    if address:
        info = lookup(address)
        if info.get("ok"):
            liquidity = liquidity or info["liquidity"]
            mcap = mcap or info["mcap"]
    liquidity = float(liquidity or 0)
    position = float(position or 0)
    safe = max_position(liquidity, max_slip)
    tranches, stuck = tranche_plan(position, liquidity, max_slip) if position else ([], 0)
    one_shot = est_slippage(position, liquidity) if position else 0
    return {
        "info": info, "liquidity": liquidity, "mcap": mcap, "position": position,
        "max_slip": max_slip, "safe_position": safe,
        "pool_share": (position / liquidity * 100) if liquidity else None,
        "mcap_share": (position / mcap * 100) if mcap else None,
        "one_shot_slip": one_shot,
        "tranches": tranches, "stuck": stuck,
        "tranches_needed": (position / safe) if safe > 0 else None,
        "curve": [{"pos": p, "slip": est_slippage(p, liquidity)}
                  for p in (250, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000)] if liquidity else [],
        "ladder": profit_ladder(position) if position else [],
    }
