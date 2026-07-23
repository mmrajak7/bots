"""Smoke tests for the regime gate (src/core/regime.py) + price floor.
All I/O monkeypatched to a temp dir; no network, no writes to real data/.
Run: cd FIFTY && python _research/filter_stretch/regime_smoke_test.py
"""
import sys, os, json, tempfile, shutil
from pathlib import Path
import pandas as pd

sys.path.insert(0, r'C:/Users/mail2/Documents/Projects/BOTS/FIFTY')
os.chdir(r'C:/Users/mail2/Documents/Projects/BOTS/FIFTY')

import src.core.regime as regime_mod
from src.core.regime import RegimeManager

TMP = Path(tempfile.mkdtemp(prefix='regime_test_'))
regime_mod.STATE_FILE = TMP / 'regime_state.json'
regime_mod.BREADTH_DIR = TMP / 'breadth'
regime_mod.HISTORY_FILE = regime_mod.BREADTH_DIR / 'history.json'
regime_mod.BREADTH_DAILY_FILE = regime_mod.BREADTH_DIR / 'breadth_daily.json'
regime_mod.SEED_FILE = regime_mod.BREADTH_DIR / 'seed_history.json.gz'

PASS, FAIL = [], []
def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS' if cond else 'FAIL') + f' | {name} | {detail}')

# Capture telegram alerts
alerts = []
class FakeTelegram:
    def send_alert(self, msg, critical=False):
        alerts.append(msg); return True
regime_mod.telegram = FakeTelegram()

rm = RegimeManager(kite=None)

# Drive the machine with synthetic sessions
sessions = iter([])
current = {}
def fake_nifty(self):
    return dict(current['nifty'])
def fake_breadth_info(self, session_s):
    # default: exact same-session reading (backtest-equivalent timing)
    return current['breadth'], current.get('breadth_date', current['nifty']['session'])
RegimeManager._nifty_daily_signals = fake_nifty
RegimeManager._breadth_info = fake_breadth_info

def step(session, rsi, breadth, close, sma, breadth_date=None, sessions=None):
    current['nifty'] = {'session': session, 'rsi': rsi, 'close': close, 'sma50': sma,
                        'sessions': sessions or [session]}
    current['breadth'] = breadth
    current['breadth_date'] = breadth_date or session
    rm._eval_cache_session = None; rm._eval_cache_result = None
    return rm.evaluate()

# --- T1: healthy market stays UNPAUSED, no alerts ---
r = step('2026-01-01', 60, 55, 105, 100)
check('T1 stays UNPAUSED', r['state'] == 'UNPAUSED' and len(alerts) == 0, str(r))

# --- T2: all-3-OFF -> instant PAUSE + exactly one alert ---
r = step('2026-01-02', 40, 30, 95, 100)
check('T2 instant PAUSE', r['state'] == 'PAUSED', str(r))
check('T2 one red alert', len(alerts) == 1 and '🔴' in alerts[0] and 'paused' in alerts[0].lower(), f'{len(alerts)} alerts')

# --- T3: re-evaluate same session -> no duplicate alert, no double count ---
rm._eval_cache_session = None
r = rm.evaluate()
check('T3 idempotent per session', len(alerts) == 1 and r['state'] == 'PAUSED', f'{len(alerts)} alerts')

# --- T4: 6 sessions of 2-on, then flicker to 1-on -> streak resets ---
for i in range(3, 9):  # sessions 03..08 = six 2-on days
    r = step(f'2026-01-{i:02d}', 55, 45, 95, 100)  # rsi ON, breadth ON, sma OFF
check('T4 streak at 6', r['streak'] == 6 and r['state'] == 'PAUSED', str(r))
r = step('2026-01-09', 55, 30, 95, 100)  # only rsi ON -> flicker
check('T4 flicker resets streak', r['streak'] == 0 and r['state'] == 'PAUSED', str(r))

# --- T5: 7 straight 2-on sessions -> UNPAUSE + exactly one green alert ---
for i in range(10, 17):
    r = step(f'2026-01-{i:02d}', 55, 45, 95, 100)
check('T5 unpause after 7', r['state'] == 'UNPAUSED', str(r))
check('T5 one green alert', len(alerts) == 2 and '🟢' in alerts[1], f'{len(alerts)} alerts')

# --- T6: entry_allowed logic ---
ok, why = rm.entry_allowed(-1.0)
check('T6 unpaused allows', ok is True and why == '', f'{ok},{why}')
step('2026-01-17', 40, 30, 95, 100)  # PAUSE again (alert #3)
ok, why = rm.entry_allowed(-2.5)
check('T6 deep override allows', ok is True and why == 'override', f'{ok},{why}')
ok, why = rm.entry_allowed(-1.0)
check('T6 paused blocks shallow', ok is False and 'PAUSED' in why, f'{ok},{why}')
ok, why = rm.entry_allowed(None)
check('T6 paused blocks None stretch', ok is False, f'{ok},{why}')

# --- T7: data failure keeps prior state, no alert ---
n_alerts = len(alerts)
current['nifty'] = None
def fake_none(self): return None
RegimeManager._nifty_daily_signals = fake_none
rm._eval_cache_session = None; rm._eval_cache_result = None
r = rm.evaluate()
check('T7 fail-safe keeps state', r['state'] == 'PAUSED' and r.get('stale') is True
      and len(alerts) == n_alerts, str(r))

# --- T8: state persists across manager instances ---
rm2 = RegimeManager(kite=None)
st = rm2._load_state()
check('T8 state persisted', st['state'] == 'PAUSED' and st['last_session'] == '2026-01-17', str(st))

# --- T9: breadth computation on synthetic history ---
regime_mod.BREADTH_DIR.mkdir(parents=True, exist_ok=True)
hist = {}
dates = [f'2026-02-{d:02d}' for d in range(1, 29)] + [f'2026-03-{d:02d}' for d in range(1, 29)]
for sym, trend in [('UPSTOCK', 1), ('DOWNSTOCK', -1), ('FLATSTOCK', 0)]:
    closes = {}
    for i, ds in enumerate(dates[-55:]):
        base = 100 + trend * i * 2
        closes[ds] = float(max(base, 1))
    hist[sym] = closes
with open(regime_mod.HISTORY_FILE, 'w') as f:
    json.dump(hist, f)
rm3 = RegimeManager(kite=None)
import src.utils.config_manager as cm
orig_get = cm.config.get
cm.config.get = lambda k, d=None: 2 if k == 'regime.breadth_min_coverage' else orig_get(k, d)
res = rm3._compute_breadth_for_last_session()
cm.config.get = orig_get
# UPSTOCK above its 50DMA, DOWNSTOCK below, FLATSTOCK equal (not >) -> 1/3
check('T9 breadth math', res is not None and abs(res[1] - 33.33) < 0.1 and res[2] == 3,
      str(res))

# --- T11: breadth-lag wait + degraded voting ---
RegimeManager._nifty_daily_signals = fake_nifty  # restore after T7's failure fake
n_al = len(alerts)
# machine currently PAUSED (from T6 block). Fresh session, breadth lags 1 session
# and pipeline is alive (reading within last 3 sessions) -> WAIT, no advance.
r = step('2026-01-20', 55, 45, 95, 100, breadth_date='2026-01-19',
         sessions=['2026-01-16', '2026-01-17', '2026-01-19', '2026-01-20'])
st_now = rm._load_state()
check('T11 lag waits (no advance)', r.get('pending_breadth') is True
      and st_now['last_session'] == '2026-01-17' and len(alerts) == n_al, str(r))
# exact reading arrives later same day -> advances (streak 1)
r = step('2026-01-20', 55, 45, 95, 100)
check('T11 exact advances', r.get('pending_breadth') is None and r['streak'] == 1
      and rm._load_state()['last_session'] == '2026-01-20', str(r))
# breadth reading ancient (pipeline dead) -> 2-signal voting, still advances
r = step('2026-01-21', 55, 45, 95, 100, breadth_date='2026-01-05',
         sessions=['2026-01-16', '2026-01-17', '2026-01-20', '2026-01-21'])
check('T11 stale degrades to 2-signal', r.get('pending_breadth') is None
      and r['breadth'] is None and r['n_on'] == 1
      and rm._load_state()['last_session'] == '2026-01-21', str(r))
# (rsi ON, sma OFF -> 1-of-2 -> streak resets under 2-signal voting)
check('T11 streak reset under voting', r['streak'] == 0, str(r))

# --- T9b: maintenance session-gap routing (capture vs backfill vs none) ---
from datetime import datetime as _dt
regime_mod.now_ist = lambda: _dt(2026, 7, 23, 10, 0)   # inside 09:20-15:25 gate
calls9 = []
rm4 = RegimeManager(kite=None)
rm4._load_history = lambda: {'X': {'2026-07-20': 100.0}}
rm4._last_session_in_history = lambda: '2026-07-20'
rm4._capture_prev_session_closes = lambda s: calls9.append(('capture', s))
rm4._backfill_sessions = lambda m: calls9.append(('backfill', tuple(m)))
def mk_sessions(*days):
    return {'session': days[-1], 'rsi': 55, 'close': 100, 'sma50': 99,
            'sessions': list(days)}
# no missing
rm4._nifty_daily_signals = lambda: mk_sessions('2026-07-17', '2026-07-20')
rm4.maintenance()
check('T9b no-gap does nothing', calls9 == [], str(calls9))
# exactly one missing -> capture
rm4._nifty_daily_signals = lambda: mk_sessions('2026-07-20', '2026-07-22')
rm4.maintenance()
check('T9b 1-missing -> capture', calls9 == [('capture', '2026-07-22')], str(calls9))
# several missing -> backfill
calls9.clear()
rm4._nifty_daily_signals = lambda: mk_sessions('2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23')
rm4.maintenance()
check('T9b multi-missing -> backfill',
      calls9 == [('backfill', ('2026-07-21', '2026-07-22', '2026-07-23'))], str(calls9))
# outside market hours -> nothing
calls9.clear()
regime_mod.now_ist = lambda: _dt(2026, 7, 23, 16, 0)
rm4.maintenance()
check('T9b time-gated', calls9 == [], str(calls9))

# --- T9c: backfill throttle (once per day, persisted) ---
regime_mod.today_ist = lambda: _dt(2026, 7, 23).date()
rm5 = RegimeManager(kite=None)
rm5._load_history = lambda: {'Y': {'2026-07-18': 50.0}}
hits = []
orig_backfill = RegimeManager._backfill_sessions
st = rm5._load_state(); st.pop('backfill_attempted_on', None); rm5._save_state(st)
class NoKite:
    def get_instrument_token(self, s): hits.append(s); raise RuntimeError('no net')
    def get_historical_data(self, *a): raise RuntimeError('no net')
rm5.kite = NoKite()
rm5._history = {'Y': {'2026-07-18': 50.0}}
rm5._backfill_sessions(['2026-07-21', '2026-07-22'])   # attempt 1: runs (hits symbol)
n1 = len(hits)
rm5._backfill_sessions(['2026-07-21', '2026-07-22'])   # attempt 2 same day: throttled
check('T9c backfill throttled to 1/day', n1 == 1 and len(hits) == n1, f'hits={hits}')

# --- T10: price floor in _add_to_queue (fake session, no DB writes) ---
from src.core import signal_processor as sp_mod
added = []
class FakeSession:
    def add(self, obj): added.append(obj)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass
orig_get_session = sp_mod.get_session
sp_mod.get_session = lambda: FakeSession()
try:
    sp = sp_mod.signal_processor
    out = sp._add_to_queue('PENNYSTK', 95.0)
    rej = added[-1]
    check('T10 floor rejects <100', out is None and rej.status.name == 'REJECTED'
          and 'floor' in rej.rejection_reason.lower(), f'{rej.rejection_reason}')
finally:
    sp_mod.get_session = orig_get_session

print(f"\n{'='*50}\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL); sys.exit(1)
shutil.rmtree(TMP, ignore_errors=True)
