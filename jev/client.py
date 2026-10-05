"""TypeSafe / Jev client: disk-cached, batched.

Jev answers several typed questions in one pass and bills per input token, so
the cheap pattern is one call per subject carrying every question at once --
batching is documented at ~12x cheaper than asking them separately. Responses
are cached to disk keyed by the full request, so re-running an evaluation
costs nothing and results stay reproducible while we iterate on wording.
"""
import hashlib
import json
import pathlib
import urllib.error
import urllib.request

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
KEY_FILE = pathlib.Path.home() / ".config" / "memescan" / "jev_key"
CACHE = pathlib.Path(__file__).parent / "cache"
MODEL = "jev-latest"


def _key():
    if not KEY_FILE.exists():
        raise SystemExit(f"no Jev key at {KEY_FILE} -- run: bash ~/crypto/setup.sh jev")
    k = KEY_FILE.read_text().strip()
    if not k or any(c.isspace() for c in k):
        raise SystemExit(f"{KEY_FILE} looks malformed -- run: bash ~/crypto/setup.sh jev")
    return k


def ask(state, questions, model=MODEL, refresh=False):
    """One Jev call. Returns {question_id: answer} or {"__error__": ...}."""
    payload = {"model": model, "state": state, "questions": questions}
    body = json.dumps(payload, sort_keys=True).encode()
    cp = CACHE / (hashlib.sha256(body).hexdigest()[:24] + ".json")
    if cp.exists() and not refresh:
        return json.loads(cp.read_text())

    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        return {"__error__": f"http {e.code}", "body": e.read().decode()[:200]}
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}
    CACHE.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(out))
    return out


def noul(ans, qid):
    a = (ans.get("answers") or {}).get(qid) or {}
    return a.get("noul")
