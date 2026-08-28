"""Task 4 — "positions MISSING!" is not a warning about a PAPER record.

The 2026-08-28 startup log opened with eight of these:

    WARNING: BCS #436 COFORGE — positions MISSING! (long=MISSING, short=MISSING)

Every line was accurate. Every line was noise. All eight cohort records are
`paper: True` — they have never had legs at any broker — so absent legs are
the DEFINITION of the record rather than a discrepancy with it. A warning that
fires on the healthy case teaches the reader to skip the warning, which is
exactly how the OI flag on COCHINSHIP got waved through.

The negative controls matter more than the fix here: a REAL record with a
missing leg, and a REAL record with a FLIPPED one (the Feb-2026 shape), must
both stay loud. `_record_says_paper` defaults to REAL for precisely that
reason, and this file fails if that default is ever "unified" with zebra's
opposite one.

Run:  cd Helper && python -m pytest bcs/tests/test_paper_position_check.py -v
"""
import inspect
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402

LONG, SHORT, QTY = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE', 700


def _trade(paper=False):
    t = {'id': 436, 'stock': 'COFORGE', 'status': 'open',
         'long_symbol': LONG, 'short_symbol': SHORT, 'quantity': QTY,
         'exchange': 'NFO', 'net_debit': 13.55, 'spot_symbol': 'NSE:COFORGE'}
    if paper:
        t['paper'] = True
    return t


def test_a_paper_record_does_not_raise_the_missing_positions_warning():
    """`WARNING: BCS #436 COFORGE — positions MISSING!` fired for all eight
    cohort records at 09:00. All eight are `paper: True`; absent legs are the
    definition of the record."""
    warn, msg = sm.position_check_line(_trade(paper=True), 'BCS', [])
    assert warn is False
    assert 'MISSING' not in msg
    assert 'PAPER record' in msg


def test_a_real_record_with_missing_legs_still_warns_loudly():
    """The negative control. The bcs / bear_put / fallen_hero books hold
    nothing but real positions and have never carried a `paper` key."""
    warn, msg = sm.position_check_line(_trade(paper=False), 'BCS', [])
    assert warn is True
    assert 'positions MISSING!' in msg


def test_a_real_record_with_a_flipped_leg_is_not_called_missing():
    """The Feb-2026 shape survives the refactor: FLIPPED and MISSING need
    different responses and must not be conflated."""
    positions = [{'tradingsymbol': LONG, 'quantity': QTY},
                 {'tradingsymbol': SHORT, 'quantity': QTY}]   # should be short
    warn, msg = sm.position_check_line(_trade(), 'BCS', positions)
    assert warn is True
    assert 'FLIPPED' in msg


def test_a_real_record_with_both_legs_present_verifies():
    positions = [{'tradingsymbol': LONG, 'quantity': QTY},
                 {'tradingsymbol': SHORT, 'quantity': -QTY}]
    warn, msg = sm.position_check_line(_trade(), 'BCS', positions)
    assert warn is False
    assert 'verified' in msg


def test_an_fh_paper_record_is_exempt_too():
    """One definition for all three branches — `feedback_the_copy_you_did_not_open`
    is this repo's most repeated defect and the pre-refactor code had the
    check written out three times."""
    fh = {'id': 1, 'stock': 'TESTCO', 'paper': True,
          'short_call_symbol': 'A', 'short_put_symbol': 'B',
          'long_put_symbol': 'C'}
    warn, msg = sm.position_check_line(fh, 'FH', [])
    assert warn is False and 'PAPER record' in msg
    del fh['paper']
    warn, msg = sm.position_check_line(fh, 'FH', [])
    assert warn is True and 'legs missing' in msg


def test_the_startup_sweep_routes_through_the_one_function():
    """A guard that exists but is not on the path that runs is decorative."""
    src = inspect.getsource(sm.monitor_all)
    assert 'position_check_line(t, strat, positions)' in src
    assert 'positions {bad}!' not in src, (
        'a hand-rolled copy of the verification came back into the loop')
