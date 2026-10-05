"""Classify on-chain launches into cohorts, using Jev for the fresh/squat call.

The GT-sourced pipeline splits echo candidates on `parent_age_h <= 72`, a hard
threshold that is wrong at the edges: a parent can be six hours old and going
nowhere, or three days old and still climbing. Calibration on 28 launches with
known outcomes showed Jev's `live_narrative` separating those cases far better
than age does (0.79 vs 0.27 mean, fresh vs squat) while `is_clone` separated
nothing at all -- everything reaching this code is a clone by construction, so
asking was meaningless.

Jev is used as a REJECT filter, never a picker. Its confidence on the
attention score ran 0.45-0.76 on tokens it scored low and 0.00-0.24 on the
ones that went on to double: it knows when to say no and not when to say yes.
Nothing here treats a high score as a buy signal.
"""
import random

from . import db, feeds
from jev import client as jev

LIVE_THRESHOLD = 0.50       # noul above this = parent is mid-run
# Steady-state control size is roughly CONTROLS_PER_PASS x cycles/hour x the
# 48h tracking window: 4/pass ~= 3,800 controls, about 12x the echo cohort.
# More than that sharpens the control estimate but cannot help the p-value,
# which is bound by the much smaller echo side.
CONTROLS_PER_PASS = 4
# The on-chain feed adds ~100 launches a cycle, so a batch below that grows a
# backlog forever. Non-matches cost one UPDATE each, so the batch can be large;
# what needs bounding is the Jev calls, which are the only network work here.
BATCH = 500
MAX_JEV_PER_PASS = 25

QUESTIONS = {
    "live_narrative": {
        "type": "noul",
        "instructions": "Is `parent_token` in the middle of a LIVE run right now, "
                        "so attention on this name is still building?",
        "criteria": {"true": "Parent is young and sharply up — attention is current and rising",
                     "false": "Parent is established or flat; there is no live wave to ride"},
    },
    "attention": {
        "type": "score",
        "instructions": "How likely is `new_token` to attract real speculative buying in the next hour?",
        "criteria": ["No chance — nobody is looking for this name", "Unlikely",
                     "Plausible", "Likely",
                     "Very likely — the name is hot and this is the obvious vehicle"],
    },
}


def _current_hot(con, max_age_sec=3600):
    cur = con.execute(
        "SELECT symbol, chain, address, mcap, liq, vol24, pct24, pool_created_at,"
        "       MAX(seen_at) AS seen_at FROM hot_ticker WHERE seen_at >= ?"
        " GROUP BY symbol, chain, address", (db.now() - max_age_sec,))
    out = {}
    for r in cur:
        out.setdefault(r["symbol"], []).append(dict(r))
    return out


def classify(con, limit=BATCH):
    """Work through unclassified on-chain launches. Returns a stats dict."""
    rows = con.execute(
        "SELECT address, symbol, name FROM rh_launch"
        " WHERE cohort='unclassified' AND source='onchain'"
        " ORDER BY first_seen ASC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return {}
    hot = _current_hot(con)
    t = db.now()
    echoes, plain, scored = [], [], 0

    for r in rows:
        sym = feeds.norm_symbol(r["symbol"])
        if sym and sym in hot and sym not in feeds.NOISE_SYMBOLS:
            echoes.append((r, sym, hot[sym]))
        else:
            plain.append(r)

    # Only take as many matches as we have Jev budget for. The rest stay
    # 'unclassified' and are picked up next cycle -- leaving them unscored
    # would drop them through to the age fallback, which is NULL on on-chain
    # rows and would silently file every one of them as a squat.
    deferred = len(echoes) - MAX_JEV_PER_PASS if len(echoes) > MAX_JEV_PER_PASS else 0
    for r, sym, parents in echoes[:MAX_JEV_PER_PASS]:
        p = max(parents, key=lambda x: x.get("vol24") or 0)
        state = {
            "new_token": {"symbol": sym, "name": r["name"], "chain": "robinhood"},
            "parent_token": {"symbol": sym, "chain": p["chain"],
                             "market_cap_usd": p["mcap"], "change_24h_pct": p["pct24"],
                             "volume_24h_usd": p["vol24"]},
        }
        a = jev.ask(state, QUESTIONS)
        live = att = conf = None
        if "__error__" not in a:
            ans = a.get("answers") or {}
            live = (ans.get("live_narrative") or {}).get("noul")
            sc = ans.get("attention") or {}
            att, conf = sc.get("score"), sc.get("confidence")
            scored += 1
        con.execute(
            "INSERT OR REPLACE INTO jev_score"
            " (address,live_narrative,attention,confidence,parent_symbol,scored_at)"
            " VALUES (?,?,?,?,?,?)", (r["address"], live, att, conf, sym, t))
        con.execute("UPDATE rh_launch SET cohort='echo' WHERE address=?", (r["address"],))
        con.execute(
            "INSERT OR IGNORE INTO echo_link"
            " (rh_address,parent_chain,parent_address,symbol,detected_at,"
            "  parent_mcap,parent_liq,parent_vol24,parent_pct24,parent_age_h,lag_h)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["address"], p["chain"], p["address"], sym, t, p["mcap"], p["liq"],
             p["vol24"], p["pct24"], None, None))

    for r in random.sample(plain, min(CONTROLS_PER_PASS, len(plain))):
        con.execute("UPDATE rh_launch SET cohort='control' WHERE address=?", (r["address"],))
    # everything else stays out of both cohorts -- recorded, never sampled
    for r in plain:
        con.execute("UPDATE rh_launch SET cohort=COALESCE(NULLIF(cohort,'unclassified'),'unsampled')"
                    " WHERE address=?", (r["address"],))
    con.commit()
    return {"seen": len(rows), "echo": min(len(echoes), MAX_JEV_PER_PASS),
            "jev": scored, "deferred": deferred,
            "control": min(CONTROLS_PER_PASS, len(plain))}


def is_fresh(con, address):
    """Fresh/squat using Jev's judgement, falling back to the age threshold."""
    r = con.execute("SELECT live_narrative FROM jev_score WHERE address=?",
                    (address,)).fetchone()
    if r and r["live_narrative"] is not None:
        return r["live_narrative"] >= LIVE_THRESHOLD
    a = con.execute("SELECT MIN(parent_age_h) FROM echo_link WHERE rh_address=?",
                    (address,)).fetchone()[0]
    return a is not None and a <= 72
