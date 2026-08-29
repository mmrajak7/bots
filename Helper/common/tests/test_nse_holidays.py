"""M4/M10 - the session count must know about holidays, and say when it does not.

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

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_nse_holidays.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import nse_holidays as h            # noqa: E402


# ── the data ────────────────────────────────────────────────────────────────

def test_the_calendar_is_not_empty():
    """An empty list would make every function here a weekday-only counter
    wearing a holiday calendar's name."""
    assert len(h.HOLIDAYS) >= 15


def test_no_holiday_falls_on_a_weekend():
    """A weekend entry is free — it changes no count — which makes it a
    silent sign the list came from a source that mixed in settlement
    holidays or a different exchange."""
    assert [d for d in h.HOLIDAYS if d.weekday() >= 5] == []


def test_the_muhurat_session_is_NOT_a_holiday():
    """2026-11-08 is a Sunday special session, not a closure of an ordinary
    trading day. Listing it would make a non-session look like one."""
    assert date(2026, 11, 8) not in h.HOLIDAYS


def test_coverage_is_declared_and_matches_the_data():
    assert h.COVERAGE_START <= min(h.HOLIDAYS)
    assert max(h.HOLIDAYS) <= h.COVERAGE_END


# ── sessions ────────────────────────────────────────────────────────────────

def test_a_holiday_inside_the_window_REDUCES_the_count():
    """THE defect, stated as a difference. Dussehra falls Tue 2026-10-20."""
    start, end = date(2026, 10, 16), date(2026, 10, 23)
    weekday_only = sum(1 for n in range(1, (end - start).days + 1)
                       if (start + __import__('datetime').timedelta(days=n)).weekday() < 5)
    assert h.sessions_between(start, end) == weekday_only - 1


def test_the_range_is_start_exclusive_and_end_inclusive():
    """What both callers already meant by 'sessions remaining': today does not
    count, expiry day does."""
    mon, tue = date(2026, 9, 21), date(2026, 9, 22)
    assert h.sessions_between(mon, tue) == 1
    assert h.sessions_between(mon, mon) == 0


def test_the_M10_worked_example():
    """From the delivery-margin research: expiry Tue 2026-09-29, so a
    6-session close fires Mon 2026-09-21 and a 5-session close Tue 09-22.
    No NSE holiday falls in that window, which is WHY the current book's
    close date is unaffected by this change — a fact worth pinning, because
    'the fix changed nothing here' is otherwise indistinguishable from 'the
    fix is not wired in'."""
    exp = date(2026, 9, 29)
    assert h.sessions_between(date(2026, 9, 21), exp) == 6
    assert h.sessions_between(date(2026, 9, 22), exp) == 5
    assert h.holidays_in(date(2026, 9, 21), exp) == []


def test_a_weekend_is_skipped():
    fri, mon = date(2026, 9, 18), date(2026, 9, 21)
    assert h.sessions_between(fri, mon) == 1


def test_a_range_that_ends_after_expiry_day_is_zero():
    assert h.sessions_between(date(2026, 9, 30), date(2026, 9, 29)) == 0


# ── running out of calendar: loud, not silent ───────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_warnings():
    """The dedup set is module-level; without this the second test measures
    the first."""
    h.reset_coverage_warnings()
    yield
    h.reset_coverage_warnings()


def test_counting_past_coverage_WARNS():
    """A calendar that quietly stops knowing things is worse than no calendar,
    because the number keeps looking authoritative. Same shape as an options
    chain nobody checked the age of."""
    said = []
    h.sessions_between(date(2026, 12, 20), date(2027, 1, 20), warn=said.append)
    assert len(said) == 1
    assert 'OVER-estimate' in said[0]
    assert 'nse_holidays.py' in said[0]


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
    written to protect. Found by the 2026-08-29 review; it fires ~Dec 2026."""
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
    n = h.sessions_between(date(2027, 1, 4), date(2027, 1, 8))
    assert n == 4


# ── both engines must agree ─────────────────────────────────────────────────

@pytest.mark.parametrize('start,expiry', [
    (date(2026, 9, 21), date(2026, 9, 29)),
    (date(2026, 10, 12), date(2026, 10, 27)),     # Dussehra inside
    (date(2026, 9, 8), date(2026, 9, 29)),        # Ganesh Chaturthi inside
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
    second calendar, silently."""
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
