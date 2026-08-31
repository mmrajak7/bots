"""Age out the log directory without ever touching what lives beside the logs.

    python -m common.log_cleanup                 # DRY RUN, the default
    python -m common.log_cleanup --apply         # actually do it
    python -m common.log_cleanup --compress-after 7 --delete-after 90 --apply

THE HAZARD, stated first because it is the whole design. `logs/` is not a log
directory. It is the TRADE STORE directory that also happens to hold logs:
`zebra_trades.json` (the book), `bcs_trades.json`, `fallen_hero_trades.json`,
the `.lock` files that serialise two writer processes, the `.nextid.json`
high-water marks that stop a quarantine reissuing ids, and the corruption
markers. Deleting the wrong file here does not cost disk space, it costs the
book -- and this repo has already lost three weeks of cohort evidence to a
script that looked safe and said so in its own comments
(`incident_deploy_reset_2026_08_30`).

So the rule is an ALLOWLIST, and it is deliberately narrow:

  * ONE suffix is eligible for compression: `.log`.
  * ONE suffix is eligible for deletion: `.log.gz`, and only one this tool
    made. A `.json` cannot be reached by any code path here.
  * A denylist would be the wrong shape. It answers "is this file dangerous",
    which requires knowing every dangerous name that will ever exist. The
    allowlist answers "is this file a log", which is a closed question.

COMPRESS BEFORE DELETE, because logs are EVIDENCE in this system. The exit book
under an option position cannot be reconstructed after the fact, the POLL line
is the only record of what the engine saw at 14:35, and a real NSE holiday was
identified from log SIZES. gzip on these files runs ~90-95%, so the default
policy keeps every byte of the last three months while reclaiming almost all of
the space. Deletion is the far edge, not the mechanism.

NEVER touches a file being appended right now: eligibility is by mtime, and a
live log's mtime is the current second. That is why the tool needs no knowledge
of which processes are running, and why rotating a live file -- which loses
whatever the writer's open fd emits next -- is not attempted at all. The fix
for a growing un-dated log is a date-stamped cron redirect, not rotation here.

The archive INHERITS the log's mtime, so retention tracks the CONTENT's age
rather than the moment the cleaner happened to run. One consequence is worth
stating: a `.log` ALREADY older than `--delete-after` converges in two runs --
compressed on the first, deleted on the second -- because eligibility is
evaluated per suffix. That is deliberate. The alternative, deleting a plain
`.log` outright, is a delete path that never passes through an archive, and
this file's whole argument is that evidence gets compressed before it gets
removed.

SAFE-TO-RERUN: dry run by default, and under --apply it converges -- a second
run finds the compressed files under a different suffix and the deleted ones
absent. Steady state is reached in at most two passes and holds thereafter.
"""
from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sys
import time
from pathlib import Path

#: The ONLY suffix that may be compressed, and the only one that may be
#: deleted. Both are checked with `str.endswith` on the full name so that
#: `zebra_trades.json` cannot match by any spelling.
COMPRESSIBLE = '.log'
DELETABLE = '.log.gz'

#: Belt and braces over the allowlist. Nothing here can match `.log` anyway --
#: these names are listed so that a future edit widening the allowlist trips a
#: test instead of the book. If you are adding a suffix, add it here too.
NEVER_TOUCH = (
    '.json', '.jsonl', '.lock', '.tmp', '.py', '.md', '.csv', '.db',
)

#: A directory that does not contain at least one of these is not the logs
#: directory, and the tool refuses rather than aging out whatever it found.
#: Cheap, and it turns a mistyped `--dir` into a refusal instead of damage.
EXPECTED_MARKERS = ('zebra_trades.json', 'bcs_trades.json', 'cron_bcs.log')

DEFAULT_COMPRESS_AFTER_DAYS = 7
DEFAULT_DELETE_AFTER_DAYS = 90


def _default_log_dir() -> Path:
    return Path(__file__).resolve().parents[1] / 'logs'


def is_compressible(name: str) -> bool:
    """A plain `.log`, and nothing else. `.log.gz` is already done."""
    if name.endswith(DELETABLE):
        return False
    return name.endswith(COMPRESSIBLE) and not _forbidden(name)


def is_deletable(name: str) -> bool:
    """Only an archive this tool created."""
    return name.endswith(DELETABLE) and not _forbidden(name)


def _forbidden(name: str) -> bool:
    return any(name.endswith(s) for s in NEVER_TOUCH)


def _age_days(path: Path, now: float) -> float:
    return (now - path.stat().st_mtime) / 86400.0


def _human(n: int) -> str:
    f = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if f < 1024 or unit == 'GB':
            return f'{f:.1f} {unit}'
        f /= 1024
    return f'{f:.1f} GB'


def plan(log_dir: Path, compress_after: float, delete_after: float,
         now: float | None = None) -> dict:
    """What WOULD happen. Pure: reads mtimes and sizes, changes nothing.

    Split from `run` so the dry run and the real run cannot disagree about what
    is eligible -- the dry run is not a separate code path that approximates
    the real one, it is the real one with the writes withheld.
    """
    now = time.time() if now is None else now
    to_compress, to_delete, skipped = [], [], []
    for p in sorted(log_dir.iterdir()):
        if p.is_dir():
            continue                      # archive/, eod/ are left alone
        try:
            age = _age_days(p, now)
        except OSError:
            continue
        if is_deletable(p.name):
            (to_delete if age >= delete_after else skipped).append((p, age))
        elif is_compressible(p.name):
            (to_compress if age >= compress_after else skipped).append((p, age))
        else:
            skipped.append((p, age))
    return {'compress': to_compress, 'delete': to_delete, 'skipped': skipped}


def run(log_dir: Path | None = None,
        compress_after: float = DEFAULT_COMPRESS_AFTER_DAYS,
        delete_after: float = DEFAULT_DELETE_AFTER_DAYS,
        apply: bool = False, now: float | None = None,
        out=print) -> dict:
    """Report, and act only when `apply` is true. Returns the plan plus totals."""
    log_dir = Path(log_dir) if log_dir else _default_log_dir()
    if not log_dir.is_dir():
        raise SystemExit(f'not a directory: {log_dir}')
    if not any((log_dir / m).exists() for m in EXPECTED_MARKERS):
        raise SystemExit(
            f'{log_dir} does not look like the Helper logs directory (none of '
            f'{", ".join(EXPECTED_MARKERS)} found). Refusing, rather than '
            f'ageing out whatever happens to live there.')

    p = plan(log_dir, compress_after, delete_after, now)
    reclaimed = 0
    # ASCII only: this prints into a cron log on a Pi whose console encoding is
    # not UTF-8, and an em-dash there arrives as a replacement character.
    mode = 'APPLY' if apply else 'DRY RUN - nothing will change; pass --apply'
    out(f'log cleanup: {log_dir}  [{mode}]')
    out(f'  compress .log older than {compress_after:g}d, '
        f'delete .log.gz older than {delete_after:g}d')

    for path, age in p['compress']:
        before = path.stat().st_size
        gz = path.with_name(path.name + '.gz')
        if apply:
            try:
                st = path.stat()
                with open(path, 'rb') as src, gzip.open(gz, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                # THE ARCHIVE INHERITS THE LOG'S OWN mtime. Without this the
                # retention clock RESTARTS at whatever moment the tool
                # happened to run, so "delete after 90 days" would silently
                # mean "90 days after I got round to compressing it" and a
                # log's real age would become unknowable once archived.
                # Retention must track the CONTENT's age, not the cleaner's
                # schedule.
                os.utime(gz, (st.st_atime, st.st_mtime))
                after = gz.stat().st_size
                path.unlink()          # only after the archive is closed
            except OSError as e:
                out(f'  SKIP {path.name}: {e}')
                if gz.exists():
                    gz.unlink()        # never leave a half-written archive
                continue
        else:
            after = 0
        reclaimed += before - after
        out(f'  gzip   {path.name:<44} {_human(before):>9}  ({age:.0f}d)')

    for path, age in p['delete']:
        size = path.stat().st_size
        if apply:
            try:
                path.unlink()
            except OSError as e:
                out(f'  SKIP {path.name}: {e}')
                continue
        reclaimed += size
        out(f'  DELETE {path.name:<44} {_human(size):>9}  ({age:.0f}d)')

    if not p['compress'] and not p['delete']:
        out('  nothing to do')
    out(f'  {len(p["skipped"])} file(s) untouched '
        f'(too recent, or not a log)')
    out(f'  reclaimed: ~{_human(max(reclaimed, 0))}'
        + ('' if apply else ' if applied'))
    p['reclaimed'] = reclaimed
    return p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='Compress and age out Helper logs. Dry run by default.')
    ap.add_argument('--dir', default=None, help='log directory')
    ap.add_argument('--compress-after', type=float,
                    default=DEFAULT_COMPRESS_AFTER_DAYS,
                    help=f'gzip .log older than N days '
                         f'(default {DEFAULT_COMPRESS_AFTER_DAYS})')
    ap.add_argument('--delete-after', type=float,
                    default=DEFAULT_DELETE_AFTER_DAYS,
                    help=f'delete .log.gz older than N days '
                         f'(default {DEFAULT_DELETE_AFTER_DAYS})')
    ap.add_argument('--apply', action='store_true',
                    help='actually compress and delete (default: dry run)')
    a = ap.parse_args(argv)
    if a.delete_after <= a.compress_after:
        raise SystemExit(
            'refusing: --delete-after must exceed --compress-after, or a log '
            'would be created as an archive and removed on the same run, '
            'which is deletion wearing compression as a disguise.')
    run(a.dir, a.compress_after, a.delete_after, a.apply)
    return 0


if __name__ == '__main__':
    sys.exit(main())
