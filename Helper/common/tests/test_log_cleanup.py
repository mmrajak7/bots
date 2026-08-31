"""The log cleaner must not be able to reach the book.

`logs/` is not a log directory. It is the TRADE STORE directory that also holds
logs: `zebra_trades.json` is the book, the `.lock` files serialise two writer
processes, and the `.nextid.json` marks stop a quarantine reissuing ids. This
repo has already lost three weeks of cohort evidence to a script that looked
safe and said so in its own comments (`incident_deploy_reset_2026_08_30`), so
the tests here are mostly about what the tool CANNOT do.

The allowlist is the design under test. A denylist answers "is this file
dangerous", which needs every dangerous name that will ever exist; the
allowlist answers "is this file a log", which is closed. `test_the_never_touch
_list_cannot_be_reached` pins that the two agree, so widening one without the
other fails here rather than in `logs/`.

Run:  cd Helper && python -m pytest common/tests/test_log_cleanup.py -v
"""
import gzip
import os
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import log_cleanup as lc                              # noqa: E402

DAY = 86400.0
NOW = 1_800_000_000.0


def _write(d: Path, name: str, content=b'x' * 4096, age_days=0.0):
    p = d / name
    p.write_bytes(content)
    t = NOW - age_days * DAY
    os.utime(p, (t, t))
    return p


@pytest.fixture
def logdir(tmp_path):
    """A directory shaped like the real one, book and all."""
    _write(tmp_path, 'zebra_trades.json', b'[{"id": 1}]', age_days=400)
    _write(tmp_path, 'bcs_trades.json', b'[]', age_days=400)
    _write(tmp_path, 'zebra_trades.lock', b'', age_days=400)
    _write(tmp_path, 'bcs_trades.nextid.json', b'{"n": 9}', age_days=400)
    _write(tmp_path, 'zebra_store_corrupt.json', b'{}', age_days=400)
    _write(tmp_path, 'order_intents_20260830.jsonl', b'{}', age_days=400)
    return tmp_path


# -- what it must never do --------------------------------------------------

def test_the_book_is_never_compressed_or_deleted(logdir):
    """THE hazard. Every store file is 400 days old — maximally eligible if the
    tool were age-driven rather than allowlist-driven."""
    p = lc.plan(logdir, 7, 90, now=NOW)
    touched = {q.name for q, _ in p['compress'] + p['delete']}
    assert touched == set(), f'tool selected non-log files: {touched}'


def test_apply_leaves_every_store_file_byte_identical(logdir):
    _write(logdir, 'cron_zebra_20260101.log', age_days=200)
    before = {q.name: q.read_bytes() for q in logdir.iterdir()
              if not q.name.endswith('.log')}
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    after = {q.name: q.read_bytes() for q in logdir.iterdir()
             if not q.name.endswith('.log') and not q.name.endswith('.log.gz')}
    assert after == before


def test_the_never_touch_list_cannot_be_reached(logdir):
    """Pins that the allowlist and the belt-and-braces denylist AGREE.

    If someone widens `COMPRESSIBLE` to a suffix on `NEVER_TOUCH`, this fails
    here rather than in the real logs directory.
    """
    for suffix in lc.NEVER_TOUCH:
        name = 'anything' + suffix
        assert not lc.is_compressible(name), suffix
        assert not lc.is_deletable(name), suffix


def test_a_file_being_appended_right_now_is_left_alone(logdir):
    """A live log's mtime is the current second, so age alone excludes it.
    This is why the tool needs no knowledge of which processes are running."""
    _write(logdir, 'cron_bcs.log', age_days=0.0)
    p = lc.plan(logdir, 7, 90, now=NOW)
    assert 'cron_bcs.log' not in {q.name for q, _ in p['compress']}


def test_a_recent_log_is_not_compressed(logdir):
    _write(logdir, 'cron_zebra_20260830.log', age_days=3)
    p = lc.plan(logdir, 7, 90, now=NOW)
    assert not p['compress']


def test_directories_are_skipped(logdir):
    (logdir / 'archive').mkdir()
    (logdir / 'archive' / 'old.log').write_bytes(b'x')
    p = lc.plan(logdir, 7, 90, now=NOW)
    names = {q.name for q, _ in p['compress'] + p['delete']}
    assert 'archive' not in names and 'old.log' not in names


# -- dry run is the default and is really dry -------------------------------

def test_dry_run_changes_nothing(logdir):
    _write(logdir, 'cron_zebra_20260101.log', age_days=200)
    before = sorted(q.name for q in logdir.iterdir())
    lc.run(logdir, 7, 90, apply=False, now=NOW, out=lambda *a: None)
    assert sorted(q.name for q in logdir.iterdir()) == before


def test_apply_defaults_to_false(logdir):
    """The signature itself must default to safe — a caller that forgets the
    keyword gets a report, not a deletion."""
    _write(logdir, 'cron_zebra_20260101.log', age_days=200)
    lc.run(logdir, 7, 90, now=NOW, out=lambda *a: None)
    assert (logdir / 'cron_zebra_20260101.log').exists()


# -- what it must do --------------------------------------------------------

def test_an_old_log_is_gzipped_and_the_content_survives(logdir):
    body = b'POLL #423 TMPV spot=316.35\n' * 500
    _write(logdir, 'cron_zebra_20260101.log', body, age_days=200)
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    gz = logdir / 'cron_zebra_20260101.log.gz'
    assert gz.exists()
    assert not (logdir / 'cron_zebra_20260101.log').exists()
    assert gzip.open(gz, 'rb').read() == body, 'evidence must survive intact'


def test_an_ancient_archive_is_deleted(logdir):
    p = logdir / 'cron_zebra_20250101.log.gz'
    with gzip.open(p, 'wb') as f:
        f.write(b'old')
    t = NOW - 200 * DAY
    os.utime(p, (t, t))
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert not p.exists()


def test_a_gz_inside_the_keep_window_survives(logdir):
    p = logdir / 'cron_zebra_20260801.log.gz'
    with gzip.open(p, 'wb') as f:
        f.write(b'recent')
    t = NOW - 30 * DAY
    os.utime(p, (t, t))
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert p.exists()


def test_a_gz_is_not_re_compressed(logdir):
    """`.log.gz` ends with neither a bare `.log` nor anything compressible —
    otherwise each run would nest another gzip layer forever."""
    assert not lc.is_compressible('cron_zebra_20260101.log.gz')


# -- idempotence, which is what SAFE-TO-RERUN claims ------------------------

def test_running_twice_changes_nothing_the_second_time(logdir):
    """A log inside the keep window: compressed once, then left alone."""
    _write(logdir, 'cron_zebra_20260801.log', age_days=30)
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    snap = {q.name: q.read_bytes() for q in logdir.iterdir()}
    second = lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert {q.name: q.read_bytes() for q in logdir.iterdir()} == snap
    assert not second['compress'] and not second['delete']


def test_the_archive_inherits_the_logs_mtime(logdir):
    """Retention must track the CONTENT's age, not the cleaner's schedule.

    Without this the clock RESTARTS on compression, so "delete after 90 days"
    silently becomes "90 days after I got round to compressing it" and a log's
    real age is unknowable once archived.
    """
    _write(logdir, 'cron_zebra_20260801.log', age_days=30)
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    gz = logdir / 'cron_zebra_20260801.log.gz'
    assert abs(lc._age_days(gz, NOW) - 30) < 0.01


def test_a_log_past_the_delete_window_converges_in_two_passes(logdir):
    """Documented, not accidental. Eligibility is per suffix, so a `.log`
    already older than --delete-after is compressed on pass one and deleted on
    pass two. The alternative -- deleting a plain `.log` outright -- is a
    delete path that never passes through an archive, and the whole argument
    of this tool is that evidence is compressed before it is removed."""
    _write(logdir, 'cron_zebra_20250101.log', age_days=200)
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert (logdir / 'cron_zebra_20250101.log.gz').exists()
    lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert not (logdir / 'cron_zebra_20250101.log.gz').exists()
    third = lc.run(logdir, 7, 90, apply=True, now=NOW, out=lambda *a: None)
    assert not third['compress'] and not third['delete'], 'must be stable'
    assert (logdir / 'zebra_trades.json').exists(), 'book untouched throughout' 


# -- refusals ---------------------------------------------------------------

def test_it_refuses_a_directory_that_is_not_the_logs_dir(tmp_path):
    """A mistyped --dir must be a refusal, not an ageing-out of whatever lives
    there. The markers are cheap and turn a typo into an error."""
    (tmp_path / 'important.log').write_bytes(b'x')
    with pytest.raises(SystemExit):
        lc.run(tmp_path, 7, 90, apply=True, now=NOW, out=lambda *a: None)


def test_delete_after_must_exceed_compress_after():
    """Equal windows would gzip a file and remove it on the same run — that is
    deletion wearing compression as a disguise, and the whole reason to
    compress first is that logs are evidence."""
    with pytest.raises(SystemExit):
        lc.main(['--compress-after', '30', '--delete-after', '30', '--apply'])


def test_a_missing_directory_refuses(tmp_path):
    with pytest.raises(SystemExit):
        lc.run(tmp_path / 'nope', 7, 90, out=lambda *a: None)


# -- reporting --------------------------------------------------------------

def test_the_dry_run_reports_what_it_would_reclaim(logdir):
    _write(logdir, 'cron_zebra_20260101.log', b'y' * 200_000, age_days=200)
    lines = []
    p = lc.run(logdir, 7, 90, apply=False, now=NOW, out=lines.append)
    assert p['reclaimed'] > 0
    assert any('DRY RUN' in ln for ln in lines)
    assert any('if applied' in ln for ln in lines)
