# Commands

Everything you need to type. The project now lives at **`~/crypto`**
(`~/Desktop/Crypto` is a symlink to it, so either path works).

```bash
cd ~/crypto
```

Every command below assumes you have run that first.

---

## 1. API key — DONE ✓

Installed at `~/.config/memescan/fomo_key`, verified against the live API.
**Nothing to do.**

To replace it later:

```bash
bash setup.sh
```

It prompts without echoing, saves, then proves the key works by pulling three
live leaderboard rows. `SETUP COMPLETE` means it worked.

> The original one-liner *did* work — it just printed nothing on success,
> which looks identical to failure. `setup.sh` always says what happened.

---

## 2. The collector — RUNS AUTOMATICALLY ✓

Installed as a launchd agent (`com.ron.echo`). It starts at login and
restarts itself if it crashes. **You do not need to start it.**

```bash
# IS IT RUNNING?   (PID in col 1, exit status in col 2 — status 0 is healthy)
launchctl list | awk 'NR==1 || /com.ron.echo/'

# WATCH IT LIVE    (ctrl-C stops watching, not the collector)
tail -f echo/logs/collector.log

# RESTART          (after editing code)
launchctl kickstart -k gui/$(id -u)/com.ron.echo

# STOP PERMANENTLY
launchctl unload ~/Library/LaunchAgents/com.ron.echo.plist

# START AGAIN
launchctl load ~/Library/LaunchAgents/com.ron.echo.plist
```

A healthy line looks like:

```
[16:11:02] seen= 91 new= 2 echo= 0 tracked=  12/  30 hot=93
```

`seen` = launches in window · `new` = recorded · `echo` = ticker matches ·
`tracked` = tokens priced · `hot` = watchlist size.

**`seen= 0` is a problem, not a quiet chain** — this chain launches ~20k pools
a day. Those cycles are logged as `empty` and counted as gaps.

---

## 3. The dashboard

```bash
python3 dash.py
```

Then open **http://localhost:8799**. It reads the live database and refreshes
every 30 seconds — leave it open while the collector runs.

If 8799 is busy it walks up to the next free port and prints the one it got.
Force a specific one with `ECHO_DASH_PORT=9100 python3 dash.py`.

Nothing is published; it binds to `127.0.0.1` only and no data leaves the Mac.

Stop it with ctrl-C, or `pkill -f dash.py` if you started it detached.

---

## 4. The backtest (terminal version)

```bash
python3 -c "import echo.analyze as a; a.report()"
```

Refuses a verdict below 20 per cohort. Below ~48h of collection it is a smoke
test, not evidence.

---

## 5. Health check

```bash
python3 - <<'PY'
import sys, time; sys.path.insert(0,'.')
from echo import db
c = db.connect(); q = lambda s: c.execute(s).fetchone()[0]
print("launches    ", q("select count(*) from rh_launch"))
print("  echo      ", q("select count(*) from rh_launch where cohort='echo'"))
print("  control   ", q("select count(*) from rh_launch where cohort='control'"))
print("observations", q("select count(*) from obs"))
print("cycles ok   ", q("select count(*) from cycle_log where kind='ok'"))
print("cycles bad  ", q("select count(*) from cycle_log where kind='error'"))
print("cycles empty", q("select count(*) from cycle_log where kind='empty'"))
print("last cycle  ", "%.0fs ago" % (time.time() - q("select max(ts) from cycle_log")))
PY
```

`bad` or `empty` above zero, or `last cycle` over ~300s, means gaps — tell Claude.

---

## 6. Why the project moved out of Desktop

macOS TCC blocks launchd agents from **reading** files in `~/Desktop`
(creating them works, which makes the failure confusing — it shows up only as
launchd exit code 78). Your Terminal has the grant; background agents cannot
get it and cannot prompt for it.

Moving to `~/crypto` sidesteps it entirely — no System Settings trip, and
nothing to re-fix when Homebrew upgrades Python. The Desktop symlink means
old paths still work.

Sleep is separate from this: launchd restarts the collector on wake, but for
a fully uninterrupted run either disable App Nap sleep on power adapter, or
run `caffeinate -s` in a spare terminal.

---

## 7. API credit budget

Free tier: **250,000 credits/month ≈ 1,000 calls.** Used so far: 1.

| call | credits |
|---|---|
| leaderboard (any limit — always ask for the full 150) | 250 |
| swaps / trades / balances / token stats / devs | 250 |
| thesis, per page | 1,250 |
| handle → wallet resolution | 2,500 |

The leaderboard already carries `wallets.evm` per row, so the 2,500-credit
resolution call is usually avoidable. Out of credits returns HTTP 402; adding
a payment method grants 500,000 more, free.

---

## 8. If something looks wrong

```bash
tail -30 echo/logs/collector.log     # recent cycles
tail -30 echo/logs/collector.err     # crashes
launchctl list | grep com.ron.echo   # col 2 is last exit status
ls -la ~/.config/memescan/fomo_key   # should be -rw------- and non-empty
```

Then tell Claude what it printed.
