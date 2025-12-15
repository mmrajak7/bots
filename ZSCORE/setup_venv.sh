#!/bin/bash
# Setup venv for ZSCORE (uses CROCODILE's shared venv)
# Run this on the Linux server: ./setup_venv.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CROCODILE_DIR="$SCRIPT_DIR/../CROCODILE"
VENV_PATH="$CROCODILE_DIR/venv"

echo "=========================================="
echo "ZSCORE Bot - Virtual Environment Setup"
echo "=========================================="
echo "Script Dir: $SCRIPT_DIR"
echo "CROCODILE Dir: $CROCODILE_DIR"
echo "Venv Path: $VENV_PATH"
echo ""

# Check if CROCODILE directory exists
if [ ! -d "$CROCODILE_DIR" ]; then
    echo "ERROR: CROCODILE directory not found at $CROCODILE_DIR"
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d "$VENV_PATH" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_PATH"
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to create venv"
        exit 1
    fi
    echo "Venv created successfully"
else
    echo "Venv already exists at $VENV_PATH"
fi

# Activate venv and install dependencies
echo ""
echo "Installing CROCODILE dependencies..."
source "$VENV_PATH/bin/activate"

pip install --upgrade pip
pip install -r "$CROCODILE_DIR/requirements.txt"

# Also install ZSCORE specific deps (if any additional)
if [ -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo ""
    echo "Installing ZSCORE dependencies..."
    pip install -r "$SCRIPT_DIR/requirements.txt"
fi

echo ""
echo "=========================================="
echo "Verifying installation..."
echo "=========================================="

# Verify kiteconnect
python3 -c "from kiteconnect import KiteConnect; print('kiteconnect: OK')" 2>/dev/null || echo "kiteconnect: FAILED"

# Verify requests
python3 -c "import requests; print('requests: OK')" 2>/dev/null || echo "requests: FAILED"

# Verify sqlalchemy
python3 -c "import sqlalchemy; print('sqlalchemy: OK')" 2>/dev/null || echo "sqlalchemy: FAILED"

echo ""
echo "=========================================="
echo "Creating logs directory for ZSCORE..."
mkdir -p "$SCRIPT_DIR/logs"
echo "Done!"
echo ""
echo "Setup complete. To test:"
echo "  $VENV_PATH/bin/python $SCRIPT_DIR/main.py --help"
echo "=========================================="
