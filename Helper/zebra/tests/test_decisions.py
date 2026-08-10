"""Decision journal for the Claude vetting layer.

The journal's job is to make the veto layer FALSIFIABLE — every judgement
joined to what actually happened, so "should this ever get live authority?"
is answered with numbers rather than trust. These tests guard the properties
that scoring depends on.

Run:  cd Helper && python -m pytest zebra/tests/test_decisions.py -v
"""
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra.decisions import DecisionStore          # noqa: E402


@pytest.fixture
def store(tmp_path):
    return DecisionStore(path=tmp_path / 'd.json',
                         lock_path=tmp_path / 'd.lock').initialize()


def _rec(store, **kw):
    base = dict(kind='entry', verdict='allow', trade_ids=[1, 2],
                stock='TESTCO', direction='CE')
    base.update(kw)
    return store.record(**base)


# ── basics ───────────────────────────────────────────────────────────────
def test_record_assigns_ids_and_persists(store, tmp_path):
    a = _rec(store)
    b = _rec(store, stock='OTHER')
    assert (a['id'], b['id']) == (1, 2)
    on_disk = json.loads((tmp_path / 'd.json').read_text())
    assert len(on_disk) == 2


def test_one_decision_covers_both_ab_arms(store):
    """Both arms share ONE verdict — that is what keeps the zebra-vs-BCS
    structure comparison unconfounded by the vetting layer."""
    d = _rec(store, trade_ids=[10, 11])
    assert store.for_trade(10) == store.for_trade(11) == [d]


def test_rejects_bad_verdict_and_kind(store):
    with pytest.raises(ValueError, match='verdict'):
        _rec(store, verdict='maybe')
    with pytest.raises(ValueError, match='kind'):
        _rec(store, kind='sideways')


def test_rejects_out_of_range_confidence(store):
    with pytest.raises(ValueError, match='confidence'):
        _rec(store, confidence=1.5)


def test_failed_record_leaves_no_phantom_row(store, tmp_path):
    """A validation error must roll back cleanly — a phantom row would be
    scored later as if it were a real judgement."""
    _rec(store)
    with pytest.raises(ValueError):
        _rec(store, verdict='nonsense')
    assert len(store.all()) == 1
    assert len(json.loads((tmp_path / 'd.json').read_text())) == 1
    assert _rec(store, stock='NEXT')['id'] == 2      # store still usable


def test_mark_acted_and_set_outcome(store):
    d = _rec(store)
    assert store.mark_acted(d['id'])['acted'] is True
    got = store.set_outcome(d['id'], {'pnl': -500.0, 'exit_reason': 'debit_sl'})
    assert got['outcome']['pnl'] == -500.0 and got['outcome_at']


def test_pending_outcome_excludes_unavailable(store):
    """`unavailable` is an outage, not a judgement — it must never sit in the
    queue waiting for a result that will never be meaningful."""
    _rec(store)
    _rec(store, verdict='unavailable', stock='DOWN')
    pending = store.pending_outcome()
    assert [d['signal_ref']['stock'] for d in pending] == ['TESTCO']


# ── scoring: the payoff ──────────────────────────────────────────────────
def test_veto_is_correct_when_the_blocked_trade_would_have_lost(store):
    for i, pnl in enumerate([-100.0, -200.0, +300.0]):
        d = _rec(store, verdict='veto', stock=f'V{i}')
        store.set_outcome(d['id'], {'pnl': pnl})
    s = store.score('entry')['veto']
    assert s['n'] == 3 and s['correct'] == 2          # 2 of 3 blocked losers
    assert s['precision'] == pytest.approx(0.667, abs=0.001)
    assert s['pnl_avoided'] == pytest.approx(0.0)     # +100+200-300


def test_allow_is_correct_when_the_trade_won(store):
    for i, pnl in enumerate([+500.0, -100.0]):
        d = _rec(store, verdict='allow', stock=f'A{i}')
        store.set_outcome(d['id'], {'pnl': pnl})
    s = store.score('entry')['allow']
    assert s['n'] == 2 and s['correct'] == 1
    assert s['pnl_captured'] == pytest.approx(400.0)


def test_scoring_ignores_unscored_and_unavailable(store):
    d = _rec(store, verdict='veto')
    store.set_outcome(d['id'], {'pnl': -50.0})
    _rec(store, verdict='veto', stock='NOOUTCOME')       # no outcome yet
    u = _rec(store, verdict='unavailable', stock='OUTAGE')
    store.set_outcome(u['id'], {'pnl': -900.0})          # must NOT count
    assert store.score('entry')['scored'] == 1


def test_breakeven_counts_as_a_loss_for_veto_precision(store):
    """pnl == 0 is not a win. Treating it as one would overstate allow
    precision on trades that merely burned slippage."""
    d = _rec(store, verdict='allow')
    store.set_outcome(d['id'], {'pnl': 0.0})
    assert store.score('entry')['allow']['correct'] == 0


def test_score_is_empty_not_crashing_with_no_data(store):
    s = store.score('entry')
    assert s['scored'] == 0 and s['veto']['n'] == 0


# ── concurrency: same discipline as the trade store ──────────────────────
WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {helper!r})
    from pathlib import Path
    from zebra.decisions import DecisionStore
    s = DecisionStore(path=Path({p!r}), lock_path=Path({l!r})).initialize()
    for i in range({n}):
        s.record(kind='entry', verdict='allow', trade_ids=[i],
                 stock='{tag}%d' % i, direction='CE')
""")


def test_concurrent_writers_lose_no_decisions(tmp_path):
    """Claude's vetting cron and the zebra cron can both journal. A lost
    decision is a hole in the scoring record exactly where it matters."""
    p, l = tmp_path / 'd.json', tmp_path / 'd.lock'
    p.write_text('[]')
    procs = [subprocess.Popen(
        [sys.executable, '-c', WRITER.format(helper=str(HELPER), p=str(p),
                                             l=str(l), n=8, tag=chr(65 + i))],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(3)]
    for pr in procs:
        _, err = pr.communicate(timeout=180)
        assert pr.returncode == 0, err
    rows = json.loads(p.read_text())
    assert len(rows) == 24, f"LOST DECISIONS: expected 24, got {len(rows)}"
    ids = [r['id'] for r in rows]
    assert len(ids) == len(set(ids)), f"DUPLICATE IDS: {sorted(ids)}"
