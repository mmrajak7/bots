#!/bin/bash
# =============================================================================
# SNAIL Entry Attempt
# =============================================================================
# Scheduled: Hourly at :30 (9:30, 10:30, 11:30, 12:30, 13:30, 14:30)
# Purpose: Check entry conditions, execute if favorable
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate virtual environment
source venv/bin/activate

# Set timezone
export TZ=Asia/Kolkata

# Log file with date
LOG_FILE="logs/cron_entry_$(date +%Y%m%d).log"

echo "" >> $LOG_FILE
echo "---------------------------------------------" >> $LOG_FILE
echo "Entry Attempt - $(date '+%H:%M:%S')" >> $LOG_FILE
echo "---------------------------------------------" >> $LOG_FILE

# Run entry check
python main.py entry >> $LOG_FILE 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    echo "Entry check completed" >> $LOG_FILE
else
    echo "Entry check failed with exit code: $EXIT_CODE" >> $LOG_FILE
fi
