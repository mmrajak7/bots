"""The projected gain at TP knows where the TP is.

`proj_value_at_tp = k * width` is a pure function of d/w. It cannot see WHERE
in the spread the target sits, so it was systematically optimistic on exactly
the trades the gate exists to catch — the ones whose TP fires with the short
leg still OTM.

    V/w = d/w + pen * (k - d/w),   pen = (target - K_long)/width, clamped [0,1]

At pen=0 the spread is worth what you paid (spot has gone nowhere); at pen=1 it
is worth the calibrated k. So the projected gain is exactly `pen * (k/(d/w) -
1)` — the OLD formula scaled by penetration, and identical to it at pen=1,
which is where k was fitted.

Checked against the 12 cohort TPs, with the three post-close bookings re-marked
at their last executable poll: mean abs error 0.068 of width against 0.082 for
the flat model, bias -0.002 against +0.020. The overall gain is modest; the
improvement is CONCENTRATED where the gate needs it —

    LICHSGFIN  #439  pen 0.59   err +0.032   flat +0.092
    WAAREEENER #449  pen 0.39   err +0.002   flat +0.115

which are the two smallest wins in the book.

STILL MEASURED, NOT ENFORCED. This changes what the flag says, not what the
engine does.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_proj_gain_penetration.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg             # noqa: E402
from zebra import strikes                   # noqa: E402


K = cfg.BCS_TP_VALUE_FRAC_OF_WIDTH


def model(dw, pen, width=100.0):
    """The formula as the docstring states it, independent of the code."""
    return width * (dw + pen * (K - dw))


# -- the identity the fix rests on -------------------------------------------

def test_at_full_penetration_it_is_the_OLD_model_exactly():
    """k was fitted on TP-at-the-short-strike exits, i.e. pen=1. Anything else
    at pen=1 would silently re-label every historical row."""
    assert model(0.40, 1.0) == pytest.approx(K * 100.0)
    assert model(0.30, 1.0) == pytest.approx(K * 100.0)


def test_at_zero_penetration_the_spread_is_worth_what_you_paid():
    """Spot has gone nowhere, so the projected gain is 0% — not -100%, which
    is what a naive `k * pen * width` would say."""
    for dw in (0.30, 0.40, 0.45):
        assert model(dw, 0.0) == pytest.approx(dw * 100.0)
        gain = model(dw, 0.0) / (dw * 100.0) - 1
        assert gain == pytest.approx(0.0)


def test_the_projected_gain_is_the_old_formula_SCALED_by_penetration():
    """`pen * (k/(d/w) - 1)`. Asserted because it is the sentence the docstring
    makes, and an implementation that merely happened to agree at pen=1 and
    pen=0 could still diverge in between — which is the whole useful range."""
    for dw in (0.30, 0.36, 0.40, 0.45):
        for pen in (0.2, 0.39, 0.59, 0.8, 1.0):
            got = model(dw, pen) / (dw * 100.0) - 1
            assert got == pytest.approx(pen * (K / dw - 1))


def test_it_is_MONOTONIC_in_penetration():
    """A target further through the spread can never project a smaller gain."""
    vals = [model(0.40, p) for p in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert vals == sorted(vals)


# -- the cases it was built for ----------------------------------------------

@pytest.mark.parametrize('name,dw,pen,actual,flat_err', [
    # stock        d/w     pen   actual V/w   |flat model error|
    ('WAAREEENER', 0.364, 0.39, 0.435, 0.115),
    ('LICHSGFIN',  0.400, 0.59, 0.458, 0.092),
])
def test_it_beats_the_flat_model_on_the_two_smallest_wins(name, dw, pen,
                                                          actual, flat_err):
    """These are the trades the WAAREEENER construction produced, and the
    reason the gate could not see them coming."""
    pen_err = abs(model(dw, pen, 1.0) - actual)
    assert pen_err < flat_err, name
    assert abs(K - actual) == pytest.approx(flat_err, abs=0.002), (
        'the quoted flat-model error no longer matches the stored outcome')


# -- wiring ------------------------------------------------------------------

def test_analyze_bcs_reports_the_penetration_it_used():
    """`tp_penetration` is the THIRD input to the flag. k and the floor already
    travel with the row; without this one a stored `proj_gain_at_tp_pct` cannot
    be re-derived at all, because the term that scales k is missing — and every
    row written under the flat model would read as pen=1 whether it was or not.

    That it SURVIVES to the record is asserted against the REAL store in
    `test_measurement_persistence.py` — the same shape as the exit-bridge write
    path, which stayed broken because no test drove the real store.

    RETIRES WHEN: `analyze_bcs` can be driven end to end without a live option
    chain, at which point the projection is asserted from its RETURN VALUE
    rather than from its source.
    """
    import inspect
    assert "'tp_penetration'" in inspect.getsource(strikes.analyze_bcs), \
        'the penetration used is not stamped on the result'


def test_unknown_penetration_falls_back_to_the_FLAT_model_not_to_zero():
    """A 0 would read as a -100% projected gain and would_block every signal
    whose target could not be parsed: the loudest possible answer to the least
    informative input (`feedback_a_default_that_looks_like_a_value`).

    RETIRES WHEN: `analyze_bcs` is callable with a stubbed chain, so the
    fallback can be exercised by passing an unparseable target instead of read
    out of the source.
    """
    import inspect
    src = inspect.getsource(strikes.analyze_bcs)
    assert '1.0 if pen is None else pen' in src, \
        'an unparseable target no longer falls back to the flat model'


def test_it_is_still_measured_and_not_enforced():
    """The change is what the flag SAYS, not what the engine does. Enforcing it
    today would block most of the book — 3 of 19 cohort entries clear the
    floor — and that decision needs its own evidence.

    RETIRES WHEN: the gain-at-TP floor is DELIBERATELY enforced on cohort
    evidence, at which point this guard is inverted rather than deleted — the
    thing worth pinning becomes that it suppresses, and says why.
    """
    import inspect
    src = inspect.getsource(strikes.analyze_bcs)
    i = src.index('would_block_on_gain_at_tp =')
    tail = src[i:i + 400]
    assert 'return' not in tail.split('logger.info')[0], \
        'the gain-at-TP flag now suppresses signals — it is meant to measure'
