#!/bin/bash
# Restart all Helper services after code update.
# Run from server: bash /home/trustit/Desktop/BOTS/Helper/restart_services.sh

set -e
cd /home/trustit/Desktop/BOTS/Helper

echo "=== Git pull ==="
git pull

echo ""
echo "=== Killing services ==="

# Spread monitor (BCS + FH + watchlist)
if pkill -f "bcs.spread_monitor --cron" 2>/dev/null; then
    echo "  Killed: spread_monitor"
else
    echo "  Not running: spread_monitor"
fi

# Magnet monitor
if pkill -f "playbook.magnet" 2>/dev/null; then
    echo "  Killed: magnet"
else
    echo "  Not running: magnet"
fi

# ST Watch
if pkill -f "playbook.st_watch" 2>/dev/null; then
    echo "  Killed: st_watch"
else
    echo "  Not running: st_watch"
fi

# Pyramid: short-lived, no kill needed
echo "  Pyramid: short-lived, picks up on next cron"

# Remove stale lock files
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
echo "Done. If services show 'waiting', check again in 5 min (cron interval)."
