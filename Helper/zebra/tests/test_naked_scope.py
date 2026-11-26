"""Live naked build, P0: a `naked_long` record is its OWN book.

Design: `docs/NAKED_LIVE_DESIGN.md` (Q4, Q5, Q9) and the 2026-11-26 code
re-map (findings N1-N4). A live naked record carries the cohort stamp, because
the exit adapter selects by it. Without structure scoping, that one record
would have:
  - pooled real-money results into the paper BCS cohort's numbers (N3),
  - been shadowed into the 100-trade test as a second copy of its own parent (N1),
  - blocked new PAPER signals on its stock, changing the test population (N2),
  - been valued by zebra as a two-leg position it is not (N4).

Every test below plants the record's SHAPE -- a cohort-stamped, entered,
`paper: False`, `structure: 'naked_long'` record -- next to a real paper BCS
parent, and asserts the reader sees only what it should.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_scope.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import capital                  # noqa: E402
from zebra import config as cfg            # noqa: E402
from zebra import reentry                  # noqa: E402
from zebra import structure_shadow as ss   # noqa: E402
from zebra import trade_store as tsm       # noqa: E402
from zebra.trade_store import ZebraStore   # noqa: E402

LOT = 100
BCS = {'long_symbol': 'TESTCO26DEC1000CE', 'short_symbol': 'TESTCO26DEC1040CE',
       'long_strike': 1000.0, 'short_strike': 1040.0, 'debit': 20.0,
       'lot_size': LOT, 'expiry': '2026-12-29', 'entry_spot': 1000.0,
       'short_extrinsic': 5.0, 'long_ask_qty': 5000, 'short_bid_qty': 5000,
       'width': 40.0, 'debit_to_width_pct': 50.0, 'tp_spot': 1040.0}
NOW = datetime(2026, 12, 1, 10, 30, 0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    sig = s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly',
                        'direction': 'CE', 'st_value': 1040.0,
                        'st_direction': 'UP', 'signal_price': 1000.0,
                        'signal_gap_pct': 4.0})
    s.mark_entered_bcs(sig['id'], dict(BCS))
    return s


def _parent(store):
    return next(t for t in store.load_trades() if t.get('structure') == 'bcs')


def _naked(store, **kw):
    rec = tsm.build_naked_record(
        _parent(store), long_symbol='TESTCO26DEC1000CE', long_strike=1000.0,
        lot_size=LOT, fill_price=21.5, now=NOW,
        quote={'ask': 21.5, 'bid': 21.0, 'ask_qty': 900, 'oi': 12000}, **kw)
    return store.add_naked_live(rec)


# ── the record itself ─────────────────────────────────────────────────────

def test_a_valid_record_has_no_problems_and_no_short_keys(store):
    rec = tsm.build_naked_record(_parent(store), long_symbol='TESTCO26DEC1000CE',
                                 long_strike=1000.0, lot_size=LOT,
                                 fill_price=21.5, now=NOW)
    assert tsm.validate_naked_record(rec) == []
    assert not [k for k in rec if k.startswith('short_')]
    assert 'width' not in rec
    assert rec['paper'] is False and rec['quantity'] == LOT
    assert rec['stop_value'] == pytest.approx(21.5 * tsm.NAKED_STOP_FRAC)


@pytest.mark.parametrize('mutate, needle', [
    (lambda r: r.update(short_symbol=None), 'forbidden key short_symbol'),
    (lambda r: r.update(width=0), 'forbidden key width'),
    (lambda r: r.update(paper=True), 'paper must be False'),
    (lambda r: r.update(quantity=2 * LOT), 'exactly 1 lot'),
    (lambda r: r.update(lots=2, quantity=2 * LOT), 'exactly 1 lot'),
    (lambda r: r.update(entry_long_price=0), 'entry_long_price must be'),
    (lambda r: r.update(entry_long_price=float('nan')), 'entry_long_price must be'),
    (lambda r: r.update(long_symbol='TESTCO26DEC1000PE'), 'does not match direction'),
    (lambda r: r.update(parent_id=True), 'parent_id must be an int'),
    (lambda r: r.update(expiry='2026-11-01'), 'before entry'),
    (lambda r: r.pop('cohort'), 'missing cohort'),
    (lambda r: r.update(rehearsal='yes'), 'rehearsal must be a bool'),
])
def test_validation_refuses_each_bad_shape(store, mutate, needle):
    rec = tsm.build_naked_record(_parent(store), long_symbol='TESTCO26DEC1000CE',
                                 long_strike=1000.0, lot_size=LOT,
                                 fill_price=21.5, now=NOW)
    mutate(rec)
    probs = tsm.validate_naked_record(rec)
    assert any(needle in p for p in probs), probs


def test_add_writes_once_and_refuses_a_second_twin(store):
    t = _naked(store)
    assert t['id'] != _parent(store)['id'] and t['version'] == 1
    with pytest.raises(ValueError, match='already has live naked twin'):
        _naked(store)
    assert sum(1 for x in store.load_trades() if tsm.is_naked(x)) == 1


def test_add_refuses_a_parent_that_is_not_a_cohort_bcs(store):
    rec = tsm.build_naked_record(_parent(store), long_symbol='TESTCO26DEC1000CE',
                                 long_strike=1000.0, lot_size=LOT,
                                 fill_price=21.5, now=NOW)
    sig = store.add_signal({'stock': 'OTHERCO', 'timeframe': 'weekly',
                            'direction': 'CE', 'st_value': 50.0,
                            'st_direction': 'UP', 'signal_price': 48.0,
                            'signal_gap_pct': 4.0})
    rec['parent_id'] = sig['id']             # a watching signal, unstamped
    with pytest.raises(ValueError, match='not a cohort BCS position'):
        store.add_naked_live(rec)


def test_add_refuses_a_stock_mismatch(store):
    rec = tsm.build_naked_record(_parent(store), long_symbol='TESTCO26DEC1000CE',
                                 long_strike=1000.0, lot_size=LOT,
                                 fill_price=21.5, now=NOW)
    rec['stock'] = 'OTHERCO'
    with pytest.raises(ValueError, match='record is OTHERCO'):
        store.add_naked_live(rec)


def test_stop_fraction_is_the_tested_arms():
    assert tsm.NAKED_STOP_FRAC == ss.ARMS['naked_long']['stop_frac']


# ── scoping ───────────────────────────────────────────────────────────────

def test_cohort_readers_default_to_the_bcs_book(store):
    naked = _naked(store)
    book = store.load_trades()
    for fn in (tsm.scored, tsm.decided, tsm.in_flight):
        ids = {t['id'] for t in fn(book)}
        assert naked['id'] not in ids, fn.__name__
        assert _parent(store)['id'] in ids, fn.__name__
    assert [t['id'] for t in tsm.scored(book, structure=tsm.STRUCTURE_NAKED)] \
        == [naked['id']]
    assert len(tsm.scored(book, structure=tsm.ANY_STRUCTURE)) == 2
    current, legacy = tsm.cohort_split(book)
    assert naked['id'] not in {t['id'] for t in current + legacy}


def test_unknown_structure_raises_not_guesses():
    with pytest.raises(ValueError, match='unknown structure'):
        tsm.structure_of({'id': 1, 'structure': 'iron_condor'})
    assert tsm.structure_of({'id': 1}) == tsm.STRUCTURE_BACK_RATIO
    assert tsm.structure_of({'id': 1, 'cohort': '2026-08-14'}) == tsm.STRUCTURE_BCS


def test_get_entered_hides_naked_unless_asked(store):
    naked = _naked(store)
    assert naked['id'] not in {t['id'] for t in store.get_entered()}
    assert naked['id'] in {t['id'] for t in store.get_entered(include_naked=True)}


def test_an_open_naked_twin_does_not_block_a_paper_signal(store):
    _naked(store)
    # The parent itself still dedups; close it so only the twin remains open.
    p = _parent(store)
    with store._mutate():
        store._must_find(p['id'])['status'] = 'exited'
    again = store.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly',
                              'direction': 'CE', 'st_value': 1040.0,
                              'st_direction': 'UP', 'signal_price': 1000.0,
                              'signal_gap_pct': 4.0})
    assert again['status'] == 'watching'


def test_capital_does_not_count_a_naked_twin(store):
    book = store.load_trades()
    before = capital.deployed(book)
    _naked(store)
    assert capital.deployed(store.load_trades()) == before


def test_reentry_does_not_report_the_twin_as_a_prior_trade(store):
    naked = _naked(store)
    book = store.load_trades()
    rows = reentry.recent_on_stock(book, 'TESTCO', today=NOW.date())
    assert naked['id'] not in {r.get('id') for r in rows}


def test_no_shadow_is_opened_for_a_naked_record(store):
    naked = _naked(store)
    state = {'shadows': {}}
    entered = [naked]
    assert ss.open_shadows(state, entered, '2026-12-01 10:35:00', kite=None) == 0
    assert str(naked['id']) not in state['shadows']


def test_arming_population_still_sees_the_live_record(store):
    from zebra import monitor
    _naked(store)
    pop = monitor._cohort_population(store.load_trades())
    from common import arming
    assert arming.LIVE_RECORD in pop


def test_status_dashboard_prints_a_naked_record(store, monkeypatch, capsys):
    from zebra import __main__ as cli
    _naked(store)
    monkeypatch.setattr('zebra.trade_store.get_store', lambda *a, **k: store)
    monkeypatch.setattr(cli, 'get_store', lambda *a, **k: store, raising=False)
    try:
        cli.cmd_status(type('A', (), {})())
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert '[NAKED LIVE' in out


# ── config ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('val, want', [
    ('off', 'off'), ('rehearsal', 'rehearsal'), ('live', 'live'),
    ('Live', 'off'), ('live ', 'off'), (True, 'off'), (1, 'off'), (None, 'off'),
])
def test_naked_live_is_strict(monkeypatch, val, want):
    monkeypatch.setitem(cfg._runtime, 'naked_live', val)
    assert cfg._naked_live_mode() == want


@pytest.mark.parametrize('val, want', [
    (0, 0.0), (75000, 75000.0), (-1, 0.0), (float('nan'), 0.0), ('75000', 0.0),
])
def test_loss_budget_allows_zero_as_off(monkeypatch, val, want):
    monkeypatch.setitem(cfg._runtime, 'naked_loss_budget', val)
    assert cfg._non_negative_num('naked_loss_budget') == want


def test_tracked_defaults_ship_disarmed():
    assert cfg._DEFAULTS['naked_live'] == 'off'
    assert cfg._DEFAULTS['naked_override'] is False
    assert cfg._DEFAULTS['naked_loss_budget'] == 0
