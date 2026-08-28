"""M14 §4 - the recovery sweep can only ever move a position toward zero.

    "The recovery sweep can never increase the absolute quantity of any leg.
     Every order it places moves a position toward zero."
                                        - M14_RECOVERY_DESIGN.md, §4

The design calls this "implement first, test hardest", and this file is the
first half. Recovery is the only automation here that places orders at a
position the machine has already failed to close once, with no human in the
loop, on a state it INFERRED from the broker rather than one it created. The
narrow action space is the whole safety argument; this is the wall around it.

**The rejected design, pinned as a test.** "Re-buy the long to restore the
hedge" is an entry-shaped order on the money path: it increases premium at
risk, re-opens a position a stop already wanted closed, contradicts the
fail-closed entry doctrine, and makes the invariant untestable. It is caught
here by the same arithmetic that catches an oversized buyback, which is the
point of stating the rule on |quantity| rather than as two rules about BUY and
SELL.

**Asserted in `FakeBroker`, not at call sites** - so every replay and every
fixture checks it, and a future edit to any close path trips it without anyone
remembering to test for it. `reduce_only` is opt-in per fixture for the same
reason `hedge_pairs` is: an ENTRY legitimately opens a position, and an
always-on rule would condemn `bcs/entry_executor.py` for doing its job.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_reduce_only_invariant.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs.tests.fakes import (FakeBroker, ReduceOnlyViolation,      # noqa: E402
                             always_complete)

SYM = 'TESTCO26SEP1390CE'


def _broker(qty, **kw):
    return FakeBroker(positions=[{'tradingsymbol': SYM, 'quantity': qty}],
                      reduce_only=True, **kw)


def _order(broker, txn, qty, symbol=SYM):
    return broker.place_order(variety='regular', exchange='NFO',
                              tradingsymbol=symbol, transaction_type=txn,
                              quantity=qty, order_type='LIMIT', price=10.0,
                              product='NRML', tag='BCS_MON')


# == what recovery is ALLOWED to do =========================================

def test_buying_back_a_short_is_allowed():
    b = _broker(-700)
    _order(b, 'BUY', 700)
    assert b.net_qty(SYM) == 0


def test_a_partial_buyback_is_allowed():
    """Closing 200 of a 700 short still moves toward zero."""
    b = _broker(-700)
    _order(b, 'BUY', 200)
    assert b.net_qty(SYM) == -500


def test_selling_a_long_is_allowed():
    b = _broker(700)
    _order(b, 'SELL', 700)
    assert b.net_qty(SYM) == 0


# == what it may NEVER do ===================================================

def test_the_rejected_design_rebuying_a_hedge_is_refused():
    """THE named rejection. A BUY against a FLAT book opens a position, and
    "restore the hedge" is exactly that order wearing a helpful name."""
    b = _broker(0)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'BUY', 700)


def test_opening_a_short_is_refused():
    b = _broker(0)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'SELL', 700)


def test_adding_to_an_existing_long_is_refused():
    b = _broker(700)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'BUY', 700)


def test_adding_to_an_existing_short_is_refused():
    b = _broker(-700)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'SELL', 700)


def test_OVER_closing_is_refused_too():
    """Buying 700 against a short of 200 leaves the book LONG 500 - a new
    position, opened by a routine whose only licence is removing them.
    `close_leg` re-reads the position and sizes to it, so this can fire only
    if that stops being true, which is exactly when it must."""
    b = _broker(-200)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'BUY', 700)


def test_closing_EXACTLY_to_flat_is_the_boundary_and_is_allowed():
    """|before| -> |after| must be allowed to reach zero, obviously, but the
    off-by-one that would forbid it is easy to write."""
    b = _broker(-200)
    _order(b, 'BUY', 200)
    assert b.net_qty(SYM) == 0


# == the refusal happens BEFORE the order exists ============================

def test_a_refused_order_is_never_recorded_or_filled():
    """Same placement as the hedge invariant: a test must not be able to
    assert against a book the invariant already condemned."""
    b = _broker(0)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'BUY', 700)
    assert b.placed == []
    assert b.order_book == []
    assert b.net_qty(SYM) == 0
    assert len(b.reduce_only_violations) == 1


def test_it_survives_the_close_paths_blanket_exception_handler():
    """`BaseException`, not `Exception`. Both close sequences wrap themselves
    in `except Exception`, so an ordinary exception here would be swallowed
    and reported to the test as a tidy freeze - hiding the violation behind
    the handler that exists to contain surprises."""
    assert issubclass(ReduceOnlyViolation, BaseException)
    assert not issubclass(ReduceOnlyViolation, Exception)

    b = _broker(0)
    try:
        try:
            _order(b, 'BUY', 700)
        except Exception:                      # noqa: BLE001 - the point
            pytest.fail('an `except Exception` swallowed the violation')
    except ReduceOnlyViolation:
        pass


# == it is OFF by default, and that is deliberate ===========================

def test_an_entry_is_not_condemned_when_the_rail_is_off():
    """Default-off. `bcs/entry_executor.py` opens positions for a living; an
    always-on rule would make the fixture unusable for entry tests."""
    b = FakeBroker(positions=[])
    b.place_order(variety='regular', exchange='NFO', tradingsymbol=SYM,
                  transaction_type='BUY', quantity=700, order_type='LIMIT',
                  price=10.0, product='NRML', tag='BCS_MON')
    assert b.net_qty(SYM) == 700
    assert b.reduce_only_violations == []


def test_arming_it_is_a_single_constructor_flag():
    """If arming ever gets harder than this, the sweep's tests will quietly
    stop doing it - which is how a rail becomes decorative."""
    assert FakeBroker(reduce_only=True).reduce_only is True
    assert FakeBroker().reduce_only is False


# == multi-leg: the rule is per symbol ======================================

def test_each_leg_is_judged_on_its_own_position():
    """A spread close touches two symbols; a violation on one must not be
    excused by the other moving the right way."""
    other = 'TESTCO26SEP1340CE'
    b = FakeBroker(positions=[{'tradingsymbol': SYM, 'quantity': -700},
                              {'tradingsymbol': other, 'quantity': 700}],
                   reduce_only=True)
    _order(b, 'BUY', 700)                      # short -> flat, fine
    _order(b, 'SELL', 700, symbol=other)       # long  -> flat, fine
    assert b.net_qty(SYM) == 0 and b.net_qty(other) == 0
    with pytest.raises(ReduceOnlyViolation):   # now both are flat
        _order(b, 'BUY', 700)


def test_an_unknown_symbol_counts_as_flat():
    """A leg the fixture never declared has no position, so ANY order on it
    opens one. Failing closed on the unknown case is the whole doctrine."""
    b = FakeBroker(positions=[], reduce_only=True)
    with pytest.raises(ReduceOnlyViolation):
        _order(b, 'BUY', 700, symbol='NEVERHEARDOF26SEP100CE')
