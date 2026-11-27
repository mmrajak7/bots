"""B1 (owner 2026-11-27): other bots on the same NEO account must never move this book.

ETF_PI trades the SAME NIFTY weekly contracts intraday (MIS) from 2 Dec; zebra
later. Before the fix, one ETF_PI MIS round trip on BTST's strike read as an
unexplained foreign SELL: BTST's stop and 09:25 SELL were refused all day, a
working ETF_PI SELL made BTST pull its own, and a later ETF_PI sale summed into
a false SHORT kill. The engine now sees only `in_scope` rows (broker.ScopedBroker).
"""
import json
from datetime import timedelta

from conftest import LOT, T
from live_helpers import MON
from nifty_btst.broker import OrderView, ScopedBroker, in_scope
from nifty_btst.live_engine import EXIT_OPEN
from test_live_engine import CE_MON, CE_NEO, holding, marked, state


def _row(tag='', prod='NRML', side='S'):
    return OrderView(order_no='1', status='complete', symbol=CE_NEO, side=side, qty=LOT,
                     filled=LOT, avg_price=70.0, price=70.0, rej_reason='', tag=tag,
                     placed_at='26-Oct-2026 10:30:00', product=prod)


# ------------------------------------------------------------------ the rule
def test_other_products_are_out_of_scope():
    assert not in_scope(_row(prod='MIS'), 'NRML')
    assert in_scope(_row(prod='NRML'), 'NRML')


def test_rows_tagged_by_another_bot_are_out_of_scope():
    for tag in ('ENT_1127093012_001', 'TGT_1127093012_002', 'EXT_1127093012_003',
                'etf-1127093012-a1b1', 'zbr-1127093012-a1b2'):
        assert not in_scope(_row(tag=tag), 'NRML'), tag


def test_our_own_untagged_and_unknown_rows_stay_in_scope():
    # ours, a row with no tag, and a tag no bot uses (a human in the NEO app):
    # unknown fails CLOSED -- the owner's manual sale must keep blocking a double sell
    for tag in ('20261026-1-EXIT-1', '', 'manual', 'ENTRY', 'btst-1127093012-a1b1'):
        assert in_scope(_row(tag=tag), 'NRML'), tag


def test_a_row_with_no_product_is_judged_by_its_tag_alone():
    assert in_scope(_row(prod=''), 'NRML')
    assert not in_scope(_row(prod='', tag='EXT_1127093012_003'), 'NRML')


def test_scoped_broker_is_not_wrapped_twice_and_passes_attributes_through(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    assert isinstance(rig.engine.broker, ScopedBroker)
    assert rig.engine.broker.sent is rig.broker.sent             # the fake stays reachable


# ------------------------------------------------------- engine, end to end
def _plant(rig, side, status, tag, prod='MIS', filled=True, minute='10:30:00'):
    no = '9911%08d' % (len(rig.broker.orders) + 1)
    rig.broker.orders.append({
        'nOrdNo': no, 'ordSt': status, 'trdSym': CE_NEO, 'trnsTp': side, 'qty': str(LOT),
        'fldQty': str(LOT if filled else 0), 'unFldSz': '0' if filled else str(LOT),
        'avgPrc': '72.00' if filled else '0.00', 'prc': '72.00', 'rejRsn': '--',
        'GuiOrdId': tag, 'ordDtTm': '26-Oct-2026 %s' % minute, 'prod': prod, 'tok': 'tok',
        '_placed': T(MON, minute).isoformat(), '_filled_at': T(MON, minute).isoformat()})
    return no


def _sells_from_us(rig):
    return [r for r in rig.broker.sent if r.side == 'S' and r.tag.split('-')[2:3] == ['EXIT']]


def _events(rig, kind):
    return [e for e in (json.loads(ln) for ln in open(rig.dir / 'events_20261026.jsonl'))
            if e.get('kind') == kind]


def test_an_etf_pi_mis_round_trip_on_our_strike_does_not_refuse_our_exit(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    _plant(rig, 'B', 'complete', 'ENT_1026100000_001', minute='10:00:00')
    _plant(rig, 'S', 'complete', 'EXT_1026103000_002', minute='10:30:00')
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)             # through the 75 stop
    rig.go(T(MON, '11:00:00'))
    assert len(_sells_from_us(rig)) == 1, rig.sent
    assert state(rig)['live']['state'] == EXIT_OPEN
    assert not _events(rig, 'SELL_REFUSED')
    assert not rig.store.kill()['tripped']


def test_a_working_etf_pi_sell_does_not_block_or_pull_ours(cfg, tmp_path, market):
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.hold_fills = True
    _plant(rig, 'S', 'open', 'TGT_1026103000_003', filled=False)  # ETF_PI's resting target
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert len(_sells_from_us(rig)) == 1
    for i in (1, 2):              # inside exit_chase_sec (3 s): any cancel now is a pull
        rig.go(T(MON, '11:00:00') + timedelta(seconds=i))
    # ours was not pulled because of the other bot's order
    ours = [o for o in rig.broker.orders if o['GuiOrdId'].endswith('-EXIT-1')]
    assert ours and ours[0]['ordSt'] == 'open'
    assert not any('CANCELLING OURS' in s for s in rig.sent)


def test_a_shared_package_tag_is_ignored_even_in_our_product(cfg, tmp_path, market):
    # after K6 ETF_PI sends `etf-` tags; a NRML row of another bot is still not ours
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    rig.broker.hold_fills = True
    _plant(rig, 'S', 'open', 'etf-1026103000-a1b2', prod='NRML', filled=False)
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert len(_sells_from_us(rig)) == 1
    assert not _events(rig, 'SELL_REFUSED')
    assert not rig.store.kill()['tripped']


def test_negative_control_an_untagged_manual_sale_still_blocks_our_sell(cfg, tmp_path, market):
    # the shape the filter must NOT swallow: a sale nobody's bot sent
    rig = marked(cfg, tmp_path, market)
    holding(rig)
    rig.engine.reconcile_ok = True
    _plant(rig, 'S', 'complete', '', prod='NRML')
    rig.market.set(CE_MON, 70.0, bid=69.8, ask=70.2)
    rig.go(T(MON, '11:00:00'))
    assert not _sells_from_us(rig)


# ------------------------------------------- F2: unclear send, criteria match
def test_an_unclear_send_is_never_bound_to_a_row_with_a_different_tag(cfg, tmp_path, market):
    """NEO echoes our tag: a same symbol/side/qty row carrying ANOTHER tag (a
    human's app order, an unknown bot) is not our send, however close in time.
    The empty-tag fallback (`test_tag_not_echoed_falls_back_to_criteria`) stays."""
    from test_live_engine import trigger_ce
    from nifty_btst.live_engine import ENTRY_SENT
    rig = marked(cfg, tmp_path, market)
    rig.broker.place_faults = ['timeout_reached']
    trigger_ce(rig)
    rig.broker.orders[0]['GuiOrdId'] = 'manual-app-1'           # not ours, not empty
    rig.go(T(MON, '10:00:01'))
    p = state(rig)
    assert p['live']['state'] == ENTRY_SENT                      # still UNKNOWN, not bound
    assert not any(h.get('bound_by') for h in p['live'].get('leg_history') or [])
