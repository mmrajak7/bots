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

# SAFE-TO-RERUN: a second run re-installs the same two crontab lines (each
# block now REPLACES rather than appends, so the crontab converges on exactly
# one of each), re-kills processes that are already dead, re-deletes lock files
# whose absence is the desired state, and moves only archive files that still
# exist. The one step that can destroy anything -- `zebra reset --confirm` --
# is opt-in behind `--reset` AND refuses a book with anything in flight AND
# refuses a book it could not read.
#
# That sentence is the rule from CLAUDE.md, and this script is why the rule
# exists: on 2026-08-30 it force-closed six open cohort positions at -100% on a
# re-run, under a header that claimed "Idempotent: re-running is safe". Prose
# is not a guard -- the guards are the flag, the in-flight refusal and the
# unreadable-book refusal above, and this line only records that the question
# was actually asked.

set -euo pipefail

BOTS_DIR=/home/trustit/Desktop/BOTS
HELPER_DIR="$BOTS_DIR/Helper"
VENV="$BOTS_DIR/CROCODILE/venv/bin/python"
# DATE-STAMPED, and the % MUST be escaped as \%.
#
# Two separate defects lived in these two lines until 2026-08-31:
#
#   1. No date stamp. `cron_bcs.log` reached 12.5 MB as one ever-growing file,
#      which is how logs stop being moved off the box at all -- and worse here,
#      `zebra/digest.py` reads ONLY `cron_zebra_<YYYYMMDD>.log`, so on a box
#      deployed by this script every digest reported zero cycles, no gaps and
#      no vet events. A broken day rendered as a quiet one.
#
#   2. It disagreed with `pi_setup.sh`, whose CRON_LINE has been dated and
#      escaped all along. Both scripts dedup on a substring of the command, so
#      whichever ran LAST silently decided which line the box kept.
#
# cron reads a bare % as a newline in the command field, so an unescaped
# $(date +%Y%m%d) truncates the entry and the redirect vanishes entirely.
ZEBRA_CRON='*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && flock -n /tmp/zebra_monitor.lock ../CROCODILE/venv/bin/python -m zebra run >> logs/cron_zebra_$(date +\%Y\%m\%d).log 2>&1'
ZEBRA_REPORT_CRON='30 15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && ../CROCODILE/venv/bin/python -m zebra report --type auto --telegram >> logs/cron_zebra_report_$(date +\%Y\%m\%d).log 2>&1'

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
    # THE LISTING MUST SUCCEED BEFORE ITS COUNT MEANS ANYTHING.
    #
    # This read `zebra list 2>/dev/null | grep -c ... || true`, which turns
    # EVERY failure of the listing -- Drive auth, a lock timeout, an import
    # error, a quarantined book -- into empty stdout, a count of 0, and a
    # cheerful walk into `zebra reset --confirm`. "I could not read the book"
    # was rendered as "the book is empty", inside the guard that exists
    # BECAUSE this script once force-closed six open positions at -100%.
    set +e
    LIST_OUT=$("$VENV" -m zebra list 2>&1)
    LIST_RC=$?
    set -e
    if [ "$LIST_RC" -ne 0 ]; then
        echo "  REFUSED: could not read the book ('zebra list' exited $LIST_RC)."
        echo "  A reset force-closes every open position at -100%, so it runs"
        echo "  only against a book this script has actually READ and found"
        echo "  empty. Fix the listing first:"
        echo "$LIST_OUT" | tail -n 15 | sed 's/^/      /'
        exit 1
    fi
    IN_FLIGHT=$(echo "$LIST_OUT" | grep -cwE 'watching|triggered|entered' || true)
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

# REPLACE, do not "add if missing".
#
# These two blocks used to skip whenever a matching line already existed, which
# made the script unable to ever CORRECT one. That mattered the moment the two
# deploy scripts disagreed: this one installed an undated `cron_zebra.log` while
# `pi_setup.sh` installed the dated, %-escaped line the digest actually reads,
# and "already present" meant whichever ran first won permanently. A box
# deployed before 2026-08-31 keeps a log the digest cannot read, and re-running
# the fixed script would have changed nothing.
#
# Dropping every existing line first also cleans up a commented-out or
# hand-edited duplicate, so the crontab converges on exactly one of each.
replace_cron_line() {
    local pattern="$1" desired="$2" label="$3"
    if grep -qF "$pattern" "$TMP_CRON"; then
        if grep -qxF "$desired" "$TMP_CRON"; then
            echo "  $label already correct"
            return
        fi
        echo "  $label present but DIFFERENT — replacing it"
    else
        echo "  $label added"
    fi
    grep -vF "$pattern" "$TMP_CRON" > "$TMP_CRON.new" || true
    mv "$TMP_CRON.new" "$TMP_CRON"
    echo "$desired" >> "$TMP_CRON"
}

replace_cron_line "python -m zebra run"    "$ZEBRA_CRON"        "zebra monitor cron"
replace_cron_line "python -m zebra report" "$ZEBRA_REPORT_CRON" "zebra report cron"

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
