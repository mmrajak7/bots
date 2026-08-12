"""Post-mortems and precedents — the feedback loop from exits back to entries.

The centrepiece here is the self-training trap. A VETOED signal never traded,
so its outcome is a spot proxy, not P&L. If a veto-leaning precedent could be
built from proxies alone, the layer would confirm its own vetoes, veto more,
and quietly shrink the book — with the evidence base looking healthier every
round. That is the one failure this file exists to make impossible.

Run:  cd Helper && python -m pytest zebra/tests/test_postmortem.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg              # noqa: E402
from zebra import postmortem as pm           # noqa: E402
from zebra.trade_store import ZebraStore      # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
ENTRY = {'long_strike': 90.0, 'short_strike': 100.0,
         'long_symbol': 'A', 'short_symbol': 'B', 'debit': 10.0,
         'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    return s


def _closed(store, stock, pnl, tid=None):
    """An exited position with real P&L."""
    t = store.add_signal(dict(SIGNAL, stock=stock))
    store.mark_entered(t['id'], dict(ENTRY))
    store.mark_exited(t['id'], 100.0, 10.0 + pnl / 100.0, 'paper:tp')
    return t['id']


def _vetoed(store, stock, label='hit'):
    """A vetoed signal whose spot shadow has resolved — a PROXY outcome."""
    t = store.add_signal(dict(SIGNAL, stock=stock))
    with store._mutate():
        store.find(t['id'])['veto_shadow'] = {
            'status': 'resolved', 'label': label,
            'resolved_at': datetime.now().isoformat()}
    return t['id']


# ── writing ──────────────────────────────────────────────────────────────
def test_both_arms_are_post_mortemed(store):
    """Covering only the trades that HAPPENED would build an evidence base made
    entirely of decisions to trade, leaving the vetoes — the layer's most
    consequential calls — with no lessons attached."""
    a = _closed(store, 'AAA', 500)
    b = _vetoed(store, 'BBB')
    ids = {r['trade_id'] for r in pm.pending(store)}
    assert ids == {a, b}
    bases = {r['trade_id']: r['basis'] for r in pm.pending(store)}
    assert bases[a] == pm.BASIS_REALISED and bases[b] == pm.BASIS_PROXY


def test_an_open_position_is_not_pending(store):
    t = store.add_signal(dict(SIGNAL))
    store.mark_entered(t['id'], dict(ENTRY))
    assert pm.pending(store) == []


def test_a_recorded_post_mortem_leaves_the_queue(store):
    a = _closed(store, 'AAA', 500)
    pm.record(store, a, ['worked_as_designed'], 'clean')
    assert pm.pending(store) == []


def test_tags_outside_the_taxonomy_are_rejected(store):
    """Free text reads better in one post-mortem and is worthless in aggregate:
    four spellings of the same idea become four precedents with support 1."""
    a = _closed(store, 'AAA', -500)
    with pytest.raises(ValueError, match='unknown tag'):
        pm.record(store, a, ['bad_book'], 'the book was awful')
    assert store.find(a).get('postmortem') is None


def test_a_post_mortem_needs_a_tag_and_a_lesson(store):
    a = _closed(store, 'AAA', -500)
    with pytest.raises(ValueError):
        pm.record(store, a, [], 'something')
    with pytest.raises(ValueError):
        pm.record(store, a, ['illiquid_book'], '   ')


def test_a_post_mortem_is_written_once(store):
    a = _closed(store, 'AAA', -500)
    pm.record(store, a, ['illiquid_book'], 'first')
    with pytest.raises(ValueError, match='already has'):
        pm.record(store, a, ['gave_back'], 'second')
    assert store.find(a)['postmortem']['lesson'] == 'first'


def test_the_basis_is_stamped_from_the_trade_not_the_caller(store):
    """An agent must not be able to label a proxy outcome as realised — that is
    the trap guard's only input."""
    b = _vetoed(store, 'BBB')
    assert pm.record(store, b, ['slow_no_move'], 'x')['basis'] == pm.BASIS_PROXY


# ── aggregation ──────────────────────────────────────────────────────────
def test_precedents_count_support_and_realised_support_separately(store):
    for i, pnl in enumerate((-500, -400)):
        pm.record(store, _closed(store, f'R{i}', pnl), ['debit_too_rich'], 'x')
    pm.record(store, _vetoed(store, 'V0'), ['debit_too_rich'], 'x')
    r = next(p for p in pm.precedents(store) if p['tag'] == 'debit_too_rich')
    assert r['support'] == 3 and r['realised_support'] == 2


def test_a_vetoed_signal_that_would_have_won_counts_AGAINST_the_veto(store):
    """A veto whose signal went on to hit its target cost money. The tag on it
    is evidence against vetoing that pattern again, not for it."""
    pm.record(store, _vetoed(store, 'V0', label='hit'), ['event_inside_window'],
              'x')
    r = next(p for p in pm.precedents(store)
             if p['tag'] == 'event_inside_window')
    assert r['worked'] == 1 and r['failed'] == 0


# ── THE TRAP ─────────────────────────────────────────────────────────────
def test_a_veto_leaning_precedent_built_only_from_vetoes_is_withheld(store):
    """The self-training trap, in one test. Veto -> proxy outcome -> precedent
    -> more vetoes, with the evidence base looking stronger every round while
    the book shrinks."""
    for i in range(4):
        pm.record(store, _vetoed(store, f'V{i}', label='miss'),
                  ['event_inside_window'], 'x')
    view = pm.for_signal(store, 'TESTCO')
    assert [r['tag'] for r in view['shown']] == []
    assert view['withheld'][0]['tag'] == 'event_inside_window'
    assert 'realised' in view['withheld'][0]['withheld_because']


def test_the_same_precedent_is_shown_once_real_trades_back_it(store):
    for i in range(3):
        pm.record(store, _vetoed(store, f'V{i}', label='miss'),
                  ['event_inside_window'], 'x')
    assert pm.for_signal(store, 'TESTCO')['shown'] == []
    for i in range(2):
        pm.record(store, _closed(store, f'R{i}', -500),
                  ['event_inside_window'], 'x')
    assert [r['tag'] for r in pm.for_signal(store, 'TESTCO')['shown']] \
        == ['event_inside_window']


def test_an_allow_leaning_precedent_needs_no_realised_support(store):
    """It cannot shrink the book, so the guard does not apply. Applying it
    anyway would suppress the evidence that a veto was WRONG — which is the
    same bias in the other direction."""
    for i in range(3):
        pm.record(store, _vetoed(store, f'V{i}', label='hit'),
                  ['event_inside_window'], 'x')
    assert [r['tag'] for r in pm.for_signal(store, 'TESTCO')['shown']] \
        == ['event_inside_window']


def test_withheld_precedents_are_reported_not_dropped(store):
    """A guard nobody can see is a guard nobody will trust — and 'we believe
    this pattern is bad but have only ever vetoed it' is itself information."""
    for i in range(3):
        pm.record(store, _vetoed(store, f'V{i}', label='miss'),
                  ['illiquid_book'], 'x')
    assert len(pm.for_signal(store, 'TESTCO')['withheld']) == 1


def test_a_single_observation_is_never_a_precedent(store):
    """A WINNING one on purpose: a losing single observation leans veto and is
    already withheld by the realised-support guard, so it cannot tell whether
    the support floor does anything at all."""
    pm.record(store, _closed(store, 'AAA', 500), ['gap_against'], 'x')
    r = next(p for p in pm.precedents(store) if p['tag'] == 'gap_against')
    assert r['leans'] == 'allow' and r['support'] == 1
    assert pm.for_signal(store, 'TESTCO')['shown'] == []


def test_injection_is_capped(store):
    for i, tag in enumerate(list(pm.TAGS)[:7]):
        for j in range(2):
            pm.record(store, _closed(store, f'S{i}{j}', 500), [tag], 'x')
    assert len(pm.for_signal(store, 'TESTCO')['shown']) == pm.INJECT_K


# ── the EOD batch ────────────────────────────────────────────────────────
def test_the_batch_is_not_due_when_nothing_settled(store):
    assert pm.due(store) is False


def test_the_batch_is_due_once_something_settles(store):
    _closed(store, 'AAA', 500)
    assert pm.due(store) is True


def test_the_batch_runs_at_most_once_a_day(store):
    """The cron starts a fresh process every five minutes, so an in-memory
    'already ran' would be forgotten instantly and spawn an agent every cycle."""
    _closed(store, 'AAA', 500)
    pm.mark_run()
    assert pm.due(store) is False
    assert pm.due(store, now=datetime.now() + timedelta(days=1)) is True


def test_the_batch_is_marked_before_it_spawns(store, monkeypatch):
    """A crashed agent must cost one day of missed post-mortems, not an agent
    every five minutes — the shape the review channel already had to be fixed
    for."""
    _closed(store, 'AAA', 500)
    monkeypatch.setattr('zebra.vet._spawn_generic',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    with pytest.raises(RuntimeError):
        pm.spawn_batch(store)
    assert pm.due(store) is False, "a crashed agent left the batch re-spawning"


def test_the_batch_is_wired_into_the_cycle():
    """The recurring failure in this fleet is code written, tested, never
    reached."""
    src = (HELPER / 'zebra' / 'monitor.py').read_text(encoding='utf-8')
    assert 'post-mortem batch' in src and '_run_postmortem_batch' in src
