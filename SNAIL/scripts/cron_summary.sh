#!/bin/bash
# =============================================================================
# SNAIL Daily Summary
# =============================================================================
# Scheduled: 3:35 PM IST (Monday-Friday)
# Purpose: Send end-of-day summary via Telegram
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate virtual environment
source venv/bin/activate

# Set timezone
export TZ=Asia/Kolkata

# Log file with date
LOG_FILE="logs/cron_summary_$(date +%Y%m%d).log"

echo "" >> $LOG_FILE
echo "=============================================" >> $LOG_FILE
echo "Daily Summary - $(date '+%Y-%m-%d %H:%M:%S')" >> $LOG_FILE
echo "=============================================" >> $LOG_FILE

# Send daily summary
python main.py summary --send >> $LOG_FILE 2>&1

echo "Summary sent" >> $LOG_FILE
