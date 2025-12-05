#!/bin/bash
# =============================================================================
# SNAIL Pi Setup Script
# =============================================================================
# Run this ONCE after pulling from git to set up SNAIL on Pi
#
# Usage: ./scripts/pi_setup.sh
# =============================================================================

set -e

BOTS_DIR="/home/trustit/Desktop/BOTS"
SNAIL_DIR="$BOTS_DIR/SNAIL"

echo "=============================================="
echo "SNAIL Setup for Raspberry Pi"
echo "=============================================="
echo "BOTS directory: $BOTS_DIR"
echo "SNAIL directory: $SNAIL_DIR"
echo ""

# Check if SNAIL directory exists
if [ ! -d "$SNAIL_DIR" ]; then
    echo "ERROR: SNAIL directory not found!"
    echo "Run 'git pull' first from $BOTS_DIR"
    exit 1
fi

cd "$SNAIL_DIR"

# -----------------------------------------------------------------------------
# Step 1: Create SNAIL venv (separate from CROCODILE)
# -----------------------------------------------------------------------------
echo "[1/5] Setting up Python virtual environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created new venv"
else
    echo "  venv already exists"
fi

source venv/bin/activate
pip install --upgrade pip -q

# -----------------------------------------------------------------------------
# Step 2: Install dependencies
# -----------------------------------------------------------------------------
echo "[2/5] Installing dependencies..."
pip install -r requirements.txt -q
echo "  Dependencies installed"

# -----------------------------------------------------------------------------
# Step 3: Create directories
# -----------------------------------------------------------------------------
echo "[3/5] Creating directories..."
mkdir -p data
mkdir -p logs
mkdir -p logs/claude
echo "  Created: data/, logs/, logs/claude/"

# -----------------------------------------------------------------------------
# Step 4: Initialize database
# -----------------------------------------------------------------------------
echo "[4/5] Initializing database..."
if [ ! -f "data/snail.db" ]; then
    python main.py init
    echo "  Database initialized"
else
    echo "  Database already exists"
fi

# -----------------------------------------------------------------------------
# Step 5: Make scripts executable
# -----------------------------------------------------------------------------
echo "[5/5] Making scripts executable..."
chmod +x scripts/*.sh
echo "  Scripts are executable"

echo ""
echo "=============================================="
echo "Setup Complete!"
echo "=============================================="
echo ""
echo "NEXT STEPS:"
echo ""
echo "1. Create .env file with your credentials:"
echo "   cp .env.template .env"
echo "   nano .env"
echo ""
echo "2. Set SNAIL_PAPER_TRADING=true in .env"
echo ""
echo "3. Test the setup:"
echo "   source venv/bin/activate"
echo "   python main.py test"
echo ""
echo "4. Add cron jobs (see below)"
echo ""
echo "=============================================="
echo "SNAIL CRON JOBS TO ADD:"
echo "=============================================="
cat << 'CRON'

# =============================================================================
# SNAIL Trading Bot (add to existing crontab)
# =============================================================================

# 9. SNAIL Daily Startup - 8:45 AM (Mon-Fri)
45 8 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py startup >> logs/cron.log 2>&1

# 10. SNAIL Entry Attempts - Hourly 9:30-14:30 (Mon-Fri)
30 9 * * 1-5  cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1
30 10 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1
30 11 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1
30 12 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1
30 13 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1
30 14 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py entry >> logs/cron.log 2>&1

# 11. SNAIL Monitor - Every 10 mins 9:16-15:26 (Mon-Fri)
16,26,36,46,56 9 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python -c "from src.workflows.monitor_workflow import MonitorWorkflow; MonitorWorkflow().run_once()" >> logs/cron.log 2>&1
6,16,26,36,46,56 10-14 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python -c "from src.workflows.monitor_workflow import MonitorWorkflow; MonitorWorkflow().run_once()" >> logs/cron.log 2>&1
6,16,26 15 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python -c "from src.workflows.monitor_workflow import MonitorWorkflow; MonitorWorkflow().run_once()" >> logs/cron.log 2>&1

# 12. SNAIL Daily Summary - 3:35 PM (Mon-Fri)
35 15 * * 1-5 cd /home/trustit/Desktop/BOTS/SNAIL && ./venv/bin/python main.py summary --send >> logs/cron.log 2>&1

CRON

echo ""
echo "To add cron jobs, run: crontab -e"
echo "Then paste the jobs above at the end."
echo ""
