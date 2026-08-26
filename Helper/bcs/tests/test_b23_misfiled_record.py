"""B23 — a record in the wrong book has its stops pointing the wrong way.

`bcs/spread_monitor.py` picks SL_SPOT and TP DIRECTION from `_store_type`,
which is stamped by whichever store the record came out of. BCS stops on a
fall and takes profit on a rise; BPS and FH are the other way round. Nothing
checked that a record in the BCS book actually holds calls.

Measured, not imagined. A bear put spread (long 1400 PE / short 1340 PE,
sl_spot 1445, target 1330) filed into the BCS book, at its own entry spot of
1360 — a perfectly healthy, just-opened position:

    SL_SPOT would fire?  True      (spot 1360 <= sl_spot 1445)
    TP would fire?       True      (spot 1360 >= target 1330)

Both, on the FIRST poll, closing at whatever the book offers. The capture flow
is natural language driven through `get_store()`, so one wrong store call is
all it takes.

Two layers, because they catch different moments:
  * the STORES refuse a mismatched record at `add_trade` — cheap, at the source
  * the MONITOR refuses to act on one already open — the stores cannot help
    anything saved before today

Run:  cd Helper && python -m pytest bcs/tests/test_b23_misfiled_record.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                        # noqa: E402
from common.option_symbols import (check_leg_types,          # noqa: E402
                                   option_type, strike)


# ── The symbol is the source of truth ───────────────────────────────────────

@pytest.mark.parametrize('sym,typ,stk', [
    ('TESTCO26SEP1400PE', 'PE', 1400.0),
    ('TESTCO26SEP1360CE', 'CE', 1360.0),
    ('POWERGRID26SEP252.5CE', 'CE', 252.5),
    ('NIFTY26SEP25000PE', 'PE', 25000.0),
    ('TESTCO26SEPFUT', None, None),
    ('', None, None),
    (None, None, None),
])
def test_type_and_strike_come_off_the_symbol(sym, typ, stk):
    assert option_type(sym) == typ
    assert strike(sym) == stk


def test_the_monitor_helpers_are_the_same_functions():
    """Re-exported, not re-implemented. One definition of option arithmetic is
    the whole lesson of B21, where the intrinsic floor existed in only one of
    the two places that needed it and the bear-put book went unguarded."""
    assert sm.option_type_from_symbol is option_type
    assert sm.strike_from_symbol is strike


# ── check_leg_types ─────────────────────────────────────────────────────────

BCS_LEGS = {'long_symbol': 'CE', 'short_symbol': 'CE'}


def test_a_consistent_record_reports_nothing():
    assert check_leg_types(
        {'long_symbol': 'A26SEP100CE', 'short_symbol': 'A26SEP110CE'},
        BCS_LEGS) == []


def test_a_put_in_the_call_book_is_reported_per_leg():
    problems = check_leg_types(
        {'long_symbol': 'A26SEP110PE', 'short_symbol': 'A26SEP100PE'},
        BCS_LEGS)
    assert len(problems) == 2
    assert all('is a PE' in p and 'holds CE' in p for p in problems), problems


def test_one_wrong_leg_is_enough():
    """A call spread with one put leg is not a vertical at all. Reporting only
    when BOTH legs are wrong would pass the stranger shape."""
    assert check_leg_types(
        {'long_symbol': 'A26SEP100CE', 'short_symbol': 'A26SEP110PE'},
        BCS_LEGS)


def test_a_missing_field_is_not_reported_here():
    """Required-field validation is the store's job, and reporting the same
    absence twice turns one error into two."""
    assert check_leg_types({'long_symbol': 'A26SEP100CE'}, BCS_LEGS) == []


def test_a_non_option_symbol_is_reported_rather_than_ignored():
    """`option_type` returning None means 'this is not an option'. Skipping it
    would let a futures symbol into an options book unremarked."""
    assert check_leg_types({'long_symbol': 'A26SEPFUT'}, BCS_LEGS)


# ── Layer 1: the stores refuse it at add_trade ──────────────────────────────

WRONG_LEGS = {
    'bcs': dict(long_symbol='TESTCO26SEP1400PE',
                short_symbol='TESTCO26SEP1340PE'),
    'bear_put': dict(long_symbol='TESTCO26SEP1400CE',
                     short_symbol='TESTCO26SEP1340CE'),
    'fallen_hero': dict(short_call_symbol='TESTCO26SEP3000PE'),
}


def test_the_right_legs_are_accepted(book):
    """Negative control, and it runs for all three books: the payloads in
    conftest are correct, so `add_trade` must not reject them."""
    store = book.make()
    assert store.add_trade(book.payload)['id'] == 3


def test_the_wrong_legs_are_refused(book):
    """Parametrised over every money book — B10 and B11 each shipped an
    untested `fallen_hero` twin because the BCS half was written first."""
    store = book.make()
    payload = dict(book.payload)
    payload.update(WRONG_LEGS[book.stem.replace('_trades', '')])
    with pytest.raises(ValueError) as ei:
        store.add_trade(payload)
    assert 'Leg types do not match' in str(ei.value), str(ei.value)


def test_a_refused_record_is_not_written_to_disk(book):
    """The check must run before anything is persisted, or the store is left
    holding exactly the record the monitor then refuses to act on."""
    store = book.make()
    before = book.read()
    payload = dict(book.payload)
    payload.update(WRONG_LEGS[book.stem.replace('_trades', '')])
    with pytest.raises(ValueError):
        store.add_trade(payload)
    assert book.read() == before


def test_every_book_declares_its_leg_types(book):
    """A fourth book added without a LEG_TYPES row would be silently
    unchecked — the failure mode this whole file exists to prevent."""
    assert getattr(book.mod, 'LEG_TYPES', None), (
        f'{book.mod.__name__} has no LEG_TYPES')
    assert set(book.mod.LEG_TYPES.values()) <= {'CE', 'PE'}


# ── Layer 2: the monitor refuses to act on one already open ─────────────────

def _tagged(store_type, **over):
    t = {'id': 1, 'stock': 'TESTCO', '_strategy': store_type.upper(),
         '_store_type': store_type, 'sl_spot': 1445.0, 'spot_symbol': 'NSE:X',
         'quantity': 700, 'target_spot': 1330.0, 'sl_spread': 6.78,
         'net_debit': 13.55, 'long_symbol': 'TESTCO26SEP1360CE',
         'short_symbol': 'TESTCO26SEP1410CE'}
    t.update(over)
    t['_misfiled'] = check_leg_types(t, sm.LEG_TYPES_BY_STORE[store_type])
    return t


def test_a_healthy_record_is_monitored():
    """Negative control for everything below. If this ever fails, the check
    has started quarantining live positions — which removes their stops, the
    failure that has actually cost money."""
    assert sm._malformed_reason(_tagged('bcs')) is None


def test_a_put_spread_in_the_bcs_book_is_not_monitored():
    reason = sm._malformed_reason(
        _tagged('bcs', long_symbol='TESTCO26SEP1400PE',
                short_symbol='TESTCO26SEP1340PE'))
    assert reason and 'wrong book' in reason
    assert 'WRONG DIRECTION' in reason, (
        'the alert does not say what is actually at stake')


def test_a_call_spread_in_the_bps_book_is_not_monitored():
    reason = sm._malformed_reason(
        _tagged('bps', long_symbol='TESTCO26SEP1400CE',
                short_symbol='TESTCO26SEP1340CE'))
    assert reason and 'wrong book' in reason


def test_a_correct_put_spread_in_the_bps_book_is_monitored():
    """Negative control for the test above: same book, right instrument."""
    assert sm._malformed_reason(
        _tagged('bps', long_symbol='TESTCO26SEP1400PE',
                short_symbol='TESTCO26SEP1340PE')) is None


def test_a_missing_field_still_reports_as_missing_not_as_misfiled():
    """Order matters: a record with no `long_symbol` has nothing to check the
    type of, and reporting it as 'wrong book' would send the owner to fix the
    wrong thing."""
    t = _tagged('bcs', long_symbol=None)
    reason = sm._malformed_reason(t)
    assert reason and reason.startswith('missing'), reason


def test_the_misfile_verdict_is_computed_at_load_not_at_check_time():
    """`_malformed_reason` reads `_misfiled`, which `_load_all_trades` stamps.
    A record that never went through the loader has nothing stamped, and the
    check must treat that as 'no problem found' rather than crashing the poll
    loop of the file that places real orders."""
    bare = {k: v for k, v in _tagged('bcs').items() if k != '_misfiled'}
    assert sm._malformed_reason(bare) is None
    assert sm.trade_is_misfiled(bare) == []


def test_every_store_tag_has_leg_types():
    """`_load_all_trades` indexes LEG_TYPES_BY_STORE with the tag it just
    stamped. A book added without a row raises KeyError at load — loud — but
    only if the two lists stay in step, which is what this asserts."""
    import re
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    body = src[src.index('def _load_all_trades'):src.index('def trade_is_misfiled')]
    tags = set(re.findall(r"'(bcs|bps|fh)'\)", body))
    assert tags == set(sm.LEG_TYPES_BY_STORE), (
        f'stores tagged {tags} but leg types declared for '
        f'{set(sm.LEG_TYPES_BY_STORE)}')


def test_the_monitor_does_not_silently_reinterpret_the_record():
    """It could flip the comparison from the symbols and keep trading. It must
    not: sl_spot, target and sl_spread were all written for the structure
    whoever saved it believed they had, so a "corrected" direction would run
    real money against numbers that may mean nothing. Withhold the ORDER, keep
    the WARNING — the same division as the kill switch.
    """
    import inspect
    src = inspect.getsource(sm.trade_is_misfiled)
    assert 'NOT auto-corrected' in src
    misfiled = _tagged('bcs', long_symbol='TESTCO26SEP1400PE',
                       short_symbol='TESTCO26SEP1340PE')
    assert misfiled['_strategy'] == 'BCS', (
        'the strategy tag was rewritten from the symbols')


# ── The loader is the only thing that stamps it in production ───────────────

def _open(**over):
    """A complete open record.

    Complete on purpose: `_malformed_reason` reports missing fields BEFORE it
    reports a misfile, so a skeletal fixture would come back "missing
    sl_spot, ..." and the loader test would pass or fail for the wrong reason.
    """
    t = {'id': 1, 'stock': 'TESTCO', 'status': 'open', 'quantity': 700,
         'sl_spot': 1319.0, 'spot_symbol': 'NSE:TESTCO', 'target_spot': 1435.0,
         'sl_spread': 6.78, 'net_debit': 13.55,
         'long_symbol': 'TESTCO26SEP1360CE',
         'short_symbol': 'TESTCO26SEP1410CE'}
    t.update(over)
    return t


def _load(bcs=(), bps=(), fh=()):
    from bcs.tests.fakes import MemoryStore
    return sm._load_all_trades(MemoryStore(trades=list(bcs)),
                               MemoryStore(trades=list(fh)),
                               MemoryStore(trades=list(bps)))


def test_the_loader_stamps_a_clean_verdict_on_a_good_record():
    """A mutation run replacing the loader's `check_leg_types(...)` call with
    a bare `[]` SURVIVED this file, because every other test builds its own
    `_misfiled` by hand. The loader is the only place that stamps it in
    production, so it needs its own test — otherwise the fix is verified
    everywhere except where it runs.
    """
    loaded = _load(bcs=[_open()])
    assert loaded[0]['_misfiled'] == []
    assert sm._malformed_reason(loaded[0]) is None


def test_the_loader_catches_a_put_spread_in_the_bcs_book():
    loaded = _load(bcs=[_open(long_symbol='TESTCO26SEP1400PE',
                              short_symbol='TESTCO26SEP1340PE')])
    assert loaded[0]['_misfiled'], 'the loader stamped no verdict'
    assert 'wrong book' in sm._malformed_reason(loaded[0])


def test_the_loader_checks_every_book_not_just_the_first():
    """The three books were three near-identical blocks before this change,
    which is how a check gets added to one of them and not the others."""
    loaded = _load(
        bcs=[_open(id=1, long_symbol='TESTCO26SEP1400PE',
                   short_symbol='TESTCO26SEP1340PE')],
        bps=[_open(id=2)],                     # calls in the put book
        fh=[_open(id=3, long_put_symbol='TESTCO26SEP2550CE',
                  short_put_symbol='TESTCO26SEP2600PE',
                  short_call_symbol='TESTCO26SEP3000CE')])
    by_id = {t['id']: t for t in loaded}
    assert len(by_id) == 3
    for tid in (1, 2, 3):
        assert by_id[tid]['_misfiled'], f'record {tid} passed unchecked'


def test_the_loader_leaves_correct_records_in_all_three_books_alone():
    """Negative control for the test above. A check that quarantined
    everything would satisfy it and remove every stop in the system."""
    loaded = _load(
        bcs=[_open(id=1)],
        bps=[_open(id=2, long_symbol='TESTCO26SEP1400PE',
                   short_symbol='TESTCO26SEP1340PE')],
        fh=[_open(id=3, long_put_symbol='TESTCO26SEP2550PE',
                  short_put_symbol='TESTCO26SEP2600PE',
                  short_call_symbol='TESTCO26SEP3000CE')])
    for t in loaded:
        assert t['_misfiled'] == [], (t['id'], t['_misfiled'])


def test_the_loader_does_not_mutate_the_stores_records(book):
    """`_load_all_trades` returns shallow copies precisely so its tags never
    reach the persisted dicts. `_misfiled` must respect that.

    Driven through a REAL store, not the fake. The real `get_open_trades`
    returns live references into `self._trades`; the fake used to return
    copies, which made it safer than production and blind to this — a mutation
    replacing `tagged = dict(t)` with `tagged = t` survived. The fake now
    aliases the same way, and this test uses the real thing regardless, since
    the tags would otherwise be persisted to the book on the next write.
    """
    store = book.make()
    tagged = sm._load_all_trades(store, store.__class__(
        config={'google_drive': {'enabled': False}}), None)
    assert tagged, 'the fixture book has no open trades to check'
    for t in store.get_open_trades():
        assert '_misfiled' not in t and '_store_type' not in t and             '_strategy' not in t, f'loader tags leaked into the store: {t}'


def test_the_fake_store_aliases_like_the_real_one():
    """Pinned, because the divergence is invisible until it hides a bug.

    A fake that hands out copies where production hands out references cannot
    exercise any guard against aliasing — and `_load_all_trades` exists partly
    to guard against exactly that.
    """
    from bcs.tests.fakes import MemoryStore
    store = MemoryStore(trades=[_open()])
    # The CONSTRUCTOR copying is right — a fixture must not be mutated by the
    # store it was handed to. What matters is that `get_open_trades` returns
    # the store's own dicts, the way `bcs/trade_store.py:363` does, so a caller
    # that writes into the result is writing into the book.
    assert store.get_open_trades()[0] is store.trades[0], (
        'MemoryStore copies where bcs.trade_store.get_open_trades aliases')
    store.get_open_trades()[0]['scribble'] = 1
    assert store.trades[0].get('scribble') == 1
