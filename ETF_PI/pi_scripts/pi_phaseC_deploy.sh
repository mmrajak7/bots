#!/bin/bash
# =====================================================================================
# Phase C deploy — ONLY after Claude confirms the Phase B NIFTY replay proof PASSED.
#
# BEFORE running: copy the NEW files into a STAGING folder (not over the live files):
#     <ETF_PI>/deploy_multi/   containing:
#       option_buy.py underlyings.py market_calendar.py event_calendar.py get_op_instruments.py
#       trade_observer.py turbulence.py indicators.py digest.py preflight.py MANIFEST.sha256
#       market_holidays.yaml events.yaml events_override.yaml
#   (common.py is NOT part of the deploy — the Pi keeps its own.)
#
# RUN:   bash /home/trustit/Desktop/Scripts/bots/ETF_PI/pi_scripts/pi_phaseC_deploy.sh
#
# Steps (stops at the first failure; nothing live changes before step 4):
#   1. verify the staged files against MANIFEST.sha256 (+ config YAML hashes)
#   2. check python deps incl. bs4/requests (EIA verification)
#   3. remove the Phase A recorder cron line
#   4. back up live files -> backup_pre_multi_<ts>/, copy staged files into place
#   5. get_op_instruments.py -> event_calendar.py build -> preflight.py (must be READY)
#   6. install the cron block (backup first; idempotent)
# Paste the whole output back to Claude.  Rollback command is printed at the end.
# =====================================================================================
set -u
ETF=/home/trustit/Desktop/Scripts/bots/ETF_PI
STG=$ETF/deploy_multi
PY=/usr/bin/python3
TS=$(date +%Y%m%d_%H%M%S)
die() { echo "  [FAIL] $*"; echo "RESULT: STOPPED — nothing further changed. Share this output."; exit 1; }
ok()  { echo "  [ OK ] $*"; }

echo "=== Phase C deploy  $(date '+%Y-%m-%d %H:%M:%S') ==="
H=$(date +%H%M)
if [ "$H" -ge 0845 ] && [ "$H" -lt 2400 ] && [ "$(date +%u)" -le 5 ]; then
    echo "  [WARN] it is $H on a weekday: the live bot / MCX cron may be running. Pre-market (before 08:45) is safer."
    read -r -p "  continue anyway? (yes/no) " a; [ "$a" = "yes" ] || die "aborted by user"
fi

echo "--- 1. staged files"
[ -d "$STG" ] || die "staging folder $STG not found"
CODE="option_buy.py underlyings.py market_calendar.py event_calendar.py get_op_instruments.py trade_observer.py turbulence.py indicators.py digest.py preflight.py"
for f in $CODE MANIFEST.sha256 market_holidays.yaml events.yaml events_override.yaml; do
    [ -f "$STG/$f" ] || die "missing in staging: $f"
done
(cd "$STG" && sha256sum -c MANIFEST.sha256 --quiet) || die "staged code does not match MANIFEST.sha256 — re-copy"
ok "10 code files match MANIFEST.sha256"
grep -q "9d463cc420c3097e2baf19ff6d3dc8e0bd184d513bdf39ed6cd8710ce478f7e7  option_buy.py\|9d463cc420c3097e2baf19ff6d3dc8e0bd184d513bdf39ed6cd8710ce478f7e7 \*option_buy.py" "$STG/MANIFEST.sha256" \
    || die "MANIFEST.sha256 is not the reviewed one (option_buy.py hash differs)"
ok "manifest is the reviewed build (option_buy.py 9d463cc4…)"
for pair in "market_holidays.yaml 396232a174c3e52fffc59bb7ce18dd3a7acd77d2092125526acf2bf4e3eb366a" \
            "events.yaml c0e447a071aff5de5e5a5ebd4d807d0b5315abaaafc4d27a9854e19e741a6c3c" \
            "events_override.yaml be801574901641949dabda3f06698707edc635922b0c99a642c03f5821ce249f"; do
    set -- $pair
    got=$(sha256sum "$STG/$1" | cut -d' ' -f1)
    [ "$got" = "$2" ] && ok "$1" || echo "  [WARN] $1 differs from the shipped copy (fine only if you edited it on purpose)"
done

echo "--- 2. python deps"
for m in pandas numpy pytz yaml kiteconnect requests bs4; do
    $PY -c "import $m" 2>/dev/null && ok "import $m" || die "import $m missing (bs4: sudo apt install python3-bs4; requests: python3-requests)"
done

echo "--- 3. remove the Phase A recorder cron line"
crontab -l > "$ETF/crontab_backup_$TS.txt" 2>/dev/null
ok "crontab backed up to crontab_backup_$TS.txt"
if grep -qF "harness/record_run.py" "$ETF/crontab_backup_$TS.txt"; then
    grep -vF "harness/record_run.py" "$ETF/crontab_backup_$TS.txt" | grep -v "Phase A NIFTY replay-proof recorder" | crontab -
    crontab -l | grep -qF "harness/record_run.py" && die "recorder line still present"
    ok "recorder line removed"
else
    ok "no recorder line present"
fi

echo "--- 4. backup + install"
BK="$ETF/backup_pre_multi_$TS"
mkdir -p "$BK"
for f in $CODE market_holidays.yaml events.yaml events_override.yaml MANIFEST.sha256; do
    [ -f "$ETF/$f" ] && cp -p "$ETF/$f" "$BK/"
done
ok "live files backed up to $BK ($(ls "$BK" | wc -l) files)"
for f in $CODE MANIFEST.sha256; do cp "$STG/$f" "$ETF/$f"; done
for f in market_holidays.yaml events.yaml events_override.yaml; do
    if [ -f "$ETF/$f" ] && ! cmp -s "$ETF/$f" "$STG/$f"; then
        echo "  [WARN] live $f exists and differs — keeping the LIVE one (your edits); staged copy ignored"
    else cp "$STG/$f" "$ETF/$f"; fi
done
(cd "$ETF" && sha256sum -c MANIFEST.sha256 --quiet) || die "live files do not match the manifest after copy"
ok "installed; live code matches MANIFEST.sha256"

echo "--- 5. first run: instruments -> event calendar -> preflight"
$PY "$ETF/get_op_instruments.py" > /tmp/phaseC_instr.txt 2>&1; rc=$?
grep -E "^  [A-Z]" /tmp/phaseC_instr.txt | sed 's/^/    /'
[ $rc -eq 0 ] && ok "get_op_instruments exit 0" || echo "  [WARN] get_op_instruments exit $rc (2 = NFO missing: file kept; 3 = MCX incomplete)"
$PY "$ETF/event_calendar.py" build > /tmp/phaseC_events.txt 2>&1; rc=$?
tail -5 /tmp/phaseC_events.txt | sed 's/^/    /'
[ $rc -eq 0 ] && ok "event_calendar build exit 0" || echo "  [WARN] event_calendar build exit $rc — check logs/event_calendar.log"
$PY "$ETF/preflight.py" > /tmp/phaseC_preflight.txt 2>&1
sed 's/^/    /' /tmp/phaseC_preflight.txt | grep -v "^    $"
grep -q "READY" /tmp/phaseC_preflight.txt && ! grep -q "NOT READY" /tmp/phaseC_preflight.txt \
    || die "preflight is NOT READY — rollback with the command below if needed"
ok "preflight READY"

echo "--- 6. cron block"
B="/home/trustit/Desktop/Scripts/bots/ETF_PI"
NEW=$(cat <<CRON
# === Option magnet multi-instrument (deployed $TS) ===
* 9-15 * * 1-5 sleep 12; /usr/bin/flock -n /tmp/option_buy_BANKNIFTY.lock /usr/bin/python3 $B/option_buy.py --underlying BANKNIFTY > /dev/null 2>&1
* 9-23 * * 1-5 sleep 24; /usr/bin/flock -n /tmp/option_buy_CRUDEOILM.lock /usr/bin/python3 $B/option_buy.py --underlying CRUDEOILM > /dev/null 2>&1
* 9-23 * * 1-5 sleep 32; /usr/bin/flock -n /tmp/option_buy_NATGASMINI.lock /usr/bin/python3 $B/option_buy.py --underlying NATGASMINI > /dev/null 2>&1
* 9-23 * * 1-5 sleep 40; /usr/bin/flock -n /tmp/option_buy_GOLDM.lock /usr/bin/python3 $B/option_buy.py --underlying GOLDM > /dev/null 2>&1
* 9-23 * * 1-5 sleep 48; /usr/bin/flock -n /tmp/option_buy_SILVERM.lock /usr/bin/python3 $B/option_buy.py --underlying SILVERM > /dev/null 2>&1
48 8 * * 1-5 /usr/bin/python3 $B/event_calendar.py build >> $B/logs/event_calendar_cron.log 2>&1
0 17 * * 3,4,5 /usr/bin/python3 $B/event_calendar.py verify >> $B/logs/event_calendar_cron.log 2>&1
0 10 * * 0 /usr/bin/python3 $B/event_calendar.py verify --week >> $B/logs/event_calendar_cron.log 2>&1
56 8 * * 1-5 /usr/bin/python3 $B/preflight.py --send >> $B/logs/cron.log 2>&1
40 15 * * 1-5 /usr/bin/python3 $B/digest.py --send >> $B/logs/cron.log 2>&1
41 15 * * 1-5 /usr/bin/python3 $B/digest.py --underlying BANKNIFTY --send >> $B/logs/cron.log 2>&1
58 23 * * 1-5 for u in CRUDEOILM NATGASMINI GOLDM SILVERM; do /usr/bin/python3 $B/digest.py --underlying \$u --send; done >> $B/logs/cron.log 2>&1
0 6 * * 0 find $B/logs -name "liquidity_probe_*.jsonl" -mtime +45 -delete
# === end option magnet multi-instrument ===
CRON
)
CUR=$(crontab -l 2>/dev/null)
if echo "$CUR" | grep -qF "option_buy.py --underlying CRUDEOILM"; then
    ok "cron block already installed — not added again"
else
    { echo "$CUR"; echo ""; echo "$NEW"; } | crontab -
    crontab -l | grep -qF "option_buy.py --underlying SILVERM" || die "cron block not found after install"
    ok "cron block installed (NIFTY line left exactly as it was)"
fi
crontab -l | grep -n "option_buy\|event_calendar\|digest.py\|preflight.py" | sed 's/^/    /'

echo "RESULT: DEPLOYED $TS"
echo "  Rollback:  cp $BK/* $ETF/  &&  crontab $ETF/crontab_backup_$TS.txt"
echo "  First day: check the 'Verify' table in DEPLOY_multi_instrument_2026-10.md"
