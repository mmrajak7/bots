#!/bin/bash
# Zebra PULL — step 1 of the deployment.
#
# Does:
#   1. git pull (brings latest code + zebra/*.sh + zebra/*.py)
#   2. Drive download for gitignored config + .md docs
#
# After this, run step 2:
#   bash Helper/zebra/deploy_server.sh
#
# Idempotent: re-running is safe.

set -euo pipefail

BOTS_DIR=/home/trustit/Desktop/BOTS
HELPER_DIR="$BOTS_DIR/Helper"
VENV="$BOTS_DIR/CROCODILE/venv/bin/python"

step() { echo; echo "=== $1 ==="; }

# ── 1. Sanity checks ─────────────────────────────────────────────────────
step "1. Sanity checks"
[ -d "$HELPER_DIR" ] || { echo "FAIL: $HELPER_DIR not found"; exit 1; }
[ -x "$VENV" ]       || { echo "FAIL: venv python not found at $VENV"; exit 1; }
[ -f "$BOTS_DIR/data/secret.json" ] || { echo "FAIL: Drive credentials missing"; exit 1; }
echo "  paths OK"

# ── 2. Git pull ──────────────────────────────────────────────────────────
step "2. Git pull"
cd "$BOTS_DIR"
git pull --rebase

# ── 3. Drive download (gitignored config + docs) ─────────────────────────
step "3. Drive download"
cd "$HELPER_DIR"
"$VENV" - <<'PYEOF'
from bcs.drive_store import get_drive_service, find_file, _extract_service
from googleapiclient.http import MediaIoBaseDownload
from pathlib import Path
import json, io

# THE FOLDER ID IS A SECRET AND THIS FILE IS TRACKED IN A PUBLIC REPO.
#
# It sat inline here as a literal until 2026-08-31. `common/layered_config.py`
# counts "a Drive folder id" among the exact values the two-layer config split
# exists to keep out of git, CLAUDE.md writes it as a POINTER ("Folder ID:
# config/bcs_config.json -> not repeated here") rather than a value, and every
# tracked config file omits it. This script republished it anyway, which is
# the whole point of the rule being mechanical rather than remembered.
#
# Read it the way every other caller does. `config/bcs_config.json` is the
# untracked overlay and is already on the box -- it is what `bcs.trade_store`
# authenticates with, so if it were missing the monitor would be local-only
# and this pull would be the second thing to notice.
from common import layered_config
_bcs_cfg = layered_config.load('bcs_config')
FOLDER_ID = (_bcs_cfg.get('google_drive') or {}).get('folder_id')
if not FOLDER_ID:
    raise SystemExit(
        '  FAIL: google_drive.folder_id missing from config/bcs_config.json.\n'
        '  That overlay is untracked and does not arrive over git -- copy it\n'
        '  to the box, or add the folder id to it, then re-run this script.')
creds = Path('/home/trustit/Desktop/BOTS/data/secret.json')
svc_ref = get_drive_service(creds)
service = _extract_service(svc_ref)


def download_bytes(file_id):
    """Raw download — works for dict, list, or binary."""
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


# Required: zebra_config.json (a dict; bcs.download_json is list-only, so raw here)
fid = find_file(svc_ref, FOLDER_ID, 'config_zebra_config.json')
if not fid:
    raise SystemExit('  FAIL: config_zebra_config.json not on Drive — push from local first')
cfg = json.loads(download_bytes(fid).decode('utf-8'))
# Force the linux credentials path (in case source had a Windows-only path)
cfg.setdefault('google_drive', {})['credentials_path_linux'] = \
    '/home/trustit/Desktop/BOTS/data/secret.json'
Path('config').mkdir(exist_ok=True)
with open('config/zebra_config.json', 'w') as f:
    json.dump(cfg, f, indent=2)
print(f"  config/zebra_config.json pulled (paper_mode={cfg.get('paper_mode')})")

# Optional: .md docs (not required for runtime; nice-to-have on server)
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

# ── 4. Done ──────────────────────────────────────────────────────────────
step "4. Pull done"
echo "  Next step (run on server):"
echo "    bash Helper/zebra/deploy_server.sh"
