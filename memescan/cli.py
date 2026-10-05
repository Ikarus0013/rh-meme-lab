"""Pipeline + terminal report."""
import argparse
import json
import sys
import time
from datetime import datetime, timezone

from . import chain
from . import config as C
from . import score as S
from . import sources as src

# ------------------------------------------------------------------ terminal
def _supports_color():
    return sys.stdout.isatty()


class Fmt:
    def __init__(self, on):
        self.on = on

    def _w(self, code, s):
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def bold(self, s):   return self._w("1", s)
    def dim(self, s):    return self._w("2", s)
    def green(self, s):  return self._w("32", s)
    def yellow(self, s): return self._w("33", s)
    def red(self, s):    return self._w("31", s)
    def cyan(self, s):   return self._w("36", s)

    def grade(self, v):
        if v >= 70:
            return self.green(f"{v:5.1f}")
        if v >= 50:
            return self.yellow(f"{v:5.1f}")
        return self.red(f"{v:5.1f}")


def money(v):
    if v is None:
        return "-"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v/div:.2f}{unit}"
    return f"${v:.0f}"


# ------------------------------------------------------------------ pipeline
def _base_symbol_from_pool_name(name):
    """GT pool names look like 'ASHIBA / NVDA'; take the base side."""
    return (name or "").split("/")[0].strip().upper()


def _age_hours(t, now):
    ms = t.get("created_ms")
    if ms:
        return max(0.0, (now - ms / 1000.0) / 3600.0)
    iso = t.get("created_at")
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return max(0.0, (now - dt.timestamp()) / 3600.0)
        except ValueError:
            pass
    return None


def run(cfg, args):
    fmt = Fmt(_supports_color() and not args.no_color)
    now = time.time()

    print(fmt.bold("\n[1/6] Discovering pools on Robinhood Chain..."))
    pools = src.discover_pools(pages=args.pages, verbose=True)
    print(f"    {len(pools)} unique pools")

    tokens = src.pools_to_tokens(pools)
    print(f"    -> {len(tokens)} unique base tokens (deduped across pools)")

    # ---- stage 1 filter: cheap, on data we already have -------------------
    excluded = C.INFRA_SYMBOLS | C.EQUITY_SYMBOLS | {e.upper() for e in args.exclude}
    stage1, drops = {}, {"infra/equity": 0, "mcap": 0, "liquidity": 0, "volume": 0}

    for addr, t in tokens.items():
        sym = _base_symbol_from_pool_name(t.get("name"))
        if sym in excluded:
            drops["infra/equity"] += 1
            continue
        mcap = t.get("mcap_gt") or t.get("fdv") or 0
        if not (cfg["mcap_min"] <= mcap <= cfg["mcap_max"]):
            drops["mcap"] += 1
            continue
        if t["liquidity"] < cfg["min_liquidity"]:
            drops["liquidity"] += 1
            continue
        if t["vol24"] < cfg["min_vol24"]:
            drops["volume"] += 1
            continue
        stage1[addr] = t

    print(fmt.bold(f"\n[2/6] Filtering to ${cfg['mcap_min']/1e6:.0f}M-"
                   f"${cfg['mcap_max']/1e6:.0f}M mcap band..."))
    for k, v in drops.items():
        print(f"    dropped {v:4d}  ({k})")
    print(f"    {fmt.cyan(str(len(stage1)))} candidates survive")

    if not stage1:
        print(fmt.yellow("\n  No tokens in that band right now. Widen it with "
                         "--mcap-min / --mcap-max, or loosen --min-liquidity."))
        return []

    # ---- stage 2: DexScreener enrichment (batched, cheap) -----------------
    print(fmt.bold("\n[3/6] Enriching via DexScreener (mcap, socials, txns)..."))
    ds = src.ds_token_batch(list(stage1.keys()))
    for addr, t in stage1.items():
        src.merge_dexscreener(t, ds.get(addr, []))
        t["age_hours"] = _age_hours(t, now)

    # Re-apply the mcap band on DexScreener's (better) market cap, and drop
    # anything whose symbol only now reveals itself as an equity/infra token.
    stage2 = {}
    for addr, t in stage1.items():
        sym = (t.get("symbol") or "").upper()
        if sym in excluded:
            continue
        mcap = t.get("mcap") or t.get("mcap_gt") or 0
        if cfg["mcap_min"] <= mcap <= cfg["mcap_max"]:
            stage2[addr] = t
    print(f"    {fmt.cyan(str(len(stage2)))} confirmed in band after DexScreener mcap")

    if not stage2:
        print(fmt.yellow("\n  Nothing survived the DexScreener mcap re-check."))
        return []

    # ---- stage 3: per-token GT info (rate limited, so cap the set) --------
    ranked_pre = sorted(stage2.values(),
                        key=lambda t: -(t.get("liquidity") or 0))
    focus = ranked_pre[:args.limit]
    print(fmt.bold(f"\n[4/6] Fetching token quality signals for top "
                   f"{len(focus)} by liquidity (~{len(focus)*C.GT_MIN_INTERVAL:.0f}s, "
                   f"GeckoTerminal free tier is 30 req/min)..."))
    for i, t in enumerate(focus, 1):
        t.update(src.gt_token_info(t["address"]))
        print(f"    [{i}/{len(focus)}] {t.get('symbol') or t['address'][:10]:12s} "
              f"gt_score={t.get('gt_score',0):.0f}")

    # ---- stage 4: contract checks ----------------------------------------
    if not args.no_chain:
        print(fmt.bold("\n[5/6] Contract checks over RPC..."))
        for t in focus:
            t["contract"] = chain.contract_facts(t["address"])
        print(f"    checked {len(focus)} contracts")
    else:
        print(fmt.dim("\n[5/6] Contract checks skipped (--no-chain)"))

    # ---- stage 5: deep holder reconstruction ------------------------------
    if args.deep:
        head = chain.head_block()
        head_ts = chain.head_timestamp(head) or now
        # Preliminary scoring pass so the expensive scan targets the tokens
        # that are actually contending, not merely the deepest ones.
        for t in focus:
            S.score_token(t, cfg)
        contenders = [t for t in focus if not t["disqualified"]] or focus
        deep_set = sorted(contenders, key=lambda t: -t["score"])[:args.deep]
        print(fmt.bold(f"\n[6/6] Deep holder scan for top {len(deep_set)} "
                       f"(replaying Transfer logs; this is the slow part)..."))
        for t in deep_set:
            sym = t.get("symbol") or t["address"][:10]

            # Anchor to the actual mint, not to pool creation -- a token is
            # often deployed days before its first pool, and starting late
            # silently corrupts every balance.
            mint_block, minted = chain.find_mint_block(t["address"], head)
            if mint_block:
                from_block = max(0, mint_block - 1000)
                origin = f"mint @ {mint_block:,}"
            else:
                age_h = t.get("age_hours") or 24 * 30
                from_block = chain.block_at_timestamp(
                    head_ts - age_h * 3600 - 3600, head, head_ts)
                origin = f"pool age fallback (~{age_h/24:.1f}d)"

            nblocks = head - from_block
            print(f"    {sym:12s} {origin}, scanning {nblocks:,} blocks")

            logs, skipped = chain.fetch_transfer_logs(
                t["address"], from_block, head, verbose=args.verbose)
            supply = (t.get("contract") or {}).get("total_supply")
            exclude = list(t.get("pool_addresses") or []) + [t["address"]]
            replay = chain.replay_balances(logs, exclude=exclude,
                                           total_supply=supply, skipped=skipped)
            # v4 singletons hold LP supply in one shared contract that no
            # per-pool exclusion catches, so ask the chain which top holders
            # have bytecode.
            contracts = chain.detect_contract_holders(replay["holders"],
                                                      verbose=args.verbose)
            window = int(24 * 3600 / C.BLOCK_TIME_SEC)
            hm = chain.holder_metrics(replay, head, window,
                                      contract_holders=contracts)
            t["holder_metrics"] = hm

            cov = hm.get("coverage")
            cov_s = f"{cov*100:.1f}%" if cov is not None else "n/a"
            line = (f"      -> {replay['n_transfers']:,} transfers, "
                    f"{hm.get('holder_count',0):,} wallet holders, "
                    f"top10={hm.get('top10_share',0)*100:.1f}%, coverage={cov_s}")
            if hm.get("contract_holders"):
                line += (f"\n         excluded {hm['contract_holders']} contract "
                         f"holders (pools/routers) = "
                         f"{hm.get('contract_supply_pct',0)*100:.1f}% of supply")
            if hm.get("reliable"):
                print(line)
            else:
                why = (f"{hm.get('gaps',0)} unread block ranges"
                       if hm.get("gaps") else f"coverage {cov_s} < 90%")
                print(fmt.yellow(f"{line}\n         [UNRELIABLE: {why}"
                                 f" - holder stats excluded from scoring]"))

    else:
        print(fmt.dim("\n[6/6] Deep holder scan skipped (pass --deep N to enable)"))

    # ---- score (final pass; re-scores with holder metrics if --deep ran) ---
    for t in focus:
        S.score_token(t, cfg)

    focus.sort(key=lambda t: (t["disqualified"], -t["score"]))
    return focus


# -------------------------------------------------------------------- report
def report(rows, cfg, args):
    fmt = Fmt(_supports_color() and not args.no_color)
    if not rows:
        return

    print(fmt.bold("\n" + "=" * 108))
    print(fmt.bold(f"  RANKED CANDIDATES  -  position size ${cfg['position_usd']:,} "
                   f"@ max {cfg['max_slippage']*100:.1f}% slippage"))
    print(fmt.bold("=" * 108))
    hdr = (f"{'#':>3} {'TICKER':<12} {'MCAP':>9} {'LIQ':>9} {'VOL24':>9} "
           f"{'SCORE':>6} {'EXIT':>6} {'HOLD':>6} {'MOMO':>6} {'MEME':>6} "
           f"{'SAFE':>6} {'MAX POS':>9}")
    print(fmt.dim(hdr))
    print(fmt.dim("-" * 108))

    for i, t in enumerate(rows, 1):
        p = t["pillars"]
        mark = fmt.red(" X") if t["disqualified"] else "  "
        if t.get("likely_utility"):
            mark += fmt.yellow("U")
        sym = (t.get("symbol") or t["address"][:10])[:12]
        print(f"{i:>3} {sym:<12} "
              f"{money(t.get('mcap') or t.get('mcap_gt')):>9} "
              f"{money(t.get('liquidity_ds') or t.get('liquidity')):>9} "
              f"{money(t.get('vol24')):>9} "
              f"{fmt.grade(t['score'])} {fmt.grade(p['exit'])} "
              f"{fmt.grade(p['holders'])} {fmt.grade(p['momentum'])} "
              f"{fmt.grade(p['meme'])} {fmt.grade(p['safety'])} "
              f"{money(t['max_position']):>9}{mark}")

    disq = [t for t in rows if t["disqualified"]]
    if disq:
        print(fmt.dim(f"\n  X = disqualified by a hard gate ({len(disq)} of "
                      f"{len(rows)}); shown for context, not as candidates."))
    util = [t for t in rows if t.get("likely_utility")]
    if util:
        names = ", ".join((t.get("symbol") or "?") for t in util)
        print(fmt.yellow(
            f"  U = description reads as a utility/revenue product, not a meme "
            f"({names}).\n      Structurally fine, but not what this screen is "
            f"for -- exclude with --exclude {names.replace(', ', ' ')}"))

    # ---- detail on the top few -------------------------------------------
    live = [t for t in rows if not t["disqualified"]][:args.detail]
    for t in live:
        print(fmt.bold(f"\n{'-'*108}"))
        sc = fmt.cyan(f"score {t['score']:.1f}")
        print(fmt.bold(f"  {t.get('symbol','?')} - {t.get('token_name','?')}  ") + sc)
        print(fmt.dim(f"  {t['address']}"))
        print(f"  mcap {money(t.get('mcap'))}   liq {money(t.get('liquidity_ds') or t.get('liquidity'))}"
              f"   age {(t.get('age_hours') or 0)/24:.1f}d"
              f"   pools {t.get('pool_count')}"
              f"   max position {fmt.cyan(money(t['max_position']))}")
        for pillar, factors in t["breakdown"].items():
            head = f"  {pillar.upper():<9} {fmt.grade(t['pillars'][pillar])}"
            bits = []
            for fname, (sc, w, note) in factors.items():
                if sc is None:
                    bits.append(fmt.dim(f"{fname}=n/a"))
                else:
                    bits.append(f"{fname}={sc:.0f}({note})")
            print(f"{head}  {fmt.dim(' | '.join(bits))}")
        if t["gate_failures"]:
            for g in t["gate_failures"]:
                print(fmt.red(f"  GATE: {g}"))

    print(fmt.bold("\n" + "=" * 108))
    print(fmt.yellow(
        "  Reality check: on Robinhood Chain's FOMO app, 95.2% of 375,740 meme\n"
        "  traders lost money or made under $100; the median trader lost ~$120.\n"
        "  This tool ranks relative structure. It cannot make a negative-sum\n"
        "  game positive-sum. Size positions as money you can lose entirely."))
    print(fmt.bold("=" * 108 + "\n"))


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="memescan",
        description="Screen Robinhood Chain meme coins for enterable positions.")
    p.add_argument("--mcap-min", type=float, default=C.DEFAULTS["mcap_min"])
    p.add_argument("--mcap-max", type=float, default=C.DEFAULTS["mcap_max"])
    p.add_argument("--min-liquidity", type=float, default=C.DEFAULTS["min_liquidity"])
    p.add_argument("--min-vol24", type=float, default=C.DEFAULTS["min_vol24"])
    p.add_argument("--min-age-hours", type=float, default=C.DEFAULTS["min_age_hours"])
    p.add_argument("--position", type=float, default=C.DEFAULTS["position_usd"],
                   help="USD you intend to deploy; drives slippage scoring")
    p.add_argument("--max-slippage", type=float, default=C.DEFAULTS["max_slippage"])
    p.add_argument("--pages", type=int, default=5,
                   help="GeckoTerminal pool pages to scan (20 pools each)")
    p.add_argument("--limit", type=int, default=15,
                   help="how many candidates to deep-enrich")
    p.add_argument("--detail", type=int, default=5,
                   help="how many to print full factor breakdowns for")
    p.add_argument("--deep", type=int, metavar="N", default=0,
                   help="reconstruct holder sets for the top N (slow, minutes each)")
    p.add_argument("--exclude", nargs="*", default=[],
                   help="extra tickers to exclude")
    p.add_argument("--no-chain", action="store_true", help="skip RPC contract checks")
    p.add_argument("--rpc-url", default=None,
                   help="override the RPC endpoint (also MEMESCAN_RPC_URL). "
                        "A dedicated endpoint makes --deep scans far faster; "
                        "the public one rate-limits hard.")
    p.add_argument("--json", metavar="PATH", help="write full results as JSON")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.rpc_url:
        C.RPC_URL = args.rpc_url
    if args.deep and C.RPC_URL == C.PUBLIC_RPC:
        print(Fmt(not args.no_color).yellow(
            "  note: --deep on the public RPC is slow and may return "
            "UNRELIABLE for busy\n        tokens. Set MEMESCAN_RPC_URL to a "
            "dedicated endpoint for usable deep scans."))

    cfg = {
        "mcap_min": args.mcap_min, "mcap_max": args.mcap_max,
        "min_liquidity": args.min_liquidity, "min_vol24": args.min_vol24,
        "min_age_hours": args.min_age_hours, "position_usd": args.position,
        "max_slippage": args.max_slippage,
    }

    rows = run(cfg, args)
    report(rows, cfg, args)

    if args.json and rows:
        payload = []
        for t in rows:
            d = {k: v for k, v in t.items() if k != "breakdown"}
            d["breakdown"] = {p: {f: {"score": s, "weight": w, "note": n}
                                  for f, (s, w, n) in fs.items()}
                              for p, fs in t["breakdown"].items()}
            payload.append(d)
        with open(args.json, "w") as fh:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat(),
                       "config": cfg, "results": payload}, fh, indent=2, default=str)
        print(f"  wrote {args.json}")
    return 0
