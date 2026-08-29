"""B21 — the intrinsic floor was call-only, so BPS had no floor at all.

`spread_intrinsic_floor` computed `max(spot - k_l, 0) - max(spot - k_s, 0)`
unconditionally. That is call arithmetic. A bear put spread holds the HIGHER
strike long, so for every spot that expression returns zero or a negative
number — and a negative structure value is already refused upstream as
`negative_spread`, which means `value < floor` could never be true.

Not "approximate for puts". INERT for puts, and inert exactly where the guard
matters: at spot 1250 a 1400/1340 put spread is worth its full 60, the old
floor said **-9.67**, and a garbage quote of 1.00 passed straight through. That
is the ABB #242 scenario itself — a junk print on a deep-ITM leg booking a
massive false loss — completely unguarded on one of the three live books.

`bcs/spread_monitor.py` monitors BCS, BPS and FH. The guard was written for the
first and silently skipped the second, which is
`feedback_copy_pasted_modules_fix_once` from the other direction: not three
copies of one bug, but one guard that only ever covered a third of its callers.

Also fixed here, same function, both found by their own failures:
  * the allowance used call intrinsic for puts too
  * B17 — a missing `entry_spot` fell back to `0.3 * net_debit`, TIGHTER than
    the truth, which blinded the monitor on a healthy book

Run:  cd Helper && python -m pytest bcs/tests/test_b21_floor_calls_and_puts.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402


#: Bull call spread: long the LOWER strike. Real ICICIBANK shape.
BCS = {'long_symbol': 'TESTCO26SEP1360CE', 'short_symbol': 'TESTCO26SEP1410CE',
       'entry_short_price': 7.65, 'entry_spot': 1360.0, 'net_debit': 13.55}

#: Bear put spread: long the HIGHER strike. Real CIPLA-shaped book.
BPS = {'_store_type': 'bps',
       'long_symbol': 'TESTCO26SEP1400PE', 'short_symbol': 'TESTCO26SEP1340PE',
       'entry_short_price': 26.45, 'entry_spot': 1360.0, 'net_debit': 13.55}


# ── The bug: a put spread at full value had a negative floor ────────────────

def test_a_deep_itm_put_spread_has_a_positive_floor():
    """spot 1250, strikes 1400/1340: worth its full 60 and cannot be worth
    less. The old call arithmetic returned -9.67 here."""
    floor = sm.spread_intrinsic_floor(BPS, 1250.0)
    assert floor is not None and floor > 0, (
        f'a put spread at maximum value has floor {floor} — the guard is '
        f'still computing call intrinsic')


def test_the_abb242_garbage_print_is_now_caught_on_a_put_spread():
    """The scenario the guard exists for, on the book it was skipping."""
    floor = sm.spread_intrinsic_floor(BPS, 1250.0)
    assert 1.00 < floor, 'a 1.00 quote on a spread worth 60 was not refused'


@pytest.mark.parametrize('spot,intrinsic', [
    (1450.0, 0.0), (1400.0, 0.0), (1370.0, 30.0), (1340.0, 60.0),
    (1250.0, 60.0),
])
def test_the_put_floor_never_exceeds_the_true_intrinsic(spot, intrinsic):
    """The floor is intrinsic MINUS a generous allowance, so it must sit at or
    below true intrinsic at every spot. Above it, and the guard would refuse
    prices the structure can genuinely trade at."""
    floor = sm.spread_intrinsic_floor(BPS, spot)
    assert floor <= intrinsic + 1e-9, (
        f'floor {floor} exceeds true put intrinsic {intrinsic} at {spot}')


# ── The call path must be untouched ─────────────────────────────────────────

@pytest.mark.parametrize('spot,expected', [
    (1409.50, 38.02), (1406.10, 34.62),
])
def test_the_call_floor_is_byte_for_byte_what_it_was(spot, expected):
    """Pinned against the numbers measured on the REAL recorded ICICI books
    before this change. Making the guard work for puts must not move calls."""
    assert sm.spread_intrinsic_floor(BCS, spot) == pytest.approx(expected,
                                                                 abs=0.01)


def test_a_call_spread_still_flags_an_impossible_price():
    """Negative control for the pinning above: the CE guard still fires."""
    floor = sm.spread_intrinsic_floor(BCS, 1450.0)
    assert 5.00 < floor, 'the call floor stopped catching impossible prices'


# ── The allowance has to use the right intrinsic too ────────────────────────

def test_a_short_put_itm_at_entry_gets_a_smaller_allowance():
    """The allowance is the short leg's EXTRINSIC at entry. For a put that is
    `short_px - max(k_s - entry_spot, 0)`; using the call form would have
    charged the full premium as extrinsic on an ITM short and pushed the floor
    down, quietly weakening the guard.
    """
    otm_short = dict(BPS, entry_spot=1360.0)          # k_s 1340 is OTM
    itm_short = dict(BPS, entry_spot=1330.0)          # k_s 1340 is 10 ITM
    assert sm.spread_intrinsic_floor(itm_short, 1250.0) > \
        sm.spread_intrinsic_floor(otm_short, 1250.0), (
            'an ITM short put was charged its whole premium as extrinsic')


def test_a_short_call_itm_at_entry_gets_a_smaller_allowance():
    """The same property on the call side, which already worked. Present so a
    future edit cannot fix puts by breaking calls."""
    otm = dict(BCS, entry_spot=1360.0)                # k_s 1410 is OTM
    itm = dict(BCS, entry_spot=1415.0)                # k_s 1410 is 5 ITM
    assert sm.spread_intrinsic_floor(itm, 1450.0) > \
        sm.spread_intrinsic_floor(otm, 1450.0)


# ── B17: ignorance must widen the benefit of the doubt, never narrow it ─────

def test_a_missing_entry_spot_no_longer_blinds_a_healthy_book():
    """The Feb-2026 replay found this. The old fallback `0.3 * net_debit` is
    4.07 against a true 7.65, moving the floor from 38.03 to 43.40 — above the
    REAL, healthy 09:16 book at 38.95. The monitor then refused every
    valuation for the rest of the session, so SL_SPREAD, SL_TRAIL and the
    trail all went dark. A false blind is the opposite of what the floor is
    for.
    """
    no_spot = {k: v for k, v in BCS.items() if k != 'entry_spot'}
    floor = sm.spread_intrinsic_floor(no_spot, 1409.50)
    assert 38.95 >= floor, (
        f'floor {floor} still refuses the real healthy book at 38.95')


@pytest.mark.parametrize('trade', [BCS, BPS], ids=['bcs', 'bps'])
@pytest.mark.parametrize('spot', [1250.0, 1340.0, 1409.50, 1450.0])
def test_dropping_entry_spot_can_only_loosen_the_floor(trade, spot):
    """The property behind the fix, checked on both structures.

    `allowance = short_px - short_intrinsic_at_entry <= short_px`, so using the
    whole premium when `entry_spot` is unknown is a strict upper bound on the
    allowance and therefore a lower — more generous — floor. Not knowing
    something must never make a guard stricter.
    """
    known = sm.spread_intrinsic_floor(trade, spot)
    unknown = sm.spread_intrinsic_floor(
        {k: v for k, v in trade.items() if k != 'entry_spot'}, spot)
    assert unknown <= known + 1e-9, (
        f'dropping entry_spot TIGHTENED the floor: {known} -> {unknown}')


def test_no_entry_short_price_disables_the_floor_rather_than_guessing():
    """There is no basis for an allowance at all. A floor built on a guess is
    not a no-arbitrage bound, and guessing too tight blinds the monitor —
    which is strictly worse than having no floor, because the other guards
    still run."""
    blind = {k: v for k, v in BCS.items() if k != 'entry_short_price'}
    assert sm.spread_intrinsic_floor(blind, 1409.50) is None


# ── Direction comes from the symbol, not the store ──────────────────────────

@pytest.mark.parametrize('sym,expect', [
    ('TESTCO26SEP1400PE', 'PE'), ('TESTCO26SEP1360CE', 'CE'),
    ('TESTCO26SEP1400.5PE', 'PE'), ('NIFTY26SEP25000CE', 'CE'),
    ('', None), (None, None), ('TESTCO26SEPFUT', None),
])
def test_the_option_type_is_read_off_the_symbol(sym, expect):
    assert sm.option_type_from_symbol(sym) == expect


def test_a_mislabelled_store_type_does_not_change_the_arithmetic():
    """`_store_type` is stamped by whichever store loaded the record — a fact
    about bookkeeping. The payoff is a fact about the contract. Had the fix
    branched on the flag instead of the symbol, a BPS record loaded through
    the wrong store would silently get call arithmetic back."""
    lying = dict(BPS, _store_type='bcs')
    assert sm.spread_intrinsic_floor(lying, 1250.0) == \
        sm.spread_intrinsic_floor(BPS, 1250.0)


def test_legs_in_different_instruments_disable_the_floor():
    """A CE long against a PE short is not a vertical, and this function has
    no idea what it is worth. Fail open rather than price a shape it does not
    know."""
    mixed = dict(BCS, short_symbol='TESTCO26SEP1410PE')
    assert sm.spread_intrinsic_floor(mixed, 1409.50) is None


def test_an_unparseable_symbol_still_disables_the_floor():
    """Pre-existing behaviour, re-pinned because the early-return order moved.
    """
    assert sm.spread_intrinsic_floor(
        dict(BCS, long_symbol='WHAT-IS-THIS'), 1409.50) is None


def test_the_missing_price_disable_is_an_explicit_branch_not_a_caught_error():
    """A mutation run removed the `short_px is None` early return and this
    file stayed green: execution fell through to `float(None)`, the broad
    `except Exception` caught the TypeError, and the function returned None
    anyway. Same answer, by accident.

    That is worth pinning because the two are not equally durable. The
    designed branch survives someone narrowing the exception handler — a
    perfectly reasonable tidy-up — and the accidental one turns into a
    TypeError raised inside the poll loop of the file that places real orders.
    Structural, since no input can tell the two apart.
    """
    import ast
    import inspect
    import textwrap

    # The ladder moved to `common.spread_valuation._allowance` on 2026-08-30
    # when the two engines' floors were merged. The property is unchanged and
    # so is the reason for it; only the file moved.
    from common import spread_valuation
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(spread_valuation._allowance)))
    guarded = any(
        isinstance(n, ast.If)
        and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Name)
        and n.test.left.id == 'short_px'
        and any(isinstance(op, ast.Is) for op in n.test.ops)
        and any(isinstance(st, ast.Return) for st in n.body)
        for n in ast.walk(tree))
    assert guarded, (
        'the allowance ladder no longer returns early when the short entry '
        'price is missing. It would still yield None today only because '
        'float(None) raises into a catch-all.')
