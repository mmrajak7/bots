"""Guards on the LIVE-money exit path.

This is the only code in the fleet that places real orders, and it has lost
money twice — both times at market open, both times on a price or an order
nobody could have justified:

  NHPC, Jul 2026   a garbage opening ask made a spread read 0.36 against an
                   entry of 1.41. The stop fired. -Rs 7,297, ~Rs 4,900 phantom.
  ICICI, Feb 2026  a monitor bug placed 4x BUY on the short leg and flipped the
                   position long. A +190% spread became +Rs 2K.

The 2026-07-24 response hardened the SPREAD triggers (reliability, debounce,
re-verify, open buffer). This file covers the four gaps a 2026-08-12 adversarial
review found still open after that work:

  1. no arbitrage floor  — the live monitor never asked whether a quoted value
                           was POSSIBLE, only whether the book looked well
                           formed. A tidy book can quote an impossible price.
  2. no independent check— reliability, debounce and re-verify all interrogate
                           the SAME order book, so identical stale reads
                           confirm one another. Spot is the only independent
                           source available.
  3. SL_SPOT single-poll — the one trigger exempt from every cooldown, running
                           at URGENT urgency whose final attempt pays through
                           uncapped, fired on ONE print.
  4. no post-close audit — nothing ever asked the broker whether the position
                           was actually flat. The ICICI bug was in the code
                           that placed the orders, so that code was never going
                           to catch it.

Run:  cd Helper && python -m pytest bcs/tests/test_exit_guards.py -v
"""
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as m          # noqa: E402


# ── the real NHPC trade, from the post-mortem in Helper/CLAUDE.md ────────
NHPC = {
    'id': 1, 'stock': 'NHPC',
    'long_symbol': 'NHPC26AUG80CE', 'short_symbol': 'NHPC26AUG86CE',
    'spot_symbol': 'NSE:NHPC', 'exchange': 'NFO',
    'quantity': 6900, 'lot_size': 6900,
    'entry_long_price': 2.05, 'entry_short_price': 0.64,
    'net_debit': 1.41, 'spread_width': 6,
    'entry_spot': 80.70, 'sl_spot': 78.28, 'sl_spread': 0.71,
    'target_spot': 86.0, 'expiry': '2026-08-27', 'status': 'open',
}


def _depth(bid, ask, bid_qty=1000, ask_qty=1000, ltp=0, fresh=False,
           prev_close=0, traded=False):
    return {'bid': bid, 'ask': ask, 'bid_qty': bid_qty, 'ask_qty': ask_qty,
            'ltp': ltp, 'ltp_fresh': fresh, 'prev_close': prev_close,
            'traded_today': traded}


# ── 1. strike recovery (the floor depends on it) ─────────────────────────
def test_strikes_are_recovered_from_the_tradingsymbol():
    """The BCS schema stores spread_width but NOT the strikes — verified
    against logs/bcs_trades.json. The floor has to parse them out."""
    assert m.strike_from_symbol('NHPC26AUG80CE') == 80.0
    assert m.strike_from_symbol('ICICIBANK26FEB1360CE') == 1360.0
    assert m.strike_from_symbol('POWERGRID26JAN252.5CE') == 252.5
    assert m.strike_from_symbol('NIFTY26AUG24000PE') == 24000.0


def test_an_unparseable_symbol_disables_the_floor_rather_than_guessing():
    """Fail-open: no strikes means no floor, never a fabricated one. A wrong
    floor would BLOCK a real stop, which is the error that costs money."""
    assert m.strike_from_symbol('WEIRD-SYMBOL') is None
    assert m.spread_intrinsic_floor(dict(NHPC, long_symbol='WEIRD'), 80.0) is None


# ── 2. the arbitrage floor (the ABB #242 signature) ──────────────────────
def test_a_value_below_intrinsic_is_refused_not_traded():
    """ABB #242, Jul 2026: a junk quote on an illiquid ITM leg booked a -50%
    stop at 335 when pure intrinsic was 1,020. Deep ITM here: spot 90 with
    strikes 80/86 means the spread is worth 6.00 at unwind; a book quoting
    2.00 is impossible, not unlucky."""
    floor = m.spread_intrinsic_floor(NHPC, spot=90.0)
    assert floor is not None and floor > 2.0

    class K:
        def quote(self, *a, **k):
            raise AssertionError('should not be reached')
    monkey = {'NHPC26AUG80CE': _depth(10.00, 10.10),
              'NHPC26AUG86CE': _depth(8.00, 8.10)}   # spread = 1.90, impossible
    orig = m.get_option_depth
    m.get_option_depth = lambda kite, exch, sym: monkey[sym]
    try:
        r = m.get_spread_value(None, NHPC, spot=90.0)
    finally:
        m.get_option_depth = orig
    assert r['spread'] is None, "an impossible price produced a tradeable value"
    assert 'below_intrinsic' in r['unreliable']


def test_the_floor_is_generous_enough_to_let_a_real_stop_through():
    """The floor must only ever fire on the impossible. At NHPC's real entry
    spot the structure legitimately trades near its debit, and a genuine
    adverse move must still be able to trigger the stop."""
    floor = m.spread_intrinsic_floor(NHPC, spot=80.70)
    assert floor is not None
    assert floor < NHPC['sl_spread'], (
        "floor %.2f would block the real sl_spread %.2f" % (floor, NHPC['sl_spread']))


def test_without_spot_the_floor_is_not_applied_at_all():
    """Backward compatible: existing callers that pass no spot behave exactly
    as before."""
    monkey = {'NHPC26AUG80CE': _depth(10.00, 10.10),
              'NHPC26AUG86CE': _depth(8.00, 8.10)}
    orig = m.get_option_depth
    m.get_option_depth = lambda kite, exch, sym: monkey[sym]
    try:
        r = m.get_spread_value(None, NHPC)
    finally:
        m.get_option_depth = orig
    assert r['spread'] == pytest.approx(1.90)


def test_the_reverify_quote_is_floor_checked_too(monkeypatch):
    """Re-verify takes the freshest, most decision-relevant quote in the whole
    flow — the last look before real orders. It was the one quote nobody
    floor-checked."""
    import datetime as _dt

    class _FakeDT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 12, 11, 30, 0)      # mid-session
    monkeypatch.setattr(m, 'datetime', _FakeDT)
    monkeypatch.setattr(m.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(m, 'send_telegram', lambda *a, **k: None)

    seen = {}

    def _fake_gsv(kite, trade, spot=None):
        seen['spot'] = spot
        return {'spread': 9.99, 'unreliable': None,
                'long': _depth(5.0, 5.1), 'short': _depth(0.1, 0.2)}
    monkeypatch.setattr(m, 'get_spread_value', _fake_gsv)

    r = m.close_spread(None, NHPC, spot=80.65, reason='SL_SPREAD',
                       dry_run=True, store=object(), reverify_sl=0.71)
    assert r == 'ABORT'                       # healed above the threshold
    assert seen['spot'] == 80.65, "re-verify quote was taken without spot, so " \
                                  "the arbitrage floor could not apply"


# ── 3. spot corroboration (the NHPC signature) ───────────────────────────
def test_a_collapse_with_a_flat_spot_is_vetoed():
    """The NHPC shape: the structure fell ~70% while spot barely moved. A
    vertical spread's value is monotonic in spot — this cannot happen."""
    st = {}
    ok, _ = m.spot_corroborates(st, spot=80.70, spread_val=1.41)
    assert ok and st['spread'] == 1.41          # first reading sets the baseline
    ok, why = m.spot_corroborates(st, spot=80.65, spread_val=0.42)
    assert not ok, "a 70% collapse on a 0.06% spot move was accepted"
    assert 'uncorroborated' in why


def test_a_collapse_the_underlying_explains_is_allowed_through():
    """Real losses must still be takeable. Spot down 3% justifies the move."""
    st = {}
    m.spot_corroborates(st, spot=80.70, spread_val=1.41)
    ok, _ = m.spot_corroborates(st, spot=78.20, spread_val=0.42)
    assert ok, "a corroborated real loss was blocked — that is how a capped " \
               "loss becomes a maximum loss"


def test_a_rejected_reading_never_becomes_the_baseline():
    """Otherwise one garbage print silently redefines 'normal', and the next
    real move gets measured against a lie."""
    st = {}
    m.spot_corroborates(st, spot=80.70, spread_val=1.41)
    m.spot_corroborates(st, spot=80.65, spread_val=0.42)      # vetoed
    assert st['spread'] == 1.41, "the vetoed value poisoned the reference"


def test_it_may_only_veto_never_validate():
    """With no reference (first poll, or after a restart) it must pass, not
    block: a restart may only DELAY a trigger, never fire one."""
    ok, _ = m.spot_corroborates({}, spot=80.0, spread_val=0.01)
    assert ok


def test_a_stale_reference_is_ignored():
    """Across a long gap the comparison is meaningless — don't veto on it."""
    st = {}
    m.spot_corroborates(st, spot=80.70, spread_val=1.41,
                        now=time.time() - m.CORROBORATION_STALE_SEC - 10)
    ok, _ = m.spot_corroborates(st, spot=80.65, spread_val=0.42)
    assert ok


def test_a_rise_is_never_vetoed():
    """Only collapses are suspicious. A jump is the trail's problem, and the
    trail has its own jump gate."""
    st = {}
    m.spot_corroborates(st, spot=80.70, spread_val=1.41)
    ok, _ = m.spot_corroborates(st, spot=80.71, spread_val=3.00)
    assert ok


# ── 4. SL_SPOT debounce ──────────────────────────────────────────────────
def test_sl_spot_needs_two_polls():
    """It stays exempt from every cooldown, but it is the one trigger running
    at URGENT urgency whose final attempt pays through uncapped."""
    assert m.SL_SPOT_CONFIRM_POLLS >= 2
    confirm = {'sl_spot': 0}
    assert m.bump_confirm(confirm, 'sl_spot') == 1      # would not fire
    assert m.bump_confirm(confirm, 'sl_spot') == 2      # fires


def test_a_recovery_breaks_the_sl_spot_streak():
    """Only contiguous hits count — same discipline as sl_spread/sl_trail."""
    confirm = {'sl_spot': 0}
    m.bump_confirm(confirm, 'sl_spot')
    confirm['sl_spot'] = 0                               # spot recovered
    assert m.bump_confirm(confirm, 'sl_spot') == 1


# ── 5. post-close reconciliation (the ICICI class) ───────────────────────
class _Kite:
    def __init__(self, net):
        self._net = net

    def positions(self):
        return {'net': [{'tradingsymbol': s, 'quantity': q}
                        for s, q in self._net.items()]}


def test_reconcile_passes_when_both_legs_are_flat():
    assert m.reconcile_after_close(_Kite({}), NHPC) is True


def test_reconcile_screams_when_a_leg_is_left_open(monkeypatch):
    """The exact ICICI shape: the short leg ends up net LONG after a close
    that reported success."""
    sent = []
    monkeypatch.setattr(m, 'send_telegram', lambda msg, **k: sent.append(msg))
    ok = m.reconcile_after_close(_Kite({'NHPC26AUG86CE': 6900}), NHPC)
    assert ok is False
    assert sent and 'NOT FLAT' in sent[0]
    assert 'NHPC26AUG86CE net +6900' in sent[0]


def test_reconcile_is_wired_into_the_success_path():
    """A guard nothing calls is not a guard.

    Structural rather than behavioural, deliberately: driving
    `_close_spread_inner` to its success path needs a full order/fill/store
    simulation, and this fleet has been bitten FOUR times by code that was
    present, correct, and never executed. The same technique guards FIFTY's
    scheduler parity (_research/filter_stretch/wiring_smoke_test.py). What it
    pins: the reconciliation must sit in the same block as the store update
    that marks the trade closed, so the two cannot drift apart.
    """
    import ast
    import inspect
    src = inspect.getsource(m._close_spread_inner)
    tree = ast.parse(''.join(src.splitlines(keepends=True)).lstrip())
    calls = {ast.unparse(n.func) if hasattr(ast, 'unparse') else ''
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'reconcile_after_close' in names or \
           'reconcile_after_close' in ' '.join(calls), \
        "the post-close position audit is not called from the close path"
    # ...and specifically after the trade is marked closed, not before.
    #
    # Scoped to the SUCCESS path. B11 (2026-08-24) added a second, EARLIER
    # reconcile call on the flipped-position abort — a path that deliberately
    # books no exit at all — so a naive `src.index()` compares the abort's
    # call against the success path's booking and fails on correct code. What
    # must hold is that a reconcile follows the exit booking, not that no
    # reconcile precedes it.
    booked = src.index('update_trade_exit')
    assert 'reconcile_after_close' in src[booked:], \
        "reconciliation must run AFTER the exit is booked, in the same block"


def test_reconcile_never_raises(monkeypatch):
    """It audits the close; it must never be able to break it."""
    class Boom:
        def positions(self):
            raise RuntimeError('kite down')
    monkeypatch.setattr(m, 'send_telegram', lambda *a, **k: None)
    assert m.reconcile_after_close(Boom(), NHPC) is False


# ── 6. the NHPC replay: end to end on the real numbers ───────────────────
def test_nhpc_replay_the_book_never_produces_a_tradeable_value():
    """The actual 09:18:24 book: 86CE bid 0.28 / ask 1.40, width 1.12 against
    a mid of 0.84 — 133% of mid. Whatever else changed, this book must never
    yield a number the monitor can act on."""
    monkey = {'NHPC26AUG80CE': _depth(1.68, 1.75),
              'NHPC26AUG86CE': _depth(0.28, 1.40)}
    orig = m.get_option_depth
    m.get_option_depth = lambda kite, exch, sym: monkey[sym]
    try:
        r = m.get_spread_value(None, NHPC, spot=80.65)
    finally:
        m.get_option_depth = orig
    assert r['spread'] is None
    assert 'wide_book' in r['unreliable']


def test_nhpc_replay_even_a_tidy_book_is_caught_by_corroboration():
    """The harder version, and the reason corroboration exists: suppose the
    book had been well formed and the value still collapsed with spot flat —
    reliability, debounce and re-verify all read the same book and would have
    agreed with each other. Spot is the only witness that would not."""
    st = {}
    m.spot_corroborates(st, spot=80.70, spread_val=1.41)
    monkey = {'NHPC26AUG80CE': _depth(0.70, 0.74),
              'NHPC26AUG86CE': _depth(0.30, 0.32)}      # tidy, spread 0.38
    orig = m.get_option_depth
    m.get_option_depth = lambda kite, exch, sym: monkey[sym]
    try:
        r = m.get_spread_value(None, NHPC, spot=80.65)
    finally:
        m.get_option_depth = orig
    assert r['spread'] is not None, "the book itself looks fine"
    ok, why = m.spot_corroborates(st, 80.65, r['spread'])
    assert not ok and 'uncorroborated' in why
