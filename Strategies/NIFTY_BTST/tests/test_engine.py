"""Whole simulated sessions driven through Engine.step()."""
from datetime import timedelta

from conftest import LOT, T, default_premiums, go, sym
from nifty_btst.engine import Engine
from nifty_btst.notify import NullNotifier

MON, TUE, WED = '2026-10-26', '2026-10-27', '2026-10-28'   # TUE is an expiry
CE_MON = sym('2026-10-27', 22850, 'CE')
PE_MON = sym('2026-10-27', 22700, 'PE')


def eng(cfg, store, market):
    return Engine(cfg, store, market, NullNotifier())


def marked_monday(cfg, store, market):
    default_premiums(market, '2026-10-27')
    e = eng(cfg, store, market)
    go(e, T(MON, '09:15:30'))
    go(e, T(MON, '09:20:01'))
    return e


def test_mark_picks_nearest_rs100_each_side_and_stamps_day_before(cfg, store, market):
    marked_monday(cfg, store, market)
    day = store.day(T(MON, '09:20:01').date())
    assert day['marks']['CE']['symbol'] == CE_MON
    assert day['marks']['PE']['symbol'] == PE_MON
    assert day['marks']['CE']['trigger'] == round(102 * 1.5, 2)
    assert day['cls'] == 'day_before' and day['marks']['CE']['dte_sessions'] == 1


def test_no_mark_before_0920(cfg, store, market):
    default_premiums(market, '2026-10-27')
    e = eng(cfg, store, market)
    go(e, T(MON, '09:19:59'))
    assert not store.day(T(MON, '09:19:59').date()).get('mark_done')


def test_expiry_day_marks_the_next_weekly(cfg, store, market):
    default_premiums(market, '2026-11-03')
    e = eng(cfg, store, market)
    go(e, T(TUE, '09:20:05'))
    day = store.day(T(TUE, '09:20:05').date())
    assert day['expiry'] == '2026-11-03'
    assert day['cls'] == 'expiry_day'
    assert day['marks']['CE']['expiry'] == '2026-11-03'


def test_full_btst_round_trip_buys_at_ask_sells_next_day_0925_at_bid(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 152.0)                 # below trigger 153
    go(e, T(MON, '10:00:00'))
    assert not store.open_positions()
    market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    go(e, T(MON, '10:00:03'))
    [p] = store.open_positions()
    assert p['entry_price'] == 153.8 and p['qty'] == LOT
    assert p['stop_level'] == round(153.8 * 0.5, 2)
    assert p['slip_vs_trigger'] == round(153.8 - 153.0, 2)
    market.set(CE_MON, 180.0)
    go(e, T(MON, '15:29:58'))                # still held overnight
    assert store.open_positions()
    market.set(CE_MON, 210.0, bid=209.5, ask=210.5)
    go(e, T(TUE, '09:15:05'))
    go(e, T(TUE, '09:24:59'))
    assert store.open_positions()             # not yet 09:25
    go(e, T(TUE, '09:25:01'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'next_day_0925' and c['exit_price'] == 209.5
    assert c['gross_pnl'] == round((209.5 - 153.8) * LOT, 2)
    assert c['net_pnl'] < c['net_ex_brokerage'] < c['gross_pnl']
    assert c['held_sessions'] == 1 and c['next_open_ltp'] == 210.0


def test_same_day_stop_and_only_once_per_side(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    market.set(CE_MON, 77.0, bid=76.8, ask=77.2)
    go(e, T(MON, '11:30:00'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'stop' and c['exit_price'] == 76.8
    market.set(CE_MON, 160.0)                 # back above trigger: no re-entry
    go(e, T(MON, '12:00:00'))
    assert not store.open_positions()


def test_both_sides_can_trade_the_same_day(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    market.set(PE_MON, 148.0)
    go(e, T(MON, '11:00:00'))
    assert [p['side'] for p in store.open_positions()] == ['CE', 'PE']


def test_no_entry_after_1525(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 160.0)
    go(e, T(MON, '15:25:01'))
    assert not store.open_positions()


def test_overnight_gap_through_stop_fires_before_0925(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    market.set(CE_MON, 40.0, bid=39.8, ask=40.2)
    go(e, T(TUE, '09:15:02'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'stop_next_morning' and c['exit_price'] == 39.8


def test_unreliable_book_defers_entry_then_fills(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 155.0, bid=0.0, ask=155.2)           # one-sided
    go(e, T(MON, '11:00:00'))
    assert not store.open_positions()
    m = store.day(T(MON, '11:00:00').date())['marks']['CE']
    assert m['entry_defers'] == 1 and 'T11:00:00' in m['trigger_seen_at']
    market.set(CE_MON, 155.0, bid=154.9, ask=155.1)
    go(e, T(MON, '11:00:03'))
    assert store.open_positions()[0]['entry_price'] == 155.1


def test_triggered_stop_stays_latched_through_a_bad_book(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    market.set(CE_MON, 70.0, bid=0.0, ask=70.5)             # stop hit, no bid
    go(e, T(MON, '11:10:00'))
    assert store.open_positions()[0]['exit_pending'] == 'stop'
    market.set(CE_MON, 90.0, bid=89.8, ask=90.2)            # bounced above stop
    go(e, T(MON, '11:10:03'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'stop' and c['exit_price'] == 89.8


def test_missed_next_day_exit_is_flagged_late(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    e2 = eng(cfg, store, market)             # box restarted mid-morning
    market.set(CE_MON, 170.0)
    go(e2, T(TUE, '11:00:00'))
    assert store.closed_positions()[0]['exit_reason'] == 'next_day_late'


def test_position_left_open_past_expiry_settles_at_intrinsic(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    market.closes[T(TUE, '00:00:00').date()] = 22900.0      # missed all of Tuesday
    default_premiums(market, '2026-11-03')
    e2 = eng(cfg, store, market)
    go(e2, T(WED, '09:15:05'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'expired_settled'
    assert c['exit_price'] == 50.0                          # 22900 - 22850
    assert c['charges']['brokerage'] == 20.0                # one order only


def test_late_start_marks_from_0920_candles_and_skips_an_already_fired_side(cfg, store, market):
    default_premiums(market, '2026-10-27')
    t920 = T(MON, '09:20:00')
    market.candles[256265] = [{'t': t920, 'open': 22800.0, 'high': 0, 'low': 0, 'close': 0}]
    for o in market.opts:
        if o['expiry'] != '2026-10-27':
            continue
        p0 = market.books[o['tradingsymbol']].ltp
        hi = 160.0 if o['tradingsymbol'] == CE_MON else p0
        market.candles[o['instrument_token']] = [
            {'t': t920, 'open': p0, 'high': p0, 'low': p0, 'close': p0},
            {'t': t920 + timedelta(minutes=30), 'open': p0, 'high': hi, 'low': p0, 'close': p0}]
    e = eng(cfg, store, market)
    market.set(CE_MON, 158.0)                # still above trigger at our start
    go(e, T(MON, '10:30:00'))
    day = store.day(T(MON, '10:30:00').date())
    assert day['marks']['CE']['late'] is True
    assert 'CE' in day['missed'] and day['fired']['CE']
    assert not store.open_positions()        # never back-filled
    assert 'PE' not in day['missed']


def test_quote_outage_alerts_once_and_recovers(cfg, store, market):
    default_premiums(market, '2026-10-27')
    n = NullNotifier()
    e = Engine(cfg, store, market, n)
    market.fail = True
    t = T(MON, '10:00:00')
    for i in range(cfg.quote_fail_alert_after + 3):
        go(e, t + timedelta(seconds=3 * i))
    assert sum('QUOTES FAILING' in s for s in n.sent) == 1
    market.fail = False
    go(e, t + timedelta(minutes=5))
    assert any('recovered' in s for s in n.sent) and e.fail_streak == 0


def test_eod_digest_text(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    text = e.eod_digest(T(MON, '15:31:00').date())
    assert 'CE' in text and 'BOUGHT' in text and 'carried 1' in text


def test_restart_after_mark_skips_a_side_that_fired_while_down(cfg, store, market):
    marked_monday(cfg, store, market)
    m = store.day(T(MON, '09:20:01').date())['marks']['CE']
    market.candles[m['token']] = [{'t': T(MON, '10:05:00'), 'open': 150, 'high': 156.0,
                                   'low': 150, 'close': 155}]
    market.set(CE_MON, 170.0)                # well above trigger by the time we are back
    e2 = eng(cfg, store, market)
    go(e2, T(MON, '11:00:00'))
    day = store.day(T(MON, '11:00:00').date())
    assert day['missed']['CE']['why'] == 'restart'
    assert not store.open_positions()


def test_restart_with_no_crossing_keeps_trading(cfg, store, market):
    marked_monday(cfg, store, market)
    e2 = eng(cfg, store, market)
    market.set(CE_MON, 140.0)
    go(e2, T(MON, '11:00:00'))
    market.set(CE_MON, 154.0)
    go(e2, T(MON, '11:00:03'))
    assert store.open_positions()[0]['side'] == 'CE'


def test_failed_mark_retries_live_every_poll_but_backs_off_once_late(cfg, store, market):
    default_premiums(market, '2026-10-27')
    calls = {'n': 0}
    real = market.options

    def broken():
        calls['n'] += 1
        raise RuntimeError('instrument dump down')
    market.options = broken
    e = eng(cfg, store, market)
    for i in range(10):                      # 09:20:00-09:20:27: live retries are cheap
        go(e, T(MON, '09:20:00') + timedelta(seconds=3 * i))
    assert calls['n'] == 10
    calls['n'] = 0
    for i in range(10):                      # late path (~120 historical calls): back off
        go(e, T(MON, '09:21:01') + timedelta(seconds=3 * i))
    assert calls['n'] == 1
    market.options = real
    t920 = T(MON, '09:20:00')
    market.candles[256265] = [{'t': t920, 'open': 22800.0, 'high': 0, 'low': 0, 'close': 0}]
    for o in market.opts:
        if o['expiry'] == '2026-10-27':
            p0 = market.books[o['tradingsymbol']].ltp
            market.candles[o['instrument_token']] = [
                {'t': t920, 'open': p0, 'high': p0, 'low': p0, 'close': p0}]
    go(e, T(MON, '09:22:01'))
    day = store.day(T(MON, '09:22:01').date())
    assert day['mark_done'] and day['marks']['CE']['late'] is True
    assert day['marks']['CE']['symbol'] == CE_MON


def test_holiday_with_stale_books_closes_the_day_and_books_nothing(cfg, store, market):
    e = marked_monday(cfg, store, market)
    go(e, T(MON, '11:00:00'))
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:03'))
    assert store.open_positions()
    # Tuesday is a holiday the calendar KNOWS about: every book is yesterday's.
    h = Engine(cfg, store, market, NullNotifier(), session_expected=False)
    default_premiums(market, '2026-11-03')
    market.set(CE_MON, 154.0)
    market.stale = set(market.books)
    go(h, T(TUE, '09:15:05'))
    go(h, T(TUE, '09:20:30'))                # the mark finds nothing traded today
    assert store.day(T(TUE, '09:20:30').date())['closed'] is True
    go(h, T(TUE, '09:25:30'))                # the 09:25 exit is due, but it is a holiday
    assert store.open_positions()            # yesterday's book is never booked
    assert not h.notify.sent                 # an expected holiday is not an alert


def test_late_start_on_a_holiday_closes_quietly(cfg, store, market):
    default_premiums(market, '2026-10-27')
    market.stale = set(market.books)         # and no candles exist for today
    e = Engine(cfg, store, market, NullNotifier(), session_expected=False)
    go(e, T(MON, '10:30:00'))
    assert store.day(T(MON, '10:30:00').date())['closed'] is True
    assert not e.notify.sent


def test_exit_due_on_a_stale_book_waits_for_a_trade_today(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 154.0)
    go(e, T(MON, '11:00:00'))
    market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    market.stale = {CE_MON}                  # not traded yet this morning
    go(e, T(TUE, '09:25:01'))
    assert store.open_positions()[0]['exit_pending'] == 'next_day_0925'
    market.stale = set()
    go(e, T(TUE, '09:25:04'))
    [c] = store.closed_positions()
    assert c['exit_reason'] == 'next_day_0925' and c['exit_price'] == 169.8


def test_unexpected_dead_market_alerts_and_does_not_mark(cfg, store, market):
    default_premiums(market, '2026-10-27')
    market.stale = set(market.books)
    n = NullNotifier()
    e = Engine(cfg, store, market, n, session_expected=True)
    go(e, T(MON, '09:20:05'))
    day = store.day(T(MON, '09:20:05').date())
    assert not day.get('mark_done') and not day.get('closed')
    assert any('MARK FAILED' in s and 'not live' in s for s in n.sent)


def test_calendar_says_holiday_but_market_trades(cfg, store, market):
    default_premiums(market, '2026-10-27')
    n = NullNotifier()
    e = Engine(cfg, store, market, n, session_expected=False)
    go(e, T(MON, '09:20:05'))
    assert store.day(T(MON, '09:20:05').date())['mark_done']
    assert any('calendar' in s.lower() for s in n.sent)


def test_stale_book_never_triggers_a_stop_or_entry(cfg, store, market):
    e = marked_monday(cfg, store, market)
    market.set(CE_MON, 160.0)
    market.stale = {CE_MON}
    go(e, T(MON, '11:00:00'))
    assert not store.open_positions()
    market.stale = set()
    go(e, T(MON, '11:00:03'))
    [p] = store.open_positions()
    market.set(CE_MON, 50.0)
    market.stale = {CE_MON}
    go(e, T(MON, '11:10:00'))
    assert store.open_positions()            # a stale print below the stop is not a stop
