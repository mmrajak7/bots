"""Nothing is executable at or after the close.

## The incident

The cron line is `*/5 9-15` and `monitor._is_market_open` tests
`(h, m) <= MARKET_CLOSE`, so the 15:30 cycle read as OPEN and polled the
CLOSING AUCTION print. Three cohort positions booked `paper:tp` on it:

    TMPV       #423  2026-08-31 15:30:28
    GMRAIRPORT #455  2026-08-31 15:30:48
    ADANIGREEN #471  2026-08-31 15:31:08

That single print moved TMPV -3.01%, SBICARD -3.13%, GMRAIRPORT -1.97%,
JINDALSTEL +2.95% and ADANIGREEN -3.80% while COALINDIA and SAGILITY sat flat.
The median 5-minute spot move across all 9,255 other polls in the record is
0.062%. TMPV's book had not even repriced: long 320 PE bid 8.90 against spot
308.85 is 2.25 BELOW intrinsic — the "TP" fired on a spot print the option
market had not yet seen, and no live order fills there.

Re-marking those three at their last executable poll turns the cohort from
12W/5L +Rs 17,481 into 11W/6L +Rs 11,610. This is not a rounding note: it is a
third of the paper book the arming gate is waiting on.

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


def drive(store, monkeypatch, now, spot=150.0, mid=30.0):
    """One `check_entered` cycle at a pinned time, spot through TP."""
    sent = []
    monkeypatch.setattr(monitor, '_exits_executable',
                        lambda n=None: (now.hour, now.minute) < cfg.MARKET_CLOSE)
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
                        lambda st, t, k, q, sp, dry_run=False: True)
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


def test_a_debit_SL_does_not_book_on_the_closing_print(store, monkeypatch):
    """Not just the TP. The whole cascade is skipped, because every one of its
    branches ends in a price nobody could have traded at."""
    drive(store, monkeypatch, at(15, 30), spot=99.0, mid=1.0)
    assert store.find(1)['status'] == 'entered'


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


# -- repairing the rows the defect already produced -------------------------

def _paths_file(tmp_path, date, tid, obs):
    import json
    d = tmp_path / 'eod'
    d.mkdir(exist_ok=True)
    (d / ('paths_%s.json' % date)).write_text(json.dumps(
        {'schema': 1, 'date': date, 'basis': 'fill',
         'trades': {str(tid): {'stock': 'TESTCO', 'direction': 'CE',
                               'obs': obs}}}), encoding='utf-8')


def _obs(ts, spot, val, q='ok'):
    return {'ts': ts, 'spot': spot, 'val': val, 'q': q,
            'long_bid': 40.0, 'long_ask': 40.2,
            'short_bid': 10.0, 'short_ask': 10.2}


@pytest.fixture
def remark(tmp_path, store, monkeypatch):
    from zebra import remark_close_print_exits as r
    monkeypatch.setattr(r, 'get_store', lambda: store)
    return r


def _book_after_close(store, val=30.0):
    with store._mutate():
        t = store.find(1)
        t.update({'status': 'exited', 'exit_date': '2026-09-04',
                  'exit_time': '15:30:28', 'exit_debit': val,
                  'exit_spot': 150.0, 'exit_reason': 'paper:tp',
                  'pnl': (val - 10.0) * 100, 'cohort': cfg.COHORT_START})


def test_it_finds_exactly_the_rows_stamped_at_or_after_the_close(remark, store):
    _book_after_close(store)
    assert [t['id'] for t, _ in remark.plan(store)] == [1]
    with store._mutate():
        store.find(1)['exit_time'] = '15:25:09'
    assert remark.plan(store) == []


def test_it_rebooks_at_the_last_EXECUTABLE_and_USABLE_poll(remark, store,
                                                           tmp_path):
    """Both conditions. `q == 'ok'` alone would happily return the 15:30 print,
    which is the whole defect; before-the-close alone returns a garbage book."""
    _book_after_close(store, val=30.0)
    _paths_file(tmp_path, '2026-09-04', 1, [
        _obs('2026-09-04 15:20:00', 140.0, 21.0),
        _obs('2026-09-04 15:25:00', 141.0, 22.0),
        _obs('2026-09-04 15:27:00', 142.0, 99.0, q='no_two_way_book'),
        _obs('2026-09-04 15:30:28', 150.0, 30.0),
    ])
    remark.main(['--apply'])
    t = store.find(1)
    assert t['exit_debit'] == 22.0 and t['exit_spot'] == 141.0
    assert t['exit_time'] == '15:25:00' and t['exit_date'] == '2026-09-04'


def test_the_pnl_is_recomputed_by_the_STORE_not_by_this_tool(remark, store,
                                                             tmp_path):
    """A second copy of the exit arithmetic is one a fee-model change would
    leave behind (`feedback_copy_pasted_modules_fix_once`)."""
    _book_after_close(store, val=30.0)
    _paths_file(tmp_path, '2026-09-04', 1,
                [_obs('2026-09-04 15:25:00', 141.0, 22.0)])
    remark.main(['--apply'])
    t = store.find(1)
    assert t['pnl'] == (22.0 - 10.0) * 100
    assert t['pnl_net'] is not None and t['pnl_net'] < t['pnl']


def test_the_original_booking_is_kept_on_the_record(remark, store, tmp_path):
    """A silently corrected number is indistinguishable from one that was
    always right, and this book is evidence before it is a scoreboard."""
    _book_after_close(store, val=30.0)
    _paths_file(tmp_path, '2026-09-04', 1,
                [_obs('2026-09-04 15:25:00', 141.0, 22.0)])
    remark.main(['--apply'])
    was = store.find(1)['exit_remarked']['was']
    assert was['exit_debit'] == 30.0 and was['exit_time'] == '15:30:28'


def test_a_dry_run_writes_nothing(remark, store, tmp_path):
    _book_after_close(store, val=30.0)
    _paths_file(tmp_path, '2026-09-04', 1,
                [_obs('2026-09-04 15:25:00', 141.0, 22.0)])
    remark.main([])
    assert store.find(1)['exit_debit'] == 30.0


def test_it_is_safe_to_rerun(remark, store, tmp_path):
    """SAFE-TO-RERUN, asserted rather than claimed in a comment. After the
    repair the row's exit_time is before the close, so it no longer matches."""
    _book_after_close(store, val=30.0)
    _paths_file(tmp_path, '2026-09-04', 1,
                [_obs('2026-09-04 15:25:00', 141.0, 22.0)])
    remark.main(['--apply'])
    first = dict(store.find(1))
    remark.main(['--apply'])
    assert store.find(1)['exit_debit'] == first['exit_debit']
    assert store.find(1)['exit_remarked'] == first['exit_remarked']


def test_a_row_with_no_path_data_is_LEFT_ALONE_not_guessed(remark, store):
    """Inventing a price to repair a row about invented prices would be its own
    joke. The day may still exist on the Pi."""
    _book_after_close(store, val=30.0)
    remark.main(['--apply'])
    t = store.find(1)
    assert t['exit_debit'] == 30.0 and 'exit_remarked' not in t


def test_it_only_touches_cohort_rows(remark, store, tmp_path):
    """The 450 legacy records were priced mid-mid by an engine that no longer
    exists; re-marking one would imply the rest are trustworthy."""
    _book_after_close(store, val=30.0)
    with store._mutate():
        store.find(1)['cohort'] = '1999-01-01'
    _paths_file(tmp_path, '2026-09-04', 1,
                [_obs('2026-09-04 15:25:00', 141.0, 22.0)])
    remark.main(['--apply'])
    assert store.find(1)['exit_debit'] == 30.0
