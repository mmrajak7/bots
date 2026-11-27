"""Verify the T1 give-back floor, the floor-reason split, and the 30M T2 anchor.

Covers the three changes shipped 2026-09-01:

  1. T1_GIVEBACK  — on T1 hit the floor goes to entry + T1_GIVEBACK_KEEP_PCT% of the
                    entry->T1 gain, closing the hole between the T1 LOCK (peak >= T1x1.02)
                    and the TRAIL (peak gain >= 8%) where a position kept NOTHING.
  2. sl_reason    — the three rules that can arm the floor now report distinct exit types,
                    so a profit-take at T1 is no longer booked as 'COST_SL'.
  3. 30M T2       — 15M's T2 anchor moves 60M -> 30M, with the 60M level still recorded as
                    t2_alt_value for counterfactual pricing.

The exit ladder is driven through the real monitor_positions() with Kite stubbed out, so
these assert the SHIPPING code path rather than a re-implementation of it.

Run either way:
    python Tests/test_t1_giveback.py
    python -m Tests.test_t1_giveback
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import option_buy as ob  # noqa: E402
import trade_observer as tobs  # noqa: E402

import _muzzle  # noqa: E402  MUST be imported before anything drives option_buy.

# The muzzle neutralises BOTH live side effects of driving the real ladder: Telegram (the
# T1-HIT and cost-SL alerts are sent from inside monitor_positions, not just _close_pos) and
# the FileHandler that would append synthetic trades into the live log. Both happened for
# real on 2026-09-01. See Tests/_muzzle.py.
_muzzle.assert_clean()
SENT = _muzzle.SENT
_LOGGED = _muzzle.LOGGED

IST = ob.IST
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


# ---------------------------------------------------------------------------
# Harness: run the REAL monitor_positions() against a scripted price path.
# ---------------------------------------------------------------------------

def run_path(pos, prices):
    """Feed `prices` one per scan into monitor_positions(); return the closed position.

    Patches only the I/O boundary (_load/_save/_quote/Telegram/observer/bridge) so the
    exit ladder, the floor arming and the reason stamping all execute for real.
    """
    key = 'NIFTY_CE_15minute_TEST'
    store = {key: dict(pos)}
    saved = {}

    orig = (ob._load, ob._save, ob._quote, ob._close_pos, ob.TRADE_ENABLED,
            ob.TF_TIME_SL, tobs.on_tick, tobs.on_exit)
    feed = list(prices)

    def fake_load(_path):
        return store

    def fake_save(data, _path):
        store.update(data)

    def fake_quote(_sym):
        return {'ltp': feed[0], 'bid': feed[0], 'day_high': 0}

    def fake_close(k, p, prem, etype):
        p['status'] = 'closed'
        p['exit_type'] = etype
        p['exit_premium'] = prem
        saved.update(p)

    ob._load, ob._save, ob._quote, ob._close_pos = fake_load, fake_save, fake_quote, fake_close
    ob.TRADE_ENABLED = False
    ob.TF_TIME_SL = {}                       # disable TIME_SL so it cannot pre-empt
    tobs.on_tick = lambda *a, **k: None
    tobs.on_exit = lambda *a, **k: None
    try:
        while feed:
            ob.monitor_positions()
            if store[key].get('status') == 'closed':
                break
            feed.pop(0)
    finally:
        (ob._load, ob._save, ob._quote, ob._close_pos, ob.TRADE_ENABLED,
         ob.TF_TIME_SL, tobs.on_tick, tobs.on_exit) = orig
    return saved or store[key]


def base_pos(entry, t1, t2=0.0):
    return {
        'status': 'open', 'opt_type': 'CE', 'signal_tf': '15minute',
        'strike': 24050, 'option_symbol': 'NIFTYTESTCE', 'exchange': 'NFO',
        'entry_premium': entry, 'entry_spot': 24000.0,
        'st_value': t1, 'entry_gap': -6.3,
        't2_value': t2, 't2_tf': '30minute' if t2 else '', 't1_hit': False,
        'entry_time': datetime.datetime.now(IST).isoformat(),
        'entry_date': datetime.datetime.now(IST).strftime('%Y%m%d'),
        'peak_premium': entry, 'cost_sl_active': False, 'cost_sl_level': 0,
        'lot_size': 65, 'tick_size': 0.05,
    }


# ---------------------------------------------------------------------------
T(1, "_arm_floor ratchets UP only and stamps the arming rule")
# ---------------------------------------------------------------------------
p = {}
check(ob._arm_floor(p, 100.0, ob.SL_COST) is True, "arms an unarmed floor")
check(p['cost_sl_level'] == 100.0 and p['cost_sl_active'] is True, "sets level and active flag")
check(p['sl_reason'] == ob.SL_COST, "stamps sl_reason = COST_SL")

check(ob._arm_floor(p, 120.0, ob.SL_T1_GIVEBACK) is True, "raises the floor 100 -> 120")
check(p['sl_reason'] == ob.SL_T1_GIVEBACK, "reason follows the raise")

check(ob._arm_floor(p, 110.0, ob.SL_COST) is False, "REFUSES to lower the floor 120 -> 110")
check(p['cost_sl_level'] == 120.0, "floor unchanged after a refused lower")
check(p['sl_reason'] == ob.SL_T1_GIVEBACK, "reason unchanged after a refused lower")

check(ob._arm_floor(p, 120.0, ob.SL_T1_LOCK) is False, "equal level is not a raise (no-op)")
check(ob._arm_floor({}, 0.0, ob.SL_COST) is False, "rejects a zero level")
check(ob._arm_floor({}, -5.0, ob.SL_COST) is False, "rejects a negative level")


# ---------------------------------------------------------------------------
T(2, "THE BUG: 2026-08-31 CE 24050 replayed — the give-back hole")
# ---------------------------------------------------------------------------
# Real tape: entry 119.20, T1 127.24, T2 211.12 (60M).
#   12:05 128.10  T1 hit  (peak/T1 = 1.007, gain +7.47%)
#   12:06 121.55 / 12:07 125.45 / 12:08 124.05 / 12:09 120.10 / 12:10 119.15
# Old ladder: lock needed 129.79, trail needed 8% -> neither fired, floor stayed 119.30,
# booked -0.05. New ladder must protect half the T1 gain: 119.20 + 0.5*8.04 = 123.22.
out = run_path(base_pos(119.20, 127.24, 211.12),
               [119.00, 121.75, 128.10, 121.55, 125.45, 124.05, 120.10, 119.15])
check(out['status'] == 'closed', "position closed")
check(out['exit_type'] == 'T1_GIVEBACK',
      "exits as T1_GIVEBACK, not COST_SL (got %s)" % out['exit_type'])
check(abs(out['cost_sl_level'] - 123.22) < 0.01,
      "floor armed at 123.22 = entry + 50%% of the T1 gain (got %.2f)" % out['cost_sl_level'])
check(out['exit_premium'] == 121.55,
      "fills on the first poll below the floor, 121.55 (got %.2f)" % out['exit_premium'])
pnl = (out['exit_premium'] - 119.20) * 65
check(pnl > 0, "turns the real -3 into a profit (+%.0f)" % pnl)


# ---------------------------------------------------------------------------
T(3, "T1 LOCK still overrides the give-back floor, and reports as T1_LOCK")
# ---------------------------------------------------------------------------
# 2026-09-01 PE 24050 shape: peak clears T1 x 1.02, so the floor must ratchet to T1
# itself — strictly above the give-back floor — and the exit must say so.
out = run_path(base_pos(109.95, 116.065, 122.383),
               [109.90, 116.20, 119.10, 115.25])
check(out['exit_type'] == 'T1_LOCK',
      "exits as T1_LOCK, not COST_SL (got %s)" % out['exit_type'])
check(abs(out['cost_sl_level'] - 116.065) < 0.001,
      "floor ratcheted all the way to T1 (got %.3f)" % out['cost_sl_level'])
check(out['cost_sl_level'] > 109.95 + 0.5 * (116.065 - 109.95),
      "T1 lock floor is strictly above the give-back floor it replaced")


# ---------------------------------------------------------------------------
T(4, "A genuine break-even stop still reports COST_SL")
# ---------------------------------------------------------------------------
# Never reaches T1: premium closes to within COST_SL_GAP_PCT of ST, arming the plain
# break-even floor, then rolls over. This is the 2026-09-01 PE 24000 shape.
out = run_path(base_pos(96.75, 103.43, 0.0),
               [96.70, 100.00, 102.20, 96.60])
check(out['exit_type'] == 'COST_SL',
      "unchanged: still COST_SL when T1 was never hit (got %s)" % out['exit_type'])
check(out.get('t1_hit') is False, "t1_hit stays False — it came 1.2% short")
check(abs(out['cost_sl_level'] - 96.85) < 0.001, "floor is entry + 0.10")


# ---------------------------------------------------------------------------
T(5, "Backward compatibility with positions written before 2026-09-01")
# ---------------------------------------------------------------------------
old = base_pos(100.0, 110.0, 0.0)
old.update(cost_sl_active=True, cost_sl_level=100.10)   # armed, but no 'sl_reason' key
check('sl_reason' not in old, "fixture has no sl_reason, like a pre-change position")
out = run_path(old, [99.00])
check(out['exit_type'] == 'COST_SL',
      "an sl_reason-less position still books COST_SL (got %s)" % out['exit_type'])


# ---------------------------------------------------------------------------
T(6, "Boundary: the floor can never sit below break-even")
# ---------------------------------------------------------------------------
# Entry gating makes T1 <= entry impossible, but nothing asserts it, and a floor below
# entry+0.10 would be a SILENT STOP WIDENING — the exact class of bug this change fixes.
out = run_path(base_pos(100.0, 100.0, 150.0), [100.00, 99.00])
check(out['cost_sl_level'] >= 100.10 - 1e-9,
      "T1 == entry still floors at entry + 0.10 (got %.2f)" % out['cost_sl_level'])

# And the give-back floor must always sit strictly BELOW T1 — that is what makes it
# unable to stop out a runner the old ladder would have kept.
for entry, t1 in ((119.20, 127.24), (96.75, 103.43), (48.55, 52.20), (232.95, 249.39)):
    floor = max(entry + 0.10, entry + (t1 - entry) * (ob.T1_GIVEBACK_KEEP_PCT / 100))
    check(entry < floor < t1, "floor sits strictly between entry and T1 (%.2f < %.2f < %.2f)"
          % (entry, floor, t1))


# ---------------------------------------------------------------------------
T(7, "30M is the 15M T2 anchor; it is NOT a signal timeframe")
# ---------------------------------------------------------------------------
check('30minute' in ob.ALL_TIMEFRAMES, "30minute is computed")
check('30minute' not in ob.SIGNAL_TIMEFRAMES,
      "30minute generates NO entries — the 2026-05-12 verdict stands")
check(ob.TF_NEXT.get('15minute') == '30minute', "15M T2 anchor is now 30M")
check(ob.TF_NEXT.get('10minute') == '15minute',
      "10M anchor UNCHANGED — its 15M T2 was already well sized (8 of 20 reached)")
check(ob.TF_NEXT.get('60minute') is None, "60M is still last: no T2")
check(ob.T2_ALT.get('15minute') == '60minute', "60M retained as the counterfactual anchor")
check(ob.T2_ALT.get('10minute') is None, "10M has no alt anchor — nothing changed for it")

# The anchor cascade: T2 may only get CLOSER, never vanish. Timeframes disagree — measured
# on the live tape 2026-09-01, 30M can be UP while 15M is DOWN, or print below the 15M ST.
def pick_t2(levels, st_val, tf='15minute'):
    """Mirror of the selection in check_and_enter, exercised over synthetic level sets."""
    for cand in (ob.TF_NEXT.get(tf), ob.T2_ALT.get(tf)):
        if not cand:
            continue
        cd = levels.get(cand, {})
        if cd.get('direction') == 'DOWN' and cd.get('value', 0) > st_val:
            return cd['value'], cand
    return 0, ''

D = lambda v: {'direction': 'DOWN', 'value': v}
U = lambda v: {'direction': 'UP', 'value': v}
check(pick_t2({'30minute': D(150.0), '60minute': D(210.0)}, 130.0) == (150.0, '30minute'),
      "both qualify -> takes the NEARER 30M anchor")
check(pick_t2({'30minute': U(150.0), '60minute': D(210.0)}, 130.0) == (210.0, '60minute'),
      "30M is UP -> falls back to 60M rather than dropping T2")
check(pick_t2({'30minute': D(125.0), '60minute': D(210.0)}, 130.0) == (210.0, '60minute'),
      "30M prints BELOW the signal ST -> falls back to 60M")
check(pick_t2({'30minute': U(150.0), '60minute': U(210.0)}, 130.0) == (0, ''),
      "neither qualifies -> no T2, exactly as before the change")
check(pick_t2({'15minute': D(150.0)}, 130.0, '10minute') == (150.0, '15minute'),
      "10M still anchors on 15M")
check(pick_t2({'15minute': U(150.0), '30minute': D(200.0)}, 130.0, '10minute') == (0, ''),
      "10M does NOT cascade to 30M — its anchor was never in question")
check(ob.TF_DISPLAY.get('30minute') == '30M', "30M renders as '30M', not '?'")
check(ob.ALL_TIMEFRAMES.index('30minute') == ob.ALL_TIMEFRAMES.index('15minute') + 1,
      "timeframes stay in ascending order")


# ---------------------------------------------------------------------------
T(8, "_record_watched_strike stores BOTH anchors for the sticky path")
# ---------------------------------------------------------------------------
lv = {'15minute': {'direction': 'DOWN', 'value': 130.0, 'close': 120.0},
      '30minute': {'direction': 'DOWN', 'value': 150.0, 'close': 120.0},
      '60minute': {'direction': 'DOWN', 'value': 210.0, 'close': 120.0}}
st = {}
ob._record_watched_strike(st, 'CE', 24050, 'SYM', '2026-09-08', '15minute', 130.0, lv)
rec = st['watched_strikes']['24050_CE']['levels']
check(rec.get('30minute') == 150.0, "live T2 anchor (30M) recorded")
check(rec.get('60minute') == 210.0, "counterfactual anchor (60M) recorded too")
check(ob._has_signal_level(st['watched_strikes']['24050_CE']),
      "record still has a signal level, so it is not swept as dead weight")

# An anchor that stops qualifying must be dropped, not left stale.
lv['30minute'] = {'direction': 'UP', 'value': 150.0, 'close': 160.0}
ob._record_watched_strike(st, 'CE', 24050, 'SYM', '2026-09-08', '15minute', 130.0, lv)
rec = st['watched_strikes']['24050_CE']['levels']
check('30minute' not in rec, "an anchor that turns UP is dropped, not resurrected as T2")
check(rec.get('60minute') == 210.0, "the other anchor is unaffected by that drop")

# Below the signal TF's own ST it is not a reachable target either.
lv['60minute'] = {'direction': 'DOWN', 'value': 125.0, 'close': 120.0}
ob._record_watched_strike(st, 'CE', 24050, 'SYM', '2026-09-08', '15minute', 130.0, lv)
check('60minute' not in st['watched_strikes']['24050_CE']['levels'],
      "an anchor BELOW the signal ST is dropped")


# ---------------------------------------------------------------------------
T(9, "Telegram renders the new exits with real labels, never '?'")
# ---------------------------------------------------------------------------
pos = base_pos(119.20, 127.24, 211.12)
pos['t1_hit'] = True
for et, want in (('T1_GIVEBACK', 'T1 GIVE-BACK SL'), ('T1_LOCK', 'T1 LOCKED'),
                 ('COST_SL', 'COST SL')):
    html = ob._fmt_exit(pos, 123.22, et)
    check(want in html, "%s renders as '%s'" % (et, want))
    check(not html.startswith('<b>? '), "%s has a real icon, not '?'" % et)


# ---------------------------------------------------------------------------
T(10, "The observer measures the new exits against their floor")
# ---------------------------------------------------------------------------
pos = base_pos(119.20, 127.24, 211.12)
pos.update(t1_hit=True, cost_sl_active=True, cost_sl_level=123.22,
           sl_reason='T1_GIVEBACK', peak_premium=128.10)
for et, want_desc in (('T1_GIVEBACK', 't1_giveback_level'),
                      ('T1_LOCK', 't1_lock_level'),
                      ('COST_SL', 'cost_sl_level')):
    lvl, desc = tobs._named_level(pos, et, 119.20, 127.24, 211.12, 128.10)
    check(lvl == 123.22, "%s names the recorded floor 123.22 (got %s)" % (et, lvl))
    check(desc == want_desc, "%s describes its own rule as '%s'" % (et, want_desc))
    check(et in tobs._LEVELLED_EXITS, "%s is measurable for overshoot" % et)
check(tobs._CFG.get('T1_GIVEBACK_KEEP_PCT') is not None,
      "observer carries T1_GIVEBACK_KEEP_PCT so replays use the right constant")


# ---------------------------------------------------------------------------
T(11, "The give-back floor cannot stop a trade the OLD ladder would have kept")
# ---------------------------------------------------------------------------
# The safety argument for this change, asserted rather than asserted-in-prose: whenever
# the old ladder had ANY protection armed (T1 lock at T1, or the 50%-of-peak trail), that
# level is already at or above the new give-back floor. So the new floor can only bind on
# trades the old ladder left at break-even.
worst = None
for gap_pct in (5.0, 5.5, 6.0, 6.5, 7.0):
    entry = 120.0
    t1 = entry / (1 - gap_pct / 100)
    give = entry + (t1 - entry) * (ob.T1_GIVEBACK_KEEP_PCT / 100)
    check(give < t1, "gap %.1f%%: give-back floor < T1 lock floor" % gap_pct)
    # Trail arms at peak >= entry * 1.08; at exactly that peak the trail sits at 50% of it.
    peak = entry * (1 + ob.TRAIL_MIN_GAIN_PCT / 100)
    trail = entry + (peak - entry) * (ob.TRAIL_PCT / 100)
    if worst is None or trail - give < worst:
        worst = trail - give
    check(give <= max(trail, give), "gap %.1f%%: floor never below the armed trail" % gap_pct)
check(worst is not None, "checked the trail comparison across the whole entry-gap band")


# ---------------------------------------------------------------------------
T(0, "REGRESSION GUARD: this test file must never reach the live Telegram channel")
# ---------------------------------------------------------------------------
# monitor_positions() alerts from inside the ladder, so a stubbed _close_pos alone lets
# synthetic "T1 HIT" messages into the real channel. Assert the muzzle is installed AND
# that it actually caught traffic, so a future refactor that reorders the imports fails
# here rather than in the user's phone.
check(ob.common.sendMsgTelegram.__name__ == '<lambda>', "sendMsgTelegram is the test stub")
check(len(SENT) > 0, "the muzzle intercepted %d alert(s) that would otherwise have been sent"
      % len(SENT))
check(any('T1 HIT' in m for m in SENT), "including the in-ladder T1 HIT alert")
check(any('COST SL' in m for m in SENT), "and the in-ladder cost-SL-armed alert")

# ...and the log file. A test that drives the ladder must not append synthetic trade lines
# into logs/option_buy_<today>.log, which the live bot shares and digest.py reads.
_leaked = _muzzle.live_log_leaks()
check(_leaked == 0, "no test line has leaked into any live option_buy log (found %d)" % _leaked)
check(_muzzle.assert_clean() is True, "muzzle still installed at end of run")
check(len(_LOGGED) > 0, "ladder log records were captured in memory instead (%d)" % len(_LOGGED))
check(any('T1 hit' in m for m in _LOGGED), "including the T1-hit record the ladder emits")


# ---------------------------------------------------------------------------
T(12, "A single poll spanning T1 AND T2 books TP at once, not a minute later")
# ---------------------------------------------------------------------------
# Hazard introduced by the closer 30M anchor: one 1-minute poll can now cover the whole
# T1->T2 move. The old `t1_hit` guard made that scan mark T1 and hold, so the position
# rode the top of the spike for a full extra minute before it could book.
out = run_path(base_pos(119.20, 127.24, 135.00), [119.00, 136.50])
check(out['exit_type'] == 'TP', "books TP on the spanning scan (got %s)" % out['exit_type'])
check(out['t1_hit'] is True, "t1_hit still recorded, so the ledger reads correctly")
check(out['exit_premium'] == 136.50, "fills at the observed price, above T2")
check('T2 TARGET' in ob._fmt_exit(out, 136.50, 'TP'), "alert reads 'T2 TARGET', not 'TARGET'")

# The ordinary two-scan path must be unchanged: T1 on one scan, T2 on a later one.
out = run_path(base_pos(119.20, 127.24, 135.00), [119.00, 128.10, 130.00, 135.20])
check(out['exit_type'] == 'TP', "normal T1-then-T2 path still books TP")
check(out['exit_premium'] == 135.20, "and does so on the scan that crosses T2")

# A T1-only position (no T2) is untouched by the guard change.
out = run_path(base_pos(96.75, 103.43, 0.0), [96.70, 104.00])
check(out['exit_type'] == 'TP', "T1-only still exits TP at T1 (got %s)" % out['exit_type'])
check(out.get('t1_hit') is False, "T1-only never sets t1_hit, so the label stays 'TARGET'")


print("\n" + "=" * 74)
if FAILURES:
    print("FAILED: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 74)
