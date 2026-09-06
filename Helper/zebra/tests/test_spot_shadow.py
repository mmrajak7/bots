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

from zebra import config as cfg                 # noqa: E402
from zebra import spot_shadow as ss             # noqa: E402

TS = '2026-09-04 10:00:00'


def trade(direction='CE', entry=100.0, **over):
    t = {'id': 1, 'stock': 'TESTCO', 'direction': direction, 'entry_spot': entry}
    t.update(over)
    return t


def step(t, spot, value=5.0, usable=True, ts=TS, quality=''):
    """Apply one observation to `t` in place and return the patch."""
    p = ss.observe(t, spot, value, usable, ts, quality)
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


# -- first breach only, never re-priced --------------------------------------

def test_the_breach_is_recorded_ONCE_at_the_price_a_stop_would_have_got():
    """A stop fires once. Overwriting the record with a later, deeper print
    re-prices the counterfactual to something no stop would ever have got —
    which is how a rule flatters itself, in whichever direction the tape went."""
    t = trade('CE')
    step(t, 98.0, value=4.0, ts='2026-09-04 10:00:00')     # -2.0%, breaches 1.5
    first = dict(t['spot_shadow']['b1_5'])
    step(t, 90.0, value=1.0, ts='2026-09-04 11:00:00')     # much worse
    assert t['spot_shadow']['b1_5']['value'] == first['value'] == 4.0
    assert t['spot_shadow']['b1_5']['spot'] == first['spot'] == 98.0
    # ...but the deeper print IS the new MAE, and the deeper threshold arms.
    assert t['spot_shadow']['mae_pct'] == 10.0
    assert t['spot_shadow']['b3']['value'] == 1.0


def test_each_threshold_arms_independently():
    t = trade('CE')
    step(t, 98.25)                                          # -1.75%
    assert 'b1_5' in t['spot_shadow']
    assert 'b2' not in t['spot_shadow'] and 'b3' not in t['spot_shadow']


def test_the_threshold_is_inclusive_at_the_boundary():
    """`>=`, not `>`. Nothing else in this file exercises an exact hit, so an
    off-by-one there survives every other test."""
    t = trade('CE')
    step(t, 98.5)                                           # exactly -1.5%
    assert 'b1_5' in t['spot_shadow']
    t2 = trade('CE')
    step(t2, 98.51)                                         # just short of it
    assert 'b1_5' not in t2['spot_shadow']


# -- a gap is not a stop -----------------------------------------------------

def test_a_breach_at_the_session_open_is_flagged_as_a_GAP():
    """Two of the five cohort firings were gaps: the stop did not fire at its
    level, it fired wherever the gap landed. Counting those as clean firings is
    exactly how a stop flatters itself, so the flag travels with the breach."""
    t = trade('CE')
    step(t, 99.9, ts='2026-09-03 15:25:00')                 # day 1, no breach
    step(t, 94.0, ts='2026-09-04 09:15:00')                 # day 2, at the open
    assert t['spot_shadow']['b1_5']['gap'] is True


def test_an_intraday_breach_is_not_a_gap():
    t = trade('CE')
    step(t, 99.9, ts='2026-09-04 09:15:00')                 # first poll, quiet
    step(t, 94.0, ts='2026-09-04 11:00:00')                 # breaches later
    assert t['spot_shadow']['b1_5']['gap'] is False


def test_gap_is_derived_from_the_CLOCK_not_from_having_no_earlier_record():
    """The first cut asked "have we seen this trade before today", which is
    also true of a position entered mid-session, of the first poll after a
    deploy, and after any dropped write — three things that are not gaps. A
    13:00 breach on a position the shadow has never seen is NOT a gap."""
    t = trade('CE')
    step(t, 94.0, ts='2026-09-04 13:00:00')
    assert t['spot_shadow']['b1_5']['gap'] is False


def test_the_open_window_is_the_one_value_triggers_are_already_dark_for():
    """Reused rather than re-chosen: a breach the engine could not have acted
    on and a breach that arrived as a gap are the same fact from two sides."""
    assert ss._within_open_buffer('2026-09-04 09:15:00') is True
    assert ss._within_open_buffer('2026-09-04 09:29:00') is True
    assert ss._within_open_buffer('2026-09-04 09:31:00') is False
    assert cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC == 900


def test_an_unparseable_stamp_leaves_gap_UNKNOWN_not_False():
    """None must not read as "not a gap" — that is the direction that quietly
    promotes an unearned firing into a clean one."""
    t = trade('CE')
    step(t, 94.0, ts='not-a-timestamp')
    assert t['spot_shadow']['b1_5']['gap'] is None


# -- a price the engine would not have acted on ------------------------------

def test_a_clean_book_is_recorded_as_ok():
    """The positive control for `q`. Without it, a shadow that stamped every
    breach `unusable` would pass this whole file — and the reader would then
    exclude every firing it has."""
    t = trade('CE')
    step(t, 94.0, value=4.0, usable=True)
    assert t['spot_shadow']['b1_5']['q'] == 'ok'


def test_a_breach_on_a_dark_book_is_still_recorded_and_says_so():
    """Refusing to record it would hide precisely the case where a real stop
    could not have booked either — the failure mode becomes invisible, not
    absent."""
    t = trade('CE')
    step(t, 94.0, value=None, usable=False)
    b = t['spot_shadow']['b1_5']
    assert b['q'] == 'unusable' and b['value'] is None


def test_the_REASON_the_price_was_unusable_is_recorded():
    """`usable` is the caller's POST-GATE verdict, and it goes False for four
    different reasons — the closing print, the open buffer, the spot veto, and
    the VALUE BOUND clamp, which returns 0.0 or `width` on a book that reads
    perfectly reliable. Without the reason the count cannot be re-run
    excluding any one of them."""
    t = trade('CE')
    step(t, 94.0, value=0.0, usable=False, quality='value_bound')
    assert t['spot_shadow']['b1_5']['q'] == 'value_bound'


# -- a one-print stop is not a stop ------------------------------------------

def test_a_one_print_breach_is_not_marked_confirmed():
    """Every value trigger in this engine is debounced, and the standing rule
    is never to act on a single top-of-book quote. A count folding one-print
    firings in would authorise a rule nobody would ship."""
    t = trade('CE')
    step(t, 94.0, ts='2026-09-04 11:00:00')
    assert t['spot_shadow']['b1_5']['confirmed_at'] is None


def test_a_breach_that_still_holds_on_the_next_poll_IS_confirmed():
    t = trade('CE')
    step(t, 94.0, ts='2026-09-04 11:00:00')
    step(t, 93.0, ts='2026-09-04 11:05:00')
    assert t['spot_shadow']['b1_5']['confirmed_at'] == '2026-09-04 11:05:00'


def test_a_breach_that_reverses_before_the_next_poll_stays_unconfirmed():
    t = trade('CE')
    step(t, 94.0, ts='2026-09-04 11:00:00')
    step(t, 100.0, ts='2026-09-04 11:05:00')                # back to entry
    assert t['spot_shadow']['b1_5']['confirmed_at'] is None


def test_confirmation_never_re_prices_the_breach():
    """The confirm poll is a later, different price. Letting it overwrite
    `value` would re-price the counterfactual — the same mistake as re-marking
    the breach itself."""
    t = trade('CE')
    step(t, 94.0, value=4.0, ts='2026-09-04 11:00:00')
    step(t, 90.0, value=1.0, ts='2026-09-04 11:05:00')
    assert t['spot_shadow']['b1_5']['value'] == 4.0
    assert t['spot_shadow']['b1_5']['spot'] == 94.0


# -- provenance --------------------------------------------------------------

def test_it_records_WHEN_it_started_watching():
    """A record whose `since` is after its own entry has an unobserved head, so
    its MAE is a lower bound and its first breach may already have happened.
    The reader marks those PARTIAL rather than counting them."""
    t = trade('CE')
    step(t, 99.0, ts='2026-09-04 11:00:00')
    assert t['spot_shadow']['since'] == '2026-09-04 11:00:00'
    step(t, 94.0, ts='2026-09-04 12:00:00')
    assert t['spot_shadow']['since'] == '2026-09-04 11:00:00', \
        'since moved — it is the start of observation, not the last poll'


def test_the_field_is_allowed_on_the_batched_poll_write():
    """`apply_mfe` raises on an unknown key, and `_UNVERSIONED_FIELDS` derives
    from the same set — a field in one and not the other is what produced the
    false MERGE CONFLICT of 2026-09-01."""
    from zebra.trade_store import _BATCHED_POLL_FIELDS, _UNVERSIONED_FIELDS
    assert ss.FIELD in _BATCHED_POLL_FIELDS
    assert ss.FIELD in _UNVERSIONED_FIELDS


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
             'cohort': '2026-08-14', 'debit': 10.0, 'quantity': 100,
             'exit_debit': 5.0,
             'spot_shadow': {'mae_pct': mae, 'since': '2026-08-20 09:15:00'}}
        if breach:
            t['spot_shadow']['b1_5'] = breach
        return t

    trades = [
        row(1, 'LOSERA', -5000, 3.6, {'at': 'x', 'spot': 96.0,
                                      'adverse_pct': 3.6, 'value': 4.0,
                                      'q': 'ok', 'gap': True,
                                      'confirmed_at': 'y'}),
        row(2, 'LOSERB', -4000, 2.1, {'at': 'x', 'spot': 97.0,
                                      'adverse_pct': 2.1, 'value': 5.0,
                                      'q': 'ok', 'gap': False,
                                      'confirmed_at': 'y'}),
        row(3, 'WINNER', +3500, 0.4, None),
    ]

    class _S:
        def load_trades(self):
            return trades
    import zebra.trade_store as ts
    monkeypatch.setattr(ts, 'get_store', lambda: _S())

    class A:
        all = False
    cli.cmd_spotstop(A())
    out = capsys.readouterr().out
    assert 'SHADOW ONLY' in out and 'spot_sl_enabled is False' in out
    # Two firings, both on losers, ONE of them a gap. The summary row must say
    # so — asserting only that the word GAP appears somewhere would pass on the
    # per-row tag while the gap COLUMN stayed stuck at zero.
    summary = [l for l in out.splitlines() if l.strip().startswith('1.5')]
    assert summary, out
    # thr fire true FALSE acc ex-gap gaps 1-print missed net
    cells = summary[0].split()
    assert cells[1:4] == ['2', '2', '0'], (
        'firings miscounted: %s' % summary[0])
    assert cells[4] == '100%', 'accuracy miscounted: %s' % summary[0]
    assert cells[6] == '1', (
        'the gap COLUMN is not counting — asserting only that the word GAP '
        'appears somewhere would pass on the per-row tag while this stayed '
        'stuck at zero: %s' % summary[0])
    assert cells[8] == '0', 'missed-loser column wrong: %s' % summary[0]
    # Both firings are on losers, but stopping at 4.0/5.0 against a real exit
    # of 5.0 books WORSE on one of them. Outcome-labelling alone cannot see
    # that, which is the whole reason this column exists.
    assert cells[9] == '-100', 'net Rs is not measured from the stored '        'breach prices: %s' % summary[0]
