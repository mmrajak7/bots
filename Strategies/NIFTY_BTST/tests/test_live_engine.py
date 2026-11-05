"""The live engine, driven through simulated sessions on the fake broker."""
import json
from datetime import timedelta
from pathlib import Path

from conftest import LOT, T, default_premiums, go, sym
from live_helpers import MON, TUE, WED, Rig, make_scrips, neo_symbol
from nifty_btst.engine import Engine
from nifty_btst.live_engine import (ENTRY_OPEN, ENTRY_SENT, ENTRY_WAIT, EXIT_DUE, EXIT_OPEN,
                                    FILLED, LiveStore)
from nifty_btst.notify import NullNotifier
from nifty_btst.store import Store

CE_MON = sym('2026-10-27', 22850, 'CE')
PE_MON = sym('2026-10-27', 22700, 'PE')
CE_NEO = neo_symbol(CE_MON)


def marked(cfg, tmp_path, market, **kw):
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market, **kw)
    rig.start(T(MON, '09:00:00'))
    rig.go(T(MON, '09:15:30'))
    rig.go(T(MON, '09:20:01'))
    return rig


def trigger_ce(rig, when=T(MON, '10:00:00'), ltp=153.5, bid=153.3, ask=153.8):
    rig.market.set(CE_MON, ltp, bid=bid, ask=ask)
    rig.go(when)


def holding(rig, symbol=CE_MON, entry_price=150.0, qty=LOT, entry_date=MON,
            expiry='2026-10-27', side='CE', strike=22850.0, at_broker=True):
    neo = neo_symbol(symbol)
    p = rig.store.add_position({
        'status': 'open', 'side': side, 'symbol': symbol, 'token': 1, 'strike': strike,
        'expiry': expiry, 'lot_size': LOT if qty % LOT == 0 else qty, 'lots': 1, 'qty': qty,
        'entry_date': entry_date,
        'entry_time': entry_date + 'T10:00:00+05:30', 'mark_price': 100.0, 'trigger': 150.0,
        'entry_price': entry_price, 'stop_level': round(entry_price * 0.5, 2),
        'cls': 'day_before', 'dte_sessions': 1, 'dte_days': 1, 'mfe_ltp': entry_price,
        'mae_ltp': entry_price, 'exit_defers': 0,
        'live': {'state': FILLED, 'mode': 'live', 'neo_symbol': neo, 'neo_token': 'tok',
                 'tick': 0.05, 'leg': None, 'entry_attempts': 1, 'exit_attempts': 0,
                 'exit_fills': []}})
    if at_broker:
        rig.broker.ext_positions[neo] = qty
    rig.store.save()
    return p


def state(rig, pid=1):
    return [p for p in rig.store.data['positions'] if p['id'] == pid][0]


# ------------------------------------------------------------ happy path
def test_full_live_round_trip_uses_the_real_fill_for_the_stop(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    day = rig.store.day(T(MON, '09:20:01').date())
    assert day['marks']['CE']['neo']['symbol'] == CE_NEO
    rig.broker.fill_price_override = 153.9           # the broker fills a tick worse
    trigger_ce(rig)
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['state'] == ENTRY_OPEN
    assert rig.broker.sent[0].symbol == CE_NEO and rig.broker.sent[0].price == '153.80'
    assert rig.broker.sent[0].tag == '20261026-1-ENTRY-1'
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['state'] == FILLED
    assert p['entry_price'] == 153.9 and p['stop_level'] == 76.95 and p['qty'] == LOT
    rig.market.set(CE_MON, 180.0)
    rig.go(T(MON, '15:29:58'))
    rig.market.set(CE_MON, 210.0, bid=209.5, ask=210.5)
    rig.broker.fill_price_override = None
    rig.go(T(TUE, '09:15:05'))
    rig.go(T(TUE, '09:24:59'))
    assert state(rig)['status'] == 'open'
    rig.go(T(TUE, '09:25:01'))
    p = state(rig)
    assert p['live']['state'] == EXIT_OPEN and rig.broker.sent[-1].side == 'S'
    assert rig.broker.sent[-1].price == '209.50' and rig.broker.sent[-1].tag == '20261027-1-EXIT-1'
    rig.go(T(TUE, '09:25:02'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'next_day_0925'
    assert p['exit_price'] == 209.5 and p['gross_pnl'] == round((209.5 - 153.9) * LOT, 2)
    assert p['charges']['brokerage'] == 0.0                      # NEO API plan
    assert [ln['ev'] for ln in rig.journal.lines()] == ['INTENT', 'PLACED', 'INTENT', 'PLACED']
    assert rig.broker.net_long(CE_NEO, 'NRML') == 0
    assert any('FILLED' in s for s in rig.sent) and any('EXIT DUE' in s for s in rig.sent)


def test_stop_next_morning_fires_off_the_real_fill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.fill_price_override = 160.0
    trigger_ce(rig)
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['stop_level'] == 80.0
    rig.broker.fill_price_override = None
    rig.market.set(CE_MON, 80.0, bid=79.8, ask=80.2)
    rig.go(T(TUE, '09:15:02'))
    assert state(rig)['live']['exit_reason'] == 'stop_next_morning'
    rig.go(T(TUE, '09:15:03'))
    assert state(rig)['exit_reason'] == 'stop_next_morning' and state(rig)['exit_price'] == 79.8


# ------------------------------------------------------------- entry cap
def test_entry_cap_boundary_exactly_plus_five_percent(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    assert rig.store.day(T(MON, '09:20:01').date())['marks']['CE']['trigger'] == 153.0
    trigger_ce(rig, ltp=160.6, bid=160.5, ask=160.65)         # 153 x 1.05 = 160.65 exactly
    assert len(rig.broker.sent) == 1 and rig.broker.sent[0].price == '160.65'


def test_entry_one_tick_above_cap_waits_then_fills_when_it_comes_back(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig, ltp=160.7, bid=160.6, ask=160.70)
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['state'] == ENTRY_WAIT
    assert not rig.broker.sent and p['live']['above_cap_since']
    assert any('above cap' in s for s in rig.sent)
    rig.market.set(CE_MON, 161.0, bid=160.9, ask=161.1)
    rig.go(T(MON, '10:05:00'))
    assert not rig.broker.sent
    rig.market.set(CE_MON, 140.0, bid=139.9, ask=140.1)        # below trigger, under cap
    rig.go(T(MON, '11:00:00'))
    assert len(rig.broker.sent) == 1 and rig.broker.sent[0].price == '140.10'


def test_pending_entry_is_abandoned_at_entry_end(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig, ltp=170.0, bid=169.9, ask=170.1)
    assert state(rig)['live']['state'] == ENTRY_WAIT
    rig.go(T(MON, '15:25:30'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'entry_abandoned'


# ------------------------------------------------ entry timeout / attempts
def test_entry_not_filled_in_5s_is_cancelled_and_repriced(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.go(T(MON, '10:00:03'))
    assert state(rig)['live']['state'] == ENTRY_OPEN and len(rig.broker.sent) == 1
    rig.market.set(CE_MON, 154.0, bid=153.8, ask=154.2)
    rig.go(T(MON, '10:00:06'))                                  # >= 5 s: cancel requested
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'
    rig.go(T(MON, '10:00:07'))                                  # cancel seen -> re-priced
    assert len(rig.broker.sent) == 2 and rig.broker.sent[1].price == '154.20'
    assert rig.broker.sent[1].tag == '20261026-1-ENTRY-2'
    rig.broker.hold_fills = False
    rig.go(T(MON, '10:00:08'))
    assert state(rig)['status'] == 'open' and state(rig)['entry_price'] == 154.2


def test_max_entry_attempts_then_abandon(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    t = T(MON, '10:00:00')
    rig.market.set(CE_MON, 153.5, bid=153.3, ask=153.8)
    for i in range(0, 80):
        rig.go(t + timedelta(seconds=i))
        if state(rig)['status'] == 'closed':
            break
    p = state(rig)
    assert p['exit_reason'] == 'entry_abandoned' and len(rig.broker.sent) == rig.neo.max_entry_attempts
    assert all(o['ordSt'] == 'cancelled' for o in rig.broker.orders)


# ------------------------------------------------------------- rejections
def test_entry_rejected_ends_the_side_and_two_rejections_trip_the_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('reject', '1004', 'Insufficient balance.'),
                               ('reject', '1004', 'Insufficient balance.')]
    trigger_ce(rig)
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'entry_rejected'
    assert '1004' in p['rej_reason'] and any('REJECTED' in s for s in rig.sent)
    assert not rig.store.kill()['tripped']
    rig.market.set(CE_MON, 170.0)
    rig.go(T(MON, '10:01:00'))
    assert len(rig.broker.sent) == 1                            # the side is done
    rig.market.set(PE_MON, 148.0, bid=147.8, ask=148.2)
    rig.go(T(MON, '10:02:00'))
    assert rig.store.kill()['tripped'] and 'rejections' in rig.store.kill()['reason']
    assert [ln['ev'] for ln in rig.journal.lines()] == ['INTENT', 'NOT_PLACED'] * 2


def test_order_placed_then_rejected_by_rms(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = [('placed_rejected', 'RMS:Margin Exceeds')]
    trigger_ce(rig)
    assert state(rig)['live']['state'] == ENTRY_OPEN
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['status'] == 'closed' and p['rej_reason'].startswith('RMS')


def test_exit_rejected_retries_and_alerts(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.place_faults = [('reject', '1005', 'Internal Error')]
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['state'] == EXIT_DUE
    assert p['live']['exit_rejections'] == 1 and any('EXIT #1' in s and 'REJECTED' in s for s in rig.sent)
    rig.go(T(MON, '11:00:03'))
    assert len(rig.broker.sent) == 2                            # fast retry
    rig.go(T(MON, '11:00:04'))
    assert state(rig)['status'] == 'closed' and state(rig)['exit_reason'] == 'stop'


# ---------------------------------------------------------- unclear sends
def test_timed_out_send_that_reached_the_exchange_is_found_by_tag_never_resent(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    p = state(rig)
    assert p['live']['state'] == ENTRY_SENT and p['live']['leg']['order_no'] is None
    assert len(rig.broker.sent) == 1 and any('UNCLEAR' in s for s in rig.sent)
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['state'] == FILLED
    assert len(rig.broker.sent) == 1
    assert [ln['ev'] for ln in rig.journal.lines()] == ['INTENT', 'BOUND']
    assert p['live']['leg_history'][0]['bound_by'] == 'tag'


def test_unreadable_book_after_an_unclear_send_halts_rather_than_resends(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    rig.broker.read_faults = ['unavailable'] * 5
    for i in range(1, 6):
        rig.go(T(MON, '10:00:00') + timedelta(seconds=i))
        assert state(rig)['live']['state'] == ENTRY_SENT and len(rig.broker.sent) == 1
    assert any('unreadable' in s for s in rig.sent)
    rig.go(T(MON, '10:00:10'))                                  # reads recover
    assert state(rig)['status'] == 'open' and len(rig.broker.sent) == 1


def test_timed_out_send_that_never_arrived_is_resent_only_after_the_grace_window(cfg, tmp_path, market):
    """Rewritten for NEO-01/02: two absent reads 3 s apart used to be 'not
    placed'. Now absence concludes nothing until `unknown_grace_sec` has
    passed with valid reads AND positions agree nothing happened."""
    rig = marked(cfg, tmp_path, market, neo_overrides={'unknown_grace_sec': 120})
    rig.broker.place_faults = ['timeout_lost']
    trigger_ce(rig)
    for i in range(1, 119, 3):
        rig.go(T(MON, '10:00:00') + timedelta(seconds=i))
        assert state(rig)['live']['state'] == ENTRY_SENT and len(rig.broker.sent) == 1
    assert any('UNKNOWN' in s for s in rig.sent)
    rig.go(T(MON, '10:02:01'))                                  # grace over, positions agree
    assert len(rig.broker.sent) == 2 and rig.broker.sent[1].tag.endswith('ENTRY-2')
    evs = [ln['ev'] for ln in rig.journal.lines()]
    assert evs == ['INTENT', 'NOT_PLACED', 'INTENT', 'PLACED']


def test_tag_not_echoed_falls_back_to_criteria(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    rig.broker.orders[0]['GuiOrdId'] = ''                       # the broker dropped our tag
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['leg_history'][0]['bound_by'] == 'criteria'
    assert len(rig.broker.sent) == 1


def test_known_order_absent_from_the_book_is_never_dropped_on_its_day(cfg, tmp_path, market):
    """Rewritten for NEO-04: an order with a number used to be dropped after
    60 s of absence and re-sent. Now it is held all day, alerted every minute,
    and resolved the NEXT day against positions."""
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.blank_book = True                                # the book shows nothing
    for i in range(1, 1200, 60):
        rig.go(T(MON, '10:00:00') + timedelta(seconds=i))
        assert state(rig)['live']['state'] == ENTRY_OPEN and len(rig.broker.sent) == 1
    assert len([s for s in rig.sent if 'ABSENT' in s]) >= 15
    # next morning: DAY validity ended, but NO position row is not evidence
    # (R2-03): the leg stays UNKNOWN, the owner is told what to do, and only
    # the owner's statement books it as never filled.
    rig.broker.blank_book = False
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:05'))
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['state'] == ENTRY_OPEN
    assert any('live-book-external' in s for s in rig.sent)
    assert len(rig.broker.sent) == 1 and not rig.store.kill()['tripped']
    rig.engine.book_external(1, T(TUE, '09:20:00'), None)
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'entry_abandoned' and p['never_filled']
    assert any(ln['ev'] == 'OWNER' for ln in rig.journal.lines())


# ---------------------------------------------------------------- restart
def test_restart_during_unknown_send_resolves_against_the_book(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    rig.broker.hold_fills = True
    trigger_ce(rig)
    assert state(rig)['live']['state'] == ENTRY_SENT
    rig.restart()
    rig.start(T(MON, '10:00:30'))
    assert rig.broker.orders[0]['ordSt'] == 'open'              # nothing cancelled on restart
    rig.go(T(MON, '10:00:31'))
    p = state(rig)
    assert p['live']['leg']['order_no'] == rig.broker.orders[0]['nOrdNo']
    assert len(rig.broker.sent) == 1 and p['live']['state'] == ENTRY_OPEN
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'        # 31 s old: timed out, re-priced
    rig.go(T(MON, '10:00:32'))
    assert len(rig.broker.sent) == 2 and state(rig)['live']['state'] == ENTRY_OPEN
    rig.broker.hold_fills = False
    rig.go(T(MON, '10:00:33'))
    assert state(rig)['status'] == 'open' and len(rig.broker.sent) == 2


def test_restart_with_entry_open_keeps_the_order_and_then_times_it_out(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.restart()
    rig.start(T(MON, '10:00:02'))
    assert rig.broker.orders[0]['ordSt'] == 'open'
    rig.go(T(MON, '10:00:06'))
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'
    rig.go(T(MON, '10:00:07'))
    assert len(rig.broker.sent) == 2


def test_restart_with_exit_open_books_the_fill_it_finds(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert state(rig)['live']['state'] == EXIT_OPEN
    rig.broker.hold_fills = False
    rig.restart()
    rig.start(T(MON, '11:00:02'))
    rig.service(T(MON, '11:00:02'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'stop' and p['exit_price'] == 69.8


def test_restart_after_crash_between_place_and_journal_binds_from_the_journal(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    p = state(rig)
    no = p['live']['leg']['order_no']
    p['live']['leg']['order_no'] = None                         # the save after PLACED never landed
    p['live']['state'] = ENTRY_SENT
    rig.store.save()
    rig.restart()
    rig.start(T(MON, '10:00:02'))
    p = state(rig)
    assert p['live']['leg']['order_no'] == no and p['live']['state'] == ENTRY_OPEN
    assert len(rig.broker.sent) == 1


def test_restart_next_day_abandons_a_stale_pending_entry(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig, ltp=170.0, bid=169.9, ask=170.1)            # above cap: ENTRY_WAIT
    rig.restart()
    default_premiums(market, '2026-11-03')
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:15:05'))
    assert state(rig)['status'] == 'closed' and state(rig)['exit_reason'] == 'entry_abandoned'


def test_restart_with_position_sold_in_the_app_books_closed_externally(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.orders.append({'nOrdNo': '999', 'ordSt': 'complete', 'trdSym': CE_NEO,
                              'trnsTp': 'S', 'qty': str(LOT), 'fldQty': str(LOT),
                              'avgPrc': '171.00', 'prc': '171.00', 'rejRsn': '--',
                              'GuiOrdId': '', 'ordDtTm': '', 'prod': 'NRML',
                              '_placed': T(MON, '10:30:00')})
    rig.restart()
    rig.start(T(MON, '11:00:00'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'closed_externally'
    assert p['exit_price'] == 171.0 and not rig.store.kill()['tripped']


def test_restart_with_broker_flat_and_no_sale_is_a_mismatch_never_flattened(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, at_broker=False)
    rig.restart()
    rig.start(T(MON, '11:00:00'))
    p = state(rig)
    # a same-day position with no row is a shortfall, but it takes TWO reads
    # outside the positions lag before the kill trips (R2-08)
    assert p['status'] == 'open' and not rig.store.kill()['tripped']
    assert rig.engine.reconcile_ok is False
    rig.engine._reconcile(T(MON, '11:05:00'), startup=False)
    assert rig.store.kill()['tripped'] and 'mismatch' in rig.store.kill()['reason']
    assert not rig.broker.sent and any('RECONCILE MISMATCH' in s for s in rig.sent)


def test_restart_never_cancels_and_reports_untracked_orders(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    rig.broker.ensure_session()
    from nifty_btst.broker import OrderRequest
    rig.broker.place(OrderRequest(CE_NEO, 't', 'B', LOT, '1.00', '20261026-42-ENTRY-1'))
    rig.journal.intent(T(MON, '10:00:00'), '20261026-42-ENTRY-1', 'ENTRY', 42, CE_NEO, 'B', LOT, '1.00')
    rig.restart()
    rig.start(T(MON, '10:00:05'))
    assert rig.broker.orders[0]['ordSt'] == 'open'              # never cancelled
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'UNTRACKED' in k['reason']      # NEO-05
    assert rig.journal.unresolved() == []
    rig.service(T(MON, '10:01:10'))
    assert any('KILL (LOUD)' in s for s in rig.sent)


# ---------------------------------------------------------- selling guard
def test_sell_refused_when_the_broker_shows_no_long(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, at_broker=False)
    rig.engine.reconcile_ok = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    for i in range(0, 70, 3):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['state'] == EXIT_DUE and not rig.broker.sent
    assert any('SELL REFUSED' in s and 'live-confirm-held' in s for s in rig.sent)
    rig.broker.ext_positions[CE_NEO] = LOT                      # the broker catches up
    rig.go(T(MON, '11:02:00'))
    assert len(rig.broker.sent) == 1 and rig.broker.sent[0].side == 'S'


def test_sell_refused_while_another_sell_is_live_on_the_symbol(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    from nifty_btst.broker import OrderRequest
    rig.broker.place(OrderRequest(CE_NEO, 't', 'S', LOT, '200.00', 'manual'))   # owner's own sell
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert len(rig.broker.sent) == 1                            # only the manual one exists
    assert any('another SELL is live' in e.get('why', '') for e in
               [json.loads(ln) for ln in open(rig.dir / 'events_20261026.jsonl')]
               if e.get('kind') == 'SELL_REFUSED')


def test_never_two_live_sells_from_the_engine(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:01'))
    rig.go(T(MON, '11:00:02'))
    assert len(rig.broker.sent) == 1 and state(rig)['live']['state'] == EXIT_OPEN
    for i in range(3, 40):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
        live_sells = [o for o in rig.broker.orders if o['trnsTp'] == 'S' and o['ordSt'] == 'open']
        assert len(live_sells) <= 1


def test_broker_unreadable_at_exit_time_refuses_the_sell(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.read_faults = ['unavailable'] * 4
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert not rig.broker.sent and state(rig)['live']['state'] == EXIT_DUE
    rig.broker.read_faults = []
    rig.go(T(MON, '11:00:03'))
    assert len(rig.broker.sent) == 1


# -------------------------------------------------------------- exit chase
def test_exit_chase_cancels_reprices_then_slows_to_once_a_minute(cfg, tmp_path, market):
    """Rewritten for R2-10: in slow mode the working SELL is cancelled only
    when the next send is due, so an order is live almost all the time."""
    rig = marked(cfg, tmp_path, market, neo_overrides={'exit_chase_fast_max': 3})
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    t0 = T(MON, '11:00:00')
    for i in range(0, 30):
        rig.go(t0 + timedelta(seconds=i))
    assert len(rig.broker.sent) == 3                            # fast chases exhausted
    assert [o.tag[-6:] for o in rig.broker.sent] == ['EXIT-1', 'EXIT-2', 'EXIT-3']
    working = 0
    for i in range(30, 68):
        rig.go(t0 + timedelta(seconds=i))
        working += any(o['ordSt'] == 'open' for o in rig.broker.orders)
    assert len(rig.broker.sent) == 3 and working >= 36          # the 3rd SELL keeps working
    for i in range(68, 72):
        rig.go(t0 + timedelta(seconds=i))
    assert len(rig.broker.sent) == 4                            # cancelled and re-sent at 60 s
    assert any('STILL OPEN' in s for s in rig.sent)
    rig.broker.hold_fills = False
    rig.go(t0 + timedelta(seconds=73))
    assert state(rig)['status'] == 'closed'


def test_exit_partial_fill_on_cancel_chases_the_remainder(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, qty=130)
    rig.broker.hold_fills = True
    rig.broker.cancel_faults = ['partial']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:03'))                                  # cancel -> half filled
    rig.go(T(MON, '11:00:04'))
    p = state(rig)
    assert p['status'] == 'open' and p['live']['exit_fills'][0]['qty'] == 65
    assert len(rig.broker.sent) == 2 and rig.broker.sent[1].qty == 65
    assert any('PARTIAL' in s for s in rig.sent)
    rig.broker.hold_fills = False
    rig.market.set(CE_MON, 60.0, bid=59.8, ask=60.2)
    rig.go(T(MON, '11:00:05'))
    rig.go(T(MON, '11:00:06'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_price'] == round((69.8 * 65 + 69.8 * 65) / 130, 2)


def test_cancel_unclear_is_repeated_not_followed_by_a_new_order(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    rig.broker.cancel_faults = ['timeout']
    trigger_ce(rig)
    rig.go(T(MON, '10:00:06'))                                  # cancel -> timeout
    assert rig.broker.orders[0]['ordSt'] == 'open' and len(rig.broker.sent) == 1
    rig.go(T(MON, '10:00:07'))
    assert len(rig.broker.sent) == 1                            # still waiting for the read
    rig.go(T(MON, '10:00:10'))                                  # cancel repeated, works
    assert rig.broker.orders[0]['ordSt'] == 'cancelled'


# ------------------------------------------------------------------- kill
def test_kill_trips_exactly_at_75000_and_blocks_entries_only(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, symbol=PE_MON, side='PE', strike=22700.0, entry_price=100.0, qty=1000)
    rig.store.kill()['peak'] = 55000.0                          # an earlier high
    rig.market.set(PE_MON, 80.05, bid=80.05, ask=80.15)         # unreal -19,950 -> dd 74,950
    rig.go(T(MON, '11:00:00'))
    k = rig.store.kill()
    assert k['drawdown'] == 74950.0 and not k['tripped']
    rig.market.set(PE_MON, 80.0, bid=80.0, ask=80.1)            # dd 75,000 exactly
    rig.go(T(MON, '11:00:03'))
    k = rig.store.kill()
    assert k['tripped'] and 'drawdown Rs 75000' in k['reason']
    assert not rig.broker.sent
    rig.market.set(PE_MON, 40.0, bid=39.9, ask=40.1)            # the stop: exits still run
    rig.go(T(MON, '11:00:06'))
    assert rig.broker.sent and rig.broker.sent[-1].side == 'S'
    trigger_ce(rig, when=T(MON, '11:01:00'))
    p2 = state(rig, 2)                  # R3-24: abandoned at the trigger, never bought later
    assert p2['live']['state'] == 'ABANDONED' and p2['never_filled']
    assert not any(o.side == 'B' for o in rig.broker.sent)
    assert any('blocked (kill tripped)' in s for s in rig.sent)


def test_kill_peak_only_advances_on_fresh_marks_and_resume_resets_it(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    p = holding(rig, entry_price=100.0, qty=1000)
    rig.market.set(CE_MON, 200.0, bid=200.0, ask=200.2)
    rig.go(T(MON, '11:00:00'))
    assert rig.store.kill()['peak'] == 100000.0
    p['live']['mark'] = 300.0                                   # a stale, higher mark
    p['live']['mark_fresh'] = False
    rig.engine._update_kill(T(MON, '11:00:03'))
    assert rig.store.kill()['peak'] == 100000.0 and rig.store.kill()['equity'] == 200000.0
    rig.market.stale = {CE_MON}                                 # no trade today: mark stays
    rig.go(T(MON, '11:00:06'))
    assert rig.store.kill()['peak'] == 100000.0 and rig.store.kill()['marks_fresh'] is False
    rig.engine._trip(T(MON, '11:01:00'), 'test')
    rig.engine.resume(T(MON, '11:02:00'))
    k = rig.store.kill()
    assert not k['tripped'] and k['peak'] == k['equity'] and k['resets'][0]['was'] == 'test'


def test_restart_keeps_a_tripped_kill(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.engine._trip(T(MON, '11:00:00'), 'manual')
    rig.store.save()
    rig.restart()
    rig.start(T(MON, '11:05:00'))
    assert rig.store.kill()['tripped'] and any('KILL is TRIPPED' in s for s in rig.sent)


# --------------------------------------------------------------- ceilings
def test_entry_order_ceiling_trips_and_abandons(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'max_entry_orders_per_day': 1})
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.go(T(MON, '10:00:06'))
    rig.go(T(MON, '10:00:07'))
    p = state(rig)
    assert len(rig.broker.sent) == 1 and p['exit_reason'] == 'entry_abandoned'
    assert rig.store.kill()['tripped']


def test_order_ceiling_never_stops_exits_but_trips_the_kill(cfg, tmp_path, market):
    """Rewritten for NEO-21: the ceiling used to stop exits dead and never
    tripped the kill. Now exits keep chasing at their minimum interval and
    the kill trips (no new entries)."""
    rig = marked(cfg, tmp_path, market, neo_overrides={'max_orders_per_day': 2,
                                                       'max_entry_orders_per_day': 2})
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    for i in range(0, 20):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert len(rig.broker.sent) >= 4 and all(r.side == 'S' for r in rig.broker.sent)
    assert rig.store.kill()['tripped'] and 'ceiling' in rig.store.kill()['reason']
    trigger_ce(rig, when=T(MON, '11:01:00'))
    assert not any(r.side == 'B' for r in rig.broker.sent)


# ------------------------------------------------------------ pre-checks
def test_funds_unknown_refuses_and_funds_short_skips(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.funds_none = True
    trigger_ce(rig)
    assert not rig.broker.sent and any('LIMITS' in s for s in rig.sent)
    assert state(rig)['live']['state'] == ENTRY_WAIT
    rig.broker.funds_none = False
    rig.broker.funds = 100.0
    rig.go(T(MON, '10:00:03'))
    assert state(rig)['exit_reason'] == 'entry_skipped_funds' and not rig.broker.sent


def test_premium_ceiling_skips_never_resizes(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'max_premium_rs': 5000})
    trigger_ce(rig)
    assert state(rig)['exit_reason'] == 'entry_skipped_premium' and not rig.broker.sent


def test_side_without_a_neo_row_is_not_traded(cfg, tmp_path, market):
    from nifty_btst.neo_symbols import ScripMap
    scrips = make_scrips()
    scrips._by_key.pop((T(TUE, '00:00:00').date(), 22850.0, 'CE'))
    rig = marked(cfg, tmp_path, market, scrips=scrips)
    day = rig.store.day(T(MON, '09:20:01').date())
    assert 'neo_error' in day['marks']['CE'] and day['marks']['PE']['neo']
    assert any('NO NEO MAPPING' in s for s in rig.sent)
    trigger_ce(rig)
    assert not rig.broker.sent and day['fired']['CE'] and not rig.store.pending()
    assert isinstance(scrips, ScripMap)


def test_lot_size_mismatch_disables_the_side(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, scrips=make_scrips(lot=75))
    day = rig.store.day(T(MON, '09:20:01').date())
    assert 'lot size' in day['marks']['CE']['neo_error']
    trigger_ce(rig)
    assert not rig.broker.sent


def test_scrip_master_outage_is_retried_not_fatal(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    calls = {'n': 0}

    def loader():
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('download failed')
        return make_scrips()
    rig = Rig(cfg, tmp_path, market)
    rig.engine._scrip_loader = loader
    rig.start(T(MON, '09:00:00'))                               # the pre-open load fails
    assert calls['n'] == 1
    rig.go(T(MON, '09:20:01'))                                  # a WORKER retries (R2-12)
    t = rig.engine._scrips_thread
    if t is not None:
        t.join(5)
    rig.go(T(MON, '09:21:05'))                                  # mapped on the next entries tick
    day = rig.store.day(T(MON, '09:20:01').date())
    assert day['marks']['CE']['neo']['symbol'] == CE_NEO and calls['n'] == 2


def test_not_armed_blocks_entries_but_exits_run(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, armed=False)
    holding(rig, symbol=PE_MON, side='PE', strike=22700.0)
    trigger_ce(rig)
    assert not rig.broker.sent and any('not armed' in s for s in rig.sent)
    rig.market.set(PE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert rig.broker.sent and rig.broker.sent[0].side == 'S'


def test_login_failure_blocks_entries_and_retries_each_minute(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market)
    rig.broker.login_fail = True
    rig.start(T(MON, '09:00:00'))
    assert not rig.engine.session_ok and any('LOGIN FAILED' in s for s in rig.sent)
    rig.go(T(MON, '09:20:01'))
    trigger_ce(rig)
    assert not rig.broker.sent
    rig.broker.login_fail = False
    rig.go(T(MON, '10:00:30'))
    assert not rig.engine.session_ok                            # under a minute
    rig.go(T(MON, '10:01:05'))
    assert rig.engine.session_ok and len(rig.broker.sent) == 1


def test_lots_above_one_is_refused_in_code(cfg, tmp_path, market):
    from dataclasses import replace
    rig = marked(replace(cfg, lots=2), tmp_path, market)
    trigger_ce(rig)
    assert not rig.broker.sent and any('MAX_LIVE_LOTS' in s for s in rig.sent)


# ------------------------------------------------------------- calendar
def test_holiday_with_stale_books_sends_nothing(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    market.stale = set(market.books)
    rig2 = Rig(cfg, tmp_path, market, session_expected=False)
    rig2.broker = rig.broker
    rig2.engine.broker = rig.broker
    rig2.start(T(TUE, '09:00:00'))
    rig2.go(T(TUE, '09:15:05'))
    rig2.go(T(TUE, '09:20:30'))
    rig2.go(T(TUE, '09:25:30'))
    assert rig2.store.day(T(TUE, '09:25:30').date())['closed'] is True
    assert not rig.broker.sent and rig2.store.holding()


def test_late_start_missed_side_is_not_entered_live(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    t920 = T(MON, '09:20:00')
    market.candles[256265] = [{'t': t920, 'open': 22800.0, 'high': 0, 'low': 0, 'close': 0}]
    for o in market.opts:
        if o['expiry'] == '2026-10-27':
            p0 = market.books[o['tradingsymbol']].ltp
            hi = 160.0 if o['tradingsymbol'] == CE_MON else p0
            market.candles[o['instrument_token']] = [
                {'t': t920, 'open': p0, 'high': p0, 'low': p0, 'close': p0},
                {'t': t920 + timedelta(minutes=30), 'open': p0, 'high': hi, 'low': p0, 'close': p0}]
    rig = Rig(cfg, tmp_path, market)
    rig.start(T(MON, '10:30:00'))
    market.set(CE_MON, 158.0)
    rig.go(T(MON, '10:30:00'))
    day = rig.store.day(T(MON, '10:30:00').date())
    assert 'CE' in day['missed'] and not rig.broker.sent and not rig.store.pending()


def test_expiry_day_backstop_alert_and_settlement_when_flat(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.go(T(TUE, '09:25:01'))                                  # exit sent, never fills
    rig.go(T(TUE, '09:31:00'))
    assert any('EXPIRY DAY BACKSTOP' in s for s in rig.sent)
    rig.go(T(TUE, '15:39:00'))
    # Wednesday: the DAY sell was mass-cancelled at the close (the fake does
    # that), the exchange settled the option, the broker shows flat.
    rig.broker.ext_positions[CE_NEO] = 0
    market.closes[T(TUE, '00:00:00').date()] = 22900.0
    rig.restart()
    rig.start(T(WED, '09:00:00'))
    rig.go(T(WED, '09:15:05'))
    p = state(rig)
    assert p['status'] == 'closed' and p['exit_reason'] == 'expired_settled' and p['exit_price'] == 50.0


def test_expired_but_still_at_the_broker_waits(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(WED, '09:00:00'))
    rig.go(T(WED, '09:15:05'))
    assert state(rig)['status'] == 'open' and any('Waiting for settlement' in s for s in rig.sent)


# ------------------------------------------------------------ quote loss
def test_quote_outage_does_not_stop_order_servicing(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    market.fail = True
    rig.broker.hold_fills = False
    rig.go(T(MON, '10:00:01'))
    assert state(rig)['status'] == 'open'                       # the fill was seen on NEO
    market.fail = False


# ------------------------------------------------------- paper untouched
def test_paper_book_is_byte_identical_with_or_without_a_live_process(cfg, tmp_path, market):
    """The same script through paper alone, then paper beside live: paper's
    book must not change by one byte, and engine.py must not know live exists."""
    src = (Path(__file__).resolve().parents[1] / 'nifty_btst' / 'engine.py').read_text()
    for name in ('live_engine', 'broker', 'neo_client', 'journal', 'live_config'):
        assert ('.%s import' % name) not in src and ('import %s' % name) not in src

    def script(paper: Engine, live=None):
        for when, prices in [(T(MON, '09:15:30'), {}), (T(MON, '09:20:01'), {}),
                             (T(MON, '10:00:00'), {CE_MON: (153.5, 153.3, 153.8)}),
                             (T(MON, '10:00:03'), {}), (T(MON, '15:29:58'), {CE_MON: (180, 179.9, 180.1)}),
                             (T(TUE, '09:15:05'), {CE_MON: (210, 209.5, 210.5)}),
                             (T(TUE, '09:25:01'), {}), (T(TUE, '09:25:04'), {})]:
            for s, (ltp, bid, ask) in prices.items():
                market.set(s, ltp, bid=bid, ask=ask)
            go(paper, when)
            if live is not None:
                live.go(when)

    default_premiums(market, '2026-10-27')
    ref = Store(tmp_path / 'ref')
    script(Engine(cfg, ref, market, NullNotifier()))
    ref.save()
    market.books.clear()
    default_premiums(market, '2026-10-27')
    paper = Store(tmp_path / 'state')
    rig = Rig(cfg, tmp_path, market)
    rig.start(T(MON, '09:00:00'))
    script(Engine(cfg, paper, market, NullNotifier()), rig)
    paper.save()
    assert (tmp_path / 'state' / 'book.json').read_bytes() == (tmp_path / 'ref' / 'book.json').read_bytes()
    assert ref.closed_positions()[0]['exit_price'] == 209.5
    assert rig.store.closed_positions()[0]['exit_price'] == 209.5
    assert not (tmp_path / 'state' / 'intents.jsonl').exists()
    assert not isinstance(paper, LiveStore)


def test_live_digest_mentions_kill_and_sends(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig)
    rig.go(T(MON, '10:00:01'))
    text = rig.engine.eod_digest(T(MON, '15:45:00').date())
    assert 'LIVE (live)' in text and 'sends today 1 (entries 1)' in text and 'BOUGHT' in text
