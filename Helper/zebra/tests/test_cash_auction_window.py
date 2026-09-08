"""The monitor's behaviour while the cash market is in its closing auction.

From 15:15 the cash market cannot print, so spot is frozen; the OPTION book
goes on trading until 15:40. Two halves of the same poll mean different things,
and the engine has to treat them differently: spot is a fourteen-minute-old
print, the book is live.

`common/tests/test_market_session.py` pins the window and the veto. This file
pins the WIRING — that the monitor actually consults it, and that the positive
control holds: a value exit in the window still books, because it would fill.

## The mistake this file replaces

An earlier version of this work gated BOOKING at 15:30 (`_exits_executable`)
on the theory that three cohort exits stamped at 15:30:28-15:31:08 had been
booked at a price nobody could trade at. That was false. The option book moved
between the 15:25 and 15:30 polls on **125 of 125** position-sessions and was
identical on 0 — those three showed live, repriced two-way books (ADANIGREEN
long 48.00/48.80 -> 63.55/65.95) — and F&O trading was extended to 15:40 when
CAS went live. The guard was refusing genuine exits, which pushes a position
overnight, and that is how this book's worst loss happened. It is gone.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_cash_auction_window.py -v
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


def drive(store, monkeypatch, spot=150.0, mid=30.0, frozen=False, vet=True):
    """One `check_entered` cycle, with the auction window pinned."""
    monkeypatch.setattr(monitor.market_session, 'cash_price_is_frozen',
                        lambda n=None: frozen)
    # PIN THE CLOCK. The auction reference is stamped with the DAY it was
    # taken, so two cycles either side of IST midnight would re-take it and
    # this file would flake once a year at 00:00 — the wall-clock rule.
    monkeypatch.setattr(monitor, '_now_ist',
                        lambda: datetime(2026, 9, 7, 15, 20, tzinfo=monitor.IST))
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
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


# -- the exits that must STILL happen ---------------------------------------

def test_a_VALUE_exit_still_books_during_the_auction(store, monkeypatch):
    """THE POSITIVE CONTROL, and the reason this is not a blackout. Options
    trade to 15:40, so a debit SL inside the window is a real exit at a real
    price. Refusing it would hold a collapsing position overnight."""
    drive(store, monkeypatch, spot=99.0, mid=1.0, frozen=True)
    drive(store, monkeypatch, spot=99.0, mid=1.0, frozen=True)
    t = store.find(1)
    assert t['status'] == 'exited' and t['exit_reason'] == 'paper:debit_sl'


def test_a_TP_still_books_during_the_auction(store, monkeypatch):
    """The uncrossing price is the official close; spot really did reach the
    target, and the spread is sold into a live option book."""
    drive(store, monkeypatch, spot=150.0, frozen=True)
    assert store.find(1)['exit_reason'] == 'paper:tp'


# -- but spot is not to be believed -----------------------------------------

def test_the_POLL_line_says_the_spot_is_stale(store, monkeypatch, caplog):
    """A fourteen-minute-old price must not read as live beside an option book
    that is — that is the confusion worth naming on the line."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=97.0, frozen=True)
    assert '[STALE:auction]' in caplog.text


def test_no_such_mark_outside_the_window(store, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=97.0, frozen=False)
    assert '[STALE:auction]' not in caplog.text


def test_the_spot_peak_is_HELD_on_a_frozen_print(store, monkeypatch):
    """The frozen print is a REPEAT of the 15:15 one the peak has already seen.
    Advancing would record the same observation fifteen more times a session,
    on every open position."""
    drive(store, monkeypatch, spot=97.0, frozen=False)
    assert store.find(1)['mfe_spot'] == 97.0
    drive(store, monkeypatch, spot=99.0, frozen=True)
    assert store.find(1)['mfe_spot'] == 97.0, (
        'the spot peak advanced on a price the cash market could not print')


def test_the_MID_peak_is_NOT_held(store, monkeypatch):
    """It measures the OPTION book, which is trading. Holding it would disarm
    the trail for the last quarter of every session."""
    drive(store, monkeypatch, spot=97.0, mid=12.0, frozen=True)
    assert store.find(1)['mfe_mid'] == 12.0


def test_the_spot_stop_shadow_is_HELD_on_a_frozen_print(store, monkeypatch):
    """Every field it records is a statement about SPOT. Recording here would
    extend every MAE with fifteen minutes of guaranteed stillness, and let the
    uncrossing print land as a 'breach' at a price no continuous cash order
    could have got."""
    drive(store, monkeypatch, spot=88.0, frozen=True)
    assert 'spot_shadow' not in store.find(1)


def test_the_shadow_still_records_outside_the_window(store, monkeypatch):
    """Negative control for the test above."""
    drive(store, monkeypatch, spot=88.0, frozen=False)
    assert store.find(1)['spot_shadow']['b3']['adverse_pct'] > 3.0


# -- the declaration checks itself ------------------------------------------

def test_a_moving_spot_inside_the_window_is_reported(store, monkeypatch, caplog):
    """If NSE moves the auction, every guard keyed to 15:15 silently starts
    answering the wrong question and nothing looks broken. Log-only: a stale
    session-time assumption must not change what the engine does today.

    BOTH cycles are inside the window. That is the fix: the reference has to be
    taken in the window it is policing.
    """
    # Below the TP of 100, or the position exits on the first cycle and there
    # is nothing left to observe on the second.
    drive(store, monkeypatch, spot=97.0, frozen=True)     # sets the reference
    with caplog.at_level(logging.WARNING, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=98.0, frozen=True)
    assert 'AUCTION WINDOW LOOKS WRONG' in caplog.text


def test_the_first_in_window_cycle_is_never_a_finding(store, monkeypatch,
                                                      caplog):
    """It is the reference. There is nothing yet to have moved away from."""
    with caplog.at_level(logging.WARNING, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=97.0, frozen=True)
    assert 'AUCTION WINDOW LOOKS WRONG' not in caplog.text
    assert store.find(1)['auction_ref']['spot'] == 97.0


def test_the_move_INTO_the_freeze_is_not_a_finding(store, monkeypatch, caplog):
    """THE REGRESSION, from 2026-09-07.

    The first cut compared the frozen spot against `corrob_spot`, which stops
    advancing at 15:15 and so still holds the last PRE-auction price. The
    market trades normally right up to 15:15, so those two differ for entirely
    ordinary reasons — on that session, on 8 of 8 open positions, producing 24
    warnings in a day when the window behaved perfectly (spot identical at
    15:15/15:20/15:25 on 8 of 8, and moving again at the 15:30 uncrossing).

    A detector whose false-positive rate is ~100% by construction is worse than
    none: it teaches the reader to skip the line.
    """
    drive(store, monkeypatch, spot=97.0, frozen=False)    # 15:10, still live
    with caplog.at_level(logging.WARNING, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=98.0, frozen=True)  # 15:15, frozen
        drive(store, monkeypatch, spot=98.0, frozen=True)  # 15:20
        drive(store, monkeypatch, spot=98.0, frozen=True)  # 15:25
    assert 'AUCTION WINDOW LOOKS WRONG' not in caplog.text


def test_the_reference_is_unversioned_and_local_only(store, monkeypatch):
    """It changes once per position per session and nothing reads it after the
    close, so pushing it to Drive would churn the network — and an undeclared
    poll field is what produced the false MERGE CONFLICT on #454."""
    from zebra.trade_store import _BATCHED_POLL_FIELDS, _UNVERSIONED_FIELDS
    assert 'auction_ref' in _BATCHED_POLL_FIELDS
    assert 'auction_ref' in _UNVERSIONED_FIELDS
    before = store.find(1)['version']
    drive(store, monkeypatch, spot=97.0, frozen=True)
    assert store.find(1)['version'] == before


def test_a_still_spot_inside_the_window_says_nothing(store, monkeypatch, caplog):
    drive(store, monkeypatch, spot=97.0, frozen=True)
    with caplog.at_level(logging.WARNING, logger='zebra.monitor'):
        drive(store, monkeypatch, spot=97.0, frozen=True)
    assert 'AUCTION WINDOW LOOKS WRONG' not in caplog.text
