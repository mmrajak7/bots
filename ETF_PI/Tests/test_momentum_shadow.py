"""Verify the pre-entry momentum shadow (2026-09-25) records correct features and can
never affect trading.

Run either way:
    python Tests/test_momentum_shadow.py
    python -m Tests.test_momentum_shadow
"""
import ast
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import option_buy as ob  # noqa: E402
import _muzzle  # noqa: E402,F401  MUST come before anything drives option_buy.

_muzzle.assert_clean()

IST = ob.IST
BASE = IST.localize(datetime.datetime(2026, 9, 25, 10, 0, 0))
SYM = 'NIFTY2692923250CE'
STRIKE = 23250
DAY = '20260925'
FAILURES = []


def check(cond, msg):
    print(("   PASS  " if cond else "   FAIL  ") + msg)
    if not cond:
        FAILURES.append(msg)


def T(n, title):
    print("\n" + "=" * 74 + "\n%s. %s\n" % (n, title) + "=" * 74)


def at(m):
    return BASE + datetime.timedelta(minutes=m)


def feed(state, path, sym=SYM, start=0):
    """Record path[i] at minute start+i."""
    for i, p in enumerate(path):
        ob._momentum_record(state, sym, p, at(start + i))


def arm(state):
    return state.get(ob._ARMS_KEY, {}).get(ob._arm_key('CE', '10minute', STRIKE, DAY))


def approx(a, b, tol=1e-3):
    return a is not None and abs(a - b) <= tol


def t1_record_and_trim():
    T(1, "buffer: one value per minute, trimmed to MOMENTUM_HISTORY_MIN, bad quotes ignored")
    st = {}
    feed(st, [100 + i for i in range(40)])
    p = st[ob._LTP_HIST_KEY][SYM]['p']
    check(len(p) == ob.MOMENTUM_HISTORY_MIN + 1, "kept %d minutes (depth+current)" % len(p))
    check(min(int(k) for k in p) == 10 * 60 + 39 - ob.MOMENTUM_HISTORY_MIN, "oldest is t-depth")
    ob._momentum_record(st, SYM, 0, at(40))
    ob._momentum_record(st, SYM, None, at(40))
    check(str(10 * 60 + 40) not in st[ob._LTP_HIST_KEY][SYM]["p"], "ltp 0 / None not recorded")
    ob._momentum_record(st, SYM, 150.0, at(39))
    check(st[ob._LTP_HIST_KEY][SYM]['p'][str(10 * 60 + 39)] == 150.0, "same minute: last write wins")


def t2_prune_other_symbols_and_days():
    T(2, "buffer: other days and symbols gone quiet are dropped")
    st = {ob._LTP_HIST_KEY: {'OLD': {'day': '20260924', 'p': {'600': 10}}}}
    feed(st, [50.0], sym='QUIET', start=0)
    feed(st, [100.0 + i for i in range(30)], start=0)
    h = st[ob._LTP_HIST_KEY]
    check('OLD' not in h, "yesterday's symbol dropped")
    check('QUIET' not in h, "symbol unquoted for > depth dropped")
    check(SYM in h, "active symbol kept")


def t3_features():
    T(3, "features: speed, acceleration, 15-min range")
    st = {}
    # flat 100 for 10 min, then +2/min for 10 min -> accelerating into the zone
    path = [100.0] * 10 + [100.0 + 2 * i for i in range(1, 11)]
    feed(st, path)
    now, ltp = at(len(path) - 1), path[-1]      # 120 at minute 19
    f = ob._momentum_features(st, SYM, ltp, now)
    check(approx(f['v5'], (120 / 110 - 1) * 100 / 5), "v5 = %% per min over 5m (%s)" % f['v5'])
    check(approx(f['v10'], (120 / 100 - 1) * 100 / 10), "v10 (%s)" % f['v10'])
    check(f['v15'] is not None and f['v3'] is not None, "v3/v15 present with enough history")
    check(approx(f['acc'], f['v5'] - (110 / 100 - 1) * 100 / 5), "acc = v5 - prior 5m speed")
    check(f['hi15'] == 0.0 and f['lo15'] == 20.0, "hi15=0 (at high), lo15=+20%")
    # spike then fade: below the 15m high
    feed(st, [115.0], start=20)
    g = ob._momentum_features(st, SYM, 115.0, at(20))
    check(g['hi15'] < 0 and g['v3'] < 0, "fade shows negative hi15 and v3")


def t4_short_history_and_slack():
    T(4, "features: missing history -> None; a skipped cron minute uses +/- slack")
    st = {}
    feed(st, [100.0, 101.0])
    f = ob._momentum_features(st, SYM, 101.0, at(1))
    check(f['v5'] is None and f['v10'] is None and f['acc'] is None, "short history -> None")
    check(f['n'] == 2, "n = minutes of history")
    st = {}
    for m in range(0, 12):
        if m != 6:                               # cron missed minute 6 (t-5 at m=11)
            ob._momentum_record(st, SYM, 100.0 + m, at(m))
    f = ob._momentum_features(st, SYM, 111.0, at(11))
    # t-5 (minute 6) missing -> nearest is t-6 (105): speed divides by the REAL 6 minutes
    check(approx(f['v5'], (111 / 105 - 1) * 100 / 6), "slack uses real dt (%s)" % f['v5'])
    empty = ob._momentum_features({}, SYM, 100.0, at(0))
    check(empty['v5'] is None and empty['hi15'] == 0.0, "no buffer at all -> safe Nones")


def t5_stamps_on_arm():
    T(5, "stamps: mom_arm at arm, mom_zone once at first enterable scan, mom_entry at entry")
    st = {}
    feed(st, [90.0 + i for i in range(12)])
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 101.0, 9.0, at(11))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 101.0, 9.0, at(11))
    a = arm(st)
    check(a is not None and 'mom_arm' in a, "arm born in watch zone gets mom_arm")
    check('mom_zone' not in a, "no mom_zone while only watched")
    ob._momentum_record(st, SYM, 104.0, at(12))
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 104.0, 6.0, at(12))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 104.0, 6.0, at(12))
    z = dict(arm(st)['mom_zone'])
    check(z['v5'] is not None, "mom_zone stamped on first enterable scan")
    ob._momentum_record(st, SYM, 105.0, at(13))
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 105.0, 5.5, at(13))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 105.0, 5.5, at(13))
    check(arm(st)['mom_zone'] == z, "mom_zone not overwritten on later scans")
    ob._confirm_mark_entry(st, 'main', 'CE', '10minute', STRIKE, at(13), 105.0)
    ob._momentum_stamp(st, 'entry', 'main', 'CE', '10minute', STRIKE, SYM, 105.0, 5.5, at(13))
    check('mom_entry' in arm(st) and arm(st)['mom_entry']['v5'] is not None, "mom_entry stamped")
    # re-arm after leaving the band replaces the dict -> fresh mom_arm
    old = arm(st)
    old.pop('entered_at')
    feed(st, [100.0], start=40)
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 100.0, 9.0, at(40))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 100.0, 9.0, at(40))
    check(arm(st) is not old and 'mom_arm' in arm(st), "re-arm gets its own mom_arm")


def t6_safety():
    T(6, "safety: corrupt state and a disabled flag never raise and never write")
    st = {ob._LTP_HIST_KEY: "junk"}
    r = ob._momentum_record(st, SYM, 100.0, at(0))
    check(r is None and st[ob._LTP_HIST_KEY][SYM]['p'] == {'600': 100.0},
          "corrupt buffer self-heals on the same scan")
    st = {ob._LTP_HIST_KEY: {'BAD1': {'day': DAY, 'p': {'x': 1}}, 'BAD2': 'junk',
                             'BAD3': {'day': DAY, 'p': ['list']}}}
    ob._momentum_record(st, SYM, 100.0, at(0))
    check(set(st[ob._LTP_HIST_KEY]) == {SYM}, "unparseable sibling records pruned, not wedging")
    st = {ob._ARMS_KEY: {ob._arm_key('CE', '10minute', STRIKE, DAY): {'broken': True}},
          ob._LTP_HIST_KEY: {SYM: {'day': DAY, 'p': {'x': 'y'}}}}
    r = ob._momentum_stamp(st, 'entry', 'main', 'CE', '10minute', STRIKE, SYM, 100.0, 6.0, at(0))
    check(r is None, "corrupt history on entry: swallowed, returns None")
    ob.MOMENTUM_SHADOW = False
    try:
        st = {}
        ob._momentum_record(st, SYM, 100.0, at(0))
        check(st == {}, "flag off: nothing recorded")
    finally:
        ob.MOMENTUM_SHADOW = True


def t8_stale_events_not_stamped():
    T(8, "freshness: an arm/zone older than MOMENTUM_STAMP_MAX_AGE_MIN is not mis-stamped")
    st = {}
    feed(st, [100.0 + i for i in range(20)])
    # arm + zone exist from BEFORE the momentum code ran (mid-session deploy)
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 101.0, 6.5, at(1))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 119.0, 6.0, at(19))
    a = arm(st)
    check('mom_arm' in a and a['mom_arm'] is None, "stale arm -> mom_arm None (decided, not re-tried)")
    check('mom_zone' in a and a['mom_zone'] is None, "stale zone -> mom_zone None")
    # fresh stamps carry when/where they were measured
    st = {}
    feed(st, [100.0 + i for i in range(12)])
    ob._confirm_observe(st, 'main', 'CE', '10minute', STRIKE, 111.0, 4.2, at(11))
    ob._momentum_stamp(st, 'observe', 'main', 'CE', '10minute', STRIKE, SYM, 111.0, 4.2, at(11))
    z = arm(st)['mom_zone']
    check(z['gap'] == 4.2 and z['ltp'] == 111.0 and z['at'] == at(11).isoformat(),
          "zone stamp records gap/ltp/at (gap 4.2 = jumped past MIN_REWARD)")


def t9_analyser_parses_sticky():
    T(9, "analyser: STICKY lines parsed; a TF missing from a STICKY line is not a fake hit")
    import logging
    import tempfile
    import analyze_momentum as am
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, 'logs'))
    path = os.path.join(d, 'logs', 'option_buy_2026-09-26.log')
    fh = logging.FileHandler(path, encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s',
                                      datefmt='%H:%M:%S'))
    lg = logging.getLogger('momentum_test_writer')
    lg.propagate = False
    lg.addHandler(fh)
    lg.setLevel(logging.INFO)
    lg.info('  CE 23250 LTP=100.00 | 10M:6.0%↓ST | 15M:8.0%↓ST')
    lg.info('  STICKY CE 23300 LTP=90.00 | 15M:6.5%↓ST')
    fh.close()
    lg.removeHandler(fh)
    old_root, old_start = am.ROOT, am.START
    am.ROOT, am.START = d, '2026-09-26'
    try:
        series, _ = am.load()
    finally:
        am.ROOT, am.START = old_root, old_start
    keys = set(series)
    check(('2026-09-26', 'CE', 23300, '15M') in keys, "STICKY line parsed")
    check(('2026-09-26', 'CE', 23300, '10M') not in keys,
          "TF absent from STICKY line skipped (not scored as above ST)")
    check(('2026-09-26', 'CE', 23250, '10M') in keys, "ATM line still parsed")


def t7_never_branched_on():
    T(7, "control flow: every momentum call in option_buy is a bare statement")
    src = open(ob.__file__, encoding='utf-8').read()
    names = {'_momentum_record', '_momentum_stamp', '_log_sticky_ltp'}
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, 'id', None) in names]
    exprs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
    bare = sum(1 for c in calls if id(c) in exprs)
    check(len(calls) == 8, "found %d call sites" % len(calls))
    check(bare == len(calls), "all %d are bare statements (result never used)" % len(calls))


if __name__ == '__main__':
    for fn in (t1_record_and_trim, t2_prune_other_symbols_and_days, t3_features,
               t4_short_history_and_slack, t5_stamps_on_arm, t6_safety, t8_stale_events_not_stamped,
               t9_analyser_parses_sticky, t7_never_branched_on):
        fn()
    _muzzle.assert_clean()
    print("\n" + ("ALL PASS" if not FAILURES else "%d FAILURE(S):\n  " % len(FAILURES)
                  + "\n  ".join(FAILURES)))
    sys.exit(1 if FAILURES else 0)
