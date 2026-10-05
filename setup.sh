#!/bin/bash
# Stores an API key and proves it works.
#
#   bash setup.sh          # set up whatever is missing
#   bash setup.sh jev      # just the TypeSafe/Jev key
#   bash setup.sh fomo     # just the FOMO key
#
# Keys are read without echoing and never touch your shell history.

set -uo pipefail
KEY_DIR="$HOME/.config/memescan"
bold(){ printf '\033[1m%s\033[0m\n' "$1"; }
ok(){   printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad(){  printf '  \033[31m✗\033[0m %s\n' "$1"; }

read_key() {  # $1 = label, $2 = where to get one
  # Prompts MUST go to stderr: this function's stdout is captured by $(...),
  # so anything echoed here would be concatenated onto the key itself.
  echo "Paste your $1 key and press Enter." >&2
  echo "  (get one at $2 — nothing will appear as you paste)" >&2
  printf "key: " >&2
  read -rs RAW; echo >&2
  # Strip all whitespace: a trailing newline from copy-paste is the single most
  # common reason a key "does not work".
  printf '%s' "$RAW" | tr -d '[:space:]'
}

save_key() {  # $1 = filename, $2 = key
  # Guard against the class of bug that wrote prompt text into the key file:
  # a real key has no whitespace and is not absurdly long.
  case "$2" in *[[:space:]]*) bad "key contains whitespace — not saving"; return 1 ;; esac
  if [ "${#2}" -lt 8 ] || [ "${#2}" -gt 400 ]; then
    bad "key length ${#2} looks wrong — not saving"; return 1
  fi
  mkdir -p "$KEY_DIR" || { bad "cannot create $KEY_DIR"; return 1; }
  printf '%s' "$2" > "$KEY_DIR/$1" || { bad "cannot write $KEY_DIR/$1"; return 1; }
  chmod 600 "$KEY_DIR/$1"
  ok "saved to $KEY_DIR/$1 ($(wc -c < "$KEY_DIR/$1" | tr -d ' ') chars, mode $(stat -f '%Lp' "$KEY_DIR/$1"))"
}

setup_fomo() {
  echo; bold "FOMO API key"
  if [ -s "$KEY_DIR/fomo_key" ]; then
    printf "  A key already exists. Replace it? [y/N] "; read -r a
    [ "${a:-n}" != "y" ] && [ "${a:-n}" != "Y" ] && { ok "keeping existing key"; K=$(cat "$KEY_DIR/fomo_key"); }
  fi
  [ -z "${K:-}" ] && K=$(read_key "FOMO" "https://fomoapi.io/dashboard")
  [ -z "$K" ] && { bad "no key entered"; return 1; }
  save_key fomo_key "$K" || return 1
  B=$(mktemp)
  C=$(curl -s -o "$B" -w '%{http_code}' --max-time 20 -H "authorization: Bearer $K" \
       "https://api.fomoapi.io/v2/leaderboard/24h?limit=3")
  case "$C" in
    200) ok "HTTP 200 — key accepted"
         python3 -c "
import json,sys
d=json.load(open('$B')); rows=d.get('traders') or []
for r in rows[:3]:
    print(f\"    #{r.get('rank','?'):<3} {str(r.get('handle'))[:18]:<18} pnl=\${float(r.get('pnlUsd') or 0):>12,.0f}\")" ;;
    401) bad "HTTP 401 — key rejected. Check you copied all of it." ;;
    402) bad "HTTP 402 — valid key, but out of credits." ;;
    *)   bad "HTTP $C"; head -c 200 "$B"; echo ;;
  esac
  rm -f "$B"; unset K
}

setup_jev() {
  echo; bold "TypeSafe / Jev API key"
  if [ -s "$KEY_DIR/jev_key" ]; then
    printf "  A key already exists. Replace it? [y/N] "; read -r a
    [ "${a:-n}" != "y" ] && [ "${a:-n}" != "Y" ] && { ok "keeping existing key"; K=$(cat "$KEY_DIR/jev_key"); }
  fi
  [ -z "${K:-}" ] && K=$(read_key "TypeSafe/Jev" "https://typesafe.ai")
  [ -z "$K" ] && { bad "no key entered"; return 1; }
  save_key jev_key "$K" || return 1

  # Verify with the question we actually need answered: is an RH-chain launch a
  # deliberate clone of a token running elsewhere? Proves the key AND the use case.
  B=$(mktemp)
  C=$(curl -s -o "$B" -w '%{http_code}' --max-time 30 \
       -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
       -X POST https://api.typesafe.ai/v1/systemone \
       -d '{"model":"jev-latest",
            "state":{"new_token":{"symbol":"JEANPHIL","name":"Jean Phil","chain":"robinhood"},
                     "running_elsewhere":{"symbol":"JEANPHIL","name":"Jean Phil","chain":"solana","mcap_usd":11580000,"change_24h_pct":2600}},
            "questions":{
              "is_clone":{"type":"noul",
                "instructions":"Is `new_token` a deliberate copy of `running_elsewhere`, launched to ride its attention?",
                "criteria":{"true":"Same meme/brand, launched after the other started running","false":"Unrelated token that merely shares a ticker"}}}}')
  case "$C" in
    200) ok "HTTP 200 — key accepted"
         python3 -c "
import json
d=json.load(open('$B'))
a=(d.get('answers') or {}).get('is_clone') or {}
print(f\"    model={d.get('model')}  is_clone probability = {a.get('noul')}\")
print(f\"    usage={d.get('usage')}\")" ;;
    401|403) bad "HTTP $C — key rejected. Check you copied all of it." ;;
    *)   bad "HTTP $C"; head -c 300 "$B"; echo ;;
  esac
  rm -f "$B"; unset K
}

case "${1:-all}" in
  jev)  setup_jev ;;
  fomo) setup_fomo ;;
  all)  setup_fomo; setup_jev ;;
  *)    echo "usage: bash setup.sh [jev|fomo]"; exit 1 ;;
esac
echo; bold "Done"; echo
