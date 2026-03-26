#!/bin/bash
# deploy_review_fixes.sh — Deploy 21 trading safety fixes to server
# Run on server: cd /home/trustit/Desktop/BOTS/Helper && bash deploy_review_fixes.sh
#
# What this deploys (2026-03-26):
#   CRITICAL: SQ7 directional, expiry validation, 5-layer exit restored,
#             trail SL profit floor, min score threshold, IST timezone,
#             hedge net_debit check, max capital cap
#   RELIABILITY: CT singleton+fsync+Drive sync, ST Watch thresholds [5,3,1],
#                EBBETF0431 added, DOWN=TREND FLIP labels, cost SL buffer
#
# Prerequisites: configs already synced via Drive (magnet_config.json, st_watch_config.json)

set -euo pipefail

BOTS=/home/trustit/Desktop/BOTS
HELPER=$BOTS/Helper
VENV=$BOTS/CROCODILE/venv/bin/python
LOG=$HELPER/logs/deploy_$(date +%Y%m%d_%H%M%S).log

echo "=== Deploy: 21 review fixes ($(date)) ===" | tee "$LOG"

# ── Step 1: Git pull ─────────────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "[1/5] Git pull..." | tee -a "$LOG"
cd "$HELPER"
git pull origin main 2>&1 | tee -a "$LOG"
echo "  Commit: $(git log --oneline -1)" | tee -a "$LOG"

# ── Step 2: Kill running monitors ────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "[2/5] Stopping running monitors..." | tee -a "$LOG"

# BCS/FH spread monitor
if pgrep -f "bcs.spread_monitor --cron" > /dev/null 2>&1; then
    pkill -f "bcs.spread_monitor --cron" && echo "  BCS monitor stopped" | tee -a "$LOG"
    sleep 2
else
    echo "  BCS monitor: not running" | tee -a "$LOG"
fi

# Magnet monitor
if pgrep -f "playbook.magnet" > /dev/null 2>&1; then
    pkill -f "playbook.magnet" && echo "  Magnet monitor stopped" | tee -a "$LOG"
    sleep 2
else
    echo "  Magnet monitor: not running" | tee -a "$LOG"
fi

# ST Watch (if long-lived cron mode)
if pgrep -f "playbook.st_watch cron" > /dev/null 2>&1; then
    pkill -f "playbook.st_watch cron" && echo "  ST Watch cron stopped" | tee -a "$LOG"
    sleep 2
else
    echo "  ST Watch cron: not running" | tee -a "$LOG"
fi

# Remove stale lock files (flock auto-releases on kill, but just in case)
rm -f /tmp/bcs_monitor.lock /tmp/st_watch.lock 2>/dev/null
echo "  Lock files cleaned" | tee -a "$LOG"

# ── Step 3: Verify configs are up to date ────────────────────────────────
echo "" | tee -a "$LOG"
echo "[3/5] Verifying configs..." | tee -a "$LOG"

# ST Watch: should have [5,3,1] thresholds and 21 symbols
$VENV -c "
import json
with open('$HELPER/config/st_watch_config.json') as f:
    d = json.load(f)
thresholds = d.get('alert_thresholds', [])
syms = sum(len(v) for v in d.get('symbols', {}).values())
ok = thresholds == [5, 3, 1] and syms == 21
print(f'  st_watch: thresholds={thresholds} symbols={syms} {\"OK\" if ok else \"MISMATCH!\"} ')
" 2>&1 | tee -a "$LOG"

# Magnet: should have confidence_file_name and max_capital_per_trade
$VENV -c "
import json
with open('$HELPER/config/magnet_config.json') as f:
    d = json.load(f)
ct_file = d.get('google_drive', {}).get('confidence_file_name', 'MISSING')
max_cap = d.get('max_capital_per_trade', 'MISSING')
use_ct = d.get('use_confidence_tracker', False)
print(f'  magnet: ct_file={ct_file} max_capital={max_cap} use_ct={use_ct}')
" 2>&1 | tee -a "$LOG"

# ── Step 4: Verify imports work ──────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "[4/5] Verifying code imports..." | tee -a "$LOG"

cd "$HELPER"
$VENV -c "
from playbook.compute_st import IST, compute_supertrend
from playbook.magnet.config import MAX_CAPITAL_PER_TRADE
from playbook.magnet.confidence_tracker import get_tracker, _MIN_TOTAL_SCORE
print(f'  compute_st: IST={IST}')
print(f'  magnet config: MAX_CAPITAL={MAX_CAPITAL_PER_TRADE}')
print(f'  confidence: MIN_TOTAL_SCORE={_MIN_TOTAL_SCORE}')
print('  All imports OK')
" 2>&1 | tee -a "$LOG"

# Quick syntax check on monitor (most complex file)
$VENV -c "import ast; ast.parse(open('playbook/magnet/monitor.py').read()); print('  monitor.py: syntax OK')" 2>&1 | tee -a "$LOG"

# ── Step 5: Clean old files ──────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "[5/5] Cleaning old files..." | tee -a "$LOG"

# Old backtest CSVs (derived data, reproducible)
count=0
for f in playbook/backtest_*_results.csv playbook/backtest_*_trades.csv playbook/backtest_magnet_futures.csv playbook/backtest_magnet_trades.csv; do
    if [ -f "$f" ]; then
        rm -f "$f"
        count=$((count + 1))
    fi
done
echo "  Deleted $count backtest CSV files" | tee -a "$LOG"

# One-off scripts
for f in playbook/analyze_velocity.py playbook/analyze_velocity_results.csv breadth_fix_cleanup.sh setup_pyramid_cron.sh; do
    if [ -f "$f" ]; then
        rm -f "$f"
        echo "  Deleted $f" | tee -a "$LOG"
    fi
done

# Old __pycache__ (force recompile with new code)
find playbook/magnet -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find playbook/st_watch -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find playbook -maxdepth 1 -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "  Cleared __pycache__ (force recompile)" | tee -a "$LOG"

# ── Summary ──────────────────────────────────────────────────────────────
echo "" | tee -a "$LOG"
echo "=== Deploy complete ($(date)) ===" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Next steps:" | tee -a "$LOG"
echo "  - BCS monitor restarts automatically via cron (*/5 9-15)" | tee -a "$LOG"
echo "  - ST Watch restarts automatically via cron (15 9-15)" | tee -a "$LOG"
echo "  - Magnet: restart manually if needed" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "Verify with:" | tee -a "$LOG"
echo "  tail -f $HELPER/logs/cron_bcs.log" | tee -a "$LOG"
echo "  tail -f $HELPER/logs/st_watch.log" | tee -a "$LOG"
echo "  $VENV -m playbook.st_watch status" | tee -a "$LOG"
