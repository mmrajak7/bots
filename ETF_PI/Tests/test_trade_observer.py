"""Verify the trade-lifecycle observer records correctly and cannot affect trading.

Two jobs, in order of importance:

  1. STRUCTURAL INERTNESS. Every trade_observer call inside option_buy.py must be a bare
     statement — never assigned, never branched on, never returned. This is checked by
     parsing the AST, not by reading the code, because a reviewer's eye is exactly what
     failed the last time an instrumentation layer was assumed inert.

  2. MEASUREMENT CORRECTNESS. The overshoot arithmetic is the number the whole module
     exists to produce, so it is driven against the real historical case it was built
     from (NIFTY_CE_10minute_20260508: COST_SL floor 176.30, filled 153.10, -13.2%).

Run either way:
    python Tests/test_trade_observer.py
    python -m Tests.test_trade_observer
"""
import ast
import datetime
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_observer as obs  # noqa: E402

IST = obs.IST
BASE = IST.localize(datetime.datetime(2026, 8, 19, 10, 0, 0))

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


def pos(**kw):
    """A position dict shaped like the ones _open_pos writes."""
    p = {
        'status': 'open', 'opt_type': 'CE', 'signal_tf': '10minute',
        'strike': 24200, 'expiry': '2026-05-13',
        'option_symbol': 'NIFTY26050824200CE', 'exchange': 'NFO',
        'entry_premium': 176.20, 'entry_spot': 24180.0,
        'st_value': 186.39, 'entry_gap': -5.5,
        't2_value': 0, 't2_tf': '', 't1_hit': False,
        'entry_time': BASE.isoformat(), 'entry_date': '20260508',
        'peak_premium': 176.20, 'cost_sl_active': False, 'cost_sl_level': 0,
        'lot_size': 65, 'tick_size': 0.05,
    }
    p.update(kw)
    return p


def read_records(path):
    out, torn = [], 0
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                torn += 1
    return out, torn


# ---------------------------------------------------------------------------
T(1, "STRUCTURAL INERTNESS — the AST check")
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src_path = os.path.join(ROOT, 'option_buy.py')
src = ast.parse(open(src_path, encoding='utf-8').read())

HOOKS = ('on_open', 'on_tick', 'on_exit')

# Every Call whose func is trade_observer.<hook>
all_calls, bare_calls = {h: 0 for h in HOOKS}, {h: 0 for h in HOOKS}


def hook_name(node):
    """Return the hook name if `node` is a trade_observer.<hook>(...) call."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if (isinstance(f, ast.Attribute) and f.attr in HOOKS
            and isinstance(f.value, ast.Name) and f.value.id == 'trade_observer'):
        return f.attr
    return None


for node in ast.walk(src):
    h = hook_name(node)
    if h:
        all_calls[h] += 1

# A bare statement is an Expr node whose value IS the call.
for node in ast.walk(src):
    if isinstance(node, ast.Expr):
        h = hook_name(node.value)
        if h:
            bare_calls[h] += 1

for h in HOOKS:
    check(all_calls[h] > 0, "option_buy.py calls trade_observer.%s() (%d site(s))"
          % (h, all_calls[h]))
    check(all_calls[h] == bare_calls[h],
          "every trade_observer.%s() call is a BARE STATEMENT — never assigned, "
          "branched on, or returned (%d/%d)" % (h, bare_calls[h], all_calls[h]))

# The import must exist and must be a plain module import, so the AST check above can
# rely on the `trade_observer.` prefix being the only way to reach these functions.
imported = any(isinstance(n, ast.Import)
               and any(a.name == 'trade_observer' and a.asname is None for a in n.names)
               for n in ast.walk(src))
check(imported, "option_buy.py imports trade_observer as a plain module (no alias, "
                "no from-import that would defeat the prefix check)")

from_imported = [n for n in ast.walk(src)
                 if isinstance(n, ast.ImportFrom) and n.module == 'trade_observer']
check(not from_imported,
      "no `from trade_observer import ...` — a bare name would escape the AST check")

# ---------------------------------------------------------------------------
T(2, "THE OVERSHOOT MEASUREMENT — the real 2026-05-08 case")
# ---------------------------------------------------------------------------
# COST_SL floor 176.30 (entry 176.20 + 0.10), actual fill 153.10.
p = pos(cost_sl_active=True, cost_sl_level=176.30, peak_premium=188.65, t1_hit=True)
lvl, desc = obs._named_level(p, 'COST_SL', 176.20, 186.39, 0, 188.65)
check(abs(lvl - 176.30) < 1e-9, "COST_SL names cost_sl_level (176.30)")
check(desc == 'cost_sl_level', "level description identifies the rule that set it")

over_pct = (153.10 - lvl) / lvl * 100
check(abs(over_pct - (-13.156)) < 0.01,
      "overshoot on the historical case = -13.16%% (ledger said -13.2%%)")
check(abs((153.10 - lvl) * 65 - (-1508.0)) < 1.0,
      "overshoot in rupees = -Rs1,508 on 65 lot (matches the ledger finding)")

# TRAIL names the higher of the 50%-of-peak line and a live cost floor.
# Deliberately NOT a tie: at entry 178.80 / peak 223.84 the trail line is exactly
# 201.32, so a cost floor set to that same number tests `>` against equality rather
# than testing the precedence this check is about.
p2 = pos(cost_sl_active=True, cost_sl_level=205.00, peak_premium=223.84,
         entry_premium=178.80)
lvl2, desc2 = obs._named_level(p2, 'TRAIL', 178.80, 190.0, 0, 223.84)
check(abs(lvl2 - 205.00) < 1e-9 and desc2 == 'cost_floor_over_trail',
      "TRAIL uses the cost floor when it sits above the 50%%-of-peak line")

# The tie itself: floor == trail line. Either answer books the same price, so the only
# requirement is that it does not crash and returns that price.
p2b = pos(cost_sl_active=True, cost_sl_level=201.32, peak_premium=223.84,
          entry_premium=178.80)
lvl2b, _ = obs._named_level(p2b, 'TRAIL', 178.80, 190.0, 0, 223.84)
check(abs(lvl2b - 201.32) < 1e-9,
      "floor exactly equal to the trail line resolves to that price either way")

p3 = pos(cost_sl_active=False, cost_sl_level=0, peak_premium=220.0,
         entry_premium=180.0)
lvl3, desc3 = obs._named_level(p3, 'TRAIL', 180.0, 190.0, 0, 220.0)
check(abs(lvl3 - 200.0) < 1e-9 and desc3 == 'trail_50pct_of_peak',
      "TRAIL uses entry + 50%% of peak gain when no cost floor is live")

# TP names T2 only once T1 has actually been hit.
lvl4, desc4 = obs._named_level(pos(t1_hit=True), 'TP', 176.20, 186.39, 199.0, 190.0)
check(abs(lvl4 - 199.0) < 1e-9 and desc4 == 't2', "TP after T1 names T2")
lvl5, desc5 = obs._named_level(pos(t1_hit=False), 'TP', 176.20, 186.39, 199.0, 190.0)
check(abs(lvl5 - 186.39) < 1e-9 and desc5 == 't1', "TP before T1 names T1")

# Time/shape exits have no price level — inventing one would manufacture findings.
for et in ('TIME_SL', 'ADV_SL', 'EOD'):
    lvl6, desc6 = obs._named_level(pos(), et, 176.20, 186.39, 0, 180.0)
    check(lvl6 is None and desc6 == '',
          "%s has no named level (overshoot is not a meaningful question)" % et)

# ---------------------------------------------------------------------------
T(3, "RECORDS ARE WRITTEN AND WELL-FORMED")
# ---------------------------------------------------------------------------
tmpdir = tempfile.mkdtemp(prefix='obs_test_')
_orig_path = obs._obs_path
obs._obs_path = lambda: os.path.join(tmpdir, 'logs', 'trade_obs.jsonl')

obs.on_open('K1', pos(), thresholds={'PREMIUM_SL_PCT': 35, 'TRAIL_PCT': 50})
obs.on_tick('K1', pos(peak_premium=180.0), 179.5, time_sl_cap=30,
            now=BASE + datetime.timedelta(minutes=5))
obs.on_exit('K1', pos(cost_sl_active=True, cost_sl_level=176.30,
                      peak_premium=188.65, t1_hit=True),
            153.10, 'COST_SL', now=BASE + datetime.timedelta(minutes=22))

recs, torn = read_records(obs._obs_path())
check(torn == 0, "no torn lines written")
check([r['type'] for r in recs] == ['plan', 'tick', 'exit'],
      "one plan, one tick, one exit — in order")
check(all(r.get('schema') == obs.OBS_SCHEMA for r in recs),
      "every record carries the schema version")
check(all(r.get('key') == 'K1' for r in recs), "every record carries the position key")

plan, tick, ex = recs
check(abs(plan['t1_distance'] - 10.19) < 0.01,
      "plan records the T1 distance in rupees (10.19)")
check(plan['thresholds'].get('PREMIUM_SL_PCT') == 35,
      "plan stamps the config in force for THIS trade")
check(abs(plan['cost_floor_at_entry'] - 176.30) < 1e-9,
      "plan records the cost floor the trade will be held to")

# Distance convention: negative = breached.
check(tick['d_t1'] < 0, "d_t1 negative while premium is below T1 (not yet reached)")
check(abs(tick['d_prem_sl'] - (179.5 - 176.20 * 0.65)) < 0.01,
      "d_prem_sl measures distance above the -35%% stop")
check(tick['time_left_min'] == 25.0, "time_left_min = cap 30 - elapsed 5")
check(tick['adv_peak_below'] is True and tick['adv_drift_below'] is False,
      "ADV_SL inputs recorded as the two separate conditions")

check(abs(ex['overshoot_pct'] - (-13.156)) < 0.01,
      "exit record carries the overshoot the module exists to measure")
check(ex['has_level'] is True, "COST_SL flagged as a levelled exit")
check(abs(ex['pnl_rs'] - ((153.10 - 176.20) * 65)) < 0.01, "exit records P&L in rupees")

# ---------------------------------------------------------------------------
T(3.5, "LEVELS FOLLOW THE LIVE CONFIG, NOT HARDCODED COPIES")
# ---------------------------------------------------------------------------
# Regression (review, 2026-08-21): these were literals, so changing TRAIL_PCT or
# PREMIUM_SL_PCT in option_buy would have left the observer measuring against levels
# the bot never used — the drifting second copy its own docstring warns about.
_saved = dict(obs._CFG)
try:
    obs.configure(TRAIL_PCT=40.0, PREMIUM_SL_PCT=30.0)
    lvl, desc = obs._named_level(pos(cost_sl_active=False, cost_sl_level=0),
                                 'TRAIL', 100.0, 110.0, 0, 200.0)
    check(abs(lvl - 140.0) < 1e-9 and '40pct' in desc,
          "TRAIL_PCT=40 moves the trail level to entry + 40%% of peak gain (140.00)")
    lvl2, desc2 = obs._named_level(pos(), 'PREM_SL', 100.0, 110.0, 0, 100.0)
    check(abs(lvl2 - 70.0) < 1e-9 and '30pct' in desc2,
          "PREMIUM_SL_PCT=30 moves the stop to 70.00, and the label says so")
    # A trade's own stamped thresholds beat the live config, so an April trade is
    # judged by April's constants rather than today's.
    lvl3, _ = obs._named_level(pos(), 'PREM_SL', 100.0, 110.0, 0, 100.0,
                               cfg={'PREMIUM_SL_PCT': 35.0})
    check(abs(lvl3 - 65.0) < 1e-9,
          "a per-trade `cfg` override wins — history is judged by its own era")
    check(obs.configure(NOT_A_REAL_KEY=1) and 'NOT_A_REAL_KEY' not in obs._CFG,
          "an unknown key is ignored, never raised")
    obs.configure(TRAIL_PCT='junk')
    check(obs._CFG['TRAIL_PCT'] == 40.0, "a junk value leaves the previous one intact")
finally:
    obs._CFG.clear()
    obs._CFG.update(_saved)
check(obs._CFG['TRAIL_PCT'] == 50.0, "defaults restored for the remaining checks")

# ---------------------------------------------------------------------------
T(4, "IT CANNOT BREAK TRADING")
# ---------------------------------------------------------------------------


class Boom(object):
    def __float__(self):
        raise RuntimeError("boom")

    def __sub__(self, other):
        raise RuntimeError("boom")


before = len(read_records(obs._obs_path())[0])
r1 = obs.on_open('K2', {'entry_premium': Boom()})
r2 = obs.on_tick('K2', {'entry_premium': Boom()}, Boom())
r3 = obs.on_exit('K2', {'entry_premium': Boom()}, Boom(), 'COST_SL')
check(r1 is None and r2 is None and r3 is None,
      "a malformed position returns None from every hook — never raises")

obs.on_tick('K3', pos(), 0)
obs.on_tick('K4', pos(entry_premium=0), 100.0)
check(True, "zero/absent premium handled without raising")

# The flag is a total no-op.
obs.OBSERVER_ENABLED = False
n_before = len(read_records(obs._obs_path())[0])
obs.on_open('K5', pos())
obs.on_tick('K5', pos(), 180.0)
obs.on_exit('K5', pos(), 180.0, 'TP')
n_after = len(read_records(obs._obs_path())[0])
check(n_before == n_after, "OBSERVER_ENABLED=False writes nothing at all")
obs.OBSERVER_ENABLED = True

# An unwritable log directory must not raise into the caller.
obs._obs_path = lambda: os.path.join(tmpdir, 'nope\x00bad', 'x.jsonl')
check(obs.on_exit('K6', pos(), 180.0, 'TP') is None,
      "an unwritable path is swallowed — trading unaffected")
obs._obs_path = _orig_path

# ---------------------------------------------------------------------------
print("\n" + "=" * 74)
if FAILURES:
    print("FAILED (%d)" % len(FAILURES))
    for f in FAILURES:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 74)
