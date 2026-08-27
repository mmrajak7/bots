"""ONE exit-reason vocabulary, and an arming gate that fails closed.

The defect these tests pin down, end to end:

    bcs/spread_monitor.py  writes  exit_reason = "ALREADY_FLAT_TP"
    bcs/zebra_adapter.py   lowercases it        "already_flat_tp"
    zebra/digest.py        counted `k != 'tp'`  -> a STOP EXIT
    zebra/digest.py        reported             "arming gate: 1 stop exit"

A TAKE-PROFIT cleared the one control standing between a cohort of pure TPs
and go-live. The root cause is polarity: `!= 'tp'` treats UNKNOWN as a stop, so
any writer whose vocabulary the reader has not learned manufactures evidence.

And the compounding case is the dangerous one. The already-flat branch is
reached when the monitor finds NO legs at the broker — precisely what happens
if exits are armed against PAPER positions. So the bad arming sequence produces
a run of `already_flat_*` bookings, each read as a stop exit, and the gate then
reports itself cleared: one mistake manufacturing the authorisation for the
next.

Two rules, therefore, and both are tested from the writers' real strings rather
than from a list someone wrote down:

    1. The stop test is an ALLOWLIST. Unrecognised counts for NOTHING.
    2. `ALREADY_FLAT_X` is an X — but a RECOVERED one, and a recovered close
       transacted nothing, so it is not evidence about the stop machinery.
"""
import logging
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import digest                          # noqa: E402
from zebra import outcomes                        # noqa: E402
from zebra.outcomes import (FLAT, HIT, MISS, STOP_KINDS,  # noqa: E402
                            classify, is_stop_exit, label_for_reason)


def _exit(i, reason, pnl=100.0, cohort='2026-08-14'):
    return {'id': i, 'stock': 'X%d' % i, 'status': 'exited', 'cohort': cohort,
            'exit_reason': reason, 'pnl': pnl, 'pnl_net': pnl - 5,
            'pnl_net_pct': 10.0, 'fees': {'basis': 'modelled'}}


def _flags(coh):
    return digest._flags({'gaps': []},
                         {'events': {}, 'blocks_at': [], 'transcripts_tiny': 0,
                          'transcripts': 0},
                         {'closed': []}, {}, coh, None)


# ── the reasons the two engines actually emit ───────────────────────────────
#
# Derived from the source, not from memory:
#   zebra/monitor.py     `_paper_auto_close(store, trade, ..., '<reason>', ...)`
#   bcs/spread_monitor.py `close_spread(kite, trade, spot, "<REASON>", dry_run)`
# lowercased on the way in by bcs/zebra_adapter.update_trade_exit.

PAPER_REASONS = ['tp', 'trail', 'spot_sl', 'debit_sl', 'time', 'expiry']
MONITOR_REASONS = ['tp', 'sl_spot', 'sl_spread', 'sl_trail',
                   'expiry_force_close']
#: The stop half of each, i.e. everything the arming gate is actually about.
PAPER_STOPS = [r for r in PAPER_REASONS if r != 'tp']
MONITOR_STOPS = [r for r in MONITOR_REASONS if r != 'tp']


@pytest.mark.parametrize('reason', PAPER_REASONS + MONITOR_REASONS)
def test_every_reason_a_writer_emits_is_in_the_vocabulary(reason):
    """The whole bug in one assertion. Five of these — the order path's own
    strings — were unknown to every reader, so a real SL_SPREAD scored as
    'no signal' while `already_flat_tp` scored as stop evidence."""
    assert classify(reason)['known'], reason
    assert classify('paper:' + reason)['known'], reason
    assert classify('ALREADY_FLAT_' + reason.upper())['known'], reason


@pytest.mark.parametrize('reason', PAPER_STOPS + MONITOR_STOPS)
def test_each_real_stop_reason_counts_toward_the_gate(reason):
    """Negative control for the allowlist. A gate that no longer accepts a
    genuine stop is not safer, it is broken — and it would never open."""
    assert is_stop_exit(reason), reason
    assert is_stop_exit('paper:' + reason), reason
    assert digest._cohort([_exit(1, 'paper:' + reason, -50)])['stop_exits'] == 1


@pytest.mark.parametrize('reason', ['tp', 'paper:tp', 'TP'])
def test_a_take_profit_is_never_stop_evidence(reason):
    """TP is a SPOT trigger. It never reads the option book, so it says
    nothing about the machinery the gate is waiting on."""
    assert not is_stop_exit(reason)


# ── 1. already_flat_tp is a take-profit, and clears nothing ─────────────────

def test_already_flat_tp_is_classified_as_a_take_profit():
    """`ALREADY_FLAT_X` must be an X, not a species of its own. An already-flat
    take-profit is a take-profit: the TP trigger fired and spot reached the
    target. What changes is HOW it was booked, not WHAT fired."""
    c = classify('already_flat_tp')
    assert c['kind'] == outcomes.TP
    assert c['recovered'] is True
    assert label_for_reason('already_flat_tp') == HIT


def test_already_flat_tp_does_not_count_as_a_stop_exit():
    """THE BUG. `already_flat_tp != 'tp'`, so the old `!=` test scored a
    take-profit as evidence that the stop path works."""
    assert not is_stop_exit('already_flat_tp')


def test_a_cohort_of_already_flat_tps_leaves_the_gate_unmet():
    coh = digest._cohort([_exit(1, 'already_flat_tp'),
                          _exit(2, 'already_flat_tp')])
    assert coh['stop_exits'] == 0
    assert any('ARMING GATE UNMET' in f for f in _flags(coh))


@pytest.mark.parametrize('reason', MONITOR_STOPS)
def test_an_already_flat_stop_is_not_stop_evidence_either(reason):
    """The substantive judgement, and the compounding case.

    An already-flat close placed NO orders — the legs were flat before the
    monitor acted — and its price is back-filled from order history, or from
    0.0 when `_find_last_fill_price` finds nothing. Nothing was demonstrated
    about ordering, depth, slippage, or the price a stop actually fills at,
    which is the entire question the gate asks.

    It is also exactly what arming exits against PAPER positions produces: no
    legs at the broker, every close down this branch. Counting these would let
    that mistake generate its own authorisation."""
    assert classify('already_flat_' + reason)['kind'] in STOP_KINDS
    assert not is_stop_exit('already_flat_' + reason)
    coh = digest._cohort([_exit(1, 'already_flat_' + reason, -50)])
    assert coh['stop_exits'] == 0
    assert any('ARMING GATE UNMET' in f for f in _flags(coh))
    assert any('ALREADY-FLAT' in f for f in _flags(coh))


# ── 2. unknown must never clear the gate ───────────────────────────────────

@pytest.mark.parametrize('reason', ['unknown', '', None, 'sl_gamma',
                                    'reconciled', 'paper:whatever'])
def test_an_unrecognised_reason_does_not_count_as_a_stop(reason):
    """Polarity. `!= 'tp'` made UNKNOWN mean STOP, which is why a single
    writer drifting by one string was enough to clear the gate."""
    assert not classify(reason)['known']
    assert not is_stop_exit(reason)


def test_an_unrecognised_reason_leaves_the_gate_unmet_and_says_so():
    coh = digest._cohort([_exit(1, 'paper:tp'), _exit(2, 'sl_gamma', -50)])
    assert coh['stop_exits'] == 0
    assert coh['unrecognised_exit_reasons'] == {'sl_gamma': 1}
    flags = _flags(coh)
    assert any('ARMING GATE UNMET' in f for f in flags), flags
    # Loud, not merely harmless: silence is how the vocabulary drifted.
    assert any('UNRECOGNISED' in f and 'sl_gamma' in f for f in flags), flags


def test_classify_logs_an_unrecognised_reason(caplog):
    with caplog.at_level(logging.WARNING, logger='zebra.outcomes'):
        classify('sl_gamma')
    assert any('UNRECOGNISED' in r.message or 'UNRECOGNISED' in r.getMessage()
               for r in caplog.records), caplog.records


# ── 3. a real stop must label as a stop, not FLAT ───────────────────────────

@pytest.mark.parametrize('reason,label', [
    ('sl_spread', MISS), ('SL_SPREAD', MISS),
    ('sl_spot', MISS), ('sl_trail', HIT),
    ('expiry_force_close', FLAT), ('paper:expiry', FLAT),
    ('paper:debit_sl', MISS), ('paper:spot_sl', MISS),
])
def test_the_monitors_own_reasons_label_as_themselves_not_flat(reason, label):
    """`_REASON_LABEL` knew only zebra's five names, so every exit the ORDER
    path booked — the only exits that risk real money — scored FLAT, i.e. "no
    signal", in the scorecard being accumulated to decide whether the vet earns
    live authority."""
    assert label_for_reason(reason) == label


def test_pnl_still_overrides_a_positive_label():
    """Doctrine preserved: a fired trail is not proof of a profit. The trigger
    is `mid <= level` and the booking is at `mid`; a gap straight through books
    below the level, possibly below the entry debit."""
    assert label_for_reason('sl_trail') == HIT
    assert label_for_reason('sl_trail', pnl=-1200.0) == MISS
    assert label_for_reason('paper:trail', pnl=-1.0) == MISS
    assert label_for_reason('already_flat_tp', pnl=-1.0) == MISS
    # ...and a MISS that somehow booked a profit stays a MISS.
    assert label_for_reason('sl_spread', pnl=5000.0) == MISS
    # Unknown P&L is not evidence either way.
    assert label_for_reason('sl_trail', pnl=float('nan')) == FLAT


# ── the gate against the REAL cohort ───────────────────────────────────────

def test_the_real_cohort_leaves_the_gate_unmet():
    """The live book, read from disk. Every cohort close to date is a
    take-profit, so the correct answer is UNMET — before this fix and after it.
    This test is the regression that fails the day that stops being true for
    the wrong reason."""
    import json
    p = HELPER / 'logs' / 'zebra_trades.json'
    if not p.exists():                       # pragma: no cover - CI without logs
        pytest.skip('no local trade store')
    rows = json.loads(p.read_text(encoding='utf-8'))
    coh = digest._cohort(rows)
    assert coh['closed'] > 0, 'cohort has no closed trades to reason about'
    assert coh['stop_exits'] == 0, coh['exit_reasons']
    assert not coh['unrecognised_exit_reasons'], \
        'a stored reason no reader understands: %s' % coh['unrecognised_exit_reasons']
    assert any('ARMING GATE UNMET' in f for f in _flags(coh))


# ── the write boundary complains, but never interferes ─────────────────────

def test_the_adapter_warns_on_a_reason_no_reader_knows(caplog):
    """Detection at the write boundary; translation stays at the read boundary.

    The record is FORENSIC — `already_flat_tp` says no orders were placed and
    the price was recovered — so normalising on the way in would destroy the
    one artefact that survives the process. But drift should be noticed where
    it happens, not weeks later in a gate."""
    from bcs import zebra_adapter

    booked = {}

    class _Store:
        def mark_exited(self, trade_id, **kw):
            booked.update(kw, trade_id=trade_id)
            return {'id': trade_id}

    a = zebra_adapter.ZebraStoreAdapter(_Store())
    with caplog.at_level(logging.WARNING):
        a.update_trade_exit(7, {'exit_reason': 'SL_GAMMA', 'exit_spot': 100.0,
                                'exit_spread': 1.0})
    # Stored verbatim (lowercased) — NOT normalised, NOT dropped.
    assert booked['reason'] == 'sl_gamma'
    assert any('sl_gamma' in r.getMessage() for r in caplog.records)


def test_a_known_reason_passes_the_write_boundary_quietly():
    """Negative control: the warning must mean something."""
    from bcs import zebra_adapter

    booked = {}

    class _Store:
        def mark_exited(self, trade_id, **kw):
            booked.update(kw)
            return {'id': trade_id}

    zebra_adapter.ZebraStoreAdapter(_Store()).update_trade_exit(
        7, {'exit_reason': 'ALREADY_FLAT_TP', 'exit_spot': 100.0,
            'exit_spread': 1.0})
    assert booked['reason'] == 'already_flat_tp'
