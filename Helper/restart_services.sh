#!/bin/bash
# Restart all Helper services after code update.
# Run: bash /home/trustit/Desktop/BOTS/Helper/restart_services.sh

set -e
BOTS_DIR=/home/trustit/Desktop/BOTS
HELPER_DIR=$BOTS_DIR/Helper

echo "=== Git pull ==="
cd "$BOTS_DIR"
# Remove this script if it blocks git pull (untracked vs tracked conflict)
rm -f "$HELPER_DIR/restart_services.sh"
git pull
echo ""

echo "=== Killing services ==="

if pkill -f "bcs.spread_monitor --cron" 2>/dev/null; then
    echo "  Killed: spread_monitor"
else
    echo "  Not running: spread_monitor"
fi

if pkill -f "playbook.magnet" 2>/dev/null; then
    echo "  Killed: magnet"
else
    echo "  Not running: magnet"
fi

if pkill -f "playbook.st_watch" 2>/dev/null; then
    echo "  Killed: st_watch"
else
    echo "  Not running: st_watch"
fi

echo "  Pyramid: short-lived, picks up on next cron"

rm -f /tmp/bcs_monitor.lock /tmp/st_watch.lock 2>/dev/null
echo "  Cleared lock files"
echo ""

echo "=== Waiting 10s for cron restart ==="
sleep 10
echo ""

echo "=== Service status ==="
pgrep -af "bcs.spread_monitor" && echo "  -> BCS: OK" || echo "  -> BCS: waiting for cron..."
pgrep -af "playbook.magnet" && echo "  -> MAGNET: OK" || echo "  -> MAGNET: waiting for cron..."
pgrep -af "playbook.st_watch" && echo "  -> ST_WATCH: OK" || echo "  -> ST_WATCH: waiting for cron..."
echo ""
echo "Done. If services show 'waiting', check again in 5 min."
