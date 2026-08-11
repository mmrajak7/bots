"""Joining decisions to what actually happened.

Until this existed, `score()` read a field nothing ever wrote — the journal
recorded every verdict and could never say whether one was right. These tests
pin the two joins and, above all, the boundary between them: a vetoed signal
has NO price, so it must never contribute a rupee figure.

Run:  cd Helper && python -m pytest zebra/tests/test_outcomes.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg              # noqa: E402
from zebra import outcomes                   # noqa: E402
from zebra import vet as vet_mod             # noqa: E402
from zebra.decisions import DecisionStore    # noqa: E402
from zebra.trade_store import ZebraStore     # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
ENTRY = {'long_strike': 96.0, 'short_strike': 100.0,
         'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
         'debit': 10.0, 'lot_size': 100, 'lots': 1, 'expiry': '2026-09-24'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    s = ZebraStore(config={})
    s._load_local()
    return s


@pytest.fixture
def decisions(tmp_path):
    return DecisionStore(path=tmp_path / 'dec.json',
                         lock_path=tmp_path / 'dec.lock').initialize()


def _vetoed(store, decision_id=1):
    store.add_signal(dict(SIGNAL))
    with store._mutate():
        store.find(1)['vet'] = {'state': 'vetoed', 'decision_id': decision_id}
    return outcomes.open_shadow(store, 1)


# ── the shadow: barriers frozen at veto time ─────────────────────────────
def test_shadow_barriers_are_symmetric_around_entry(store):
    s = _vetoed(store)
    # entry 96, target (ST) 100 -> distance 4 -> adverse 92. Symmetric on
    # purpose: an arbitrary stop width would tilt the label.
    assert s['target_spot'] == 100.0 and s['adverse_spot'] == 92.0


def test_shadow_is_idempotent(store):
    first = _vetoed(store)
    again = outcomes.open_shadow(store, 1)
    assert again['opened_at'] == first['opened_at']


def test_target_touch_labels_the_signal_a_hit(store):
    _vetoed(store)
    assert outcomes.track_shadows(store, {'TESTCO': 100.5})
    assert store.find(1)['veto_shadow']['label'] == outcomes.HIT


def test_adverse_touch_labels_a_miss(store):
    _vetoed(store)
    outcomes.track_shadows(store, {'TESTCO': 91.0})
    assert store.find(1)['veto_shadow']['label'] == outcomes.MISS


def test_pe_direction_inverts_the_barriers(store):
    store.add_signal(dict(SIGNAL, direction='PE', st_value=92.0,
                          st_direction='DOWN'))
    with store._mutate():
        store.find(1)['vet'] = {'state': 'vetoed', 'decision_id': 1}
    s = outcomes.open_shadow(store, 1)
    assert s['target_spot'] == 92.0 and s['adverse_spot'] == 100.0
    outcomes.track_shadows(store, {'TESTCO': 91.5})
    assert store.find(1)['veto_shadow']['label'] == outcomes.HIT


def test_unresolved_shadow_goes_flat_at_the_horizon(store):
    _vetoed(store)
    later = datetime.now() + timedelta(days=cfg.VETO_SHADOW_DAYS + 1)
    outcomes.track_shadows(store, {'TESTCO': 96.0}, now=later)
    assert store.find(1)['veto_shadow']['label'] == outcomes.FLAT


def test_a_resolved_shadow_is_not_relabelled(store):
    _vetoed(store)
    outcomes.track_shadows(store, {'TESTCO': 100.5})
    outcomes.track_shadows(store, {'TESTCO': 80.0})     # would be a MISS
    assert store.find(1)['veto_shadow']['label'] == outcomes.HIT


def test_missing_ltp_leaves_the_shadow_open(store):
    _vetoed(store)
    outcomes.track_shadows(store, {})
    assert store.find(1)['veto_shadow']['status'] == 'open'


# ── the join ─────────────────────────────────────────────────────────────
def test_veto_outcome_carries_a_label_and_deliberately_no_pnl(store, decisions):
    d = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    _vetoed(store, decision_id=d['id'])
    outcomes.track_shadows(store, {'TESTCO': 91.0})
    assert outcomes.join(store, decisions) == 1
    o = decisions.find(d['id'])['outcome']
    assert o['basis'] == 'spot_barrier' and o['label'] == outcomes.MISS
    # The vetoed structure was never priced. A proxy in the pnl field would let
    # score() report a rupee figure that is pure invention.
    assert 'pnl' not in o


def test_unresolved_veto_is_not_joined_yet(store, decisions):
    d = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    _vetoed(store, decision_id=d['id'])
    assert outcomes.join(store, decisions) == 0
    assert decisions.find(d['id'])['outcome'] is None


def test_allowed_outcome_is_realised_pnl(store, decisions):
    store.add_signal(dict(SIGNAL))
    store.mark_entered(1, dict(ENTRY))
    d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    store.mark_exited(1, 101.0, 16.0, 'tp')
    assert outcomes.join(store, decisions) == 1
    o = decisions.find(d['id'])['outcome']
    assert o['basis'] == 'realized' and o['label'] == outcomes.HIT
    assert o['pnl'] == 600.0                     # (16 - 10) * 100


def test_open_position_is_not_joined(store, decisions):
    store.add_signal(dict(SIGNAL))
    store.mark_entered(1, dict(ENTRY))
    d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    assert outcomes.join(store, decisions) == 0
    assert decisions.find(d['id'])['outcome'] is None


def test_join_is_idempotent(store, decisions):
    store.add_signal(dict(SIGNAL))
    store.mark_entered(1, dict(ENTRY))
    decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                     stock='TESTCO', direction='CE')
    store.mark_exited(1, 101.0, 16.0, 'tp')
    assert outcomes.join(store, decisions) == 1
    assert outcomes.join(store, decisions) == 0


def test_time_exit_is_flat_not_a_loss(store, decisions):
    """Expiring unresolved says the signal was slow, not wrong. Scoring it as a
    loss would make every veto of a slow signal look brilliant."""
    store.add_signal(dict(SIGNAL))
    store.mark_entered(1, dict(ENTRY))
    d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    store.mark_exited(1, 97.0, 9.0, 'time')
    outcomes.join(store, decisions)
    assert decisions.find(d['id'])['outcome']['label'] == outcomes.FLAT


# ── scoring ──────────────────────────────────────────────────────────────
def test_signal_quality_scores_both_arms_on_one_basis(store, decisions):
    """The money score can only see allows. This is the comparable view."""
    # A veto that was RIGHT: the blocked signal went the wrong way.
    dv = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                          stock='TESTCO', direction='CE')
    _vetoed(store, decision_id=dv['id'])
    outcomes.track_shadows(store, {'TESTCO': 91.0})
    # An allow that was RIGHT: it hit target.
    store.add_signal(dict(SIGNAL, stock='OTHERCO'))
    store.mark_entered(2, dict(ENTRY))
    decisions.record(kind='entry', verdict='allow', trade_ids=[2],
                     stock='OTHERCO', direction='CE')
    store.mark_exited(2, 101.0, 16.0, 'tp')

    outcomes.join(store, decisions)
    q = decisions.score('entry')['signal_quality']
    assert q['veto']['precision'] == 1.0 and q['allow']['precision'] == 1.0
    assert q['labelled'] == 2


def test_flat_rows_are_excluded_from_precision_but_reported(store, decisions):
    d = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    _vetoed(store, decision_id=d['id'])
    later = datetime.now() + timedelta(days=cfg.VETO_SHADOW_DAYS + 1)
    outcomes.track_shadows(store, {'TESTCO': 96.0}, now=later)
    outcomes.join(store, decisions)
    q = decisions.score('entry')['signal_quality']
    assert q['veto']['flat'] == 1 and q['veto']['decisive'] == 0
    assert q['veto']['precision'] is None       # not 0.0 — that would be a lie


def test_money_score_ignores_veto_rows_entirely(store, decisions):
    """A spot label must never be counted as cash."""
    d = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    _vetoed(store, decision_id=d['id'])
    outcomes.track_shadows(store, {'TESTCO': 91.0})
    outcomes.join(store, decisions)
    s = decisions.score('entry')
    assert s['veto'] == {'n': 0} and s['scored'] == 0
