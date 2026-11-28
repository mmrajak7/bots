"""Pure rules (docs/EXECUTOR_SPEC.md §4-§6, §13): every boundary of every trigger."""
import os
import sys

import pytest

ETF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ETF)
from Tests import _muzzle  # noqa: E402,F401  (no Telegram / paper log from option_buy)
import option_buy as ob    # noqa: E402
ob._init_bridge()          # TF_TIME_SL, exactly as executor.import_option_buy() does
assert ob.TF_TIME_SL.get('10minute') == 30

from executor import config as C   # noqa: E402
from executor import rules as R    # noqa: E402

CFG = C.load(local_path=os.path.join(ETF, 'no_such_overlay.yaml'))


def q(bid, ask, ltp=None, ts=1000.0, bids=None, asks=None):
    return R.Quote(ltp=ltp if ltp is not None else bid, bid=bid, ask=ask,
                   bids=bids or [(bid, 1000)], asks=asks or [(ask, 1000)], ts=ts)


def pos(**kw):
    p = {'entry_premium': 100.0, 'peak_premium': 100.0, 'st_value': 110.0, 't2_value': 0.0,
         'cost_sl_active': False, 'cost_sl_level': 0, 'sl_reason': None, 'trail_level': 0.0,
         'floor_breach_since': None, 't1_hit': False, 'signal_tf': '10minute', 'opt_type': 'CE',
         'strike': 26000, 'entry_time': '2026-11-30T10:00:00+05:30', 'tick_size': 0.05}
    p.update(kw)
    return p


# -- quote validity (F13) --------------------------------------------------------------
@pytest.mark.parametrize('bid,ask,ok', [(10, 10.1, True), (0, 10, False), (10, 0, False),
                                        (10.2, 10.1, False), (10, 10, True)])
def test_quote_valid(bid, ask, ok):
    assert q(bid, ask).valid() is ok


def test_a_zero_bid_never_triggers_a_stop():
    p = pos(cost_sl_active=True, cost_sl_level=100.1)
    assert R.tick_exit(p, q(0, 50), 1000, CFG, ob) is None


# -- X2 TP on the bid --------------------------------------------------------------------
def test_tp_t1_only_on_bid_touch():
    p = pos()
    assert R.tick_exit(p, q(109.95, 110.5, ltp=110.2), 1000, CFG, ob) is None   # LTP is not enough
    assert R.tick_exit(p, q(110.0, 110.5), 1000, CFG, ob) == 'TP'


def test_tp_uses_t2_when_present():
    p = pos(t2_value=120.0)
    assert R.tick_exit(p, q(115, 115.5), 1000, CFG, ob) is None
    assert R.tick_exit(p, q(120, 120.5), 1000, CFG, ob) == 'TP'


# -- X4 / X5 -------------------------------------------------------------------------------
def test_prem_sl_at_minus_35_pct_on_the_bid():
    p = pos()
    assert R.tick_exit(p, q(65.05, 66), 1000, CFG, ob) is None or \
        R.tick_exit(p, q(65.05, 66), 1000, CFG, ob) == 'ADV_SL'
    assert R.tick_exit(pos(peak_premium=110), q(65.0, 66), 1000, CFG, ob) == 'PREM_SL'


def test_adv_backstop_needs_a_small_peak():
    assert R.tick_exit(pos(peak_premium=102.9), q(88.0, 88.5), 1000, CFG, ob) == 'ADV_SL'
    assert R.tick_exit(pos(peak_premium=102.9), q(88.05, 88.5), 1000, CFG, ob) is None
    assert R.tick_exit(pos(peak_premium=103.0), q(88.0, 88.5), 1000, CFG, ob) is None


# -- X3 floors with persistence (F14) -----------------------------------------------------
def test_floor_needs_5s_of_breach_and_resets_above():
    p = pos(cost_sl_active=True, cost_sl_level=100.10, sl_reason='COST_SL')
    assert R.tick_exit(p, q(100.10, 100.3), 1000.0, CFG, ob) is None
    assert p['floor_breach_since'] == 1000.0
    assert R.tick_exit(p, q(100.05, 100.3), 1004.9, CFG, ob) is None
    assert R.tick_exit(p, q(100.15, 100.3), 1005.0, CFG, ob) is None     # above: reset
    assert p['floor_breach_since'] is None
    assert R.tick_exit(p, q(100.05, 100.3), 1006.0, CFG, ob) is None
    assert R.tick_exit(p, q(100.05, 100.3), 1011.0, CFG, ob) == 'COST_SL'


def test_floor_breached_by_half_a_percent_exits_at_once():
    p = pos(cost_sl_active=True, cost_sl_level=100.0, sl_reason='T1_GIVEBACK')
    assert R.tick_exit(p, q(99.5, 99.7), 1000.0, CFG, ob) == 'T1_GIVEBACK'


def test_trail_binds_when_above_the_armed_floor():
    p = pos(peak_premium=120.0, cost_sl_active=True, cost_sl_level=100.1, sl_reason='COST_SL')
    p['trail_level'] = R.trail_level(p, ob)
    assert p['trail_level'] == pytest.approx(110.0)
    assert R.floor_level(p) == (pytest.approx(110.0), 'TRAIL')
    assert R.tick_exit(p, q(109.0, 109.5), 1000.0, CFG, ob) == 'TRAIL'


def test_trail_is_zero_below_min_gain():
    assert R.trail_level(pos(peak_premium=107.9), ob) == 0.0
    assert R.trail_level(pos(peak_premium=108.0), ob) == pytest.approx(104.0)


# -- X1 minute sample: arming kept, only TIME_SL / ADV_SL exits (F12) ---------------------
def test_minute_sample_takes_time_sl_and_ignores_a_floor_exit():
    import datetime as dt
    now = dt.datetime.fromisoformat('2026-11-30T10:31:00+05:30')
    p = pos(cost_sl_active=True, cost_sl_level=100.1, sl_reason='COST_SL')
    assert R.minute_sample('k', p, q(99, 99.5), now, ob._ladder, ob) == 'TIME_SL'
    now2 = dt.datetime.fromisoformat('2026-11-30T10:05:00+05:30')
    p2 = pos(cost_sl_active=True, cost_sl_level=100.1, sl_reason='COST_SL', peak_premium=104)
    assert R.minute_sample('k', p2, q(100.0, 100.3), now2, ob._ladder, ob) is None


def test_minute_sample_adv_sl_on_ltp():
    import datetime as dt
    now = dt.datetime.fromisoformat('2026-11-30T10:05:00+05:30')
    p = pos(peak_premium=101)
    assert R.minute_sample('k', p, q(91.9, 92.1, ltp=92.0), now, ob._ladder, ob) == 'ADV_SL'


def test_tick_arm_sets_giveback_floor_only():
    import datetime as dt
    now = dt.datetime.fromisoformat('2026-11-30T10:05:00+05:30')
    p = pos(t2_value=125.0)
    assert R.tick_arm('k', p, q(110.0, 110.2), now, ob._ladder) is True
    assert p['t1_hit'] and p['sl_reason'] == 'T1_GIVEBACK'
    assert p['cost_sl_level'] == pytest.approx(105.0)
    assert R.tick_arm('k', p, q(111.0, 111.2), now, ob._ladder) is False   # once


# -- entry (E2/E3) -------------------------------------------------------------------------
def test_entry_cap_is_the_tightest_of_three():
    # slip: max(3 ticks, 0.3%) of 100 = 0.30 -> 100.30; ST 110 x 0.95 = 104.5; ST-3 = 107
    assert R.entry_cap(100.0, 110.0, 0.05, CFG, ob) == pytest.approx(100.30)
    # ST close: 103 x 0.95 = 97.85 below the signal -> cap 97.85
    assert R.entry_cap(100.0, 103.0, 0.05, CFG, ob) == pytest.approx(97.85)


def test_entry_limit_walks_depth_and_respects_the_cap():
    qq = q(99.9, 100.0, asks=[(100.0, 30), (100.05, 30), (100.1, 100)])
    assert R.entry_limit(qq, 65, 100.30, 0.05) == pytest.approx(100.10)
    assert R.entry_limit(qq, 65, 100.05, 0.05) == pytest.approx(100.05)   # clipped
    assert R.entry_limit(q(99.9, 100.4), 65, 100.30, 0.05) is None        # ask above cap
    assert R.entry_limit(q(0, 100), 65, 100.30, 0.05) is None


# -- exit price schedule (X11 / F15) -----------------------------------------------------------
def test_exit_price_schedule():
    assert R.exit_price(100.0, 0, 0.05, CFG, False, first=99.95) == pytest.approx(99.95)
    assert R.exit_price(100.0, 1, 0.05, CFG, False) == pytest.approx(99.70)
    assert R.exit_price(100.0, 2, 0.05, CFG, False) == pytest.approx(99.40)
    assert R.exit_price(100.0, 3, 0.05, CFG, False) == pytest.approx(99.0)
    assert R.exit_price(100.0, 4, 0.05, CFG, False) == pytest.approx(98.0)
    assert R.exit_price(100.0, 9, 0.05, CFG, False) == pytest.approx(98.0)
    assert R.exit_price(100.0, 5, 0.05, CFG, True) == pytest.approx(96.0)  # stale: cumulative
    assert R.exit_price(0.04, 5, 0.05, CFG, True) == pytest.approx(0.05)   # never below a tick


def test_depth_walk():
    assert R.depth_walk([], 65) is None
    assert R.depth_walk([(10, 10), (9.9, 100)], 65) == 9.9
    assert R.depth_walk([(10, 10), (9.9, 10)], 65) == 9.9                   # thin: deepest


# -- config ------------------------------------------------------------------------------------
def test_config_refuses_bad_values(tmp_path):
    lp = str(tmp_path / 'x.yaml')
    with pytest.raises(C.ConfigError):
        C.load(local_path=lp, override={'mode': 'live'})                   # no live_until
    with pytest.raises(C.ConfigError):
        C.load(local_path=lp, override={'lots': 11})                       # above the ceiling
    with pytest.raises(C.ConfigError):
        C.load(local_path=lp, override={'product': 'CNC'})
    (tmp_path / 'y.yaml').write_text('mod: live\n')
    with pytest.raises(C.ConfigError, match='unknown keys'):
        C.load(local_path=str(tmp_path / 'y.yaml'))
    c = C.load(local_path=lp, override={'mode': 'live', 'live_until': '2026-12-31'})
    import datetime as dt
    assert C.live_allowed(c, dt.date(2026, 12, 31)) and not C.live_allowed(c, dt.date(2027, 1, 1))


def test_time_sl_is_not_masked_by_an_ltp_tp_print():
    """Traceability review: _ladder returns TP first on an LTP print at/above the target;
    the minute sample discards TP (the bid rule decides) -- TIME_SL must still fire."""
    import datetime as dt
    now = dt.datetime.fromisoformat('2026-11-30T10:31:00+05:30')
    p = pos()
    assert R.minute_sample('k', p, q(109.5, 110.5, ltp=110.2), now, ob._ladder, ob) == 'TIME_SL'
    assert p['exit_detail']['how'] == 'minute_ltp'
