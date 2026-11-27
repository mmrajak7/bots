"""Verify the B6 abrupt-move tripwire, and that it gates NOTHING while unpriced.

The single most important check in this file is section 5: with BLOCK_ENTRIES False,
`should_block()` must return False no matter how violent the inputs are. The thresholds
in turbulence.py are unmeasured guesses, and the ledger says a wrongly-closed desk can
cost more than the worst day ever lost (best day +Rs7,605 vs worst -Rs4,313). Until the
measurement exists, "records and alerts, blocks nothing" is the contract.

Run either way:
    python Tests/test_turbulence.py
    python -m Tests.test_turbulence
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turbulence as tb  # noqa: E402

IST = tb.IST
NOW = IST.localize(datetime.datetime(2026, 8, 20, 9, 45, 0))

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


tmp = tempfile.mkdtemp(prefix='turb_test_')
tb.CURRENT_DIR = tmp   # redirect state + log into the sandbox


def reset():
    shutil.rmtree(os.path.join(tmp, 'logs'), ignore_errors=True)


def calm(**kw):
    """A thoroughly ordinary day."""
    base = dict(vix=12.5, vix_prev_close=12.4, spot=24500, prev_close=24480,
                day_open=24490, day_high=24540, day_low=24460, now=NOW)
    base.update(kw)
    return tb.evaluate(**base)


# ---------------------------------------------------------------------------
T(1, "A CALM DAY IS CALM")
# ---------------------------------------------------------------------------
v = calm()
check(v['verdict'] == tb.CALM, "ordinary tape => CALM")
check(v['signals_ok'] == 3, "all three voting signals contributed (VIX, jump, gap)")
check(v['reasons'] == [], "no reasons recorded on a calm day")

# ---------------------------------------------------------------------------
T(2, "EACH SIGNAL ESCALATES ON ITS OWN")
# ---------------------------------------------------------------------------
# NOTE: vix_prev_close is moved with vix on purpose. Leaving it at the calm default
# makes the JUMP signal fire too, so the test would not be isolating the LEVEL signal.
# (That the module behaves this way is correct: a VIX of 19 that was 12.4 yesterday is
# a bigger event than a VIX of 19 that was 18.5 yesterday.)
check(calm(vix=19.0, vix_prev_close=18.5)['verdict'] == tb.ELEVATED,
      "VIX level 19, flat vs yesterday => ELEVATED")
check(calm(vix=26.0, vix_prev_close=25.5)['verdict'] == tb.TURBULENT,
      "VIX level 26, flat vs yesterday => TURBULENT")
check(calm(vix=13.9, vix_prev_close=12.4)['verdict'] == tb.ELEVATED,
      "VIX +12% vs prev close (level still calm) => ELEVATED")
check(calm(vix=15.5, vix_prev_close=12.4)['verdict'] == tb.TURBULENT,
      "VIX +25% vs prev close (level still calm) => TURBULENT")
check(calm(vix=19.0, vix_prev_close=12.4)['verdict'] == tb.TURBULENT,
      "level ELEVATED + jump TURBULENT => TURBULENT (worst signal wins)")
check(calm(day_open=24160, prev_close=24480)['verdict'] == tb.ELEVATED,
      "NIFTY gapped -1.31% => ELEVATED (below the 1.75% turbulent line)")

# Gap is judged on ABSOLUTE size — this book trades both CE and PE.
up = calm(day_open=24480 * 1.012, prev_close=24480)
dn = calm(day_open=24480 * 0.988, prev_close=24480)
check(up['verdict'] == tb.ELEVATED and dn['verdict'] == tb.ELEVATED,
      "a +1.2% gap and a -1.2% gap escalate identically (both sides are traded)")
check(calm(day_open=24480 * 1.02, prev_close=24480)['verdict'] == tb.TURBULENT,
      "a 2% gap => TURBULENT")

# Worst signal wins.
v = calm(vix=26.0, day_open=24480 * 1.012, prev_close=24480)
check(v['verdict'] == tb.TURBULENT and len(v['reasons']) >= 2,
      "worst signal wins and every reason is recorded (%d reasons)" % len(v['reasons']))

# ---------------------------------------------------------------------------
T(3, "RANGE IS RECORDED BUT NEVER VOTES")
# ---------------------------------------------------------------------------
v = calm(day_high=25100, day_low=24000, prev_close=24480)
check(v['range_pct'] is not None and v['range_pct'] > 4.0,
      "a 4.5%% intraday range is measured (%.2f%%)" % v['range_pct'])
check(v['range_notable'] is True, "and flagged as notable")
check(v['verdict'] == tb.CALM,
      "but the verdict stays CALM — range grows through the session, so a fixed "
      "threshold would trip every ordinary afternoon")

# ---------------------------------------------------------------------------
T(4, "MISSING DATA IS NOT CALM")
# ---------------------------------------------------------------------------
v = tb.evaluate(now=NOW)
check(v['verdict'] == tb.CALM and v['signals_ok'] == 0,
      "no inputs => verdict CALM but signals_ok=0, so a reader can tell the "
      "difference between 'quiet' and 'blind'")
v = tb.evaluate(vix=26.0, now=NOW)
check(v['signals_ok'] == 1 and v['verdict'] == tb.TURBULENT,
      "one usable signal still votes")
v = tb.evaluate(vix=None, vix_prev_close=0, spot=1, prev_close=0, day_open=1, now=NOW)
check(v['signals_ok'] == 0, "zero/None denominators are refused, not divided by")

# REGRESSION (found in review 2026-08-21). _scan_turbulence runs from 09:00 but the
# market opens 09:15, so Kite's ohlc.open is 0 until the first print. Guarding only the
# denominator made that a -100% gap, which cleared every threshold: the tripwire
# latched TURBULENT and alerted EVERY morning, and would have closed the desk daily if
# blocking were ever enabled.
v = tb.evaluate(vix=12.0, vix_prev_close=11.9, spot=24200, prev_close=24480,
                day_open=0, day_high=0, day_low=0, now=NOW)
check(v['verdict'] == tb.CALM,
      "*** pre-market ohlc.open=0 is CALM, not a -100%% gap ***")
check(v['gap_pct'] is None, "the gap reports unavailable rather than -100%")
check(v['signals_ok'] == 2, "and the missing gap does not count as a usable signal")
v = tb.evaluate(vix=0, vix_prev_close=11.9, spot=24200, prev_close=24480,
                day_open=24490, now=NOW)
check(v['vix_jump_pct'] is None and v['signals_ok'] == 1,
      "a zero VIX print is 'no data', not a -100% collapse")
check(tb.evaluate(vix='junk', prev_close='junk', day_open='junk', now=NOW)
      ['signals_ok'] == 0, "non-numeric inputs are refused without raising")

# ---------------------------------------------------------------------------
T(5, "*** IT GATES NOTHING WHILE UNPRICED ***")
# ---------------------------------------------------------------------------
reset()
check(tb.BLOCK_ENTRIES is False,
      "BLOCK_ENTRIES ships False — thresholds are unmeasured guesses")

worst = calm(vix=45.0, vix_prev_close=12.0, day_open=24480 * 1.05, prev_close=24480)
check(worst['verdict'] == tb.TURBULENT, "a catastrophic tape reads TURBULENT")
tb.observe(worst)
check(tb.should_block(NOW) is False,
      "*** should_block() is False even on the worst possible reading, because "
      "BLOCK_ENTRIES is off ***")
check(worst['block_enabled'] is False,
      "every record stamps whether blocking was enabled when it was written")

# With the flag on it does latch — proving the wiring works, so flipping the flag is
# the only change needed once the measurement lands.
tb.BLOCK_ENTRIES = True
try:
    reset()
    tb.observe(calm(vix=45.0, vix_prev_close=12.0))
    check(tb.should_block(NOW) is True, "with the flag ON, a turbulent day blocks")
    check(tb.should_block(NOW + datetime.timedelta(days=1)) is False,
          "the block does not leak into the next day")
    reset()
    tb.observe(calm(vix=19.0, vix_prev_close=18.5))
    check(tb.day_summary(NOW)['worst'] == tb.ELEVATED, "day latched ELEVATED")
    check(tb.should_block(NOW) is False,
          "ELEVATED alone does not block — only TURBULENT does")
finally:
    tb.BLOCK_ENTRIES = False

# ---------------------------------------------------------------------------
T(6, "LATCHING, DEDUPE AND ALERTS")
# ---------------------------------------------------------------------------
reset()
sent = []
tb.observe(calm(), notify=sent.append)
check(sent == [], "a calm day sends no alert")

tb.observe(calm(vix=26.0), notify=sent.append)
check(len(sent) == 1, "escalating to TURBULENT sends exactly one alert")
check('TURBULENCE' in sent[0] and 'VIX' in sent[0],
      "the alert names the verdict and the reason")
check('NOT blocked' in sent[0] or 'not blocked' in sent[0].lower(),
      "and states plainly that entries were not blocked")

tb.observe(calm(vix=26.0), notify=sent.append)
tb.observe(calm(vix=27.0), notify=sent.append)
check(len(sent) == 1, "the same verdict does not re-alert every scan")

# The latch only ratchets up.
tb.observe(calm(), notify=sent.append)
check(tb.day_summary(NOW)['worst'] == tb.TURBULENT,
      "a later calm reading does NOT un-latch the day — the event still happened")
check(len(sent) == 1, "and de-escalating sends no alert")

# A fresh day resets.
tomorrow = NOW + datetime.timedelta(days=1)
tb.observe(calm(now=tomorrow), notify=sent.append)
check(tb.day_summary(tomorrow)['worst'] == tb.CALM, "the latch resets with the date")

# ELEVATED then TURBULENT alerts twice — the second is new information.
reset()
sent2 = []
tb.observe(calm(vix=19.0, vix_prev_close=18.5), notify=sent2.append)
tb.observe(calm(vix=26.0, vix_prev_close=25.5), notify=sent2.append)
check(len(sent2) == 2, "ELEVATED then TURBULENT alerts on each escalation")

# ---------------------------------------------------------------------------
T(7, "IT CANNOT BREAK TRADING")
# ---------------------------------------------------------------------------
reset()
# Unwritable state path: observe must not raise.
_sp = tb._state_path
tb._state_path = lambda: os.path.join(tmp, 'no\x00pe', 's.json')
try:
    tb.observe(calm(vix=26.0))
    check(True, "an unwritable state file is swallowed, not raised")
except Exception as e:
    check(False, "unwritable state file raised: %s" % e)
finally:
    tb._state_path = _sp

# Corrupt state file must not raise and must not block.
reset()
os.makedirs(os.path.join(tmp, 'logs'), exist_ok=True)
open(tb._state_path(), 'w').write('{ not json')
check(tb._load_state() == {}, "a corrupt state file reads as empty, not an exception")
check(tb.should_block(NOW) is False, "and cannot cause a spurious block")

# Records are valid JSONL.
reset()
tb.observe(calm(vix=26.0))
with open(tb._log_path(), encoding='utf-8') as fh:
    lines = [json.loads(x) for x in fh if x.strip()]
check(len(lines) >= 1 and lines[-1]['verdict'] == tb.TURBULENT,
      "the record is well-formed JSONL carrying the verdict")
check('latched_worst' in lines[-1], "and the day's latched worst alongside it")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
if FAILURES:
    print("FAILED (%d)" % len(FAILURES))
    for f in FAILURES:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 74)
