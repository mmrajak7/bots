"""Live engine edge paths: defers, the paper-mark comparison, a session dying
on a send, an extra lot at the broker, expiry with an unreadable broker."""
import json

from conftest import LOT, T, default_premiums
from live_helpers import MON, TUE, WED, Rig
from nifty_btst.live_engine import ENTRY_SENT, ENTRY_WAIT
from test_live_engine import CE_MON, CE_NEO, PE_MON, holding, marked, state, trigger_ce


def test_unreliable_book_at_trigger_defers_the_live_entry(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig, ltp=155.0, bid=0.0, ask=155.2)              # one-sided
    assert not rig.broker.sent and not rig.store.pending()
    assert rig.store.day(T(MON, '10:00:00').date())['marks']['CE']['entry_defers'] == 1
    trigger_ce(rig, when=T(MON, '10:00:03'), ltp=155.0, bid=154.9, ask=155.1)
    assert rig.broker.sent[0].price == '155.10'


def test_pending_entry_on_a_one_sided_book_defers(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.hold_fills = True
    trigger_ce(rig, when=T(MON, '10:10:00'))
    rig.go(T(MON, '10:10:06'))
    rig.market.set(CE_MON, 154.0, bid=0.0, ask=154.2)
    rig.go(T(MON, '10:10:07'))
    assert state(rig)['live']['state'] == ENTRY_WAIT and len(rig.broker.sent) == 1


def test_exit_due_on_a_book_that_has_not_traded_waits_latched(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    default_premiums(market, '2026-11-03')
    rig.market.set(CE_MON, 170.0, bid=169.8, ask=170.2)
    rig.market.stale = {CE_MON}
    rig.go(T(TUE, '09:25:01'))
    p = state(rig)
    # R3-10: nothing polled has traded today (the index spot carries no trade
    # time, the mark has no candle), so the day is not SHOWN trading: no
    # latch, and the owner hears why. The moment the book trades, the 09:25
    # rule applies at once.
    assert p['live']['state'] == 'FILLED' and p['live'].get('exit_reason') is None
    assert not rig.broker.sent and any('nothing has traded today' in s for s in rig.sent)
    rig.market.stale = set()
    rig.go(T(TUE, '09:25:04'))
    assert state(rig)['live']['exit_reason'] == 'next_day_0925'
    assert rig.broker.sent[0].price == '169.80'


def test_mark_differing_from_paper_is_reported(cfg, tmp_path, market):
    default_premiums(market, '2026-10-27')
    rig = Rig(cfg, tmp_path, market)
    paper = tmp_path / 'paper_book.json'
    paper.write_text(json.dumps({'days': {MON: {'mark_done': True, 'marks': {
        'CE': {'symbol': 'SOMETHING-ELSE'}, 'PE': {'symbol': PE_MON}}}}}))
    rig.engine.paper_book = paper
    rig.start(T(MON, '09:00:00'))
    rig.go(T(MON, '09:20:01'))
    diffs = [s for s in rig.sent if 'differs from PAPER' in s]
    assert len(diffs) == 1 and 'CE' in diffs[0]
    paper.write_text('garbage')                                 # unreadable: silently skipped
    rig.engine._compare_with_paper(T(MON, '09:20:02'), rig.store.day(T(MON, '09:20:02').date()))


def test_session_dying_on_a_send_is_unclear_and_marks_the_session_down(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['auth']
    trigger_ce(rig)
    p = state(rig)
    assert p['live']['state'] == ENTRY_SENT and not rig.engine.session_ok
    assert any('session dead' in s for s in rig.sent)


def test_extra_lot_at_the_broker_trips_the_kill_loud_and_is_never_sold(cfg, tmp_path, market):
    """Rewritten for NEO-05: an extra lot used to be an hourly NOTE with
    reconcile reading clean. Now it trips the kill, loud, and exits still
    sell only what the book holds."""
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.ext_positions[CE_NEO] = 2 * LOT
    rig.engine._reconcile(T(MON, '11:00:00'), startup=False)
    assert not rig.engine.reconcile_ok and not rig.store.kill()['tripped']   # R3-05: one read reports
    assert any('READ ONCE' in s for s in rig.sent)
    rig.engine._reconcile(T(MON, '11:00:03'), startup=False)
    k = rig.store.kill()
    assert not rig.engine.reconcile_ok and k['tripped'] and k['loud'] and 'EXTRA LOT' in k['reason']
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:03'))
    assert rig.broker.sent[0].qty == LOT and rig.broker.sent[0].side == 'S'
    rig.service(T(MON, '11:01:05'))
    assert any('KILL (LOUD)' in s for s in rig.sent)


def test_expired_position_with_unreadable_broker_or_no_close_is_not_booked(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.broker.ext_positions[CE_NEO] = 0
    default_premiums(market, '2026-11-03')
    rig.restart()
    rig.start(T(WED, '09:00:00'))
    rig.broker.read_faults = ['unavailable']
    rig.engine.settle_expired(T(WED, '09:15:05'))
    assert state(rig)['status'] == 'open' and any('broker unreadable' in s for s in rig.sent)
    rig.engine.settle_expired(T(WED, '09:30:05'))                # flat, but no NIFTY close known
    assert state(rig)['status'] == 'open' and any('no NIFTY close' in s for s in rig.sent)


def test_fill_without_an_average_price_uses_the_limit_and_flags_it(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    trigger_ce(rig)
    rig.broker.fill_price_override = 0.0
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['status'] == 'open' and p['entry_price'] == 153.8 and p['live']['avg_price_missing']
