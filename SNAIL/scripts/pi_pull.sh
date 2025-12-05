#!/bin/bash
# =============================================================================
# SNAIL Git Pull Script
# =============================================================================
# Run this anytime to pull latest code from GitHub
#
# Usage: ./scripts/pi_pull.sh
# =============================================================================

BOTS_DIR="/home/trustit/Desktop/BOTS"
SNAIL_DIR="$BOTS_DIR/SNAIL"

echo "=============================================="
echo "Pulling latest SNAIL code from GitHub"
echo "=============================================="

cd "$BOTS_DIR"

# Pull latest
echo "[1/3] Pulling from GitHub..."
git pull origin main

# Update dependencies if requirements changed
echo "[2/3] Checking dependencies..."
cd "$SNAIL_DIR"
source venv/bin/activate
pip install -r requirements.txt -q

# Show status
echo "[3/3] Current status..."
git log -1 --oneline

echo ""
echo "=============================================="
echo "Pull complete!"
echo "=============================================="
