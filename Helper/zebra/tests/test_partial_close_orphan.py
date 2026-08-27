"""`partial_close` — the status the state machine forgot.

It is written by `bcs/spread_monitor.py` in seven places: a leg that would not
close, a leg that came back FLIPPED (the Feb-2026 shape), an exception past the
close lock. In every one of them the record is frozen because legs are LIVE at
the broker and a human is needed.

It was then absent from every reader that decides what is still committed:

* `capital.HOLDING` — so a frozen position's rupees read as FREE capital, and
  `max_open_per_stock` (which counts from the same tuple) stopped counting it.
  A replacement could be sized against money committed to a position whose real
  size nobody knows.
* `add_signal`'s dedup — so the scanner could re-signal, and the pipeline
  re-enter, the SAME stock while the stranded position sat at the broker.

That was survivable while `partial_close` was a rare edge case. It stopped
being one when the exit bridge landed: `mark_exited` refused the 'closing'
status `begin_close` had just written, so EVERY bridged close raised and froze
its record here. The orphan status was the guaranteed terminal state of every
live cohort exit.

Run:  cd Helper && python -m pytest zebra/tests/test_partial_close_orphan.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import capital                      # noqa: E402
from zebra import config as cfg                # noqa: E402
from zebra.trade_store import ZebraStore     # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    return s


def pos(status, debit=13.55, qty=700, stock='TESTCO'):
    return {'id': 1, 'status': status, 'stock': stock,
            'debit': debit, 'quantity': qty, 'lot_size': qty, 'lots': 1}


# ── capital: frozen rupees are not free rupees ──────────────────────────────

def test_a_frozen_position_still_counts_as_deployed():
    rupees, n, unpriced = capital.deployed([pos('partial_close')])
    assert n == 1 and unpriced == 0
    assert rupees == pytest.approx(13.55 * 700)


def test_a_frozen_position_still_occupies_its_stock_slot():
    """`max_open_per_stock` counts from the same tuple. A stranded position
    blocking a replacement on the same stock is the POINT — the replacement
    would sit beside live legs nobody is managing."""
    ok, reason = capital.check([pos('partial_close')],
                               pos('watching', stock='TESTCO'))
    assert not ok and 'max_open_per_stock' in reason


@pytest.mark.parametrize('status', ['entered', 'closing', 'partial_close'])
def test_every_status_with_live_legs_holds_its_money(status):
    """One assertion over all three, rather than one test for the status that
    happened to burn us. The three differ only in how the position got there;
    in every one of them the legs are at the broker."""
    assert status in capital.HOLDING
    rupees, n, _ = capital.deployed([pos(status)])
    assert n == 1 and rupees > 0


@pytest.mark.parametrize('status', ['watching', 'triggered', 'exited',
                                    'cancelled'])
def test_a_position_with_no_legs_holds_nothing(status):
    """The inverse review. A guard is only worth what it refuses AND what it
    permits: counting a cancelled signal as deployed capital would block real
    entries just as silently as the bug it fixes let them through."""
    rupees, n, _ = capital.deployed([pos(status)])
    assert n == 0 and rupees == 0


# ── dedup: a frozen position is still this thesis, live ─────────────────────

def _enter(store, status):
    store.add_signal(dict(SIGNAL))
    with store._mutate():
        store.find(1)['status'] = status
    return store


@pytest.mark.parametrize('status', ['closing', 'partial_close'])
def test_the_same_thesis_cannot_re_signal_while_a_close_is_unfinished(
        store, status):
    """Both transient close states, not just the frozen one.

    'closing' means orders are out and the legs are still there until they
    fill; 'partial_close' means they are there and will stay there. Either way
    a second signal on the same (stock, timeframe, direction) is a second
    position on one thesis — and under `max_open_per_stock: 1` the pipeline
    that entered the first would happily enter the second."""
    _enter(store, status)
    with pytest.raises(ValueError, match='already open'):
        store.add_signal(dict(SIGNAL))


@pytest.mark.parametrize('status', ['exited', 'cancelled'])
def test_a_finished_thesis_does_not_block_a_new_signal(store, status):
    """The inverse again. Widening the dedup set must not have made a closed
    position block its own stock forever — that would silently retire a
    symbol from the scanner."""
    _enter(store, status)
    fresh = store.add_signal(dict(SIGNAL))
    assert fresh['status'] == 'watching'


@pytest.mark.parametrize('status', capital.HOLDING)
def test_the_dedup_set_and_the_capital_set_agree_about_live_legs(
        store, status):
    """The two lists live in different files and were out of step twice.

    Asserted through BEHAVIOUR, not by comparing the two constants: the
    property that matters is "a record holding money blocks a duplicate
    signal", and a test that compares tuples would pass the day someone
    satisfies it by editing the tuple rather than the check. Parametrised over
    `capital.HOLDING` so a fifth live status is covered the moment it is
    added."""
    _enter(store, status)
    with pytest.raises(ValueError, match='already open'):
        store.add_signal(dict(SIGNAL))
