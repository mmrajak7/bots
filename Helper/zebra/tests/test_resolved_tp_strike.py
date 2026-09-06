"""The short strike is chosen against the target we will actually exit at.

## The mismatch

`_build_bcs` asked `analyze_bcs` for the strike nearest the raw ST line, and
the swing shortening ran afterwards moving only `tp_spot`. So whenever a swing
level applied, the short strike sat at a price the position was never allowed
to reach:

    WAAREEENER #449  short 2500, TP at the 2561 swing, exited 63 points short
                     of its own short leg. 43.5% of width, +19.7%.
    LICHSGFIN  #439  the same shape, +14.4%.

Those are the two smallest wins in the cohort.

## Why it CHOOSES rather than suppressing

The first cut entered the re-picked pair and suppressed the signal when that
pair failed a gate. Two things are wrong with that:

* the re-picked pair is NARROWER, so it reads a higher d/w and lands on the 45%
  cap by construction — on the three swing signals in the book it would have
  suppressed two, both winners, carrying 39% of the cohort's gross;
* it is not clear the re-picked pair is even better. The TP fires on TOUCH with
  27-36 DTE left, so a nearer short leg carries MORE extrinsic at the exit and
  V/w rises less than d/w does. By the identity `(V/w)/(d/w) - 1`, narrowing
  can LOSE.

So both pairs are ranked on one yardstick and the better is entered. It can
never stop a trade that would otherwise have opened, which is what makes it
safe to ship on three closes.

## And it happens BEFORE capital and the vet

The first cut ran inside `_enter_as_bcs`, which is after `_capital_context`
has sized the pair and after the vetting agent has been shown it under a note
reading "this is the pair that would open". That is the #449 defect
re-introduced one door along. `test_history` and `test_review_tail` pin the
ordering; this file pins the choice.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_resolved_tp_strike.py -v
"""
import logging
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import monitor                  # noqa: E402


TRADE = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 120.0}
ANALYSIS = {'expiry': '2026-09-30', 'atm_strike': 100.0,
            'atm_quote': {'mid': 5.0}, 'lot_size': 100}
#: The ST-line pair: short out at 120, the full 20 wide, d/w 40%.
ST_PAIR = {'short_strike': 120.0, 'width': 20.0, 'debit': 8.0,
           'debit_to_width_pct': 40.0, 'long_strike': 100.0}
#: A swing level at 110 — the TP the position will actually be exited at.
SWING = {'applied': True, 'tp_spot': 110.0, 'kind': 'swing_high',
         'st_value': 120.0, 'timeframe': 'weekly', 'bars_ago': 3,
         'shortened_by_pct': 50.0, 'retained_pct': 50.0}


def pair(short=110.0, width=10.0, debit=5.0, **over):
    p = {'short_strike': short, 'width': width, 'debit': debit,
         'debit_to_width_pct': round(debit / width * 100, 1),
         'long_strike': 100.0, 'short_symbol': 'S', 'short_oi': 99999}
    p.update(over)
    return p


def pick(monkeypatch, result, st_pair=None, swing=None, trade=None):
    """Run the chooser with `analyze_bcs` stubbed to return `result`."""
    calls = []

    def fake(kite, stock, direction, price, target_spot, expiry, atm_strike,
             atm_quote, lot_size):
        calls.append(target_spot)
        if isinstance(result, Exception):
            raise result
        return dict(result)

    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs', fake)
    base = dict(st_pair or ST_PAIR)
    base['swing_tp'] = dict(swing or SWING)
    out = monitor._pick_pair_for_resolved_tp(
        None, dict(trade or TRADE), ANALYSIS, 100.0, base)
    return out, calls


# -- it asks for the right target --------------------------------------------

def test_the_alternative_is_priced_against_the_SWING_target(monkeypatch):
    out, calls = pick(monkeypatch, pair())
    assert calls == [110.0], (
        'the alternative was priced against %s, not the swing TP — this is '
        'the WAAREEENER construction' % calls)


def test_no_swing_means_no_second_quote_at_all(monkeypatch):
    """The swing applies on roughly one signal in twenty. Everything else must
    cost exactly what it cost before."""
    out, calls = pick(monkeypatch, pair(),
                      swing={'applied': False, 'tp_spot': None})
    assert calls == [] and out['short_strike'] == 120.0


def test_an_unparseable_swing_target_leaves_the_pair_alone(monkeypatch):
    out, calls = pick(monkeypatch, pair(), swing=dict(SWING, tp_spot=None))
    assert calls == [] and out['short_strike'] == 120.0


# -- the choice --------------------------------------------------------------

def test_the_better_projected_pair_is_the_one_entered(monkeypatch):
    """ST-line: d/w 40%, pen (110-100)/20 = 0.5 -> 0.5*(0.55/0.40-1) = +18.8%.
    Re-picked: d/w 50%, pen 1.0 -> 1.0*(0.55/0.50-1) = +10.0%.
    So on THESE numbers the ST-line pair wins despite the mismatch — which is
    the whole reason this is a comparison and not a rule."""
    out, _ = pick(monkeypatch, pair(debit=5.0))     # width 10, d/w 50%
    assert out['short_strike'] == 120.0, 'the narrower pair won on worse odds'


def test_the_repicked_pair_wins_when_it_actually_projects_higher(monkeypatch):
    """The negative control. A cheap narrow pair — d/w 30%, pen 1.0 ->
    +83.3% — beats the ST-line pair's +18.8%."""
    out, _ = pick(monkeypatch, pair(debit=3.0))     # width 10, d/w 30%
    assert out['short_strike'] == 110.0
    assert out['width'] == 10.0 and out['debit'] == 3.0


def test_a_tie_goes_to_the_STATUS_QUO(monkeypatch):
    """The projection is an estimate with a mean error of 0.068 of width.
    Moving a strike on a difference smaller than that is reading noise."""
    # Same d/w and same penetration => identical projections.
    out, _ = pick(monkeypatch, pair(short=110.0, width=10.0, debit=4.0),
                  st_pair=dict(ST_PAIR, debit=4.0, width=10.0,
                               short_strike=120.0, debit_to_width_pct=40.0))
    assert out['short_strike'] == 120.0


def test_the_ranking_uses_the_RESOLVED_target_for_BOTH_pairs(monkeypatch):
    """The ST-line pair's own `proj_gain_at_tp_pct` was computed against the ST
    LINE. Reusing it would compare a projection to the magnet against one to
    the swing — this very mismatch, moved into the comparison."""
    from zebra import strikes
    # pen against the ST line would be 1.0 and would make the ST pair look
    # better than it is; against the swing it is 0.5.
    _, g_stline_target, _ = strikes.project_at_tp(8.0, 20.0, 100.0, 120.0, 'CE')
    _, g_resolved, _ = strikes.project_at_tp(8.0, 20.0, 100.0, 110.0, 'CE')
    assert g_stline_target > g_resolved, 'the fixture no longer distinguishes'
    # d/w 34% at pen 1.0 projects +61.8%: beats the resolved-target reading of
    # the ST pair (+18.8%) but LOSES to its ST-line reading (+37.5%).
    out, _ = pick(monkeypatch, pair(debit=3.4))
    assert out['short_strike'] == 110.0, (
        'the ST-line pair was ranked on its projection to the ST line, not to '
        'the target the position will be exited at')


# -- it can never suppress ---------------------------------------------------

def test_a_REJECTED_alternative_keeps_the_ST_line_pair(monkeypatch, caplog):
    """The alternative failed ITS gates; the pair we hold passed its own.
    Refusing to trade because a DIFFERENT structure is unattractive would be a
    new rule, unmeasured, applied to a population of three."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        out, _ = pick(monkeypatch, {'error': 'debit 9 is 90.0% of width'})
    assert out is not None and out['short_strike'] == 120.0
    assert 'RESOLVED-TP PAIR REJECTED' in caplog.text


def test_an_alternative_that_RAISES_keeps_the_ST_line_pair(monkeypatch):
    """One bad chain must not stop the other positions being monitored, and
    must not cost a qualified signal its entry either."""
    out, _ = pick(monkeypatch, RuntimeError('chain fetch died'))
    assert out is not None and out['short_strike'] == 120.0


def test_an_alternative_below_ITS_OWN_breakeven_keeps_the_ST_line_pair(
        monkeypatch, caplog):
    """Breakeven is re-checked against the pair that would be entered. The
    narrower spread has a smaller debit and so a NEARER breakeven, so this can
    only rescue a level the wider pair rejected — re-run rather than assumed,
    because the direction of that inequality stops being true the moment
    someone changes the strike rule."""
    # long 100 + debit 12 = breakeven 112, and the swing TP is 110.
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        out, _ = pick(monkeypatch, pair(debit=12.0))
    assert out['short_strike'] == 120.0
    assert 'BELOW ITS OWN BREAKEVEN' in caplog.text


def test_it_returns_a_TRADEABLE_pair_in_every_branch(monkeypatch):
    """The property that makes this safe to ship on three closes: it cannot
    stop a trade that would otherwise have opened."""
    for result in (pair(), pair(debit=12.0), pair(debit=99.0),
                   {'error': 'nope'}, RuntimeError('boom')):
        out, _ = pick(monkeypatch, result)
        assert out is not None and out.get('short_strike')


# -- the pair that lost is kept ----------------------------------------------

def test_the_UNTRADED_pair_is_stored_for_comparison(monkeypatch):
    out, _ = pick(monkeypatch, pair(debit=3.0))     # re-picked wins
    shadow = out['swing_shadow']
    assert shadow['short_strike'] == 120.0, 'the shadow is not the ST-line pair'
    assert shadow['chosen'] == 'resolved_tp'
    assert shadow['same_strike'] is False


def test_the_shadow_records_which_construction_won_either_way(monkeypatch):
    out, _ = pick(monkeypatch, pair(debit=5.0))     # ST-line holds
    assert out['swing_shadow']['chosen'] == 'st_line'
    assert out['swing_shadow']['short_strike'] == 110.0


def test_the_shadow_keeps_the_two_targets_apart(monkeypatch):
    """`target_spot` is the yardstick the choice was made on, not a property of
    either pair; `st_value` is what the ST-line pair was BUILT against. Storing
    only one of them is how the field started mislabelling itself."""
    out, _ = pick(monkeypatch, pair(debit=3.0))
    assert out['swing_shadow']['target_spot'] == 110.0
    assert out['swing_shadow']['st_value'] == 120.0


def test_a_rejected_alternative_is_RECORDED_not_swallowed(monkeypatch):
    """Carried over from the retired `_swing_target_shadow` suite: a rejected
    alternative is a RESULT, and a result that is only logged cannot be scored
    at ~10 swing signals."""
    out, _ = pick(monkeypatch, {'error': 'debit too rich'})
    assert out['swing_shadow']['error'] == 'debit too rich'
    assert out['swing_shadow']['chosen'] == 'st_line'


def test_a_broken_shadow_never_stops_a_qualified_entry(monkeypatch):
    """Also carried over. The record is measurement; it must not be able to
    veto the trade it describes. The retired version had two `logger.info`
    calls and a `float(trade['st_value'])` OUTSIDE its own try, so a malformed
    record raised straight into the entry path from a SHADOW."""
    monkeypatch.setattr(monitor, '_swing_shadow_report',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('x')))
    out, _ = pick(monkeypatch, pair(debit=3.0))
    assert out is not None and out['swing_shadow'] is None


# -- audit trail -------------------------------------------------------------

def test_the_choice_is_logged_with_both_sides(monkeypatch, caplog):
    """A short strike that silently differs from the one the signal was built
    on reads as a bug to whoever is holding the position."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        pick(monkeypatch, pair(debit=3.0))
    assert 'RESOLVED-TP CHOICE' in caplog.text
    assert 're-picked WINS' in caplog.text
    assert 'TP SHORTENED' in caplog.text


def test_the_log_says_when_the_status_quo_held(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        pick(monkeypatch, pair(debit=5.0))
    assert 'ST-line holds' in caplog.text


def test_the_expiry_and_entry_spot_are_carried_onto_the_chosen_pair(monkeypatch):
    """`_build_bcs` stamps these after `analyze_bcs` returns; when the
    alternative wins it replaces that dict wholesale, so it has to stamp them
    too or the record enters with no expiry."""
    out, _ = pick(monkeypatch, pair(debit=3.0))
    assert out['expiry'] == '2026-09-30' and out['entry_spot'] == 100.0


# -- the dead code is gone ---------------------------------------------------

def test_the_retired_functions_no_longer_exist():
    """`feedback_dropped_but_still_wired`: a dropped construction that stays
    callable keeps deciding."""
    assert not hasattr(monitor, '_swing_target_shadow')
    assert not hasattr(monitor, '_rebuild_against_resolved_tp')
