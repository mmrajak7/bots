#!/bin/bash
# =============================================================================
# ETF_PI Server Cleanup & Sync Script
# Run on Raspberry Pi: bash server_cleanup.sh
#
# What this does:
#   1. Backs up ALL local-only files (secrets, legacy scripts, state, data)
#   2. Sparse checkout — removes duplicate project folders (Bouncer, CROCODILE...)
#   3. Syncs ETF_PI with latest from GitHub
#   4. Restores all local-only files from backup
#   5. Verifies every cron-referenced file exists
#
# Safe to run multiple times. Does NOT touch /home/trustit/Desktop/BOTS/
# =============================================================================

set -uo pipefail

REPO_DIR="/home/trustit/Desktop/Scripts/bots"
ETF_DIR="$REPO_DIR/ETF_PI"
BACKUP_DIR="/tmp/etf_pi_backup_$(date +%Y%m%d_%H%M%S)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}OK${NC}  $1"; }
fail() { echo -e "  ${RED}FAIL${NC}  $1"; }
warn() { echo -e "  ${YELLOW}WARN${NC}  $1"; }

echo "=============================================="
echo " ETF_PI Server Cleanup & Sync"
echo " $(date)"
echo "=============================================="

# --- Pre-flight checks ---
if [ ! -d "$REPO_DIR/.git" ]; then
    echo -e "${RED}ERROR: $REPO_DIR is not a git repo${NC}"
    exit 1
fi

if [ ! -d "$ETF_DIR" ]; then
    echo -e "${RED}ERROR: $ETF_DIR does not exist${NC}"
    exit 1
fi

# ==========================================================================
# STEP 1: Backup ALL local-only files
# ==========================================================================
echo ""
echo "--- Step 1: Backing up local-only files ---"
mkdir -p "$BACKUP_DIR"

# Files that are local-only (secrets, legacy scripts, runtime data)
# These are NOT in git and must survive the sync
LOCAL_FILES=(
    "common.py"
    "neo_credentials.yaml"
    "secret.json"
    "extract_data.py"
    "extract_data_mom.py"
    "tracker.py"
    "kite_trade.py"
    "kiteext.py"
    "oppurtunity.py"
    "option_buy_backup.py"
    "crude_inventory.py"
    "dora_poller.py"
    "index_opp.py"
    "token.txt"
    "test.py"
    "tesss.py"
    "backtest_support_zone.py"
    "backtest_weekly.py"
    "backtest_time_to_tp.py"
    "neo_bridge.py"
)

backed_up=0
for f in "${LOCAL_FILES[@]}"; do
    if [ -f "$ETF_DIR/$f" ]; then
        cp "$ETF_DIR/$f" "$BACKUP_DIR/"
        ok "backed up $f"
        backed_up=$((backed_up + 1))
    fi
done

# Backup neo/data/ directory (session cache, state, symbol cache)
if [ -d "$ETF_DIR/neo/data" ]; then
    mkdir -p "$BACKUP_DIR/neo_data"
    cp -r "$ETF_DIR/neo/data/"* "$BACKUP_DIR/neo_data/" 2>/dev/null || true
    ok "backed up neo/data/ (session, state, tracker)"
fi

# Backup state JSON files (st_opt_*.json, st_opt_state.json, etc.)
for f in "$ETF_DIR"/st_opt_*.json "$ETF_DIR"/*_state.json "$ETF_DIR"/*_positions.json; do
    if [ -f "$f" ]; then
        cp "$f" "$BACKUP_DIR/"
        ok "backed up $(basename $f)"
        backed_up=$((backed_up + 1))
    fi
done

# Backup logs directory
if [ -d "$ETF_DIR/logs" ]; then
    mkdir -p "$BACKUP_DIR/logs"
    cp -r "$ETF_DIR/logs/"* "$BACKUP_DIR/logs/" 2>/dev/null || true
    ok "backed up logs/"
fi

# Backup op_instruments.csv (regenerated daily, but keep today's)
if [ -f "$ETF_DIR/op_instruments.csv" ]; then
    cp "$ETF_DIR/op_instruments.csv" "$BACKUP_DIR/"
    ok "backed up op_instruments.csv"
fi

# Backup kite_access_token.json if exists locally
if [ -f "$ETF_DIR/kite_access_token.json" ]; then
    cp "$ETF_DIR/kite_access_token.json" "$BACKUP_DIR/"
    ok "backed up kite_access_token.json"
fi

echo "Total backed up: $backed_up files + directories"
echo "Backup location: $BACKUP_DIR"

# ==========================================================================
# STEP 2: Sparse checkout — remove duplicate project folders
# ==========================================================================
echo ""
echo "--- Step 2: Sparse checkout (ETF_PI only) ---"
cd "$REPO_DIR"

# Show what's being removed
echo "Before cleanup:"
du -sh */ 2>/dev/null | grep -v ETF_PI || true

git sparse-checkout init --cone
git sparse-checkout set ETF_PI

echo "Sparse checkout set to: $(git sparse-checkout list)"

# ==========================================================================
# STEP 3: Sync with GitHub
# ==========================================================================
echo ""
echo "--- Step 3: Syncing with GitHub ---"
git fetch origin

# Reset to remote — safe because local-only files are untracked (backed up above)
git reset --hard origin/main

echo "Synced to: $(git log --oneline -1)"

# ==========================================================================
# STEP 4: Restore local-only files
# ==========================================================================
echo ""
echo "--- Step 4: Restoring local-only files ---"

restored=0
for f in "${LOCAL_FILES[@]}"; do
    if [ -f "$BACKUP_DIR/$f" ]; then
        cp "$BACKUP_DIR/$f" "$ETF_DIR/"
        ok "restored $f"
        restored=$((restored + 1))
    fi
done

# Restore neo/data/
if [ -d "$BACKUP_DIR/neo_data" ]; then
    mkdir -p "$ETF_DIR/neo/data"
    cp -r "$BACKUP_DIR/neo_data/"* "$ETF_DIR/neo/data/" 2>/dev/null || true
    ok "restored neo/data/"
fi

# Restore state JSON files
for f in "$BACKUP_DIR"/st_opt_*.json "$BACKUP_DIR"/*_state.json "$BACKUP_DIR"/*_positions.json; do
    if [ -f "$f" ]; then
        cp "$f" "$ETF_DIR/"
        ok "restored $(basename $f)"
        restored=$((restored + 1))
    fi
done

# Restore op_instruments.csv
if [ -f "$BACKUP_DIR/op_instruments.csv" ]; then
    cp "$BACKUP_DIR/op_instruments.csv" "$ETF_DIR/"
    ok "restored op_instruments.csv"
fi

# Restore kite_access_token.json
if [ -f "$BACKUP_DIR/kite_access_token.json" ]; then
    cp "$BACKUP_DIR/kite_access_token.json" "$ETF_DIR/"
    ok "restored kite_access_token.json"
fi

# Ensure logs directory exists
mkdir -p "$ETF_DIR/logs"
if [ -d "$BACKUP_DIR/logs" ]; then
    cp -r "$BACKUP_DIR/logs/"* "$ETF_DIR/logs/" 2>/dev/null || true
    ok "restored logs/"
fi

echo "Restored: $restored files + directories"

# ==========================================================================
# STEP 5: Verify cron-referenced files
# ==========================================================================
echo ""
echo "--- Step 5: Verifying cron-referenced files ---"

ALL_OK=true

# Files directly called by cron
CRON_SCRIPTS=(
    "option_buy.py"
    "get_instruments.py"
    "get_op_instruments.py"
    "neo_login.py"
    "extract_data.py"
    "tracker.py"
    "rs.py"
)

for f in "${CRON_SCRIPTS[@]}"; do
    if [ -f "$ETF_DIR/$f" ]; then
        ok "$f"
    else
        fail "$f — CRON JOB WILL FAIL"
        ALL_OK=false
    fi
done

# Dependencies imported by cron scripts
DEPS=(
    "common.py"
    "indicators.py"
    "kite_trade.py"
    "neo/__init__.py"
    "neo/orders.py"
    "neo/config.py"
    "neo/session.py"
    "neo/tracker.py"
    "neo/symbols.py"
    "neo/tick.py"
    "neo/fields.py"
    "neo/audit.py"
    "neo/state.py"
    "config_trade.yaml"
)

echo ""
echo "--- Dependencies ---"
for f in "${DEPS[@]}"; do
    if [ -f "$ETF_DIR/$f" ]; then
        ok "$f"
    else
        fail "$f — IMPORT WILL FAIL"
        ALL_OK=false
    fi
done

# Kite token (read from shared BOTS path, not local)
KITE_TOKEN="/home/trustit/Desktop/BOTS/data/kite_access_token.json"
echo ""
echo "--- External dependencies ---"
if [ -f "$KITE_TOKEN" ]; then
    ok "Kite token: $KITE_TOKEN"
else
    warn "Kite token missing: $KITE_TOKEN (SNAIL generates this at 8:45)"
fi

# ==========================================================================
# STEP 6: Final directory listing
# ==========================================================================
echo ""
echo "--- Final directory structure ---"
echo ""
echo "Repository root ($REPO_DIR):"
ls "$REPO_DIR" | grep -v "^\."
echo ""
echo "ETF_PI directory:"
ls -la "$ETF_DIR" | tail -n +2
echo ""
echo "ETF_PI/neo/:"
ls "$ETF_DIR/neo/"

# ==========================================================================
# Summary
# ==========================================================================
echo ""
echo "=============================================="
if $ALL_OK; then
    echo -e " ${GREEN}ALL CHECKS PASSED${NC}"
    echo " Cleanup complete. All cron jobs should work."
else
    echo -e " ${RED}SOME CHECKS FAILED${NC}"
    echo " Review the failures above."
    echo " Files with secrets must be manually present on server."
fi
echo ""
echo " Backup preserved at: $BACKUP_DIR"
echo " (safe to delete after verifying everything works)"
echo "=============================================="
