"""The holiday calendar is DATA, and data goes wrong in four different ways.

`common/nse_holidays.py` reads `BOTS/data/holiday_calendar.json`, which SNAIL's
startup workflow scrapes daily from Zerodha. `sessions_between` already warns
when it is ASKED to count past what the file knows — and that warning is
passive twice over: it fires only once a position with such an expiry already
exists, and it lands in a cron log on the day it starts mattering.

So the state of the calendar is checked every cycle instead, and the bad states
get different sentences because they need different actions:

  missing / unreadable  the scrape job is not producing a calendar
  stale                 it stopped running; a holiday declared since is unknown
  expiring / expired    the year is running out and December has not happened

All of them degrade the count to WEEKDAYS-ONLY, which OVER-estimates the
sessions remaining and fires every delivery close LATER, into the margin ramp.

Run:  cd Helper && python -m pytest common/tests/test_calendar_coverage.py -v
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import nse_holidays as h        # noqa: E402

TODAY = date(2026, 8, 30)


@pytest.fixture(autouse=True)
def _fresh():
    h.reload()
    yield
    h.reload()


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    def write(payload):
        f = tmp_path / 'holiday_calendar.json'
        f.write_text(json.dumps(payload), encoding='utf-8')
        monkeypatch.setattr(h, 'HOLIDAY_FILE', f)
        h.reload()
        return f
    return write


def _year(year, updated=None, dates=('-10-02',)):
    payload = {str(year): [{'date': '%d%s' % (year, d), 'name': 'x'}
                           for d in dates]}
    if updated:
        payload['_last_updated'] = updated
    return payload


# -- the healthy case --------------------------------------------------------

def test_a_fresh_file_with_room_to_spare_is_quiet(calendar):
    calendar(_year(2026, updated='2026-08-29'))
    st = h.coverage_status(TODAY)
    assert st['state'] == 'ok'
    assert st['days_left'] > h.COVERAGE_WARN_DAYS


def test_the_real_deployed_file_is_healthy_today():
    """The negative control that matters most: a status that is never 'ok' on
    the actual file is an alarm nobody will read."""
    st = h.coverage_status(TODAY)
    assert st['state'] == 'ok', st['detail']


# -- the year running out ----------------------------------------------------

def test_the_warning_arrives_before_the_lapse_not_on_it(calendar):
    """Two months, because Zerodha publishes the next year in December and the
    scraper cannot find what does not exist yet."""
    calendar(_year(2026, updated='2026-11-30'))
    st = h.coverage_status(date(2026, 12, 1))
    assert st['state'] == 'expiring'
    assert 'December' in st['detail']


def test_the_boundary_is_where_it_says_it_is(calendar):
    """Both sides, so the threshold cannot drift to 'any day at all'."""
    end = date(2026, 12, 31)
    calendar(_year(2026, updated=(end - timedelta(days=1)).isoformat()))
    assert h.coverage_status(
        end - timedelta(days=h.COVERAGE_WARN_DAYS))['state'] == 'expiring'
    calendar(_year(2026, updated=(
        end - timedelta(days=h.COVERAGE_WARN_DAYS + 1)).isoformat()))
    assert h.coverage_status(
        end - timedelta(days=h.COVERAGE_WARN_DAYS + 1))['state'] == 'ok'


def test_past_coverage_is_a_different_state_from_expiring(calendar):
    """One is 'refresh this soon', the other is 'every session count is
    already wrong, in a known direction'."""
    calendar(_year(2026, updated='2027-01-02'))
    st = h.coverage_status(date(2027, 1, 2))
    assert st['state'] == 'expired'
    assert 'WEEKDAYS-ONLY' in st['detail'] and 'LATER' in st['detail']


# -- the file itself failing -------------------------------------------------

def test_a_missing_file_is_its_own_state(tmp_path, monkeypatch):
    monkeypatch.setattr(h, 'HOLIDAY_FILE', tmp_path / 'gone.json')
    h.reload()
    st = h.coverage_status(TODAY)
    assert st['state'] == 'missing'
    assert 'holiday_scraper' in st['detail'], (
        'the alert must name the job that writes the file — otherwise the '
        'reader knows something is wrong and not what to restart')


def test_an_unreadable_file_is_its_own_state(tmp_path, monkeypatch):
    f = tmp_path / 'broken.json'
    f.write_text('{{{', encoding='utf-8')
    monkeypatch.setattr(h, 'HOLIDAY_FILE', f)
    h.reload()
    assert h.coverage_status(TODAY)['state'] == 'unreadable'


def test_a_file_with_no_years_is_unreadable_not_ok(calendar):
    """A scrape that returned an empty page would otherwise parse cleanly and
    report a working calendar with nothing in it."""
    calendar({'_last_updated': '2026-08-29', '_comment': 'nothing here'})
    assert h.coverage_status(TODAY)['state'] == 'unreadable'


def test_a_stale_file_is_reported_even_though_it_still_parses(calendar):
    """The quietest failure of the four: the file is present, valid and
    answering, and simply has not been updated since the job died."""
    calendar(_year(2026, updated='2026-07-01'))
    st = h.coverage_status(TODAY)
    assert st['state'] == 'stale'
    assert '60 days ago' in st['detail']


def test_a_weekend_gap_does_not_read_as_stale(calendar):
    """The scraper runs daily, but a long weekend plus a holiday can leave the
    file untouched for days. An alarm that fires on a normal Tuesday is one
    the reader stops seeing."""
    calendar(_year(2026, updated=(TODAY - timedelta(days=4)).isoformat()))
    assert h.coverage_status(TODAY)['state'] == 'ok'


# -- every bad state says which way the error points -------------------------

@pytest.mark.parametrize('state,when,payload', [
    ('stale', TODAY, _year(2026, updated='2026-01-01')),
    ('expired', date(2027, 1, 2), _year(2026, updated='2027-01-02')),
    ('missing', TODAY, None),
])
def test_the_detail_names_the_direction_of_the_error(calendar, tmp_path,
                                                     monkeypatch, state, when,
                                                     payload):
    """A warning that says 'the calendar is stale' tells the reader nothing
    about whether to act today. The consequence is what makes it actionable,
    and it is asymmetric: an unknown holiday can only push a close LATER."""
    if payload is None:
        monkeypatch.setattr(h, 'HOLIDAY_FILE', tmp_path / 'gone.json')
        h.reload()
    else:
        calendar(payload)
    st = h.coverage_status(when)
    assert st['state'] == state
    assert 'LATER' in st['detail']


def test_it_does_not_extrapolate(calendar):
    """A calendar that guesses next year is the same shape as an option chain
    nobody checked the age of: it still parses, still answers, still sizes the
    trade. It must degrade LOUDLY, not silently."""
    calendar(_year(2026, updated='2026-08-29'))
    assert h.covers(date(2027, 3, 1)) is False
    assert h.is_holiday(date(2027, 1, 26)) is False, (
        'Republic Day 2027 was inferred — the calendar must only know what it '
        'was given')


# -- wiring ------------------------------------------------------------------

def test_the_engine_checks_coverage_every_cycle():
    """Beside the store-corruption and options-CSV checks, because it fails the
    same way they do: the number keeps looking authoritative.

    RETIRES WHEN: the input-freshness checks (store corruption, options CSV,
    calendar) are driven from one registry the cycle iterates.
    """
    import inspect
    from zebra import monitor
    assert '_alert_calendar_coverage' in inspect.getsource(monitor.run_cycle)


def test_a_HEALTHY_calendar_still_says_so_in_the_log():
    """A check that is silent when it passes cannot be confirmed working.

    This logged the healthy case at `debug` until the owner said they would
    watch the Pi logs for it — which would have shown nothing, leaving "the
    calendar is fine" and "the check is not wired in" looking identical. The
    same distinction `test_the_M10_worked_example` exists for.

    RETIRES WHEN: the cycle prints one status block assembled from every
    input-freshness check, so no check chooses its own visibility.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor._alert_calendar_coverage)
    i = src.index("st['state'] == 'ok'")
    assert 'logger.info' in src[i:i + 500], (
        'the healthy calendar is not logged at INFO — a passing check that '
        'says nothing cannot be observed passing')


def test_a_broken_calendar_is_alerted_DAILY_and_a_lapsing_one_weekly():
    """`expiring` is a diary note about December; the other four mean the
    session counts have ALREADY degraded. Same fault class, same cadence.

    RETIRES WHEN: alert cadence is a property of the status itself rather than
    a list the caller keeps in step with the states.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor._alert_calendar_coverage)
    assert "'expired', 'missing', 'unreadable', 'stale'" in src


def test_the_alert_reads_the_IST_date():
    """M7. A naive `date.today()` would move the lapse warning by a day on a
    UTC box — small here, and the same defect that made `is_market_open()`
    answer for the wrong timezone.

    RETIRES WHEN: the module has no access to a naive clock — `date.today`
    unimported and the IST clock the only one reachable.
    """
    import inspect
    from zebra import monitor
    # CODE ONLY: the comment beside that line explains the bug by naming it,
    # so a raw substring search matches the explanation and passes whatever
    # the code does.
    code = '\n'.join(
        ln.split('#', 1)[0] for ln in
        inspect.getsource(monitor._alert_calendar_coverage).splitlines())
    assert 'datetime.now(IST).date()' in code
    assert 'date.today()' not in code


def test_a_broken_coverage_check_cannot_stop_the_cycle():
    """An input-freshness check that can stop exit monitoring would be a worse
    bug than the one it reports — the rule the other two already follow.

    RETIRES WHEN: the freshness registry above runs every check inside one
    shared guard, so per-call `try` blocks stop being the mechanism.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.run_cycle)
    i = src.index('_alert_calendar_coverage')
    assert 'except Exception' in src[i:i + 400]
