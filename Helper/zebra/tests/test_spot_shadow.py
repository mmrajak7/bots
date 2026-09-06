"""The adverse-spot stop, MEASURED and not armed.

The replay says a 1.5-2.0% adverse-spot stop is the best loss-side fix
available on the cohort: 0 of 12 winners cut, 5 of 5 losers caught, payoff
0.79 -> 1.44. The arithmetic underneath says that is not enough to act on —
one true stop saves ~25.6 points of debit, one false stop costs ~60.5, so the
rule needs ~70% firing accuracy merely to break even, and 5 of 5 gives a 95%
lower bound of 54.9%.

So this module counts, and `spot_sl_enabled` stays False. These tests are
mostly about what the shadow CANNOT do: gate, raise, or flatter itself.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_spot_shadow.py -v
"""
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import spot_shadow as ss             # noqa: E402

TS = '2026-09-04 10:00:00'


def trade(direction='CE', entry=100.0, **over):
    t = {'id': 1, 'stock': 'TESTCO', 'direction': direction, 'entry_spot': entry}
    t.update(over)
    return t


def step(t, spot, value=5.0, reliable=True, ts=TS):
    """Apply one observation to `t` in place and return the patch."""
    p = ss.observe(t, spot, value, reliable, ts)
    t.update(p)
    return p


# -- the sign is the whole module --------------------------------------------

def test_adverse_flips_with_direction():
    """CE loses as the underlying falls, PE as it rises. Get this backwards and
    the shadow is wrong in both directions at once, silently."""
    assert ss.adverse_pct(trade('CE'), 98.0) == 2.0
    assert ss.adverse_pct(trade('CE'), 102.0) == -2.0
    assert ss.adverse_pct(trade('PE'), 102.0) == 2.0
    assert ss.adverse_pct(trade('PE'), 98.0) == -2.0


def test_a_favourable_move_is_negative_adverse_and_breaches_nothing():
    t = trade('CE')
    step(t, 110.0)
    assert t['spot_shadow']['mae_pct'] == -10.0
    assert 'b1_5' not in t['spot_shadow']


# -- first breach only -------------------------------------------------------

def test_the_breach_is_recorded_ONCE_at_the_price_a_stop_would_have_got():
    """A stop fires once. Overwriting the record with a later, deeper print
    re-prices the counterfactual to something no stop would ever have got —
    which is how a rule flatters itself into looking worse than it is, or
    better, depending on which way the tape went."""
    t = trade('CE')
    step(t, 98.0, value=4.0, ts='2026-09-04 10:00:00')     # -2.0%, breaches 1.5
    first = dict(t['spot_shadow']['b1_5'])
    step(t, 90.0, value=1.0, ts='2026-09-04 11:00:00')     # much worse
    assert t['spot_shadow']['b1_5'] == first
    assert t['spot_shadow']['b1_5']['value'] == 4.0
    # ...but the deeper print IS the new MAE, and the deeper threshold arms.
    assert t['spot_shadow']['mae_pct'] == 10.0
    assert t['spot_shadow']['b3']['value'] == 1.0


def test_each_threshold_arms_independently():
    t = trade('CE')
    step(t, 98.25)                                          # -1.75%
    assert 'b1_5' in t['spot_shadow']
    assert 'b2' not in t['spot_shadow'] and 'b3' not in t['spot_shadow']


# -- a gap is not a stop -----------------------------------------------------

def test_a_breach_first_seen_at_the_session_open_is_flagged_as_a_GAP():
    """Two of the five cohort firings were gaps: the stop did not fire at its
    level, it fired wherever the gap landed. Counting those as clean firings is
    exactly how a stop flatters itself, so the flag travels with the breach."""
    t = trade('CE')
    step(t, 99.9, ts='2026-09-03 15:25:00')                 # day 1, no breach
    step(t, 99.8, ts='2026-09-03 15:29:00')
    step(t, 94.0, ts='2026-09-04 09:15:00')                 # day 2 FIRST poll
    assert t['spot_shadow']['b1_5']['gap'] is True


def test_an_intraday_breach_is_not_a_gap():
    t = trade('CE')
    step(t, 99.9, ts='2026-09-04 09:15:00')                 # first poll, quiet
    step(t, 94.0, ts='2026-09-04 11:00:00')                 # breaches later
    assert t['spot_shadow']['b1_5']['gap'] is False


def test_the_first_poll_a_position_EVER_sees_counts_as_a_session_open():
    """A position entered mid-session has its own first poll, and the honest
    answer for "was this a gap" on it is yes-as-far-as-we-know: the engine has
    no earlier observation to say the move happened while it was watching."""
    t = trade('CE')
    step(t, 94.0, ts='2026-09-04 13:00:00')
    assert t['spot_shadow']['b1_5']['gap'] is True


# -- an unusable book is RECORDED, not skipped -------------------------------

def test_a_breach_on_a_dark_book_is_still_recorded_and_says_so():
    """Refusing to record it would hide precisely the case where a real stop
    could not have booked either — the failure mode is invisible, not absent."""
    t = trade('CE')
    step(t, 94.0, value=None, reliable=False)
    b = t['spot_shadow']['b1_5']
    assert b['q'] == 'unusable' and b['value'] is None

    t2 = trade('CE')
    step(t2, 94.0, value=4.0, reliable=False)
    assert t2['spot_shadow']['b1_5']['q'] == 'unusable'


# -- it cannot break a cycle -------------------------------------------------

def test_no_spot_is_a_no_op_not_an_error():
    t = trade('CE')
    assert ss.observe(t, None, 5.0, True, TS) == {}


def test_a_record_with_no_entry_spot_returns_nothing():
    t = {'id': 1, 'direction': 'CE'}
    assert ss.observe(t, 94.0, 5.0, True, TS) == {}


def test_garbage_in_the_record_never_raises_into_the_cycle():
    """Measurement sitting in the exit path. A measurement that can throw is a
    new way to fail an exit."""
    for bad in ({'id': 1, 'direction': 'CE', 'entry_spot': 'oops'},
                {'id': 1, 'direction': 'CE', 'entry_spot': 0},
                {'id': 1, 'direction': 'CE', 'entry_spot': None},
                {'id': 1, 'direction': 'CE', 'entry_spot': 100.0,
                 'spot_shadow': 'not-a-dict'}):
        assert isinstance(ss.observe(bad, 94.0, 5.0, True, TS), dict)


def test_a_quiet_poll_returns_an_EMPTY_patch():
    """The caller folds this into one batched store write per cycle. A patch on
    every poll would rewrite the ~1 MB book every five minutes for nothing."""
    t = trade('CE')
    step(t, 94.0)
    assert ss.observe(t, 99.0, 5.0, True, TS) == {}


def test_it_never_returns_a_decision():
    """VETO-free, TRIGGER-free. The patch is data; there is no field in it a
    caller could mistake for an instruction to exit."""
    t = trade('CE')
    p = step(t, 90.0)
    assert set(p) == {'spot_shadow'}
    assert not any(k in p['spot_shadow'] for k in ('exit', 'action', 'trigger'))


# -- the reader --------------------------------------------------------------

def test_the_cli_counts_FIRINGS_and_separates_gaps(capsys, monkeypatch):
    """The denominator that matters is firings, not trades, and a GAP is a
    firing the rule did not earn — the stop booked wherever the gap landed
    rather than at its level. Two of the cohort's five were gaps."""
    from zebra import __main__ as cli

    def row(tid, stock, net, mae, breach):
        t = {'id': tid, 'stock': stock, 'direction': 'CE', 'status': 'exited',
             'pnl_net': net, 'pnl_net_pct': net / 100.0,
             'cohort': '2026-08-14',
             'spot_shadow': {'mae_pct': mae}}
        if breach:
            t['spot_shadow']['b1_5'] = breach
        return t

    trades = [
        row(1, 'LOSERA', -5000, 3.6, {'at': 'x', 'spot': 96.0,
                                      'adverse_pct': 3.6, 'value': 4.0,
                                      'q': 'ok', 'gap': True}),
        row(2, 'LOSERB', -4000, 2.1, {'at': 'x', 'spot': 97.0,
                                      'adverse_pct': 2.1, 'value': 5.0,
                                      'q': 'ok', 'gap': False}),
        row(3, 'WINNER', +3500, 0.4, None),
    ]

    class _S:
        def load_trades(self):
            return trades
    monkeypatch.setattr(cli, 'cmd_spotstop', cli.cmd_spotstop)
    import zebra.trade_store as ts
    monkeypatch.setattr(ts, 'get_store', lambda: _S())

    class A:
        all = False
    cli.cmd_spotstop(A())
    out = capsys.readouterr().out
    assert 'SHADOW ONLY' in out and 'spot_sl_enabled is False' in out
    # two firings, both on losers -> 100% accuracy, one of them a gap
    assert '100%' in out
    assert 'GAP' in out
    # and the bar it has to clear is stated beside the number
    assert '70%' in out
