"""Does the trail even FIRE? The question the first draft got backwards.

`outcome_if_trailed` decides, from a closed record, whether a gain-anchored
trail would have triggered. The peak is by definition the maximum over the
whole life, so the final value is at or after it — which makes "did it fire"
answerable from two numbers: the trail fires **iff the trade ended at or below
the level**, because ending below is the proof it crossed.

The first version had that comparison the other way round. It reported that
trailing cost money at every engage fraction, "hurting" precisely the two
trades that ran straight to TP and never retraced at all. Clean, decisive, and
backwards — the kind of result that gets acted on because it agrees with the
prior (`zebra_trail_never_arms` says the trail is not worth lowering).

Run:  cd Helper && python -m pytest zebra/tests/test_trail_study.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra.trail_study import outcome_if_trailed, rows_for   # noqa: E402


def bcs(peak, exit_debit, debit=10.0, width=50.0, qty=100):
    """A closed BCS. P&L is derived from exit_debit so the fixture cannot
    quietly disagree with itself."""
    return {'id': 1, 'stock': 'TESTCO', 'status': 'exited', 'structure': 'bcs',
            'width': width, 'debit': debit, 'quantity': qty,
            'mfe_mid': peak, 'exit_debit': exit_debit,
            'pnl': (exit_debit - debit) * qty}


# max_gain = 40. engage 0.5 -> arms at peak_gain >= 20, i.e. mfe_mid >= 30.
# retain 0.5 -> level = 10 + 0.5 * peak_gain.

def test_a_run_straight_to_target_is_never_touched_by_the_trail():
    """THE regression. Peak == exit: the trade never gave anything back, so
    the level was never crossed and the trail is irrelevant to it.

    The inverted version booked this at the level — half the gain — and called
    the difference a cost of trailing.
    """
    t = bcs(peak=50.0, exit_debit=50.0)
    armed, fired, trailed, actual = outcome_if_trailed(t, 0.5, 0.5)
    assert armed is True, 'peak gain 40 of a 40 max gain must arm'
    assert fired is False, 'armed is not fired — nothing ever retraced'
    assert trailed == actual == pytest.approx(4000.0), (
        'the trail cut a run it never touched')


def test_a_giveback_after_the_peak_is_caught_by_the_trail():
    """The case the trail exists for: peaked at 40.0, ended at 12.0. The level
    sits at 10 + 0.5*30 = 25.0, and ending below it proves it was crossed."""
    t = bcs(peak=40.0, exit_debit=12.0)
    armed, fired, trailed, actual = outcome_if_trailed(t, 0.5, 0.5)
    assert armed is True
    assert actual == pytest.approx(200.0)
    assert trailed == pytest.approx(1500.0), 'the trail should have saved this'
    assert trailed > actual


def test_an_exit_exactly_at_the_level_counts_as_fired():
    """Boundary. `mid <= level` is the monitor's own comparison, so a study
    using `<` would disagree with the code it models.

    Only the FIRED flag can catch that: at the boundary both branches book the
    same rupees by construction, so a value assertion passes either way. A
    mutation run flipping `<=` to `<` SURVIVED an earlier version of this file
    that had nothing but the value to look at — which is why
    `outcome_if_trailed` now reports armed and fired separately.
    """
    t = bcs(peak=40.0, exit_debit=25.0)
    armed, fired, trailed, actual = outcome_if_trailed(t, 0.5, 0.5)
    assert armed is True and fired is True
    assert trailed == pytest.approx(actual)


def test_an_exit_one_tick_above_the_level_does_not_fire():
    """Negative control for the boundary above."""
    t = bcs(peak=40.0, exit_debit=25.05)
    armed, fired, trailed, actual = outcome_if_trailed(t, 0.5, 0.5)
    assert armed is True and fired is False
    assert trailed == pytest.approx(actual), 'fired without being crossed'


def test_a_peak_below_the_engage_threshold_never_arms():
    """Peak gain 10 against a 40 max gain is 25%, under the 50% engage."""
    t = bcs(peak=20.0, exit_debit=5.0)
    armed, fired, trailed, actual = outcome_if_trailed(t, 0.5, 0.5)
    assert armed is False
    assert trailed == actual == pytest.approx(-500.0), (
        'an unarmed trail changed the outcome')


def test_lowering_the_engage_fraction_arms_the_same_trade():
    """Negative control for the test above: same record, looser threshold."""
    t = bcs(peak=20.0, exit_debit=5.0)
    assert outcome_if_trailed(t, 0.20, 0.5)[0] is True


# ── The trail can only ever help, given how firing is determined ────────────

@pytest.mark.parametrize('peak,exit_debit', [
    (50.0, 50.0), (40.0, 12.0), (35.0, 34.0), (30.0, 2.0), (48.0, 10.0),
])
def test_a_fired_trail_never_books_worse_than_the_real_exit(peak, exit_debit):
    """A property, not an example. The trail fires only when the trade ended
    at or below the level, so booking AT the level is weakly better every
    time. If this ever goes red the firing condition has been inverted again —
    which is exactly how the first draft read as a decisive negative result.
    """
    armed, fired, trailed, actual = outcome_if_trailed(
        bcs(peak, exit_debit), 0.5, 0.5)
    assert trailed >= actual - 1e-9
    if not fired:
        assert trailed == pytest.approx(actual), (
            'a trail that never fired still changed the outcome')


# ── What the study refuses to answer ────────────────────────────────────────

def test_a_zebra_record_is_not_costed_as_a_vertical():
    """A back ratio has no capped payoff, so "fraction of max gain" is
    meaningless. `rows_for` filters on structure == 'bcs'; treating a zebra as
    a vertical is the error that would invert the whole answer."""
    z = dict(bcs(peak=40.0, exit_debit=12.0), structure='zebra')
    assert rows_for([z], 0.5, 0.5) == []


@pytest.mark.parametrize('missing', ['width', 'debit', 'mfe_mid', 'quantity',
                                     'pnl'])
def test_a_record_missing_what_it_needs_is_skipped_not_guessed(missing):
    """208 of the closed records predate MFE capture. Defaulting any of these
    to zero would fold a fabricated row into the total."""
    t = bcs(peak=40.0, exit_debit=12.0)
    t[missing] = None
    assert outcome_if_trailed(t, 0.5, 0.5) is None


def test_a_debit_at_or_above_the_width_has_no_max_gain():
    """Degenerate structure — max_gain <= 0 makes every fraction of it
    meaningless, including the engage threshold."""
    t = bcs(peak=40.0, exit_debit=12.0, debit=50.0, width=50.0)
    assert outcome_if_trailed(t, 0.5, 0.5) is None
