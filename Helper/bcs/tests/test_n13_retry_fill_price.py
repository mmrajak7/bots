"""N13 - a retry fill's PRICE is discarded, and it can flip a loss into a win.

A partial close gets ONE urgent retry on the residual (that is D2/D3, and it is
correct). The retry's `filled_quantity` is read - to decide whether residue
remains - and its `average_price` is **thrown away**. The leg keeps the FIRST
tranche's price, weighted at 100%.

That is not a rounding error. The work order's measured case, reproduced below
as `test_the_measured_case`:

    short leg fills 600 @ 5.00, the retry clears 100 @ 50.00
      booked   short_fill  5.0000  -> pnl/share +1.45  -> total  +Rs 1,015
      true     short_fill 11.4286  -> pnl/share -4.98  -> total  -Rs 4,980

**Rs 4,500 out, and it reports a LOSS AS A WIN** - on the live-money BCS path,
into the record the arming decision reads.

It is NOT the seed-ambiguity family (D1/D2/D4, `feedback_a_default_that_looks
_like_a_value`). Nothing here is a sentinel mistaken for a reading: both prices
are real and observed. It is a WEIGHTING defect - the right numbers combined
wrongly - which is why it needed its own fix rather than another None-guard.

It only bites when a retry CLEARS the residue, which is exactly the path D2/D3
just made reachable and correct. It gets more likely, not less.

The unpriced-tranche cases matter as much as the arithmetic: a tranche that
filled quantity at a price we never saw makes the whole leg unpriced, and
`_blend_fill` returns None so `_refuse_unpriced_close` books nothing. Weighting
only the half we can see would be inventing a fill - the mistake this file's
neighbours exist to stop.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_n13_retry_fill_price.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)
from bcs.tests.test_d2_partial_close_residue import (              # noqa: E402
    B_LONG, B_QTY, B_SHORT, BCS_BOOKS, FH_BOOKS, FILL, LC, LP, SC, SP,
    _LegScript, _bcs, _bcs_pos, _complete, _fh, _fh_pos, _partial)


# == the helper, in isolation ================================================

def test_blend_is_quantity_weighted_not_first_wins():
    # 600 @ 5.00 + 100 @ 50.00 -> (3000 + 5000) / 700
    assert sm._blend_fill(5.0, 600, 50.0, 100) == pytest.approx(11.428571,
                                                                abs=1e-6)


def test_no_retry_fill_leaves_the_first_price_untouched():
    """The overwhelmingly common path: the retry filled nothing."""
    assert sm._blend_fill(5.0, 600, 50.0, 0) == 5.0
    assert sm._blend_fill(5.0, 600, None, 0) == 5.0


def test_a_first_tranche_that_filled_nothing_yields_the_retry_price():
    assert sm._blend_fill(0.0, 0, 50.0, 100) == 50.0
    assert sm._blend_fill(None, 0, 50.0, 100) == 50.0


@pytest.mark.parametrize('retry_price', [None, 0.0, -1.0])
def test_a_retry_that_filled_at_an_UNSEEN_price_makes_the_leg_unpriced(
        retry_price):
    """Quantity moved, price unknown -> None, never the half we can see.

    Returning 5.0 here would report a price nobody transacted at, on a leg that
    demonstrably filled at something else. None routes to
    `_refuse_unpriced_close`, which books nothing and escalates.
    """
    assert sm._blend_fill(5.0, 600, retry_price, 100) is None


@pytest.mark.parametrize('first_price', [None, 0.0])
def test_an_unpriced_FIRST_tranche_is_equally_fatal(first_price):
    assert sm._blend_fill(first_price, 600, 50.0, 100) is None


def test_the_blend_is_bounded_by_its_two_inputs():
    """A weighted mean can never leave the interval. Cheap invariant, and it
    catches a transposed numerator - the way this arithmetic usually breaks."""
    for fq, rq in ((600, 100), (100, 600), (1, 999), (500, 500)):
        got = sm._blend_fill(5.0, fq, 50.0, rq)
        assert 5.0 <= got <= 50.0


# == end to end, on the live-money BCS close path ============================

@pytest.fixture
def bcs_env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_bcs()])


def _run_bcs(store, script, monkeypatch):
    monkeypatch.setattr(sm, 'close_leg', script)
    kite = FakeBroker(books=BCS_BOOKS, positions=_bcs_pos())
    return sm._close_spread_inner(kite, store, _bcs(), spot=1400.0,
                                  reason='SL_SPREAD', dry_run=False,
                                  label='BCS')


def test_the_measured_case(bcs_env, monkeypatch):
    """The work order's numbers, end to end. Fails pre-fix at short_fill 5.00."""
    spy, store = bcs_env
    # Short buyback: 600 of 700 @ 5.00, then the residual 100 @ 50.00.
    # Long sale fills in one go at 10.00 so the arithmetic stays legible.
    script = _LegScript(**{B_SHORT: [_partial(600, 5.00),
                                     _complete(100, 50.00)],
                           B_LONG: [_complete(B_QTY, 10.00)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is True, 'the retry cleared the residue; the close must book'
    exit_data = store.called('update_trade_exit')[0][1][1]

    assert exit_data['short_fill'] == pytest.approx(11.428571, abs=1e-6), \
        'short_fill kept the first tranche price - this is N13'
    # exit_net = long - short = 10.00 - 11.4286
    assert exit_data['exit_spread'] == pytest.approx(-1.428571, abs=1e-6)
    assert exit_data['total_pnl'] < 0, \
        'N13 booked this LOSS as a WIN (+Rs 1,015 against a true -Rs 4,980)'


def test_the_long_leg_has_the_same_defect(bcs_env, monkeypatch):
    """D3's retry, same discarded price. The long fill is the other half of
    `exit_net`, so the error lands with the opposite sign."""
    spy, store = bcs_env
    script = _LegScript(**{B_SHORT: [_complete(B_QTY, 10.00)],
                           B_LONG: [_partial(600, 40.00),
                                    _complete(100, 5.00)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is True
    exit_data = store.called('update_trade_exit')[0][1][1]
    # (40*600 + 5*100) / 700 = 24500/700 = 35.00
    assert exit_data['long_fill'] == pytest.approx(35.00, abs=1e-6), \
        'long_fill kept the first tranche price - N13 on the long leg'
    assert exit_data['exit_spread'] == pytest.approx(25.00, abs=1e-6)


def test_a_retry_filling_at_an_unseen_price_refuses_to_book(bcs_env,
                                                            monkeypatch):
    """The blend's None must reach the existing refusal, not become a zero."""
    spy, store = bcs_env
    script = _LegScript(**{B_SHORT: [_partial(600, 5.00),
                                     _complete(100, 0.0)],
                           B_LONG: [_complete(B_QTY, 10.00)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit'), \
        'a leg with an unpriced tranche must book NOTHING'


def test_a_clean_complete_close_is_unchanged(bcs_env, monkeypatch):
    """Negative control. The blend must not touch the ordinary path."""
    spy, store = bcs_env
    script = _LegScript(**{B_SHORT: [_complete(B_QTY, 10.00)],
                           B_LONG: [_complete(B_QTY, 40.00)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is True
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert exit_data['short_fill'] == 10.00
    assert exit_data['long_fill'] == 40.00
    assert exit_data['exit_spread'] == pytest.approx(30.00)


# == the FH twin =============================================================
#
# The order-placing FH path is unreachable from the monitor (owner, 2026-08-28
# - Fallen Hero is traded by hand), so this is not a live-money exposure. It is
# fixed and pinned anyway: `_close_fh_inner` is retained precisely because it is
# the only executable statement of what an FH close is, and the D-suite drives
# it directly. A defect left in the reference copy is one the next reader
# inherits.

@pytest.fixture
def fh_env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_fh()])


def test_fh_folds_the_retry_price_into_the_leg(fh_env, monkeypatch):
    spy, store = fh_env
    # Short put: 300 of 400 @ 8.00, residual 100 @ 40.00
    #   -> (2400 + 4000) / 400 = 16.00
    script = _LegScript(**{SP: [_partial(300, 8.00), _complete(100, 40.00)]})
    kite = FakeBroker(books=FH_BOOKS, positions=_fh_pos())
    monkeypatch.setattr(sm, 'close_leg', script)
    ok = sm._close_fh_inner(kite, store, _fh(), spot=3050.0,
                            reason='SL_SPOT', dry_run=False)

    assert ok is True
    exit_data = store.called('update_trade_exit')[0][1][1]
    # close_cost = short_call + short_put - long_call - long_put
    assert exit_data['close_cost'] == pytest.approx(
        FILL[SC] + 16.00 - FILL[LC] - FILL[LP], abs=1e-6), \
        'the short put kept its first tranche price - N13 in the FH twin'
