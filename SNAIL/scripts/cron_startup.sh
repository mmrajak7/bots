#!/bin/bash
# =============================================================================
# SNAIL Daily Startup
# =============================================================================
# Scheduled: 8:45 AM IST (Monday-Friday)
# Purpose: Authenticate Kite, refresh instruments, run pre-market checks
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate virtual environment
source venv/bin/activate

# Set timezone
export TZ=Asia/Kolkata

# Log file with date
LOG_FILE="logs/cron_startup_$(date +%Y%m%d).log"

echo "" >> $LOG_FILE
echo "=============================================" >> $LOG_FILE
echo "SNAIL Daily Startup - $(date '+%Y-%m-%d %H:%M:%S')" >> $LOG_FILE
echo "=============================================" >> $LOG_FILE

# Run startup
python main.py startup >> $LOG_FILE 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "Startup completed successfully" >> $LOG_FILE
else
    echo "Startup FAILED with exit code: $EXIT_CODE" >> $LOG_FILE
fi

echo "=============================================" >> $LOG_FILE
