"""The NSE holiday list is DATA, and data runs out.

`common/nse_holidays.py` covers 2026 only. `sessions_between` already warns
when it is ASKED to count past the window — and that warning is passive twice
over: it fires only once a position with a next-year expiry already exists, and
it lands in a cron log on the day it starts mattering.

Refreshing the list is not a code change. Somebody has to find next year's NSE
circular, and NSE publishes it in December. So the notice has to arrive with
time to act on it, and the failure it prevents is specific: past coverage the
session count silently degrades to weekdays-only, which OVER-estimates the
sessions remaining and fires every delivery close LATER — into the margin ramp
the 6-session schedule exists to clear.

Run:  cd Helper && python -m pytest common/tests/test_calendar_coverage.py -v
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import nse_holidays as h        # noqa: E402


def test_well_inside_coverage_is_quiet():
    st = h.coverage_status(date(2026, 8, 29))
    assert st['state'] == 'ok'
    assert st['days_left'] > h.COVERAGE_WARN_DAYS


def test_the_warning_arrives_before_the_lapse_not_on_it():
    """Two months, because the fix needs a human and a circular."""
    st = h.coverage_status(h.COVERAGE_END - timedelta(days=30))
    assert st['state'] == 'expiring'
    assert 'Refresh' in st['detail']


def test_the_boundary_is_where_it_says_it_is():
    """Both sides, so the threshold cannot drift to 'any day at all'."""
    assert h.coverage_status(
        h.COVERAGE_END - timedelta(days=h.COVERAGE_WARN_DAYS)
    )['state'] == 'expiring'
    assert h.coverage_status(
        h.COVERAGE_END - timedelta(days=h.COVERAGE_WARN_DAYS + 1)
    )['state'] == 'ok'


def test_past_coverage_is_a_different_state_from_expiring():
    """They need different sentences: one is 'refresh this soon', the other is
    'every session count is already wrong, in a known direction'."""
    st = h.coverage_status(h.COVERAGE_END + timedelta(days=1))
    assert st['state'] == 'expired'
    assert 'WEEKDAYS-ONLY' in st['detail']
    assert 'LATER' in st['detail']


def test_the_detail_names_the_direction_of_the_error():
    """A warning that says 'the calendar is stale' tells the reader nothing
    about whether to act today. The consequence is what makes it actionable,
    and the consequence is asymmetric: a missed holiday can only push a close
    LATER, never earlier."""
    for d in (h.COVERAGE_END - timedelta(days=1),
              h.COVERAGE_END + timedelta(days=1)):
        assert 'LATER' in h.coverage_status(d)['detail']


def test_it_does_not_extrapolate():
    """A calendar that guesses next year is the same shape as an option chain
    nobody checked the age of: it still parses, still answers, still sizes the
    trade. It must degrade LOUDLY, not silently."""
    assert h.covers(date(2027, 3, 1)) is False
    assert h.is_holiday(date(2027, 1, 26)) is False, (
        'Republic Day 2027 was inferred — the list must only know what it was '
        'given')


def test_the_engine_checks_coverage_every_cycle():
    """Beside the store-corruption and options-CSV checks, because it fails the
    same way they do: the number keeps looking authoritative."""
    import inspect
    from zebra import monitor
    assert '_alert_calendar_coverage' in inspect.getsource(monitor.run_cycle)


def test_the_alert_reads_the_IST_date():
    """M7. A naive `date.today()` would move the lapse warning by a day on a
    UTC box — small here, and the same defect that made `is_market_open()`
    answer for the wrong timezone."""
    import inspect
    # CODE ONLY: the comment beside that line explains the bug by naming it,
    # so a raw substring search matches the explanation and passes whatever
    # the code does.
    code = chr(10).join(
        ln.split('#', 1)[0] for ln in
        inspect.getsource(zebra_monitor()._alert_calendar_coverage).splitlines())
    assert 'datetime.now(IST).date()' in code
    assert 'date.today()' not in code


def zebra_monitor():
    from zebra import monitor
    return monitor


def test_a_broken_coverage_check_cannot_stop_the_cycle(monkeypatch):
    """An input-freshness check that can stop exit monitoring would be a worse
    bug than the one it reports — the rule the other two already follow."""
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.run_cycle)
    i = src.index('_alert_calendar_coverage')
    assert 'except Exception' in src[i:i + 400]
