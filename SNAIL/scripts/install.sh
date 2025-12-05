#!/bin/bash
# =============================================================================
# SNAIL Installation Script for Raspberry Pi
# =============================================================================
# Run this after cloning the repository

set -e

echo "=============================================="
echo "SNAIL Installation Script"
echo "=============================================="

# Navigate to SNAIL directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
SNAIL_DIR=$(pwd)
BOTS_DIR=$(dirname "$SNAIL_DIR")

echo "SNAIL Directory: $SNAIL_DIR"
echo "BOTS Directory: $BOTS_DIR"

# Check Python version
echo ""
echo "[1/6] Checking Python..."
python3 --version
if [ $? -ne 0 ]; then
    echo "ERROR: Python 3 not found!"
    exit 1
fi

# Create virtual environment
echo ""
echo "[2/6] Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "Virtual environment created"
else
    echo "Virtual environment already exists"
fi

# Activate and install dependencies
echo ""
echo "[3/6] Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Create SNAIL directories
echo ""
echo "[4/6] Creating SNAIL directories..."
mkdir -p data
mkdir -p logs
mkdir -p logs/claude
echo "Created: data/, logs/, logs/claude/"

# Create shared data directory
echo ""
echo "[5/6] Creating shared data directory..."
mkdir -p "$BOTS_DIR/data"
echo "Created: $BOTS_DIR/data/"

# Initialize database
echo ""
echo "[6/6] Initializing database..."
python main.py init

echo ""
echo "=============================================="
echo "Installation Complete!"
echo "=============================================="
echo ""
echo "Next Steps:"
echo "1. Create .env file:"
echo "   cp .env.template .env"
echo "   nano .env"
echo ""
echo "2. Add your credentials to .env"
echo ""
echo "3. Set SNAIL_PAPER_TRADING=true for paper trading"
echo ""
echo "4. Test the installation:"
echo "   source venv/bin/activate"
echo "   python main.py test"
echo ""
echo "5. Configure cron jobs (see DEPLOYMENT_GUIDE.md)"
echo ""
echo "6. Start Telegram service:"
echo "   sudo systemctl enable snail-telegram"
echo "   sudo systemctl start snail-telegram"
echo ""
