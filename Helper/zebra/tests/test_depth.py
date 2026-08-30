"""Depth at the touch — the one sizing limit that is a fact, not a choice.

Owner, 2026-08-30: *"let us collect oi and depth in coming weeks and decide"*.

Sizing has three limits: the budget, the `capital_per_lot` ladder, and
LIQUIDITY. The first two are arithmetic on numbers we pick; the third decides
whether "legs intact" survives scaling, and the book had never recorded it.

The EXIT side is the half that matters. An entry that cannot fill costs
nothing — the executor gives up and the signal returns next cycle. A stop that
cannot fill is unbounded, and this book has twice paid real money for exits
through a book that could not carry them.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_depth.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import depth as d                    # noqa: E402

LOT = 100


def legs(long_bid_qty=None, short_ask_qty=None, **extra):
    return {'long': {'symbol': 'L', 'bid_qty': long_bid_qty,
                     'ask_qty': 9999, **extra},
            'short': {'symbol': 'S', 'ask_qty': short_ask_qty,
                      'bid_qty': 9999, **extra}}


def trade(**over):
    t = {'id': 1, 'stock': 'TESTCO', 'lot_size': LOT}
    t.update(over)
    return t


# -- which side binds --------------------------------------------------------

def test_it_reads_the_sides_a_CLOSE_actually_trades_against():
    """Closing SELLS the long (needs size on the long's BID) and BUYS BACK the
    short (size on the short's ASK). Reading the entry sides instead would
    measure a trade nobody is going to make."""
    assert d.exit_lots(legs(long_bid_qty=500, short_ask_qty=900), LOT) == 5
    assert d.exit_lots(legs(long_bid_qty=900, short_ask_qty=300), LOT) == 3


def test_the_thinner_side_binds():
    assert d.exit_lots(legs(long_bid_qty=250, short_ask_qty=10_000), LOT) == 2


def test_it_is_the_MIRROR_of_the_entry_side_calculation():
    """`capital.liquidity_lots` sizes the entry off the long ASK and the short
    BID. Same arithmetic, opposite sides — pinned together so a change to one
    cannot silently leave the other measuring the wrong book."""
    from zebra import capital
    entry = capital.liquidity_lots(
        {'long': {'ask_qty': 400}, 'short': {'bid_qty': 700}}, LOT)
    exit_ = d.exit_lots(legs(long_bid_qty=400, short_ask_qty=700), LOT)
    assert entry == exit_ == 4


@pytest.mark.parametrize('bad', [None, {}, {'long': {}}, {'long': {'bid_qty': None},
                                                          'short': {'ask_qty': 5}}])
def test_absent_depth_is_None_not_zero(bad):
    """0 is a FINDING — "the touch is empty" — and manufacturing it from a
    missing field would report measured illiquidity that was never measured."""
    assert d.exit_lots(bad, LOT) is None


def test_a_genuinely_empty_touch_IS_zero():
    """The negative control for the rule above: when the book really does quote
    no size, that must come through as 0 and be counted."""
    assert d.exit_lots(legs(long_bid_qty=0, short_ask_qty=800), LOT) == 0


@pytest.mark.parametrize('lot', [None, 0, -1, 'x'])
def test_a_useless_lot_size_is_unknowable_not_infinite(lot):
    assert d.exit_lots(legs(long_bid_qty=500, short_ask_qty=500), lot) is None


# -- sampling ---------------------------------------------------------------

def test_the_first_reading_is_always_taken():
    patch = d.observe(trade(), legs(300, 400), now=1000.0)
    assert patch[d.FIELD]['samples'] == 1
    assert patch[d.FIELD]['ge_1'] == 1
    assert patch[d.FIELD]['ge_3'] == 1
    assert patch[d.FIELD]['ge_5'] == 0
    assert patch[d.FIELD]['worst_lots'] == 3


def test_a_poll_inside_the_window_writes_NOTHING():
    """The property `zebra.mfe` is deliberately built on: once nothing is
    changing, the tracking goes completely silent. A per-poll counter would
    rewrite a ~1MB store every cycle forever, undoing the batching that exists
    to prevent exactly that."""
    t = trade(**d.observe(trade(), legs(300, 400), now=1000.0))
    assert d.observe(t, legs(300, 400), now=1000.0 + d.SAMPLE_SEC - 1) == {}


def test_a_poll_past_the_window_samples_again():
    t = trade(**d.observe(trade(), legs(300, 400), now=1000.0))
    patch = d.observe(t, legs(100, 400), now=1000.0 + d.SAMPLE_SEC)
    assert patch[d.FIELD]['samples'] == 2
    assert patch[d.FIELD]['worst_lots'] == 1


def test_the_sample_rate_is_independent_of_the_callers_cadence():
    """zebra polls every 5 minutes and the order path every 5 SECONDS. Counting
    polls would make the same book look 60x better measured under one engine
    than the other, and would silently re-weight everything the day exits
    arm."""
    slow = trade()
    for i in range(4):                       # 4 x 5min = 20min
        p = d.observe(slow, legs(300, 400), now=1000.0 + i * 300)
        slow.update(p)
    fast = trade()
    for i in range(240):                     # 240 x 5s = 20min
        p = d.observe(fast, legs(300, 400), now=1000.0 + i * 5)
        fast.update(p)
    assert slow[d.FIELD]['samples'] == fast[d.FIELD]['samples']


# -- the histogram -----------------------------------------------------------

def test_a_dark_book_is_counted_not_skipped():
    """A book that stops quoting size is exactly the book a stop cannot leave.
    Dropping those readings would let a position improve its own score by
    going dark."""
    t = trade()
    t.update(d.observe(t, legs(500, 500), now=0.0))
    t.update(d.observe(t, None, now=d.SAMPLE_SEC))
    s = d.summary(t)
    assert s['polls'] == 2 and s['unknown'] == 1 and s['measured'] == 1
    assert s['unknown_pct'] == 50.0
    assert s['ge_1_pct'] == 100.0      # of the readings that COULD be taken


def test_the_worst_reading_is_kept_whole():
    """The histogram answers "how often"; a stop only has to meet the worst
    book once."""
    t = trade()
    for i, (lb, sa) in enumerate([(900, 900), (100, 900), (900, 900)]):
        t.update(d.observe(t, legs(lb, sa), now=i * d.SAMPLE_SEC))
    assert d.summary(t)['worst_lots'] == 1
    assert d.summary(t)['ge_5_pct'] == pytest.approx(66.7, abs=0.1)


def test_a_position_never_measured_reports_None():
    """Absent evidence must not render as a score. `report` says so in words
    rather than showing an empty table that reads like a clean book."""
    assert d.summary(trade()) is None
    assert 'No depth measured yet' in d.report([trade()])


def test_observe_never_raises():
    """Measurement sitting in the exit path. A measurement that can throw is a
    new way to fail an exit."""
    for junk in (None, 'x', 42, {'long': 'not-a-dict'}):
        assert isinstance(d.observe(trade(), junk), dict)
    assert isinstance(d.observe({'lot_size': 'x'}, legs(1, 1)), dict)


# -- the report --------------------------------------------------------------

def test_the_report_aggregates_over_readings_not_positions():
    """A position open three weeks carries more evidence than one open two
    days; averaging their percentages would weight them equally."""
    long_lived = trade(id=1, stock='LONGCO')
    for i in range(9):
        long_lived.update(d.observe(long_lived, legs(900, 900),
                                    now=i * d.SAMPLE_SEC))
    short_lived = trade(id=2, stock='SHORTCO')
    short_lived.update(d.observe(short_lived, legs(50, 900), now=0.0))

    out = d.report([long_lived, short_lived])
    # 9 of 10 readings carried 5+ lots; a per-position mean would say 50%.
    assert '90%' in out
    assert 'ALL' in out


def test_the_report_shows_the_worst_and_says_to_read_it():
    t = trade()
    t.update(d.observe(t, legs(50, 900), now=0.0))
    out = d.report([t])
    assert 'worst' in out
    assert 'worst book once' in out


# -- wiring ------------------------------------------------------------------

def test_the_monitor_actually_samples_it():
    """The recurring bug in this fleet is code that is written, tested and
    never reached.

    RETIRES WHEN: the per-poll measurements (peak, corroboration, depth) are
    driven from one list the cycle iterates, so a measurement that is declared
    is taken.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.check_entered)
    assert 'depth_mod.observe' in src


def test_the_leg_book_carries_the_size():
    """`_quote_option` has returned bid_qty/ask_qty all along and `_leg_book`
    dropped them, so the POLL line, the persisted `exit_legs` and the vetting
    context all described a book without saying how much of it there was.

    RETIRES WHEN: `_leg_book` is generated from the quote's own schema rather
    than by naming fields, so a field cannot be silently omitted.
    """
    from zebra import monitor
    book = monitor._leg_book('SYM', {'bid': 1.0, 'ask': 1.2, 'mid': 1.1,
                                     'bid_qty': 700, 'ask_qty': 800})
    assert book['bid_qty'] == 700 and book['ask_qty'] == 800


def test_the_field_is_allowed_on_the_batched_poll_write():
    """`apply_mfe` refuses any key outside its allowlist, because callers pass
    whole-state patches and one typo would overwrite `status` or `debit` on a
    live position. A new batched field has to be declared there."""
    from zebra.trade_store import _BATCHED_POLL_FIELDS
    assert d.FIELD in _BATCHED_POLL_FIELDS
