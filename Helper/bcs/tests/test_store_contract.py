"""The fake must never be safer than the strictest real store.

`feedback_fake_must_not_be_safer_than_production`, sixth instance. The five
before it were argued one at a time and each fix covered exactly the method
that had already burned somebody. This file is the general fix: it PROBES the
real stores for what they accept, and fails if `fakes.MemoryStore` accepts
anything they refuse.

The instance that prompted it:

    def update_trade_exit(self, trade_id, exit_data):
        t = self._find(trade_id)
        t.update(exit_data)
        t['status'] = 'closed'          # <- no status check, ever

`ZebraStore.mark_exited` refuses anything that is not 'entered' or 'closing'.
Because `bcs/tests/replay.py` substitutes `MemoryStore` for the cohort book,
every replay of a live close booked cleanly while production raised on the
identical call — which is how a 100%-reproducible bug on the money path
reached the Pi under a green suite.

The vocabularies differ, so the table is expressed in ROLES
-----------------------------------------------------------
`bcs`/`bear_put`/`fallen_hero` say 'open' and 'closed'; `zebra` says 'entered'
and 'exited'. Comparing raw status strings would compare the wrong things. Each
store declares its own name for each role and the contract is stated once.

A known, deliberate divergence
------------------------------
The three BCS-family stores have NO status check on `update_trade_exit` at all
— they will book an exit onto a closed or a frozen record. That is laxer than
zebra and is asserted below rather than glossed over, so the day it changes,
this file says so. It is not fixed here: those stores are outside this change's
file ownership. See `test_the_bcs_family_is_laxer_than_zebra_on_booking`.
"""
from __future__ import annotations

import importlib
import json

import pytest

from bcs.tests.fakes import MemoryStore
from common import store_contract
from bcs.zebra_adapter import ZebraStoreAdapter
from zebra import config as zcfg
from zebra.trade_store import ZebraStore

#: THE CONTRACT AND THE VOCABULARIES ARE IMPORTED, not declared here.
#:
#: They were declared here until 2026-08-30, and this file's own docstring
#: explained why that was a problem without naming it as one: the BCS family
#: diverged from the table and the divergence was recorded in a test rather
#: than fixed, because the table was not something production could consult.
#: `common/store_contract.py` is that table now, every store asks it, and this
#: file went back to being a test of an implementation against its spec.
OPEN, CLOSING = store_contract.OPEN, store_contract.CLOSING
FROZEN, TERMINAL = store_contract.FROZEN, store_contract.TERMINAL
ROLES = store_contract.ROLES
CONTRACT = store_contract.CONTRACT
BCS_FAMILY_STATUSES = store_contract.BCS_FAMILY_STATUSES
ZEBRA_STATUSES = store_contract.ZEBRA_STATUSES

#: (module, class, file-stem) per real book. The vocabularies that used to sit
#: here are imported above.
REAL_BOOKS = [
    ('bcs', 'bcs.trade_store', 'TradeStore', 'bcs_trades'),
    ('bear_put', 'bear_put.trade_store', 'BearPutStore', 'bear_put_trades'),
    ('fallen_hero', 'fallen_hero.trade_store', 'FallenHeroStore',
     'fallen_hero_trades'),
]

_EXIT_DATA = {'exit_reason': 'TP', 'exit_spot': 1401.0,
              'exit_spread': 40.00, 'short_fill': 10.20, 'long_fill': 50.20}


def _record(status):
    """One record carrying every field any of the four stores needs to book."""
    return {
        'id': 1, 'version': 1, 'status': status, 'stock': 'TESTCO',
        'quantity': 700, 'lot_size': 700, 'lots': 1,
        # bcs/bps/fh vocabulary
        'long_symbol': 'TESTCO26SEP1340CE', 'short_symbol': 'TESTCO26SEP1390CE',
        'spot_symbol': 'NSE:TESTCO', 'exchange': 'NFO', 'net_debit': 13.55,
        'spread_width': 50, 'expiry': '2026-09-29',
        # zebra vocabulary
        'cohort': zcfg.COHORT_START, 'structure': 'bcs',
        'debit': 13.55, 'width': 50, 'timeframe': 'monthly', 'direction': 'CE',
        'entry_spot': 1360.0, 'tp_spot': 1400.0, 'sl_spot': 1319.0,
    }


def _probe(store, method):
    """Did the call take effect? Refusal is False OR an exception — both are a
    refusal, and which one a store chooses is not what this contract is about.
    """
    try:
        if method == 'update_trade_exit':
            store.update_trade_exit(1, dict(_EXIT_DATA))
            return True                      # returns None on success
        if method == 'recover_closing_trade':
            return bool(store.recover_closing_trade(1))
        return bool(getattr(store, method)(1, 'TP'))
    except TypeError:
        # A signature mismatch is a harness bug, not a refusal, and silently
        # scoring it as one would let every cell in the column read "refused"
        # while proving nothing. It cost three green-looking failures here.
        raise
    except Exception:
        return False


def _bcs_family_store(modname, clsname, stem, tmp_path, monkeypatch, status):
    mod = importlib.import_module(modname)
    data = tmp_path / f'{stem}.json'
    monkeypatch.setattr(mod, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(mod, 'LOCAL_TRADES_FILE', data)
    monkeypatch.setattr(mod, 'LOCK_FILE', tmp_path / f'{stem}.lock')
    data.write_text(json.dumps([_record(status)]), encoding='utf-8')
    s = getattr(mod, clsname)(config={'google_drive': {'enabled': False}})
    s.initialize()
    return s


def _zebra_store(tmp_path, monkeypatch, status):
    d = tmp_path / 'zebra'
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text(json.dumps([_record(status)]))
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.initialize()
    return ZebraStoreAdapter(s)


@pytest.mark.parametrize('method,role', sorted(CONTRACT))
def test_the_cohort_store_matches_the_contract(tmp_path, monkeypatch,
                                               method, role):
    """`ZebraStore` through the adapter IS the strictest real store, and it is
    the one the money path talks to for this cohort. The contract table is
    pinned to it, not merely compared with it."""
    store = _zebra_store(tmp_path, monkeypatch, ZEBRA_STATUSES[role])
    assert _probe(store, method) is CONTRACT[(method, role)]


@pytest.mark.parametrize('method,role', sorted(CONTRACT))
def test_the_fake_matches_the_contract(method, role):
    """The whole point of the file.

    Against the pre-fix `MemoryStore` this fails on five of the twelve cells —
    every `update_trade_exit` refusal, plus `begin_close` and
    `recover_closing_trade` from states the real stores decline — because the
    fake had no status checks at all and answered True unconditionally.

    A fake that accepts what production refuses does not merely fail to catch a
    bug; it actively certifies the broken code.
    """
    store = MemoryStore(trades=[_record(BCS_FAMILY_STATUSES[role])])
    assert _probe(store, method) is CONTRACT[(method, role)]


@pytest.mark.parametrize('method,role', sorted(CONTRACT))
def test_the_fake_speaks_zebras_vocabulary_too(method, role):
    """`replay.py` hands `MemoryStore` to `_open_zebra_store`, so the same fake
    stands in for a store whose open state is called 'entered'. If it only
    understood 'open' it would refuse every cohort close — stricter than
    production this time, but equally a fake testing something production does
    not do."""
    store = MemoryStore(trades=[_record(ZEBRA_STATUSES[role])])
    assert _probe(store, method) is CONTRACT[(method, role)]


@pytest.mark.parametrize('name,modname,clsname,stem', REAL_BOOKS,
                         ids=[b[0] for b in REAL_BOOKS])
@pytest.mark.parametrize('role', ROLES)
def test_the_fake_is_never_laxer_than_a_real_book(tmp_path, monkeypatch,
                                                  name, modname, clsname,
                                                  stem, role):
    """The inequality, stated directly and over every book.

    The table above says what the fake SHOULD do. This says the weaker thing
    that must hold no matter what the table says: for each real store and each
    role, anything the real store refuses, the fake must refuse too. It is the
    assertion that survives a future store being made stricter.
    """
    for method in ('begin_close', 'update_trade_exit', 'recover_closing_trade'):
        real = _probe(
            _bcs_family_store(modname, clsname, stem, tmp_path, monkeypatch,
                              BCS_FAMILY_STATUSES[role]),
            method)
        fake = _probe(MemoryStore(trades=[_record(BCS_FAMILY_STATUSES[role])]),
                      method)
        if not real:
            assert not fake, (
                f'{name}.{method} refuses a {role} record and MemoryStore '
                f'accepts it — the fake is safer than production, which is '
                f'how the exit-bridge bug shipped under a green suite')


@pytest.mark.parametrize('name,modname,clsname,stem', REAL_BOOKS,
                         ids=[b[0] for b in REAL_BOOKS])
@pytest.mark.parametrize('method,role', sorted(CONTRACT))
def test_every_real_book_matches_the_contract(tmp_path, monkeypatch, name,
                                              modname, clsname, stem,
                                              method, role):
    """The divergence this file used to RECORD is now closed.

    Until 2026-08-30 there was a test here called
    `test_the_bcs_family_is_laxer_than_zebra_on_booking`, and it asserted that
    `TradeStore.update_trade_exit` (and its two copies) had no status check at
    all — it would stamp 'closed' onto a record already closed, or onto one
    FROZEN at `partial_close` with live legs. It was recorded rather than fixed
    because those files were "outside this change's ownership", with the
    consequence spelled out: on those three books a double-close still
    double-books, and the only thing between that and a real order was
    `begin_close`.

    That is what a specification only a test reads always becomes. The table
    moved into `common/store_contract.py`, every store asks it, and the four
    books now hold to ONE rule set instead of two. So this replaces the
    divergence test with the assertion it was standing in for.
    """
    store = _bcs_family_store(modname, clsname, stem, tmp_path, monkeypatch,
                              BCS_FAMILY_STATUSES[role])
    assert _probe(store, method) is CONTRACT[(method, role)]


def test_the_table_is_the_implementation_not_a_copy_of_it():
    """The point of the whole item.

    `CONTRACT` here IS `common.store_contract.CONTRACT` — imported, not
    restated. A test that declared its own copy would be checking two
    implementations against each other again, which is exactly the arrangement
    that let the BCS family drift for as long as it did.

    RETIRES WHEN: nothing. This is an identity check on an import and costs
    nothing to keep; it fails only if somebody reintroduces a local table.
    """
    # The module-level import, NOT a fresh one inside the function: under
    # pytest's rootdir handling `common.store_contract` can be reached by two
    # import paths and therefore exist as two module objects, and an identity
    # check against the wrong one fails while proving nothing.
    assert CONTRACT is store_contract.CONTRACT
    assert BCS_FAMILY_STATUSES is store_contract.BCS_FAMILY_STATUSES
    assert ZEBRA_STATUSES is store_contract.ZEBRA_STATUSES
    # And the table is not ALSO written down here. Identity above catches a
    # copy assigned from the import; this catches one that was typed out —
    # which is what the file held before 2026-08-30.
    #
    # Parsed, not searched for. A substring check matches this very
    # assertion -- the self-reference trap two other guards in this session
    # walked into -- so the question is asked of the syntax tree: how many
    # module-level bindings of CONTRACT are there, and is the one that exists
    # an attribute read rather than a dict literal?
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(inspect.getmodule(_probe)))
    binds = [n for n in tree.body if isinstance(n, ast.Assign)
             for t in n.targets
             if isinstance(t, ast.Name) and t.id == 'CONTRACT']
    assert len(binds) == 1, (
        'CONTRACT is bound %d times at module level — the table is being '
        'restated again' % len(binds))
    assert isinstance(binds[0].value, ast.Attribute), (
        'CONTRACT is built here rather than imported')


def test_every_store_asks_the_table_rather_than_a_literal():
    """Read off the source, over all four books and the fake.

    The behavioural tests above prove the stores AGREE with the table today.
    They cannot prove the agreement is structural rather than coincidental, and
    the coincidence is the failure mode: two implementations that happen to
    match are exactly what this file spent months certifying.

    RETIRES WHEN: the four stores share one base class that owns the
    precondition, so there is one call site rather than four to check.
    """
    import inspect
    import importlib
    from bcs.tests import fakes
    from zebra.trade_store import ZebraStore

    subjects = [(importlib.import_module(m), c) for _n, m, c, _s in REAL_BOOKS]
    subjects.append((importlib.import_module('zebra.trade_store'),
                     'ZebraStore'))
    subjects.append((fakes, 'MemoryStore'))
    for mod, clsname in subjects:
        src = inspect.getsource(getattr(mod, clsname))
        assert 'store_contract.' in src, (
            '%s.%s does not consult common.store_contract — it is back to '
            'holding its own copy of the rules' % (mod.__name__, clsname))
    assert ZebraStore is not None


# ── M14 · every book can NAME its frozen records ────────────────────────────
#
# `get_frozen_trades` existed on the adapter and nowhere else, and nothing
# called it. A `partial_close` record drops out of the open book, so a store
# that cannot list them has no way to tell anyone a live position stopped being
# watched — the failure M14's sweep exists to end. Presence is checked over
# every book so a fifth store cannot be added without one.

ALL_BOOKS = REAL_BOOKS + [('zebra', None, None, None)]


@pytest.mark.parametrize('name,modname,clsname,stem', ALL_BOOKS,
                         ids=[b[0] for b in ALL_BOOKS])
def test_every_book_can_list_its_frozen_records(tmp_path, monkeypatch,
                                                name, modname, clsname, stem):
    if name == 'zebra':
        store = _zebra_store(tmp_path, monkeypatch, 'partial_close')
    else:
        store = _bcs_family_store(modname, clsname, stem, tmp_path,
                                  monkeypatch, 'partial_close')
    frozen = store.get_frozen_trades()
    assert [t['id'] for t in frozen] == [1], (
        f'{name}.get_frozen_trades() cannot see a partial_close record')


@pytest.mark.parametrize('name,modname,clsname,stem', ALL_BOOKS,
                         ids=[b[0] for b in ALL_BOOKS])
@pytest.mark.parametrize('status', ['open', 'closing', 'closed'])
def test_only_frozen_records_are_listed(tmp_path, monkeypatch, name, modname,
                                        clsname, stem, status):
    """The inverse review. A method that returned everything would make the
    sweep act on OPEN positions — orders on a live, correctly-monitored trade."""
    if name == 'zebra':
        zstatus = {'open': 'entered', 'closing': 'closing',
                   'closed': 'exited'}[status]
        store = _zebra_store(tmp_path, monkeypatch, zstatus)
    else:
        store = _bcs_family_store(modname, clsname, stem, tmp_path,
                                  monkeypatch, status)
    assert store.get_frozen_trades() == []


def test_the_fake_can_list_frozen_records_too():
    """`replay.py` hands `MemoryStore` to the monitor, so the sweep's own tests
    run against this. A fake without the method makes them unwritable."""
    frozen, open_rec = _record('partial_close'), _record('open')
    open_rec['id'] = 2
    store = MemoryStore(trades=[frozen, open_rec])
    assert [t['id'] for t in store.get_frozen_trades()] == [1]


def test_the_adapter_narrows_frozen_records_to_the_cohort(tmp_path,
                                                          monkeypatch):
    """The store method is deliberately NOT cohort-filtered — going direct must
    show the older generation's freezes too — and the adapter narrows. Both
    halves matter: filtering in the store would hide records from every direct
    reader, and not filtering in the adapter would hand the cohort sweep
    positions from a strategy that no longer trades."""
    d = tmp_path / 'zebra'
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    outsider = _record('partial_close')
    outsider['id'] = 2
    outsider.pop('cohort')
    (d / 'zebra_trades.json').write_text(
        json.dumps([_record('partial_close'), outsider]))
    raw = ZebraStore(config={'google_drive': {'enabled': False}})
    raw.initialize()

    assert sorted(t['id'] for t in raw.get_frozen_trades()) == [1, 2]
    assert [t['id'] for t in ZebraStoreAdapter(raw).get_frozen_trades()] == [1]


# ── S3 · every book can NAME its post-close residues ────────────────────────
#
# The residue twin of the frozen block above, and the same argument: a record
# BOOKED CLOSED whose reconcile found a live leg is out of the open book, out
# of `get_frozen_trades()`, and out of every sweep there is. A store that
# cannot list them has no way to tell anyone that a live leg stopped being
# watched — one Telegram at close time was the entire lifecycle.
#
# Presence is checked over every book so a fifth store cannot be added without
# one, and the sweep's `AttributeError` branch stays a belt on top of a brace.

def _with_residue(status, state='open'):
    r = _record(status)
    r['reconcile_residue'] = {'detected_at': '2026-08-29T10:00:00',
                              'state': state, 'detail': 'X net -700'}
    return r


def _seeded(name, modname, clsname, stem, tmp_path, monkeypatch, record):
    """One store holding exactly `record`, by whichever route it is built."""
    if name == 'zebra':
        d = tmp_path / 'zebra'
        d.mkdir(exist_ok=True)
        monkeypatch.setattr(zcfg, 'LOG_DIR', d)
        monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
        monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
        (d / 'zebra_trades.json').write_text(json.dumps([record]))
        s = ZebraStore(config={'google_drive': {'enabled': False}})
        s.initialize()
        return ZebraStoreAdapter(s)
    # Same construction as `_bcs_family_store`, which seeds `_record(status)`;
    # this one needs an arbitrary record, so the two share the recipe rather
    # than one calling the other with a flag.
    mod = importlib.import_module(modname)
    data = tmp_path / f'{stem}.json'
    monkeypatch.setattr(mod, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(mod, 'LOCAL_TRADES_FILE', data)
    monkeypatch.setattr(mod, 'LOCK_FILE', tmp_path / f'{stem}.lock')
    data.write_text(json.dumps([record]), encoding='utf-8')
    store = getattr(mod, clsname)(config={'google_drive': {'enabled': False}})
    store.initialize()
    return store


@pytest.mark.parametrize('name,modname,clsname,stem', ALL_BOOKS,
                         ids=[b[0] for b in ALL_BOOKS])
def test_every_book_can_list_its_post_close_residues(tmp_path, monkeypatch,
                                                     name, modname, clsname,
                                                     stem):
    terminal = ZEBRA_STATUSES if name == 'zebra' else BCS_FAMILY_STATUSES
    store = _seeded(name, modname, clsname, stem, tmp_path, monkeypatch,
                    _with_residue(terminal[TERMINAL]))
    assert [t['id'] for t in store.get_residue_trades()] == [1], (
        f'{name}.get_residue_trades() cannot see a closed record with an '
        f'unresolved residue')


@pytest.mark.parametrize('name,modname,clsname,stem', ALL_BOOKS,
                         ids=[b[0] for b in ALL_BOOKS])
@pytest.mark.parametrize('state', ['resolved', 'cleared'])
def test_a_settled_residue_is_not_listed(tmp_path, monkeypatch, name, modname,
                                         clsname, stem, state):
    """The inverse review. A method that kept returning settled incidents would
    nag forever about a leg that is gone, or one the owner cleared on purpose —
    and an alert nobody can end is one the reader learns to ignore."""
    terminal = ZEBRA_STATUSES if name == 'zebra' else BCS_FAMILY_STATUSES
    store = _seeded(name, modname, clsname, stem, tmp_path, monkeypatch,
                    _with_residue(terminal[TERMINAL], state=state))
    assert store.get_residue_trades() == []


@pytest.mark.parametrize('name,modname,clsname,stem', ALL_BOOKS,
                         ids=[b[0] for b in ALL_BOOKS])
@pytest.mark.parametrize('role', [OPEN, CLOSING, FROZEN])
def test_only_BOOKED_records_carry_a_residue_incident(tmp_path, monkeypatch,
                                                      name, modname, clsname,
                                                      stem, role):
    """Deliberately NOT the frozen list, and not the open one.

    A `partial_close` record already has a watcher (the M14 recovery sweep) and
    a nag of its own; naming it here too would double every alert for one
    position and would put a residue reader on a record the ORDER path owns.
    An OPEN record's legs are supposed to be live. The only records this may
    name are the ones nothing else can see.
    """
    statuses = ZEBRA_STATUSES if name == 'zebra' else BCS_FAMILY_STATUSES
    store = _seeded(name, modname, clsname, stem, tmp_path, monkeypatch,
                    _with_residue(statuses[role]))
    assert store.get_residue_trades() == []


def test_the_fake_can_list_residues_too():
    """`replay.py` hands `MemoryStore` to the monitor, so the sweep's own tests
    run against this. A fake without the method makes them unwritable."""
    store = MemoryStore(trades=[_with_residue('closed'),
                                dict(_with_residue('closed'), id=2,
                                     reconcile_residue={'state': 'resolved'})])
    assert [t['id'] for t in store.get_residue_trades()] == [1]


def test_the_adapter_narrows_residues_to_the_cohort(tmp_path, monkeypatch):
    """Same split as the frozen twin: the store method sees every generation,
    the adapter narrows. Not filtering in the adapter would hand the residue
    sweep a retired strategy's positions; filtering in the store would hide
    them from every reader that goes direct."""
    d = tmp_path / 'zebra'
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    outsider = _with_residue('exited')
    outsider['id'] = 2
    outsider.pop('cohort')
    (d / 'zebra_trades.json').write_text(
        json.dumps([_with_residue('exited'), outsider]))
    raw = ZebraStore(config={'google_drive': {'enabled': False}})
    raw.initialize()

    assert sorted(t['id'] for t in raw.get_residue_trades()) == [1, 2]
    assert [t['id'] for t in ZebraStoreAdapter(raw).get_residue_trades()] == [1]


def test_the_adapter_maps_a_residue_record_into_the_monitors_vocabulary(
        tmp_path, monkeypatch):
    """The sweep names legs by `_legs_of`, which reads `short_symbol` /
    `long_symbol`. Handing it a RAW zebra record would give it a record with no
    legs it can read — a false clean of exactly the kind this item exists to
    end."""
    d = tmp_path / 'zebra'
    d.mkdir(exist_ok=True)
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text(json.dumps([_with_residue('exited')]))
    raw = ZebraStore(config={'google_drive': {'enabled': False}})
    raw.initialize()

    from bcs.spread_monitor import _legs_of
    [mapped] = ZebraStoreAdapter(raw).get_residue_trades()
    assert _legs_of(mapped), 'the adapter handed the sweep a legless record'
