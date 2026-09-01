"""Two entry-path holes that re-open the duplicate-order amplification.

Found 2026-08-31. `_entry_already_in_flight` was built on 2026-08-31 to stop a
signal being sent back to the order path while a leg from the previous attempt
might still be live at the broker. It has two sources: the `entry_residue`
record, and the order journal. BOTH were blind to two of `open_spread`'s
outcomes, because in each case the structured output is assigned AFTER the call
that failed:

1. **The order path RAISED between the legs.** `place_limit_order` re-raises on
   a broker exception (deliberately — the journal keeps the record). A round
   that bought its long and then hit a `TokenException` on the short reported
   PROSE only: no `orphan`, no `partials`, so `_entry_residue_legs` returned
   `{}` and `_record_entry_residue` wrote nothing at all.

2. **An order could not be confirmed dead.** `open_leg` returns
   `{'unknown': True, ...}` when a rate-limited cancel plus an unreadable
   `orders()` leave an order that may be working RIGHT NOW (the documented
   2026-08-27 `Too many requests` shape). `unknown_orders` was recorded on
   `out` and never read by the residue extractor.

Either way the next cycle found no residue, the journal intent was already
resolved, and the SAME signal went back to the order path — buying another
long, every cycle, none of them in any store, invisible to `capital.check`,
to `--list`, and to every sweep.

The unknown-SHORT case is the sharp one: an unknown short filling later beside
a new round's short is two lots short against one long — the net naked short
that long-first sequencing exists to make impossible, achieved across cycles
instead of within a run.

MUST LAND BEFORE `auto_entry` IS ARMED. Latent today only because the live
order path is switched off.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_entry_residue_gaps.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import entry_executor as ee                             # noqa: E402
from zebra.monitor import _entry_residue_legs                    # noqa: E402

LONG, SHORT = 'TESTCO26SEP100CE', 'TESTCO26SEP110CE'
LOT = 700


def _run(monkeypatch, leg_behaviour, lots=1):
    """Drive `open_spread` with `open_leg` replaced. Returns `out`."""
    monkeypatch.setattr(ee, 'open_leg', leg_behaviour)
    monkeypatch.setattr(ee, 'prospective_debit', lambda *a, **k: 1.0)
    monkeypatch.setattr(ee, 'entries_allowed', lambda log=None: (True, ''))
    return ee.open_spread(
        kite=None, stock='TESTCO', long_symbol=LONG, short_symbol=SHORT,
        exchange='NFO', lots=lots, lot_size=LOT, dry_run=False,
        gated_debit=None, trade_id=1, log=lambda *a, **k: None,
        telegram=lambda *a, **k: None)


def _filled(price=10.0):
    return {'status': 'COMPLETE', 'filled_quantity': LOT,
            'average_price': price, 'partial': False}


# ── 1. the order path raised between the legs ──────────────────────────────

def test_a_raise_on_the_SHORT_leg_names_both_legs(monkeypatch):
    """THE DEFECT. The long is HELD and the short's fate is unknown; pre-fix
    `out` carried prose and nothing else."""
    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        if symbol == LONG:
            return _filled()
        raise RuntimeError('TokenException: api_key/access_token invalid')

    out = _run(monkeypatch, legs)

    assert out['raised_legs'] == {LONG: LOT, SHORT: 0}, out.get('raised_legs')
    legs_seen = _entry_residue_legs(out)
    assert legs_seen == {LONG: LOT, SHORT: 0}, (
        'the residue record would be EMPTY, so the same signal re-enters')


def test_a_raise_on_the_LONG_leg_still_names_it(monkeypatch):
    """Nothing is proven filled, but the order may have reached the broker
    before the exception. Quantity 0 means ask."""
    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        raise RuntimeError('NetworkException')

    out = _run(monkeypatch, legs)

    assert out['raised_legs'] == {LONG: 0}
    assert _entry_residue_legs(out) == {LONG: 0}


def test_a_clean_run_leaves_nothing_in_flight(monkeypatch):
    """The negative control. A completed spread is accounted for by its
    record, so it must NOT look like residue."""
    out = _run(monkeypatch, lambda *a, **k: _filled())

    assert out['lots_filled'] == 1
    assert 'in_flight' not in out, 'a completed round left a phantom leg'
    assert 'raised_legs' not in out
    assert _entry_residue_legs(out) == {}


def test_a_completed_round_before_a_raise_is_not_counted_twice(monkeypatch):
    """Round 1 completes, round 2 raises on its long. The completed spread is
    a RECORD, not residue; only round 2's leg is unaccounted for."""
    calls = {'n': 0}

    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        calls['n'] += 1
        if calls['n'] <= 2:
            return _filled()
        raise RuntimeError('boom')

    out = _run(monkeypatch, legs, lots=2)

    assert out['lots_filled'] == 1
    assert out['raised_legs'] == {LONG: 0}


def test_an_orphan_still_works_as_before(monkeypatch):
    """The pre-existing source must not regress: a short that simply does not
    fill is an orphan long at a KNOWN size."""
    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        return _filled() if symbol == LONG else None

    out = _run(monkeypatch, legs)

    assert out['orphan']['symbol'] == LONG
    assert _entry_residue_legs(out) == {LONG: LOT}


# ── 2. an order that could not be confirmed dead ───────────────────────────

def test_an_unknown_SHORT_order_reaches_the_residue(monkeypatch):
    """THE NAKED-SHORT PATH. It may fill later beside a new round's short."""
    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        if symbol == LONG:
            return _filled()
        return {'unknown': True, 'order_id': '2508', 'symbol': SHORT,
                'status': 'OPEN', 'filled_quantity': 0}

    out = _run(monkeypatch, legs)

    assert out['unknown_orders'], 'the executor did not report it at all'
    assert SHORT in _entry_residue_legs(out), (
        'an order that may be LIVE at the broker was left out of the residue')


def test_an_unknown_LONG_order_reaches_the_residue(monkeypatch):
    """`lots_filled == 0` and no orphan: pre-fix this produced NOTHING."""
    def legs(kite, exchange, symbol, is_buy, qty, dry_run, **kw):
        return {'unknown': True, 'order_id': '2507', 'symbol': LONG,
                'status': 'unreadable', 'filled_quantity': 0}

    out = _run(monkeypatch, legs)

    assert _entry_residue_legs(out) == {LONG: 0}, (
        'a possibly-live long order was invisible to every sweep')


def test_an_unknown_does_not_clobber_a_known_orphan_size(monkeypatch):
    """`max`, not `+`. An unknown carries no proven quantity, so it must
    neither overwrite a known size nor inflate it."""
    out = {'orphan': {'symbol': LONG, 'qty': LOT},
           'unknown_orders': [{'symbol': LONG, 'order_id': 'x'}]}
    assert _entry_residue_legs(out) == {LONG: LOT}


def test_the_caller_supplied_extra_still_adds(monkeypatch):
    """`extra` is the record-could-not-be-written case and keeps its old
    additive behaviour."""
    assert _entry_residue_legs({}, extra={LONG: 0, SHORT: 0}) == {LONG: 0,
                                                                 SHORT: 0}
