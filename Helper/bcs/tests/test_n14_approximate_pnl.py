"""N14 - the BCS twin admitted its P&L was approximate to the LOG and to nobody else.

`_close_spread_inner` has always logged

    NOTE: Short was already flat. P&L is approximate (long fill only).

and then persisted a record that read as EXACT. The missing leg is counted at
0.00, so the figure is not merely uncertain - it is **wrong in a known
direction** - and nothing downstream could tell: not `pnl_net`, not the
digest's cohort running total, not `bcs/journal_report.py`, not the arming
gate's own evidence.

Fallen Hero had already been fixed (D4): it marks `pnl_approximate`, surfaces
`exit_approximate` at the top level, and clamps at the store's write boundary.
BCS did none of it. This file is the twin, and it pins the same four properties
on the same terms:

    1. MARK      - an explicit `None` leg fill makes the figure approximate
    2. CLAMP     - `exit_spread` is held inside [0, width], and the P&L is
                   RE-DERIVED from the clamped value, not scaled
    3. PROPAGATE - the marker crosses `bcs/zebra_adapter.py` into the zebra
                   book in the SAME locked write as the number it qualifies
    4. SURFACE   - `journal_report` and the digest say so out loud

Negative controls carry as much weight as the regressions: a clean two-leg
close must NOT be marked, and records closed before the marker existed must not
retroactively grow a caveat. ~450 of those exist; a caveat on every line is how
a caveat stops being read.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_n14_approximate_pnl.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import journal_report                                     # noqa: E402
from bcs import spread_monitor as sm                               # noqa: E402
from bcs import trade_store as bts                                 # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)
from bcs.tests.test_d2_partial_close_residue import (              # noqa: E402
    B_LONG, B_QTY, B_SHORT, BCS_BOOKS, _LegScript, _bcs, _complete)

#: A 50-wide vertical bought for 13.55 - the ICICI 1360/1410 shape.
TRADE = {'id': 1, 'stock': 'TESTCO', 'quantity': 700,
         'spread_width': 50.0, 'net_debit': 13.55}


# == 1. MARK =================================================================

def test_an_explicitly_unknown_leg_makes_the_figure_approximate():
    out = bts.bound_bcs_exit(TRADE, {'short_fill': None, 'long_fill': 40.0,
                                     'exit_spread': 40.0})
    assert out['pnl_approximate'] is True
    assert out['unpriced_legs'] == ['short_fill']


def test_a_MISSING_leg_key_is_not_the_same_as_an_unknown_one():
    """A hand-written exit with no per-leg detail has an exact number and no
    breakdown. Inferring doubt from silence would caveat every manual record."""
    out = bts.bound_bcs_exit(TRADE, {'exit_spread': 40.0})
    assert 'pnl_approximate' not in out


def test_a_clean_two_leg_exit_is_not_marked():
    out = bts.bound_bcs_exit(TRADE, {'short_fill': 10.20, 'long_fill': 50.20,
                                     'exit_spread': 40.0})
    assert 'pnl_approximate' not in out


# == 2. CLAMP ================================================================

def test_an_exit_value_above_the_width_is_clamped_and_marked():
    out = bts.bound_bcs_exit(TRADE, {'exit_spread': 62.0,
                                     'pnl_per_share': 48.45})
    assert out['exit_spread'] == 50.0
    assert out['pnl_clamped_from'] == 62.0
    assert out['pnl_approximate'] is True


def test_a_negative_exit_value_is_clamped_to_zero():
    """PIIND #50 booked -112.4% on a -100%-capped structure for want of this."""
    out = bts.bound_bcs_exit(TRADE, {'exit_spread': -30.04,
                                     'pnl_per_share': -43.59})
    assert out['exit_spread'] == 0.0
    assert out['pnl_clamped_from'] == -30.04
    assert out['pnl_approximate'] is True


def test_the_pnl_is_REDERIVED_from_the_clamp_not_scaled():
    """For a vertical the P&L is exactly `exit_spread - net_debit`, so the
    clamped value has ONE correct answer; a ratio would invent a second."""
    out = bts.bound_bcs_exit(TRADE, {'exit_spread': 62.0,
                                     'pnl_per_share': 48.45,
                                     'total_pnl': 33915.0})
    assert out['pnl_per_share'] == pytest.approx(50.0 - 13.55)
    assert out['total_pnl'] == pytest.approx((50.0 - 13.55) * 700)


def test_a_value_inside_the_bounds_is_left_completely_alone():
    out = bts.bound_bcs_exit(TRADE, {'exit_spread': 40.0,
                                     'pnl_per_share': 26.45})
    assert out['exit_spread'] == 40.0
    assert 'pnl_clamped_from' not in out and 'pnl_approximate' not in out


@pytest.mark.parametrize('trade', [
    {'id': 1, 'net_debit': 13.55},                       # no width
    {'id': 1, 'spread_width': 0, 'net_debit': 13.55},    # nonsense width
    {'id': 1, 'spread_width': 'x', 'net_debit': 13.55},  # unparseable
])
def test_nothing_to_bound_against_does_not_strand_the_close(trade):
    """Refusing the whole write over a missing optional field would strand a
    close already made at the broker."""
    out = bts.bound_bcs_exit(trade, {'exit_spread': 999.0})
    assert out['exit_spread'] == 999.0


def test_it_never_mutates_the_callers_dict():
    given = {'exit_spread': 62.0, 'pnl_per_share': 48.45}
    bts.bound_bcs_exit(TRADE, given)
    assert given == {'exit_spread': 62.0, 'pnl_per_share': 48.45}


# == exit_is_approximate reads BOTH places ===================================

def test_the_marker_is_found_at_the_top_level_and_inside_exit():
    assert bts.exit_is_approximate({'exit_approximate': True}) is True
    assert bts.exit_is_approximate({'exit': {'pnl_approximate': True}}) is True


def test_absence_means_exact():
    """~450 records predate the marker; they must not grow a caveat."""
    assert bts.exit_is_approximate({}) is False
    assert bts.exit_is_approximate({'exit': {'total_pnl': 100}}) is False


# == the monitor marks it at the source ======================================

@pytest.fixture
def bcs_env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_bcs()])


def test_an_already_flat_short_leg_records_the_leg_as_UNKNOWN(bcs_env,
                                                              monkeypatch):
    """The log said "approximate"; the record has to as well."""
    spy, store = bcs_env
    script = _LegScript(**{B_LONG: [_complete(B_QTY, 40.00)]})
    monkeypatch.setattr(sm, 'close_leg', script)
    # Short already flat at the broker; only the long is live.
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_LONG,
                                  'quantity': B_QTY}])
    ok = sm._close_spread_inner(kite, store, _bcs(), spot=1400.0,
                                reason='SL_SPREAD', dry_run=False, label='BCS')

    assert ok is True
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert exit_data['pnl_approximate'] is True
    assert exit_data['unpriced_legs'] == ['short_fill']
    assert exit_data['short_fill'] is None, \
        'an unclosed leg must be UNKNOWN, never the 0.00 the arithmetic used'
    assert exit_data['long_fill'] == 40.00


def test_a_normal_two_leg_close_carries_no_marker(bcs_env, monkeypatch):
    """Negative control at the source."""
    spy, store = bcs_env
    script = _LegScript(**{B_SHORT: [_complete(B_QTY, 10.00)],
                           B_LONG: [_complete(B_QTY, 40.00)]})
    monkeypatch.setattr(sm, 'close_leg', script)
    kite = FakeBroker(books=BCS_BOOKS,
                      positions=[{'tradingsymbol': B_SHORT,
                                  'quantity': -B_QTY},
                                 {'tradingsymbol': B_LONG,
                                  'quantity': B_QTY}])
    ok = sm._close_spread_inner(kite, store, _bcs(), spot=1400.0,
                                reason='SL_SPREAD', dry_run=False, label='BCS')

    assert ok is True
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert 'pnl_approximate' not in exit_data
    assert exit_data['short_fill'] == 10.00 and exit_data['long_fill'] == 40.00


# == 3. PROPAGATE - across the bridge, in ONE locked write ===================

def test_the_marker_crosses_the_bridge_into_the_zebra_book(tmp_path,
                                                           monkeypatch):
    from bcs.zebra_adapter import ZebraStoreAdapter
    from zebra import config as zcfg
    from zebra.trade_store import ZebraStore
    from bcs.tests.test_exit_bridge_real_store import ENTERED

    d = tmp_path / 'zebra'
    d.mkdir()
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text(json.dumps([dict(ENTERED)]))
    store = ZebraStore(config={'google_drive': {'enabled': False}})
    store.initialize()
    assert not store._drive_enabled

    ZebraStoreAdapter(store).update_trade_exit(419, {
        'exit_spot': 1401.0, 'exit_reason': 'SL_SPREAD',
        'exit_spread': 40.0, 'short_fill': None, 'long_fill': 40.0,
        'pnl_approximate': True,
    })
    rec = [t for t in store.load_trades() if t['id'] == 419][0]
    assert rec['status'] == 'exited'
    assert rec['exit_approximate'] is True, \
        'a bridged approximation must not read as exact in the zebra book'


def test_an_exact_bridged_close_is_not_marked(tmp_path, monkeypatch):
    from bcs.zebra_adapter import ZebraStoreAdapter
    from zebra import config as zcfg
    from zebra.trade_store import ZebraStore
    from bcs.tests.test_exit_bridge_real_store import ENTERED

    d = tmp_path / 'zebra'
    d.mkdir()
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text(json.dumps([dict(ENTERED)]))
    store = ZebraStore(config={'google_drive': {'enabled': False}})
    store.initialize()

    ZebraStoreAdapter(store).update_trade_exit(419, {
        'exit_spot': 1401.0, 'exit_reason': 'tp',
        'exit_spread': 40.0, 'short_fill': 10.20, 'long_fill': 50.20,
    })
    rec = [t for t in store.load_trades() if t['id'] == 419][0]
    assert 'exit_approximate' not in rec


# == 4. SURFACE ==============================================================

def test_journal_report_reads_both_schemas():
    assert journal_report._exit_is_approximate({'exit_approximate': True})
    assert journal_report._exit_is_approximate(
        {'exit': {'pnl_approximate': True}})
    assert not journal_report._exit_is_approximate({'exit': {'total_pnl': 1}})


def test_the_digest_counts_approximate_cohort_exits():
    from zebra import digest
    rows = [
        {'id': 1, 'stock': 'A', 'status': 'exited', 'cohort': '2026-08-14',
         'exit_reason': 'paper:tp', 'pnl': 100.0, 'pnl_net': 95.0,
         'pnl_net_pct': 10.0, 'exit_date': '2026-08-24',
         'fees': {'basis': 'modelled'}},
        {'id': 2, 'stock': 'B', 'status': 'exited', 'cohort': '2026-08-14',
         'exit_reason': 'sl_spread', 'pnl': -50.0, 'pnl_net': -55.0,
         'pnl_net_pct': -5.0, 'exit_date': '2026-08-24',
         'exit_approximate': True, 'fees': {'basis': 'modelled'}},
    ]
    coh = digest._cohort(rows, '2026-08-28')
    assert coh['closed'] == 2
    assert coh['approximate'] == 1


def test_a_cohort_with_no_approximations_reports_none():
    from zebra import digest
    rows = [{'id': 1, 'stock': 'A', 'status': 'exited',
             'cohort': '2026-08-14', 'exit_reason': 'paper:tp', 'pnl': 100.0,
             'pnl_net': 95.0, 'pnl_net_pct': 10.0, 'exit_date': '2026-08-24',
             'fees': {'basis': 'modelled'}}]
    assert digest._cohort(rows, '2026-08-28')['approximate'] == 0


def test_the_live_cohort_has_no_approximate_exits_today():
    """A fact worth pinning: all 7 cohort closes to date carry a full exit
    book. The day that changes, this fails and someone reads why."""
    p = HELPER / 'logs' / 'zebra_trades.json'
    if not p.exists():                       # pragma: no cover - CI without logs
        pytest.skip('no local trade store')
    from zebra import digest
    rows = json.loads(p.read_text(encoding='utf-8'))
    assert digest._cohort(rows, '2026-08-28')['approximate'] == 0
