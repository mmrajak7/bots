#!/bin/bash
# Zebra deployment for the Linux server.
# Run AFTER `git pull` brings this file onto the server.
# Idempotent: safe to re-run.
#
# What it does:
#   1. Sanity checks (venv, creds, Helper dir)
#   2. git pull (Helper/zebra + silencing patches)
#   3. Pull zebra_config.json + 4 .md docs from Drive
#   4. Verify zebra imports + `zebra status` runs
#   5. Update crontab: comment magnet/strack/flow lines, add zebra line
#   6. Kill any running old monitors + clear lock files
#   7. Print summary
#
# Required on server (must already be true):
#   /home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python
#   /home/trustit/Desktop/BOTS/data/secret.json   (Drive service-account creds)
#   /home/trustit/Desktop/BOTS/data/kite_access_token.json  (fresh today)

set -euo pipefail

BOTS_DIR=/home/trustit/Desktop/BOTS
HELPER_DIR="$BOTS_DIR/Helper"
VENV="$BOTS_DIR/CROCODILE/venv/bin/python"
ZEBRA_CRON='*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && flock -n /tmp/zebra_monitor.lock ../CROCODILE/venv/bin/python -m zebra run >> logs/cron_zebra.log 2>&1'

step() { echo; echo "=== $1 ==="; }

# ── 1. Sanity checks ─────────────────────────────────────────────────────
step "1. Sanity checks"
[ -d "$HELPER_DIR" ] || { echo "FAIL: $HELPER_DIR not found"; exit 1; }
[ -x "$VENV" ]       || { echo "FAIL: venv python not found at $VENV"; exit 1; }
[ -f "$BOTS_DIR/data/secret.json" ] || { echo "FAIL: Drive credentials missing"; exit 1; }
[ -f "$BOTS_DIR/data/kite_access_token.json" ] || { echo "WARN: Kite token missing"; }
echo "  paths OK"

# ── 2. Git pull ──────────────────────────────────────────────────────────
step "2. Git pull"
cd "$BOTS_DIR"
git pull --rebase

# ── 3. Pull zebra_config.json + .md docs from Drive ──────────────────────
step "3. Pull config + docs from Drive"
cd "$HELPER_DIR"
"$VENV" - <<'PYEOF'
from bcs.drive_store import get_drive_service, find_file, _extract_service
from googleapiclient.http import MediaIoBaseDownload
from pathlib import Path
import json, io

FOLDER_ID = '1gikSnfw7jI-KMB31SVjwGHTvDVcPpNX1'
creds = Path('/home/trustit/Desktop/BOTS/data/secret.json')
svc_ref = get_drive_service(creds)
service = _extract_service(svc_ref)


def download_bytes(file_id):
    """Raw download — works for any JSON shape (dict or list) or binary."""
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# Required: zebra_config.json (a dict — can't use bcs.download_json which is list-only)
fid = find_file(svc_ref, FOLDER_ID, 'config_zebra_config.json')
if not fid:
    raise SystemExit('  FAIL: config_zebra_config.json not on Drive — push from local first')
cfg = json.loads(download_bytes(fid).decode('utf-8'))
# Force the linux credentials path (in case the source had a Windows-only path)
cfg.setdefault('google_drive', {})['credentials_path_linux'] = '/home/trustit/Desktop/BOTS/data/secret.json'
Path('config').mkdir(exist_ok=True)
with open('config/zebra_config.json', 'w') as f:
    json.dump(cfg, f, indent=2)
print('  config/zebra_config.json pulled')

# Optional: .md docs
for drive_name, local_path in [
    ('CLAUDE.md', 'CLAUDE.md'),
    ('playbook_CLAUDE.md', 'playbook/CLAUDE.md'),
    ('zebra_PLAYBOOK.md', 'zebra/PLAYBOOK.md'),
    ('zebra_SERVER_SETUP.md', 'zebra/SERVER_SETUP.md'),
]:
    fid = find_file(svc_ref, FOLDER_ID, drive_name)
    if not fid:
        print(f'  {local_path}: skip (not on Drive)')
        continue
    body = download_bytes(fid)
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    with open(local_path, 'wb') as f:
        f.write(body)
    print(f'  {local_path} pulled')
PYEOF

# ── 4. Verify zebra package ──────────────────────────────────────────────
step "4. Verify zebra"
cd "$HELPER_DIR"
"$VENV" -c "from zebra import config, scanner, strikes, monitor, trade_store; print('  imports OK')"
"$VENV" -m zebra status

# ── 5. Update crontab ────────────────────────────────────────────────────
step "5. Update crontab"
TMP_CRON=$(mktemp)
crontab -l 2>/dev/null > "$TMP_CRON" || true

# Comment out old systems (idempotent: only commented if not already)
sed -i -E 's|^([^#]*python -m playbook\.magnet)|# (zebra) \1|' "$TMP_CRON"
sed -i -E 's|^([^#]*python -m flow run)|# (zebra) \1|' "$TMP_CRON"

# Add zebra line if missing
if grep -qF "python -m zebra run" "$TMP_CRON"; then
    echo "  zebra cron already present"
else
    echo "$ZEBRA_CRON" >> "$TMP_CRON"
    echo "  zebra cron added"
fi

crontab "$TMP_CRON"
rm "$TMP_CRON"

# ── 6. Stop old monitors ─────────────────────────────────────────────────
step "6. Stop old monitors"
if pkill -f "playbook.magnet" 2>/dev/null; then
    echo "  killed magnet/strack processes"
else
    echo "  no magnet/strack processes running"
fi
if pkill -f "python -m flow" 2>/dev/null; then
    echo "  killed flow processes"
else
    echo "  no flow processes running"
fi
rm -f /tmp/magnet_monitor.lock /tmp/strack.lock /tmp/flow_monitor.lock 2>/dev/null || true
echo "  cleared lock files"

# ── 7. Archive old logs + stores ─────────────────────────────────────────
step "7. Archive deprecated logs + stores"
ARCHIVE_DIR="$HELPER_DIR/logs/archive/2026-05-11"
mkdir -p "$ARCHIVE_DIR"
moved=0

# Specific files (stores + cron logs from deprecated systems)
for f in \
    magnet_trades.json \
    confidence_tracker.json \
    spot_tracker.json \
    flow_trades.json \
    cron_magnet.log \
    cron_flow.log \
    strack.log \
    magnet_dashboard.html \
    magnet_alert_analysis.csv \
    magnet_alert_analysis_v2.csv \
    magnet_entry_levels_sim.csv \
    magnet_wider_entry_sim.csv \
; do
    src="$HELPER_DIR/logs/$f"
    if [ -e "$src" ]; then
        mv "$src" "$ARCHIVE_DIR/$f"
        moved=$((moved + 1))
    fi
done

# Pattern-matched dailies (magnet_YYYYMMDD.log etc.)
shopt -s nullglob
for f in "$HELPER_DIR/logs/magnet_"*.log; do
    mv "$f" "$ARCHIVE_DIR/" && moved=$((moved + 1))
done
shopt -u nullglob

echo "  moved $moved file(s) to $ARCHIVE_DIR"
if [ "$moved" -gt 0 ]; then
    echo "  archive contents (head):"
    ls -la "$ARCHIVE_DIR" | head -15 | sed 's/^/    /'
fi

# ── 8. Summary ───────────────────────────────────────────────────────────
step "8. Summary"
echo "  crontab — zebra/magnet/flow lines:"
crontab -l | grep -E "zebra|magnet|flow" | sed 's/^/    /' || echo "    (none)"
echo
echo "  Next 5-min market-hours tick will start zebra. Tail the log:"
echo "    tail -f $HELPER_DIR/logs/cron_zebra.log"
echo
echo "  Manual sanity:"
echo "    pgrep -f 'zebra run' && echo RUNNING || echo NOT RUNNING"
echo "    $VENV -m zebra status"
echo
echo "Done."
