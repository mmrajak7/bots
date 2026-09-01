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


# -- THE ESCALATION BOUNDARY ------------------------------------------------
#
# Found reviewing the first cut of this fix. Guarding the late-day guard and
# the exception TEXT was not enough: the callers escalate on ANY falsy return,
# and both the exception handler and `_close_spread_inner`'s eight `return
# False` paths are falsy. So a paper record could still produce
# "close FAILED. Manual intervention needed!" from the call site -- the same
# instruction to trade, arriving by a different door.

def test_an_exception_on_a_paper_record_does_not_report_failure(
        spy, monkeypatch):
    """`False` here makes each of the four call sites Telegram the owner.

    The defect is already reported in its own words by the handler above; the
    caller must not stack "Manual intervention needed!" on top of it.
    """
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert _close(PAPER) is True
    assert 'CODE defect' in '\n'.join(spy), 'but it must still be reported'


def test_an_exception_on_a_REAL_record_still_reports_failure(spy, monkeypatch):
    """Negative control: a real close that threw IS an emergency."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    assert _close(REAL) is False


def test_an_inner_failure_on_a_paper_record_does_not_report_failure(
        spy, monkeypatch):
    """`_close_spread_inner` has eight `return False` paths — unfilled legs,
    rejected orders, an unreadable book. Each means "this close did not
    complete", which is an emergency for a REAL position and meaningless for a
    record with no legs. Converted once at the boundary so the paths added
    later are covered too."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner', lambda *a, **k: False)
    assert _close(PAPER) is True


def test_an_inner_failure_on_a_REAL_record_still_reports_failure(
        spy, monkeypatch):
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner', lambda *a, **k: False)
    assert _close(REAL) is False


def test_an_abort_is_never_converted(spy, monkeypatch):
    """`is False`, not falsy: 'ABORT' has its own caller branch (cooldown, no
    `closed = True`) and turning it into True would silently retire a retry."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner', lambda *a, **k: 'ABORT')
    assert _close(PAPER) == 'ABORT'


def test_a_successful_rehearsal_is_passed_through_unchanged(spy, monkeypatch):
    """The conversion must not mask a result the inner close actually gave."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(10, 0)))
    monkeypatch.setattr(sm, '_close_spread_inner', lambda *a, **k: True)
    assert _close(PAPER) is True


# ── THE THIRD DOOR (found 2026-08-31, second review) ────────────────────────
#
# Everything above works at the `close_spread` BOUNDARY, converting its RETURN
# VALUE. But `_close_spread_inner` and `close_leg` send their OWN Telegrams
# from deep inside the close, long before that boundary is reached, and
# neither had ever heard of `paper_passthrough`. So a paper record walked in
# dry run purely to journal its intended orders could still emit, verbatim:
#
#   "WARNING: <stock> LONG LEG CLOSE FAILED! Short is closed. Naked long
#    remains. Close manually: SELL <qty>"
#
# -- three false statements and one instruction, about a position that exists
# at no broker. Reachable without anything exotic: the depth loop runs
# `fresh=True` REGARDLESS of dry_run, so a rate-limit burst (70 in one day on
# 2026-08-28) makes a leg "fail" inside a rehearsal.

#: The inner close indexes `trade['exchange']`; without it the whole thing
#: raises KeyError and every "this text is absent" assertion below passes
#: vacuously. The REAL control is what catches that, which is its job.
PAPER_X = dict(PAPER, exchange='NFO')
REAL_X = dict(REAL, exchange='NFO')


def test_a_failed_LONG_leg_on_a_paper_rehearsal_says_nothing_to_a_human(
        spy, monkeypatch):
    """THE DEFECT. The short 'fills' (dry stub), the long fails, and the old
    code sent 'Close manually: SELL <qty>' for legs at no broker."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(11, 0)))

    def legs(kite, exchange, symbol, txn, qty, **kw):
        if symbol == PAPER['short_symbol']:
            return {'status': 'COMPLETE', 'filled_quantity': qty,
                    'average_price': 1.0}
        return None                      # the long leg cannot be closed
    monkeypatch.setattr(sm, 'close_leg', legs)

    _close(PAPER)

    joined = '\n'.join(spy)
    assert 'Close manually' not in joined, (
        'a paper rehearsal told a human to SELL a leg that exists nowhere')
    assert 'LONG LEG CLOSE FAILED' not in joined
    assert 'Naked long' not in joined, (
        'a paper rehearsal asserted a naked leg that cannot exist')


def test_a_failed_LONG_leg_on_a_REAL_record_still_escalates(spy, monkeypatch):
    """The negative control, and the one that matters most: suppressing the
    paper case must not suppress the real emergency it was written for."""
    monkeypatch.setattr(sm, 'now_ist',
                        lambda: datetime.combine(date.today(), dtime(11, 0)))

    def legs(kite, exchange, symbol, txn, qty, **kw):
        if symbol == REAL['short_symbol']:
            return {'status': 'COMPLETE', 'filled_quantity': qty,
                    'average_price': 1.0}
        return None
    monkeypatch.setattr(sm, 'close_leg', legs)

    _close(REAL_X, dry_run=True)

    joined = '\n'.join(spy)
    assert 'LONG LEG CLOSE FAILED' in joined, (
        'a real record with a failed long leg went unannounced')


def test_the_guard_suppresses_only_when_passthrough_is_set():
    """`_human_escalation` in isolation, both directions."""
    sent = []
    import bcs.spread_monitor as m
    real_send = m.send_telegram
    try:
        m.send_telegram = lambda msg, *a, **k: sent.append(msg)
        assert m._human_escalation('go and trade', paper_passthrough=True) is False
        assert sent == []
        assert m._human_escalation('go and trade', paper_passthrough=False) is True
        assert sent == ['go and trade']
    finally:
        m.send_telegram = real_send


def test_every_escalation_inside_the_inner_close_is_routed_through_the_guard():
    """The rule is at the SOURCE, but the inner close has its own sends and
    they are the ones that were missed. Pins that none of them regressed to a
    bare `send_telegram` that cannot know about the passthrough.

    RETIRES WHEN: `_close_spread_inner` no longer sends Telegrams itself --
    i.e. it returns a structured outcome and `close_spread` does all the
    talking, which is the change that makes the boundary genuinely single.
    """
    import inspect
    src = inspect.getsource(sm._close_spread_inner)
    bare = [l.strip() for l in src.splitlines()
            if 'send_telegram(' in l and '_human_escalation' not in l]
    # The trigger and close alerts are allowed: they route through
    # `alert_policy` and are already paper-aware.
    allowed = ('trigger_alert_text', 'close_alert_text', 'already flat')
    leaked = [l for l in bare if not any(a in l for a in allowed)]
    assert not leaked, (
        'these escalations bypass the paper-passthrough guard and can tell a '
        'human to trade a position that exists at no broker:\n  '
        + '\n  '.join(leaked))
