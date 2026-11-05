"""One regression test per finding of docs/NEO_REVIEW_1.md, each built from
the reviewer's own reproduction and asserting the FIXED behaviour. Findings
whose regression lives in another file are named there in the docstring.

Covered elsewhere:
  NEO-03/11/25/34 -> tests/test_neo_client.py (fail-closed envelopes, invalid
                     session body, persistent 403, transient 1005)
  NEO-07/08/09/12/13 -> tests/test_live_cli.py (PROBE_ARM vs ARM+PASS, token-
                     optional start, startup exceptions, persisted fake, paper
                     isolation from live keys)
  NEO-17 (CLI side) -> tests/test_live_cli.py; engine side below
  NEO-19/20/24/33 -> tests/test_probe_safety.py
  NEO-23 -> tests/test_broker.py (the hostile fake) and every test here
  NEO-36 -> `mypy --strict nifty_btst` is clean for the new modules
"""
import json
import logging
from datetime import timedelta

import requests

from conftest import LOT, T, default_premiums
from live_helpers import MON, TUE, neo_symbol
from nifty_btst import report
from nifty_btst.journal import Journal
from nifty_btst.live_engine import ENTRY_OPEN, ENTRY_SENT, EXIT_SENT, RESUME_FILE
from nifty_btst.notify import Notifier
from nifty_btst.ticks import round_tick
from test_live_engine import CE_MON, CE_NEO, PE_MON, holding, marked, state, trigger_ce


def sells(rig):
    return [r for r in rig.broker.sent if r.side == 'S']


def buys(rig):
    return [r for r in rig.broker.sent if r.side == 'B']


# ---------------------------------------------------------------- NEO-01
def test_neo01_late_visible_exit_never_doubles_and_never_goes_short(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'unknown_grace_sec': 120})
    holding(rig)
    rig.broker.visibility_lag_sec = 30.0
    rig.broker.place_faults = ['timeout_reached']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    for i in range(1, 29, 3):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
        assert len(sells(rig)) == 1 and state(rig)['live']['state'] == EXIT_SENT
    rig.go(T(MON, '11:00:31'))                                      # the first SELL shows up
    p = state(rig)
    assert p['status'] == 'closed' and len(sells(rig)) == 1
    assert rig.broker.true_net(CE_NEO) == 0
    rig.engine._reconcile(T(MON, '11:01:00'), startup=False)
    assert rig.engine.reconcile_ok and not rig.store.kill()['tripped']


def test_neo01_a_tag_written_off_that_later_appears_trips_the_kill_loud(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'unknown_grace_sec': 60})
    holding(rig)
    rig.broker.visibility_lag_sec = 600.0                           # beyond the grace
    rig.broker.positions_lag_sec = 600.0
    rig.broker.place_faults = ['timeout_reached']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:30'))
    rig.go(T(MON, '11:01:01'))                                      # written off, re-sent
    assert any(ln['ev'] == 'NOT_PLACED' for ln in rig.journal.lines())
    assert len(sells(rig)) == 2
    rig.broker.hold_fills = True
    rig.go(T(MON, '11:10:05'))                                      # the first appears
    rig.engine._reconcile(T(MON, '11:10:06'), startup=False)
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'LATE ARRIVAL' in k['reason']
    rig.service(T(MON, '11:11:10'))
    assert any('KILL (LOUD)' in s for s in rig.sent)


# ---------------------------------------------------------------- NEO-02
def test_neo02_late_visible_entry_never_double_buys(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'unknown_grace_sec': 120})
    rig.broker.visibility_lag_sec = 30.0
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    for i in range(1, 29, 3):
        rig.go(T(MON, '10:00:00') + timedelta(seconds=i))
        assert len(buys(rig)) == 1 and state(rig)['live']['state'] == ENTRY_SENT
    rig.go(T(MON, '10:00:31'))
    p = state(rig)
    assert p['status'] == 'open' and len(buys(rig)) == 1
    assert rig.broker.true_net(CE_NEO) == LOT
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    rig.go(T(MON, '11:00:31'))                                      # the SELL shows after the lag
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


def test_neo02_buy_guard_refuses_while_a_buy_is_live_or_the_broker_holds_more(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    from nifty_btst.broker import OrderRequest
    rig.broker.hold_fills = True
    rig.broker.place(OrderRequest(CE_NEO, 't', 'B', LOT, '150.00', 'manual'))
    trigger_ce(rig)
    assert len(buys(rig)) == 1 and any('BUY REFUSED' in s and 'another BUY' in s for s in rig.sent)
    rig2 = marked(cfg, tmp_path / 'b', market)
    rig2.broker.ext_positions[CE_NEO] = LOT                          # an extra lot nobody tracks
    trigger_ce(rig2)
    assert not buys(rig2) and not rig2.store.kill()['tripped']       # R3-05: one read reports, no buy
    rig2.engine._reconcile(T(MON, '10:00:03'), startup=False)        # the second read confirms
    assert not buys(rig2) and rig2.store.kill()['tripped']
    assert 'EXTRA LOT' in rig2.store.kill()['reason'] or 'UNEXPLAINED LONG' in rig2.store.kill()['reason']


# ---------------------------------------------------------------- NEO-04
def test_neo04_blank_book_never_drops_a_live_exit_leg(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.hold_fills = True
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert len(sells(rig)) == 1
    rig.broker.blank_book = True
    for i in range(1, 600, 30):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
        assert len(sells(rig)) == 1
    rig.broker.blank_book = False
    live = [o for o in rig.broker.order_book() if o.side == 'S' and o.live]
    assert len(live) == 1
    rig.broker.hold_fills = False
    rig.go(T(MON, '11:10:05'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


# ---------------------------------------------------------------- NEO-05
def test_neo05_short_or_untracked_negative_net_trips_the_kill_loud(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.set_external_position(neo_symbol(PE_MON), -LOT)       # a short nobody tracks
    rig.engine._reconcile(T(MON, '11:00:00'), startup=False)
    assert not rig.store.kill()['tripped'] and not rig.engine.reconcile_ok   # R3-20: one read reports
    rig.engine._reconcile(T(MON, '11:00:03'), startup=False)
    k = rig.store.kill()
    assert k['tripped'] and k['loud'] and 'SHORT' in k['reason'] and not rig.engine.reconcile_ok
    assert not rig.broker.sent                                       # never repaired by trading


# ---------------------------------------------------------------- NEO-06
def test_neo06_entry_filled_during_an_outage_is_adopted_next_morning_and_exited(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)                                                  # ENTRY_OPEN, then the box dies
    rig.broker.hold_fills = False
    rig.broker.order_book()                                          # it fills while nobody looks
    assert rig.broker.true_net(CE_NEO) == LOT
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))                                    # the DAY book no longer lists it
    rig.go(T(TUE, '09:15:30'))
    p = state(rig)
    assert p['status'] == 'open' and p['entry_price'] == 153.8       # adopted from the trade book
    assert not rig.store.kill()['tripped']
    rig.go(T(TUE, '09:25:01'))
    rig.go(T(TUE, '09:25:03'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


def test_neo06_a_leg_with_unknown_fill_is_never_abandoned(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig)
    rig.broker.blank_book = True
    rig.broker.read_faults = []
    rig.go(T(MON, '15:26:00'))                                       # past entry_end
    p = state(rig)
    assert p['status'] == 'pending' and p['live']['state'] == ENTRY_OPEN


# ---------------------------------------------------------------- NEO-10
def test_neo10_digest_and_report_survive_abandoned_and_rejected_entries(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, armed=False)
    trigger_ce(rig)
    rig.go(T(MON, '15:26:00'))
    p = state(rig)
    assert p['exit_reason'] == 'entry_abandoned' and p['never_filled'] and p['net_pnl'] == 0.0
    text = rig.engine.eod_digest(T(MON, '15:45:00').date())
    assert 'never filled today' in text
    rig.engine.eod_digest(T(TUE, '15:45:00').date())
    assert report.render(rig.store.closed_positions(), rig.store.holding()).startswith('No closed')
    assert rig.store.closed_positions() == [] and len(rig.store.never_filled()) == 1


# ---------------------------------------------------------------- NEO-14
def test_neo14_wide_book_does_not_block_a_due_stop(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.market.set(CE_MON, 70.0, bid=55.0, ask=85.0)
    rig.go(T(MON, '11:00:00'))
    assert sells(rig) and sells(rig)[0].price == '55.00'
    assert any(json.loads(ln).get('kind') == 'WIDE_BOOK'
               for ln in open(rig.dir / 'events_20261026.jsonl'))


def test_neo14_no_bid_defers_with_an_alert_every_minute(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.market.set(CE_MON, 70.0, bid=0.0, ask=85.0)
    for i in range(0, 300, 3):
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    assert not sells(rig) and len([s for s in rig.sent if 'no usable bid' in s]) >= 5


# ---------------------------------------------------------------- NEO-15
def test_neo15_quote_outage_across_0925_latches_the_exit_and_alerts_each_minute(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    market.fail = True
    for i in range(0, 600, 3):
        rig.go(T(TUE, '09:20:00') + timedelta(seconds=i))
    p = state(rig)
    # R3-10: quotes out and nothing SHOWN trading today: the exit is not
    # latched; the owner hears why each minute. The moment a book trades the
    # day is proven and the 09:25 rule applies at once.
    assert p['live'].get('exit_reason') is None and p['live']['state'] == 'FILLED'
    assert len([s for s in rig.sent if 'NO QUOTES' in s]) >= 8
    assert len([s for s in rig.sent if 'nothing has traded today' in s]) >= 4
    market.fail = False
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.go(T(TUE, '09:30:03'))
    assert sells(rig) and state(rig)['live']['exit_reason'] in ('next_day_0925', 'next_day_late')


# ---------------------------------------------------------------- NEO-16
def test_neo16_no_row_refuses_the_sell_names_the_owner_command_and_never_trips(cfg, tmp_path, market):
    """Rewritten for R2-01/03: no row is UNKNOWN, not a mismatch and not a
    licence to sell. The owner's confirmation is the positive source."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.carry_forward_rows = False
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    for i in range(0, 120, 3):
        rig.go(T(TUE, '09:25:00') + timedelta(seconds=i))
    assert not sells(rig) and not rig.store.kill()['tripped'] and not rig.engine.reconcile_ok
    assert any('SELL REFUSED' in s and 'live-confirm-held' in s for s in rig.sent)
    assert any('NO POSITION ROW' in s for s in rig.sent)
    rig.engine.confirm_held(1, T(TUE, '09:27:00'))
    rig.go(T(TUE, '09:27:01'))
    assert len(sells(rig)) == 1
    rig.go(T(TUE, '09:27:02'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


def test_neo16_holdings_is_a_positive_source_when_enabled(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market, neo_overrides={'holdings_source_enabled': True})
    holding(rig)
    rig.broker.carry_forward_rows = False
    rig.broker.holdings_source = {CE_NEO: LOT}
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.restart()
    rig.start(T(TUE, '09:00:00'))
    rig.go(T(TUE, '09:25:01'))
    assert len(sells(rig)) == 1
    rig.go(T(TUE, '09:25:02'))
    assert state(rig)['status'] == 'closed' and rig.broker.true_net(CE_NEO) == 0


# ---------------------------------------------------------------- NEO-17
def test_neo17_resume_request_file_is_applied_by_the_running_loop(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.engine._trip(T(MON, '10:00:00'), 'test')
    (rig.dir / RESUME_FILE).write_text(json.dumps({'action': 'resume', 'date': MON}))   # stamped (R3-02)
    rig.service(T(MON, '10:00:01'))
    k = rig.store.kill()
    assert not k['tripped'] and not (rig.dir / RESUME_FILE).exists()
    assert any('KILL CLEARED' in s for s in rig.sent)
    assert not json.loads((rig.dir / 'book.json').read_text())['kill']['tripped']


# ---------------------------------------------------------------- NEO-18
def test_neo18_removing_the_arm_disarms_a_running_process(cfg, tmp_path, market):
    armed = {'until': T(MON, '00:00:00').date() + timedelta(days=5)}
    rig = marked(cfg, tmp_path, market)
    rig.engine._arm_reader = lambda today: armed['until']
    assert rig.engine.armed_today(T(MON, '10:00:00').date(), T(MON, '10:00:00'))
    armed['until'] = None                                           # the owner deleted ARM
    assert rig.engine.armed_today(T(MON, '10:00:30').date(), T(MON, '10:00:30'))   # cached
    assert not rig.engine.armed_today(T(MON, '10:01:01').date(), T(MON, '10:01:01'))
    trigger_ce(rig, when=T(MON, '10:02:00'))
    assert not buys(rig) and any('not armed' in s for s in rig.sent)


# ---------------------------------------------------------------- NEO-21
def test_neo21_probe_sends_do_not_count_against_the_strategy(tmp_path):
    j = Journal(tmp_path)
    t = T(MON, '10:00:00')
    j.intent(t, '20261026-0-ENTRY-1', 'ENTRY', 0, 'S', 'B', 65, '1.00', probe=True)
    j.intent(t, '20261026-1-ENTRY-1', 'ENTRY', 1, 'S', 'B', 65, '1.00')
    assert j.sends_today(t.date()) == 1 and j.sends_today(t.date(), include_probe=True) == 2


# ---------------------------------------------------------------- NEO-22
def test_neo22_live_poll_is_its_own_key():
    from nifty_btst.live_config import build_neo
    from live_helpers import neo_raw
    assert build_neo(neo_raw(poll_seconds=5)).poll_seconds == 5


# ---------------------------------------------------------------- NEO-26
def test_neo26_closed_day_with_a_holding_keeps_watching_and_exits(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.engine.session_expected = False
    rig.start(T(TUE, '09:00:00'))
    market.stale = set(market.books)
    rig.go(T(TUE, '09:20:01'))
    assert rig.engine.closed_today == T(TUE, '09:20:01').date()
    market.stale = set()
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.go(T(TUE, '09:25:01'))
    assert sells(rig) and any('marked CLOSED' in s for s in rig.sent)


# ---------------------------------------------------------------- NEO-27
def test_neo27_reconcile_sums_by_symbol(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    holding(rig)
    rig.broker.ext_positions[CE_NEO] = LOT                           # only ONE lot at the broker
    rig.engine._reconcile(T(MON, '11:00:00'), startup=False)
    assert not rig.engine.reconcile_ok and not rig.store.kill()['tripped']   # first read: deferred
    rig.engine._reconcile(T(MON, '11:05:00'), startup=False)
    assert not rig.engine.reconcile_ok and rig.store.kill()['tripped']


# ---------------------------------------------------------------- NEO-28
def test_neo28_torn_line_does_not_swallow_the_next_intent(tmp_path):
    j = Journal(tmp_path)
    t = T(MON, '10:00:00')
    with open(j.path, 'a') as f:
        f.write('{"ev": "PLACED", "tag": "x')
    j.intent(t, '20261026-1-EXIT-1', 'EXIT', 1, 'S', 'S', 65, '10.00')
    assert [u['tag'] for u in j.unresolved()] == ['20261026-1-EXIT-1']
    assert j.sends_today(t.date()) == 1


# ---------------------------------------------------------------- NEO-29
def test_neo29_buy_rounds_up_sell_rounds_down(cfg, tmp_path, market):
    assert round_tick(149.98, 0.05, 'down') == 149.95 and round_tick(150.02, 0.05, 'up') == 150.05
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig, ltp=153.5, bid=153.3, ask=153.82)               # off-grid ask
    assert buys(rig)[0].price == '153.85'
    holding(rig, symbol=PE_MON, side='PE', strike=22700.0)
    rig.market.set(PE_MON, 70.0, bid=69.83, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert sells(rig)[0].price == '69.80'


# ---------------------------------------------------------------- NEO-30
def test_neo30_stop_fires_on_the_bid_as_well_as_on_ltp(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig, entry_price=150.0)                                  # stop 75
    rig.market.set(CE_MON, 80.0, bid=74.9, ask=80.5)                 # ltp above, bid through
    rig.go(T(MON, '11:00:00'))
    assert state(rig)['live']['exit_reason'] == 'stop' and sells(rig)
    rig2 = marked(cfg, tmp_path / 'b', market)
    holding(rig2, entry_price=150.0)
    rig2.market.set(CE_MON, 80.0, bid=74.9, ask=0.0)                 # one-sided: not a stop
    rig2.go(T(MON, '11:00:00'))
    assert state(rig2)['live'].get('exit_reason') is None


# ---------------------------------------------------------------- NEO-32
def test_neo32_settle_expired_is_retried_within_one_process(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.ext_positions[CE_NEO] = 0
    default_premiums(market, '2026-11-03')
    market.closes[T(TUE, '00:00:00').date()] = 22900.0
    rig.restart()
    rig.start(T('2026-10-28', '09:00:00'))
    rig.broker.read_faults = ['unavailable'] * 3                     # reconcile, settle, its retry
    rig.go(T('2026-10-28', '09:15:05'))                             # every read this tick fails
    assert state(rig)['status'] == 'open' and any('broker unreadable' in s for s in rig.sent)
    rig.go(T('2026-10-28', '09:16:10'))                             # retried a minute later
    assert state(rig)['status'] == 'closed' and state(rig)['exit_reason'] == 'expired_settled'


# ---------------------------------------------------------------- NEO-35
def test_neo35_telegram_failure_never_logs_the_bot_token(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.ERROR)
    tg = tmp_path / 'tg.json'
    tg.write_text(json.dumps({'bot_token': '123456:SECRET', 'chat_id': 'c'}))

    def post(url, **kw):
        raise requests.ConnectionError('HTTPSConnectionPool(host=%s)' % url)
    monkeypatch.setattr(requests, 'post', post)
    assert Notifier(tg, True, 'P').send('x') is False
    assert not any('SECRET' in r.getMessage() for r in caplog.records)
    assert any('ConnectionError' in r.getMessage() for r in caplog.records)
