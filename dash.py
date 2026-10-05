"""Local live dashboard for the echo study.

Reads the SQLite the collector is writing, plus the saved FOMO cohort, and
serves one page on localhost. Nothing is published and nothing leaves the
machine.

The page is built around a single honesty rule: a number that is not yet
trustworthy must LOOK untrustworthy. Cohort rows below the sample threshold
render greyed with their n shown, the verdict banner says what is missing
rather than showing a figure, and collector gaps are surfaced as a status
chip instead of being averaged away.

Run:  python3 dash.py       ->  http://localhost:8787
"""
import json
import pathlib
import sqlite3
import statistics
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).parent
DB = ROOT / "echo" / "echo.db"
COHORT = ROOT / "cohort" / "cohort_rh.json"
PORT = int(__import__("os").environ.get("ECHO_DASH_PORT", 8799))

MIN_N = 20          # per-cohort sample floor before any verdict
DELAYS = (5, 15, 60)
TP, SL, TIMEOUT_H = 1.00, -0.50, 24
FRESH_H = 72


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def one(con, sql, args=()):
    r = con.execute(sql, args).fetchone()
    return r[0] if r else None


# ------------------------------------------------------------------ backtest
def _sim(obs, entry_ts):
    entry = next((o for o in obs if o[0] >= entry_ts), None)
    if not entry or not entry[1]:
        return None
    p0, peak = entry[1], 0.0
    deadline = entry[0] + TIMEOUT_H * 3600
    for ts, price in obs:
        if ts < entry[0]:
            continue
        r = price / p0 - 1.0
        peak = max(peak, r)
        if r >= TP:
            return TP, peak
        if r <= SL:
            return SL, peak
        if ts >= deadline:
            return r, peak
    return obs[-1][1] / p0 - 1.0, peak


def backtest(con):
    launches = q(con, """
        SELECT l.address, l.cohort, l.first_seen,
               (SELECT MIN(parent_age_h) FROM echo_link e WHERE e.rh_address=l.address)
          FROM rh_launch l""")
    series = {}
    for addr, ts, price in q(con, "SELECT address, ts, price FROM obs WHERE price>0 ORDER BY ts"):
        series.setdefault(addr, []).append((ts, price))

    out = {}
    for d in DELAYS:
        buckets = {"echo/fresh": [], "echo/squat": [], "control": []}
        for addr, cohort, seen, page in launches:
            obs = series.get(addr) or []
            if len(obs) < 2:
                continue
            s = _sim(obs, seen + d * 60)
            if not s:
                continue
            if cohort == "control":
                buckets["control"].append(s)
            else:
                buckets["echo/fresh" if (page is not None and page <= FRESH_H)
                        else "echo/squat"].append(s)
        rows = []
        for label, vals in buckets.items():
            if not vals:
                rows.append({"label": label, "n": 0})
                continue
            rets = [v[0] for v in vals]
            peaks = [v[1] for v in vals]
            rows.append({
                "label": label, "n": len(rets),
                "median": statistics.median(rets),
                "mean": statistics.fmean(rets),
                "win": sum(1 for r in rets if r > 0) / len(rets),
                "p2x": sum(1 for p in peaks if p >= 1.0) / len(peaks),
                "prug": sum(1 for r in rets if r <= SL) / len(rets),
                "medpeak": statistics.median(peaks),
                "enough": len(rets) >= MIN_N,
            })
        out[d] = rows
    return out


# --------------------------------------------------------------------- state
_STATE_CACHE = {"ts": 0, "val": None}


def build_state(max_age=20):
    """Cached briefly: the page polls every 30s and this walks the whole DB."""
    import time as _t
    if _STATE_CACHE["val"] is not None and _t.time() - _STATE_CACHE["ts"] < max_age:
        return _STATE_CACHE["val"]
    val = _build_state()
    _STATE_CACHE.update(ts=_t.time(), val=val)
    return val


def _build_state():
    if not DB.exists():
        return {"error": "no database yet - is the collector running?"}
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    now = int(time.time())

    span = q(con, "SELECT MIN(first_seen), MAX(first_seen) FROM rh_launch")[0]
    hours = ((span[1] or now) - (span[0] or now)) / 3600 or 0.001
    n_launch = one(con, "SELECT COUNT(*) FROM rh_launch") or 0
    last_ts = one(con, "SELECT MAX(ts) FROM cycle_log") or 0

    health = {
        "launches": n_launch,
        "echo": one(con, "SELECT COUNT(*) FROM rh_launch WHERE cohort='echo'") or 0,
        "control": one(con, "SELECT COUNT(*) FROM rh_launch WHERE cohort='control'") or 0,
        "obs": one(con, "SELECT COUNT(*) FROM obs") or 0,
        "ok": one(con, "SELECT COUNT(*) FROM cycle_log WHERE kind='ok'") or 0,
        "err": one(con, "SELECT COUNT(*) FROM cycle_log WHERE kind='error'") or 0,
        "empty": one(con, "SELECT COUNT(*) FROM cycle_log WHERE kind='empty'") or 0,
        "hours": hours,
        "last_cycle_age": now - last_ts if last_ts else None,
        "per_hour": n_launch / hours,
        "src_gt": one(con, "SELECT COUNT(*) FROM rh_launch WHERE source='gt'") or 0,
        "src_oc": one(con, "SELECT COUNT(*) FROM rh_launch WHERE source='onchain'") or 0,
        "jev_scored": one(con, "SELECT COUNT(*) FROM jev_score") or 0,
        "unclassified": one(con, "SELECT COUNT(*) FROM rh_launch WHERE cohort='unclassified'") or 0,
    }

    # live echo detections, newest first, with return since detection
    echoes = []
    for (addr, sym, seen, pchain, page, ppct, plag, pmcap, src,
         jlive, jattn, jconf) in q(con, """
        SELECT l.address, l.symbol, l.first_seen, e.parent_chain, e.parent_age_h,
               e.parent_pct24, e.lag_h, e.parent_mcap, l.source,
               j.live_narrative, j.attention, j.confidence
          FROM rh_launch l JOIN echo_link e ON e.rh_address = l.address
          LEFT JOIN jev_score j ON j.address = l.address
         WHERE l.cohort='echo'
      GROUP BY l.address
      ORDER BY l.first_seen DESC LIMIT 40"""):
        obs = q(con, "SELECT ts, price, fdv, liq FROM obs WHERE address=? AND price>0 ORDER BY ts", (addr,))
        ret = None
        if len(obs) >= 2 and obs[0][1]:
            ret = obs[-1][1] / obs[0][1] - 1.0
        echoes.append({
            "address": addr, "symbol": sym or "?", "age_min": (now - seen) / 60,
            "parent_chain": pchain, "parent_age_h": page, "parent_pct24": ppct,
            "lag_h": plag, "parent_mcap": pmcap,
            "source": src, "jev_live": jlive, "jev_attn": jattn, "jev_conf": jconf,
            "fresh": (jlive >= 0.50) if jlive is not None
                     else (page is not None and page <= FRESH_H),
            "by_jev": jlive is not None,
            "ret": ret, "n_obs": len(obs),
            "fdv": obs[-1][2] if obs else None, "liq": obs[-1][3] if obs else None,
        })

    # hot parents currently on the watchlist
    hot = [{"symbol": s, "chain": c, "mcap": m, "pct24": p, "vol24": v}
           for (s, c, m, p, v) in q(con, """
        SELECT symbol, chain, mcap, pct24, vol24 FROM hot_ticker
         WHERE seen_at >= ? GROUP BY symbol, chain
      ORDER BY vol24 DESC LIMIT 12""", (now - 3600,))]

    con.close()

    cohort = {"available": False}
    if COHORT.exists():
        c = json.loads(COHORT.read_text())
        split = {}
        for t in c["traders"]:
            for k, v in (t.get("split") or {}).items():
                if not k.isdigit():
                    split[k] = split.get(k, 0) + v
        tot = sum(abs(v) for v in split.values()) or 1
        cohort = {
            "available": True, "n_board": c["n_board"], "n_with_rh": c["n_with_rh"],
            "split": sorted(({"chain": k, "pnl": v, "share": abs(v) / tot}
                             for k, v in split.items()), key=lambda x: -x["pnl"])[:6],
            "top": c["traders"][:12],
        }

    # significance of the fresh-vs-control gap, per entry delay
    sig, zero_share = {}, 0.0
    bt_con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    bt_con.row_factory = sqlite3.Row   # analyze.cohorts reads rows by name
    from echo import analyze as _an
    for d in DELAYS:
        cc = _an.cohorts(bt_con, d)
        f, ct = cc["echo/fresh"], cc["control"]
        if d == 15 and (f or ct):
            allr = [r["ret"] for v in cc.values() for r in v]
            zero_share = (sum(1 for r in allr if abs(r) < 1e-9) / len(allr)) if allr else 0.0
        if len(f) >= MIN_N and len(ct) >= MIN_N:
            k1 = sum(1 for r in f if r["peak"] >= 1.0)
            k2 = sum(1 for r in ct if r["peak"] >= 1.0)
            r = _an.z_prop(k1, len(f), k2, len(ct))
            if r:
                sig[d] = {"p1": r[0], "p2": r[1], "lift": r[2], "p": r[3],
                          "k1": k1, "n1": len(f), "k2": k2, "n2": len(ct)}
    bt_con.close()

    return {"sig": sig, "zero_share": zero_share, "health": health, "backtest": backtest(sqlite3.connect(f"file:{DB}?mode=ro", uri=True)),
            "echoes": echoes, "hot": hot, "cohort": cohort, "now": now,
            "min_n": MIN_N, "delays": list(DELAYS)}


# ---------------------------------------------------------------------- page
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>echo study</title>
<style>
:root{
  color-scheme: light;
  --plane:#f9f9f7; --surface:#fcfcfb;
  --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b; --up:#006300;
}
@media (prefers-color-scheme:dark){ :root:not([data-theme="light"]){
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --up:#0ca30c;
}}
:root[data-theme="dark"]{
  color-scheme: dark;
  --plane:#0d0d0d; --surface:#1a1a19;
  --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --up:#0ca30c;
}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
  font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 64px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:6px}
h1{font-size:19px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border-radius:999px;
  font-size:12px;font-weight:600;border:1px solid var(--ring)}
.dot{width:7px;height:7px;border-radius:50%;flex:none}
section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:18px 20px;margin-top:18px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);
  margin:0 0 4px;font-weight:600}
.note{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(122px,1fr));gap:12px}
.tile{padding:11px 13px;border:1px solid var(--ring);border-radius:9px;background:var(--plane)}
.tile .k{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:23px;font-weight:600;margin-top:3px;letter-spacing:-.02em}
.tile .v small{font-size:12px;font-weight:400;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;font-size:11.5px;color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.05em;padding:0 8px 7px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:8px;border-bottom:1px solid var(--grid);font-size:13px}
tr:last-child td{border-bottom:none}
.scroll{overflow-x:auto}
.dim{opacity:.45}
.banner{border-radius:9px;padding:12px 14px;font-size:13.5px;border:1px solid var(--ring);
  background:var(--plane);display:flex;gap:10px;align-items:flex-start}
.banner b{font-weight:600}
.bar{height:9px;border-radius:0 4px 4px 0;background:var(--s1)}
.barrow{display:grid;grid-template-columns:92px 1fr 118px;gap:10px;align-items:center;
  padding:5px 0;font-size:13px}
.badge{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:5px;
  letter-spacing:.03em;text-transform:uppercase}
.tabs{display:flex;gap:6px;margin:0 0 12px}
.tab{padding:5px 12px;border-radius:7px;border:1px solid var(--ring);background:var(--plane);
  color:var(--ink2);font-size:12.5px;cursor:pointer;font-weight:600}
.tab[aria-selected="true"]{background:var(--s1);border-color:var(--s1);color:#fff}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.pos{color:var(--up);font-weight:600}
.neg{color:var(--crit);font-weight:600}
footer{color:var(--muted);font-size:12px;margin-top:26px;text-align:center}
.help{display:none;border-left:3px solid var(--s1);background:var(--plane);border-radius:0 8px 8px 0;
  padding:12px 14px;margin:0 0 14px;font-size:13px;line-height:1.62;color:var(--ink2)}
body.show-help .help{display:block}
.help b{color:var(--ink)}
.help dl{margin:8px 0 0;display:grid;grid-template-columns:auto 1fr;gap:5px 12px}
.help dt{font-weight:600;color:var(--ink);white-space:nowrap;font-size:12.5px}
.help dd{margin:0}
.help .trap{margin-top:10px;padding:8px 10px;border-radius:6px;
  background:color-mix(in srgb,var(--warn) 12%,transparent);color:var(--ink)}
.helpbtn{padding:4px 11px;border-radius:7px;border:1px solid var(--ring);background:var(--plane);
  color:var(--ink2);font-size:12.5px;cursor:pointer;font-weight:600}
body.show-help .helpbtn{background:var(--s1);border-color:var(--s1);color:#fff}
</style></head><body><div class="wrap">
<header>
  <h1>echo study</h1>
  <span id="status"></span>
  <span class="sub" id="updated"></span>
  <button class="helpbtn" id="helpbtn" onclick="toggleHelp()">? how to read this</button>
</header>
<div id="app"></div>
<footer>local only · reads <span class="mono">echo/echo.db</span> live · refreshes every 30s</footer>
</div>
<script>
const $ = (h) => { const d=document.createElement('div'); d.innerHTML=h.trim(); return d.firstChild; };
const pct = (x,d=1) => x===null||x===undefined||Number.isNaN(x) ? '—' : (x*100).toFixed(d)+'%';
const usd = (x) => x===null||x===undefined ? '—' :
  Math.abs(x)>=1e9?'$'+(x/1e9).toFixed(2)+'B':Math.abs(x)>=1e6?'$'+(x/1e6).toFixed(2)+'M':
  Math.abs(x)>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+Number(x).toFixed(0);
const sgn = (x) => x===null||x===undefined ? '<span class="dim">—</span>' :
  `<span class="${x>=0?'pos':'neg'}">${x>=0?'+':''}${(x*100).toFixed(1)}%</span>`;
let DELAY = 15;

function statusChip(h){
  let tone='good', label='collecting';
  if(h.last_cycle_age===null||h.last_cycle_age>420){tone='crit';label='collector stalled';}
  else if(h.err>0){tone='crit';label=h.err+' failed cycles';}
  else if(h.empty>0){tone='warn';label=h.empty+' gap'+(h.empty>1?'s':'');}
  const c={good:'var(--good)',warn:'var(--warn)',crit:'var(--crit)'}[tone];
  const icon={good:'●',warn:'▲',crit:'■'}[tone];
  return `<span class="chip" style="color:${c}"><span class="dot" style="background:${c}"></span>${icon} ${label}</span>`;
}

function render(s){
  document.getElementById('status').innerHTML = statusChip(s.health);
  document.getElementById('updated').textContent =
    `updated ${new Date(s.now*1000).toLocaleTimeString()} · last cycle ${s.health.last_cycle_age}s ago`;
  const h=s.health, app=document.getElementById('app'); app.innerHTML='';

  // ---- health tiles
  app.append($(`<section><h2>Collector</h2>
   <p class="note">Robinhood Chain launches ~20k pools/day and only ~7 minutes of that is retrievable, so a gap is permanent. <b>empty</b> cycles are fetches that returned nothing — they are holes in the denominator, not quiet periods.</p>
   <div class="tiles">
    <div class="tile"><div class="k">collected</div><div class="v">${h.hours.toFixed(1)}<small> h</small></div></div>
    <div class="tile"><div class="k">launches</div><div class="v">${h.launches}</div></div>
    <div class="tile"><div class="k">feed gt / chain</div><div class="v" style="font-size:19px">${h.src_gt}<small> / </small>${h.src_oc}</div></div>
    <div class="tile"><div class="k">jev scored</div><div class="v">${h.jev_scored}<small> / ${h.unclassified} queued</small></div></div>
    <div class="tile"><div class="k">echo / control</div><div class="v">${h.echo}<small> / ${h.control}</small></div></div>
    <div class="tile"><div class="k">observations</div><div class="v">${h.obs}</div></div>
    <div class="tile"><div class="k">cycles ok</div><div class="v">${h.ok}</div></div>
    <div class="tile"><div class="k">gaps</div><div class="v" style="color:${(h.err+h.empty)?'var(--warn)':'inherit'}">${h.err+h.empty}</div></div>
   </div><div class="help">
<b>What this is.</b> The collector watches every new Robinhood Chain pool and records two groups: tokens whose ticker matches something already running on another chain (<b>echo</b>), and random tokens that matched nothing (<b>control</b>). Control is the baseline — without it, "echo clones go up" is unfalsifiable, because on this chain lots of things go up.
<dl>
<dt>collected</dt><dd>Hours of data. <b>Below ~48h nothing here is evidence.</b></dd>
<dt>launches</dt><dd>Tokens recorded — <i>not</i> all launches. The chain does ~20k/day; controls are sampled.</dd>
<dt>echo / control</dt><dd>The two groups being compared. Control grows faster by design.</dd>
<dt>observations</dt><dd>Price readings. A token needs at least 2 before it can produce a return.</dd>
<dt>gaps</dt><dd>Fetches that came back empty or errored.</dd>
</dl>
<div class="trap"><b>The trap:</b> a gap is not a quiet period — this chain never goes quiet. Every gap is a set of launches we will never know about, and we cannot tell whether the ones we missed were winners or losers. Gaps bias the result in an unknown direction, which is worse than bias in a known one.</div></div></section>`));

  // ---- verdict + backtest
  const rows=s.backtest[DELAY]||[];
  const fresh=rows.find(r=>r.label==='echo/fresh'), ctl=rows.find(r=>r.label==='control');
  let banner;
  if(!fresh||!ctl||!fresh.enough||!ctl.enough){
    const need=Math.max(0,s.min_n-(fresh?fresh.n:0)), needC=Math.max(0,s.min_n-(ctl?ctl.n:0));
    banner=`<div class="banner" style="border-color:var(--warn)"><b style="color:var(--warn)">▲ No verdict yet.</b>
      <span>Need ${s.min_n} per cohort. Short by <b>${need}</b> fresh-echo and <b>${needC}</b> control.
      Rows below are shown for shape only — they are not evidence.</span></div>`;
  } else {
    const st=s.sig&&s.sig[DELAY];
    if(!st){ banner=`<div class="banner"><span>no test available</span></div>`; }
    else{
      const good=st.lift>1&&st.p<0.05, strong=st.p<0.05/9;
      const col=good?'var(--good)':'var(--crit)';
      banner=`<div class="banner" style="border-color:${col}">
        <b style="color:${col}">${good?'●':'■'} P(2x) ${(st.p1*100).toFixed(1)}% vs ${(st.p2*100).toFixed(1)}%${st.lift?' — '+st.lift.toFixed(1)+'x':''}</b>
        <span>fresh-echo (${st.k1}/${st.n1}) vs control (${st.k2}/${st.n2}) at +${DELAY}min entry.
        p=${st.p.toFixed(4)} — <b>${strong?'significant after correcting for 9 tests':(st.p<0.05?'suggestive only; does not survive multiple-comparison correction':'not significant')}</b>.
        Median is not used: ${(s.zero_share*100).toFixed(0)}% of launches never trade again and sit at exactly 0%, so every cohort medians to 0.</span></div>`;
    }
  }
  const tabs=s.delays.map(d=>`<button class="tab" aria-selected="${d===DELAY}" onclick="DELAY=${d};render(window.__S)">+${d} min</button>`).join('');
  const body=rows.map(r=>{
    const dim=!r.enough?' class="dim"':'';
    if(!r.n) return `<tr class="dim"><td>${r.label}</td><td>0</td><td colspan="6">no data yet</td></tr>`;
    return `<tr${dim}><td>${r.label}${r.label==='echo/fresh'?' <span class="badge" style="background:var(--s1);color:#fff">thesis</span>':''}</td>
     <td>${r.n}</td><td>${sgn(r.median)}</td><td>${sgn(r.mean)}</td><td>${pct(r.win,0)}</td>
     <td>${pct(r.p2x,0)}</td><td>${pct(r.prug,0)}</td><td>${pct(r.medpeak)}</td></tr>`;
  }).join('');
  app.append($(`<section><h2>Backtest</h2>
    <p class="note">Entry is always <b>delayed</b> — you cannot buy at launch price. Exit is mechanical: +100% / −50% / 24h, walked forward. <b>Peak is unrealizable</b> and shown only as context.</p>
    <div class="help">
<b>Read this one row against another, never on its own.</b> The question is never "did echo clones make money". It is <b>"did echo clones beat a random launch"</b>. If control did the same thing, there is no signal — just a chain where things move.
<dl>
<dt>n</dt><dd>Sample size. <b>Greyed rows are below 20 and should be ignored entirely</b>, however good the numbers look.</dd>
<dt>median</dt><dd>The typical outcome. <b>This is the number to use.</b></dd>
<dt>mean</dt><dd>The average. One 10x drags it up hard. If mean is far above median, a single token is carrying the whole result.</dd>
<dt>win</dt><dd>Share of trades that ended above entry.</dd>
<dt>P(2x)</dt><dd>How often the token ever doubled at any point — a <i>peak</i>, not something you captured.</dd>
<dt>P(rug)</dt><dd>How often it hit the −50% stop. Your downside frequency.</dd>
<dt>med peak</dt><dd>Typical best moment. <b>Unrealizable</b> — nobody sells the top. Context only.</dd>
</dl>
<b>The +5 / +15 / +60 tabs</b> are when you entered after the signal was visible. You cannot buy at launch price. <b>If an edge exists at +5min but is gone by +15, it is not tradeable by a human</b> — it belongs to whoever is faster than you.
<div class="trap"><b>The one number that decides everything:</b> the banner — fresh-echo median <i>minus</i> control median. Positive and it survived. Zero or negative and the thesis is dead, which is a real result and costs you nothing to learn.</div></div><div class="tabs">${tabs}</div>${banner}
    <div class="scroll" style="margin-top:14px"><table><thead><tr>
     <th>cohort</th><th>n</th><th>median</th><th>mean</th><th>win</th><th>P(2x)</th><th>P(rug)</th><th>med peak</th>
    </tr></thead><tbody>${body}</tbody></table></div></section>`));

  // ---- echo detections
  const er=s.echoes.map(e=>`<tr>
    <td><b>${e.symbol}</b> <span class="badge" style="background:${e.fresh?'var(--s1)':'var(--s2)'};color:#fff">${e.fresh?'fresh':'squat'}</span>
      ${e.by_jev?'<span class="badge" style="background:var(--plane);color:var(--muted);border:1px solid var(--ring)">jev</span>':''}</td>
    <td>${e.age_min<60?e.age_min.toFixed(0)+'m':(e.age_min/60).toFixed(1)+'h'}</td>
    <td>${e.parent_chain||'—'}</td>
    <td>${e.parent_age_h===null?'—':e.parent_age_h<48?e.parent_age_h.toFixed(0)+'h':(e.parent_age_h/24).toFixed(0)+'d'}</td>
    <td>${e.parent_pct24===null?'—':(e.parent_pct24>=0?'+':'')+e.parent_pct24.toFixed(0)+'%'}</td>
    <td>${usd(e.parent_mcap)}</td><td>${usd(e.fdv)}</td><td>${usd(e.liq)}</td>
    <td>${e.jev_live===null||e.jev_live===undefined?'<span class="dim">—</span>':e.jev_live.toFixed(2)}</td>
    <td>${e.jev_attn===null||e.jev_attn===undefined?'<span class="dim">—</span>':e.jev_attn.toFixed(1)+(e.jev_conf!==null&&e.jev_conf!==undefined?` <span class="dim">±${(1-e.jev_conf).toFixed(2)}</span>`:'')}</td>
    <td class="dim">${e.source||'—'}</td>
    <td>${e.n_obs<2?'<span class="dim">—</span>':sgn(e.ret)}</td></tr>`).join('');
  app.append($(`<section><h2>Echo detections</h2>
    <p class="note"><b>fresh</b> = parent launched within 72h and is running — the thesis. <b>squat</b> = parent is an old established brand, which is a different phenomenon and tracked separately.</p>
    <div class="help">
<b>Every ticker match, including the ones that died.</b> Keeping the failures visible is the point — a list of only the winners would show a 100% hit rate on any thesis.
<dl>
<dt>fresh</dt><dd>Parent launched within 72h and is running. <b>This is the thesis</b> — a live narrative spreading across chains.</dd>
<dt>squat</dt><dd>Parent is an old established brand (PUMP, BTC). Someone farming a famous name. Tracked separately because it is a different phenomenon; mixing them would average a real signal against noise.</dd>
<dt>parent 24h</dt><dd>How hard the parent is running. A parent that is flat is not a narrative.</dd>
<dt>parent age</dt><dd>What splits fresh from squat.</dd>
<dt>liq</dt><dd>Liquidity. <b>Check this before anything else</b> — a token you cannot exit is worth nothing no matter what the return column says.</dd>
<dt>jev live</dt><dd>Jev's probability that the parent is mid-run <i>right now</i>. Above 0.50 = fresh. This replaces the old "parent younger than 72h" rule, which confused <b>young</b> with <b>running</b> — it called a 25h-old parent sitting at 0% with zero volume "fresh", and missed a 996h-old parent that was up 30,000% in a day.</dd>
<dt>jev attn</dt><dd>Likelihood of drawing real buying (0-4), with Jev's own uncertainty after the ±.</dd>
<dt>feed</dt><dd><b>onchain</b> = read from mint events (complete). <b>gt</b> = GeckoTerminal (~7min window, lossy).</dd>
<dt>since detect</dt><dd>Return since we spotted it, at the current price.</dd>
</dl>
<div class="trap"><b>On Jev:</b> it is used as a <b>reject filter, never a picker</b>. Its confidence ran 0.45-0.76 on tokens it scored low but 0.00-0.24 on the ones that went on to double — it knows when to say no, not when to say yes. A high attention score is not a buy signal.</div>
<div class="trap"><b>The trap:</b> "since detect" is <i>not</i> what you would have made. It measures from detection to right now — no entry delay, no exit rule, and it keeps moving. The backtest panel is the honest version.</div></div><div class="scroll"><table><thead><tr><th>ticker</th><th>age</th><th>parent</th><th>parent age</th>
     <th>parent 24h</th><th>parent mcap</th><th>fdv</th><th>liq</th><th>jev live</th><th>jev attn</th><th>feed</th><th>since detect</th>
    </tr></thead><tbody>${er||'<tr><td colspan="12" class="dim">none yet</td></tr>'}</tbody></table></div></section>`));

  // ---- cohort
  if(s.cohort.available){
    const c=s.cohort, max=Math.max(...c.split.map(x=>Math.abs(x.pnl)));
    const bars=c.split.map(x=>`<div class="barrow"><span>${x.chain}</span>
      <span><span class="bar" style="width:${Math.max(2,Math.abs(x.pnl)/max*100)}%;display:block"></span></span>
      <span style="text-align:right">${usd(x.pnl)} <span class="dim">${pct(x.share,0)}</span></span></div>`).join('');
    const tr=c.top.map(t=>`<tr><td>${t.handle}</td><td>#${t.rank}</td>
      <td>${usd(t.pnl_rh)}</td><td>${pct(t.rh_share,0)}</td>
      <td class="mono">${(t.evm||'').slice(0,12)}…</td></tr>`).join('');
    app.append($(`<section><h2>FOMO cohort</h2>
      <p class="note">${c.n_with_rh} of ${c.n_board} leaderboard traders hold Robinhood Chain positions. The split below is across <b>those ${c.n_with_rh}</b>, so it overstates RH versus the whole board (which is 53.9%). Figures come from <b>current holdings</b> — largely <b>unrealised</b>.</p>
      <div class="help">
<b>Who the top FOMO traders are and where they actually earn.</b> FOMO ranks by account-wide PnL across every chain, which hides whether someone makes money on Robinhood Chain or somewhere else. This re-ranks by Robinhood Chain alone.
<dl>
<dt>RH pnl</dt><dd>Profit attributed to Robinhood Chain via each holding's network id.</dd>
<dt>RH share</dt><dd>What fraction of that trader's PnL is Robinhood Chain. 100% means they earn here and nowhere else.</dd>
<dt>board rank</dt><dd>Their position on FOMO's own leaderboard, for comparison.</dd>
</dl>
<div class="trap"><b>Two traps here.</b> First, the bar chart covers only the traders <i>holding</i> RH positions, so it reads higher than the whole board (53.9%). Second and more important: these are <b>current holdings, so mostly unrealised</b> — paper gains on open bags. On the one trader whose closed trades we could retrieve, realised RH PnL was <b>negative</b> while the unrealised figure showed +$5.75M. Until that resolves, treat "top trader" as "holding something that is up", not "has taken money off the table".</div></div>${bars}
      <div class="scroll" style="margin-top:16px"><table><thead><tr>
       <th>trader</th><th>board rank</th><th>RH pnl</th><th>RH share</th><th>evm wallet</th>
      </tr></thead><tbody>${tr}</tbody></table></div></section>`));
  }

  // ---- hot parents
  const hr=s.hot.map(x=>`<tr><td><b>${x.symbol}</b></td><td>${x.chain}</td><td>${usd(x.mcap)}</td>
    <td>${sgn(x.pct24/100)}</td><td>${usd(x.vol24)}</td></tr>`).join('');
  app.append($(`<section><h2>Watchlist — parents running elsewhere</h2>
    <p class="note">Tickers currently running on Solana, Base and BSC. A Robinhood Chain launch matching one of these is what fires an echo detection.</p>
    <div class="help">
<b>The input side.</b> These are tickers running right now on Solana, Base and BSC — refreshed every 15 minutes. When a Robinhood Chain launch matches one of these names, it gets tagged as an echo.
<div class="trap"><b>Worth knowing:</b> matching is by ticker text alone. A clone that renames itself is missed, and an innocent name collision is a false positive. Quote assets, majors and equities are filtered out, which removes the worst of it but not all.</div></div><div class="scroll"><table><thead><tr><th>ticker</th><th>chain</th><th>mcap</th><th>24h</th><th>vol 24h</th></tr></thead>
    <tbody>${hr||'<tr><td colspan="5" class="dim">none</td></tr>'}</tbody></table></div></section>`));
}

function toggleHelp(){
  const on = !document.body.classList.contains('show-help');
  document.body.classList.toggle('show-help', on);
  try{ localStorage.setItem('echo.help', on?'1':'0'); }catch(e){}
  document.getElementById('helpbtn').textContent = on ? '? hide guide' : '? how to read this';
}
try{ if(localStorage.getItem('echo.help')==='1'){
  document.body.classList.add('show-help');
  document.getElementById('helpbtn').textContent='? hide guide'; } }catch(e){}

async function tick(){
  try{
    const s=await (await fetch('/data',{cache:'no-store'})).json();
    if(s.error){document.getElementById('app').innerHTML=`<section><b>${s.error}</b></section>`;return;}
    window.__S=s; render(s);
  }catch(e){ document.getElementById('updated').textContent='connection lost — is dash.py still running?'; }
}
tick(); setInterval(tick, 30000);
</script></body></html>"""

EXIT_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>exit planner</title>
<style>
:root{color-scheme:light;--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;
 --muted:#898781;--grid:#e1e0d9;--ring:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;
 --good:#0ca30c;--warn:#fab219;--crit:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
 --plane:#0d0d0d;--surface:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--muted:#898781;
 --grid:#2c2c2a;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926}}
:root[data-theme="dark"]{color-scheme:dark;--plane:#0d0d0d;--surface:#1a1a19;--ink:#fff;
 --ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--ring:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:26px 20px 70px}
h1{font-size:20px;margin:0 0 3px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
section{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:18px 20px;margin-top:16px}
h2{font-size:12.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);margin:0 0 4px;font-weight:600}
.note{color:var(--muted);font-size:12.5px;margin:0 0 14px}
.form{display:grid;grid-template-columns:1fr 150px 130px auto;gap:10px;align-items:end}
@media(max-width:760px){.form{grid-template-columns:1fr}}
label{display:block;font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:5px}
input,select{width:100%;padding:9px 11px;border-radius:8px;border:1px solid var(--ring);
 background:var(--plane);color:var(--ink);font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
button{padding:10px 20px;border-radius:8px;border:1px solid var(--s1);background:var(--s1);
 color:#fff;font-weight:600;font-size:13.5px;cursor:pointer}
button:disabled{opacity:.5;cursor:default}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:4px}
.tile{padding:12px 14px;border:1px solid var(--ring);border-radius:9px;background:var(--plane)}
.tile .k{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile .v{font-size:23px;font-weight:600;margin-top:3px;letter-spacing:-.02em}
.tile .v small{font-size:12px;font-weight:400;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;font-size:11.5px;color:var(--muted);font-weight:600;text-transform:uppercase;
 letter-spacing:.05em;padding:0 8px 7px;border-bottom:1px solid var(--grid)}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--grid);font-size:13px}
tr:last-child td{border-bottom:none}
.banner{border-radius:9px;padding:12px 14px;font-size:13.5px;border:1px solid var(--ring);background:var(--plane);margin-bottom:14px}
.bar{height:9px;border-radius:0 4px 4px 0;background:var(--s1);display:block}
.barrow{display:grid;grid-template-columns:90px 1fr 74px;gap:10px;align-items:center;padding:4px 0;font-size:12.5px}
.dim{opacity:.5}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.pill{font-size:10.5px;font-weight:700;padding:2px 7px;border-radius:5px;text-transform:uppercase;letter-spacing:.03em}
footer{color:var(--muted);font-size:12px;margin-top:24px;text-align:center}
</style></head><body><div class="wrap">
<h1>exit planner</h1>
<p class="sub">Size a position by what you can get <b>out</b>. Impact is superlinear &mdash; a position worth half a pool costs about half its value to leave.</p>

<section>
  <div class="form">
    <div><label>token address</label><input id="addr" placeholder="0x… or Solana address"></div>
    <div><label>position (USD)</label><input id="pos" value="1000" inputmode="decimal"></div>
    <div><label>impact budget</label>
      <select id="slip"><option value="0.01">1%</option><option value="0.02" selected>2%</option>
      <option value="0.03">3%</option><option value="0.05">5%</option></select></div>
    <div><button id="go" onclick="run()">Plan</button></div>
  </div>
  <p class="note" style="margin:12px 0 0" id="hint">Live liquidity is pulled from the chain; nothing here leaves your machine except the token lookup.</p>
</section>

<div id="out"></div>
<footer>constant-product approximation &middot; assumes liquidity is static, so treat every number as the <b>optimistic</b> case</footer>
</div>
<script>
const usd=x=>x===null||x===undefined?'—':(Math.abs(x)>=1e9?'$'+(x/1e9).toFixed(2)+'B':
 Math.abs(x)>=1e6?'$'+(x/1e6).toFixed(2)+'M':Math.abs(x)>=1e3?'$'+(x/1e3).toFixed(1)+'k':'$'+Number(x).toFixed(0));
const usdx=x=>'$'+Number(x||0).toLocaleString(undefined,{maximumFractionDigits:0});
const pct=(x,d=1)=>x===null||x===undefined?'—':(x*100).toFixed(d)+'%';

async function run(){
  const b=document.getElementById('go'); b.disabled=true; b.textContent='…';
  const q=new URLSearchParams({address:document.getElementById('addr').value.trim(),
    position:document.getElementById('pos').value||0, max_slip:document.getElementById('slip').value});
  try{
    const d=await (await fetch('/exit/plan?'+q,{cache:'no-store'})).json();
    render(d);
  }catch(e){ document.getElementById('out').innerHTML=
    '<section><b style="color:var(--crit)">request failed</b> — is dash.py still running?</section>'; }
  b.disabled=false; b.textContent='Plan';
}

function render(d){
  const o=document.getElementById('out');
  if(d.info && !d.info.ok){ o.innerHTML=`<section><b style="color:var(--crit)">■ ${d.info.error}</b>
    <p class="note" style="margin-top:8px">Very new tokens are often not indexed yet. You can still plan by entering liquidity manually — coming soon.</p></section>`; return; }
  const i=d.info||{};
  const over = d.position>d.safe_position;
  const sev = d.one_shot_slip>=0.25?'crit':d.one_shot_slip>=0.10?'warn':'good';
  const col={good:'var(--good)',warn:'var(--warn)',crit:'var(--crit)'}[sev];
  const icon={good:'●',warn:'▲',crit:'■'}[sev];
  const verdict={good:'This size leaves you a real exit.',
    warn:'Workable, but you will give up real money getting out.',
    crit:'You do not have an exit at this size — this is a mark, not a position.'}[sev];

  let h=`<section><h2>${i.symbol||'token'} ${i.name?'· '+i.name:''} ${i.chain?`<span class="pill" style="background:var(--s1);color:#fff">${i.chain}</span>`:''}</h2>
   <p class="note">market cap ${usd(i.mcap)} · total liquidity ${usd(d.liquidity)} ${i.mcap?`· liq/mcap <b>${(d.liquidity/i.mcap*100).toFixed(1)}%</b>`:''}</p>
   <div class="banner" style="border-color:${col}"><b style="color:${col}">${icon} ${verdict}</b>
     <span> Selling ${usdx(d.position)} in one go moves the price <b>${pct(d.one_shot_slip)}</b>.</span></div>
   <div class="tiles">
    <div class="tile"><div class="k">max size @ ${pct(d.max_slip,0)}</div><div class="v">${usd(d.safe_position)}</div></div>
    <div class="tile"><div class="k">your one-shot impact</div><div class="v" style="color:${col}">${pct(d.one_shot_slip)}</div></div>
    <div class="tile"><div class="k">% of pool</div><div class="v">${d.pool_share===null?'—':d.pool_share.toFixed(2)+'%'}</div></div>
    <div class="tile"><div class="k">tranches to exit</div><div class="v">${d.tranches_needed?Math.ceil(d.tranches_needed):'—'}</div></div>
   </div></section>`;

  if(d.curve && d.curve.length){
    const mx=Math.max(...d.curve.map(c=>c.slip));
    h+=`<section><h2>Impact by position size</h2>
      <p class="note">What each size costs you on the way out, at this pool's current depth.</p>`+
      d.curve.map(c=>`<div class="barrow"><span class="mono">${usdx(c.pos)}</span>
        <span><span class="bar" style="width:${Math.max(2,c.slip/mx*100)}%;background:${c.slip>=0.25?'var(--crit)':c.slip>=0.10?'var(--warn)':'var(--s1)'}"></span></span>
        <span style="text-align:right">${pct(c.slip)}</span></div>`).join('')+`</section>`;
  }

  if(d.tranches && d.tranches.length){
    const netted=d.tranches.reduce((a,t)=>a+t.net,0);
    h+=`<section><h2>Exit ladder</h2>
      <p class="note">Selling in tranches that each stay under ${pct(d.max_slip,0)}. <b>Liquidity is assumed static</b> — every real sale drains the pool further, so this is the floor on how many trades it takes.</p>
      <table><thead><tr><th>#</th><th>sell</th><th>impact</th><th>you receive</th></tr></thead><tbody>`+
      d.tranches.map(t=>`<tr><td>${t.n}</td><td>${usdx(t.sell)}</td><td>${pct(t.slip)}</td><td>${usdx(t.net)}</td></tr>`).join('')+
      `</tbody></table>
      <p class="note" style="margin-top:12px">${d.stuck>1?`<b style="color:var(--crit)">${usdx(d.stuck)} still stuck after ${d.tranches.length} tranches.</b> `:''}
      Laddered you net about <b>${usdx(netted)}</b>; dumped in one trade you net <b>${usdx(d.position*(1-d.one_shot_slip))}</b>.</p></section>`;
  }

  if(d.ladder && d.ladder.length){
    h+=`<section><h2>Profit ladder — decide this now</h2>
      <p class="note">Pre-commit while nothing is at stake. At 10x you will be certain it goes to 100x; that certainty is what leaves people holding an unsellable mark.</p>
      <table><thead><tr><th>at</th><th>sell</th><th>proceeds</th><th>cumulative</th><th></th></tr></thead><tbody>`+
      d.ladder.map(r=>`<tr><td>${r.mult}x</td><td>${pct(r.frac,0)} of remaining</td><td>${usdx(r.proceeds)}</td><td>${usdx(r.cum)}</td>
        <td>${r.recovered?'<span class="pill" style="background:var(--good);color:#fff">cost basis out</span>':''}</td></tr>`).join('')+
      `</tbody></table>
      <p class="note" style="margin-top:12px">Past the rung marked <b>cost basis out</b>, everything remaining is a free roll — that single move is most of the benefit.</p></section>`;
  }
  o.innerHTML=h;
}
document.getElementById('addr').addEventListener('keydown',e=>{if(e.key==='Enter')run()});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/exit/plan"):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            g = lambda k, d=None: (q.get(k) or [d])[0]
            import exitplan
            try:
                self._json(exitplan.plan(address=g("address") or None,
                                         position=float(g("position") or 0),
                                         max_slip=float(g("max_slip") or 0.02)))
            except Exception as e:
                self._json({"info": {"ok": False, "error": f"{type(e).__name__}: {e}"}})
            return
        if self.path.startswith("/exit"):
            body = EXIT_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/data"):
            payload = json.dumps(build_state(), default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(port=PORT, tries=12):
    """Bind the first free port at or above `port`.

    Ports get squatted by unrelated dev servers; failing with "address already
    in use" would look like the dashboard itself is broken.

    Threaded, because a single slow handler (a token lookup that has to probe
    several chains) would otherwise block every other request -- including
    re-loading the page, which makes the whole UI look dead.
    """
    last = None
    for p in range(port, port + tries):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError as e:
            last = e
            continue
        print(f"echo dashboard -> http://localhost:{p}   (ctrl-C to stop)", flush=True)
        srv.serve_forever()
        return
    raise SystemExit(f"no free port in {port}-{port + tries - 1}: {last}")


if __name__ == "__main__":
    serve()
