"""One floor, two engines — and the divergence that made it worth merging.

`bcs.spread_intrinsic_floor` and `zebra.monitor._intrinsic_floor` computed the
same no-arbitrage bound by different arithmetic. Arming is when a divergence
between the two engines becomes a live-money property, so the arithmetic is
shared and the POLICY is not: the exit rules differ deliberately and are
measured, and `bcs/zebra_adapter.ZEBRA_EXIT_POLICY` carries them with the trade
for that reason.

Run:  cd Helper && python -m pytest common/tests/test_spread_valuation.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import spread_valuation as sv      # noqa: E402

#: The real ICICI record B17 was measured on: 1360/1410 CE, short sold at 7.65
#: with spot at 1360, so the short leg's premium is entirely extrinsic.
ICICI = {
    'long_symbol': 'ICICIBANK26FEB1360CE',
    'short_symbol': 'ICICIBANK26FEB1410CE',
    'entry_short_price': 7.65, 'entry_spot': 1360.0,
    'net_debit': 13.55, 'debit': 13.55, 'spread_width': 50.0,
}

#: A bear put spread: the LONG leg is the HIGHER strike.
BPS = {
    'long_symbol': 'TESTCO26SEP1400PE',
    'short_symbol': 'TESTCO26SEP1340PE',
    'entry_short_price': 8.0, 'entry_spot': 1400.0,
    'net_debit': 20.0, 'spread_width': 60.0,
}


# -- the arithmetic ----------------------------------------------------------

def test_a_deep_ITM_call_spread_has_a_large_positive_floor():
    """The ABB #242 shape. At spot 1450 the 1360/1410 spread is worth its full
    50, so a quote of 1.00 is proof of a broken book, not an unlucky price."""
    floor = sv.intrinsic_floor(ICICI, 1450.0)
    assert floor is not None and floor > 30.0
    assert floor < 50.0, 'the floor must stay BELOW true value, not at it'


def test_a_bear_put_spread_gets_PUT_arithmetic():
    """B21. Call arithmetic on a put spread returns 0 or negative for every
    spot, so the floor could never fire — inert on a whole live book, and
    inert exactly where the guard matters."""
    deep = sv.intrinsic_floor(BPS, 1300.0)      # both legs ITM, worth ~60
    assert deep is not None and deep > 40.0
    assert sv.intrinsic_floor(BPS, 1500.0) == 0.0   # both OTM


def test_the_direction_comes_from_the_SYMBOLS_not_a_label():
    """A `direction` field is bookkeeping; the symbol is the contract. A record
    mislabelled CE on a put spread would otherwise get call arithmetic and a
    floor that cannot fire."""
    mislabelled = dict(BPS, direction='CE')
    assert sv.intrinsic_floor(mislabelled, 1300.0) > 40.0


def test_strikes_and_direction_are_the_FALLBACK_for_zebra_records():
    """Some zebra rows carry strikes and a direction rather than symbols, and
    the floor must still work for them."""
    rec = {'long_strike': 1360.0, 'short_strike': 1410.0, 'direction': 'CE',
           'entry_short_price': 7.65, 'entry_spot': 1360.0, 'debit': 13.55}
    assert sv.intrinsic_floor(rec, 1450.0) > 30.0


def test_the_back_ratio_multiplier_is_an_ARGUMENT_not_a_guess():
    """Two long legs price differently. The shared module must not read it off
    the record, because the books name it differently and a reader that guessed
    would misprice the whole structure rather than fail."""
    one = sv.intrinsic_floor(ICICI, 1450.0, long_multiplier=1.0)
    two = sv.intrinsic_floor(ICICI, 1450.0, long_multiplier=2.0)
    assert two > one


# -- the allowance ladder ----------------------------------------------------

def test_a_stored_entry_extrinsic_wins():
    rec = dict(ICICI, short_extrinsic_entry=7.65)
    assert sv.intrinsic_floor(rec, 1450.0) == sv.intrinsic_floor(ICICI, 1450.0)


def test_the_derived_allowance_subtracts_the_shorts_intrinsic_at_entry():
    """A short sold ITM is not all extrinsic. Counting it as if it were makes
    the floor too GENEROUS, which only ever costs a missed catch."""
    itm_short = dict(ICICI, entry_spot=1450.0)   # short 1410 was 40 ITM
    assert sv.intrinsic_floor(itm_short, 1500.0) \
        > sv.intrinsic_floor(ICICI, 1500.0)


def test_no_basis_for_an_allowance_DISABLES_the_floor():
    """B17, and the whole reason the ladder ends in None.

    zebra fell back to `0.3 * debit`, measured at 4.07 against a true 7.65 on
    this very record. That is a TIGHTER floor than the truth, so a healthy book
    falls below it and every valuation is refused for the rest of the session
    — SL_SPREAD, SL_TRAIL and the trail all dark. No floor is a known gap; a
    wrong floor is a guard that refuses healthy books.
    """
    blind = {k: v for k, v in ICICI.items() if k != 'entry_short_price'}
    assert sv.intrinsic_floor(blind, 1450.0) is None


def test_the_old_zebra_fallback_would_have_been_tighter_than_the_truth():
    """The measurement, run rather than quoted. The floor built on
    `0.3 * debit` sits ABOVE the one built on the real premium, and a higher
    floor is the one that refuses more books.
    """
    true_floor = sv.intrinsic_floor(ICICI, 1400.0)
    faked = dict(ICICI, short_extrinsic_entry=0.3 * ICICI['debit'])
    assert sv.intrinsic_floor(faked, 1400.0) > true_floor


def test_a_missing_entry_spot_uses_the_whole_premium():
    """The premium is a strict UPPER bound on the extrinsic, so this is the
    generous reading — the right direction when ignorant."""
    no_spot = {k: v for k, v in ICICI.items() if k != 'entry_spot'}
    assert sv.intrinsic_floor(no_spot, 1450.0) is not None


# -- the bound, and the claim that did not survive ---------------------------

def test_the_floor_is_never_negative():
    """Kept because CLAUDE.md's bounds table says so and it costs nothing.

    NOT because it fixes anything: both engines clamp the VALUE to >= 0 before
    consulting the floor, so a floor at or below zero rejects nothing either
    way. That was the third bullet in this merge's justification and
    `test_the_intrinsic_floor_is_inert_below_the_long_strike_by_construction`
    refused it.
    """
    assert sv.intrinsic_floor(ICICI, 1000.0) == 0.0
    assert sv.intrinsic_floor(BPS, 1900.0) == 0.0


@pytest.mark.parametrize('rec', [
    {}, {'long_symbol': 'NOT-AN-OPTION', 'short_symbol': 'ALSO-NOT'},
    {'long_symbol': 'TESTCO26SEP1400PE', 'short_symbol': 'TESTCO26SEP1340CE'},
])
def test_an_unreadable_shape_returns_None_rather_than_guessing(rec):
    """Mixed legs are not a vertical, and a floor this module cannot price must
    stand down rather than invent a bound."""
    assert sv.intrinsic_floor(rec, 1400.0) is None


def test_it_never_raises():
    """A valuation guard that raises is a new way to fail an exit that has
    already been decided. It may refuse; it may never interfere."""
    for junk in (None, 'x', float('nan'), object()):
        assert sv.intrinsic_floor({'long_symbol': junk}, junk) is None


# -- both engines use it -----------------------------------------------------

def test_both_engines_delegate_to_this_module():
    """The point of the merge. Two engines computing one arithmetic two ways is
    a divergence that becomes a live-money property the moment exits arm.

    RETIRES WHEN: neither engine defines an intrinsic-floor function of its own
    at all, so there is nothing left that could stop delegating.
    """
    import inspect
    from bcs import spread_monitor as sm
    from zebra import monitor as zm
    for fn in (sm.spread_intrinsic_floor, zm._intrinsic_floor):
        assert 'spread_valuation.intrinsic_floor' in inspect.getsource(fn)


def test_the_two_engines_now_agree_on_a_cohort_record():
    """The property that actually matters, checked by running both.

    A cohort BCS record valued by the paper engine and by the order path must
    get the same floor, or an exit that fires in one is refused by the other —
    and the handover between them is the arming step.
    """
    from bcs import spread_monitor as sm
    from zebra import monitor as zm
    rec = dict(ICICI, structure='bcs', long_strike=1360.0, short_strike=1410.0,
               direction='CE')
    for spot in (1300.0, 1360.0, 1400.0, 1450.0, 1500.0):
        assert sm.spread_intrinsic_floor(rec, spot) == \
            zm._intrinsic_floor(rec, spot), spot


def test_the_TRIGGERS_are_deliberately_not_shared():
    """The exit rules differ on purpose and the differences are measured: the
    spot stop is OFF in the cohort (a 3% stop cut 31 of 78 winners for a Rs
    8.9L giveaway), the trail is gain-anchored rather than 2x debit, and the
    time stop counts sessions rather than firing on expiry day.

    Merging those would delete a stop somebody measured, so the adapter carries
    them with the TRADE instead.

    RETIRES WHEN: the exit policy becomes a value object the engines execute
    rather than a set of module constants either could redefine.
    """
    from bcs.zebra_adapter import ZEBRA_EXIT_POLICY
    assert ZEBRA_EXIT_POLICY['spot_sl_enabled'] is False
    assert ZEBRA_EXIT_POLICY['trail_policy'] == 'gain_anchored'
    assert ZEBRA_EXIT_POLICY['time_policy'] == 'sessions_before_expiry'
