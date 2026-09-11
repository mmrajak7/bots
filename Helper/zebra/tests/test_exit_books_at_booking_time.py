"""A vetted exit books the book at BOOKING time, not the one it triggered on.

Owner, 2026-09-11: "aft vet - book in real price bid-ask".

#498 KEI's take-profit was quoted at 09:25:11. Its exit vet answered in-cycle
at 09:27:11 (M12 waits up to 120s in line), and the close booked `mid=66.75`
and its `exit_legs` from that 117-second-old book at 09:27:18. Every vetted
exit — tp, trail, spot_sl, debit_sl — passed the pre-vet quote straight to
`_paper_auto_close`. In paper mode the booked number IS the result.

Pinned here:
1. an aged book is read again, and that read is what is booked AND what the
   alert states
2. a book still inside the quote TTL is NOT read again (the quote budget)
3. an unusable read defers: nothing booked, no "auto-closed" alert, and the
   exit re-fires next cycle
4. a collapse between trigger and booking that spot does not explain defers
5. a LIVE record is never re-read — its alert is the order ticket
6. every vetted booking site reads first, alerts second, books third, and a
   deferred booking stands down the rest of the cascade

Run:  cd Helper && python -m pytest zebra/tests/test_exit_books_at_booking_time.py -v
"""
import inspect

import pytest

from zebra import monitor, strikes
from zebra.tests.test_quote_prefetch import (  # noqa: F401  (fixture import)
    _429, CountingKite, _book, one_open_position)

# fill basis: long BID - short ASK
TRIGGER = ((11.9, 12.1), (1.9, 2.1))        # 11.9 - 2.1 = 9.80
BOOKING = ((20.0, 20.2), (3.0, 3.2))        # 20.0 - 3.2 = 16.80
COLLAPSED = ((5.0, 5.2), (1.0, 1.2))        # 5.0 - 1.2 = 3.80, -61%


def _broker(trade, books):
    return CountingKite({'NFO:' + trade['long_symbol']: _book(*books[0]),
                         'NFO:' + trade['short_symbol']: _book(*books[1])})


def _set_books(kite, trade, books):
    kite.books['NFO:' + trade['long_symbol']] = _book(*books[0])
    kite.books['NFO:' + trade['short_symbol']] = _book(*books[1])


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    return now


@pytest.fixture
def alerts_on(monkeypatch):
    monkeypatch.setattr(monitor, '_alerts_enabled', lambda trade: True)


def _tp_cycle(monkeypatch, store, trade, kite, spot_now, during_vet):
    """Spot through the target; the exit vet 'waits', and `during_vet` is what
    the market does meanwhile. `spot_now` is a one-item list the vet may move."""
    if not spot_now:
        spot_now.append(float(trade['tp_spot']) + 1.0)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda k, stocks: {trade['stock']: spot_now[0]})

    def vetted_after_a_wait(*a, **k):
        during_vet(kite)
        return True
    monkeypatch.setattr(monitor, '_exit_cleared', vetted_after_a_wait)
    monitor.check_entered(store, kite=kite, dry_run=True)


def test_an_aged_book_is_read_again_and_that_is_what_books_and_alerts(
        one_open_position, monkeypatch, clock, alerts_on,  # noqa: F811
        telegrams):
    """The #498 shape. The trigger saw 9.80; two minutes of vet later the book
    is 20.0/20.2 and 3.0/3.2 and spot has moved. A close sent then fills at
    16.80 — and the Telegram must say 16.80, not the trigger's 9.80."""
    store, trade = one_open_position
    assert trade['direction'] == 'CE'
    kite = _broker(trade, TRIGGER)
    spot_now = []

    def two_minutes_pass(k):
        clock[0] += 120.0
        _set_books(k, trade, BOOKING)
        spot_now[0] += 1.0
    _tp_cycle(monkeypatch, store, trade, kite, spot_now, two_minutes_pass)

    t = store.find(1)
    assert t['status'] == 'exited', t.get('status')
    assert t['exit_debit'] == 16.8, (
        'booked %s — the pre-vet trigger book' % t['exit_debit'])
    assert t['exit_legs']['long']['bid'] == 20.0
    assert t['exit_legs']['short']['ask'] == 3.2
    assert t['exit_spot'] == spot_now[0], 'the booking did not re-read spot'
    closed = [m for m in telegrams if 'auto-closed' in m]
    assert len(closed) == 1 and 'exit_mid 16.80' in closed[0], telegrams


def test_a_current_book_is_not_read_again_and_an_aged_one_is(
        one_open_position, monkeypatch, clock):  # noqa: F811
    """Review M1. Inside the quote TTL the trigger's book IS the current book;
    re-reading it on every exit spent the budget `d8ef36e` protects."""
    store, trade = one_open_position
    kite = _broker(trade, TRIGGER)
    ltp_calls = []
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda k, s: ltp_calls.append(list(s))
                        or {trade['stock']: 100.0})
    strikes.prefetch_quotes(kite, [trade['long_symbol'], trade['short_symbol']])
    trigger = monitor._structure_quote(kite, trade, 100.0)
    assert trigger['mid'] == 9.8
    _set_books(kite, trade, BOOKING)

    clock[0] += strikes.QUOTE_CACHE_TTL_SEC - 1.0
    quote, _ = monitor._booking_quote(kite, trade, 100.0, trigger, 'tp')
    assert quote is trigger
    assert len(kite.calls) == 1 and ltp_calls == [], 'a current book was re-read'

    clock[0] += 2.0
    quote, _ = monitor._booking_quote(kite, trade, 100.0, trigger, 'tp')
    assert quote['mid'] == 16.8
    assert len(kite.calls) == 2 and len(ltp_calls) == 1


def test_an_unusable_book_at_booking_defers_silently_and_refires(
        one_open_position, monkeypatch, clock, alerts_on,  # noqa: F811
        telegrams):
    """Review H1. A close that cannot book must not announce "auto-closed" —
    and must not do it again every cycle it keeps deferring. The flag is
    released, so the exit fires again next cycle."""
    store, trade = one_open_position
    kite = _broker(trade, TRIGGER)
    spot_now = []

    def book_goes_dark(k):
        clock[0] += 120.0
        k.raises = _429('Too many requests')
    _tp_cycle(monkeypatch, store, trade, kite, spot_now, book_goes_dark)
    t = store.find(1)
    assert t['status'] == 'entered', 'booked a close with no readable book'
    assert not t.get('exit_debit')
    # No exit alert AT ALL, not merely none saying auto-closed: the booked
    # line of a deferred close reads auto-close pending, and re-sending that
    # every cycle it keeps deferring is the noise H1 was about.
    assert not [m for m in telegrams if 'TP</b>' in m], telegrams

    kite.raises = None
    strikes.reset_quote_cache()               # the next cycle is a new process
    clock[0] += 300.0
    _tp_cycle(monkeypatch, store, trade, kite, spot_now, lambda k: None)
    t = store.find(1)
    assert t['status'] == 'exited', 'the deferred exit never re-fired'
    assert t['exit_debit'] == 9.8
    closed = [m for m in telegrams if 'auto-closed' in m]
    assert len(closed) == 1 and 'exit_mid 9.80' in closed[0], telegrams


def test_a_collapse_spot_does_not_explain_defers_the_booking(
        one_open_position, monkeypatch, clock):  # noqa: F811
    """Review M2. The vet and the corroboration veto judged the TRIGGER book.
    A book that falls apart during the wait while spot stands still is the
    NHPC signature, and must not become the booked result."""
    monkeypatch.setattr(monitor.cfg, 'SPOT_VETO_ENABLED', True)
    store, trade = one_open_position
    kite = _broker(trade, TRIGGER)
    spot_now = []

    def collapses_on_a_still_spot(k):
        clock[0] += 120.0
        _set_books(k, trade, COLLAPSED)
    _tp_cycle(monkeypatch, store, trade, kite, spot_now,
              collapses_on_a_still_spot)
    t = store.find(1)
    assert t['status'] == 'entered', (
        'booked %s off an uncorroborated collapse' % t.get('exit_debit'))


def test_the_same_collapse_books_when_spot_explains_it(
        one_open_position, monkeypatch, clock):  # noqa: F811
    """Veto-only, like the veto it mirrors: when spot DID move, the lower value
    is a real repricing and is booked."""
    monkeypatch.setattr(monitor.cfg, 'SPOT_VETO_ENABLED', True)
    store, trade = one_open_position
    kite = _broker(trade, TRIGGER)
    spot_now = []

    def collapses_with_spot(k):
        clock[0] += 120.0
        _set_books(k, trade, COLLAPSED)
        spot_now[0] *= 1.0 + 3 * monitor.cfg.SPOT_MOVE_MIN_PCT
    _tp_cycle(monkeypatch, store, trade, kite, spot_now, collapses_with_spot)
    t = store.find(1)
    assert t['status'] == 'exited', t.get('status')
    assert t['exit_debit'] == 3.8


def test_a_live_record_is_never_re_read(one_open_position, monkeypatch):  # noqa: F811
    """A LIVE record books nothing in zebra and its alert is the order ticket:
    it must describe what fired, and cost no request."""
    store, trade = one_open_position
    live = dict(trade, paper=False)
    assert not monitor.is_paper_record(live)
    kite = CountingKite()
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda *a: pytest.fail('a live record re-read spot'))
    trigger = {'mid': 9.8, 'reliable': True}
    quote, spot = monitor._booking_quote(kite, live, 100.0, trigger, 'tp')
    assert quote is trigger and spot == 100.0 and kite.calls == []


def test_the_alert_states_the_trigger_and_the_booking_separately(monkeypatch):
    """What fired and what booked are different facts once they can differ."""
    monkeypatch.setattr(monitor.cfg, 'PAPER_MODE', True)
    t = {'stock': 'ACME', 'direction': 'CE', 'structure': 'bcs',
         'debit': 10.0, 'debit_sl_value': 5.0, 'quantity': 100}
    msg = monitor._format_debit_sl_alert(t, 4.4, booked=4.9)
    assert 'Mid 4.40' in msg, msg                       # the trigger
    assert 'exit_mid 4.90' in msg and 'exit_mid 4.40' not in msg, msg
    assert 'Lost ~51%' in msg, msg                      # the booking
    assert monitor._format_debit_sl_alert(t, 4.4) == \
        monitor._format_debit_sl_alert(t, 4.4, booked=4.4)


def test_every_vetted_exit_reads_then_alerts_then_books():
    """The behaviour tests drive TP. Trail, spot-SL and debit-SL share the
    helpers but not the call sites, and a site that alerted before reading, or
    booked the trigger's `mid`, would pass every test above.

    RETIRES WHEN: the vetted exits in `check_entered` collapse into one
    booking path (one read/alert/close sequence for every price-driven kind),
    or valuation moves behind a shared booking service both engines use.
    """
    src = inspect.getsource(monitor.check_entered)
    for kind in ('tp', 'trail', 'spot_sl', 'debit_sl'):
        i_read = src.index("_booking_quote(kite, trade, spot, sq, '%s')" % kind)
        i_send = src.index("_send_exit_alert(store, trade, '%s'" % kind)
        i_book = src.index("_paper_auto_close(store, trade, bq['mid'], '%s'" % kind)
        assert i_read < i_send < i_book, kind
    assert '_paper_auto_close(store, trade, mid,' not in src
    for gate in ("if booking_deferred or not debit_usable or not tl",
                 "sl_hit = not booking_deferred and",
                 "if booking_deferred or not debit_usable:"):
        assert gate in src, 'a deferred booking no longer stands down: ' + gate


def test_a_close_that_did_not_book_reads_pending_in_every_formatter(monkeypatch):
    """Second review, L1. TP and SPOT SL said "pending" for booked=None, while
    TRAIL and DEBIT SL fell back to the trigger and said "auto-closed" — the H1
    message, one careless caller away. Suppression keeps it unreachable today;
    the four formatters must not disagree about it anyway."""
    monkeypatch.setattr(monitor.cfg, 'PAPER_MODE', True)
    t = {'stock': 'ACME', 'direction': 'CE', 'structure': 'bcs',
         'debit': 10.0, 'debit_sl_value': 5.0, 'quantity': 100,
         'tp_spot': 104.0, 'sl_spot': 95.0, 'entry_spot': 100.0,
         'long_symbol': 'ACME26SEP100CE', 'short_symbol': 'ACME26SEP110CE'}
    tl = {'level': 6.0, 'peak_gain': 3.0, 'peak_pct_of_max': 50.0}
    msgs = {
        'tp': monitor._format_tp_alert(t, 105.0, 9.8, booked=None),
        'spot_sl': monitor._format_spot_sl_alert(t, 95.0, 9.8, booked=None),
        'debit_sl': monitor._format_debit_sl_alert(t, 4.4, booked=None),
        'trail': monitor._format_trail_alert(t, 5.9, tl, booked=None),
    }
    for kind, msg in msgs.items():
        assert 'auto-close pending' in msg, (kind, msg)
        assert 'auto-closed' not in msg, (kind, msg)
