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

from zebra import config as cfg                             # noqa: E402
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


# ── the engage level sits on a cliff, and the cliff is the point ────────────

def test_the_trail_engage_cliff():
    """`trail_engage_frac` was moved 0.50 -> 0.25 on 2026-09-03 by replaying
    all 15 cohort closes against their real 5-minute value paths (9,449 POLL
    observations rebuilt from the complete Pi logs). This pins WHY 0.25, so the
    next person to nudge it has to argue with the replay rather than with
    intuition:

        engage 0.50 / 0.30  ->  Rs      0   never arms at all
        engage 0.25         ->  Rs +1,080   clips no winner
        engage 0.20         ->  Rs -4,400   clips KOTAKBANK, COFORGE
        engage 0.15         ->  Rs -7,537   clips 4 winners

    0.25 is the ONLY setting that helps, and it is one notch above a Rs -4,400
    outcome. Its entire benefit is a single position (COALINDIA #440, which
    gapped 7.85 -> 3.15 overnight), which is also why it is insensitive to
    `trail_retain_frac`.

    RETIRES WHEN: ~30 cohort closes exist and the replay is re-run; if the
    cliff has moved, this test should move with it and say so.
    """
    assert cfg.TRAIL_ENGAGE_FRAC == pytest.approx(0.25), (
        'trail_engage_frac is %r. Anything at or above 0.30 makes the trail '
        'decorative (it armed on 0 of 19 cohort positions at 0.50); anything '
        'at or below 0.20 cost money on the replay. Re-run the value-path '
        'replay before changing this.' % cfg.TRAIL_ENGAGE_FRAC)


def test_which_spreads_the_trail_can_physically_arm_on():
    """The structural reason 0.50 could never fire, as arithmetic so it cannot
    rot. The TP books at the short strike holding ~k of width, so the best peak
    a position can reach BEFORE its own TP takes it is

        peak_frac = (k - d/w) / (1 - d/w)

    which is decreasing in d/w: the more you pay, the less room there is above
    you. Solving peak_frac = engage gives the d/w above which the trail is
    unreachable no matter what the market does.

    At k=0.55, engage=0.25 that boundary is d/w = 0.40 EXACTLY — so the trail
    arms only on the cheap half of the permitted band (9 of the 19 cohort
    entries), and a spread at the 45% cap can never trail. That is coherent
    rather than a defect: an expensive spread has almost no room above it,
    which is the same fact `bcs_min_gain_at_tp_pct` is measuring. It is also
    the honest limit of the 0.25 change — it does not give every position a
    trail, it gives one to the cheap ones.
    """
    from zebra import mfe
    k, eng = cfg.BCS_TP_VALUE_FRAC_OF_WIDTH, cfg.TRAIL_ENGAGE_FRAC
    boundary = (k - eng) / (1 - eng)            # d/w where peak_frac == engage
    assert boundary == pytest.approx(0.40, abs=0.005), (
        'the arm/no-arm boundary moved to d/w %.3f. That changes which half of '
        'the book has a working trail; re-run the value-path replay.' % boundary)

    # Driven through `mfe.trail_levels`, not asserted against the test's own
    # arithmetic. An earlier version of this test computed `peak_frac` inline
    # and compared it to `eng` — it exercised ZERO production code and would
    # have passed with the trail deleted.
    WIDTH = 100.0
    for dw, reachable in ((0.36, True), (0.39, True), (0.42, False), (0.45, False)):
        debit = dw * WIDTH
        # the best the position can be worth before its own TP takes it
        peak_at_tp = k * WIDTH
        tl = mfe.trail_levels({'width': WIDTH, 'debit': debit,
                               'mfe_mid': peak_at_tp})
        assert tl['armed'] is reachable, (
            'at d/w %.2f a position peaks at %.1f%% of max gain before the TP '
            'fires; trail_levels says armed=%s, expected %s'
            % (dw, tl['peak_pct_of_max'], tl['armed'], reachable))


def test_lowering_the_engage_level_can_arm_an_ALREADY_OPEN_position():
    """PINNED AS A DECISION, because two reviewers read it opposite ways.

    `mfe.trail_levels` reads `cfg.TRAIL_ENGAGE_FRAC` at CALL time and re-derives
    `armed` from the stored peak — trail state is deliberately never persisted
    (`mfe.py`). So lowering the engage level applies RETROACTIVELY: on the first
    poll after a deploy, any open position whose peak already exceeded the new
    level arms, and if its value has since decayed past the retain line it can
    exit IMMEDIATELY, under a rule that did not exist when it was entered.

    That is in tension with the pricing box's principle that a basis "never
    changes under an open position — flipping a live trade would move its stop
    levels beneath it". It is accepted here because the book is PAPER and the
    engage move is 0.50 -> 0.25 (looser to tighter is the protective
    direction). It must be RE-CONSIDERED before real money: the honest options
    are to grandfather open positions on their entry-time engage level, or to
    make config moves market-closed-only.

    Verified against the four positions open on 2026-09-03 — peaks 11.5%, 0%,
    9.1%, 0.2% of max gain, all below 0.25 — so the deploy itself arms nothing.
    That was luck, not design, and is exactly why this is pinned.
    """
    from zebra import mfe
    # width 40 / debit 10 / max gain 30. Peak 20.0 = 33% of max gain: under the
    # OLD 0.50 engage this position was not armed; under 0.25 it is.
    pos = {'width': 40.0, 'debit': 10.0, 'mfe_mid': 20.0}
    tl = mfe.trail_levels(pos)
    assert tl['peak_pct_of_max'] == pytest.approx(33.3, abs=0.1)
    assert tl['armed'] is True, (
        'a position that was NOT armed under engage 0.50 must be understood to '
        'arm under 0.25 — retroactively, from its stored peak')
    # and its stop now sits above the entry debit, i.e. a level that did not
    # exist for this position yesterday
    assert tl['level'] == pytest.approx(15.0)   # 10 + 0.5 * 10
    assert tl['level'] > pos['debit']
