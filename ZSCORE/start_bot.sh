#!/bin/bash
# Z-Score Trading Bot - Startup Script for Cron
# Schedule: 0 12 * * 1-5 cd /home/trustit/Desktop/BOTS/ZSCORE && ./start_bot.sh
# (Runs at 12:00 PM on weekdays to ensure it's ready by 13:00)

# Configuration
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_PATH="$SCRIPT_DIR/../CROCODILE/venv/bin/python"
LOG_DIR="$SCRIPT_DIR/logs"
PID_FILE="$SCRIPT_DIR/zscore_bot.pid"

# Create log directory
mkdir -p "$LOG_DIR"

# Log file for this session
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/startup_$DATE.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

# Check if already running
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
        log "Bot already running with PID $OLD_PID"
        exit 0
    else
        log "Stale PID file found, removing..."
        rm -f "$PID_FILE"
    fi
fi

# Check if it's a trading day (skip weekends)
DAY_OF_WEEK=$(date +%u)
if [ "$DAY_OF_WEEK" -gt 5 ]; then
    log "Weekend - skipping"
    exit 0
fi

log "=========================================="
log "Starting Z-Score Trading Bot"
log "Script Dir: $SCRIPT_DIR"
log "Python: $PYTHON_PATH"
log "=========================================="

# Change to script directory
cd "$SCRIPT_DIR"

# Start the bot in background
nohup "$PYTHON_PATH" main.py >> "$LOG_FILE" 2>&1 &
BOT_PID=$!

# Save PID
echo "$BOT_PID" > "$PID_FILE"

log "Bot started with PID: $BOT_PID"
log "Logs: $LOG_FILE"

# Wait a few seconds to check if it started successfully
sleep 5

if ps -p "$BOT_PID" > /dev/null 2>&1; then
    log "Bot is running successfully"
else
    log "ERROR: Bot failed to start!"
    rm -f "$PID_FILE"
    exit 1
fi
