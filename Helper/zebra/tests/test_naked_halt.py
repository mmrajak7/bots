"""The live naked entry halt: pause, kill, restart, fails closed.

`zebra/naked_halt.py`; design F16/F27/G11/G13/G14/G16/Q7; limits
`docs/NAKED_100_TRADE_TEST.md` section 11 and the section 4 pilot budget.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_halt.py -v
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import naked_halt as nh       # noqa: E402

CAP = 500000.0
T0 = datetime(2026, 12, 2, 11, 0, 0)      # Wednesday


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'NAKED_CAPITAL', CAP)
    monkeypatch.setattr(cfg, 'NAKED_KILL_DRAWDOWN_PCT', 25.0)
    monkeypatch.setattr(cfg, 'NAKED_SLIPPAGE_KILL_PCT', 3.0)
    monkeypatch.setattr(cfg, 'NAKED_SLIPPAGE_WINDOW', 20)
    monkeypatch.setattr(cfg, 'NAKED_LOSS_BUDGET', 0.0)
    monkeypatch.setattr(cfg, 'NAKED_PAUSE_STOPS', 4)
    monkeypatch.setattr(cfg, 'NAKED_PAUSE_WINDOW_SESSIONS', 3)
    monkeypatch.setattr(cfg, 'NAKED_PAUSE_SESSIONS', 2)
    # weekday-only sessions: these tests are about the halt, not the calendar
    monkeypatch.setattr('common.nse_holidays.is_holiday', lambda d: False)
    return tmp_path


_ids = iter(range(1000, 100000))


def rec(*, status='entered', debit=20.0, qty=1000, pnl_net=None, reason=None,
        exit_date=None, exit_time='12:00:00', rehearsal=False, ask=None,
        exit_debit=None, exit_touch=None, tid=None, entry_date='2026-12-01'):
    t = {'id': tid if tid is not None else next(_ids), 'structure': 'naked_long',
         'cohort': '2026-08-14', 'paper': False, 'rehearsal': rehearsal,
         'stock': 'X', 'status': status, 'debit': debit,
         'entry_long_price': debit, 'long_ask_entry': ask if ask is not None else debit,
         'quantity': qty, 'entry_date': entry_date, 'entry_time': '10:00:00'}
    if status == 'exited':
        t.update(pnl_net=pnl_net, pnl=pnl_net, exit_reason=reason or 'tp',
                 exit_date=exit_date or '2026-12-02', exit_time=exit_time,
                 exit_debit=exit_debit, exit_touch=exit_touch)
    return t


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def ev(trades, marks=None, now=T0, mode='live'):
    return nh.evaluate(trades, marks or {}, now, mode)


# ── the gate and the file ──────────────────────────────────────────────────

def test_a_fresh_book_is_clear():
    assert nh.entries_allowed('live', T0.date()) == (True, 'clear')
    r = ev([])
    assert r['allowed'] and r['equity'] == CAP and r['peak'] == CAP


def test_an_unreadable_file_halts_and_is_never_overwritten():
    p = nh.state_path('live')
    p.write_text('{not json', encoding='utf-8')
    ok, why = nh.entries_allowed('live', T0.date())
    assert not ok and 'fails closed' in why
    r = ev([])
    assert not r['allowed'] and 'fails closed' in r['reason']
    assert _read(p) == '{not json'


def test_a_wrong_shape_halts():
    nh.state_path('live').write_text(json.dumps({'schema': 1, 'killed': 'no',
                                                 'history': []}), encoding='utf-8')
    assert not nh.entries_allowed('live', T0.date())[0]


def test_deleting_the_file_does_not_restart_a_kill():
    nh.halt('live', 'review FAIL', T0, 'cli:test')
    nh.state_path('live').unlink()
    ok, why = nh.entries_allowed('live', T0.date())
    assert not ok and 'MISSING' in why


def test_after_a_resume_a_missing_file_is_a_fresh_book():
    code = nh.halt('live', 'x', T0, 'cli:test')['code']
    nh.resume('live', code, T0, 'cli:test')
    nh.state_path('live').unlink()
    assert nh.entries_allowed('live', T0.date())[0]


def test_rehearsal_and_live_never_share_a_file_or_a_book():
    nh.halt('rehearsal', 'drill', T0, 'cli:test')
    assert nh.entries_allowed('live', T0.date())[0]
    assert not nh.entries_allowed('rehearsal', T0.date())[0]
    live_loss = rec(status='exited', pnl_net=-200000.0)
    assert ev([live_loss], mode='rehearsal')['equity'] == CAP
    assert ev([rec(status='exited', pnl_net=-1.0, rehearsal=True)])['equity'] == CAP


def test_bcs_records_are_not_in_the_naked_book():
    bcs = {'id': 1, 'cohort': '2026-08-14', 'structure': 'bcs', 'status': 'exited',
           'pnl_net': -300000.0}
    assert ev([bcs])['equity'] == CAP


def test_bad_mode_raises():
    with pytest.raises(ValueError):
        nh.state_path('off')


# ── equity (G11) ─────────────────────────────────────────────────────────────

def test_equity_includes_open_marks_at_the_bid():
    t = rec(debit=20.0, qty=1000)
    r = ev([t], {str(t['id']): 15.0})
    assert r['equity'] == CAP - 5000


def test_a_missing_mark_uses_the_last_reliable_one_then_entry():
    t = rec(debit=20.0, qty=1000)
    ev([t], {str(t['id']): 12.0})
    r = ev([t], {}, now=T0.replace(minute=10))
    assert r['equity'] == CAP - 8000 and not r['unmarked']
    t2 = rec(debit=10.0)
    r = ev([t, t2], {}, now=T0.replace(minute=15))
    assert str(t2['id']) in r['unmarked']


def test_an_uncosted_close_counts_its_gross_and_says_so():
    t = rec(status='exited', pnl_net=None)
    t['pnl'] = -4000.0
    r = ev([t])
    assert r['equity'] == CAP - 4000 and str(t['id']) in r['uncosted']


# ── the drawdown kill ──────────────────────────────────────────────────────────

def test_drawdown_kill_trips_at_exactly_25pct_of_the_peak():
    win = rec(status='exited', pnl_net=100000.0)
    ev([win])                                           # peak 600,000
    t = rec(debit=100.0, qty=1500)                      # 150,000 at entry
    r = ev([win, t], {str(t['id']): 0.0}, now=T0.replace(minute=5))
    assert r['drawdown_pct'] == 25.0 and not r['allowed']
    assert r['events'][0]['event'] == 'kill' and r['events'][0]['kind'] == 'drawdown'
    assert r['events'][0]['code']


def test_just_under_the_drawdown_limit_does_not_kill():
    win = rec(status='exited', pnl_net=100000.0)
    ev([win])
    t = rec(debit=100.0, qty=1499)
    r = ev([win, t], {str(t['id']): 0.0}, now=T0.replace(minute=5))
    assert r['allowed'] and not r['events']


def test_a_kill_stays_until_the_right_code_and_resets_the_peak():
    win = rec(status='exited', pnl_net=100000.0)
    ev([win])
    t = rec(debit=100.0, qty=1500)
    code = ev([win, t], {str(t['id']): 0.0})['events'][0]['code']
    # recovery does not clear a kill
    assert not ev([win, t], {str(t['id']): 100.0})['allowed']
    with pytest.raises(ValueError):
        nh.resume('live', 'WRONG', T0, 'cli:test')
    assert not nh.entries_allowed('live', T0.date())[0]
    nh.resume('live', code.lower(), T0, 'cli:test')
    r = ev([win, t], {str(t['id']): 0.0}, now=T0.replace(hour=12))
    assert r['allowed'] and r['peak'] == r['equity'] == 450000.0
    hist = [h['event'] for h in nh.load('live')['history']]
    assert hist == ['kill', 'resume_refused', 'resume', 'peak_reset']


def test_a_code_works_once():
    code = nh.halt('live', 'x', T0, 'cli:test')['code']
    nh.resume('live', code, T0, 'cli:test')
    with pytest.raises(ValueError):
        nh.resume('live', code, T0, 'cli:test')


# ── slippage kill (G14) ─────────────────────────────────────────────────────────

def _slipped(n, pct):
    # buy fill pct% of premium above the touch
    return [rec(debit=20.0 * (1 + pct / 100), ask=20.0, qty=1,
                entry_date='2026-11-%02d' % (1 + i % 28)) for i in range(n)]


def test_slippage_kill_needs_a_full_window():
    r = ev(_slipped(19, 10.0))
    assert r['allowed'] and r['slippage_n'] == 19


def test_slippage_kill_trips_above_the_limit_on_a_full_window():
    r = ev(_slipped(20, 3.5))
    assert not r['allowed'] and r['events'][0]['kind'] == 'slippage'


def test_slippage_at_the_limit_is_not_a_kill():
    assert ev(_slipped(20, 2.9))['allowed']


def test_exit_slippage_counts_from_the_touch():
    t = rec(status='exited', debit=20.0, pnl_net=0.0, exit_debit=19.0,
            exit_touch=20.0)
    assert nh.fill_slippages([t]) == [0.0, 5.0]


def test_a_fill_without_its_touch_is_not_measured():
    t = rec(debit=20.0)
    t['long_ask_entry'] = None
    assert nh.fill_slippages([t]) == []


# ── loss budget (pilot) ─────────────────────────────────────────────────────────

def test_loss_budget_off_by_default(monkeypatch):
    assert ev([rec(status='exited', pnl_net=-100000.0)])['allowed']


def test_loss_budget_kills_from_capital_including_open_marks(monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LOSS_BUDGET', 75000.0)
    lost = rec(status='exited', pnl_net=-70000.0)
    t = rec(debit=10.0, qty=1000)
    assert ev([lost, t], {str(t['id']): 6.0})['allowed']            # -74,000
    r = ev([lost, t], {str(t['id']): 5.0}, now=T0.replace(minute=5))  # -75,000
    assert not r['allowed'] and r['events'][0]['kind'] == 'loss_budget'


def test_a_restart_does_not_reset_the_loss_budget(monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LOSS_BUDGET', 75000.0)
    lost = rec(status='exited', pnl_net=-80000.0)
    code = ev([lost])['events'][0]['code']
    nh.resume('live', code, T0, 'cli:test')
    r = ev([lost], now=T0.replace(minute=5))
    assert not r['allowed'] and r['events'][0]['kind'] == 'loss_budget'


# ── pause ─────────────────────────────────────────────────────────────────────────

def _stops(dates, reason='stop'):
    return [rec(status='exited', pnl_net=-1000.0, reason=reason, exit_date=d,
                exit_time='1%d:00:00' % i) for i, d in enumerate(dates)]


def test_four_value_stops_in_three_sessions_pause_two_sessions():
    st = _stops(['2026-11-30', '2026-12-01', '2026-12-02', '2026-12-02'])
    r = ev(st)                                   # Wed 12-02, window Mon-Wed
    assert not r['allowed'] and r['events'][0]['event'] == 'pause'
    assert r['events'][0]['through'] == '2026-12-04'       # Thu + Fri
    assert not nh.entries_allowed('live', date(2026, 12, 4))[0]
    assert nh.entries_allowed('live', date(2026, 12, 7))[0]


def test_three_stops_do_not_pause():
    assert ev(_stops(['2026-12-01', '2026-12-02', '2026-12-02']))['allowed']


def test_a_stop_outside_the_window_does_not_count():
    st = _stops(['2026-11-27', '2026-12-01', '2026-12-02', '2026-12-02'])
    assert ev(st)['allowed']                      # Fri 11-27 is 4 sessions back


def test_time_exits_do_not_pause():
    st = _stops(['2026-12-02'] * 4, reason='time')
    assert ev(st)['allowed']


def test_one_cluster_pauses_once():
    st = _stops(['2026-12-02'] * 4)
    ev(st)
    r = ev(st, now=datetime(2026, 12, 7, 10, 0))   # pause over, same 4 stops
    assert r['allowed'] and not r['events']


def test_a_pause_with_a_kill_keeps_the_kill_reason():
    code = nh.halt('live', 'manual', T0, 'cli:test')['code']
    r = ev(_stops(['2026-12-02'] * 4))
    assert 'KILLED' in r['reason']
    nh.resume('live', code, T0, 'cli:test')
    ok, why = nh.entries_allowed('live', T0.date())
    assert not ok and 'PAUSED' in why


# ── manual halt ───────────────────────────────────────────────────────────────────

def test_manual_halt_needs_a_reason_and_cannot_double_trip():
    with pytest.raises(ValueError):
        nh.halt('live', '  ', T0, 'cli:test')
    nh.halt('live', 'FAIL read', T0, 'cli:test')
    with pytest.raises(ValueError):
        nh.halt('live', 'again', T0, 'cli:test')


def test_manual_halt_works_on_an_unreadable_file():
    p = nh.state_path('live')
    p.write_text('garbage', encoding='utf-8')
    ev_ = nh.halt('live', 'box fault', T0, 'cli:test')
    assert ev_['code'] and nh.load('live')['killed']
    assert list(p.parent.glob('entry_halt.json.unreadable.*'))


def test_every_action_is_journalled_with_its_sender():
    code = nh.halt('live', 'x', T0, 'cli:owner')['code']
    nh.resume('live', code, T0, 'telegram:owner')
    lines = _read(nh.journal_path('live')).splitlines()
    evs = [json.loads(x) for x in lines]
    assert [(e['event'], e['by']) for e in evs] == [('kill', 'cli:owner'),
                                                     ('resume', 'telegram:owner')]


def test_resume_never_clears_a_pause():
    ev(_stops(['2026-12-02'] * 4))
    with pytest.raises(ValueError):
        nh.resume('live', 'ANY', T0, 'cli:test')        # not killed
    assert not nh.entries_allowed('live', T0.date())[0]


def test_summary_never_raises_on_garbage():
    nh.state_path('live').write_text('x', encoding='utf-8')
    assert 'fails closed' in nh.summary('live')


# ── CLI ─────────────────────────────────────────────────────────────────────────────

def test_cli_halt_then_resume(capsys):
    from argparse import Namespace
    from zebra import __main__ as zm
    zm.cmd_halt(Namespace(reason='review FAIL', status=False, mode='live'))
    out = capsys.readouterr().out
    code = out.split('restart code: ')[1].split()[0]
    assert not nh.entries_allowed('live', T0.date())[0]
    with pytest.raises(SystemExit):
        zm.cmd_resume(Namespace(code='BAD', mode='live'))
    zm.cmd_resume(Namespace(code=code, mode='live'))
    assert nh.entries_allowed('live', T0.date())[0]
    assert 'RESUMED' in capsys.readouterr().out


def test_cli_halt_without_a_reason_only_shows_status(capsys):
    from argparse import Namespace
    from zebra import __main__ as zm
    zm.cmd_halt(Namespace(reason=None, status=False, mode='live'))
    assert 'not killed' in capsys.readouterr().out
    assert nh.entries_allowed('live', T0.date())[0]


# ── restart semantics per trigger ───────────────────────────────────────────────────

def test_a_slippage_kill_can_be_restarted_and_needs_a_fresh_window():
    old = _slipped(20, 3.5)
    code = ev(old)['events'][0]['code']
    later = T0.replace(hour=14)
    nh.resume('live', code, later, 'cli:test')
    assert ev(old, now=later.replace(minute=5))['allowed']
    # 20 fresh bad fills after the restart kill again
    fresh = [rec(debit=20.7, ask=20.0, qty=1, entry_date='2026-12-03')
             for _ in range(20)]
    r = ev(old + fresh, now=datetime(2026, 12, 3, 11, 0))
    assert not r['allowed'] and r['events'][0]['kind'] == 'slippage'


def test_a_deleted_file_under_a_kill_resumes_with_the_journal_code():
    code = nh.halt('live', 'x', T0, 'cli:test')['code']
    nh.state_path('live').unlink()
    with pytest.raises(ValueError):
        nh.resume('live', 'WRONG', T0, 'cli:test')
    nh.resume('live', code, T0, 'cli:test')
    assert nh.entries_allowed('live', T0.date())[0]


def test_a_corrupt_file_is_not_resumable_by_code():
    nh.halt('live', 'x', T0, 'cli:test')
    nh.state_path('live').write_text('garbage', encoding='utf-8')
    with pytest.raises(nh.HaltUnreadable):
        nh.resume('live', 'ANY', T0, 'cli:test')


def test_evaluate_stamps_when_it_ran():
    ev([])
    assert nh.load('live')['evaluated_at'] == '2026-12-02 11:00:00'
