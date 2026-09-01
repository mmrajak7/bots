"""The live-money bull-call book validates its own arithmetic, strictly.

THE GAP (found 2026-08-31). The Fallen Hero store checks ten cross-field
invariants; this one -- the book that holds real bull call spreads -- checked
leg types and lot arithmetic and nothing else.

So a hand-captured trade with `spread_width: 5` typed for 50 saved cleanly.
At close, `bound_bcs_exit` clamps a real `exit_spread` of 30 down to 5 and
re-derives `pnl_per_share` and `total_pnl` from the clamp. The result is booked
"approximate" -- a marker that says the number is imprecise when it is in fact
WRONG, in an unknown direction. That is the inverse of the guard's purpose.

STRICT BY DECISION (owner, 2026-09-01: *"if in doubt ... do strict to one"*).
Every mismatch raises. These records are captured by hand from a fill, so a
disagreement between the strikes in the SYMBOLS and the numbers typed beside
them is a typo, and the cheapest moment to catch it is before it becomes a
position managed with wrong levels. `add_trade` already promises to raise on
bad input; this extends that promise from the fields that decide bookkeeping
to the fields that decide money.

Verified against both real historical BCS records (ICICIBANK 1360/1410 and
NHPC 80/86): each still saves.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_add_trade_cross_fields.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs.trade_store import _check_cross_fields as check          # noqa: E402

#: The ICICIBANK 1360/1410 trade exactly as the book holds it. The entry that
#: went +190%, and the model this validator must never reject.
REAL = {
    'stock': 'ICICIBANK',
    'long_symbol': 'ICICIBANK26FEB1360CE',
    'short_symbol': 'ICICIBANK26FEB1410CE',
    'spread_width': 50, 'net_debit': 13.55,
    'entry_long_price': 21.20, 'entry_short_price': 7.65,
    'entry_spot': 1360.0, 'sl_spot': 1319.0, 'target_spot': 1435.0,
}


def _with(**patch):
    d = dict(REAL)
    d.update(patch)
    return d


def test_the_real_record_is_accepted():
    """The negative control, and the one that matters: a validator that
    rejects the book's own best trade is worse than none."""
    check(dict(REAL))


# ── the defect that motivated this ─────────────────────────────────────────

def test_a_typod_spread_width_is_refused():
    """THE DEFECT. `spread_width: 5` for 50 later makes `bound_bcs_exit` clamp
    a real exit of 30 down to 5 and book a confidently wrong P&L."""
    with pytest.raises(ValueError) as e:
        check(_with(spread_width=5))
    assert 'spread_width' in str(e.value)
    assert '1410' in str(e.value) and '1360' in str(e.value), (
        'the message must show the arithmetic so the typo is obvious')


def test_the_symbols_are_the_source_of_truth():
    """A width that matches the typed strikes but not the SYMBOLS is still
    wrong -- the symbol is what the exchange trades."""
    with pytest.raises(ValueError):
        check(_with(spread_width=45))


# ── direction ──────────────────────────────────────────────────────────────

def test_swapped_legs_are_refused():
    """Long the higher strike is not a bull call spread, and every stop and
    target would run backwards."""
    with pytest.raises(ValueError) as e:
        check(_with(long_symbol='ICICIBANK26FEB1410CE',
                    short_symbol='ICICIBANK26FEB1360CE'))
    assert 'not a bull call spread' in str(e.value)


def test_identical_strikes_are_refused():
    with pytest.raises(ValueError):
        check(_with(short_symbol='ICICIBANK26FEB1360CE', spread_width=0))


# ── the money arithmetic ───────────────────────────────────────────────────

def test_a_net_debit_that_disagrees_with_the_fills_is_refused():
    with pytest.raises(ValueError) as e:
        check(_with(net_debit=11.00))
    assert 'net_debit' in str(e.value)


def test_paisa_rounding_is_tolerated():
    """A hand-typed fill carries rounding. The tolerance must admit that and
    still reject a transposed digit."""
    check(_with(net_debit=13.56))
    with pytest.raises(ValueError):
        check(_with(net_debit=13.85))


def test_a_zero_or_negative_debit_is_refused():
    for bad in (0, -1.0):
        with pytest.raises(ValueError):
            check(_with(net_debit=bad, entry_long_price=7.65 + bad,
                        entry_short_price=7.65))


def test_a_debit_at_or_above_the_width_is_refused():
    """A structure that cannot make money at any price."""
    with pytest.raises(ValueError) as e:
        check(_with(net_debit=50.0, entry_long_price=57.65,
                    entry_short_price=7.65))
    assert 'cannot make money' in str(e.value)


# ── the stops ──────────────────────────────────────────────────────────────

def test_an_sl_spot_above_entry_is_refused():
    """It would fire on the first poll of a perfectly healthy position."""
    with pytest.raises(ValueError) as e:
        check(_with(sl_spot=1400.0))
    assert 'sl_spot' in str(e.value)


def test_a_target_at_or_below_entry_is_refused():
    with pytest.raises(ValueError) as e:
        check(_with(target_spot=1300.0))
    assert 'target_spot' in str(e.value)


# ── it must not become a reason a real capture fails ───────────────────────

def test_missing_optional_fields_do_not_raise():
    """Only what is PRESENT is checked. A capture that omits `target_spot`
    must still save -- the validator refuses contradictions, not gaps."""
    check({'long_symbol': 'ICICIBANK26FEB1360CE',
           'short_symbol': 'ICICIBANK26FEB1410CE'})
    check({'net_debit': 13.55, 'entry_long_price': 21.20,
           'entry_short_price': 7.65})
    check({})


def test_an_unparseable_symbol_does_not_raise():
    """`option_symbols.strike` returns None on anything unexpected, and the
    caller treats that as "cannot check" -- never as a failure."""
    check(_with(long_symbol='WEIRD', short_symbol='ALSOWEIRD'))


def test_a_non_numeric_field_says_which_one():
    with pytest.raises(ValueError) as e:
        check(_with(net_debit='thirteen'))
    assert 'net_debit' in str(e.value)


def test_every_real_record_in_the_book_still_validates():
    """Run against the live file, not a fixture. A validator that would have
    rejected a trade the book already holds is one that will reject the next
    real one too.

    RETIRES WHEN: the BCS book is rebuilt from a schema that enforces these
    relationships at write time, so they cannot be captured inconsistently.
    """
    import io
    import json
    p = HELPER / 'logs' / 'bcs_trades.json'
    if not p.exists():
        pytest.skip('no local BCS book')
    for t in json.load(io.open(str(p), encoding='utf-8')):
        check(dict(t))
