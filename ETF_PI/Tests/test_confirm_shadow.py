"""Verify the entry-confirmation shadow produces meaningful, uncontaminated data.

Drives _confirm_observe / _confirm_mark_entry directly with synthetic price paths, and
checks the analyser's scoring function against the same records.

Run either way:
    python Tests/test_confirm_shadow.py
    python -m Tests.test_confirm_shadow
"""
import ast
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import analyze_confirm_shadow as an  # noqa: E402
import option_buy as ob  # noqa: E402
import _muzzle  # noqa: E402,F401  MUST come before anything drives option_buy.

# Without this the confirm-shadow probes log through option_buy's FileHandler and append
# ~48 synthetic "CONFIRM ARM ... 24500" lines into the LIVE logs/option_buy_<today>.log on
# every run. See Tests/_muzzle.py.
_muzzle.assert_clean()

IST = ob.IST
BASE = IST.localize(datetime.datetime(2026, 8, 19, 10, 0, 0))
STRIKE = 24500

FAILURES = []


def check(cond, msg):
    if cond:
        print("   PASS  " + msg)
    else:
        print("   FAIL  " + msg)
        FAILURES.append(msg)


def T(n, title):
    print("\n" + "=" * 74)
    print("%s. %s" % (n, title))
    print("=" * 74)


def at(m):
    return BASE + datetime.timedelta(minutes=m)


def arm_of(state, tf='10minute', strike=STRIKE, day='20260819'):
    return state.get(ob._ARMS_KEY, {}).get(ob._arm_key('CE', tf, strike, day))


def obs(state, m, ltp, gap, site='main', typ='CE', tf='10minute', strike=STRIKE):
    ob._confirm_observe(state, site, typ, tf, strike, ltp, gap, at(m))


def enter(state, m, ltp, site='main', typ='CE', tf='10minute', strike=STRIKE):
    ob._confirm_mark_entry(state, site, typ, tf, strike, at(m), ltp)


def run(path, label, entry=True):
    state = {}
    for m, ltp, gap in path:
        obs(state, m, ltp, gap)
    if entry:
        m, ltp, _ = path[-1]
        enter(state, m, ltp)
    a = arm_of(state)
    print("\n-- %s" % label)
    if a is None:
        print("   arm: None")
        return state, None
    print("   obs=%s low=%s entry_ltp=%s bounce=%s fired=%s fired@entry=%s +%smin"
          % (a.get('obs'), a.get('low'), a.get('entry_ltp'), a.get('entry_bounce_pct'),
             sorted(a.get('fired', {})), sorted(a.get('fired_at_entry', {}) or {}),
             a.get('entry_after_min')))
    return state, a


# ===========================================================================
T(1, "KNIFE: premium falls the whole way; gap narrows only because ST descends")
knife = [(0, 100.0, 9.0), (3, 98.5, 8.4), (6, 97.0, 7.8), (9, 95.5, 7.2),
         (12, 94.0, 6.6), (15, 92.5, 6.0)]
_, a = run(knife, "knife path")
check(a and a['obs'] == 6, "6 observations accumulated (pre-fix this was always 1)")
check(a and a['entry_after_min'] == 15.0, "entry stamped +15.0min (pre-fix always +0.0)")
check(a and a['low'] == 92.5, "running low tracked down to 92.5")
check(a and a['entry_bounce_pct'] == 0.0, "bounce 0.0% -> bought while still falling")
check(a and not a['fired_at_entry'], "no candidate confirmed at entry")

T(2, "TURN: premium dips, bottoms, then lifts into the entry zone")
turn = [(0, 100.0, 9.0), (3, 97.0, 8.6), (6, 95.0, 8.0), (9, 95.6, 7.4),
        (12, 96.5, 6.8), (15, 97.5, 6.2)]
_, a2 = run(turn, "turn path")
check(a2 and a2['low'] == 95.0, "running low pinned at the 95.0 bottom")
check(a2 and a2['entry_bounce_pct'] > 2.0,
      "bounce %s%% -> bought into a turn" % (a2 or {}).get('entry_bounce_pct'))
check(a2 and sorted(a2['fired_at_entry']) == ['0.3', '0.5', '1.0'],
      "all three X candidates confirmed before entry")
check(a2 and a2['fired_at_entry']['1.0']['min'] == 12.0, "X=1.0% turn stamped at +12.0min")

T(3, "DISCRIMINATION: the two shapes are separable")
check(a and a2 and a['entry_bounce_pct'] != a2['entry_bounce_pct']
      and sorted(a['fired_at_entry']) != sorted(a2['fired_at_entry']),
      "knife and turn differ on BOTH bounce% and fired{} (pre-fix both were empty)")

T(4, "LONG MARINATE: 40min in the watch zone (the old 20min cap froze the record)")
long_path = [(m, 100.0 - m * 0.1, 9.0) for m in range(0, 40, 5)]
long_path += [(40, 97.0, 6.5), (45, 98.0, 6.0)]
_, a3 = run(long_path, "40min watch then entry")
check(a3 and a3['obs'] == len(long_path), "all %d observations kept" % len(long_path))
check(a3 and a3['entry_after_min'] == 45.0, "entry stamped at +45.0min")
check(a3 and a3['fired_at_entry'], "turn still detected past the old freeze window")

# ===========================================================================
T(5, "S1 REGRESSION: an ENTERED arm must survive leaving the band (was destroyed)")
# The bug: winners rally until gap < MIN_REWARD_PCT and knives fall until gap >
# WATCH_GAP_PCT, so observation stops; when the premium drifts back >15min later the
# staleness rule popped the arm, deleting entered_at / entry_bounce_pct / fired_at_entry.
# Nothing archives an arm until the next morning, so the matched trade vanished silently.
st = {}
for m, ltp, gap in [(0, 100.0, 9.0), (3, 97.0, 7.5), (6, 95.0, 6.2)]:
    obs(st, m, ltp, gap)
enter(st, 6, 95.0)
before = dict(arm_of(st))
obs(st, 40, 101.0, 6.5)          # back in band after a 34-minute excursion
a4 = arm_of(st)
check(a4 is not None, "entered arm still present after a 34min band excursion")
check(a4 and a4.get('entered_at') == before['entered_at'], "entered_at preserved")
check(a4 and a4.get('entry_bounce_pct') == before['entry_bounce_pct'],
      "entry_bounce_pct preserved (%s)" % before['entry_bounce_pct'])
check(a4 and a4.get('fired_at_entry') == before['fired_at_entry'],
      "fired_at_entry snapshot preserved")
check(a4 and a4.get('post_entry_hole') is True, "post-entry hole flagged for the analysis")

T(6, "STALENESS still expires an arm that has NOT entered")
st = {}
obs(st, 0, 100.0, 9.0)
obs(st, 3, 90.0, 8.0)
stale_low = arm_of(st)['low']
obs(st, 28, 130.0, 8.0)          # 25min hole, no entry -> re-arm
a5 = arm_of(st)
check(a5 is not None, "S3: re-armed IN PLACE on the same scan, not left absent")
check(a5 and a5['low'] == 130.0,
      "re-armed at 130.0, not anchored to the stale low of %s" % stale_low)
check(a5 and a5.get('entered_at') is None, "fresh arm carries no entry stamp")

T(7, "S3 REGRESSION: stale re-arm and a real entry on the SAME scan")
st = {}
obs(st, 0, 100.0, 9.0)
obs(st, 2, 99.0, 8.0)
obs(st, 30, 96.0, 6.5)           # stale -> re-arms in place
enter(st, 30, 96.0)              # entry fires later in this same pass
a6 = arm_of(st)
check(a6 and a6.get('entered_at') is not None,
      "entry recorded (pre-fix the pop+return left no arm and the trade vanished)")
check(a6 and a6.get('entry_bounce_pct') == 0.0, "bounce measured off the fresh low")

T(8, "S4 REGRESSION: main -> sticky handoff keeps ONE continuous low")
st = {}
obs(st, 0, 100.0, 9.0, site='main')
obs(st, 3, 94.0, 8.0, site='main')      # the real low of the move
obs(st, 6, 96.0, 7.0, site='sticky')    # ATM drifted; sticky takes over
obs(st, 9, 97.0, 6.5, site='sticky')
enter(st, 9, 97.0, site='sticky')
a7 = arm_of(st)
check(len(st.get(ob._ARMS_KEY, {})) == 1, "one arm, not one per site")
check(a7 and a7['low'] == 94.0, "low is the move's real 94.0, not the post-handoff stub")
check(a7 and a7['entry_bounce_pct'] > 3.0,
      "bounce %s%% measured over the whole move" % (a7 or {}).get('entry_bounce_pct'))
check(a7 and a7.get('site') == 'sticky', "site records the path that actually entered")

T(9, "OBS DEBOUNCE: both sites observing one scan counts once")
st = {}
obs(st, 0, 100.0, 9.0, site='main')
obs(st, 5, 99.0, 8.0, site='main')
n_before = arm_of(st)['obs']
ob._confirm_observe(st, 'sticky', 'CE', '10minute', STRIKE, 99.0, 8.0,
                    at(5) + datetime.timedelta(seconds=1))
check(arm_of(st)['obs'] == n_before, "second site on the same scan did not double-count")

T(10, "NEW LOW RESETS THE TURN (fired must not contradict entry_bounce_pct)")
st = {}
obs(st, 0, 100.0, 9.0)
obs(st, 2, 98.0, 8.5)
obs(st, 4, 99.5, 8.0)            # +1.53% off 98.0 -> all candidates fire
check(sorted(arm_of(st)['fired']) == ['0.3', '0.5', '1.0'], "turn fired off the 98.0 low")
obs(st, 6, 95.0, 7.5)            # fresh low undercuts it
check(arm_of(st)['fired'] == {}, "fired cleared by the new low")
check(arm_of(st)['low_resets'] == 1, "low_resets counted")
obs(st, 8, 95.1, 7.0)            # only +0.1% off the new low
enter(st, 8, 95.1)
a8 = arm_of(st)
check(a8['entry_bounce_pct'] < 0.3 and not a8['fired_at_entry'],
      "entry reads as a knife on BOTH metrics, not 'confirmed' off a stale low")

# ===========================================================================
T(11, "ANALYSER: confirmed() anchors N to entry, not to arm creation")
fresh = {'day': '20260820', 'entry_after_min': 46.0,
         'fired_at_entry': {'0.5': {'min': 45.0}}}          # turn 1min before entry
stale = {'day': '20260820', 'entry_after_min': 46.0,
         'fired_at_entry': {'0.5': {'min': 5.0}}}           # turn 41min before entry
check(an.confirmed(fresh, 0.5, 10),
      "S2: long marinate + turn 1min before entry IS confirmed at N=10")
check(not an.confirmed(stale, 0.5, 10), "turn 41min before entry is NOT confirmed at N=10")
check(an.confirmed(stale, 0.5, 45), "same arm IS confirmed at a wide enough N")

T(12, "ANALYSER: hindsight cannot leak in through the legacy fallback")
contaminated = {'day': '20260820', 'entry_after_min': 5.0,
                'fired': {'0.5': {'min': 40.0}}}            # post-entry turn, no snapshot
check(not an.confirmed(contaminated, 0.5, 20),
      "S7: current-era arm with no snapshot is NOT scored off live fired{}")
legacy = {'day': '20260817', 'fired': {'0.5': {'min': 2.0}}}
check(an.confirmed(legacy, 0.5, 10), "pre-era arm still falls back to fired{}")
check(an.confirmed({'day': '20260820'}, 0.5, 10) is False, "arm with no fired data at all")

T(13, "POST-ENTRY CONTAMINATION: fired_at_entry frozen at the entry instant")
st = {}
for m, ltp, gap in [(0, 100.0, 9.0), (3, 97.0, 7.5), (6, 94.0, 6.2)]:
    obs(st, m, ltp, gap)
enter(st, 6, 94.0)
check(sorted(arm_of(st).get('fired_at_entry', {})) == [], "nothing confirmed at entry")
for m, ltp, gap in [(9, 97.0, 5.9), (12, 99.0, 5.4)]:
    obs(st, m, ltp, gap)          # position open, premium rips
a9 = arm_of(st)
check(sorted(a9['fired']) == ['0.3', '0.5', '1.0'], "live fired{} kept the post-entry path")
check(sorted(a9.get('fired_at_entry', {})) == [], "fired_at_entry still empty")
check(a9['entry_bounce_pct'] == 0.0, "entry_bounce_pct unaffected by the rally")

# ===========================================================================
T(14, "INERTNESS: shadow calls can never influence trading")
src = ast.parse(open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'option_buy.py'), encoding='utf-8').read())
calls = {'_confirm_observe': [], '_confirm_mark_entry': []}
for node in ast.walk(src):
    fn = getattr(getattr(node, 'func', None), 'id', None)
    if isinstance(node, ast.Call) and fn in calls:
        calls[fn].append(node)
bare = {'_confirm_observe': 0, '_confirm_mark_entry': 0}
for node in ast.walk(src):
    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        fn = getattr(node.value.func, 'id', None)
        if fn in bare:
            bare[fn] += 1
for fn in calls:
    check(len(calls[fn]) == bare[fn] and bare[fn] > 0,
          "every %s() call is a bare statement - never assigned, branched on, or "
          "returned (%d found)" % (fn, bare[fn]))

# 14b: a raise inside the shadow must not escape into the caller
class Boom(object):
    def __le__(self, other):
        raise RuntimeError("boom")

    def __float__(self):
        raise RuntimeError("boom")


st = {}
obs(st, 0, Boom(), 9.0)
check(st.get(ob._ARMS_KEY) in (None, {}), "malformed ltp swallowed, no arm written")

# 14c: shadow off is a total no-op
ob.ENTRY_CONFIRM_SHADOW = False
st = {}
obs(st, 0, 100.0, 9.0)
enter(st, 0, 100.0)
check(st == {}, "ENTRY_CONFIRM_SHADOW=False writes nothing at all")
ob.ENTRY_CONFIRM_SHADOW = True

T(15, "ROBUSTNESS")
# legacy arm with no last_seen
st = {ob._ARMS_KEY: {ob._arm_key('CE', '10minute', STRIKE, '20260819'): {
    'day': '20260819', 'site': 'main', 'opt_type': 'CE', 'tf': '10minute',
    'strike': STRIKE, 'armed_at': BASE.isoformat(), 'arm_ltp': 100.0,
    'low': 100.0, 'obs': 1, 'fired': {}, 'entered_at': None}}}
obs(st, 2, 101.0, 6.5)
a10 = arm_of(st)
check(a10 and a10['obs'] == 2 and a10.get('last_seen'),
      "legacy arm without last_seen upgraded in place, no crash")

# bad quotes must not poison the low
st = {}
obs(st, 0, 100.0, 9.0)
obs(st, 1, 0, 9.0)
obs(st, 2, None, 9.0)
check(arm_of(st)['low'] == 100.0 and arm_of(st)['obs'] == 1,
      "ltp of 0/None ignored entirely - low and obs untouched")

# mark_entry with no arm is a no-op that writes nothing
st = {}
enter(st, 0, 100.0)
check(st.get(ob._ARMS_KEY) in (None, {}), "mark_entry with no arm wrote nothing")

# corrupt armed_at re-arms in place rather than leaving a hole
st = {ob._ARMS_KEY: {ob._arm_key('CE', '10minute', STRIKE, '20260819'): {
    'day': '20260819', 'armed_at': 'not-a-timestamp', 'low': 50.0, 'fired': {},
    'obs': 1, 'entered_at': None}}}
obs(st, 1, 100.0, 8.0)
check(arm_of(st) and arm_of(st)['low'] == 100.0, "corrupt armed_at replaced on the spot")

# clock skew re-arms
st = {}
obs(st, 10, 100.0, 9.0)
obs(st, 0, 99.0, 9.0)
check(arm_of(st) and arm_of(st)['low'] == 99.0, "armed_at in the future re-arms cleanly")

print("\n" + "=" * 74)
if FAILURES:
    print("FAILED: %d of %d" % (len(FAILURES), len(FAILURES) + 0))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
