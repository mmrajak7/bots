#!/bin/bash
# Sniper - Cron Setup Script for Linux
# Run this on Linux server to automatically configure cron job

set -e  # Exit on error

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "Sniper Scanner - Cron Setup"
echo "=========================================="
echo ""

# Get current directory
SNIPER_DIR="$(cd "$(dirname "$0")" && pwd)"
BOTS_DIR="$(dirname "$SNIPER_DIR")"

echo "Sniper directory: $SNIPER_DIR"
echo "BOTS directory: $BOTS_DIR"
echo ""

# Check Python 3
echo -n "Checking Python 3... "
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}FAILED${NC}"
    echo "Python 3 not found. Please install: sudo apt-get install python3"
    exit 1
fi
PYTHON_VERSION=$(python3 --version)
echo -e "${GREEN}OK${NC} ($PYTHON_VERSION)"

# Check pip3
echo -n "Checking pip3... "
if ! command -v pip3 &> /dev/null; then
    echo -e "${RED}FAILED${NC}"
    echo "pip3 not found. Please install: sudo apt-get install python3-pip"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# Check required packages
echo -n "Checking kiteconnect... "
if python3 -c "import kiteconnect" 2>/dev/null; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}MISSING${NC}"
    echo "Installing kiteconnect..."
    pip3 install kiteconnect
fi

echo -n "Checking requests... "
if python3 -c "import requests" 2>/dev/null; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${YELLOW}MISSING${NC}"
    echo "Installing requests..."
    pip3 install requests
fi

# Check config files
echo -n "Checking Bouncer config... "
if [ -f "$BOTS_DIR/Bouncer/config/config.json" ]; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}MISSING${NC}"
    echo "File not found: $BOTS_DIR/Bouncer/config/config.json"
    echo "Please copy config.json to Bouncer/config/"
    exit 1
fi

echo -n "Checking Kite token... "
if [ -f "$BOTS_DIR/data/kite_access_token.json" ]; then
    echo -e "${GREEN}OK${NC}"
else
    echo -e "${RED}MISSING${NC}"
    echo "File not found: $BOTS_DIR/data/kite_access_token.json"
    echo "Please copy kite_access_token.json to data/"
    exit 1
fi

# Test scanner
echo ""
echo "Testing scanner..."
cd "$SNIPER_DIR"
python3 scanner.py --test > /tmp/sniper_test.log 2>&1

if grep -q "FULL SCAN" /tmp/sniper_test.log; then
    echo -e "${GREEN}Scanner test passed!${NC}"
else
    echo -e "${RED}Scanner test failed!${NC}"
    echo "Check /tmp/sniper_test.log for details"
    exit 1
fi

# Show current crontab
echo ""
echo "Current crontab:"
crontab -l 2>/dev/null || echo "(empty)"

# Ask to add cron job
echo ""
echo "=========================================="
echo "Ready to add cron job"
echo "=========================================="
echo ""
echo "This will run scanner every minute during market hours:"
echo "  - 9:15 AM - 3:30 PM IST"
echo "  - Monday to Friday"
echo ""
read -p "Add cron job? (y/n): " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cron setup cancelled."
    echo ""
    echo "To add manually, run:"
    echo "  crontab -e"
    echo ""
    echo "And add these lines:"
    echo "  * 9-14 * * 1-5 cd $SNIPER_DIR && python3 scanner.py >> logs/cron.log 2>&1"
    echo "  0-30 15 * * 1-5 cd $SNIPER_DIR && python3 scanner.py >> logs/cron.log 2>&1"
    exit 0
fi

# Add to crontab
echo ""
echo "Adding to crontab..."

# Backup current crontab
crontab -l > /tmp/crontab.bak 2>/dev/null || touch /tmp/crontab.bak

# Check if already exists
if grep -q "Sniper Scanner" /tmp/crontab.bak; then
    echo -e "${YELLOW}Cron job already exists. Replacing...${NC}"
    grep -v "Sniper Scanner" /tmp/crontab.bak > /tmp/crontab.new
    grep -v "cd $SNIPER_DIR" /tmp/crontab.new > /tmp/crontab.tmp || true
    mv /tmp/crontab.tmp /tmp/crontab.new
else
    cp /tmp/crontab.bak /tmp/crontab.new
fi

# Add new cron jobs
cat >> /tmp/crontab.new << EOF

# Sniper Scanner - Every minute during market hours (9:15 AM - 3:30 PM IST)
* 9-14 * * 1-5 cd $SNIPER_DIR && python3 scanner.py >> logs/cron.log 2>&1
0-30 15 * * 1-5 cd $SNIPER_DIR && python3 scanner.py >> logs/cron.log 2>&1
EOF

# Install new crontab
crontab /tmp/crontab.new

echo -e "${GREEN}Cron job added successfully!${NC}"
echo ""

# Show final crontab
echo "New crontab:"
crontab -l | tail -5

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Scanner will run every minute during market hours."
echo ""
echo "Monitor logs:"
echo "  tail -f $SNIPER_DIR/logs/scanner_\$(date +%Y%m%d).log"
echo ""
echo "Check alerts:"
echo "  grep 'ALERT:' $SNIPER_DIR/logs/scanner_\$(date +%Y%m%d).log"
echo ""
echo "View cron output:"
echo "  tail -f $SNIPER_DIR/logs/cron.log"
echo ""
