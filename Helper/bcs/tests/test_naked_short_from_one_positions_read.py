"""One glitchy `positions()` read must not make this engine sell the hedge.

THE DEFECT (found 2026-08-31). `_close_spread_inner`'s preflight takes ONE
`kite.positions()` read, and `get_net_position` returned 0 both for a leg that
is genuinely flat and for a leg whose row is simply MISSING from the response.
The close path then reasoned:

    "SHORT leg already flat/long (qty=0). Skipping BUY."
    "STEP 2: Close LONG leg -> SELL ..."

-- and sold the hedge off a LIVE SHORT. A naked short, manufactured by the
close path itself, in direct violation of the one rule this file is built
around: buy the short back FIRST, always.

The response shape is not hypothetical; this same file documents it as the
reason residue resolution needs two dated reads -- "an empty list during the
early-session sync window, a degraded response, a row simply missing". The
both-legs-flat case (0/0) already got refuse-and-escalate treatment. The
asymmetric case -- short row missing, long row present -- had nothing, and it
is the one that ends with unhedged risk.

THE FIX IS TWO SOURCES, NOT TWO CHECKS. A leg we are short of should HAVE a
row (intraday, or carried forward), so absence is evidence about the RESPONSE
rather than about the position. It is believed only when corroborated by:

  * the row being present and reading 0 -- an ordinary squared-off leg; or
  * the RECORD's own `short_fill` / `close_failed_leg='long'`, written from an
    order result rather than from the position book (this is the M14 recovery
    case: a naked long whose short is known closed); or
  * a second `positions()` read a moment later.

Refusing costs one poll -- the trade stays open and monitoring continues.
Being wrong costs a naked short.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_naked_short_from_one_positions_read.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy)
from bcs.tests.test_d2_partial_close_residue import (             # noqa: E402
    B_LONG, B_QTY, B_SHORT, BCS_BOOKS, _LegScript, _complete)


def _bcs(**extra):
    t = {
        'id': 1, 'stock': 'TESTCO', 'status': 'open',
        'long_symbol': B_LONG, 'short_symbol': B_SHORT,
        'quantity': B_QTY, 'exchange': 'NFO', 'net_debit': 13.55,
        'spread_width': 50, 'spot_symbol': 'NSE:TESTCO',
    }
    t.update(extra)
    return t


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    monkeypatch.setattr(sm, 'reconcile_after_close', lambda *a, **k: True)
    # Never actually sleep in the confirming re-read.
    monkeypatch.setattr(sm.time, 'sleep', lambda *_: None)
    return TelegramSpy().install(monkeypatch, sm)


def _close(kite, store, trade, script):
    import unittest.mock as _m
    with _m.patch.object(sm, 'close_leg', script):
        return sm._close_spread_inner(kite, store, trade, spot=1400.0,
                                      reason='SL_SPREAD', dry_run=False,
                                      label='BCS')


# ── THE DEFECT ─────────────────────────────────────────────────────────────

def test_a_missing_short_row_does_NOT_get_the_long_sold(env):
    """The short is LIVE; its row is missing from a degraded response."""
    store = MemoryStore(trades=[_bcs()])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG,
                                  'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    result = _close(kite, store, _bcs(), script)

    assert result == 'ABORT', result
    assert script.calls == [], 'the long was sold against an unconfirmed short'
    assert kite.placed == []


def test_the_refusal_says_which_symbol_and_why(env):
    store = MemoryStore(trades=[_bcs()])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])

    _close(kite, store, _bcs(), _LegScript())

    joined = '\n'.join(env.sent)
    assert B_SHORT in joined
    assert 'missing from the broker' in joined
    assert 'monitoring continues' in joined, (
        'the operator must be told the trade is still being watched')


def test_the_trade_is_left_OPEN_not_frozen(env):
    """An abort touched nothing, so the record must stay closeable. Freezing
    it here would strand a healthy position on a bad read."""
    store = MemoryStore(trades=[_bcs(status='closing')])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])

    _close(kite, store, _bcs(status='closing'), _LegScript())

    assert store.called('recover_closing_trade'), (
        'the close lock was not released, so nothing can retry the exit')
    assert not store.called('set_trade_status'), 'an abort froze the record'


# ── the three corroborations that must still go through ────────────────────

def test_a_PRESENT_zero_row_is_believed(env):
    """An ordinary squared-off leg: Kite keeps the row at quantity 0."""
    store = MemoryStore(trades=[_bcs()])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_SHORT, 'quantity': 0},
                                 {'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    result = _close(kite, store, _bcs(), script)

    assert result is True
    assert [c['symbol'] for c in script.calls] == [B_LONG]


def test_the_RECORDS_own_short_fill_corroborates(env):
    """M14's naked long: the short's fill was written from an order result,
    which is a different system from the position book."""
    trade = _bcs(short_fill=10.0, close_failed_leg='long')
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    result = _close(kite, store, dict(trade), script)

    assert result is True
    assert [c['symbol'] for c in script.calls] == [B_LONG]


def test_a_close_failure_naming_the_long_leg_corroborates(env):
    """The frozen record's own `close_failure.leg`, same evidence."""
    trade = _bcs(close_failure={'leg': 'long', 'cause': 'unfilled'})
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    assert _close(kite, store, dict(trade), script) is True


def test_a_second_read_that_finds_the_row_lets_it_through(env, monkeypatch):
    """The transient case: the first response was mid-sync, the second is
    complete and the leg really is flat."""
    store = MemoryStore(trades=[_bcs()])
    reads = {'n': 0}

    def detail(kite, symbol):
        if symbol != B_SHORT:
            return B_QTY, True
        reads['n'] += 1
        return (0, False) if reads['n'] == 1 else (0, True)

    monkeypatch.setattr(sm, 'get_net_position_detail', detail)
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})

    assert _close(kite, store, _bcs(), script) is True
    assert reads['n'] == 2, 'the confirming re-read did not happen'


def test_a_second_read_showing_a_LIVE_short_refuses(env, monkeypatch):
    """The read was not merely incomplete -- it was wrong, and the short is
    still there. This is the case that would have gone naked."""
    store = MemoryStore(trades=[_bcs()])
    reads = {'n': 0}

    def detail(kite, symbol):
        if symbol != B_SHORT:
            return B_QTY, True
        reads['n'] += 1
        return (0, False) if reads['n'] == 1 else (-B_QTY, True)

    monkeypatch.setattr(sm, 'get_net_position_detail', detail)
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])
    script = _LegScript()

    assert _close(kite, store, _bcs(), script) == 'ABORT'
    assert script.calls == []


def test_a_raising_second_read_refuses(env, monkeypatch):
    """Fail CLOSED: an unreadable confirmation is not a confirmation."""
    calls = {'n': 0}

    def detail(kite, symbol):
        if symbol != B_SHORT:
            return B_QTY, True
        calls['n'] += 1
        if calls['n'] == 1:
            return 0, False
        raise RuntimeError('Too many requests')

    monkeypatch.setattr(sm, 'get_net_position_detail', detail)
    store = MemoryStore(trades=[_bcs()])
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG, 'quantity': B_QTY}])

    assert _close(kite, store, _bcs(), _LegScript()) == 'ABORT'


# ── the helper in isolation ────────────────────────────────────────────────

def test_get_net_position_detail_separates_absent_from_flat():
    """The distinction the whole fix rests on."""
    kite = FakeBroker(positions=[{'tradingsymbol': B_SHORT, 'quantity': 0}])
    assert sm.get_net_position_detail(kite, B_SHORT) == (0, True)
    assert sm.get_net_position_detail(kite, B_LONG) == (0, False)
    # the old accessor still reads the same as it always did
    assert sm.get_net_position(kite, B_LONG) == 0
