"""The forward collector.

Robinhood Chain's launch history is not retrievable: GeckoTerminal's
`new_pools` reaches back about seven minutes at the chain's observed launch
rate, and the block explorer is behind Cloudflare. So the dataset this study
needs does not exist until something records it. That is this module's whole
job -- run continuously, miss as little as possible, and never write a fact
that was not knowable at the time it was written.

Two cohorts are recorded, always:

  echo    -- an RH launch whose ticker matches a token currently running on
             Solana, Base or BSC.
  control -- randomly sampled RH launches that matched nothing.

The control group is not optional. "Echo clones average +80%" means nothing
if a random Robinhood Chain launch also averages +80%; only the spread
between the cohorts is evidence of an edge.
"""
import datetime
import random
import time

from . import classify as _classify
from . import db, feeds, onchain

CYCLE_SEC = 180          # well inside the ~7min new_pools horizon
HOT_REFRESH_SEC = 900    # parent boards move slower than the firehose
CONTROLS_PER_CYCLE = 2
TRACK_HOURS = 48         # outcome window
DENSE_HOURS = 2          # poll every cycle for this long, then back off
SPARSE_SEC = 900


def _ts(iso):
    """'2026-09-22T13:29:36Z' -> epoch seconds."""
    if not iso:
        return None
    try:
        return int(datetime.datetime.strptime(
            iso, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc).timestamp())
    except ValueError:
        return None


def refresh_hot(con):
    rows = feeds.fetch_hot_tickers()
    t = db.now()
    con.executemany(
        "INSERT OR REPLACE INTO hot_ticker"
        " (symbol,chain,address,seen_at,mcap,liq,vol24,pct24,pool_created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [(h["symbol"], h["chain"], h["address"], t, h["mcap"], h["liq"],
          h["vol24"], h["pct24"], h["pool_created_at"]) for h in rows],
    )
    con.commit()
    return len(rows)


def _current_hot(con, max_age_sec=3600):
    """Latest snapshot per (symbol, chain, address), recent ones only.

    A stale board would manufacture matches against tokens that stopped
    running days ago, so anything older than an hour is not a live parent.
    """
    cur = con.execute(
        "SELECT symbol, chain, address, mcap, liq, vol24, pct24, pool_created_at,"
        "       MAX(seen_at) AS seen_at"
        "  FROM hot_ticker WHERE seen_at >= ?"
        " GROUP BY symbol, chain, address",
        (db.now() - max_age_sec,),
    )
    out = {}
    for r in cur:
        out.setdefault(r["symbol"], []).append(dict(r))
    return out


def ingest_onchain(con):
    """Record launches straight from mint events.

    Runs alongside the GeckoTerminal feed rather than replacing it: GT has 24h
    of history behind it, and swapping the source outright would change the
    denominator mid-study with no way to tell the two regimes apart. Every row
    carries `source`, so analysis can use either feed or compare them.
    """
    seen = {r[0] for r in con.execute("SELECT address FROM onchain_seen")}
    cursor = con.execute(
        "SELECT MAX(first_block) FROM onchain_seen").fetchone()[0]
    kept, head, err, st = onchain.poll_launches(seen, since_block=cursor)
    if err:
        return 0, err, st
    t = db.now()
    con.executemany(
        "INSERT OR IGNORE INTO onchain_seen (address, first_block, seen_at)"
        " VALUES (?,?,?)",
        [(a, m.get("block"), t) for a, m in kept.items()])
    con.executemany(
        "INSERT OR IGNORE INTO rh_launch"
        " (address,symbol,name,pool,dex,pool_created_at,first_seen,"
        "  init_liq,init_fdv,cohort,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(a, feeds.norm_symbol(m.get("symbol")), m.get("name") or "", "", "",
          None, t, 0.0, 0.0, "unclassified", "onchain") for a, m in kept.items()])
    con.commit()
    return len(kept), None, st


def ingest(con):
    """One pass of the firehose: record launches, classify, link echoes."""
    launches = feeds.fetch_rh_launches(pages=5)
    hot = _current_hot(con)
    t = db.now()

    known = {r[0] for r in con.execute(
        "SELECT address FROM rh_launch WHERE first_seen > ?", (t - 86400,))}
    fresh = [l for l in launches if l["address"] not in known]

    echoes, plain = [], []
    for l in fresh:
        (echoes if (l["symbol"] and l["symbol"] in hot) else plain).append(l)

    # Controls are sampled, not exhaustive -- 20k launches a day is far more
    # than the outcome tracker can poll, and a random sample is an unbiased
    # estimator of the baseline either way.
    controls = random.sample(plain, min(CONTROLS_PER_CYCLE, len(plain)))

    rows = [(l["address"], l["symbol"], l["name"], l["pool"], l["dex"],
             l["pool_created_at"], t, l["init_liq"], l["init_fdv"], cohort, "gt")
            for cohort, group in (("echo", echoes), ("control", controls))
            for l in group]
    con.executemany(
        "INSERT OR REPLACE INTO rh_launch"
        " (address,symbol,name,pool,dex,pool_created_at,first_seen,"
        "  init_liq,init_fdv,cohort,source) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    links = []
    for l in echoes:
        rh_born = _ts(l["pool_created_at"]) or t
        for p in hot[l["symbol"]]:
            p_born = _ts(p["pool_created_at"])
            links.append((
                l["address"], p["chain"], p["address"], l["symbol"], t,
                p["mcap"], p["liq"], p["vol24"], p["pct24"],
                (t - p_born) / 3600.0 if p_born else None,
                (rh_born - p_born) / 3600.0 if p_born else None,
            ))
    con.executemany(
        "INSERT OR IGNORE INTO echo_link"
        " (rh_address,parent_chain,parent_address,symbol,detected_at,"
        "  parent_mcap,parent_liq,parent_vol24,parent_pct24,parent_age_h,lag_h)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", links)
    con.commit()
    return len(launches), len(rows), len(echoes)


def track(con):
    """Poll outcomes for everything inside the tracking window.

    Dense early, sparse later: the decision this dataset has to support is
    "enter at +5/+15/+60 minutes", so resolution matters most in the first
    couple of hours and barely at all on day two.
    """
    t = db.now()
    # Only cohort members. The on-chain feed records every launch, but most are
    # never sampled into a cohort -- polling those wastes the DexScreener budget
    # on tokens no analysis will ever read (and most have no pool yet, so the
    # call returns nothing anyway).
    cur = con.execute(
        "SELECT l.address, l.first_seen, MAX(o.ts) AS last_ts"
        "  FROM rh_launch l LEFT JOIN obs o ON o.address = l.address"
        " WHERE l.first_seen > ? AND l.cohort IN ('echo','control')"
        " GROUP BY l.address",
        (t - TRACK_HOURS * 3600,),
    )
    due = []
    for r in cur:
        age = t - r["first_seen"]
        if age <= DENSE_HOURS * 3600 or not r["last_ts"] or (t - r["last_ts"]) >= SPARSE_SEC:
            due.append(r["address"])

    prices = feeds.fetch_prices(due)

    # Append a social row only on change, so the series stays small and the
    # FIRST row is the state as it was when we detected the token.
    last = {r[0]: (r[1], r[2], r[3], r[4]) for r in con.execute(
        "SELECT address, tw, tg, web, img FROM social_snapshot s WHERE ts ="
        " (SELECT MAX(ts) FROM social_snapshot x WHERE x.address = s.address)")}
    changed = []
    for a, p_ in prices.items():
        cur = (int(p_.get("tw", False)), int(p_.get("tg", False)),
               int(p_.get("web", False)), int(p_.get("img", False)))
        if last.get(a) != cur:
            changed.append((a, t, *cur))
    if changed:
        con.executemany("INSERT OR REPLACE INTO social_snapshot"
                        " (address,ts,tw,tg,web,img) VALUES (?,?,?,?,?,?)", changed)

    con.executemany(
        "INSERT OR REPLACE INTO obs"
        " (address,ts,price,fdv,liq,vol_h1,buys_h1,sells_h1) VALUES (?,?,?,?,?,?,?,?)",
        [(a, t, p["price"], p["fdv"], p["liq"], p["vol_h1"], p["buys_h1"], p["sells_h1"])
         for a, p in prices.items()],
    )
    con.commit()
    return len(due), len(prices)


def run(cycles=None, verbose=True):
    con = db.connect()
    last_hot = 0.0
    i = 0
    while cycles is None or i < cycles:
        started = time.monotonic()
        note = ""
        try:
            if time.time() - last_hot >= HOT_REFRESH_SEC:
                n_hot = refresh_hot(con)
                last_hot = time.time()
                note = f"hot={n_hot}"
            n_seen, n_new, n_echo = ingest(con)
            n_oc, oc_err, oc_st = ingest_onchain(con)
            if oc_err:
                note = (note + " " if note else "") + f"onchain:{oc_err}"
            elif oc_st:
                note = (note + " " if note else "") + f"oc={n_oc}/{oc_st.get('raw',0)}"
            cl = _classify.classify(con)
            if cl:
                note = (note + " " if note else "") + \
                       f"cls={cl['echo']}e/{cl['control']}c/j{cl['jev']}"
            n_due, n_got = track(con)
            # The chain launches ~20k pools a day, so it never genuinely goes
            # quiet: seeing zero means the fetch failed, and recording that as
            # a clean cycle would hide a hole in the denominator.
            kind = "ok" if n_seen else "empty"
            con.execute(
                "INSERT OR REPLACE INTO cycle_log"
                " (ts,kind,n_seen,n_new,n_echo,n_tracked,note) VALUES (?,?,?,?,?,?,?)",
                (db.now(), kind, n_seen, n_new, n_echo, n_got, note))
            con.commit()
            if verbose:
                print(f"[{datetime.datetime.now():%H:%M:%S}] seen={n_seen:3d} "
                      f"new={n_new:2d} echo={n_echo:2d} tracked={n_got:4d}/{n_due:4d} {note}",
                      flush=True)
        except Exception as e:
            # A collector that dies on one bad response loses the window it was
            # built to capture. Log the gap and keep going.
            con.execute(
                "INSERT OR REPLACE INTO cycle_log (ts,kind,note) VALUES (?,?,?)",
                (db.now(), "error", f"{type(e).__name__}: {e}"))
            con.commit()
            if verbose:
                print(f"[{datetime.datetime.now():%H:%M:%S}] ERROR {type(e).__name__}: {e}",
                      flush=True)
        i += 1
        if cycles is None or i < cycles:
            time.sleep(max(0, CYCLE_SEC - (time.monotonic() - started)))
    return con
