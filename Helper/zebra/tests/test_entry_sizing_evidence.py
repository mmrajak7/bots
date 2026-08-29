"""The size decision has to be auditable, or the lot ladder cannot be calibrated.

Owner, 2026-08-30: *"capital utilisation and how many lots we can enter as we
scale ensuring legs intact ... capital utilisation and risk management should
go together."*

Answering that from the book turned out to be impossible, for one reason:

  * `capital.liquidity_lots` sizes a position from `long_ask_qty` /
    `short_bid_qty` — the resting size at the touch, and the ONLY thing
    standing between "3 lots" and an order bigger than the book — and neither
    was ever persisted. 13 cohort records, ZERO with depth on them.
  * `capital.plan` returns every limit's own answer and its docstring says why
    ("a size is a decision, and '3 lots' with no record of what the other four
    limits said cannot be audited after the fact"). It was computed on every
    triggered signal and spent on a log line.

So the two inputs to every future scaling decision were the two the book did
not keep. Same shape, and the same fix, as the 2026-08-12 change that started
persisting the entry BOOKS: 42 records, zero with a book on them, which made
the entry-cost gate impossible to calibrate.

Pure observability. No sizing behaviour changes here.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_entry_sizing_evidence.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg               # noqa: E402
from zebra.trade_store import ZebraStore      # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    return s


def _bcs(**over):
    b = {
        'long_strike': 100.0, 'short_strike': 140.0,
        'long_symbol': 'TESTCO26SEP100CE', 'short_symbol': 'TESTCO26SEP140CE',
        'debit': 10.0, 'width': 40.0, 'lot_size': 100, 'lots': 1,
        'expiry': '2026-09-30', 'structure': 'bcs',
        'entry_spot': 96.0, 'tp_spot': 104.0,
        'long_bid': 11.0, 'long_ask': 11.2, 'long_mid': 11.1,
        'short_bid': 1.1, 'short_ask': 1.3, 'short_mid': 1.2,
        'long_oi': 50000, 'short_oi': 40000,
        # The two the book never kept.
        'long_ask_qty': 4200, 'short_bid_qty': 900,
        # A REAL plan: `lots` is min(bounds.values()) and `bound` is the key
        # that achieved it. The first draft of this fixture said
        # bound='liquidity' with lots=1 against liquidity=9, which
        # `test_the_binding_limit_is_recoverable_from_the_record` rejected —
        # the invariant it asserts is exactly what makes the field auditable.
        'entry_plan': {'lots': 1, 'bound': 'max_lots',
                       'bounds': {'max_lots': 1, 'max_trade_rupees': 2,
                                  'liquidity': 9},
                       'capital': 1000.0},
    }
    b.update(over)
    return b


# -- depth ------------------------------------------------------------------

def test_the_depth_the_size_was_chosen_from_is_persisted(store):
    """THE defect. `liquidity_lots` reads exactly these two fields and they
    vanished the moment the entry was written."""
    store.mark_entered_bcs(1, _bcs())
    t = store.find(1)
    assert t['long_ask_qty_entry'] == 4200
    assert t['short_bid_qty_entry'] == 900


def test_the_THINNER_side_is_recoverable_from_the_record(store):
    """What `liquidity_lots` actually decides on: entry BUYS the long (needs
    size on the ask) and SELLS the short (needs size on the bid), so the
    binding side is whichever is thinner. Both are stored because which one
    binds is itself the finding."""
    from zebra import capital
    store.mark_entered_bcs(1, _bcs())
    t = store.find(1)
    replayed = capital.liquidity_lots(
        {'long': {'ask_qty': t['long_ask_qty_entry']},
         'short': {'bid_qty': t['short_bid_qty_entry']}}, t['lot_size'])
    assert replayed == 9        # 900 // 100, the short bid binds


def test_missing_depth_is_stored_as_None_not_as_zero(store):
    """A quote that carried no depth must not read back as "the book was
    empty". None is unknown; 0 is a finding, and `liquidity_lots` treats 0 as
    "no lots fit" — inventing that from an absent field would look like
    measured illiquidity forever."""
    b = _bcs()
    b.pop('long_ask_qty')
    b.pop('short_bid_qty')
    store.mark_entered_bcs(1, b)
    t = store.find(1)
    assert t['long_ask_qty_entry'] is None
    assert t['short_bid_qty_entry'] is None


def test_OI_is_not_used_as_a_substitute(store):
    """OI counts open contracts, not resting size at the touch. The cohort's
    thinner-leg OI runs 8,550 to 2.1M, which says nothing about whether one
    lot fills without walking the book. They are separate fields and must
    stay separate."""
    store.mark_entered_bcs(1, _bcs())
    t = store.find(1)
    assert t['long_oi_entry'] == 50000
    assert t['long_ask_qty_entry'] == 4200
    assert t['long_oi_entry'] != t['long_ask_qty_entry']


# -- the sizing decision ----------------------------------------------------

def test_every_limits_answer_is_kept_not_just_the_winner(store):
    """`capital.plan`'s own docstring: "3 lots" with no record of what the
    other four limits said cannot be audited after the fact. It said that and
    then handed `bounds` to a log line."""
    store.mark_entered_bcs(1, _bcs())
    plan = store.find(1)['entry_plan']
    assert plan['bound'] == 'max_lots'
    assert plan['bounds'] == {'max_lots': 1, 'max_trade_rupees': 2,
                              'liquidity': 9}
    # The losers are the point: at Rs 2L the LADDER decided, and the book now
    # says the touch would have taken 9 lots and the rupee cap 2. That is the
    # measurement the scaling question needs.


def test_the_binding_limit_is_recoverable_from_the_record(store):
    """The question this exists to answer: as capital grows, WHICH limit
    actually decides the size? Measured on the cohort as of 2026-08-30 the
    ladder binds at every capital level and the 12.5% per-trade cap has never
    bound anything — but that was derived by replaying `plan`, not read off
    the book, because the book did not carry it."""
    store.mark_entered_bcs(1, _bcs())
    plan = store.find(1)['entry_plan']
    assert plan['bounds'][plan['bound']] == plan['lots']


def test_a_record_written_without_a_plan_still_works(store):
    """The pre-gate can fail (`capital pre-gate failed ... continuing
    unsized`) and a hand-placed `zebra enter` never runs it at all. An absent
    plan must be absent, not a fabricated one."""
    b = _bcs()
    b.pop('entry_plan')
    store.mark_entered_bcs(1, b)
    assert store.find(1)['entry_plan'] is None


def test_this_changes_no_sizing_behaviour():
    """Observability only. `plan` decides exactly what it decided before; the
    record simply now says what it decided and why.

    RETIRES WHEN: `mark_entered_bcs` takes the plan as an argument rather than
    reading it off the `bcs` dict, so an entry cannot be written without one.
    """
    import inspect
    from zebra import capital
    src = inspect.getsource(capital.plan)
    assert 'entry_plan' not in src, (
        'capital.plan now reads the persisted field — the recorder has become '
        'an input to the decision it records')
