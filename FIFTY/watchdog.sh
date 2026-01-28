#!/bin/bash
# FIFTY Bot Watchdog Script
# Monitors daemon health and restarts if needed
# Add to cron: */5 * * * * /home/trustit/Desktop/BOTS/FIFTY/watchdog.sh

SERVICE_NAME="fifty-daemon"
BOT_DIR="/home/trustit/Desktop/BOTS/FIFTY"
HEARTBEAT_FILE="$BOT_DIR/data/.heartbeat"
LOG_FILE="$BOT_DIR/logs/watchdog.log"
MAX_HEARTBEAT_AGE=300  # 5 minutes

# Telegram alert function
send_telegram() {
    local message="$1"
    # Read config for bot token and chat id
    BOT_TOKEN=$(grep -oP 'bot_token:\s*\K[^\s]+' "$BOT_DIR/config/config.yaml" 2>/dev/null)
    CHAT_ID=$(grep -oP 'chat_id:\s*\K[^\s]+' "$BOT_DIR/config/config.yaml" 2>/dev/null)

    if [ -n "$BOT_TOKEN" ] && [ -n "$CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \
            -d "chat_id=$CHAT_ID" \
            -d "text=$message" \
            -d "parse_mode=HTML" > /dev/null 2>&1
    fi
}

log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

# Ensure log directory exists
mkdir -p "$BOT_DIR/logs"

# Check if service is running
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    log_message "Service $SERVICE_NAME is not running. Restarting..."
    send_telegram "<b>FIFTY Watchdog</b>: Service was down. Restarting..."

    sudo systemctl restart "$SERVICE_NAME"
    sleep 5

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        log_message "Service restarted successfully"
        send_telegram "<b>FIFTY Watchdog</b>: Service restarted successfully"
    else
        log_message "ERROR: Failed to restart service"
        send_telegram "<b>FIFTY Watchdog ALERT</b>: Failed to restart service! Manual intervention needed."
    fi
    exit 0
fi

# Check heartbeat file (if daemon is writing it)
if [ -f "$HEARTBEAT_FILE" ]; then
    HEARTBEAT_AGE=$(($(date +%s) - $(stat -c %Y "$HEARTBEAT_FILE")))

    if [ $HEARTBEAT_AGE -gt $MAX_HEARTBEAT_AGE ]; then
        log_message "Heartbeat stale ($HEARTBEAT_AGE seconds). Service may be frozen. Restarting..."
        send_telegram "<b>FIFTY Watchdog</b>: Heartbeat stale (${HEARTBEAT_AGE}s). Restarting..."

        sudo systemctl restart "$SERVICE_NAME"
        sleep 5

        if systemctl is-active --quiet "$SERVICE_NAME"; then
            log_message "Service restarted after stale heartbeat"
            send_telegram "<b>FIFTY Watchdog</b>: Service restarted successfully"
        else
            log_message "ERROR: Failed to restart after stale heartbeat"
            send_telegram "<b>FIFTY Watchdog ALERT</b>: Failed to restart! Manual intervention needed."
        fi
    fi
fi

# All good - service is running and heartbeat is fresh
exit 0
