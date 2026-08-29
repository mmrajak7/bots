"""M4/M10 — the session count must know about holidays, and say when it does not.

Both engines counted sessions to expiry by skipping weekends and nothing else,
and both said so in a docstring. The omission is not neutral:

  * a holiday inside the window makes a weekday-only count an OVER-estimate —
    it reports more sessions than remain, so the close fires LATER;
  * NSE moves each delivery-margin tranche EARLIER around a holiday (it
    collects a holiday's margins on the preceding session).

The two errors compound in one direction, and that direction is "still holding
a physically-settled ITM option when the ramp starts". For this book the ramp
demands ~Rs 2.82L against a Rs 2L account, and a long ITM PUT is a
give-delivery obligation that goes to auction with a 20% floor and no ceiling.

WHAT CHANGED ON 2026-08-30
--------------------------
The dates come from `BOTS/data/holiday_calendar.json`, scraped daily from
Zerodha, instead of from a static list in the module. The static list was
mostly WRONG: checked against 160 FIFTY daemon logs it scored 2 of 6 evidenced
holidays and carried 6 dates that were full trading days. Three of the ones it
missed are the half that costs money.

Most of the tests below used to assert on that list. They are rewritten around
the file, and two of them had to be INVERTED because their premises were false
— see `test_a_weekend_entry_is_harmless` and
`test_the_muhurat_session_changes_no_count`.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_nse_holidays.py -v
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import nse_holidays as h            # noqa: E402

#: Evidenced against the FIFTY daemon logs: a ~20-35 line log means the daemon
#: started, found the market closed and exited; a trading day leaves ~5,700.
EVIDENCED_CLOSED = [date(2026, 1, 26), date(2026, 2, 19), date(2026, 3, 19),
                    date(2026, 4, 1), date(2026, 4, 3)]
#: Same source, the other way: these are dates the OLD static list called
#: holidays and the daemon logged a full session on.
EVIDENCED_TRADED = [date(2026, 3, 3), date(2026, 3, 26), date(2026, 3, 31),
                    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
                    date(2026, 6, 26)]


@pytest.fixture(autouse=True)
def _fresh():
    """The parse cache and the warning dedup are both module-level."""
    h.reload()
    h.reset_coverage_warnings()
    yield
    h.reload()
    h.reset_coverage_warnings()


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    """Write a calendar file and point the module at it."""
    def write(payload):
        f = tmp_path / 'holiday_calendar.json'
        f.write_text(json.dumps(payload), encoding='utf-8')
        monkeypatch.setattr(h, 'HOLIDAY_FILE', f)
        h.reload()
        return f
    return write


# -- the real file -----------------------------------------------------------

def test_the_calendar_is_not_empty():
    """An empty set would make every function here a weekday-only counter
    wearing a holiday calendar's name."""
    assert len(h.holidays()) >= 10
    assert 2026 in h.covered_years()


@pytest.mark.parametrize('d', EVIDENCED_CLOSED, ids=lambda d: d.isoformat())
def test_every_evidenced_closure_is_in_the_calendar(d):
    """The measurement that condemned the old static list, kept as a test.

    Three of these (02-19, 03-19, 04-01) were MISSING from it, and a missing
    holiday is the direction that holds a position into the delivery ramp.
    """
    assert h.is_holiday(d), '%s was evidenced closed and is not in the file' % d
    assert not h.is_session(d)


@pytest.mark.parametrize('d', EVIDENCED_TRADED, ids=lambda d: d.isoformat())
def test_no_evidenced_trading_day_is_called_a_holiday(d):
    """The other half. The old list carried all seven of these, which makes a
    session count UNDER-estimate — safe direction, still wrong, and it was the
    sign the list was a generic festival calendar rather than NSE's."""
    assert not h.is_holiday(d)
    assert h.is_session(d)


def test_a_weekend_entry_is_harmless():
    """INVERTED on 2026-08-30. This used to assert the calendar contains no
    weekend dates, on the reasoning that one would be "a silent sign the list
    came from a source that mixed in settlement holidays".

    The real file does contain them (2026-02-15 Sun, 03-21 Sat, 08-15 Sat), and
    the owner's decision is to take the file as-is. They are free: `is_session`
    excludes weekends before it ever consults the holiday set, so they change
    no count. Asserting the property that actually matters instead.
    """
    for d in h.holidays():
        if d.weekday() >= 5:
            assert not h.is_session(d)


def test_the_muhurat_session_changes_no_count():
    """INVERTED for the same reason. 2026-11-08 is a Sunday special session —
    the market is OPEN — and the file lists it as a holiday, which is
    backwards. It costs nothing because it is a Sunday, so `is_session` was
    already going to say False.

    Pinned so that if a Muhurat session ever lands on a WEEKDAY, this fails and
    somebody looks — that one would shorten a real count.
    """
    muhurat = date(2026, 11, 8)
    assert muhurat.weekday() >= 5
    assert not h.is_session(muhurat)


def test_the_known_imperfections_are_still_the_known_ones():
    """Owner decision, 2026-08-30: trust the file as-is, no hand-patching.

    Two divergences from observed sessions are therefore live and deliberate,
    and both are recorded here so that a future reader finds the decision
    rather than a bug. If either flips, the file changed and that is worth
    knowing.
    """
    # Evidenced closed (FIFTY log, 25 lines) and NOT in the file: the count
    # over-estimates by one session across it, firing the close LATER.
    assert not h.is_holiday(date(2026, 6, 23))
    # In the file, but `logs/cron_zebra_20260826.log` shows a full session
    # (347 polls, 265 distinct spot prints): the count under-estimates by one,
    # firing the close EARLIER. The safe direction.
    assert h.is_holiday(date(2026, 8, 26))


# -- sessions ----------------------------------------------------------------

def test_a_holiday_inside_the_window_REDUCES_the_count():
    """THE defect, stated as a difference.

    The week of 2026-03-30 holds TWO closures — 04-01 and Good Friday 04-03 —
    and the count must drop by both. Written as "minus the holidays in the
    window" rather than "minus one": the first draft of this test assumed a
    single holiday and failed, which is the same over-confidence about the
    calendar that produced the wrong static list in the first place.
    """
    start, end = date(2026, 3, 30), date(2026, 4, 6)
    weekday_only = sum(1 for n in range(1, (end - start).days + 1)
                       if (start + timedelta(days=n)).weekday() < 5)
    inside = [d for d in h.holidays_in(start + timedelta(days=1), end)
              if d.weekday() < 5]
    assert len(inside) == 2, inside
    assert h.sessions_between(start, end) == weekday_only - len(inside)


def test_the_range_is_start_exclusive_and_end_inclusive():
    """What both callers already meant by 'sessions remaining': today does not
    count, expiry day does."""
    mon, tue = date(2026, 9, 21), date(2026, 9, 22)
    assert h.sessions_between(mon, tue) == 1
    assert h.sessions_between(mon, mon) == 0


def test_the_M10_worked_example():
    """From the delivery-margin research: expiry Tue 2026-09-29, so a
    6-session close fires Mon 2026-09-21 and a 5-session close Tue 09-22.

    Re-verified against the NEW source on 2026-08-30 and unchanged — no
    holiday falls in that window in either calendar, so swapping the data did
    not move the live book's close date. Worth pinning, because 'the change
    moved nothing here' is otherwise indistinguishable from 'the change is not
    wired in'.
    """
    exp = date(2026, 9, 29)
    assert h.sessions_between(date(2026, 9, 21), exp) == 6
    assert h.sessions_between(date(2026, 9, 22), exp) == 5
    assert h.holidays_in(date(2026, 9, 21), exp) == []


def test_a_weekend_is_skipped():
    fri, mon = date(2026, 9, 18), date(2026, 9, 21)
    assert h.sessions_between(fri, mon) == 1


def test_a_range_that_ends_after_expiry_day_is_zero():
    assert h.sessions_between(date(2026, 9, 30), date(2026, 9, 29)) == 0


# -- the file as a live input ------------------------------------------------

def test_a_refresh_lands_without_a_restart(calendar):
    """The whole reason the file is read at call time. The scraper rewrites it
    daily while both engines are long-lived, and a calendar cached at import
    would keep answering from the copy that was there at 09:00."""
    f = calendar({'2026': [{'date': '2026-05-01', 'name': 'x'}]})
    assert h.is_holiday(date(2026, 5, 1))
    f.write_text(json.dumps(
        {'2026': [{'date': '2026-05-01', 'name': 'x'},
                  {'date': '2026-05-04', 'name': 'y'}]}), encoding='utf-8')
    # No reload() call: the cache key is the file's identity, not a TTL.
    import os
    os.utime(f, (0, 0))
    assert h.is_holiday(date(2026, 5, 4))


def test_a_missing_file_degrades_to_weekdays_and_SAYS_SO(tmp_path, monkeypatch):
    """Loud, not silent. Weekday-only counting is the OLD behaviour, and it
    over-estimates — so it must never be reached quietly."""
    monkeypatch.setattr(h, 'HOLIDAY_FILE', tmp_path / 'nope.json')
    h.reload()
    assert h.holidays() == frozenset()
    assert h.is_session(date(2026, 4, 3)) is True      # Good Friday, unknown
    st = h.coverage_status(date(2026, 8, 30))
    assert st['state'] == 'missing'
    assert 'WEEKDAYS-ONLY' in st['detail'] and 'LATER' in st['detail']


def test_an_unreadable_file_is_a_DIFFERENT_state_from_a_missing_one(calendar,
                                                                    tmp_path,
                                                                    monkeypatch):
    """They need different sentences: one is 'the job never ran', the other is
    'the job ran and produced something that is not a calendar'."""
    f = tmp_path / 'broken.json'
    f.write_text('{not json', encoding='utf-8')
    monkeypatch.setattr(h, 'HOLIDAY_FILE', f)
    h.reload()
    assert h.coverage_status(date(2026, 8, 30))['state'] == 'unreadable'


def test_one_bad_row_does_not_discard_the_year(calendar):
    """A date the parser silently dropped is a holiday the count does not know
    about, and that direction holds a position into the ramp. It keeps the
    rest and names what it dropped."""
    calendar({'2026': [{'date': '2026-05-01', 'name': 'good'},
                       {'date': 'not-a-date', 'name': 'bad'},
                       {'date': '2026-05-04', 'name': 'also good'}]})
    assert h.is_holiday(date(2026, 5, 1))
    assert h.is_holiday(date(2026, 5, 4))


def test_a_stale_file_is_reported(calendar):
    """The scraper runs daily. If `_last_updated` stops moving, the job has
    stopped — and a holiday declared since then is unknown here."""
    calendar({'_last_updated': '2026-07-01',
              '2026': [{'date': '2026-10-02', 'name': 'x'}]})
    st = h.coverage_status(date(2026, 8, 30))
    assert st['state'] == 'stale'
    assert 'scraped daily' in st['detail']


def test_a_fresh_file_is_quiet(calendar):
    """The negative control. A status that is never 'ok' is an alarm nobody
    reads."""
    calendar({'_last_updated': '2026-08-29',
              '2026': [{'date': '2026-10-02', 'name': 'x'}]})
    assert h.coverage_status(date(2026, 8, 30))['state'] == 'ok'


def test_coverage_is_PER_YEAR_not_a_span(calendar):
    """The file is a mapping of year to rows. A year that is absent is absent
    whether or not it sits between two present ones, and claiming a contiguous
    window would assert knowledge of a gap."""
    calendar({'2025': [{'date': '2025-12-25', 'name': 'x'}],
              '2027': [{'date': '2027-01-26', 'name': 'y'}]})
    assert h.covers(date(2025, 6, 1))
    assert h.covers(date(2027, 6, 1))
    assert not h.covers(date(2026, 6, 1))


# -- running out of calendar: loud, not silent -------------------------------

def test_counting_past_coverage_WARNS():
    """A calendar that quietly stops knowing things is worse than no calendar,
    because the number keeps looking authoritative."""
    said = []
    h.sessions_between(date(2026, 12, 20), date(2027, 1, 20), warn=said.append)
    assert len(said) == 1
    assert 'OVER-estimate' in said[0]
    assert 'holiday_calendar.json' in said[0]


def test_counting_INSIDE_coverage_is_silent():
    """The negative control. A warning on every count is a warning nobody
    reads — the failure mode the bid-ask flag died of."""
    said = []
    h.sessions_between(date(2026, 9, 21), date(2026, 9, 29), warn=said.append)
    assert said == []


def test_the_warning_is_deduped_per_uncovered_date():
    """The order engine calls `sessions_to_expiry` per open trade per
    FIVE-SECOND poll. Un-deduped, the moment a next-year expiry exists it
    would write ~30,000 five-line notices a session — burying the log it was
    written to protect."""
    said = []
    for _ in range(50):
        h.sessions_between(date(2026, 12, 20), date(2027, 1, 20),
                           warn=said.append)
    assert len(said) == 1


def test_a_SECOND_uncovered_expiry_still_gets_its_own_line():
    """Deduping on "we already warned once" rather than on WHAT we warned
    about would hide the second contract past coverage."""
    said = []
    h.sessions_between(date(2026, 12, 20), date(2027, 1, 20), warn=said.append)
    h.sessions_between(date(2026, 12, 20), date(2027, 2, 24), warn=said.append)
    assert len(said) == 2


def test_past_coverage_it_still_ANSWERS():
    """Degrading to weekday-only is the OLD behaviour. Returning None or
    raising would turn a data-staleness problem into an outage on a path that
    decides when to close a physically-settled position."""
    assert h.sessions_between(date(2027, 1, 4), date(2027, 1, 8)) == 4


# -- both engines must agree -------------------------------------------------

@pytest.mark.parametrize('start,expiry', [
    (date(2026, 9, 21), date(2026, 9, 29)),
    (date(2026, 3, 30), date(2026, 4, 6)),        # Good Friday inside
    (date(2026, 3, 16), date(2026, 3, 27)),       # 2026-03-19 inside
    (date(2026, 12, 21), date(2026, 12, 31)),     # Christmas inside
])
def test_the_two_engines_count_the_same_sessions(start, expiry):
    """One position, two engines. Counters that disagreed would put them on
    different close dates — `feedback_the_copy_you_did_not_open`, applied to
    a date. They share this module rather than each having a loop."""
    from bcs import spread_monitor as sm
    from zebra import monitor as zm

    assert (sm.sessions_to_expiry({'expiry': expiry.isoformat()}, today=start)
            == zm._sessions_left(start, expiry)
            == h.sessions_between(start, expiry))


def test_neither_engine_still_has_its_own_weekday_loop():
    """The point of sharing the module is that there is nothing left to
    diverge. A leftover `cur.weekday() < 5` loop in either counter would be a
    second calendar, silently.

    RETIRES WHEN: the session counters take a calendar object as an argument,
    so an engine that wanted its own loop would have to be handed one.
    """
    import ast
    from bcs import spread_monitor as sm
    from zebra import monitor as zm

    for mod, fn in ((sm, 'sessions_to_expiry'), (zm, '_sessions_left')):
        tree = ast.parse(Path(mod.__file__).read_text(encoding='utf-8'))
        body = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        # An `.weekday` ATTRIBUTE ACCESS, not the word. `ast.dump` includes
        # the docstring, and these two docstrings legitimately explain that
        # the shared helper degrades to weekday-only past its coverage —
        # a text scan flags the documentation of the fix. Same correction the
        # M7 clock guard needed.
        uses = [n for n in ast.walk(body)
                if isinstance(n, ast.Attribute) and n.attr == 'weekday']
        assert not uses, (
            '%s.%s counts weekdays itself again — that is a second calendar'
            % (mod.__name__, fn))
