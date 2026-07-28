"""Smoke tests for the regime gate (src/core/regime.py) + price floor.
All I/O monkeypatched to a temp dir; no network, no writes to real data/.
Run: cd FIFTY && python _research/filter_stretch/regime_smoke_test.py
"""
import sys, os, json, tempfile, shutil
from pathlib import Path
import pandas as pd

# Derive the FIFTY root from this file - these tests run on the Pi too, so a
# hardcoded Windows path breaks the deployment script.
FIFTY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FIFTY_DIR)
os.chdir(FIFTY_DIR)

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
rm4._run_backfill = lambda f, t, s: calls9.append(('backfill', tuple(s)))
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
# outside the capture window: batch-OHLC capture must NOT run (its close
# semantics are intraday-only) but the historical backfill MUST still run -
# that is what lets a multi-session gap heal the same day it is noticed.
calls9.clear()
regime_mod.now_ist = lambda: _dt(2026, 7, 23, 16, 0)
rm4._nifty_daily_signals = lambda: mk_sessions('2026-07-20', '2026-07-22')
rm4.maintenance()
check('T9b capture is intraday-gated', calls9 == [], str(calls9))
calls9.clear()
rm4._nifty_daily_signals = lambda: mk_sessions('2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23')
rm4.maintenance()
check('T9b backfill runs outside window',
      calls9 == [('backfill', ('2026-07-21', '2026-07-22', '2026-07-23'))], str(calls9))

# --- T9c: backfill is resumable + time-budgeted (never blocks the daemon) ---
regime_mod.today_ist = lambda: _dt(2026, 7, 23).date()
regime_mod.now_ist = lambda: _dt(2026, 7, 23, 16, 0)
UNIVERSE = {f'S{i:03d}': {'2026-07-18': 50.0} for i in range(5)}
hits = []
class OkKite:
    """Returns real rows, so the cursor is allowed to advance.
    (A broker that fetches NOTHING must instead rewind - see T9f.)"""
    def get_instrument_token(self, s): hits.append(s); return '1'
    def get_historical_data(self, *a):
        return pd.DataFrame([{'Date': '2026-07-21', 'Close': 10.0},
                             {'Date': '2026-07-22', 'Close': 11.0}])
rm5 = RegimeManager(kite=OkKite())
rm5._history = dict(UNIVERSE)
st = rm5._load_state(); st.pop('backfill', None); rm5._save_state(st)
# budget 0 -> exactly one symbol per cycle, but ALWAYS at least one (forward
# progress must not depend on the budget being generous)
regime_mod.config._config.setdefault('regime', {})['backfill_seconds_per_cycle'] = 0
rm5._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
c1 = rm5._load_state()['backfill']['cursor']
rm5._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
c2 = rm5._load_state()['backfill']['cursor']
check('T9c backfill resumes from cursor', c1 == 1 and c2 == 2, f'{c1}->{c2} hits={hits}')

# runs to completion across cycles, then stops
for _ in range(5):
    rm5._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
bf = rm5._load_state()['backfill']
check('T9c completes and latches done',
      bf['cursor'] == 5 and bf['done'] and bf['complete'], str(bf))
n_before = len(hits)
rm5._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
check('T9c done short-circuits', len(hits) == n_before, f'hits grew to {len(hits)}')

# --- T9f: an empty df is NOT success (circuit breaker open) ---------------
# get_historical_data returns an EMPTY frame (no exception) when the historical
# breaker trips. Counting that as filled would let a dead API sweep the whole
# universe instantly and latch a COMPLETE holding no data.
class DeadKite:
    """Breaker-open behaviour: returns empty frames, never raises."""
    def get_instrument_token(self, s): return '1'
    def get_historical_data(self, *a): return pd.DataFrame()

rm8 = RegimeManager(kite=DeadKite())
rm8._history = {f'S{i:03d}': {'2026-07-18': 50.0} for i in range(5)}
st = rm8._load_state(); st.pop('backfill', None); rm8._save_state(st)
regime_mod.config._config.setdefault('regime', {})['backfill_seconds_per_cycle'] = 999
rm8._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
bf8 = rm8._load_state()['backfill']
check('T9f empty frames do not fake a COMPLETE',
      bf8['cursor'] == 0 and not bf8['complete'] and bf8['filled'] == 0, str(bf8))

# and once the API recovers, the sweep proceeds normally from the same cursor
class LiveKite:
    def get_instrument_token(self, s): return '1'
    def get_historical_data(self, *a):
        return pd.DataFrame([{'Date': '2026-07-21', 'Close': 10.0},
                             {'Date': '2026-07-22', 'Close': 11.0}])

rm8.kite = LiveKite()
rm8._run_backfill('2026-07-21', '2026-07-22', ['2026-07-21', '2026-07-22'])
bf8b = rm8._load_state()['backfill']
check('T9f recovers and completes once the API returns data',
      bf8b['cursor'] == 5 and bf8b['complete'] and bf8b['filled'] == 5, str(bf8b))

# --- T9g: day rollover must not strand symbols past the cursor -------------
# A sweep abandoned at cursor N leaves symbols N.. without that range. The next
# day `missing` is computed off the GLOBAL max session, which the swept symbols
# already advanced - so the older gap looks filled. The range must widen back.
rm9 = RegimeManager(kite=LiveKite())
rm9._history = {f'S{i:03d}': {'2026-07-18': 50.0} for i in range(5)}
rm9._save_state({'state': 'UNPAUSED', 'streak': 0, 'since': None,
                 'last_session': None, 'transitions': [],
                 'backfill': {'from': '2026-07-21', 'to': '2026-07-22',
                              'day': '2026-07-22', 'sessions': ['2026-07-21', '2026-07-22'],
                              'cursor': 2, 'cycles': 3, 'filled': 2,
                              'done': True, 'complete': False}})
# next day, only the newest session looks missing
rm9._run_backfill('2026-07-23', '2026-07-23', ['2026-07-23'])
bf9 = rm9._load_state()['backfill']
check('T9g incomplete prior sweep widens the range (no stranded gap)',
      bf9['from'] == '2026-07-21' and bf9['to'] == '2026-07-23'
      and bf9['sessions'] == ['2026-07-21', '2026-07-22', '2026-07-23']
      and bf9['cursor'] == 5,
      f"range={bf9['from']}..{bf9['to']} sessions={bf9['sessions']}")

# a COMPLETED prior sweep must NOT widen - that would re-fetch the universe daily
rm10 = RegimeManager(kite=LiveKite())
rm10._history = {f'S{i:03d}': {'2026-07-18': 50.0} for i in range(5)}
rm10._save_state({'state': 'UNPAUSED', 'streak': 0, 'since': None,
                  'last_session': None, 'transitions': [],
                  'backfill': {'from': '2026-07-21', 'to': '2026-07-22',
                               'day': '2026-07-22', 'sessions': ['2026-07-21'],
                               'cursor': 5, 'cycles': 1, 'filled': 5,
                               'done': True, 'complete': True}})
rm10._run_backfill('2026-07-23', '2026-07-23', ['2026-07-23'])
bf10 = rm10._load_state()['backfill']
check('T9g completed prior sweep does NOT widen',
      bf10['from'] == '2026-07-23', f"range={bf10['from']}..{bf10['to']}")

# --- T9d: state machine HOLDS while a backfill is mid-flight ---
rm6 = RegimeManager(kite=None)
rm6._history = dict(UNIVERSE)
rm6._save_state({'state': 'UNPAUSED', 'streak': 0, 'since': None,
                 'last_session': '2026-07-20', 'transitions': [],
                 'backfill': {'from': '2026-07-21', 'to': '2026-07-22',
                              'day': '2026-07-23', 'cursor': 2,
                              'done': False, 'complete': False}})
def _boom(self):
    raise AssertionError('must not hit the API while backfilling')
rm6._nifty_daily_signals = lambda: _boom(rm6)
r6 = rm6.evaluate()
check('T9d evaluate holds during backfill',
      r6.get('backfill') is True and r6['state'] == 'UNPAUSED', str(r6))
allowed, why = rm6.entry_allowed(-0.5)
check('T9d gate fails open while holding', allowed is True, f'{allowed},{why}')

# --- T9e: per-session breadth ignores later closes (backfill scoring) ---
rm7 = RegimeManager(kite=None)
# 51 sessions; the last close is a huge spike that must NOT leak into the
# reading for the session before it
days = [f'2026-05-{d:02d}' for d in range(1, 29)] + \
       [f'2026-06-{d:02d}' for d in range(1, 24)]
def _spiked():
    closes = {d: 100.0 for d in days[:-1]}
    closes[days[-1]] = 900.0
    return closes
rm7._history = {f'S{i}': _spiked() for i in range(400)}
regime_mod.config._config.setdefault('regime', {})['breadth_min_coverage'] = 300
r_last = rm7._compute_breadth()
r_prev = rm7._compute_breadth(days[-2])
check('T9e breadth scored at target session',
      r_last[0] == days[-1] and r_last[1] == 100.0
      and r_prev[0] == days[-2] and r_prev[1] == 0.0,
      f'last={r_last} prev={r_prev}')

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
