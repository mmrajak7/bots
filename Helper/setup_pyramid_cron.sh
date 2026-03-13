#!/bin/bash
# Setup Pyramid SL breach monitor on server
# Run: bash setup_pyramid_cron.sh

set -e
VENV="/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python"
HELPER="/home/trustit/Desktop/BOTS/Helper"
DRIVE_FOLDER="1gikSnfw7jI-KMB31SVjwGHTvDVcPpNX1"
CREDS="/home/trustit/Desktop/BOTS/data/secret.json"

echo "=========================================="
echo "  Pyramid Cron Setup"
echo "=========================================="

# 1. Pull latest code
echo ""
echo "[1/7] Pulling latest from GitHub..."
cd /home/trustit/Desktop/BOTS
git stash 2>/dev/null || true
git pull --ff-only || git pull
git stash pop 2>/dev/null || true
echo "  OK"

# 2. Download config files from Drive (they're gitignored)
echo ""
echo "[2/7] Downloading config files from Drive..."
mkdir -p "$HELPER/config"
cd "$HELPER"
$VENV -c "
import sys; sys.path.insert(0, '$HELPER')
from bcs.drive_store import get_drive_service, find_file
from googleapiclient.http import MediaIoBaseDownload
from pathlib import Path
import io, json

service = get_drive_service(Path('$CREDS'))
folder = '$DRIVE_FOLDER'

configs = [
    'config_pyramid_config.json',
    'config_portfolio_config.json',
    'config_bcs_config.json',
    'config_fallen_hero_config.json',
    'config_watchlist_config.json',
    'config_scanner_config.json',
]

for drive_name in configs:
    local_name = drive_name.replace('config_', 'config/', 1)
    fid = find_file(service, folder, drive_name)
    if not fid:
        print(f'  {local_name}: not on Drive, skipping')
        continue
    request = service.files().get_media(fileId=fid)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    data = json.loads(buf.read().decode('utf-8'))
    with open(local_name, 'w') as f:
        json.dump(data, f, indent=2)
    print(f'  {local_name}: OK')
"
echo "  OK"

# 3. Verify pyramid imports + Drive
echo ""
echo "[3/7] Verifying pyramid package..."
cd "$HELPER"
$VENV -c "
from playbook.pyramid import get_pyramid_store
s = get_pyramid_store()
print(f'  Positions: {len(s.load_positions())}, Drive: {s._drive_enabled}')
"
echo "  OK"

# 4. Verify QSK814 token
echo ""
echo "[4/7] Verifying QSK814 token (FIFTY)..."
$VENV -c "
from pathlib import Path
import json
p = Path('$HELPER') / '..' / 'FIFTY' / 'data' / 'kite_access_token.json'
p = p.resolve()
if not p.exists():
    print(f'  WARN: {p} not found, will fall back to YL6478')
else:
    with open(p) as f: t = json.load(f)
    print(f'  Account: {t[\"user_id\"]}')
    print(f'  Generated: {t[\"generated_at\"]}')
"
echo "  OK"

# 5. Test breach check
echo ""
echo "[5/7] Testing breach check..."
cd "$HELPER"
$VENV -m playbook.pyramid breach
echo "  OK"

# 6. Create logs dir if missing
echo ""
echo "[6/7] Ensuring logs directory..."
mkdir -p "$HELPER/logs"
echo "  OK"

# 7. Add/replace cron jobs (removes old pyramid entries, adds fresh)
echo ""
echo "[7/7] Setting up cron jobs..."

BREACH_CRON="*/5 9-15 * * 1-5 cd $HELPER && ../CROCODILE/venv/bin/python -m playbook.pyramid breach >> logs/cron_pyramid.log 2>&1"
CHECK_CRON="0 10 2 * * cd $HELPER && ../CROCODILE/venv/bin/python -m playbook.pyramid check >> logs/cron_pyramid.log 2>&1"

# Remove any existing pyramid cron entries, then add fresh
(
    crontab -l 2>/dev/null | grep -v "playbook.pyramid"
    echo ""
    echo "# Pyramid SL Breach Monitor — every 5 min, Mon-Fri 9-15 IST"
    echo "# De-duplicated: alerts once per position per day, skips outside 9:15-15:30"
    echo "$BREACH_CRON"
    echo ""
    echo "# Pyramid Month-End Check — 2nd of every month at 10:00 AM"
    echo "# Updates trailing SLs, flags L2/L3 level triggers, sends Telegram summary"
    echo "$CHECK_CRON"
) | crontab -
echo "  Breach monitor: added (every 5 min, Mon-Fri)"
echo "  Month-end check: added (2nd of each month, 10 AM)"

# Verify
echo ""
echo "=========================================="
echo "  DONE. Verifying cron:"
echo "=========================================="
crontab -l | grep -A1 pyramid
echo ""
echo "Telegram alerts -> chat_id REDACTED_CHAT_ID"
