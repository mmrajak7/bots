"""Three books, one implementation — and each still its own book.

`common/spread_store.py` collapsed ~870 lines of duplication out of
`bcs`/`bear_put`/`fallen_hero`. The duplication was not a tidiness problem: the
S3 residue sweep had to be added to three files, `reconcile_after_close` read
two leg fields of six because Fallen Hero's copy was never opened, and
`get_entry_residue_trades` was written three times in one afternoon of this
same session.

The merge's own risk is the exact opposite of the one it removes: shared code
that resolves ONE store's paths, lock or logger for all three would be far
worse than three copies. Everything below is about that.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_spread_store_base.py -v
"""
import importlib
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common.locked_store import LockedStoreMixin      # noqa: E402
from common.spread_store import SpreadStoreBase       # noqa: E402

NO_DRIVE = {'google_drive': {'enabled': False}}

#: (module, class, file stem)
BOOKS = [
    ('bcs.trade_store', 'TradeStore', 'bcs_trades'),
    ('bear_put.trade_store', 'BearPutStore', 'bear_put_trades'),
    ('fallen_hero.trade_store', 'FallenHeroStore', 'fallen_hero_trades'),
]
IDS = [b[1] for b in BOOKS]


def _cls(modname, clsname):
    return getattr(importlib.import_module(modname), clsname)


# -- each book is still its own book -----------------------------------------

@pytest.mark.parametrize('modname,clsname,stem', BOOKS, ids=IDS)
def test_each_store_resolves_its_OWN_paths(modname, clsname, stem):
    """The merge's central risk, stated directly.

    Shared code that resolved one book's data file for all three would put
    three sets of real positions in one file, or lock a book against the wrong
    writer. Worse than the duplication it replaced.
    """
    store = _cls(modname, clsname)(config=dict(NO_DRIVE))
    assert store._data_path().name == '%s.json' % stem
    assert store._lock_path().name == '%s.lock' % stem


@pytest.mark.parametrize('modname,clsname,stem', BOOKS, ids=IDS)
def test_each_store_logs_under_its_own_name(modname, clsname, stem):
    """A shared logger would file every book's warnings under one name, and
    the first question asked of any of these logs is WHICH BOOK."""
    store = _cls(modname, clsname)(config=dict(NO_DRIVE))
    assert store._logger.name == modname


@pytest.mark.parametrize('modname,clsname,stem', BOOKS, ids=IDS)
def test_the_paths_are_resolved_at_CALL_time(monkeypatch, modname, clsname,
                                             stem, tmp_path):
    """The property the original `_lock_path` docstring existed to protect.

    A class attribute captured at import would freeze the lock in the real
    `logs/` directory while a test redirected the data to `tmp_path` --
    "locked, but not against the writer that matters". Every store test in
    this repo monkeypatches the module global, so the lookup must go through
    the module every time.
    """
    mod = importlib.import_module(modname)
    store = _cls(modname, clsname)(config=dict(NO_DRIVE))
    monkeypatch.setattr(mod, 'LOCAL_TRADES_FILE', tmp_path / 'moved.json')
    monkeypatch.setattr(mod, 'LOCK_FILE', tmp_path / 'moved.lock')
    assert store._data_path() == tmp_path / 'moved.json'
    assert store._lock_path() == tmp_path / 'moved.lock'


@pytest.mark.parametrize('modname,clsname,stem', BOOKS, ids=IDS)
def test_no_store_inherits_the_mixins_refusing_lock_path(modname, clsname,
                                                         stem):
    """`LockedStoreMixin._lock_path` raises by design. A store that reached it
    would refuse every write."""
    cls = _cls(modname, clsname)
    assert cls._lock_path is not LockedStoreMixin._lock_path


# -- what deliberately did NOT merge -----------------------------------------

@pytest.mark.parametrize('modname,clsname,stem', BOOKS, ids=IDS)
def test_the_per_schema_methods_stay_in_each_book(modname, clsname, stem):
    """`add_trade` is 58 / 80 / 164 lines of cross-field validation about three
    different structures. Merging it would mean one method that knows about
    bull call spreads AND reverse jade lizards, which is how a base class
    becomes the thing everybody is afraid to touch."""
    cls = _cls(modname, clsname)
    for name in ('add_trade', 'list_trades'):
        assert name in cls.__dict__, (
            '%s no longer defines %s — a per-schema method drifted into the '
            'shared base' % (clsname, name))


def test_the_zebra_store_is_NOT_merged_in():
    """Measured 0.095 similar: a genuinely different state machine, with
    signals that never become positions. Forcing it into this hierarchy would
    manufacture the coupling the base exists to remove."""
    from zebra.trade_store import ZebraStore
    assert not issubclass(ZebraStore, SpreadStoreBase)


def test_the_exit_bound_is_a_hook_and_bear_put_visibly_lacks_one():
    """A vertical is capped by its width; a reverse jade lizard is not capped
    the same way at all, so the bound is per-structure.

    `bear_put` has never had one. Before the merge that was an absent call in a
    method nobody compared; now it is an unoverridden hook, which is the
    difference between a gap and an oversight.
    """
    from bcs.trade_store import TradeStore
    from bear_put.trade_store import BearPutStore
    from fallen_hero.trade_store import FallenHeroStore
    assert '_bound_exit' in TradeStore.__dict__
    assert '_bound_exit' in FallenHeroStore.__dict__
    assert '_bound_exit' not in BearPutStore.__dict__
    assert BearPutStore._bound_exit is SpreadStoreBase._bound_exit


def test_the_default_bound_is_the_identity():
    """It must not invent a clamp for a structure nobody has measured one for.
    An unbounded book is a known gap; a wrong bound is a booked number that is
    quietly false."""
    data = {'exit_reason': 'TP', 'exit_spread': 41.0}
    assert SpreadStoreBase._bound_exit(None, {'id': 1}, data) is data


# -- the module resolution itself --------------------------------------------

def test_a_store_that_forgets_MODULE_fails_loudly():
    """Falling through to whatever the BASE module happens to define would, for
    a book of real positions, be the worst available failure — so it raises."""
    class Orphan(SpreadStoreBase):
        pass

    with pytest.raises(RuntimeError) as e:
        Orphan()._mod()
    assert '_MODULE' in str(e.value)


def test_a_subclass_of_a_real_store_still_finds_that_stores_module():
    """Resolved by walking the MRO rather than by `type(self).__module__`.
    Test doubles subclass these stores (`test_entry_residue._Broken` does), and
    resolving to the TEST's module would find no paths at all."""
    from bcs.trade_store import TradeStore

    class Probe(TradeStore):
        pass

    assert Probe(config=dict(NO_DRIVE))._data_path().name == 'bcs_trades.json'


def test_every_book_declares_its_module():
    for modname, clsname, _stem in BOOKS:
        cls = _cls(modname, clsname)
        assert cls.__dict__.get('_MODULE') == modname
