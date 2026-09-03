"""The swing-TP / short-strike mismatch, measured but NOT acted on.

THE MISMATCH (found 2026-09-02, confirmed in code). `_build_bcs` picks the
short strike against `trade['st_value']`; the swing shortening runs afterwards
and moves ONLY the TP. So on a swing-shortened signal the short leg sits at a
price the position is never allowed to reach.

    WAAREEENER #449  short 2500, TP 2561 (swing)  -> exited 63 pts short of its
                                                    own short leg, +19.7%
    LICHSGFIN  #439  short 480,  TP 488  (swing)  -> +14.4%
    the 9 st_line TPs, target == strike           -> median +44.5%

`_swing_target_shadow` quotes the alternative pair and stores it. It must never
change what is traded: a narrower spread reads a HIGHER d/w, so the "obvious"
fix interacts with the gates, and three swing closes cannot settle that.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_swing_target_shadow.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import monitor                  # noqa: E402


ANALYSIS = {'expiry': '2026-09-29', 'lot_size': 100, 'atm_strike': 100.0,
            'atm_quote': {'mid': 6.0, 'bid': 5.9, 'ask': 6.1, 'oi': 20000,
                          'reliable': True}}
TRADE = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 120.0,
         'timeframe': 'weekly'}
TRADED = {'short_strike': 120.0, 'debit_to_width_pct': 40.0}
SWING = {'applied': True, 'tp_spot': 110.0, 'kind': 'swing_high'}


def _alt(monkeypatch, result):
    seen = {}

    def fake(kite, stock, direction, spot, *, target_spot, **kw):
        seen['target_spot'] = target_spot
        return dict(result)

    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs', fake)
    return seen


def test_the_shadow_is_quoted_against_the_SHORTENED_target(monkeypatch):
    """The whole point: the alternative pair is priced to the swing level, not
    to the ST line the traded pair used."""
    seen = _alt(monkeypatch, {'short_strike': 110.0, 'width': 10.0,
                              'debit': 4.0, 'debit_to_width_pct': 40.0})
    out = monitor._swing_target_shadow(None, TRADE, ANALYSIS, 100.0, TRADED, SWING)
    assert seen['target_spot'] == 110.0, (
        'the shadow was priced to %r, not the swing target — it is measuring '
        'the same construction twice' % seen.get('target_spot'))
    assert out['short_strike'] == 110.0
    assert out['same_strike'] is False


def test_it_records_when_the_two_constructions_agree(monkeypatch):
    """Most signals have no swing, and some swings land on the same strike.
    Those rows are the control group and must be distinguishable."""
    _alt(monkeypatch, {'short_strike': 120.0, 'width': 20.0, 'debit': 8.0})
    out = monitor._swing_target_shadow(None, TRADE, ANALYSIS, 100.0, TRADED, SWING)
    assert out['same_strike'] is True


def test_a_rejected_alternative_is_recorded_not_raised(monkeypatch):
    """A narrower spread reads a HIGHER d/w and can fail the gates. That is a
    RESULT — it is the argument against the 'obvious' fix — so it must be
    stored, not swallowed."""
    _alt(monkeypatch, {'error': 'debit 5 (fill) is 50.0% of width 10'})
    out = monitor._swing_target_shadow(None, TRADE, ANALYSIS, 100.0, TRADED, SWING)
    assert 'error' in out and out['target_spot'] == 110.0


def test_a_broken_shadow_never_breaks_the_cycle(monkeypatch):
    """One bad chain must not stop the other positions being monitored."""
    def boom(*a, **k):
        raise RuntimeError('chain unavailable')
    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs', boom)
    assert monitor._swing_target_shadow(
        None, TRADE, ANALYSIS, 100.0, TRADED, SWING) is None


@pytest.mark.parametrize('swing', [
    {'applied': True, 'tp_spot': None},
    {'applied': True},
])
def test_a_missing_target_is_not_guessed(monkeypatch, swing):
    _alt(monkeypatch, {'short_strike': 110.0})
    assert monitor._swing_target_shadow(
        None, TRADE, ANALYSIS, 100.0, TRADED, swing) is None


def test_the_shadow_never_becomes_the_traded_pair(monkeypatch):
    """THE SAFETY PROPERTY. If this ever fails, a measurement has been wired
    into the order path without the evidence to justify it."""
    _alt(monkeypatch, {'short_strike': 110.0, 'width': 10.0, 'debit': 4.0,
                       'short_symbol': 'SHADOWCE'})
    traded = dict(TRADED)
    out = monitor._swing_target_shadow(None, TRADE, ANALYSIS, 100.0, traded, SWING)
    assert traded == TRADED, 'the shadow mutated the pair being traded'
    assert out['short_strike'] != traded['short_strike']
