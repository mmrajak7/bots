"""The digest is a separate crontab line whose failure mode is SILENCE.

It went uninstalled for 18 days (found 2026-09-01; two of those sessions had
already aged out of the retained logs and are gone permanently), and it failed
again on 2026-09-07. Both times it was found by a human running `ls`.

Nothing else in the fleet notices: `logs/eod/` simply stays empty, which is
indistinguishable from a quiet day. And the cost is on a timer —
`common.log_cleanup` deletes the raw log at 90 days, after which the session
cannot be rebuilt at all.
"""

import json
from datetime import date, datetime, timedelta

import pytest

from zebra import eod_coverage as E


@pytest.fixture(autouse=True)
def weekdays_are_sessions(monkeypatch):
    """Pin the calendar. The real one is a scraped file that changes daily, and
    it carries two known divergences (2026-06-23, 2026-08-26) — a unit test
    that reads it is asserting on today's scrape, not on this module."""
    monkeypatch.setattr(E.nse_holidays, 'is_session', lambda d: d.weekday() < 5)


@pytest.fixture
def eod(tmp_path):
    d = tmp_path / 'eod'
    d.mkdir()
    return d


def _write(d, day, digest=True, paths=True):
    if digest:
        (d / ('%s.json' % day)).write_text('{}')
        (d / ('%s.md' % day)).write_text('#')
    if paths:
        (d / ('paths_%s.json' % day)).write_text('{}')


def _populate(d, no_digest=(), no_paths=()):
    """Fill the whole lookback window, then punch the holes the test is about.

    Written this way rather than as a literal list of dates because the window
    SLIDES: excluding one day pushes `expected_sessions` one further back, and
    a hand-written list then reports a gap the test never intended.
    """
    day = date(2026, 9, 7)
    for _ in range(30):
        if day.weekday() < 5:
            iso = day.isoformat()
            _write(d, iso, digest=iso not in no_digest,
                   paths=iso not in no_paths)
        day -= timedelta(days=1)


#: Mon 2026-09-07 evening, past the 15:47 deadline. Pinned, never `now()`:
#: every threshold here is a clock comparison and a test that reads the wall
#: clock passes or fails by the hour it is run at.
EVENING = datetime(2026, 9, 7, 19, 0)


def test_a_fully_captured_week_is_ok(eod):
    _populate(eod)
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['state'] == 'ok'


def test_todays_missing_digest_is_reported(eod):
    _populate(eod, no_digest={'2026-09-07'}, no_paths={'2026-09-07'})
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['state'] == 'missing'
    assert st['missing'] == ['2026-09-07']


def test_two_or_more_gaps_are_LAPSED_not_merely_missing(eod):
    """One gap is "rebuild it and check the crontab". Two means nobody has been
    watching, and the 90-day deletion clock is already running on the oldest."""
    _populate(eod, no_digest={'2026-09-07', '2026-09-04'},
              no_paths={'2026-09-07', '2026-09-04'})
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['state'] == 'lapsed'
    assert st['missing'] == ['2026-09-07', '2026-09-04']


def test_it_is_SILENT_before_the_digest_is_due(eod):
    """A checker that cries all morning is one nobody reads by afternoon."""
    _populate(eod, no_digest={'2026-09-07'}, no_paths={'2026-09-07'})
    st = E.coverage_status(date(2026, 9, 7), datetime(2026, 9, 7, 12, 0), eod)
    assert st['state'] == 'ok', "today is not due yet, and nothing else is missing"


def test_the_deadline_includes_a_grace_period(eod):
    """The cron fires at 15:47; the run itself takes time. Alerting at 15:47:01
    would report a job that is still writing."""
    _populate(eod, no_digest={'2026-09-07'}, no_paths={'2026-09-07'})
    at_1750 = E.coverage_status(date(2026, 9, 7), datetime(2026, 9, 7, 17, 50), eod)
    assert at_1750['state'] == 'missing'
    at_1548 = E.coverage_status(date(2026, 9, 7), datetime(2026, 9, 7, 15, 48), eod)
    assert at_1548['state'] == 'ok', 'still inside the grace window'


def test_a_holiday_is_not_a_gap(monkeypatch, eod):
    """A real NSE holiday has no session and therefore no digest. Counting it
    would make the checker cry on exactly the days the engine did nothing
    wrong — which is how the STATIC holiday list scored 2 of 6."""
    monkeypatch.setattr(E.nse_holidays, 'is_session',
                        lambda d: d.weekday() < 5 and d.isoformat() != '2026-09-04')
    _populate(eod, no_digest={'2026-09-04'}, no_paths={'2026-09-04'})
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['state'] == 'ok'
    assert '2026-09-04' not in st['missing']


def test_missing_VALUE_PATHS_are_reported_separately(eod):
    """The digest and the path capture ride the SAME crontab line deliberately,
    so a day with one and not the other is a DIFFERENT fault — a job that ran
    and half-failed, not a job that never fired. Flattening them hides it."""
    _populate(eod, no_paths={'2026-09-07'})
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['missing'] == []
    assert st['paths_missing'] == ['2026-09-07']
    assert st['state'] == 'missing'
    assert 'value paths MISSING' in st['detail']


def test_a_PARTIAL_digest_does_not_count_as_coverage(eod):
    """THE REGRESSION, from the 2026-09-07 review.

    The crontab is `0,47 15 * * 1-5` — 15:00 AND 15:47. The 15:00 run writes a
    `partial: true` digest for a session still open. If that counts, this
    module is blind to the exact failure it was written for: the 15:47 line not
    firing on a day the 15:00 one did.
    """
    _populate(eod)
    (eod / '2026-09-07.json').write_text('{"partial": true}')
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['state'] == 'missing'
    assert st['missing'] == ['2026-09-07']


def test_a_COMPLETE_digest_does_count(eod):
    """Negative control for the test above — without it, a typo in the key
    name would make every digest 'partial' and the module would cry daily."""
    _populate(eod)
    (eod / '2026-09-07.json').write_text('{"partial": false}')
    assert E.coverage_status(date(2026, 9, 7), EVENING, eod)['state'] == 'ok'


def test_an_UNREADABLE_digest_does_not_count_as_coverage(eod):
    _populate(eod)
    (eod / '2026-09-07.json').write_text('{ not json')
    assert E.coverage_status(date(2026, 9, 7), EVENING, eod)['state'] == 'missing'


def test_a_STALE_paths_capture_does_not_count_as_coverage(eod, monkeypatch):
    """A capture frozen at a mid-session extraction is not the session —
    `paths_2026-09-03.json` ended at 11:15:16 with 100 observations against
    307-623 on its neighbours."""
    from zebra import value_paths
    monkeypatch.setattr(value_paths, 'capture_is_stale',
                        lambda d: d == '2026-09-07')
    _populate(eod)
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert st['paths_missing'] == ['2026-09-07']
    assert st['state'] == 'missing'


def test_the_detail_names_the_deletion_deadline(eod):
    """A gap found inside 90 days costs one `zebra digest --date`. Outside it,
    the session is unrecoverable — so the alert has to say which clock is
    running, not merely that a file is absent."""
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert 'zebra digest --date' in st['detail']
    assert '90' in st['detail']


def test_a_MISSING_directory_reports_the_gaps(tmp_path):
    st = E.coverage_status(date(2026, 9, 7), EVENING, tmp_path / 'nope')
    assert st['state'] == 'lapsed'


def test_an_UNREADABLE_directory_says_so_and_does_not_raise(tmp_path):
    """A FILE where the directory should be. The previous version of this test
    passed a NONEXISTENT path, which takes the `exists()` branch — so the
    `unreadable` arm it claimed to cover was never executed."""
    blocker = tmp_path / 'eod'
    blocker.write_text('not a directory')
    st = E.coverage_status(date(2026, 9, 7), EVENING, blocker)
    assert st['state'] == 'unreadable'
    assert 'cannot list' in st['detail']


def test_the_lookback_is_BOUNDED(eod):
    """Otherwise it reports the pre-installation era forever and the alert is
    permanent — which is the same fatigue defect one level up."""
    st = E.coverage_status(date(2026, 9, 7), EVENING, eod)
    assert len(st['missing']) <= E.LOOKBACK_SESSIONS


# ── the ALERT, not just the checker ────────────────────────────────────────
#
# `common/tests/test_calendar_coverage.py` pins five properties of the function
# this one was copied from. The copy pinned none — and on this project
# "wired in, looked deployed, could never fire" is a named failure shape that
# has already cost a vetting banner nobody could see.

import inspect  # noqa: E402

from zebra import monitor  # noqa: E402


@pytest.fixture
def alerting(tmp_path, monkeypatch):
    """A LOG_DIR with no `eod/` at all, so coverage is unambiguously broken."""
    monkeypatch.setattr(monitor.cfg, 'LOG_DIR', tmp_path)
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    return sent


def test_the_check_is_REACHED_from_the_cycle():
    """Not "does the function work" — does anything call it.

    RETIRES WHEN: the digest becomes a step the engine runs itself, at which
    point its absence is a code path that fails rather than a missing crontab
    line that produces nothing.
    """
    assert '_alert_digest_coverage(' in inspect.getsource(monitor.run_cycle)


def test_it_has_its_OWN_except_not_the_calendars():
    """Two independent probes sharing one `except` means the first one raising
    silently disables the second, under a log line naming the wrong check.

    RETIRES WHEN: the cycle's preflight probes are driven from a list with
    per-probe isolation, so a shared handler is not expressible and this cannot
    regress by editing one call site.
    """
    src = inspect.getsource(monitor.run_cycle)
    i = src.index('_alert_calendar_coverage(')
    j = src.index('_alert_digest_coverage(')
    assert 'except' in src[i:j], 'the two calls share an except block'


def test_a_BROKEN_coverage_state_alerts(alerting):
    monitor._alert_digest_coverage(dry_run=False)
    assert len(alerting) == 1
    assert 'EOD DIGEST' in alerting[0]


def test_a_HEALTHY_state_sends_NOTHING(monkeypatch, alerting):
    """The negative control. Without it the test above passes on a function
    that alerts unconditionally."""
    monkeypatch.setattr(monitor.eod_coverage, 'coverage_status',
                        lambda: {'state': 'ok', 'detail': 'fine',
                                 'missing': [], 'paths_missing': []})
    monitor._alert_digest_coverage(dry_run=False)
    assert alerting == []


def test_it_does_not_repeat_within_24h(alerting):
    monitor._alert_digest_coverage(dry_run=False)
    monitor._alert_digest_coverage(dry_run=False)
    assert len(alerting) == 1, 'daily, or it becomes the noise it cures'


def test_a_STATE_CHANGE_re_alerts_immediately(monkeypatch, alerting):
    states = iter(['lapsed', 'missing'])
    monkeypatch.setattr(monitor.eod_coverage, 'coverage_status',
                        lambda: {'state': next(states), 'detail': 'd',
                                 'missing': [], 'paths_missing': []})
    monitor._alert_digest_coverage(dry_run=False)
    monitor._alert_digest_coverage(dry_run=False)
    assert len(alerting) == 2


def test_a_RAISING_checker_does_not_break_the_cycle(monkeypatch, alerting):
    def boom():
        raise RuntimeError('calendar exploded')
    monkeypatch.setattr(monitor.eod_coverage, 'coverage_status', boom)
    assert monitor._alert_digest_coverage(dry_run=False) is None
    assert alerting == []
