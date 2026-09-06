"""The projected gain at TP knows where the TP is.

`proj_value_at_tp = k * width` is a pure function of d/w. It cannot see WHERE
in the spread the target sits, so it was systematically optimistic on exactly
the trades the gate exists to catch — the ones whose TP fires with the short
leg still OTM.

    V/w = d/w + pen * (k - d/w),   pen = (target - K_long)/width, clamped [0,1]

At pen=0 the spread is worth what you paid (spot has gone nowhere); at pen=1 it
is worth the calibrated k. So the projected gain is exactly `pen * (k/(d/w) -
1)` — the OLD formula scaled by penetration, and identical to it at pen=1.

Checked against the 12 cohort TPs: mean abs error 0.068 of width against 0.082
for the flat model, bias -0.002 against +0.020. The overall gain is modest; the
improvement is CONCENTRATED where the gate needs it —

    LICHSGFIN  #439  pen 0.59   err +0.032   flat +0.092
    WAAREEENER #449  pen 0.39   err +0.002   flat +0.115

which are the two smallest wins in the book.

## What k is fitted on, stated honestly

k = 0.55 is the mean `exit_debit / width` over those 12 TPs, whose penetrations
average ~0.9 and include two rows at 0.39 and 0.59. So k is NOT a clean pen=1
constant, and the model is scored in-sample on the rows it was fitted on with
one extra input. Both numbers above should be read as "not worse", not as
validation. Re-fitting k on the pen>=0.9 subset is the right move at ~30
closes; doing it now on nine rows would be trading one in-sample fit for a
smaller one.

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

# Deliberately NOT the ATM strike, so a mutation that reads spot where it
# should read the long strike cannot hide. `test_bcs_gates`'s harness uses
# spot == ATM, which is exactly why that suite missed it.
STOCK, EXPIRY, DIRECTION = 'TESTCO', '2026-08-25', 'CE'
SPOT, ATM, SHORT = 995.0, 1000.0, 1040.0    # width 40
LOT = 500


def _run(monkeypatch, target, atm_mid=30.0, tgt_mid=16.0, direction='CE'):
    """Drive the REAL `analyze_bcs` with a stubbed chain.

    A bull call spread's short strike is ABOVE the long; a bear put spread's
    is BELOW. Both are 40 wide with the long 5 away from spot, so `target`
    means the same thing in both and only the sign under test differs.
    """
    atm = ATM if direction == 'CE' else SHORT
    short = SHORT if direction == 'CE' else ATM
    monkeypatch.setattr(strikes, '_load_options_csv', lambda: None)
    monkeypatch.setattr(strikes, '_OPTIONS_CACHE', {STOCK: {EXPIRY: {
        atm: {direction: {'tradingsymbol': 'TESTCO26AUGA%s' % direction}},
        short: {direction: {'tradingsymbol': 'TESTCO26AUGB%s' % direction}},
    }}})
    monkeypatch.setattr(strikes, '_list_strikes', lambda *a, **k: [atm, short])

    def q(mid, oi):
        return {'mid': mid, 'bid': mid, 'ask': mid, 'oi': oi, 'reliable': True}
    monkeypatch.setattr(strikes, '_quote_option', lambda kite, sym: q(tgt_mid, 20000))
    spot = SPOT if direction == 'CE' else 1045.0
    return strikes.analyze_bcs(
        kite=None, stock=STOCK, direction=direction, spot=spot,
        target_spot=target, expiry=EXPIRY, atm_strike=atm,
        atm_quote=q(atm_mid, 20000), lot_size=LOT)


# -- the real function, at real penetrations ---------------------------------

def test_the_penetration_it_used_is_the_one_it_reports(monkeypatch):
    """debit 14 on width 40; target 1020 sits halfway between the strikes."""
    r = _run(monkeypatch, target=1020.0)
    assert r['debit'] == 14.0 and r['width'] == 40.0
    assert r['tp_penetration'] == 0.5


def test_at_full_penetration_it_is_the_OLD_flat_model_exactly(monkeypatch):
    """k was fitted on TP-at-the-short-strike exits. Anything else at pen=1
    would silently re-label every historical row."""
    r = _run(monkeypatch, target=SHORT)
    assert r['tp_penetration'] == 1.0
    assert r['proj_value_at_tp'] == pytest.approx(K * 40.0, abs=0.01)


def test_a_target_BEYOND_the_short_strike_clamps_to_one(monkeypatch):
    """Spot past the short strike cannot make the spread worth more than a
    vertical's ceiling, and an unclamped pen would say it does."""
    r = _run(monkeypatch, target=1100.0)
    assert r['tp_penetration'] == 1.0
    assert r['proj_value_at_tp'] == pytest.approx(K * 40.0, abs=0.01)


def test_a_target_AT_the_long_strike_projects_a_zero_gain(monkeypatch):
    """Spot has gone nowhere, so the spread is worth what you paid — not
    -100%, which is what a naive `k * pen * width` would say."""
    r = _run(monkeypatch, target=ATM)
    assert r['tp_penetration'] == 0.0
    assert r['proj_value_at_tp'] == pytest.approx(r['debit'], abs=0.01)
    assert r['proj_gain_at_tp_pct'] == pytest.approx(0.0, abs=0.1)


def test_the_projection_is_MONOTONIC_in_the_target(monkeypatch):
    vals = [_run(monkeypatch, target=t)['proj_value_at_tp']
            for t in (1000.0, 1010.0, 1020.0, 1030.0, 1040.0)]
    assert vals == sorted(vals)
    assert vals[0] < vals[-1], 'the projection does not move with the target'


def test_the_gain_is_the_old_formula_SCALED_by_penetration(monkeypatch):
    """`pen * (k/(d/w) - 1)`. Asserted on the real function at a FRACTIONAL
    penetration, because a model that merely agreed at pen=0 and pen=1 could
    still diverge across the whole useful range in between."""
    r = _run(monkeypatch, target=1020.0)
    dw = r['debit'] / r['width']
    assert r['proj_gain_at_tp_pct'] == pytest.approx(
        r['tp_penetration'] * (K / dw - 1) * 100, abs=0.2)


def test_it_reads_the_LONG_STRIKE_not_spot(monkeypatch):
    """Spot is 995 and the long strike is 1000. A target of 1020 is 0.5 of the
    way through the SPREAD and 0.556 of the way from spot; only one of those is
    the payoff."""
    assert _run(monkeypatch, target=1020.0)['tp_penetration'] == 0.5


def test_the_sign_flips_for_a_PE_spread(monkeypatch):
    """A bear put spread travels DOWN. Reading the CE sign here would report a
    negative penetration, clamp it to 0, and project every put spread at a 0%
    gain."""
    r = _run(monkeypatch, target=1020.0, direction='PE')
    assert 'error' not in r, r
    assert r['long_strike'] == SHORT and r['short_strike'] == ATM
    assert r['tp_penetration'] == 0.5


# -- the cases it was built for ----------------------------------------------

@pytest.mark.parametrize('name,dw,pen,actual,flat_err', [
    # stock        d/w     pen   actual V/w   |flat model error|
    ('WAAREEENER', 0.364, 0.39, 0.435, 0.115),
    ('LICHSGFIN',  0.400, 0.59, 0.458, 0.092),
])
def test_it_beats_the_flat_model_on_the_two_smallest_wins(name, dw, pen,
                                                          actual, flat_err):
    """The trades the ST-line construction produced, and the reason the flag
    could not see them coming. Arithmetic on stored outcomes, so it states the
    model rather than driving it — the tests above do the driving."""
    pen_err = abs((dw + pen * (K - dw)) - actual)
    assert pen_err < flat_err, name
    assert abs(K - actual) == pytest.approx(flat_err, abs=0.002), (
        'the quoted flat-model error no longer matches the stored outcome')


# -- it measures, it does not gate -------------------------------------------

def test_a_would_block_projection_is_still_returned_TRADEABLE(monkeypatch):
    """The change is what the flag SAYS, not what the engine does. Enforcing it
    today would block most of the book — 3 of 19 cohort entries clear the floor
    — and that decision needs its own evidence."""
    r = _run(monkeypatch, target=ATM)              # pen 0 => 0% projected gain
    assert r['would_block_on_gain_at_tp'] is True
    assert 'error' not in r, 'the gain-at-TP flag now suppresses signals'
    assert r['short_strike'] == SHORT


def test_all_three_inputs_to_the_flag_travel_with_it(monkeypatch):
    """k and the floor already travelled. `tp_penetration` is the third, and
    without it a stored `proj_gain_at_tp_pct` cannot be re-derived at all —
    every row written under the flat model would read as pen=1 whether it was
    or not. That it SURVIVES to the record is pinned against the REAL store in
    `test_measurement_persistence.py`."""
    r = _run(monkeypatch, target=1020.0)
    assert r['tp_value_frac_of_width_k'] == K
    assert r['min_gain_at_tp_pct_at_entry'] == cfg.BCS_MIN_GAIN_AT_TP_PCT
    assert r['tp_penetration'] == 0.5
