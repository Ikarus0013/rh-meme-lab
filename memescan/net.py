"""HTTP + JSON-RPC plumbing: per-host rate limiting, retries, backoff.

Stdlib only, so the tool runs with no pip install.
"""
import json
import time
import urllib.error
import urllib.request
from collections import defaultdict

RPC_MIN_INTERVAL = 0.60

_last_call = defaultdict(float)

UA = "memescan/0.1 (+screener)"


def _throttle(key, min_interval):
    """Block until at least `min_interval` has passed since the last call."""
    wait = min_interval - (time.monotonic() - _last_call[key])
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.monotonic()


def get_json(url, key, min_interval, tries=4, timeout=30):
    """GET JSON with throttling and exponential backoff.

    Returns None rather than raising: a screener should degrade to partial data
    instead of dying because one token's endpoint 404s.
    """
    for attempt in range(tries):
        _throttle(key, min_interval)
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            # 429 / 5xx -> back off and retry
            if attempt == tries - 1:
                return None
            time.sleep((2 ** attempt) * 1.5)
        except Exception:
            if attempt == tries - 1:
                return None
            time.sleep((2 ** attempt) * 1.0)
    return None


def rpc(method, params, url, tries=4, timeout=60):
    """Single JSON-RPC call.

    Returns the `result` field on success, or {"__error__": msg} on failure.
    Failures are reported rather than swallowed as None, because callers need
    to tell an HTTP 429 (slow down, same request will work) apart from a
    server-side query timeout (the request itself is too big) -- those demand
    opposite responses.
    """
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()

    for attempt in range(tries):
        _throttle("rpc", RPC_MIN_INTERVAL)
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.load(resp)
        except urllib.error.HTTPError as e:
            # 429 and 5xx both surface here, never as a JSON-RPC error object.
            msg = f"http {e.code}"
            if e.code == 429:
                msg = "http 429 too many requests"
            if attempt == tries - 1:
                return {"__error__": msg}
            time.sleep((2 ** attempt) * 2.0)
            continue
        except Exception as e:
            if attempt == tries - 1:
                return {"__error__": f"transport: {type(e).__name__}"}
            time.sleep((2 ** attempt) * 1.5)
            continue

        if "error" in out:
            msg = str(out["error"])
            if attempt == tries - 1:
                return {"__error__": msg}
            time.sleep((2 ** attempt) * 2.0)
            continue
        return out.get("result")

    return {"__error__": "exhausted retries"}
