"""FOMO API client: disk-cached, credit-accounted.

Two properties matter more than speed here. Every response is cached to disk
keyed by its full request, so re-running an analysis costs nothing -- the free
tier is 1,000 calls a month and an exploratory script can burn it in one
careless loop. And every call is logged with its credit cost, so the spend is
visible rather than discovered at HTTP 402.
"""
import hashlib
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.fomoapi.io"
KEY_FILE = pathlib.Path.home() / ".config" / "memescan" / "fomo_key"
CACHE = pathlib.Path(__file__).parent / "cache"
LEDGER = pathlib.Path(__file__).parent / "credits.jsonl"

# From GET /v1 -> pricing.creditCosts
COSTS = {
    "leaderboard": 250, "alerts_feed": 125, "normal": 250,
    "thesis_per_page": 1250, "wallet_resolution": 2500,
}


def _key():
    if not KEY_FILE.exists():
        raise SystemExit(f"No API key at {KEY_FILE} -- run: bash ~/crypto/setup.sh")
    k = KEY_FILE.read_text().strip()
    if not k:
        raise SystemExit(f"{KEY_FILE} is empty -- run: bash ~/crypto/setup.sh")
    return k


def _cache_path(path, params):
    raw = path + "?" + urllib.parse.urlencode(sorted((params or {}).items()))
    return CACHE / (hashlib.sha256(raw.encode()).hexdigest()[:24] + ".json")


def _log(path, cost, cached):
    with LEDGER.open("a") as f:
        f.write(json.dumps({"ts": int(time.time()), "path": path,
                            "credits": 0 if cached else cost, "cached": cached}) + "\n")


def get(path, params=None, cost_key="normal", refresh=False, quiet=False):
    """GET a FOMO endpoint. Returns parsed JSON, or {"__error__": ...}.

    Errors are returned rather than raised so a 60-trader sweep is not killed
    by one unresolvable handle -- a partial cohort is still analysable, and
    which traders failed is itself information.
    """
    cp = _cache_path(path, params)
    if cp.exists() and not refresh:
        _log(path, 0, True)
        return json.loads(cp.read_text())

    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "authorization": f"Bearer {_key()}",
        "accept": "application/json",
        "user-agent": "cohort/0.1",
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            CACHE.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(data))
            _log(path, COSTS.get(cost_key, 250), False)
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if e.code in (402, 401):        # out of credits / bad key: fatal
                raise SystemExit(f"HTTP {e.code} on {path}: {body}")
            if e.code == 409 and attempt < 2:   # handle still resolving
                time.sleep(3); continue
            if e.code == 429 and attempt < 2:
                time.sleep(10); continue
            # 503 here is FOMO's upstream timing out, and it says so with
            # retryable:true. Treating it as an empty history would silently
            # record an active trader as having made no trades.
            if e.code == 503 and attempt < 2:
                time.sleep(6 * (attempt + 1)); continue
            if not quiet:
                print(f"    ! HTTP {e.code} {path}: {body[:90]}")
            return {"__error__": f"http {e.code}", "body": body}
        except Exception as e:
            if attempt < 2:
                time.sleep(3); continue
            return {"__error__": f"{type(e).__name__}: {e}"}
    return {"__error__": "exhausted retries"}


def spend():
    """(credits_spent, calls_made, cache_hits) so far."""
    if not LEDGER.exists():
        return 0, 0, 0
    rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]
    live = [r for r in rows if not r["cached"]]
    return sum(r["credits"] for r in live), len(live), len(rows) - len(live)
