"""An order that cannot be proven dead must NEVER be replaced by another one.

THE DEFECT THIS PINS (found 2026-08-31). `open_leg` timed out, called
`cancel_order_safe` -- which SWALLOWS a failed cancel -- then read
`_order_final_state`, whose own docstring says "callers must treat None as
unknown, never as did not fill". The loop then fell through to the next
attempt on None, and on a state that was still OPEN, and placed a SECOND live
order for the same leg.

Why that is the worst shape available: Kite rate-limits the quote family at
1/sec, and this box has logged `Too many requests` for a full session
(2026-08-27). One burst refuses the cancel AND the follow-up `orders()` read.
The original order then fills alongside the retry's fill, giving TWO short
lots against ONE long lot -- a net NAKED SHORT. That is the Feb-2026
four-fill shape, moved from the exit path to the entry path, and it defeats
the long-first sequencing that exists to make naked shorts impossible.

The rule now: retry only on a TERMINAL status (CANCELLED/REJECTED) carrying
no fill. Everything else stops the run and is reported as UNKNOWN -- which
the caller must never describe as "nothing extra held".

Run:  cd Helper && python -m pytest bcs/tests/test_entry_retry_on_unknown.py -v
"""
import datetime as _dt

import pytest

from bcs import entry_executor as ee
from bcs import spread_monitor as sm
from zebra import config as cfg

from .test_entry_executor import (Broker, EX, LOT, LONG, SHORT, _pin_clock,
                                  run)


class UnknownBroker(Broker):
    """Times out, then answers the post-cancel state check per a script.

    `final_states` is consumed one per cancel: a dict is returned as the
    order's state, None means the order book could not be read at all.
    """

    def __init__(self, final_states, fills=None):
        super().__init__(fills=fills)
        self.final_states = list(final_states)

    def cancel(self, kite, order_id, dry_run):
        # `cancel_order_safe` catches everything, so a REFUSED cancel is
        # invisible to open_leg. Modelled as a no-op that still records.
        self.cancelled.append(order_id)

    def final(self, kite, order_id):
        return self.final_states.pop(0) if self.final_states else None


@pytest.fixture
def unknown(monkeypatch):
    def _make(final_states, fills=None):
        b = UnknownBroker(final_states, fills=fills)
        monkeypatch.setattr(sm, 'get_option_depth', b.depth)
        monkeypatch.setattr(sm, 'place_limit_order', b.place)
        monkeypatch.setattr(sm, 'wait_for_fill', b.wait)
        monkeypatch.setattr(sm, 'cancel_order_safe', b.cancel)
        monkeypatch.setattr(sm, '_order_final_state', b.final)
        monkeypatch.setattr(sm.time, 'sleep', lambda s: None)
        # These tests are all about the LIVE order path, so both arming
        # switches are held open deliberately -- the gate itself is pinned
        # elsewhere (`test_auto_entry_is_off_by_default`).
        monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
        monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
        _pin_clock(monkeypatch, _dt.datetime(2026, 9, 15, 11, 0, 0))
        return b
    return _make


# -- the core rule ----------------------------------------------------------

def test_an_unreadable_order_book_does_not_get_a_second_order(unknown):
    """`_order_final_state` -> None is UNKNOWN. Exactly one order is placed."""
    b = unknown([None], fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)

    long_orders = [p for p in b.placed if p['symbol'] == LONG]
    assert len(long_orders) == 1, (
        'a second order was placed against an order of unknown state -- this '
        'is the naked-short path')
    assert out['lots_filled'] == 0
    assert out['unknown_orders'] and out['unknown_orders'][0]['leg'] == 'long'


def test_a_still_open_order_does_not_get_a_second_order(unknown):
    """A cancel that did not take leaves status OPEN. Still not dead."""
    b = unknown([{'status': 'OPEN', 'filled_quantity': 0, 'order_id': 'O1'}],
                fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert len([p for p in b.placed if p['symbol'] == LONG]) == 1
    assert out['unknown_orders']


def test_a_confirmed_cancelled_order_IS_retried(unknown):
    """The retry must survive. Proves the fix did not just disable retrying."""
    b = unknown([{'status': 'CANCELLED', 'filled_quantity': 0,
                  'order_id': 'O1'}],
                fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert len([p for p in b.placed if p['symbol'] == LONG]) == 2, (
        'a terminal status with no fill is safe to retry and must still be')
    assert out['lots_filled'] == 1


def test_a_confirmed_rejected_order_IS_retried(unknown):
    b = unknown([{'status': 'REJECTED', 'filled_quantity': 0,
                  'order_id': 'O1'}],
                fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert len([p for p in b.placed if p['symbol'] == LONG]) == 2
    assert out['lots_filled'] == 1


def test_a_fill_in_the_cancel_race_is_still_adopted(unknown):
    """The pre-existing race handling must be untouched by the new guard."""
    b = unknown([{'status': 'COMPLETE', 'average_price': 30.2,
                  'filled_quantity': LOT, 'order_id': 'O1'}],
                fills={LONG: ['TIMEOUT']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert out['long_fills'] == [30.2]


# -- what the caller is told ------------------------------------------------

def test_an_unknown_long_is_never_called_nothing_extra_held(unknown):
    """The old message was a false statement about the world."""
    b = unknown([None], fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    joined = ' '.join(out['problems'])
    assert 'nothing extra held' not in joined
    assert 'LIVE at the broker' in joined
    assert 'O1' in joined, 'the operator needs the order id to check Kite'


def test_an_unknown_short_reports_BOTH_the_orphan_and_the_lost_order(unknown):
    """Long filled, short's fate unknown: an orphan long AND a live unknown."""
    b = unknown([None], fills={LONG: ['FILL'], SHORT: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)

    assert len([p for p in b.placed if p['symbol'] == SHORT]) == 1
    assert out['orphan'] and out['orphan']['symbol'] == LONG
    assert out['unknown_orders'][0]['leg'] == 'short'
    assert out['lots_filled'] == 0, (
        'a spread whose short cannot be proven is not a spread')


def test_the_telegram_names_the_unknown_order(unknown):
    b = unknown([None], fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert sent, 'an entry that lost an order must alert'
    assert 'ORDER STATE UNKNOWN' in sent[0]


def test_a_run_with_a_lost_order_never_prints_complete(unknown):
    """Filling everything asked for does not make the run clean."""
    b = unknown([None], fills={LONG: ['FILL'], SHORT: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=False)
    assert not any('ENTRY COMPLETE' in s for s in said)


# -- dry run keeps its old behaviour ----------------------------------------

def test_dry_run_still_retries(unknown):
    """`_order_final_state` is deliberately not called under dry run, so None
    there means "no broker", not "unknown". The rehearsal must still rehearse
    the retry."""
    b = unknown([], fills={LONG: ['TIMEOUT', 'FILL']})
    out, said, sent = run(b, lots=1, dry_run=True)
    assert len([p for p in b.placed if p['symbol'] == LONG]) == 2
    assert out['lots_filled'] == 1
    assert not out['unknown_orders']
