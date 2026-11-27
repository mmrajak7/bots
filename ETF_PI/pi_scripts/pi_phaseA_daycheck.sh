#!/bin/bash
# =====================================================================================
# Phase A day check — run after 15:30 on each recording day (read-only, except the archive).
#
# RUN:   bash /home/trustit/Desktop/Scripts/bots/ETF_PI/pi_scripts/pi_phaseA_daycheck.sh [YYYY-MM-DD]
#
# Prints the day's recording summary (paste it back to Claude) and packs
#   harness/recordings/<day>  ->  harness/recording_<day>.tar.gz
# Copy that .tar.gz to the desktop working copy:  ETF_PI\harness\  (Claude unpacks it).
#
# Acceptance across the recorded days (review-2 D2): >=1 entry, >=1 ladder exit, >=1 ATM shift,
# the EOD pass. The counts below come from the RECORDER's own sandbox log (the baseline bot
# running in harness/sandbox_rec), not from the live bot.
# =====================================================================================
set -u
ETF=/home/trustit/Desktop/Scripts/bots/ETF_PI
DAY=${1:-$(date +%Y-%m-%d)}
REC="$ETF/harness/recordings/$DAY"
SLOG="$ETF/harness/sandbox_rec/logs/option_buy_$DAY.log"

echo "=== Phase A day check  $DAY  (run $(date '+%Y-%m-%d %H:%M')) ==="
if [ ! -d "$REC" ]; then
    echo "  [FAIL] no recording for $DAY at $REC"
    echo "  last record.log lines:"; tail -5 "$ETF/harness/record.log" 2>/dev/null | sed 's/^/    /'
    exit 1
fi

runs=$(ls "$REC/runs" 2>/dev/null | wc -l)
first=$(ls "$REC/runs" 2>/dev/null | head -1 | cut -c1-6)
last=$(ls "$REC/runs" 2>/dev/null | tail -1 | cut -c1-6)
echo "--- recorder"
echo "  minutes recorded : $runs   (first $first, last $last; a full day is ~375)"
echo "  recorder errors  : $(grep -c 'recorder error' "$ETF/harness/record.log" 2>/dev/null)"
echo "  refusals         : $(grep -c 'REFUSING' "$ETF/harness/record.log" 2>/dev/null)"
grep -h "stale_open_at_start" "$REC/day_meta.json" 2>/dev/null | sed 's/^/  day_meta: /'
grep -h "WARNING: recording day starts" "$ETF/harness/record.log" 2>/dev/null | tail -1 | sed 's/^/  /'

echo "--- what the recorded bot did (acceptance)"
if [ -f "$SLOG" ]; then
    echo "  entries (Opened:)      : $(grep -c 'Opened:' "$SLOG")"
    echo "  ladder exits           : $(grep -c 'FORENSICS' "$SLOG")"
    echo "  ATM shifts (PRE-SHIFT) : $(grep -c 'PRE-SHIFT' "$SLOG")"
    echo "  EOD pass               : $(grep -c 'EOD -- closing all' "$SLOG")"
    echo "  Kite errors in log     : $(grep -c 'failed: \|Quote failed\|LTP failed\|Hist failed' "$SLOG")"
    echo "  exits by type:"; grep -o 'FORENSICS [A-Z_0-9]*' "$SLOG" | sort | uniq -c | sed 's/^/    /'
else
    echo "  [WARN] recorder sandbox log not found: $SLOG"
fi
echo "--- live bot (for comparison only)"
LLOG="$ETF/logs/option_buy_$DAY.log"
[ -f "$LLOG" ] && echo "  live entries: $(grep -c 'Opened:' "$LLOG")  live exits: $(grep -c 'FORENSICS' "$LLOG")"

echo "--- archive"
TAR="$ETF/harness/recording_$DAY.tar.gz"
tar -czf "$TAR" -C "$ETF/harness/recordings" "$DAY" && \
    echo "  $TAR  ($(du -h "$TAR" | cut -f1))  -> copy to desktop ETF_PI\\harness\\"
echo "  disk free: $(df -h "$ETF" | awk 'NR==2{print $4}')"
