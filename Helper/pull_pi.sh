#!/bin/bash
# pull_pi.sh — copy the Pi's tokens, logs, journals and trade books to this laptop.
#
# READ-ONLY on the Pi: it runs `find`, `tar`, `stat` and read-only status verbs.
# It never pulls code, restarts anything, sends Telegram, or writes on the Pi.
#
# WHAT LANDS WHERE
#   tokens  -> their REAL local paths (BOTS/data, FIFTY/data, CROCODILE/data,
#              ETF_PI/neo/data), replaced only when the Pi's copy is NEWER.
#              All four paths are gitignored; the script refuses to write one
#              that is not.  Token VALUES are never printed.
#   evidence-> Helper/logs/pi_mirror/{helper,etf_pi,btst,momentum}/ (gitignored)
#              NOT over Helper/logs/: the laptop's *_trades.json are Drive-synced
#              store copies, and a file copy over a version-merged store is how
#              the 2026-08-30 reset incident needed restore_snapshot. A mirror is
#              evidence, never a book.
#
# NEVER COPIED: secret.json, neo_credentials.yaml, cred.env, MOMENTUM/secrets/,
#               large regenerable instrument dumps.
#
# Route: alias `pi-ts` (Tailscale, any network) in ~/.ssh/config;
#        PI_HOST=pi for the home-LAN alias.
#
# Usage (Git Bash, from Helper/):
#   bash pull_pi.sh             # tokens + last 1 day of logs + all books/journals
#   bash pull_pi.sh 5           # logs from the last 5 days
#   bash pull_pi.sh 1 --stats   # also print zebra / BTST status AS THE PI SEES IT
#
# SAFE-TO-RERUN: it only extracts copies into pi_mirror/ and replaces a local
# token with a strictly newer one; a second run changes nothing it did not
# already change.

set -euo pipefail

PI_HOST="${PI_HOST:-pi-ts}"
DAYS="${1:-1}"
STATS="${2:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"          # Helper/
BOTS_LOCAL="$(cd "$HERE/.." && pwd)"
MIRROR="$HERE/logs/pi_mirror"
R_BOTS='/home/trustit/Desktop/BOTS'
R_ETF='/home/trustit/Desktop/Scripts/bots/ETF_PI'  # ETF_PI RUNS from here (cron), not BOTS/ETF_PI
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$PI_HOST")

case "$DAYS" in ''|*[!0-9]*) echo "FAIL: days must be a positive integer, got '$DAYS'"; exit 2;; esac
[ "$DAYS" -ge 1 ] || { echo "FAIL: days must be >= 1"; exit 2; }

echo "=== reaching $PI_HOST ==="
"${SSH[@]}" true || { echo "FAIL: cannot reach $PI_HOST (Tailscale up on both? at home try PI_HOST=pi)"; exit 1; }

# The mirror must be gitignored before anything lands in it.
git -C "$BOTS_LOCAL" check-ignore -q "Helper/logs/pi_mirror/x" \
  || { echo "FAIL: Helper/logs/pi_mirror is NOT gitignored - refusing to copy evidence there"; exit 1; }

# pull_tree <remote_dir> <local_subdir> <find-args...>
# The file list is built ON the Pi, so a pattern that matches nothing is simply
# absent instead of a tar error. Paths stay relative to <remote_dir>.
pull_tree() {
  local rdir="$1" sub="$2"; shift 2
  local dest="$MIRROR/$sub"
  if ! "${SSH[@]}" "test -d '$rdir'"; then echo "  [$sub] $rdir not on the Pi - skipped"; return 0; fi
  mkdir -p "$dest"
  local q; q=$(printf "%q " "$@")
  "${SSH[@]}" "cd '$rdir' && find . $q -type f \
       ! -name secret.json ! -name 'neo_credentials*' ! -name 'cred.env' ! -path './secrets/*' -print \
     | tar czf - -T -" | tar xzf - -C "$dest"
  echo "  [$sub] $(find "$dest" -type f | wc -l | tr -d ' ') files -> logs/pi_mirror/$sub"
}

# pull_token <remote_path> <local_path_relative_to_BOTS>
pull_token() {
  local rpath="$1" lrel="$2" lpath="$BOTS_LOCAL/$2"
  local rm; rm=$("${SSH[@]}" "stat -c %Y '$rpath' 2>/dev/null" || true)
  if [ -z "$rm" ]; then echo "  -- $lrel: none on the Pi"; return 0; fi
  if ! git -C "$BOTS_LOCAL" check-ignore -q "$lrel"; then
    echo "  !! $lrel is NOT gitignored - refusing to write a token there"; return 0; fi
  local lm=0; [ -f "$lpath" ] && lm=$(stat -c %Y "$lpath")
  if [ "$rm" -le "$lm" ]; then echo "  == $lrel: local is current"; return 0; fi
  mkdir -p "$(dirname "$lpath")"
  "${SSH[@]}" "cat '$rpath'" > "$lpath.tmp" && [ -s "$lpath.tmp" ] && mv -f "$lpath.tmp" "$lpath"
  touch -d "@$rm" "$lpath"
  # Print only the timestamp field, never a value.
  local gen; gen=$(grep -oE '"generated_at"[^,}]*' "$lpath" | head -1 | sed 's/"generated_at"[: ]*//' || true)
  echo "  <- $lrel: updated (Pi file $(date -d "@$rm" '+%Y-%m-%d %H:%M'))${gen:+, generated_at $gen}"
}

echo "=== tokens (newer-only) ==="
pull_token "$R_BOTS/data/kite_access_token.json"      "data/kite_access_token.json"        # YL6478
pull_token "$R_BOTS/FIFTY/data/kite_access_token.json" "FIFTY/data/kite_access_token.json"  # QSK814
pull_token "$R_BOTS/CROCODILE/data/kite_access_token.json" "CROCODILE/data/kite_access_token.json"
pull_token "$R_ETF/neo/data/session.json"             "ETF_PI/neo/data/session.json"       # Neo (absent while trade_enabled=false)

echo "=== evidence (logs: last $DAYS day(s); books/journals: all) ==="
M="-mtime -$DAYS"
# Cohort / zebra / BCS / FH: cron + monitor logs by age; books, shadows, eod digests in full.
pull_tree "$R_BOTS/Helper/logs" helper -maxdepth 2 \( \
    \( -name 'cron_*.log' $M \) -o \( -name 'spread_monitor_cron_*.log' $M \) -o \( -name 'vet_cli_*.log' $M \) \
    -o -name '*_trades.json' -o -name 'shadow_*.json' -o -path './eod/*' \)
# ETF_PI (live checkout under Scripts/): state + journals in full, logs by age.
pull_tree "$R_ETF" etf_pi -maxdepth 2 \( \
    -name 'st_opt_positions.json' -o -name 'st_opt_state.json' -o \( -name 'st_opt_levels_*.json' $M \) \
    -o -path './logs/*.jsonl' -o -path './logs/*state*.json' -o -path './logs/digest*' \
    -o \( -path './logs/*.log' $M \) \)
# NIFTY BTST: paper, rehearsal and live books + intents + event journals in full; logs by age.
pull_tree "$R_BOTS/Strategies/NIFTY_BTST" btst -maxdepth 2 \( \
    -path './state*/book.json' -o -path './state*/heartbeat.json' -o -path './state*/intents.jsonl' \
    -o -path './state*/fake_broker.json' -o -path './state*/events_*.jsonl' -o -path './state*/*journal*' \
    -o \( -path './logs/*' $M \) \)
# MOMENTUM: not installed on the Pi until the owner runs deploy/setup.sh; picked up once it is.
pull_tree "$R_BOTS/MOMENTUM" momentum -maxdepth 3 \( -path './state/*' -o -path './control/*' -o -path './logs/*' \)

echo "=== Pi git state ==="
echo "  Pi BOTS:       $("${SSH[@]}" "git -C $R_BOTS log --oneline -1")"
echo "  laptop/GitHub: $(git -C "$HERE" log --oneline -1 origin/main 2>/dev/null || echo unknown)"

if [ "$STATS" = "--stats" ]; then
  # Run ON the Pi: a Windows-run status reads the laptop's config, not the live one.
  PY='../CROCODILE/venv/bin/python'
  echo; echo "=== zebra status (cohort, on the Pi) ==="
  "${SSH[@]}" "cd $R_BOTS/Helper && $PY -m zebra status 2>/dev/null"
  echo; echo "=== NIFTY BTST status / live-status (on the Pi) ==="
  "${SSH[@]}" "cd $R_BOTS/Strategies/NIFTY_BTST && ../$PY -m nifty_btst status 2>/dev/null; ../$PY -m nifty_btst live-status 2>/dev/null"
  echo; echo "=== ETF_PI latest digest (written by its 15:40 cron) ==="
  "${SSH[@]}" "ls -t $R_ETF/logs/digest*.md 2>/dev/null | head -1 | xargs -r cat"
  echo; echo "(full 100-trade decision pack: ssh pi-ts 'cd $R_BOTS/Helper && $PY -m zebra golive')"
fi
