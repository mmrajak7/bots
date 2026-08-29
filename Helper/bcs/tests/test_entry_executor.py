"""Opening a spread with real orders (Phase 3) — the failure modes.

This is new order-placing code, the category `feedback_live_automation_bar`
exists for. The happy path is the least interesting thing here; what these
tests pin is what happens when it goes wrong, because both real-money losses
on this book came from order code behaving badly under failure rather than
under success.

The asymmetry driving every rule: a missed entry costs nothing, a bad one
costs capital. So the entry path never escalates, never pays through, never
retries the whole spread and never unwinds.
"""
import pytest

from bcs import entry_executor as ee
from bcs import spread_monitor as sm

EX, LOT = 'NFO', 100
LONG, SHORT = 'TESTCO26SEP1000CE', 'TESTCO26SEP1040CE'

GOOD = {'bid': 30.0, 'bid_qty': 5000, 'ask': 30.2, 'ask_qty': 5000,
        'ltp': 30.1, 'prev_close': 29.0, 'traded_today': True,
        'ltp_fresh': True}
GOOD_SHORT = {'bid': 10.0, 'bid_qty': 5000, 'ask': 10.2, 'ask_qty': 5000,
              'ltp': 10.1, 'prev_close': 9.5, 'traded_today': True,
              'ltp_fresh': True}
#: The 2026-07-24 NHPC shape: width 133% of mid, an unformed book.
JUNK = {'bid': 0.28, 'bid_qty': 25, 'ask': 1.40, 'ask_qty': 25, 'ltp': 0.30,
        'prev_close': 1.0, 'traded_today': True, 'ltp_fresh': True}


class Broker:
    """Records orders and answers fills by a per-symbol script."""

    def __init__(self, books=None, fills=None):
        self.books = books or {LONG: GOOD, SHORT: GOOD_SHORT}
        self.fills = fills or {}          # symbol -> list of 'FILL'/'TIMEOUT'
        self.placed = []
        self.cancelled = []
        self._n = 0

    def depth(self, kite, exchange, symbol):
        return self.books[symbol]

    def place(self, kite, exchange, symbol, txn, qty, price, dry_run,
              context=None):
        self._n += 1
        oid = 'O%d' % self._n
        self.placed.append({'symbol': symbol, 'txn': txn, 'qty': qty,
                            'price': price, 'order_id': oid,
                            'context': context})
        return oid

    def wait(self, kite, order_id, dry_run):
        sym = next(p['symbol'] for p in self.placed
                   if p['order_id'] == order_id)
        script = self.fills.get(sym)
        verdict = script.pop(0) if script else 'FILL'
        if verdict != 'FILL':
            return None
        px = next(p['price'] for p in self.placed if p['order_id'] == order_id)
        return {'status': 'COMPLETE', 'average_price': px,
                'order_id': order_id, 'filled_quantity': LOT}

    def cancel(self, kite, order_id, dry_run):
        self.cancelled.append(order_id)

    def final(self, kite, order_id):
        return None


@pytest.fixture
def broker(monkeypatch):
    b = Broker()
    monkeypatch.setattr(sm, 'get_option_depth', b.depth)
    monkeypatch.setattr(sm, 'place_limit_order', b.place)
    monkeypatch.setattr(sm, 'wait_for_fill', b.wait)
    monkeypatch.setattr(sm, 'cancel_order_safe', b.cancel)
    monkeypatch.setattr(sm, '_order_final_state', b.final)
    monkeypatch.setattr(sm.time, 'sleep', lambda s: None)
    # Well inside the order window, so the cutoff is not what is under test.
    import datetime as _dt

    class _DT(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime(2026, 9, 15, 11, 0, 0)
    monkeypatch.setattr(ee, 'datetime', _DT)
    return b


def run(b, lots=1, dry_run=True, **kw):
    said = []
    sent = []
    out = ee.open_spread(
        kite=None, stock='TESTCO', long_symbol=LONG, short_symbol=SHORT,
        exchange=EX, lot_size=LOT, lots=lots, dry_run=dry_run,
        log=said.append, telegram=lambda m, *a, **k: sent.append(m), **kw)
    return out, said, sent


# ── the gate ────────────────────────────────────────────────────────────────

def test_auto_entry_is_off_by_default():
    """The third switch, and the only one that arms code which OPENS
    positions."""
    from zebra import config as cfg
    assert cfg._DEFAULTS['auto_entry'] is False
    allowed, why = ee.entries_allowed()
    assert allowed is False and 'auto_entry' in why


def test_the_gate_is_read_strictly():
    """No input distinguishes the two readings — the constant is evaluated
    once at import from a file this process does not control."""
    import inspect
    from zebra import config as cfg
    assert "AUTO_ENTRY = _strict_bool('auto_entry')" in inspect.getsource(cfg)


def test_an_unreadable_config_means_NO(monkeypatch):
    """FAILS CLOSED, the opposite of `sm.trading_enabled()`. That one fails
    OPEN because it guards a path that only CLOSES positions, where a config
    error must not abandon live stops. Here the same error must not start
    placing orders."""
    import builtins
    real = builtins.__import__

    def boom(name, *a, **k):
        if name == 'zebra.config' or name == 'zebra':
            raise ImportError('config is broken')
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', boom)
    allowed, why = ee.entries_allowed()
    assert allowed is False and 'unreadable' in why


def test_the_kill_switch_stops_entries_too(monkeypatch):
    """Anyone who disarms trading expects it to stop ALL orders. A switch that
    only half-works is worse than one that does not exist."""
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: False)
    allowed, why = ee.entries_allowed()
    assert allowed is False and 'kill switch' in why
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    assert ee.entries_allowed()[0] is True


def test_a_live_run_refuses_when_the_gate_is_shut(broker):
    out, _said, _sent = run(broker, lots=1, dry_run=False)
    assert out['lots_filled'] == 0
    assert broker.placed == [], 'orders were placed with entries disarmed'


# ── the happy path, and the order of it ─────────────────────────────────────

def test_a_single_lot_buys_the_long_before_selling_the_short(broker):
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 1
    assert [(p['txn'], p['symbol']) for p in broker.placed] == [
        ('BUY', LONG), ('SELL', SHORT)]


def test_each_round_completes_a_whole_spread(broker):
    """Not all-the-longs-then-all-the-shorts. That holds N naked longs in the
    middle, and a short leg that then cannot fill leaves a position nobody
    chose. Round-at-a-time means stopping early leaves complete spreads."""
    out, _said, _sent = run(broker, lots=3)
    assert out['lots_filled'] == 3
    assert [(p['txn'], p['symbol']) for p in broker.placed] == [
        ('BUY', LONG), ('SELL', SHORT)] * 3


def test_orders_are_one_lot_each(broker):
    out, _said, _sent = run(broker, lots=3)
    assert all(p['qty'] == LOT for p in broker.placed)
    assert len(broker.placed) == 6


def test_the_buy_pays_the_ask_and_the_sell_takes_the_bid(broker):
    """Crossing the touch by a bounded, known amount. Pricing an entry at mid
    quotes a debit nobody can fill at — the same error the whole book was
    re-based off in August."""
    run(broker, lots=1)
    buy, sell = broker.placed
    assert buy['price'] > GOOD['ask']
    assert sell['price'] < GOOD_SHORT['bid']


def test_the_recorded_debit_comes_from_the_FILLS(broker):
    """Every stop and the trail derive from the entry debit. Recording the
    intended price instead of the paid one puts every level under the
    position."""
    out, _said, _sent = run(broker, lots=2)
    paid = [a - b for a, b in zip(out['long_fills'], out['short_fills'])]
    assert ee.entry_debit(out) == pytest.approx(sum(paid) / len(paid), abs=0.01)
    assert ee.entry_debit(out) > GOOD['ask'] - GOOD_SHORT['bid']


def test_no_fills_means_no_debit(broker):
    assert ee.entry_debit({'lots_filled': 0, 'long_fills': [],
                           'short_fills': []}) is None


# ── the failure modes ───────────────────────────────────────────────────────

def test_a_junk_book_is_not_entered(monkeypatch, broker):
    """No `urgent` escape hatch, unlike the close path. There, refusing to act
    on a bad book leaves a live position unhedged; here it just means no
    trade."""
    broker.books = dict(broker.books, **{LONG: JUNK})
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 0
    assert broker.placed == [], 'an order was placed against an unformed book'


def test_a_long_that_never_fills_leaves_nothing_held(broker):
    broker.fills = {LONG: ['TIMEOUT', 'TIMEOUT']}
    out, _said, _sent = run(broker, lots=2)
    assert out['lots_filled'] == 0 and out['orphan'] is None
    assert not any(p['txn'] == 'SELL' for p in broker.placed), \
        'the short leg was sold with no long against it'


def test_a_timed_out_order_is_CANCELLED_before_any_retry(broker):
    """`wait_for_fill` returns None WITHOUT cancelling — the order is still
    live. Placing another on top is exactly how the Feb-2026 short leg got
    bought four times."""
    broker.fills = {LONG: ['TIMEOUT', 'FILL']}
    run(broker, lots=1)
    assert broker.cancelled == ['O1']
    assert len(broker.placed) == 3          # O1 timed out, O2 long, O3 short


def test_a_fill_landing_in_the_cancel_race_is_kept(monkeypatch, broker):
    """Cancel and fill can cross. Treating the position as absent when the
    broker has it is how a live leg goes unrecorded."""
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    broker.fills = {LONG: ['TIMEOUT']}
    monkeypatch.setattr(
        sm, '_order_final_state',
        # A price NO ordinary attempt could produce. It was 30.3, which is
        # exactly ask+buffer -- so when the "keep the race fill" branch was
        # deleted, attempt 2 filled at the same 30.3 and the test still
        # passed. The mutation survived on a coincidence of arithmetic.
        lambda kite, oid: {'status': 'COMPLETE', 'average_price': 99.9,
                           'order_id': oid, 'filled_quantity': LOT})
    # LIVE, because the race only exists when a real order is out there --
    # `_order_final_state` is deliberately not consulted in dry run.
    out, _said, _sent = run(broker, lots=1, dry_run=False)
    assert out['long_fills'] == [99.9]
    longs = [p for p in broker.placed if p['symbol'] == LONG]
    assert len(longs) == 1, (
        'a replacement long went out after the cancel race had already '
        'filled: %r' % (longs,))


def test_an_orphan_long_is_reported_and_NOT_unwound(broker):
    """The whole point of long-first. A round that dies between the legs
    leaves a capped-risk LONG, never a naked short — and placing a corrective
    order through a book that just failed to fill, with the same code that
    produced the problem, is the amplification that turned a stop into a
    four-fill loss."""
    broker.fills = {SHORT: ['TIMEOUT', 'TIMEOUT']}
    out, _said, sent = run(broker, lots=1)
    assert out['lots_filled'] == 0
    assert out['orphan'] == {'symbol': LONG, 'qty': LOT, 'fill': broker.placed[0]['price']}
    sells_of_the_long = [p for p in broker.placed
                         if p['symbol'] == LONG and p['txn'] == 'SELL']
    assert sells_of_the_long == [], 'the orphan was auto-unwound'
    assert sent and 'UNHEDGED LONG' in sent[0]


def test_a_partial_entry_records_only_the_COMPLETE_spreads(broker):
    """Two rounds fill, the third loses its short. Two complete spreads are a
    valid, monitorable position; the third round's long is not part of it."""
    broker.fills = {SHORT: ['FILL', 'FILL', 'TIMEOUT', 'TIMEOUT']}
    # FOUR lots, so there is a round LEFT after the orphan. At three the
    # orphan landed in the final round and `break` was indistinguishable from
    # `continue` -- the mutation that carried on regardless survived.
    out, _said, sent = run(broker, lots=4)
    assert out['lots_filled'] == 2
    assert out['orphan'] is not None
    assert len(out['long_fills']) == 2 and len(out['short_fills']) == 2
    assert sent and '2/4' in sent[0]
    # The orphan happens in round 3; round 4 must never start. Asserted on
    # the ROUND rather than an order count -- the short leg retries within its
    # round, so a count conflates "carried on" with "tried twice", which is
    # the distinction under test.
    assert max(p['context']['round'] for p in broker.placed) == 3, (
        'the run carried on past an orphan into another round: %r'
        % ([p['context']['round'] for p in broker.placed],))
    assert len([p for p in broker.placed if p['txn'] == 'BUY']) == 3, (
        'a fourth long was bought after the orphan')


def test_an_incomplete_entry_always_telegrams(broker):
    """A partial that only appears in a log line is the same failure as an
    unmonitored position: the record and the world disagree and nobody is
    told."""
    broker.fills = {LONG: ['TIMEOUT', 'TIMEOUT']}
    _out, _said, sent = run(broker, lots=1)
    assert sent, 'a failed entry said nothing to the owner'


def test_a_complete_entry_does_not_telegram(broker):
    """One signal, one alert. The ticket already went out."""
    _out, _said, sent = run(broker, lots=2)
    assert sent == []


def test_past_the_cutoff_nothing_is_placed(monkeypatch, broker):
    """Entries get the NORMAL cutoff and never the urgent one — there is no
    entry worth placing at 15:24.

    The clock is pinned on `spread_monitor`, not on this module. That is the
    point of the change it caught: `LAST_ORDER_TIME` is an IST time-of-day,
    and this cutoff used to compare the BOX clock against it — so on a UTC box
    (box time 03:45-10:00 during Indian market hours) it would NEVER fire and
    a retry loop could place an entry order past 15:25 IST. The executor now
    reads `sm.now_ist()`, the fleet's one clock, so a test that stubs only
    this module's `datetime` no longer reaches it — which is how this test
    failed and said so.
    """
    import datetime as _dt

    class _Late(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            late = _dt.datetime(2026, 9, 15, 15, 22, 0)
            return late.replace(tzinfo=tz) if tz else late
    monkeypatch.setattr(sm, 'datetime', _Late)
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 0 and broker.placed == []


def test_it_never_raises_on_a_broken_broker(monkeypatch, broker):
    """A quote that explodes must cost the entry, not the cycle."""
    def boom(*a, **k):
        raise RuntimeError('kite is down')
    monkeypatch.setattr(sm, 'get_option_depth', boom)
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 0


def test_zero_lots_places_nothing(broker):
    out, _said, _sent = run(broker, lots=0)
    assert out['lots_filled'] == 0 and broker.placed == []


# ── evidence ────────────────────────────────────────────────────────────────

def test_every_order_carries_its_context_to_the_journal(broker):
    """`place_limit_order` journals INTENT before the broker call and RESULT
    after. Without context an intent says an order happened but not which
    trade, leg or round it belonged to."""
    run(broker, lots=2)
    for p in broker.placed:
        c = p['context']
        assert c['reason'] == 'ENTRY' and c['strategy'] == 'BCS'
        assert c['leg'] in ('long', 'short') and c['stock'] == 'TESTCO'
    assert [p['context']['round'] for p in broker.placed] == [1, 1, 2, 2]


def test_dry_run_exercises_the_path_without_the_gate(broker):
    """Dry run places nothing, so it is allowed to walk the whole flow with
    `auto_entry` still off. That is what makes the path testable on the box
    before it is armed — the same reason the monitor keeps `--dry-run`."""
    assert ee.entries_allowed()[0] is False
    out, _said, _sent = run(broker, lots=1, dry_run=True)
    assert out['lots_filled'] == 1


# ── adversarial review, 2026-08-26 ──────────────────────────────────────────
#
# Five findings, all in this file's first version. Each test below is the one
# that would have caught it.

def test_a_PARTIAL_fill_is_a_position_not_a_non_event(broker, monkeypatch):
    """C1. The first version accepted only status COMPLETE and returned None
    for everything else, reasoning that "the order is one lot, so it fills or
    it does not". A lot is hundreds of shares and NSE fills partially —
    `wait_for_fill` documents returning a CANCELLED order carrying
    `filled_quantity > 0`, and those shares are HELD."""
    def half(kite, order_id, dry_run):
        return {'status': 'CANCELLED', 'average_price': 30.3,
                'order_id': order_id, 'filled_quantity': LOT // 2}
    monkeypatch.setattr(sm, 'wait_for_fill', half)
    out, _said, sent = run(broker, lots=1)
    assert out['lots_filled'] == 0
    assert out['partials'] == [{'symbol': LONG, 'qty': LOT // 2,
                                'fill': 30.3, 'round': 1}]
    assert sent and 'PARTIAL' in sent[0], (
        'shares are held at the broker and the owner was not told')
    assert not any('nothing extra held' in p for p in out['problems']), (
        'it reported holding nothing while holding half a lot')


def test_a_partial_short_against_a_full_long_reports_BOTH(broker, monkeypatch):
    """Worse than an orphan: neither a spread nor a clean single leg."""
    real = sm.wait_for_fill

    def script(kite, order_id, dry_run):
        sym = next(p['symbol'] for p in broker.placed
                   if p['order_id'] == order_id)
        if sym == SHORT:
            return {'status': 'CANCELLED', 'average_price': 9.9,
                    'order_id': order_id, 'filled_quantity': LOT // 4}
        return real(kite, order_id, dry_run)
    monkeypatch.setattr(sm, 'wait_for_fill', script)
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 0
    assert out['orphan'] is not None, 'the full long went unreported'
    assert out['partials'] and out['partials'][0]['symbol'] == SHORT


def test_an_exception_mid_run_KEEPS_the_rounds_that_filled(broker,
                                                           monkeypatch):
    """C2. `place_limit_order` RE-RAISES on a broker exception, and nothing
    caught it — so an exception on round 3 escaped with the result discarded,
    taking two real spreads with it. The caller then recorded nothing."""
    calls = {'n': 0}
    real = broker.place

    def boom(*a, **k):
        calls['n'] += 1
        if calls['n'] == 5:              # round 3's long
            raise RuntimeError('broker rejected: margin')
        return real(*a, **k)
    monkeypatch.setattr(sm, 'place_limit_order', boom)
    out, _said, sent = run(broker, lots=3)
    assert out['lots_filled'] == 2, (
        'the completed spreads were lost with the exception')
    assert len(out['long_fills']) == 2
    assert any('raised' in p for p in out['problems'])
    assert sent, 'an exception mid-entry said nothing to the owner'


def test_a_book_that_moved_past_the_gate_is_NOT_entered(broker):
    """C3. The signal was gated (debit/width <= 45%, entry cost <= 15%)
    against a book that no longer exists. Nothing re-checked the price at
    execution, so the executor would pay whatever the new book said and open a
    trade the gates had rejected. CLAUDE.md: ASHOKLEY read 33% at signal and
    40.5% next morning — a hard fail."""
    # gated at 15.0; the live book prices it at 20.3 - 9.9 = 20.4
    out, _said, _sent = run(broker, lots=1, gated_debit=15.0)
    assert out['lots_filled'] == 0
    assert broker.placed == [], 'it paid a price the gates would have rejected'
    assert any('book moved' in p for p in out['problems'])


def test_a_book_INSIDE_the_slippage_allowance_still_enters(broker):
    """The negative control, and the one that matters more: the cap must not
    refuse the slippage this file already intends to pay. Two legs crossing by
    ENTRY_SLIPPAGE_TICKS is the design, not a moved market."""
    gated = GOOD['ask'] - GOOD_SHORT['bid']          # 20.2
    out, _said, _sent = run(broker, lots=1, gated_debit=gated)
    assert out['lots_filled'] == 1, (
        'the cap refused the buffer the executor is designed to pay')


def test_with_no_gated_debit_the_cap_does_not_apply(broker):
    """A caller that has no gate to check against must not be silently
    blocked by one it never supplied."""
    out, _said, _sent = run(broker, lots=1)
    assert out['lots_filled'] == 1


def test_the_switch_is_re_read_EVERY_round(broker, monkeypatch):
    """C4. Read once per entry, a 3-lot entry places 6 orders over minutes.
    `trading_enabled`'s own docstring: a switch consulted once at startup
    cannot stop something already running."""
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    def flips():
        # Disarmed the moment anything has actually been bought. Keyed on the
        # ORDERS rather than a call count, so the test does not silently
        # depend on how many times the gate happens to be consulted.
        return not any(p['txn'] == 'BUY' for p in broker.placed)
    monkeypatch.setattr(sm, 'trading_enabled', flips)
    out, _said, _sent = run(broker, lots=3, dry_run=False)
    assert out['lots_filled'] == 1, (
        'the kill switch was thrown mid-entry and orders kept going out')
    assert any('disarmed' in p or 'kill switch' in p for p in out['problems'])
    assert len([p for p in broker.placed if p['txn'] == 'BUY']) == 1


def test_a_cancelled_order_that_filled_the_WHOLE_lot_is_a_full_fill(
        broker, monkeypatch):
    """An order can be cancelled after it has completely filled.

    The first version of the partial-fill fix returned None for
    `filled >= qty`, throwing away an entire real leg because a string did not
    say COMPLETE. Found by a mutation, not by reading.
    """
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    monkeypatch.setattr(sm, 'wait_for_fill', lambda k, oid, d: None)
    monkeypatch.setattr(
        sm, '_order_final_state',
        lambda kite, oid: {'status': 'CANCELLED', 'average_price': 30.3,
                           'order_id': oid, 'filled_quantity': LOT})
    out, _said, _sent = run(broker, lots=1, dry_run=False)
    assert out['lots_filled'] == 1, 'a complete leg was discarded as "no fill"'
    assert out['partials'] == []


def test_a_partial_in_the_CANCEL_RACE_is_still_held(broker, monkeypatch):
    """The race path had its own `status == COMPLETE` test, so a partial that
    landed while cancelling was discarded exactly like the main path's was."""
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    monkeypatch.setattr(sm, 'wait_for_fill', lambda k, oid, d: None)
    monkeypatch.setattr(
        sm, '_order_final_state',
        lambda kite, oid: {'status': 'CANCELLED', 'average_price': 30.3,
                           'order_id': oid, 'filled_quantity': LOT // 4})
    out, _said, sent = run(broker, lots=1, dry_run=False)
    assert out['lots_filled'] == 0
    assert out['partials'] and out['partials'][0]['qty'] == LOT // 4
    assert sent and 'PARTIAL' in sent[0]


def test_an_unpriceable_book_stops_the_entry(broker, monkeypatch):
    """`prospective_debit` returning None must mean DO NOT ENTER, not "no cap
    applies". Absent is not unlimited — the same rule the sizing layer uses
    for missing depth."""
    monkeypatch.setattr(ee, 'prospective_debit', lambda *a, **k: None)
    out, _said, _sent = run(broker, lots=1, gated_debit=20.2)
    assert out['lots_filled'] == 0 and broker.placed == []
    assert any('could not price' in p for p in out['problems'])


def test_the_prospective_debit_prices_the_sides_we_actually_TRADE():
    """ASK on the long plus the buffer, BID on the short minus it — the same
    numbers `open_leg` sends. Reading the other two sides quotes a cheaper
    spread than anyone could open, which is the mid-pricing error in a new
    costume: it would let a moved book through the cap."""
    books = {LONG: GOOD, SHORT: GOOD_SHORT}
    import bcs.spread_monitor as _sm
    real = _sm.get_option_depth
    try:
        _sm.get_option_depth = lambda k, e, sym, fresh=False: books[sym]
        got = ee.prospective_debit(None, EX, LONG, SHORT)
    finally:
        _sm.get_option_depth = real
    buf = ee.ENTRY_SLIPPAGE_TICKS * sm.TICK_SIZE
    assert got == pytest.approx((GOOD['ask'] + buf) - (GOOD_SHORT['bid'] - buf))
    # ...and it must be DEARER than the touch-to-touch figure, never cheaper.
    assert got > GOOD['ask'] - GOOD_SHORT['bid']


def test_the_cap_refuses_a_book_that_moved_only_slightly(broker):
    """Discrimination test. gated 19.6 -> cap 19.8; the real cost to open is
    20.4. A version reading the wrong sides of the books computes 19.8 and
    lets it through, which no coarser scenario separates."""
    out, _said, _sent = run(broker, lots=1, gated_debit=19.6)
    assert out['lots_filled'] == 0, (
        'a spread costing 20.4 was opened against a 19.8 cap')


def test_an_ordinary_cancel_with_no_fill_is_not_reported_as_a_partial(
        broker, monkeypatch):
    """`filled_quantity: 0` is the NORMAL timeout-then-cancel result, not a
    position. Without the zero guard every routine non-fill would announce
    "PARTIAL fill 0 x SYM is held" — a false alarm on the commonest path,
    which makes the real partial alert worth less."""
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)
    monkeypatch.setattr(sm, 'wait_for_fill', lambda k, oid, d: None)
    monkeypatch.setattr(
        sm, '_order_final_state',
        lambda kite, oid: {'status': 'CANCELLED', 'average_price': 0,
                           'order_id': oid, 'filled_quantity': 0})
    out, _said, sent = run(broker, lots=1, dry_run=False)
    assert out['partials'] == [], 'a no-fill was announced as a held position'
    assert not any('PARTIAL' in m for m in sent)
