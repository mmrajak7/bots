"""One book, one fee calculator.

The cohort scorecard (`zebra status`) carried its own hardcoded copy of the
fee rates — `brok = 80.0`, `0.0003503 * turnover`, `0.001 * sell_turn`. Every
one of them matched `cfg.FEE_RATES` exactly, which is what made it dangerous
rather than merely redundant: nothing was wrong today, and nothing would look
wrong on the day it went stale.

The go-live plan reconciles `cfg.FEE_RATES` against the first real contract
note. After that the digest would move and the scorecard would not, quoting
April's published rates at whoever is reading the number to decide whether to
scale up. Of the two places, the scorecard is the worse one to leave stale.

Run:  cd Helper && python -m pytest zebra/tests/test_scorecard_fees.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                      # noqa: E402
from zebra.__main__ import _trade_cost               # noqa: E402


def _trade_fees(t):
    return _trade_cost(t)[0]


def _basis(t):
    return _trade_cost(t)[1]


def trade(**kw):
    """A closed BCS carrying everything `zebra.fees` needs to model it.

    The per-leg entry prices and both symbols are load-bearing, not padding:
    without them `round_trip_for_trade` degrades to its brokerage FLOOR, which
    is a constant. A first draft of this file omitted them and every test that
    tried to move a rate saw the same Rs 47.20 — the fixture, not the code,
    was the thing being measured.
    """
    base = dict(id=1, stock='TESTCO', structure='bcs', direction='CE',
                long_symbol='TESTCO26SEP1340CE',
                short_symbol='TESTCO26SEP1390CE',
                quantity=700, debit=13.55, exit_debit=20.00,
                long_ask_entry=21.20, short_bid_entry=7.65,
                short_extrinsic_entry=7.65, pnl=4515.0)
    base.update(kw)
    return base


# ── The stamped cost wins ───────────────────────────────────────────────────

def test_the_cost_stamped_at_close_is_used_verbatim():
    """`pnl_net` was computed at close from the REAL exit book. Re-deriving it
    from a model afterwards would throw away the only accurate number in the
    record."""
    t = trade(pnl=4515.0, pnl_net=4170.0)
    assert _trade_fees(t) == pytest.approx(345.0)


def test_the_stamped_cost_is_preferred_over_the_model():
    """Negative control for the test above: the modelled figure is a long way
    from 345, so a passing test cannot be an accident of the two agreeing."""
    t = trade(pnl=4515.0, pnl_net=4170.0)
    modelled = _trade_fees(trade())          # same trade, no stamp
    assert abs(modelled - 345.0) > 20.0, (
        f'the model happens to return {modelled}, so this file cannot tell '
        f'stamped from modelled — pick different numbers')
    assert _trade_fees(t) == pytest.approx(345.0)


def test_a_negative_stamped_cost_is_floored_at_zero():
    """`pnl_net` above `pnl` means a bad stamp, not a rebate. Costs are never
    negative, and a negative one would show up as INCOME in the scorecard."""
    assert _trade_fees(trade(pnl=100.0, pnl_net=250.0)) == 0.0


@pytest.mark.parametrize('bad', ['', None, 'n/a', float('nan')])
def test_a_malformed_stamp_falls_back_rather_than_crashing(bad):
    """The scorecard must not be takeable down by one bad record."""
    out = _trade_fees(trade(pnl_net=bad))
    assert out >= 0.0
    assert out == out, 'NaN leaked into the scorecard total'


# ── The fallback tracks cfg.FEE_RATES ───────────────────────────────────────

def test_an_unstamped_trade_is_costed_from_the_config(monkeypatch):
    """THE point of the change. Doubling the published brokerage must move the
    scorecard; with the old hardcoded `brok = 80.0` it did not.
    """
    before = _trade_fees(trade())
    assert before > 0, 'an unstamped trade came back free'

    rates = dict(cfg.FEE_RATES)
    rates['brokerage_per_order'] = float(rates['brokerage_per_order']) * 2
    monkeypatch.setattr(cfg, 'FEE_RATES', rates)

    after = _trade_fees(trade())
    assert after > before, (
        'the scorecard did not react to a change in cfg.FEE_RATES — it is '
        'costing the book from its own copy of the rates again')


def test_the_stt_rate_also_reaches_the_scorecard(monkeypatch):
    """A second rate, because brokerage is the one a re-hardcoded version
    would be most likely to read from config while inlining the rest."""
    before = _trade_fees(trade())
    rates = dict(cfg.FEE_RATES)
    rates['stt_sell_pct'] = float(rates['stt_sell_pct']) * 3
    monkeypatch.setattr(cfg, 'FEE_RATES', rates)
    assert _trade_fees(trade()) > before


def test_a_record_with_no_prices_is_charged_the_brokerage_FLOOR():
    """Not zero, and not a plausible-looking estimate either.

    A two-leg debit structure is four executed orders round trip whatever the
    book did, so brokerage is never in doubt even when the premiums are lost.
    `zebra.fees` charges that floor and labels the row `brokerage_only`,
    because STT — the largest single charge — cannot be recovered without the
    premiums. The scorecard prints that count and says the NET is better than
    reality, which is the only honest way to show a number that is missing its
    biggest component.
    """
    from zebra import config as _cfg
    floor_per_order = float(_cfg.FEE_RATES['brokerage_per_order'])
    gst = 1 + float(_cfg.FEE_RATES['gst_pct']) / 100.0

    cost, basis = _trade_cost({'id': 99})
    assert basis == 'brokerage_only'
    assert cost == pytest.approx(2 * floor_per_order * gst, rel=0.01)


def test_the_floor_grows_to_four_orders_once_both_symbols_are_known():
    """The order COUNT is knowable from the symbols even when prices are not,
    and undercounting it once reported a median below the fixed brokerage —
    an impossible number that flattered the cohort's net P&L."""
    from zebra import config as _cfg
    per = float(_cfg.FEE_RATES['brokerage_per_order'])
    gst = 1 + float(_cfg.FEE_RATES['gst_pct']) / 100.0
    cost, basis = _trade_cost({'id': 99, 'long_symbol': 'A26SEP1CE',
                               'short_symbol': 'A26SEP2CE'})
    assert basis == 'brokerage_only'
    assert cost == pytest.approx(4 * per * gst, rel=0.01)


def test_the_scorecard_holds_no_rate_of_its_own():
    """Anti-regression on the CODE, not the prose.

    A first draft grepped the whole module for the old literals and failed on
    its own docstring, which quotes them to explain why they were removed. It
    now walks the function's AST: any float constant that is not 0.0 is a rate
    someone inlined.
    """
    import ast
    import inspect
    import textwrap
    from zebra import __main__ as m

    tree = ast.parse(textwrap.dedent(inspect.getsource(m._trade_cost)))
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert all(c == 0.0 for c in consts), (
        f'_trade_cost carries its own numeric rates {consts}. Cost the book '
        f'through zebra.fees so one contract-note reconciliation moves every '
        f'number at once.')
