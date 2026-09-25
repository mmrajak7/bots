"""One setup, one verdict a day.

On 2026-09-25 ICICIBANK PE weekly hovered at the 3% stale line against one ST
line (1,285.52). Each veto was followed by a stale cancel, the cancel freed the
dedup slot, the next scan re-added the same setup, and it was vetted again:
#614 14:17, #616 14:37, #617 14:57, #618 15:17 -- four agent runs, one question.
"""
from datetime import datetime

import pytest

from zebra import config as cfg
from zebra import scanner

TODAY = '2026-09-25'
LINE = 1285.52


def _rec(i, state='vetoed', day=TODAY, st=LINE, status='cancelled', **kw):
    t = {'id': i, 'stock': 'ICICIBANK', 'timeframe': 'weekly', 'direction': 'PE',
         'st_value': st, 'status': status, 'shadow_of': None,
         'vet': {'state': state, 'requested_at': day + 'T14:15:27',
                 'decided_at': day + 'T14:17:24'}}
    t.update(kw)
    return t


def test_the_icici_loop_is_recognised_as_one_setup():
    trades = [_rec(614), _rec(616), _rec(617)]
    hit = scanner.vetoed_today(trades, 'ICICIBANK', 'weekly', 'PE', LINE, TODAY)
    assert hit is not None and hit['id'] == 614


@pytest.mark.parametrize('trades,why', [
    ([_rec(1, day='2026-09-24')], 'a veto from yesterday: the agent gets a fresh look today'),
    ([_rec(1, st=1300.0)], 'a new ST line is a new setup'),
    ([_rec(1, state='allowed')], 'an ALLOWED setup is never blocked'),
    ([_rec(1, direction='CE')], 'the other direction is a different setup'),
    ([_rec(1, timeframe='monthly')], 'the other timeframe is a different setup'),
    ([_rec(1, stock='HDFCBANK')], 'another stock'),
    ([_rec(1, shadow_of=7)], 'a shadow record is never a signal'),
    ([dict(_rec(1), vet=None)], 'never vetted'),
])
def test_only_the_same_setup_vetoed_today_blocks(trades, why):
    assert scanner.vetoed_today(trades, 'ICICIBANK', 'weekly', 'PE', LINE, TODAY) is None, why


def test_float_noise_in_the_recomputed_line_is_still_the_same_line():
    trades = [_rec(1, st=LINE + 0.01)]
    assert scanner.vetoed_today(trades, 'ICICIBANK', 'weekly', 'PE', LINE, TODAY) is not None


def test_the_scan_does_not_re_add_a_setup_vetoed_today(tmp_path, monkeypatch, caplog):
    """The real `validate_and_add`: after a vetoed record went stale, the same
    setup coming back into the band is skipped, not re-added for a new vet."""
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    store = ZebraStore()
    t = store.add_signal({'stock': 'ICICIBANK', 'timeframe': 'weekly', 'direction': 'PE',
                          'st_value': LINE, 'st_direction': 'UP', 'signal_price': 1325.0,
                          'signal_gap_pct': 3.07})
    from zebra.vet import _now
    today = _now().date().isoformat()          # the vet module's own clock
    with store._mutate():
        r = store._must_find(t['id'])
        r['status'] = 'cancelled'
        r['cancel_reason'] = 'stale: gap 2.83% < 3.0%'
        r['vet'] = {'state': 'vetoed', 'requested_at': today + 'T14:15:27',
                    'decided_at': today + 'T14:17:24'}

    monkeypatch.setattr(scanner, 'run_all_scanners',
                        lambda: [{'stock': 'ICICIBANK', 'timeframe': 'weekly'}])
    monkeypatch.setattr(scanner, 'get_ltp', lambda kite, stocks: {'ICICIBANK': 1325.0})
    monkeypatch.setattr(scanner, 'compute_st_for_stock',
                        lambda kite, s, tf: {'st': LINE, 'direction': 'UP', 'atr': 20.0})
    monkeypatch.setattr(scanner, 'check_freshness', lambda *a, **k: (True, 'fresh'))
    import logging
    before = len(store.load_trades())
    with caplog.at_level(logging.DEBUG, logger=scanner.logger.name):
        added = scanner.validate_and_add(store, kite=object())
    assert added == [] and len(store.load_trades()) == before
    assert 'was VETOED today as #%d' % t['id'] in caplog.text     # skipped for THIS reason
    assert 'vetoed_today=1' in caplog.text                          # and counted on the summary

    # Control: the same scan on a DIFFERENT line is a new setup and is added.
    monkeypatch.setattr(scanner, 'compute_st_for_stock',
                        lambda kite, s, tf: {'st': 1290.0, 'direction': 'UP', 'atr': 20.0})
    monkeypatch.setattr(scanner, 'get_ltp', lambda kite, stocks: {'ICICIBANK': 1330.0})
    assert len(scanner.validate_and_add(store, kite=object())) == 1
