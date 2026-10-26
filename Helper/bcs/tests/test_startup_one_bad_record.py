"""F6 (naked-live design, 2026-10-26) — one unreadable record must not stop the
monitor from starting.

`monitor_all` already isolates a malformed record INSIDE the poll loop
(`_malformed_reason` -> skip, alert once). The startup block before the loop
did not: the banner read `t['short_symbol']` and `t['sl_spread']`, and
`position_check_line` / `arm_time_stop` ran on every record with no
per-record guard. One record without a short leg -- the shape a naked long
has -- raised KeyError there, `monitor_all` exited, cron restarted it five
minutes later into the same crash, and EVERY book went unmonitored all day.

Run:  cd Helper && python -m pytest bcs/tests/test_startup_one_bad_record.py -v
"""
import pytest

from bcs import spread_monitor as sm
from bcs.tests.replay import Tick, run_session
from bcs.tests.test_tp_latch import (COHORT, DAY, LONG_BOOK, SHORT_BOOK,
                                     TARGET, _pos, books, gate)  # noqa: F401

#: The healthy cohort position goes THROUGH its target, so a monitor that
#: really started (and really polled) closes it. A monitor that crashed at
#: startup places nothing. The span outlasts the `gate` fixture's one deferral
#: plus the 300s abort cooldown that follows it.
TICKS = [Tick(t, TARGET + 5, LONG_BOOK, SHORT_BOOK, 'through the target')
         for t in ('11:00:00', '11:00:30', '11:03:00', '11:05:30',
                   '11:06:00', '11:07:00')]


def _broken(**drop):
    """A second cohort record with the given fields REMOVED (not set to None):
    absent is the shape that reached the unguarded startup reads."""
    t = dict(COHORT, id=777, stock='OTHERCO', spot_symbol='NSE:OTHERCO',
             long_symbol='OTHERCO26SEP500CE')
    for k in drop:
        t.pop(k, None)
    return t


def _run(monkeypatch, cohort):
    trade = dict(COHORT)
    _clock, kite, _store, spy = run_session(
        monkeypatch, sm, trade, TICKS, DAY, _pos(), dry_run=False,
        cohort=cohort)
    return kite, spy


@pytest.mark.parametrize('drop', [
    ('short_symbol',),                 # a naked long
    ('short_symbol', 'sl_spread'),     # and the other banner field
])
def test_a_record_without_a_short_leg_does_not_stop_startup(
        monkeypatch, books, gate, drop):
    kite, spy = _run(monkeypatch, [dict(COHORT), _broken(**dict.fromkeys(drop))])

    assert kite.placed, (
        'the healthy position went through its target and nothing was '
        'placed -- the monitor never reached the poll loop, so one bad record '
        'left every book unmonitored')


def test_the_bad_record_is_reported_once_not_silently_skipped(
        monkeypatch, books, gate):
    _kite, spy = _run(monkeypatch, [dict(COHORT), _broken(short_symbol=1)])

    loud = [m for _c, m in spy.offered if '#777' in m and 'malformed' in m]
    assert len(loud) == 1, (
        'expected exactly one CRITICAL for the unreadable record, got %d: %r'
        % (len(loud), loud))


def test_a_clean_book_is_unchanged(monkeypatch, books, gate):
    """Negative control: with no bad record the same session closes as before
    and nothing is called malformed."""
    kite, spy = _run(monkeypatch, [dict(COHORT)])
    assert kite.placed
    assert not [m for _c, m in spy.offered if 'malformed' in m]
