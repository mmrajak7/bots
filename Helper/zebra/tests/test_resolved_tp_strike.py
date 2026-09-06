"""The short strike is picked against the target we will actually exit at.

## The mismatch this closes

`_build_bcs` called `analyze_bcs(target_spot=trade['st_value'])` — the ST line
— and the swing shortening ran AFTERWARDS, moving only the TP. So whenever a
swing level applied, the short strike sat at a price the position was never
allowed to reach:

    WAAREEENER #449  short 2500, TP at the 2561 swing, exited 63 points short
                     of its own short leg. 43.5% of width, +19.7%.
    LICHSGFIN  #439  the same shape, +14.4%.

Those are the two smallest wins in the cohort. The nine `st_line` TPs, where
target and strike agree, ran a median +44.5%.

## Why it SUPPRESSES rather than falling back

The narrower spread reads a HIGHER d/w and so faces the same gates on worse
terms. That is the point rather than a side effect: d/w is the market's own
quote for reaching that strike, and a spread priced to the target we actually
believe in is the only one whose gates mean anything.

Falling back to the ST-line pair would re-enter the exact structure this
exists to stop. Falling back to the ST-line TP would keep a target the chart
says has resistance in front of it. So neither is a fallback — a signal that
does not qualify honestly is not entered (`feedback_no_rush_to_enter`).

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
#: The pair built against the ST line: short out at 120, the full 20 wide.
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


def rebuild(monkeypatch, result, trade=None, st_pair=None, swing=None):
    """Run the rebuild with `analyze_bcs` stubbed to return `result`."""
    calls = []

    def fake(kite, stock, direction, price, target_spot, expiry, atm_strike,
             atm_quote, lot_size):
        calls.append(target_spot)
        if isinstance(result, Exception):
            raise result
        return dict(result)

    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs', fake)
    out = monitor._rebuild_against_resolved_tp(
        None, dict(trade or TRADE), ANALYSIS, 100.0,
        dict(st_pair or ST_PAIR), dict(swing or SWING))
    return out, calls


# -- it asks for the right target --------------------------------------------

def test_the_strike_is_repicked_against_the_SWING_target_not_the_ST_line(
        monkeypatch):
    out, calls = rebuild(monkeypatch, pair())
    assert calls == [110.0], (
        'the short strike was re-picked against %s, not the swing TP — this '
        'is the WAAREEENER construction' % calls)
    assert out['short_strike'] == 110.0
    assert out['width'] == 10.0


def test_the_traded_pair_IS_the_rebuilt_one(monkeypatch):
    """Not a shadow any more. The old `_swing_target_shadow` quoted exactly
    this pair and threw it away; the whole change is that it is now entered."""
    out, _ = rebuild(monkeypatch, pair())
    assert out['debit'] == 5.0 and out['debit_to_width_pct'] == 50.0


def test_the_expiry_and_entry_spot_are_carried_onto_the_rebuilt_pair(
        monkeypatch):
    """`_build_bcs` stamps these after `analyze_bcs` returns; the rebuild
    replaces that pair wholesale, so it has to stamp them too or the record
    enters with no expiry."""
    out, _ = rebuild(monkeypatch, pair())
    assert out['expiry'] == '2026-09-30'
    assert out['entry_spot'] == 100.0


# -- it suppresses rather than falling back ----------------------------------

def test_a_rejected_rebuild_SUPPRESSES_the_signal(monkeypatch, caplog):
    """The gates now see the spread priced to the achievable target. Failing
    them means the trade has no payoff left to the level we would exit at —
    which is a reason not to trade, not a reason to buy the wider one."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        out, _ = rebuild(monkeypatch, {'error': 'debit 9 (fill) is 90.0% of width'})
    assert out is None, 'the ST-line pair was entered as a fallback'
    assert 'RESOLVED-TP PAIR REJECTED' in caplog.text


def test_a_rebuild_that_RAISES_suppresses_rather_than_entering(monkeypatch,
                                                               caplog):
    """The old code path was a shadow, so an exception there was harmless and
    the docstring said 'never raises'. It is the TRADE now: the safe direction
    on an unknown error is no position (`feedback_no_rush_to_enter`)."""
    with caplog.at_level(logging.ERROR, logger='zebra.monitor'):
        out, _ = rebuild(monkeypatch, RuntimeError('chain fetch died'))
    assert out is None
    assert 'resolved-TP rebuild FAILED' in caplog.text


def test_it_never_raises_into_the_cycle(monkeypatch):
    """One bad chain must not stop the other positions being monitored."""
    out, _ = rebuild(monkeypatch, RuntimeError('boom'))
    assert out is None


def test_an_unparseable_swing_target_leaves_the_pair_alone(monkeypatch):
    """Nothing to re-pick against. The ST-line pair and the ST-line TP already
    agree in that case, so there is no mismatch to close."""
    out, calls = rebuild(monkeypatch, pair(),
                         swing=dict(SWING, tp_spot=None))
    assert out['short_strike'] == 120.0 and calls == []


# -- breakeven is re-checked against the NEW pair ----------------------------

def test_breakeven_is_rechecked_against_the_REBUILT_pair(monkeypatch, caplog):
    """The narrower spread has a smaller debit and therefore a NEARER
    breakeven, so this can only ever rescue a swing level the wider pair
    rejected. Re-run rather than assumed: the direction of that inequality
    stops being true the moment someone changes the strike rule."""
    # long 100 + debit 12 = breakeven 112, and the swing TP is 110.
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        out, _ = rebuild(monkeypatch, pair(debit=12.0))
    assert out is None, 'a pair that cannot make money at its own target entered'
    assert 'BELOW BREAKEVEN' in caplog.text


def test_a_pair_that_clears_its_own_breakeven_is_entered(monkeypatch):
    """The negative control for the test above."""
    out, _ = rebuild(monkeypatch, pair(debit=5.0))     # breakeven 105 < TP 110
    assert out is not None and out['short_strike'] == 110.0


# -- the shadow points the other way now -------------------------------------

def test_the_RETIRED_st_line_construction_is_stored_for_comparison(monkeypatch):
    """The two constructions still have to be comparable at exit; only the
    polarity flipped. The stored shape is unchanged so rows written either
    side of the switch read the same way."""
    out, _ = rebuild(monkeypatch, pair())
    shadow = out['swing_shadow']
    assert shadow['short_strike'] == 120.0, 'the shadow is not the ST-line pair'
    assert shadow['same_strike'] is False


def test_a_broken_shadow_never_stops_a_qualified_entry(monkeypatch):
    """Carried over from the retired `_swing_target_shadow` suite. The record
    is measurement; it must not be able to veto the trade it describes."""
    monkeypatch.setattr(monitor, '_swing_shadow_report',
                        lambda *a, **k: (_ for _ in ()).throw(ValueError('x')))
    out, _ = rebuild(monkeypatch, pair())
    assert out is not None and out['swing_shadow'] is None


def test_the_move_is_logged_loudly_enough_to_audit(monkeypatch, caplog):
    """A short strike that silently differs from the one the signal was built
    on reads as a bug to whoever is holding the position."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        rebuild(monkeypatch, pair())
    assert 'SHORT STRIKE MOVED' in caplog.text


def test_nothing_is_logged_as_MOVED_when_the_strike_is_unchanged(monkeypatch,
                                                                 caplog):
    """The nearest strike to the swing target is often the same one. Saying
    'MOVED 120 -> 120' would train the reader to skip the line."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        rebuild(monkeypatch, pair(short=120.0, width=20.0, debit=8.0))
    assert 'SHORT STRIKE MOVED' not in caplog.text


# -- the dead code is gone ---------------------------------------------------

def test_the_retired_shadow_function_no_longer_exists():
    """`feedback_dropped_but_still_wired`: a dropped construction that stays
    callable keeps deciding. The alternative pair IS the trade now."""
    assert not hasattr(monitor, '_swing_target_shadow')
