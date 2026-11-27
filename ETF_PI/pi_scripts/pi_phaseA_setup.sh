#!/bin/bash
# =====================================================================================
# Phase A setup — NIFTY replay proof recorder (DEPLOY_multi_instrument_2026-10.md)
#
# BEFORE running: copy these two files from the desktop into <ETF_PI>/harness/ on the Pi:
#     harness/record_run.py       (9,031 bytes)
#     harness/harness_common.py   (6,529 bytes)
#
# RUN:   bash /home/trustit/Desktop/Scripts/bots/ETF_PI/pi_scripts/pi_phaseA_setup.sh
#
# What it does (the live bot is NOT touched):
#   1. verifies the two recorder files (sha256) and that the LIVE option_buy.py is the
#      unmodified baseline the proof is made against
#   2. checks python modules the recorder imports
#   3. compiles + imports the recorder (no Kite call, no file written outside harness/)
#   4. backs up the crontab, then adds the ONE recorder line (idempotent: never twice)
#   5. prints a summary — paste the whole output back to Claude
# =====================================================================================
set -u
ETF=/home/trustit/Desktop/Scripts/bots/ETF_PI
PY=/usr/bin/python3
OK=0; FAIL=0
pass() { echo "  [ OK ] $*"; OK=$((OK+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo "=== Phase A setup  $(date '+%Y-%m-%d %H:%M:%S')  host=$(hostname) ==="

echo "--- 1. files"
check_hash() {   # file expected_sha256 label
    if [ ! -f "$1" ]; then fail "$3: $1 missing"; return; fi
    got=$(sha256sum "$1" | cut -d' ' -f1)
    if [ "$got" = "$2" ]; then pass "$3 ($(wc -c < "$1") bytes, sha256 ${got:0:16})"
    else fail "$3 sha256 ${got:0:16} != expected ${2:0:16} — re-copy the file (binary/as-is)"; fi
}
check_hash "$ETF/harness/record_run.py"     c270cecb2ac0dac693dd0349cdcc57a18ab4f3bcd865950c1e2d4ce4e6b06cae "record_run.py"
check_hash "$ETF/harness/harness_common.py" 9cb0624180a296b46127e5ad717f017d1943e055f117a8134a3922804e36574b "harness_common.py"
check_hash "$ETF/option_buy.py"             2b897a4cb1f9ced43c9473b79ea4d0005d8880b965034326371c6c443db194cc "LIVE option_buy.py is the baseline"

echo "--- 2. python modules"
for m in pandas numpy pytz yaml kiteconnect; do
    if $PY -c "import $m" 2>/dev/null; then pass "import $m"; else fail "import $m (sudo apt install python3-$m)"; fi
done
if [ -f /home/trustit/Desktop/BOTS/data/kite_access_token.json ]; then pass "Kite token file present"
else fail "Kite token file missing: /home/trustit/Desktop/BOTS/data/kite_access_token.json"; fi

echo "--- 3. compile + import (no Kite call)"
if $PY -m py_compile "$ETF/harness/record_run.py" "$ETF/harness/harness_common.py" 2>/tmp/phaseA_compile.txt; then
    pass "py_compile"
else fail "py_compile: $(cat /tmp/phaseA_compile.txt)"; fi
if (cd "$ETF/harness" && $PY -c "import harness_common, record_run; print('imports ok')" >/tmp/phaseA_import.txt 2>&1); then
    pass "import harness_common, record_run"
else fail "import: $(tail -3 /tmp/phaseA_import.txt)"; fi

echo "--- 4. cron"
LINE='* 9-15 * * 1-5 sleep 25; /usr/bin/flock -n /tmp/ob_record.lock /usr/bin/python3 /home/trustit/Desktop/Scripts/bots/ETF_PI/harness/record_run.py >> /home/trustit/Desktop/Scripts/bots/ETF_PI/harness/record.log 2>&1'
if [ "$FAIL" -ne 0 ]; then
    echo "  [SKIP] cron NOT changed because of the failures above — fix them and re-run"
else
    BK="$ETF/harness/crontab_backup_$(date +%Y%m%d_%H%M%S).txt"
    crontab -l > "$BK" 2>/dev/null
    pass "crontab backed up to $BK ($(wc -l < "$BK") lines)"
    if grep -qF "harness/record_run.py" "$BK"; then
        pass "recorder line already present — not added again"
    else
        { cat "$BK"; echo ""; echo "# Phase A NIFTY replay-proof recorder (remove BEFORE Phase C deploy)"; echo "$LINE"; } | crontab -
        if crontab -l | grep -qF "harness/record_run.py"; then pass "recorder cron line added"
        else fail "cron line not found after install — restore with: crontab $BK"; fi
    fi
fi

echo "--- summary"
echo "  checks ok=$OK failed=$FAIL"
crontab -l | grep -n "option_buy\|record_run" | sed 's/^/  cron: /'
if [ "$FAIL" -eq 0 ]; then
    echo "  RESULT: PHASE A READY — the recorder starts at the next cron minute (09:00-15:59, Mon-Fri)."
    echo "  Tomorrow after 09:20 run:  tail -3 $ETF/harness/record.log   (expect '...... ok calls=N')"
    echo "  After 15:30 each day run:   bash $ETF/pi_scripts/pi_phaseA_daycheck.sh"
else
    echo "  RESULT: NOT READY — share this output with Claude."
fi
