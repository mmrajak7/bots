#!/bin/bash
# =============================================================================
# SNAIL Position Monitor
# =============================================================================
# Scheduled: Every 10 minutes (9:16 AM - 3:26 PM)
# Purpose: Monitor P&L, check exit triggers, send alerts
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate virtual environment
source venv/bin/activate

# Set timezone
export TZ=Asia/Kolkata

# Log file with date
LOG_FILE="logs/cron_monitor_$(date +%Y%m%d).log"

# Check if within market hours
HOUR=$(date +%H)
MIN=$(date +%M)
TIME_NUM=$((HOUR * 100 + MIN))

if [ $TIME_NUM -lt 916 ] || [ $TIME_NUM -gt 1526 ]; then
    echo "$(date '+%H:%M:%S') - Outside market hours, skipping" >> $LOG_FILE
    exit 0
fi

echo "" >> $LOG_FILE
echo "$(date '+%H:%M:%S') - Monitor cycle" >> $LOG_FILE

# Run single monitor cycle
python -c "
import sys
sys.path.insert(0, '.')
from src.workflows.monitor_workflow import MonitorWorkflow
monitor = MonitorWorkflow()
monitor.run_once()
" >> $LOG_FILE 2>&1

EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "Monitor failed with exit code: $EXIT_CODE" >> $LOG_FILE
fi
