"""The backtest: does an echo clone outperform a random Robinhood Chain launch?

Three rules keep this honest, and they are the reason the study exists at all:

1. Entry is always DELAYED. You cannot buy at the launch price -- you buy some
   minutes after the signal is detectable. Every return here is measured from
   `detected_at + delay`, using the first observation at or after that instant.

2. Exit follows a mechanical rule evaluated forward through the series. No
   "sold at the top" -- the peak is reported separately, and labelled as
   unrealizable, precisely so it never gets mistaken for a result.

3. Every number is reported against the CONTROL cohort. An echo edge is the
   spread between cohorts, not the echo cohort's absolute return. If random
   launches did the same thing, there is no signal.
"""
import math
import statistics

from . import db

DELAYS_MIN = (5, 15, 60)
TAKE_PROFIT = 1.00      # +100%
STOP_LOSS = -0.50       # -50%
TIMEOUT_H = 24

# A parent that launched within this many hours is a live narrative; older
# than this and an RH clone is squatting an established brand, which the
# data already shows is a different phenomenon.
FRESH_PARENT_H = 72


def series(con, address):
    return [dict(r) for r in con.execute(
        "SELECT ts, price, fdv, liq FROM obs WHERE address=? AND price>0 ORDER BY ts",
        (address,))]


def simulate(obs, entry_ts, tp=TAKE_PROFIT, sl=STOP_LOSS, timeout_h=TIMEOUT_H):
    """Walk the series forward from `entry_ts`; exit on the first rule hit.

    Returns None when there is no observation at or after entry -- an
    untradeable signal, which is itself a result and must not be silently
    dropped as a zero.
    """
    entry = next((o for o in obs if o["ts"] >= entry_ts), None)
    if not entry or not entry["price"]:
        return None
    p0 = entry["price"]
    deadline = entry["ts"] + timeout_h * 3600
    peak = 0.0
    for o in obs:
        if o["ts"] < entry["ts"]:
            continue
        r = o["price"] / p0 - 1.0
        peak = max(peak, r)
        if r >= tp:
            return {"ret": tp, "reason": "tp", "peak": peak, "held_h": (o["ts"] - entry["ts"]) / 3600}
        if r <= sl:
            return {"ret": sl, "reason": "sl", "peak": peak, "held_h": (o["ts"] - entry["ts"]) / 3600}
        if o["ts"] >= deadline:
            return {"ret": r, "reason": "timeout", "peak": peak, "held_h": (o["ts"] - entry["ts"]) / 3600}
    last = obs[-1]
    return {"ret": last["price"] / p0 - 1.0, "reason": "open", "peak": peak,
            "held_h": (last["ts"] - entry["ts"]) / 3600}


def z_prop(k1, n1, k2, n2):
    """Two-proportion z-test -> (p1, p2, lift, p_value).

    Memecoin returns are not a distribution a median describes: ~72% of
    launches never trade again and sit at exactly 0.0%, so the median of every
    cohort is 0.0% and a median-difference test reports "no edge" no matter
    what the live tail is doing. What separates these cohorts is the RATE of
    rare large moves, which is a proportion -- so test it as one.
    """
    if not n1 or not n2:
        return None
    p1, p2 = k1 / n1, k2 / n2
    pool = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pool * (1 - pool) * (1 / n1 + 1 / n2))
    if se == 0:
        return p1, p2, None, 1.0
    z = (p1 - p2) / se
    pv = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return p1, p2, (p1 / p2 if p2 else None), pv


def _pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p * (len(xs) - 1)))))
    return xs[k]


def summarize(results):
    rets = [r["ret"] for r in results]
    peaks = [r["peak"] for r in results]
    closed = [r for r in results if r["reason"] != "open"]
    if not rets:
        return None
    return {
        "n": len(rets),
        "median": statistics.median(rets),
        "mean": statistics.fmean(rets),
        "win_rate": sum(1 for r in rets if r > 0) / len(rets),
        "p_2x": sum(1 for p in peaks if p >= 1.0) / len(peaks),
        "p_rug": sum(1 for r in rets if r <= STOP_LOSS) / len(rets),
        "p90_peak": _pct(peaks, 0.90),
        "median_peak": statistics.median(peaks),
        "closed": len(closed),
    }


def cohorts(con, delay_min, source=None):
    """Build {label: [sim results]} for one entry delay.

    Fresh/squat prefers Jev's `live_narrative` where it exists and falls back
    to the age threshold otherwise. The two feeds carry different evidence --
    GT rows have `parent_age_h`, on-chain rows have a Jev score and no age --
    so a single rule would silently dump every on-chain echo into `squat`.

    `source` filters to one feed ('gt' | 'onchain') so the feeds can be
    compared rather than blended.
    """
    out = {"echo/fresh": [], "echo/squat": [], "control": []}
    q = ("SELECT l.address, l.cohort, l.first_seen,"
         "       (SELECT MIN(parent_age_h) FROM echo_link e WHERE e.rh_address=l.address) AS page,"
         "       (SELECT j.live_narrative FROM jev_score j WHERE j.address=l.address) AS live"
         "  FROM rh_launch l")
    args = ()
    if source:
        q += " WHERE l.source=?"
        args = (source,)
    for r in con.execute(q, args):
        obs = series(con, r["address"])
        if len(obs) < 2:
            continue
        sim = simulate(obs, r["first_seen"] + delay_min * 60)
        if not sim:
            continue
        if r["cohort"] == "control":
            out["control"].append(sim)
        elif r["cohort"] == "echo":
            if r["live"] is not None:
                fresh = r["live"] >= 0.50
            else:
                fresh = r["page"] is not None and r["page"] <= FRESH_PARENT_H
            out["echo/fresh" if fresh else "echo/squat"].append(sim)
    return out


def report(con=None):
    con = con or db.connect()
    n_launch = con.execute("SELECT COUNT(*) FROM rh_launch").fetchone()[0]
    n_obs = con.execute("SELECT COUNT(*) FROM obs").fetchone()[0]
    span = con.execute("SELECT MIN(first_seen), MAX(first_seen) FROM rh_launch").fetchone()
    hours = ((span[1] or 0) - (span[0] or 0)) / 3600 if span[0] else 0

    print(f"dataset: {n_launch} launches, {n_obs} observations, {hours:.1f}h of collection")
    errs = con.execute(
        "SELECT COUNT(*) FROM cycle_log WHERE kind='error'").fetchone()[0]
    empty = con.execute(
        "SELECT COUNT(*) FROM cycle_log WHERE kind='empty'").fetchone()[0]
    oks = con.execute("SELECT COUNT(*) FROM cycle_log WHERE kind='ok'").fetchone()[0]
    print(f"cycles: {oks} ok, {errs} errored, {empty} empty"
          + ("   <-- gaps in the denominator" if (errs or empty) else ""))

    if hours < 6:
        print("\nToo early for a verdict. Entry delays need hours of series per token;\n"
              "treat anything below ~48h of collection as a smoke test, not evidence.")

    for d in DELAYS_MIN:
        c = cohorts(con, d)
        print(f"\n--- entry at detection +{d}min "
              f"(exit: +{TAKE_PROFIT:.0%} / {STOP_LOSS:.0%} / {TIMEOUT_H}h) ---")
        print(f"  {'cohort':<12} {'n':>4} {'median':>8} {'mean':>8} {'win%':>6} "
              f"{'P(2x)':>7} {'P(rug)':>7} {'medPeak':>8}")
        for label in ("echo/fresh", "echo/squat", "control"):
            s = summarize(c[label])
            if not s:
                print(f"  {label:<12} {'--':>4}   (no data yet)")
                continue
            print(f"  {label:<12} {s['n']:>4} {s['median']:>7.1%} {s['mean']:>8.1%} "
                  f"{s['win_rate']:>5.0%} {s['p_2x']:>7.0%} {s['p_rug']:>7.0%} "
                  f"{s['median_peak']:>7.1%}")
        f, ctl = c["echo/fresh"], c["control"]
        if len(f) >= 20 and len(ctl) >= 20:
            k1 = sum(1 for r in f if r["peak"] >= 1.0)
            k2 = sum(1 for r in ctl if r["peak"] >= 1.0)
            res = z_prop(k1, len(f), k2, len(ctl))
            p1, p2, lift, pv = res
            # 9 tests across 3 delays x 3 metrics, so the honest bar is
            # Bonferroni (0.05/9), not a bare 0.05.
            mark = ("SIGNIFICANT" if pv < 0.05 / 9 else
                    "suggestive" if pv < 0.05 else "not significant")
            print(f"  >>> P(2x) fresh {p1:.1%} ({k1}/{len(f)}) vs control {p2:.1%} "
                  f"({k2}/{len(ctl)})"
                  + (f"  lift {lift:.1f}x" if lift else "")
                  + f"  p={pv:.4f}  [{mark}]")
        else:
            print("  >>> edge: insufficient n (need 20+ per cohort)")
    return con


# --------------------------------------------------------- costed simulation
def simulate_costed(obs, entry_ts, position_usd, fee=0.01,
                    tp=TAKE_PROFIT, sl=STOP_LOSS, timeout_h=TIMEOUT_H):
    """Like simulate(), but charges what it actually costs to trade.

    Three costs the mid-price version ignores, all of which bite hardest on
    exactly the tokens this study is about -- they launch at ~$5k liquidity,
    where a $500 order is a sixth of the pool:

      entry slippage   position against liquidity AT ENTRY
      exit slippage    the position's VALUE against liquidity AT EXIT -- both
                       have moved, and on a winner they have moved a lot
      fee              charged on both legs

    Returns None when the position cannot be entered at all, which happens
    when slippage alone would exceed the take-profit -- an untradeable signal,
    and a result rather than a zero.
    """
    from memescan.score import est_slippage

    entry = next((o for o in obs if o["ts"] >= entry_ts), None)
    if not entry or not entry["price"] or not entry.get("liq"):
        return None
    slip_in = est_slippage(position_usd, entry["liq"])
    if slip_in >= 0.90:
        return None
    # tokens actually received, after paying impact and fee
    p_eff = entry["price"] * (1.0 + slip_in) / (1.0 - fee)
    tokens = position_usd / p_eff

    deadline = entry["ts"] + timeout_h * 3600
    peak = 0.0
    for o in obs:
        if o["ts"] < entry["ts"]:
            continue
        gross = tokens * o["price"]
        liq = o.get("liq") or 0.0
        slip_out = est_slippage(gross, liq) if liq > 0 else 1.0
        net = gross * (1.0 - slip_out) * (1.0 - fee)
        r = net / position_usd - 1.0
        peak = max(peak, r)
        if r >= tp:
            return {"ret": tp, "reason": "tp", "peak": peak, "slip_in": slip_in}
        if r <= sl:
            return {"ret": sl, "reason": "sl", "peak": peak, "slip_in": slip_in}
        if o["ts"] >= deadline:
            return {"ret": r, "reason": "timeout", "peak": peak, "slip_in": slip_in}
    last = obs[-1]
    gross = tokens * last["price"]
    liq = last.get("liq") or 0.0
    net = gross * (1.0 - (est_slippage(gross, liq) if liq > 0 else 1.0)) * (1.0 - fee)
    return {"ret": net / position_usd - 1.0, "reason": "open", "peak": peak, "slip_in": slip_in}
