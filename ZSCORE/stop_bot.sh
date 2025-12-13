#!/bin/bash
# Z-Score Trading Bot - Stop Script
# Can be scheduled to stop after market hours:
# 30 15 * * 1-5 cd /home/trustit/Desktop/BOTS/ZSCORE && ./stop_bot.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/zscore_bot.pid"
LOG_DIR="$SCRIPT_DIR/logs"
DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/startup_$DATE.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"
}

if [ ! -f "$PID_FILE" ]; then
    log "No PID file found - bot may not be running"
    exit 0
fi

PID=$(cat "$PID_FILE")

if ps -p "$PID" > /dev/null 2>&1; then
    log "Stopping bot with PID $PID..."
    kill -SIGINT "$PID"

    # Wait for graceful shutdown
    for i in {1..30}; do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            log "Bot stopped gracefully"
            rm -f "$PID_FILE"
            exit 0
        fi
        sleep 1
    done

    # Force kill if still running
    log "Bot not responding, force killing..."
    kill -9 "$PID" 2>/dev/null
    rm -f "$PID_FILE"
    log "Bot force stopped"
else
    log "Bot not running (stale PID file)"
    rm -f "$PID_FILE"
fi
