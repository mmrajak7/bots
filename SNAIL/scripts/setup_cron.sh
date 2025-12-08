#!/bin/bash
# SNAIL Cron Jobs Setup Script
# Run on Raspberry Pi: bash scripts/setup_cron.sh

set -e

SNAIL_DIR="/home/trustit/Desktop/BOTS/SNAIL"

echo "=========================================="
echo "SNAIL Cron Jobs Setup"
echo "=========================================="

# Create cron wrapper scripts
echo "[1/4] Creating cron wrapper scripts..."

# Startup script
cat > "$SNAIL_DIR/scripts/cron_startup.sh" << 'EOF'
#!/bin/bash
cd /home/trustit/Desktop/BOTS/SNAIL
source venv/bin/activate
export TZ=Asia/Kolkata
python main.py startup >> logs/cron_startup.log 2>&1
EOF

# Entry script
cat > "$SNAIL_DIR/scripts/cron_entry.sh" << 'EOF'
#!/bin/bash
cd /home/trustit/Desktop/BOTS/SNAIL
source venv/bin/activate
export TZ=Asia/Kolkata
python main.py entry >> logs/cron_entry.log 2>&1
EOF

# Monitor script
cat > "$SNAIL_DIR/scripts/cron_monitor.sh" << 'EOF'
#!/bin/bash
cd /home/trustit/Desktop/BOTS/SNAIL
source venv/bin/activate
export TZ=Asia/Kolkata
python -c "
from src.workflows.monitor_workflow import MonitorWorkflow
monitor = MonitorWorkflow()
monitor.run_once()
" >> logs/cron_monitor.log 2>&1
EOF

# Summary script
cat > "$SNAIL_DIR/scripts/cron_summary.sh" << 'EOF'
#!/bin/bash
cd /home/trustit/Desktop/BOTS/SNAIL
source venv/bin/activate
export TZ=Asia/Kolkata
python main.py summary --send >> logs/cron_summary.log 2>&1
EOF

chmod +x "$SNAIL_DIR/scripts/cron_startup.sh"
chmod +x "$SNAIL_DIR/scripts/cron_entry.sh"
chmod +x "$SNAIL_DIR/scripts/cron_monitor.sh"
chmod +x "$SNAIL_DIR/scripts/cron_summary.sh"

echo "[2/4] Creating cron job file..."

# Create cron content
CRON_CONTENT="# =============================================================================
# SNAIL Trading Bot - Cron Schedule (IST)
# Pi timezone must be IST: sudo timedatectl set-timezone Asia/Kolkata
# =============================================================================

# DAILY STARTUP - 8:45 AM (before market opens)
# Authenticates Kite, refreshes instruments, sends morning summary
45 8 * * 1-5 $SNAIL_DIR/scripts/cron_startup.sh

# ENTRY ATTEMPTS - Hourly 9:30 AM to 2:30 PM
# Checks entry conditions, triggers pre-entry analysis if conditions met
30 9 * * 1-5  $SNAIL_DIR/scripts/cron_entry.sh
30 10 * * 1-5 $SNAIL_DIR/scripts/cron_entry.sh
30 11 * * 1-5 $SNAIL_DIR/scripts/cron_entry.sh
30 12 * * 1-5 $SNAIL_DIR/scripts/cron_entry.sh
30 13 * * 1-5 $SNAIL_DIR/scripts/cron_entry.sh
30 14 * * 1-5 $SNAIL_DIR/scripts/cron_entry.sh

# POSITION MONITORING - Every 10 mins 9:20 AM to 3:25 PM
# Monitors P&L, triggers alerts, checks exit conditions
20,30,40,50 9 * * 1-5 $SNAIL_DIR/scripts/cron_monitor.sh
0,10,20,30,40,50 10-14 * * 1-5 $SNAIL_DIR/scripts/cron_monitor.sh
0,10,20 15 * * 1-5 $SNAIL_DIR/scripts/cron_monitor.sh

# DAILY SUMMARY - 3:35 PM (after market closes)
# Sends end-of-day P&L summary via Telegram
35 15 * * 1-5 $SNAIL_DIR/scripts/cron_summary.sh

# LOG CLEANUP - Weekly on Sunday midnight
# Remove logs older than 30 days
0 0 * * 0 find $SNAIL_DIR/logs -name \"*.log\" -mtime +30 -delete
"

echo "[3/4] Installing cron jobs..."

# Backup existing crontab
crontab -l > /tmp/cron_backup_$(date +%Y%m%d_%H%M%S).txt 2>/dev/null || true

# Install new crontab (append to existing)
(crontab -l 2>/dev/null | grep -v "SNAIL"; echo "$CRON_CONTENT") | crontab -

echo "[4/4] Verifying installation..."

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Installed cron jobs:"
crontab -l | grep -A1 "SNAIL\|cron_"
echo ""
echo "Schedule Summary:"
echo "  08:45 AM  - Daily startup & morning summary"
echo "  09:30 AM  - Entry check (hourly until 2:30 PM)"
echo "  09:20 AM  - Position monitor (every 10 mins until 3:25 PM)"
echo "  03:35 PM  - Daily summary"
echo ""
echo "Log files:"
echo "  $SNAIL_DIR/logs/cron_startup.log"
echo "  $SNAIL_DIR/logs/cron_entry.log"
echo "  $SNAIL_DIR/logs/cron_monitor.log"
echo "  $SNAIL_DIR/logs/cron_summary.log"
echo ""
echo "To view cron jobs: crontab -l"
echo "To remove SNAIL crons: crontab -l | grep -v SNAIL | grep -v cron_ | crontab -"
