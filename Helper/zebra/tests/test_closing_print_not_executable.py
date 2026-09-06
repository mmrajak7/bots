"""Nothing is executable at or after the close.

## The incident

The cron line is `*/5 9-15` and `monitor._is_market_open` tests
`(h, m) <= MARKET_CLOSE`, so the 15:30 cycle read as OPEN and polled the
CLOSING AUCTION print. Three cohort positions booked `paper:tp` on it:

    TMPV       #423  2026-08-31 15:30:28
    GMRAIRPORT #455  2026-08-31 15:30:48
    ADANIGREEN #471  2026-08-31 15:31:08

TMPV's book had not even repriced: long 320 PE bid 8.90 against spot 308.85 is
2.25 BELOW intrinsic — the "TP" fired on a spot print the option market had not
yet seen, and no live order fills there.

The three rows are FLAGGED, not re-marked. The first cut of the repair tool
re-booked them at the last poll before the close and kept `paper:tp`, at a
moment when none of the three had touched its target — a worse fiction than the
one it was correcting. See `zebra/flag_close_print_exits.py`.

Note also that **the spot feed is frozen for the last 15 minutes of every
session** (identical at 15:15/15:20/15:25 on 125 of 125 observations, against 0
of 122 at 12:15-12:25), so there is no such thing as a trustworthy pre-close
mark to re-book AT.

## The shape of the fix

Measurement is NOT gated — the POLL line, the peak, the depth sample and the
spot-stop shadow all still accrue on the closing poll, because that
observation IS the session's close and it is the evidence record. What stops
is BOOKING, on the same seam and by the same reasoning as the stand-down.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_closing_print_not_executable.py -v
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra.trade_store import ZebraStore   # noqa: E402


def at(h, m):
    """A pinned wall clock. These guards read `datetime.now`, so a test that
    did not pin it would pass or fail depending on when it ran
    (`feedback_pin_the_wall_clock_in_tests`) — and this suite must pass at
    02:00 on a Sunday."""
    return datetime(2026, 9, 4, h, m, tzinfo=cfg.IST)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    return s


def drive(store, monkeypatch, now, spot=150.0, mid=30.0, vet=True):
    """One `check_entered` cycle at a pinned time, spot through TP."""
    sent = []
    # The REAL predicate, at a pinned clock — not a lambda re-implementing
    # `<`, which would keep passing after the comparison itself regressed.
    real = monitor._exits_executable
    monkeypatch.setattr(monitor, '_exits_executable',
                        lambda n=None, _r=real: _r(n or now))
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': mid, 'reliable': True, 'reason': None,
                            'legs': {'long': {'symbol': 'L', 'bid': 40.0,
                                              'ask': 40.2},
                                     'short': {'symbol': 'S', 'bid': 10.0,
                                               'ask': 10.2}},
                            'floored': False})
    monkeypatch.setattr(monitor, '_exit_cleared',
                        lambda st, t, k, q, sp, dry_run=False: vet)
    monitor.check_entered(store, kite=None, dry_run=True)
    return sent


# -- the predicate ----------------------------------------------------------

def test_the_close_minute_itself_is_not_executable():
    """15:30 EXACTLY is the boundary that was wrong. `<=` is what booked the
    three exits; the whole fix is this one comparison."""
    assert monitor._exits_executable(at(15, 29)) is True
    assert monitor._exits_executable(at(15, 30)) is False
    assert monitor._exits_executable(at(15, 31)) is False


def test_it_tracks_the_configured_close_not_a_second_copy_of_1530(monkeypatch):
    """A literal 15:30 here would be a second definition of the close, and the
    two would drift the first time anyone moved MARKET_CLOSE."""
    monkeypatch.setattr(cfg, 'MARKET_CLOSE', (14, 0))
    assert monitor._exits_executable(at(13, 59)) is True
    assert monitor._exits_executable(at(14, 0)) is False


def test_it_is_NOT_the_same_question_as_is_market_open():
    """`_is_market_open` decides whether to run the cycle at all, and the 15:30
    cycle still has real work — the closing observation is the evidence record.
    Folding one into the other would throw that away to fix this."""
    assert monitor._is_market_open is not monitor._exits_executable


# -- booking stops ----------------------------------------------------------

def test_a_TP_through_target_does_not_book_on_the_closing_print(store,
                                                                monkeypatch):
    drive(store, monkeypatch, at(15, 30), spot=150.0)
    assert store.find(1)['status'] == 'entered', (
        'a paper exit was booked on the closing auction print — this is the '
        'TMPV / GMRAIRPORT / ADANIGREEN defect of 2026-08-31')


def test_the_same_TP_books_normally_one_poll_earlier(store, monkeypatch):
    """The negative control. Without it this suite would pass just as happily
    against a monitor that had stopped booking anything at all."""
    drive(store, monkeypatch, at(15, 25), spot=150.0)
    assert store.find(1)['status'] == 'exited'
    assert store.find(1)['exit_reason'] == 'paper:tp'


def test_a_debit_SL_whose_second_confirm_lands_on_the_close_does_not_book(
        store, monkeypatch):
    """Not just the TP — but it takes TWO drives to show it.

    `DEBIT_SL_CONFIRM_POLLS` is 2, so a SINGLE cycle can never book a debit SL
    and a one-drive test passes against a monitor with no gate at all. The
    shape that matters is confirm-1 before the close and confirm-2 landing ON
    it."""
    drive(store, monkeypatch, at(15, 25), spot=99.0, mid=1.0)     # confirm 1
    assert store.find(1)['status'] == 'entered', 'booked on one confirm'
    drive(store, monkeypatch, at(15, 30), spot=99.0, mid=1.0)     # confirm 2
    assert store.find(1)['status'] == 'entered', (
        'a debit SL booked on the closing print')


def test_the_same_debit_SL_books_when_both_confirms_are_executable(store,
                                                                   monkeypatch):
    """The negative control for the test above, and the one that proves the
    two-drive shape can book at all."""
    drive(store, monkeypatch, at(15, 20), spot=99.0, mid=1.0)
    drive(store, monkeypatch, at(15, 25), spot=99.0, mid=1.0)
    t = store.find(1)
    assert t['status'] == 'exited' and t['exit_reason'] == 'paper:debit_sl'


def test_a_TP_LATCHED_before_the_close_does_not_book_ON_the_close(store,
                                                                  monkeypatch):
    """The case the cascade gate exists for, and the one the latch gate alone
    does NOT cover: the touch was real and executable at 15:25, the vet held it,
    and the next poll is the closing print. Without the `continue` the latch is
    already armed and the 15:30 poll books it."""
    drive(store, monkeypatch, at(15, 25), spot=150.0, vet=False)
    assert store.find(1)['status'] == 'entered'
    assert store.find(1).get('tp_touched_at'), 'the touch was not latched'
    drive(store, monkeypatch, at(15, 30), spot=150.0, vet=True)
    assert store.find(1)['status'] == 'entered', (
        'a latched TP booked on the closing print — the latch gate cannot '
        'catch this one, only the cascade gate can')


def test_the_TP_touch_is_not_even_LATCHED_on_the_closing_print(store,
                                                               monkeypatch):
    """The latch is the mechanism that would carry a closing-print touch into
    the next poll's booking. It is same-day, so refusing to arm costs at most
    one cycle — and the next session re-decides on a tradeable print."""
    drive(store, monkeypatch, at(15, 30), spot=150.0)
    assert not store.find(1).get('tp_touched_at'), \
        'the closing print armed a latch, so it can still book later'


def test_it_says_so_rather_than_going_quiet(store, monkeypatch, caplog):
    """A position nothing acts on looks exactly like a quiet one
    (`feedback_never_asked_is_not_failed`)."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        drive(store, monkeypatch, at(15, 30), spot=150.0)
    assert 'CLOSED-FOR-BOOKING' in caplog.text
    assert 'CLOSING POLL' in caplog.text


# -- measurement does NOT stop ----------------------------------------------

def test_the_peak_still_accrues_on_the_closing_poll(store, monkeypatch):
    """The closing observation IS the session's close and it is the evidence
    record (`logs/eod/paths_*.json`). Gating measurement to fix a booking bug
    would trade one silent loss of evidence for another."""
    # A modest move: `mfe` refuses an implausible jump from entry as a
    # corrupted-spot guard, and 150 from an entry of 96 trips it — which would
    # make this test pass for the wrong reason on either side of the fix.
    drive(store, monkeypatch, at(15, 30), spot=101.0)
    assert store.find(1).get('mfe_spot') == 101.0


def test_the_POLL_line_is_still_written_on_the_closing_poll(store, monkeypatch,
                                                            caplog):
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        drive(store, monkeypatch, at(15, 30), spot=150.0)
    assert 'POLL #1 TESTCO' in caplog.text


def test_the_spot_stop_shadow_still_accrues_on_the_closing_poll(store,
                                                               monkeypatch):
    drive(store, monkeypatch, at(15, 30), spot=88.0)
    assert store.find(1)['spot_shadow']['b3']['adverse_pct'] > 3.0


# -- flagging the rows the defect already produced --------------------------
#
# `zebra/flag_close_print_exits.py` FLAGS and does not re-book, and the reason
# is a mistake made here first. The original version of that tool re-booked
# each exit at the last poll before the close and kept `exit_reason:
# paper:tp`. At that poll none of the three had touched its target — TMPV
# 318.45 against a TP of 309.29, GMRAIRPORT 95.51 against 94.09, ADANIGREEN
# 1262.90 against 1247.43, all PE, all still above target. So it stamped a
# take-profit at a moment no exit rule had fired, which is a worse fiction
# than the one it was correcting, and it moved the cohort headline on the
# strength of it.
#
# The honest counterfactual is not available either: under the fixed rule the
# touch is never latched, so those positions would have carried to the next
# session, and no path was recorded for what happened next because the old
# engine closed them.


@pytest.fixture
def flag(store, monkeypatch):
    from zebra import flag_close_print_exits as f
    monkeypatch.setattr(f, 'get_store', lambda: store)
    return f


def _book(store, when='15:30:28', reason='paper:tp', val=30.0):
    with store._mutate():
        t = store.find(1)
        t.update({'status': 'exited', 'exit_date': '2026-09-04',
                  'exit_time': when, 'exit_debit': val, 'exit_spot': 150.0,
                  'exit_reason': reason, 'pnl': (val - 10.0) * 100,
                  'pnl_net': (val - 10.0) * 100 - 150.0,
                  'cohort': cfg.COHORT_START})


def test_it_finds_exactly_the_rows_stamped_at_or_after_the_close(flag, store):
    _book(store)
    assert [t['id'] for t in flag.plan(store)] == [1]
    _book(store, when='15:25:09')
    assert flag.plan(store) == []


def test_the_booked_numbers_are_NOT_touched(flag, store):
    """The whole point. A re-mark invents either a price or a trigger; this
    only records that the price came from a print nobody could trade at."""
    _book(store, val=30.0)
    before = {k: store.find(1)[k]
              for k in ('exit_debit', 'exit_spot', 'exit_time', 'exit_date',
                        'exit_reason', 'pnl', 'pnl_net')}
    flag.main(['--apply'])
    t = store.find(1)
    assert {k: t[k] for k in before} == before
    assert t[flag.FIELD]['booked_at'] == '2026-09-04 15:30:28'


def test_the_flag_says_what_to_exclude_the_row_from(flag, store):
    """A caveat nobody can act on is decoration. Name the statistics."""
    _book(store)
    flag.main(['--apply'])
    assert store.find(1)[flag.FIELD]['exclude_from'] == [
        'fill quality', 'exit slippage', 'TP timing']


def test_a_dry_run_writes_nothing(flag, store):
    _book(store)
    flag.main([])
    assert flag.FIELD not in store.find(1)


def test_it_is_safe_to_rerun(flag, store):
    """SAFE-TO-RERUN, asserted rather than claimed in a comment."""
    _book(store)
    flag.main(['--apply'])
    first = dict(store.find(1)[flag.FIELD])
    v = store.find(1)['version']
    flag.main(['--apply'])
    assert store.find(1)[flag.FIELD] == first
    assert store.find(1)['version'] == v, 'a re-run bumped the version'


def test_an_expiry_settle_stamped_at_the_close_is_NOT_flagged(flag, store):
    """`_settle_if_expired` deliberately runs above the cascade gate and still
    books on the closing poll — it fires strictly PAST expiry, when there is no
    fill to be had at any hour. Flagging it would put a caveat on the one exit
    that never needed one."""
    _book(store, reason='paper:expiry')
    assert flag.plan(store) == []


def test_it_only_touches_cohort_rows(flag, store):
    """The ~450 legacy records were priced mid-mid by an engine that no longer
    exists; flagging one would imply the rest had been checked."""
    _book(store)
    with store._mutate():
        store.find(1)['cohort'] = '1999-01-01'
    assert flag.plan(store) == []


def test_it_tracks_the_configured_close_here_too(flag, store, monkeypatch):
    """A third copy of `15:30` would drift from the predicate the first time
    anyone moved MARKET_CLOSE."""
    _book(store, when='14:30:00')
    assert flag.plan(store) == []
    monkeypatch.setattr(cfg, 'MARKET_CLOSE', (14, 0))
    assert [t['id'] for t in flag.plan(store)] == [1]
