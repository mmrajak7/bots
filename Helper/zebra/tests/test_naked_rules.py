"""The naked long's exit rules -- the ONE copy the shadow and live both run.

`zebra/naked_rules.py`, design Q1. Equivalence with the pre-refactor shadow
was proven on 2026-11-26 by a golden replay (152 stored positions, 36,052
polls, 394 arm exits; 3,000 fuzz cases, 14,568 exits; zero state mismatches;
a planted `<=` -> `<` change was caught). These tests pin each rule directly
so a later edit cannot change one silently.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_rules.py -v
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg              # noqa: E402
from zebra import naked_rules as nr          # noqa: E402
from zebra import outcomes                   # noqa: E402
from zebra import structure_shadow as ss     # noqa: E402

EV = 40.0                                     # entry value
STOP = EV * 0.50
TGT_CE = 1030.0
DAY = datetime(2026, 12, 2, 11, 0, 0)         # a Wednesday, well past the buffer


def _step(a, *, v, spot=1000.0, now=DAY, left=20, direction='CE', target=TGT_CE,
          take_tp=True, stop_frac=0.50, value_armed=None):
    if value_armed is None:
        value_armed = not nr.within_open_buffer(now)
    return nr.step(a, take_tp=take_tp, stop_frac=stop_frac, v=v, spot=spot,
                   direction=direction, target=target, now=now,
                   today=now.date(), left=left, value_armed=value_armed)


def fresh():
    return {'entry_value': EV}


# ── TP ──────────────────────────────────────────────────────────────────────

def test_tp_books_at_the_bid_when_spot_reaches_the_target():
    assert _step(fresh(), v=55.0, spot=TGT_CE) == ('tp', 55.0)


def test_pe_tp_is_spot_at_or_below_the_target():
    a = fresh()
    assert _step(a, v=55.0, spot=971.0, direction='PE', target=970.0) is None
    assert _step(a, v=55.0, spot=970.0, direction='PE', target=970.0) == ('tp', 55.0)


def test_tp_is_not_dark_in_the_opening_buffer():
    open_poll = DAY.replace(hour=9, minute=16)
    assert nr.within_open_buffer(open_poll)
    assert _step(fresh(), v=55.0, spot=TGT_CE, now=open_poll) == ('tp', 55.0)


def test_unpriced_tp_latches_and_books_at_the_next_price_even_off_target():
    a = fresh()
    assert _step(a, v=None, spot=TGT_CE) is None
    assert a['pending']['reason'] == 'tp'
    later = DAY + timedelta(minutes=5)
    assert _step(a, v=50.0, spot=1000.0, now=later) == ('tp', 50.0)


def test_the_tp_latch_lapses_at_the_end_of_its_session():
    a = fresh()
    _step(a, v=None, spot=TGT_CE)
    nxt = DAY + timedelta(days=1)
    assert _step(a, v=50.0, spot=1000.0, now=nxt) is None   # back to live spot test
    assert 'pending' not in a and a['tp_latch_expired']


def test_an_arm_without_a_target_ignores_spot():
    assert _step(fresh(), v=55.0, spot=TGT_CE, take_tp=False) is None


# ── STOP ────────────────────────────────────────────────────────────────────

def test_stop_needs_the_configured_number_of_consecutive_breaches():
    a, now = fresh(), DAY
    for i in range(cfg.DEBIT_SL_CONFIRM_POLLS - 1):
        assert _step(a, v=STOP, now=now) is None
        now += timedelta(minutes=5)
    assert _step(a, v=STOP, now=now) == ('stop', STOP)


def test_stop_is_at_or_below_the_fraction_never_above():
    a = fresh()
    for k in range(5):
        assert _step(a, v=STOP + 0.01, now=DAY + timedelta(minutes=5 * k)) is None


def test_a_recovery_resets_the_streak():
    a = fresh()
    _step(a, v=STOP, now=DAY)
    _step(a, v=STOP + 5, now=DAY + timedelta(minutes=5))
    assert _step(a, v=STOP, now=DAY + timedelta(minutes=10)) is None


def test_an_unusable_price_freezes_the_streak_not_resets_it():
    a = fresh()
    _step(a, v=STOP, now=DAY)
    assert _step(a, v=None, now=DAY + timedelta(minutes=5)) is None
    if cfg.DEBIT_SL_CONFIRM_POLLS == 2:
        assert _step(a, v=STOP, now=DAY + timedelta(minutes=10)) == ('stop', STOP)


def test_a_streak_older_than_the_stale_window_restarts():
    assert cfg.DEBIT_SL_CONFIRM_POLLS >= 2      # the rule under test needs a pair
    a = fresh()
    _step(a, v=STOP, now=DAY)
    late = DAY + timedelta(seconds=cfg.CONFIRM_STALE_SEC + 1)
    assert _step(a, v=STOP, now=late) is None
    assert a['confirm']['stop']['n'] == 1       # restarted, not continued


def test_a_breach_at_the_close_cannot_pair_with_the_next_open():
    a = fresh()
    _step(a, v=STOP, now=DAY.replace(hour=15, minute=25))
    nxt = (DAY + timedelta(days=1)).replace(hour=9, minute=35)
    assert _step(a, v=STOP, now=nxt) is None


def test_the_stop_is_dark_in_the_opening_buffer():
    a = fresh()
    t0 = DAY.replace(hour=9, minute=16)
    for k in range(4):
        assert _step(a, v=1.0, now=t0 + timedelta(minutes=3 * k)) is None


def test_an_arm_without_a_stop_never_stops():
    a = fresh()
    for k in range(5):
        assert _step(a, v=0.5, now=DAY + timedelta(minutes=5 * k), stop_frac=None) is None


# ── TIME ────────────────────────────────────────────────────────────────────

def test_time_fires_on_the_calendar_and_books_after_the_buffer():
    assert _step(fresh(), v=30.0, left=cfg.TIME_SL_DAYS) == ('time', 30.0)
    assert _step(fresh(), v=30.0, left=cfg.TIME_SL_DAYS + 1) is None


def test_time_is_held_through_the_opening_buffer():
    a = fresh()
    t0 = DAY.replace(hour=9, minute=16)
    assert _step(a, v=30.0, left=cfg.TIME_SL_DAYS, now=t0) is None
    assert a['pending']['reason'] == 'time'
    assert _step(a, v=30.0, left=cfg.TIME_SL_DAYS, now=DAY) == ('time', 30.0)


def test_a_pending_time_never_blocks_a_tp():
    a = fresh()
    _step(a, v=None, left=cfg.TIME_SL_DAYS)
    assert _step(a, v=60.0, spot=TGT_CE, left=cfg.TIME_SL_DAYS,
                 now=DAY + timedelta(minutes=5)) == ('tp', 60.0)


def test_past_expiry_with_no_price_books_unpriced_rather_than_stay_open():
    assert _step(fresh(), v=None, left=0) == ('time', None)


def test_unknown_sessions_left_never_fires_time():
    assert _step(fresh(), v=30.0, left=None) is None


# ── wiring ──────────────────────────────────────────────────────────────────

def test_the_shadow_runs_these_rules_for_its_plain_naked_arms():
    assert set(ss.NAKED_RULE_ARMS) == {'naked_long', 'naked_hold', 'naked_runner'}
    assert ss._confirmed is nr.confirmed and ss._tp_hit is nr.tp_hit


def test_the_naked_stop_is_a_value_stop_and_time_is_not():
    assert outcomes.is_value_stop('stop')
    assert outcomes.is_value_stop('paper:debit_sl')
    assert not outcomes.is_value_stop('time')
    assert not outcomes.is_value_stop('tp')
    assert not outcomes.is_value_stop('already_flat_stop')
    assert not outcomes.is_value_stop('something_new')
    assert outcomes.is_stop_exit('time')        # the arming gate is unchanged


def test_sessions_left_is_none_for_garbage():
    assert nr.sessions_left('not-a-date', date(2026, 12, 2)) is None
