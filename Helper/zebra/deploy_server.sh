#!/bin/bash
# Zebra DEPLOY — step 2 of the deployment.
#
# Prerequisite (step 1): user has already run
#   bash Helper/zebra/pull.sh
# which does git pull + downloads zebra_config.json + .md docs from Drive.
#
# What this script does (no fetching — that's pull.sh):
#   1. Sanity checks
#   2. Verify zebra package imports + status
#   3. Reset in-flight signals — ONLY with --reset, and only on an EMPTY book
#   4. Update crontab (add monitor + report lines, comment old)
#   5. Stop old monitors + clear lock files
#   6. Archive deprecated logs + stores (idempotent — only moves files that exist)
#   7. Summary + verify hint

set -euo pipefail

BOTS_DIR=/home/trustit/Desktop/BOTS
HELPER_DIR="$BOTS_DIR/Helper"
VENV="$BOTS_DIR/CROCODILE/venv/bin/python"
ZEBRA_CRON='*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && flock -n /tmp/zebra_monitor.lock ../CROCODILE/venv/bin/python -m zebra run >> logs/cron_zebra.log 2>&1'
ZEBRA_REPORT_CRON='30 15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && ../CROCODILE/venv/bin/python -m zebra report --type auto --telegram >> logs/cron_zebra_report.log 2>&1'

step() { echo; echo "=== $1 ==="; }

# ── 1. Sanity checks ─────────────────────────────────────────────────────
step "1. Sanity checks"
[ -d "$HELPER_DIR" ] || { echo "FAIL: $HELPER_DIR not found"; exit 1; }
[ -x "$VENV" ]       || { echo "FAIL: venv python not found at $VENV"; exit 1; }
[ -f "$BOTS_DIR/data/secret.json" ] || { echo "FAIL: Drive credentials missing"; exit 1; }
[ -f "$HELPER_DIR/config/zebra_config.json" ] || \
    { echo "FAIL: zebra_config.json missing — run pull.sh first"; exit 1; }
[ -f "$BOTS_DIR/data/kite_access_token.json" ] || { echo "WARN: Kite token missing"; }
echo "  paths OK"

# ── 2. Verify zebra package ──────────────────────────────────────────────
step "2. Verify zebra"
cd "$HELPER_DIR"
"$VENV" -c "from zebra import config, scanner, strikes, monitor, trade_store, report; print('  imports OK')"
"$VENV" -m zebra status

# ── 3. Reset in-flight signals — OPT-IN, NEVER ON A LIVE BOOK ────────────
#
# This was an unguarded `zebra reset --confirm` until 2026-08-30, described in
# the header as "one-time hygiene". It is not one-time in any enforced sense:
# re-running this script force-closed SIX open cohort positions at -100% under
# `reset_force_close` and cancelled three signals. Paper records, so no money —
# and three weeks of the evidence the arming gate is waiting on.
#
# A deploy script must be safe to re-run. That is the entire contract of one.
# So the reset is now opt-in AND refuses a book that has anything in flight:
# the operator has to ask for it twice, once with a flag and once by having an
# empty book.
step "3. Reset in-flight signals (skipped unless --reset)"
if [ "${1:-}" = "--reset" ]; then
    IN_FLIGHT=$("$VENV" -m zebra list 2>/dev/null \
        | grep -cE '\b(watching|triggered|entered)\b' || true)
    if [ "${IN_FLIGHT:-0}" -gt 0 ]; then
        echo "  REFUSED: $IN_FLIGHT signal(s) are in flight."
        echo "  A reset force-closes every one of them at -100%. If that is"
        echo "  really what you want, close them deliberately first:"
        echo "    $VENV -m zebra close <ID> --exit-debit X --reason ..."
        echo "  Recover a book reset by accident with:"
        echo "    $VENV -m zebra.restore_snapshot logs/archive/<date>/<snap>.json"
        exit 1
    fi
    "$VENV" -m zebra reset --confirm || true
else
    echo "  skipped — pass --reset to run it (it force-closes the whole book)"
fi

# ── 4. Update crontab ────────────────────────────────────────────────────
step "4. Update crontab"
TMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TMP_CRON" || true

# Comment out old systems (idempotent: only commented if not already)
sed -i -E 's|^([^#]*python -m playbook\.magnet)|# (zebra) \1|' "$TMP_CRON"
sed -i -E 's|^([^#]*python -m flow run)|# (zebra) \1|' "$TMP_CRON"

# Add zebra monitor cron if missing
if grep -qF "python -m zebra run" "$TMP_CRON"; then
    echo "  zebra monitor cron already present"
else
    echo "$ZEBRA_CRON" >> "$TMP_CRON"
    echo "  zebra monitor cron added"
fi

# Add zebra EOD report cron if missing
if grep -qF "python -m zebra report" "$TMP_CRON"; then
    echo "  zebra report cron already present"
else
    echo "$ZEBRA_REPORT_CRON" >> "$TMP_CRON"
    echo "  zebra report cron added (15:30 Mon-Fri, auto daily/weekly)"
fi

crontab "$TMP_CRON"
rm "$TMP_CRON"

# ── 5. Stop old monitors ─────────────────────────────────────────────────
step "5. Stop old monitors"
if pkill -f "playbook.magnet" 2>/dev/null; then
    echo "  killed magnet/strack processes"
else
    echo "  no magnet/strack processes running"
fi
if pkill -f "python -m flow" 2>/dev/null; then
    echo "  killed flow processes"
else
    echo "  no flow processes running"
fi
rm -f /tmp/magnet_monitor.lock /tmp/strack.lock /tmp/flow_monitor.lock 2>/dev/null || true
echo "  cleared lock files"

# ── 6. Archive old logs + stores ─────────────────────────────────────────
step "6. Archive deprecated logs + stores"
ARCHIVE_DIR="$HELPER_DIR/logs/archive/2026-05-11"
mkdir -p "$ARCHIVE_DIR"
moved=0
for f in \
    magnet_trades.json \
    confidence_tracker.json \
    spot_tracker.json \
    flow_trades.json \
    cron_magnet.log \
    cron_flow.log \
    strack.log \
    magnet_dashboard.html \
    magnet_alert_analysis.csv \
    magnet_alert_analysis_v2.csv \
    magnet_entry_levels_sim.csv \
    magnet_wider_entry_sim.csv \
; do
    src="$HELPER_DIR/logs/$f"
    if [ -e "$src" ]; then
        mv "$src" "$ARCHIVE_DIR/$f"
        moved=$((moved + 1))
    fi
done
shopt -s nullglob
for f in "$HELPER_DIR/logs/magnet_"*.log; do
    mv "$f" "$ARCHIVE_DIR/" && moved=$((moved + 1))
done
shopt -u nullglob
echo "  moved $moved file(s) to $ARCHIVE_DIR"

# ── 7. Summary ───────────────────────────────────────────────────────────
step "7. Summary"
echo "  crontab — zebra/magnet/flow lines:"
crontab -l | grep -E "zebra|magnet|flow" | sed 's/^/    /' || echo "    (none)"
echo
echo "  Next 5-min market-hours tick will start zebra. Tail the log:"
echo "    tail -f $HELPER_DIR/logs/cron_zebra.log"
echo
echo "  Step 3 — verify:"
echo "    crontab -l | grep zebra"
echo "    $VENV -m zebra status"
echo "    pgrep -f 'zebra run' && echo RUNNING || echo NOT RUNNING"
echo
echo "Done."
