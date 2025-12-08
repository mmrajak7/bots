#!/bin/bash
# SNAIL Telegram Service Setup Script
# Run on Raspberry Pi: bash scripts/setup_telegram_service.sh

set -e

echo "=========================================="
echo "SNAIL Telegram Service Setup"
echo "=========================================="

# Variables
USER="trustit"
SNAIL_DIR="/home/trustit/Desktop/BOTS/SNAIL"
SERVICE_FILE="/etc/systemd/system/snail-telegram.service"

# Create logs directory
echo "[1/5] Creating logs directory..."
mkdir -p "$SNAIL_DIR/logs"

# Create service file
echo "[2/5] Creating systemd service file..."
sudo tee $SERVICE_FILE > /dev/null << 'EOF'
[Unit]
Description=SNAIL Telegram Polling Bot
After=network.target

[Service]
Type=simple
User=trustit
WorkingDirectory=/home/trustit/Desktop/BOTS/SNAIL
Environment=PATH=/home/trustit/Desktop/BOTS/SNAIL/venv/bin
ExecStart=/home/trustit/Desktop/BOTS/SNAIL/venv/bin/python scripts/telegram_poller.py
Restart=always
RestartSec=10

# Logging
StandardOutput=append:/home/trustit/Desktop/BOTS/SNAIL/logs/telegram_service.log
StandardError=append:/home/trustit/Desktop/BOTS/SNAIL/logs/telegram_service_error.log

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd
echo "[3/5] Reloading systemd..."
sudo systemctl daemon-reload

# Enable service
echo "[4/5] Enabling service on boot..."
sudo systemctl enable snail-telegram

# Start service
echo "[5/5] Starting service..."
sudo systemctl start snail-telegram

# Show status
echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
sudo systemctl status snail-telegram --no-pager

echo ""
echo "Useful commands:"
echo "  sudo systemctl status snail-telegram   # Check status"
echo "  sudo systemctl restart snail-telegram  # Restart"
echo "  sudo systemctl stop snail-telegram     # Stop"
echo "  tail -f $SNAIL_DIR/logs/telegram_service.log  # View logs"
