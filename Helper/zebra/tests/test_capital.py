"""Portfolio capital limits and position sizing (Phase 2).

Before this there was one portfolio control in the whole system —
`max_open_trades`, a COUNT — and nothing anywhere limited RUPEES. Measured on
the cohort, the book carried 7 concurrent positions holding Rs 81,291 with no
figure capping the total.

Owner, 2026-08-26: load Rs 2L initially, reserve some per trade, and as capital
grows to Rs 4L let it go to 2 lots automatically — "capital based risk and so
on + compounding" — with Rs 25,000 per trade and 1 lot for now. The vet must
see capital; liquidity may not support the size the budget allows; recommend
one lot at a time and verify entries afterwards. Neo charges no per-order
brokerage, so slicing into one-lot orders is free.

Those numbers are one scheme, and this file pins that they stay one scheme:

    Rs 2,00,000 / 8 slots = Rs 25,000 each = 12.5% of capital

Run:  cd Helper && python -m pytest zebra/tests/test_capital.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import capital                 # noqa: E402
from zebra import config as cfg           # noqa: E402


def lim(capital_=200000.0, max_open=8, max_per_stock=1, max_lots=1,
        max_trade=None, max_deployed=None, basis='base'):
    """Resolved limits for one scenario, stated instead of monkeypatched.

    `check`/`plan` take these as an argument precisely so a test says what it
    is testing — and so a real decision can be replayed later against the
    limits that were in force when it was made.
    """
    return capital.Limits(capital=capital_, basis=basis, max_open=max_open,
                          max_per_stock=max_per_stock, max_lots=max_lots,
                          max_trade=max_trade, max_deployed=max_deployed)


def pos(stock='A', debit=10.0, lot=100, lots=1, status='entered'):
    return {'stock': stock, 'debit': debit, 'lot_size': lot, 'lots': lots,
            'status': status}


def closed(pnl_net=None, **kw):
    t = pos(status='exited', **kw)
    if pnl_net is not None:
        t['pnl_net'] = pnl_net
    return t


# ── the owner's scheme, as one coherent thing ───────────────────────────────

def test_the_shipped_numbers_are_the_owners_numbers():
    """Rs 2L, 8 slots, Rs 25,000 each. If any of the three drifts, the other
    two silently stop meaning what they were chosen to mean."""
    L = capital.limits([])
    assert L.capital == 200000
    assert L.max_open == 8
    assert L.max_trade == 25000
    assert L.max_lots == 1


def test_the_per_trade_cap_is_exactly_one_slot():
    """8 x 12.5% = 100%. Stored as ratios so the whole scheme scales on ONE
    number; stored as three rupee figures they drift apart the first time
    capital moves, and nothing announces it."""
    L = capital.limits([])
    assert L.max_trade * L.max_open == pytest.approx(L.max_deployed)


# ── capital-driven sizing: the compounding the owner asked for ──────────────

def test_lots_step_with_capital():
    """'as the capital grows to say 4L then it can auto go for 2 lots and so
    on'. One lot per `capital_per_lot`, which keeps risk-per-trade a constant
    fraction of the account instead of a fixed rupee number."""
    assert capital.lots_for_capital(200000) == 1
    assert capital.lots_for_capital(400000) == 2
    assert capital.lots_for_capital(600000) == 3


def test_lots_floor_never_round_up():
    """Rs 3.9L is not Rs 4L. Rounding up sizes an account as though money it
    does not have were already there."""
    assert capital.lots_for_capital(399999) == 1


def test_lots_never_drop_below_one():
    """A book below one unit of capital should be refused by the RUPEE limits,
    with a reason — not silently sized to zero lots by the arithmetic."""
    assert capital.lots_for_capital(0) == 1
    assert capital.lots_for_capital(50000) == 1


def test_a_stray_zero_cannot_order_fifty_lots():
    assert capital.lots_for_capital(50000000) == cfg.MAX_LOTS_HARD


# ── compounding ─────────────────────────────────────────────────────────────

def test_compounding_is_off_until_it_is_armed():
    """Same alert-only-first discipline every other control here shipped
    with: a number that moves position size gets watched before it is
    believed."""
    assert cfg.COMPOUND is False
    book = [closed(pnl_net=50000)]
    cap, basis = capital.effective_capital(book)
    assert cap == 200000 and basis == 'base'


def test_compounding_adds_realised_pnl(monkeypatch):
    monkeypatch.setattr(cfg, 'COMPOUND', True)
    book = [closed(pnl_net=150000), closed(pnl_net=50000)]
    cap, basis = capital.effective_capital(book)
    assert cap == 400000 and 'compounded' in basis
    # ...and the size follows, which is the whole point.
    assert capital.limits(book).max_lots == 2


def test_a_loss_compounds_downward_too(monkeypatch):
    monkeypatch.setattr(cfg, 'COMPOUND', True)
    cap, _ = capital.effective_capital([closed(pnl_net=-30000)])
    assert cap == 170000


def test_compounding_uses_NET_pnl_only():
    """Gross overstates the account by the fee drag — 0.64% proportional plus
    ~Rs 87/leg on this book — and compounding on it sizes up on money that was
    never there."""
    book = [dict(closed(), pnl=99999)]          # gross only, no pnl_net
    total, costed, uncosted = capital.realised_pnl(book)
    assert (total, costed, uncosted) == (0.0, 0, 1)


def test_uncostable_history_is_counted_not_assumed_zero():
    """179 old rows can never be costed at all — no per-leg prices were ever
    stored. They must be reported, not silently treated as break-even."""
    book = [closed(pnl_net=1000), closed(), closed()]
    total, costed, uncosted = capital.realised_pnl(book)
    assert (total, costed, uncosted) == (1000.0, 1, 2)


def test_only_closed_positions_count_as_realised():
    book = [dict(pos(), pnl_net=99999), closed(pnl_net=500)]
    total, _c, _u = capital.realised_pnl(book)
    assert total == 500.0


# ── what a position costs ───────────────────────────────────────────────────

def test_capital_is_the_debit_times_quantity():
    assert capital.position_capital(pos(debit=13.55, lot=700)) == pytest.approx(9485)


def test_an_explicit_quantity_wins_over_lot_maths():
    t = pos(debit=10.0, lot=100, lots=1)
    t['quantity'] = 500
    assert capital.position_capital(t) == 5000


@pytest.mark.parametrize('bad', [
    {'lot_size': 100},                                  # no debit
    {'debit': 10.0},                                    # no size
    {'debit': 'NA', 'lot_size': 100},                   # unparseable debit
    {'debit': 10.0, 'lot_size': 0},                     # zero lot
    {'debit': 10.0, 'quantity': -100},                  # negative
])
def test_an_unreadable_position_is_None_never_zero(bad):
    """Zero would be added to a total and silently declared affordable. None
    forces the caller to decide, and every caller here refuses."""
    assert capital.position_capital(bad) is None


# ── what the book is holding ────────────────────────────────────────────────

def test_deployed_counts_only_what_is_still_holding_money():
    book = [pos('A'), pos('B'), pos('C', status='exited'),
            pos('D', status='watching')]
    rupees, n, unpriced = capital.deployed(book)
    assert (n, unpriced) == (2, 0)
    assert rupees == 2000


def test_a_closing_position_still_holds_its_money():
    """The close lock is taken but the position is not out of the market.
    Treating it as free would size a replacement against committed capital."""
    rupees, n, _ = capital.deployed([pos('A', status='closing')])
    assert (n, rupees) == (1, 1000)


def test_an_unpriceable_holding_is_counted_and_reported():
    book = [pos('A'), {'stock': 'B', 'status': 'entered'}]
    rupees, n, unpriced = capital.deployed(book)
    assert (n, unpriced, rupees) == (2, 1, 1000)


# ── the limits ──────────────────────────────────────────────────────────────

def test_an_unpriceable_candidate_fails_CLOSED():
    """The one refusal here that is not about a number being too big — it is
    about not having the number. A missed entry costs nothing."""
    ok, why = capital.check([], {'stock': 'A'}, lim())
    assert ok is False and 'unknown' in why


def test_the_count_cap_still_binds():
    ok, why = capital.check([pos('A'), pos('B'), pos('C')], pos('D'),
                            lim(max_open=3))
    assert ok is False and 'max_open_trades' in why


def test_one_position_per_stock():
    """Codifies what the cohort already did — 10 of 10 distinct stocks — so it
    is a limit instead of a coincidence."""
    ok, why = capital.check([pos('RELIANCE')], pos('RELIANCE'), lim())
    assert ok is False and 'max_open_per_stock' in why
    ok, _ = capital.check([pos('RELIANCE')], pos('INFY'), lim())
    assert ok is True


def test_the_per_trade_cap():
    L = lim(max_trade=12000)
    ok, why = capital.check([], pos(debit=200.0, lot=100), L)   # Rs 20,000
    assert ok is False and 'per-trade cap' in why
    ok, _ = capital.check([], pos(debit=100.0, lot=100), L)     # Rs 10,000
    assert ok is True


def test_the_total_deployed_cap():
    """The limit that did not exist at all. Peak observed was Rs 81,291."""
    L = lim(max_deployed=50000)
    book = [pos('A', debit=100.0, lot=450)]                    # Rs 45,000
    ok, why = capital.check(book, pos('B', debit=100.0, lot=100), L)  # +10,000
    assert ok is False and 'book cap' in why
    ok, _ = capital.check(book, pos('B', debit=10.0, lot=100), L)     # +1,000
    assert ok is True


def test_an_incomplete_book_refuses_rather_than_approves():
    """`held + want fits` is not a fact when `held` is understated by an
    unknown amount."""
    book = [pos('A'), {'stock': 'B', 'status': 'entered'}]
    ok, why = capital.check(book, pos('C'), lim(max_deployed=50000))
    assert ok is False and 'understated' in why


def test_a_zero_capital_book_applies_only_the_count_limits():
    """Capital 0 leaves the ratios nothing to resolve against, so the rupee
    caps are None. Not a licence to trade — a book with no capital configured
    has bigger problems — but the arithmetic must not divide by it or refuse
    everything with a confusing reason."""
    L = lim(capital_=0.0, max_trade=None, max_deployed=None)
    ok, _ = capital.check([], pos(debit=10000.0, lot=1000), L)
    assert ok is True


def test_describe_states_the_capital_and_everything_derived():
    """Nothing about this file is visible from outside until it refuses
    something, and this system has already shipped two controls that were
    wired in, looked deployed and could never fire."""
    d = capital.describe([])
    assert 'CAPITAL Rs 200000' in d and 'base' in d
    assert '1 lot(s)/position' in d
    assert 'Rs 25000 (12.5%)' in d          # the owner's per-trade figure
    assert 'max 8 open' in d


def test_describe_reports_what_compounding_would_give():
    """The number gets watched before it is believed — so it has to be
    printed while it is still switched off."""
    d = capital.describe([closed(pnl_net=25000)])
    assert 'compounding OFF' in d and '225000' in d


def test_describe_flags_history_it_could_not_cost(monkeypatch):
    """A compounded figure standing on a partial P&L is a different number
    from one standing on a complete one."""
    monkeypatch.setattr(cfg, 'COMPOUND', True)
    d = capital.describe([closed(pnl_net=1000), closed()])
    assert 'no pnl_net' in d


# ── liquidity ───────────────────────────────────────────────────────────────

def test_liquidity_is_the_thinner_of_the_two_sides():
    """Entry BUYS the long (needs ASK size) and SELLS the short (needs BID
    size). Whichever side is thinner is the one that binds."""
    d = {'long': {'ask_qty': 1000}, 'short': {'bid_qty': 350}}
    assert capital.liquidity_lots(d, 100) == 3


def test_a_one_sided_book_supports_nothing():
    assert capital.liquidity_lots(
        {'long': {'ask_qty': 0}, 'short': {'bid_qty': 900}}, 100) == 0


def test_unknown_depth_is_None_not_infinity():
    assert capital.liquidity_lots(None, 100) is None
    assert capital.liquidity_lots({}, 100) is None


# ── sizing ──────────────────────────────────────────────────────────────────

def test_a_thin_book_sizes_down_even_when_the_budget_is_large():
    """The owner's case exactly: 'sometimes we may not have enough liquidity
    to trade 5 lots'."""
    pl = capital.plan([], pos(debit=10.0, lot=100),
                      depth={'long': {'ask_qty': 250},
                             'short': {'bid_qty': 250}},
                      lim=lim(max_lots=5, max_deployed=500000))
    assert pl['lots'] == 2 and pl['bound'] == 'liquidity'


def test_a_full_budget_sizes_down_even_when_the_book_is_deep():
    deep = {'long': {'ask_qty': 99999}, 'short': {'bid_qty': 99999}}
    pl = capital.plan([], pos(debit=10.0, lot=100), depth=deep,   # Rs 1,000/lot
                      lim=lim(max_lots=5, max_deployed=13000))
    assert pl['lots'] == 5 and pl['bound'] == 'max_lots'
    pl = capital.plan([], pos(debit=10.0, lot=100), depth=deep,
                      lim=lim(max_lots=5, max_deployed=3500))
    assert pl['lots'] == 3 and pl['bound'] == 'max_deployed_rupees'


def test_the_bound_is_reported_not_just_the_number():
    """Bound by LIQUIDITY is a reason to distrust the signal; bound by BUDGET
    says nothing about it. The agent and the ticket need to tell them apart."""
    pl = capital.plan([], pos(debit=10.0, lot=100),
                      depth={'long': {'ask_qty': 99999},
                             'short': {'bid_qty': 99999}},
                      lim=lim(max_lots=10, max_trade=4000))
    assert pl['bound'] == 'max_trade_rupees'
    assert set(pl['bounds']) >= {'max_lots', 'max_trade_rupees', 'liquidity'}


def test_a_refused_candidate_plans_zero_lots():
    pl = capital.plan([pos('RELIANCE')], pos('RELIANCE'), lim=lim())
    assert pl['lots'] == 0 and 'max_open_per_stock' in pl['reason']


def test_missing_depth_sizes_to_one_not_to_the_budget():
    """Depth absent is not depth unlimited. Same class as an unpriceable
    candidate: take the size that is certainly executable."""
    pl = capital.plan([], pos(debit=10.0, lot=100), depth={},
                      lim=lim(max_lots=5))
    assert pl['lots'] == 1 and pl['bound'] == 'liquidity_unknown'


def test_no_depth_argument_at_all_leaves_liquidity_out_of_it():
    """A caller that never had depth to give is a different case from one
    whose depth lookup came back empty — it must not be silently capped at 1
    lot by a limit it was never measured against."""
    pl = capital.plan([], pos(debit=10.0, lot=100), lim=lim(max_lots=4))
    assert pl['lots'] == 4 and 'liquidity' not in pl['bounds']


def test_orders_are_always_one_lot():
    """Free on Neo, and each fill is confirmed before the next goes out — so a
    book thinner than the top-of-book claimed part-fills instead of paying
    through."""
    assert capital.SLICE_LOTS == 1
    pl = capital.plan([], pos(debit=10.0, lot=100), lim=lim())
    assert pl['slice_lots'] == 1


# ── after the fill ──────────────────────────────────────────────────────────

_T = {'long_symbol': 'X26SEP100CE', 'short_symbol': 'X26SEP110CE',
      'quantity': 700}


def _net(long_qty, short_qty):
    return [{'tradingsymbol': 'X26SEP100CE', 'quantity': long_qty},
            {'tradingsymbol': 'X26SEP110CE', 'quantity': short_qty}]


def test_a_matching_position_verifies():
    assert capital.verify_entry(_net(700, -700), _T)['ok'] is True


def test_a_short_fill_is_caught():
    v = capital.verify_entry(_net(700, -350), _T)
    assert v['ok'] is False and 'record says -700' in v['problems'][0]


def test_a_missing_leg_is_caught():
    v = capital.verify_entry([{'tradingsymbol': 'X26SEP100CE',
                               'quantity': 700}], _T)
    assert v['ok'] is False and 'NO position' in v['problems'][0]


def test_a_leg_with_the_right_size_and_the_WRONG_SIGN_is_caught():
    """The Feb-2026 shape: four BUYs on the short leg flipped it long. The
    magnitude looks right and the leg reads as present."""
    v = capital.verify_entry(_net(700, +700), _T)
    assert v['ok'] is False and 'shows +700' in v['problems'][0]


def test_an_unreadable_broker_is_NOT_a_pass():
    """"We could not look" and "we looked and it is fine" must never render
    the same. `checked` says which happened; `ok` is False for both."""
    v = capital.verify_entry(None, _T)
    assert v['ok'] is False and v['checked'] is False
    assert capital.verify_entry(_net(700, -700), _T)['checked'] is True


def test_verification_never_orders():
    """A mismatch means the ORDER-PLACING code needs a human. Putting more
    orders on top is the amplification that turned a stop into a four-fill
    loss."""
    import inspect
    src = inspect.getsource(capital.verify_entry)
    assert 'place_order' not in src and 'place_limit' not in src


@pytest.mark.parametrize('junk', [None, [], [{}], [{'tradingsymbol': None}]])
def test_verification_never_raises_on_a_malformed_position_book(junk):
    capital.verify_entry(junk, _T)


# ── the config itself ───────────────────────────────────────────────────────

def test_compounding_is_read_strictly():
    """Asserted on the SOURCE, because no input distinguishes the two readings.

    `_strict_bool` is well covered, but the constant is evaluated once at
    import from a file this process does not control, so swapping it for a
    plain `bool(_runtime.get(...))` changes nothing anywhere and the mutation
    survives. This decides position SIZE from a P&L figure — `"compound": 1`
    must not be able to arm that by accident.
    """
    import inspect
    assert "COMPOUND = _strict_bool('compound')" in inspect.getsource(cfg)


#: Keys where the code fallback and the tracked defaults file disagree.
#:
#: They are not equivalent: `config/zebra_config.defaults.json` is a real
#: config LAYER and wins at runtime, so for these three the `_DEFAULTS` value
#: in `config.py` is dead and reading it gives the wrong answer. All three
#: predate the capital work and are left as they are — the effective values
#: (55 / 0.015 / 5) are the ones the book has been running on.
#:
#: The list exists so a NEW divergence fails here instead of being discovered
#: the way these were: by a mutation that changed a default and altered
#: nothing.
KNOWN_DEFAULT_DRIFT = {
    'max_dte': (45, 55),
    'max_leg_spread_pct': (0.01, 0.015),
    'time_sl_days_before_expiry': (4, 5),
}


def test_the_two_default_sources_do_not_drift_further():
    import json
    tracked = json.loads(
        (HELPER / 'config' / 'zebra_config.defaults.json').read_text())
    drift = {k: (cfg._DEFAULTS[k], v) for k, v in tracked.items()
             if k in cfg._DEFAULTS and cfg._DEFAULTS[k] != v}
    assert drift == KNOWN_DEFAULT_DRIFT, (
        'the code fallback and the tracked defaults file disagree on a key '
        'that is not on the known list. The FILE wins at runtime, so the '
        'code default is dead and misleading to read: %r' % (drift,))


def test_the_capital_keys_agree_across_both_sources():
    """Specifically the Phase 2 keys, since a silent divergence there changes
    position size."""
    import json
    tracked = json.loads(
        (HELPER / 'config' / 'zebra_config.defaults.json').read_text())
    for k in ('capital_rupees', 'capital_per_lot', 'max_trade_pct',
              'max_deployed_pct', 'max_lots_hard', 'compound',
              'max_open_trades', 'max_open_per_stock'):
        assert tracked[k] == cfg._DEFAULTS[k], k
