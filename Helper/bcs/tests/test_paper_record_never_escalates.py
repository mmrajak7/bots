"""A record this engine refuses must never tell a human to go and trade.

THE DEFECT, from production on 2026-08-31 15:29:55. The monitor recognised
paper cohort record #455 GMRAIRPORT twice, in its own words --

    BPS #455 GMRAIRPORT: TP touched at 93.63 - latch NOT written (dry run);
        the engine that owns this record's exits arms it.
    [DRY RUN] trade #455 is a PAPER record - armed, this close would be
        REFUSED and nothing placed. Continuing so the intended orders are
        journalled.

-- and then, four lines later, sent this to the owner:

    BPS TP TRIGGERED GMRAIRPORT @ 93.63
    BUT past 15:20 - NOT auto-closing.
    Close manually in Kite!
    BPS #455 GMRAIRPORT: TP close FAILED. Manual intervention needed!

zebra booked the same record correctly 53 seconds later at +54.5%. Nothing was
placed -- dry run, and the late-day guard had already stopped orders -- but an
owner who obeyed that instruction would have put REAL orders into Kite against
a position that exists at no broker: at best a rejected order, at worst a live
naked leg on a position nobody holds.

WHY IT HAPPENED. The dry-run passthrough is deliberate and correct: the
compare study needs the orders the armed engine WOULD have placed, so walking
on and journalling them is the point. What was missing is that journalling is
not ownership -- walking on also carried the record into every human-escalation
path below, and an escalation is an instruction to trade.

The fix is at the SOURCE (`close_spread`) rather than at the four call sites
that each send their own "manual intervention needed" Telegram. Fixing those
would be `feedback_the_copy_you_did_not_open` written out four times, and the
fifth caller added later would arrive unguarded.

Run:  cd Helper && python -m pytest bcs/tests/test_paper_record_never_escalates.py -v
"""
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

import bcs.spread_monitor as sm                                   # noqa: E402


PAPER = {
    'id': 455, 'stock': 'GMRAIRPORT', 'paper': True,
    'long_symbol': 'GMRAIRPORT26SEP97PE', 'short_symbol': 'GMRAIRPORT26SEP94PE',
    'quantity': 6975, 'lot_size': 6975, 'net_debit': 1.10,
    'long_strike': 97, 'short_strike': 94, 'status': 'open',
}
REAL = dict(PAPER, id=99, paper=False, stock='REALCO')


@pytest.fixture(autouse=True)
def isolate_stores(tmp_path, monkeypatch):
    """Keep every store path inside tmp_path.

    `close_spread` lazily reaches the BCS store on its first call in a session,
    which `conftest.ProductionWriteAttempted` correctly refuses. Redirecting
    the module globals is what the shared `book` fixture does; that one is
    parametrised per book, and this file needs no particular book -- only that
    nothing lands in the real logs/ directory
    (`feedback_tests_must_not_touch_production`).
    """
    import bcs.trade_store as ts
    monkeypatch.setattr(ts, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(ts, 'LOCAL_TRADES_FILE', tmp_path / 'bcs_trades.json')
    monkeypatch.setattr(ts, 'LOCK_FILE', tmp_path / 'bcs_trades.lock')


@pytest.fixture
def spy(monkeypatch):
    """Capture Telegram and silence the log."""
    sent = []
    monkeypatch.setattr(sm, 'send_telegram',
                        lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sm, 'log', lambda *a, **k: None)
    return sent


@pytest.fixture
def after_cutoff(monkeypatch):
    """Pin the clock past the late-day guard, as it was at 15:29:55.

    `feedback_pin_the_wall_clock_in_tests`: without this the test passes only
    when it happens to run after 15:20 IST.
    """
    stamp = datetime.combine(date.today(), dtime(15, 29, 55))
    monkeypatch.setattr(sm, 'now_ist', lambda: stamp)


def _close(trade, dry_run=True):
    return sm.close_spread(None, dict(trade), 93.63, 'TP', dry_run,
                           store=None, strategy_label='BPS')


# -- THE DEFECT -------------------------------------------------------------

def test_a_paper_record_past_the_cutoff_does_not_tell_anyone_to_trade(
        spy, after_cutoff):
    """The exact production sequence, reproduced."""
    _close(PAPER)
    joined = '\n'.join(spy)
    assert 'Close manually in Kite' not in joined, (
        'told a human to close a position that exists at no broker')
    assert 'manual intervention' not in joined.lower()


def test_it_does_not_report_failure_for_a_record_it_never_owned(
        spy, after_cutoff):
    """`False` makes every caller send its own "close FAILED" Telegram.

    True is the honest answer and has precedent in this same function: the
    already-closing branch returns True with "Not an error - another process
    has it". This is that, for a different owner.
    """
    assert _close(PAPER) is True


# -- the negative control, which is the whole point -------------------------

def test_a_REAL_record_past_the_cutoff_still_escalates(spy, after_cutoff):
    """The guard exists because a real spread left open over the close is a
    genuine emergency. Silencing that would be far worse than the bug fixed
    here, so it is pinned in the same file."""
    result = _close(REAL)
    joined = '\n'.join(spy)
    assert 'Close manually in Kite' in joined
    assert result is False, 'a real un-closed spread must report failure'


def test_a_real_record_is_not_affected_by_the_paper_flag_being_absent(
        spy, after_cutoff):
    """`_record_says_paper` defaults to REAL on purpose: three of the four
    books have never carried a `paper` key, and defaulting them to paper would
    abandon the stops on real money."""
    no_flag = {k: v for k, v in REAL.items() if k != 'paper'}
    _close(no_flag)
    assert 'Close manually in Kite' in '\n'.join(spy)


# -- the armed path is untouched --------------------------------------------

def test_armed_still_refuses_a_paper_record_outright(spy, monkeypatch):
    """Not dry run: the paper record must ABORT before the order path, exactly
    as before. This fix only concerns what dry run does after journalling."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    assert _close(PAPER, dry_run=False) == 'ABORT'


# -- the exception path -----------------------------------------------------

def test_an_exception_on_a_paper_record_reports_a_defect_not_an_emergency(
        spy, monkeypatch):
    """An exception is still reported -- hiding a code defect is worse -- but
    it must not be dressed as a position emergency."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    _close(PAPER)
    joined = '\n'.join(spy)
    assert joined, 'an exception must still be reported'
    assert 'boom' in joined
    assert 'CODE defect' in joined
    assert 'Manual intervention needed' not in joined


def test_an_exception_on_a_real_record_still_says_manual_intervention(
        spy, monkeypatch):
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    _close(REAL)
    assert 'Manual intervention needed' in '\n'.join(spy)
