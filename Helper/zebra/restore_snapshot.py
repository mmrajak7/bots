"""Restore the trade store from an archived snapshot, and make it STICK.

WHY THIS EXISTS
---------------
2026-08-30: `zebra/deploy_server.sh` step 3 runs `zebra reset --confirm`, a
ONE-TIME hygiene step from the 2026-05-11 first deployment. Re-running the
deploy script on a live book force-closed all six open cohort positions at
-100% under `reset_force_close` and cancelled three signals. Paper records, so
no money — but three weeks of the evidence the arming gate is waiting on.

The script archives before it resets, so the good state exists. Restoring it is
NOT a file copy, and that is the whole reason this tool is a tool:

**`ZebraStore._merge` resolves by VERSION, higher wins.** The reset incremented
every version it touched and pushed them to Drive, so a restored file carrying
the OLD versions loses the next sync and the reset silently comes back. The
restore has to out-version what it is replacing.

WHAT IT DOES
------------
For every record in the snapshot, writes the snapshot's content with
`version = max(snapshot, live) + 1`. Records that exist only in the live store
are left alone — a restore is not a rollback of everything that happened since.

DRY RUN BY DEFAULT. Nothing is written without `--apply`.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def _load(path: Path) -> list:
    with io.open(path, encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit('%s is not a trade list' % path)
    return data


def plan(snapshot: list, live: list) -> dict:
    """What the restore would change. Pure; no I/O."""
    live_by_id = {t['id']: t for t in live}
    snap_by_id = {t['id']: t for t in snapshot}
    changes, unchanged = [], 0
    for tid, snap in sorted(snap_by_id.items()):
        cur = live_by_id.get(tid)
        if cur is None:
            changes.append({'id': tid, 'stock': snap.get('stock'),
                            'from': '(absent)', 'to': snap.get('status'),
                            'reason': None})
            continue
        same_status = cur.get('status') == snap.get('status')
        same_exit = cur.get('exit_reason') == snap.get('exit_reason')
        if same_status and same_exit:
            unchanged += 1
            continue
        changes.append({'id': tid, 'stock': snap.get('stock'),
                        'from': cur.get('status'), 'to': snap.get('status'),
                        'reason': cur.get('exit_reason')})
    only_live = sorted(set(live_by_id) - set(snap_by_id))
    return {'changes': changes, 'unchanged': unchanged, 'only_live': only_live}


def rebuild(snapshot: list, live: list) -> list:
    """The store as it should be written, out-versioned so it wins a merge."""
    live_by_id = {t['id']: t for t in live}
    out = []
    for snap in snapshot:
        rec = dict(snap)
        cur = live_by_id.get(rec['id'])
        floor = max(int(rec.get('version') or 0),
                    int((cur or {}).get('version') or 0))
        rec['version'] = floor + 1
        rec['restored_from_snapshot_at'] = datetime.now().isoformat(
            timespec='seconds')
        out.append(rec)
    seen = {t['id'] for t in out}
    # A record that exists only in the LIVE store stays. A restore undoes a
    # known incident; it does not roll back everything that came after.
    for tid, rec in live_by_id.items():
        if tid not in seen:
            out.append(rec)
    return sorted(out, key=lambda t: t['id'])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('snapshot', help='archived zebra_trades_*.json')
    ap.add_argument('--apply', action='store_true',
                    help='actually write (default: show the plan only)')
    ap.add_argument('--no-drive', action='store_true',
                    help='write locally only; Drive keeps the current state')
    args = ap.parse_args(argv)

    from zebra import config as cfg
    snap_path = Path(args.snapshot)
    if not snap_path.exists():
        print('snapshot not found: %s' % snap_path)
        return 1
    snapshot = _load(snap_path)
    live = _load(cfg.LOCAL_FILE) if cfg.LOCAL_FILE.exists() else []

    p = plan(snapshot, live)
    print()
    print('SNAPSHOT %s  (%d records)' % (snap_path.name, len(snapshot)))
    print('LIVE     %s  (%d records)' % (cfg.LOCAL_FILE.name, len(live)))
    print()
    if not p['changes']:
        print('Nothing to restore — the live store already matches.')
        return 0
    print('%-6s %-14s %-12s %-12s %s'
          % ('id', 'stock', 'live now', 'restore to', 'live exit reason'))
    print('-' * 68)
    for c in p['changes']:
        print('%-6s %-14s %-12s %-12s %s'
              % (c['id'], str(c['stock'])[:14], c['from'], c['to'],
                 c['reason'] or ''))
    print()
    print('%d record(s) restored, %d already match, %d live-only left alone.'
          % (len(p['changes']), p['unchanged'], len(p['only_live'])))
    print('Versions are raised above the live ones so the restore WINS the '
          'next Drive merge.')

    if not args.apply:
        print()
        print('DRY RUN. Nothing written. Re-run with --apply to commit.')
        return 0

    backup = cfg.LOG_DIR / ('zebra_trades_pre_restore_%s.json'
                            % datetime.now().strftime('%Y%m%d_%H%M%S'))
    if cfg.LOCAL_FILE.exists():
        shutil.copy2(cfg.LOCAL_FILE, backup)
        print()
        print('Backed up the CURRENT store -> %s' % backup)

    merged = rebuild(snapshot, live)
    from zebra.trade_store import get_store
    store = get_store()
    with store._mutate(drive=not args.no_drive):
        store._trades = merged
    print('Restored %d record(s). Drive: %s'
          % (len(merged), 'skipped' if args.no_drive else 'updated'))
    print()
    print('Verify:  python -m zebra status')
    return 0


if __name__ == '__main__':
    sys.exit(main())
