"""Verify the Phase C deterministic reporters: digest.py and preflight.py.

Both exist to be trusted on a bad morning, so the checks that matter most are the
degradation ones: a missing ledger, an unreadable log, an empty observer record. A
reporter that raises when the day went wrong is a reporter that is absent exactly when
it is needed.

Run either way:
    python Tests/test_digest_preflight.py
    python -m Tests.test_digest_preflight
"""
import datetime
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digest  # noqa: E402
import preflight as pf  # noqa: E402

IST = digest.IST
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


tmp = tempfile.mkdtemp(prefix='phasec_')
os.makedirs(os.path.join(tmp, 'logs'), exist_ok=True)
DAY = '20260821'
ISO = '2026-08-21'


def write_log(lines):
    with open(os.path.join(tmp, 'logs', 'option_buy_%s.log' % ISO), 'w',
              encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')


def write_positions(d):
    with open(os.path.join(tmp, 'st_opt_positions.json'), 'w') as fh:
        json.dump(d, fh)


def write_obs(recs):
    with open(os.path.join(tmp, 'logs', 'trade_obs.jsonl'), 'w', encoding='utf-8') as fh:
        for r in recs:
            fh.write(json.dumps(r) + '\n')


def write_cfg(enabled=False, log_only=True):
    with open(os.path.join(tmp, 'config_trade.yaml'), 'w') as fh:
        fh.write('trade_enabled: %s\nlog_only: %s\nmax_loss_per_day: 10000\n'
                 'num_lots: 1\n' % (str(enabled).lower(), str(log_only).lower()))


digest.CURRENT_DIR = tmp
pf.CURRENT_DIR = tmp
write_cfg()

# ---------------------------------------------------------------------------
T(1, "DIGEST — a clean day")
# ---------------------------------------------------------------------------
write_log([
    '09:00:03 | INFO    | NIFTY OPTION MAGNET',
    '09:00:05 | INFO    | --- LTP SCAN ---',
    '09:00:05 | INFO    | COMPLETE',
    '09:01:03 | INFO    | NIFTY OPTION MAGNET',
    '09:01:05 | INFO    | COMPLETE',
])
write_positions({
    'NIFTY_CE_10minute_%s' % DAY: {
        'status': 'closed', 'opt_type': 'CE', 'signal_tf': '10minute',
        'strike': 24200, 'entry_premium': 100.0, 'exit_premium': 110.0,
        'exit_type': 'TP', 'lot_size': 65, 'entry_date': DAY,
        'entry_time': '%sT09:30:00+05:30' % ISO,
        'exit_time': '%sT09:45:00+05:30' % ISO,
        'st_value': 108.0, 't2_value': 0, 't1_hit': False, 'peak_premium': 110.0,
    }})
write_obs([])
d = digest.build(DAY)
check(d['trades']['n'] == 1, "one closed trade found")
check(abs(d['trades']['gross'] - 650.0) < 0.01, "gross P&L = (110-100)*65 = Rs650")
check(d['cycles']['cycles'] == 2 and d['cycles']['completed'] == 2,
      "2 cycles, both completed")
check(d['cycles']['gaps'] == [], "consecutive minutes are not a gap")
check('PAPER' in d['mode']['mode'], "mode reported as PAPER")
check(d['mode']['guardian'] == 'DORMANT', "guardian reported dormant on paper")
md = digest.render(d)
check('## Flags' in md and '## Trades' in md, "markdown renders its sections")
check(md.index('## Flags') < md.index('## Trades'),
      "FLAGS come first — the part worth reading is not buried")

# ---------------------------------------------------------------------------
T(2, "DIGEST — flags name what happened")
# ---------------------------------------------------------------------------
write_log([
    '09:00:03 | INFO    | NIFTY OPTION MAGNET',
    '09:00:05 | INFO    | COMPLETE',
    '09:09:03 | INFO    | NIFTY OPTION MAGNET',          # 9-minute hole
    '09:09:05 | INFO    | COMPLETE',
    '09:10:03 | INFO    | NIFTY OPTION MAGNET',           # started, never completed
    '09:11:00 | ERROR   | Neo on_exit failed: boom',
    '09:11:01 | ERROR   | Exit alert failed K: telegram down',
    '09:12:00 | INFO    | ORPHAN POSITION: broker holds 65 of NIFTYX but no live '
    'software position tracks it',
    '09:13:00 | INFO    | NO-ENTRY WINDOW: CE 10M 24200 gap=6.0% - entry suppressed',
])
d = digest.build(DAY)
flags = ' | '.join(d['flags'])
check('MISSED CYCLES' in flags, "a 9-minute hole is flagged as missed cycles")
check(any('9.0 min' in f for f in d['flags']), "and the gap length is named")
check('never logged COMPLETE' in flags, "an incomplete cycle is flagged")
check('ORPHAN' in flags, "an orphan position is flagged")
check('alert failure' in flags.lower() or 'Telegram' in flags,
      "a lost Telegram alert is flagged (every caller swallows the exception)")
check('no_entry_window' in (d['gates'] or {}), "the no-entry gate is counted")
check('LEDGER/OBSERVER MISMATCH' in flags,
      "a closed trade with no observer record is flagged — this is exactly how "
      "broken instrumentation hides")

# ---------------------------------------------------------------------------
T(3, "DIGEST — overshoot is the headline it was built for")
# ---------------------------------------------------------------------------
write_obs([
    {'schema': 1, 'type': 'plan', 'ts': '%sT09:30:00+05:30' % ISO, 'key': 'K1'},
    {'schema': 1, 'type': 'tick', 'ts': '%sT09:31:00+05:30' % ISO, 'key': 'K1'},
    {'schema': 1, 'type': 'tick', 'ts': '%sT09:32:00+05:30' % ISO, 'key': 'K1'},
    {'schema': 1, 'type': 'exit', 'ts': '%sT09:45:00+05:30' % ISO, 'key': 'K1',
     'exit_type': 'COST_SL', 'named_level_desc': 'cost_sl_level',
     'named_level': 176.30, 'exit_premium': 153.10, 'overshoot_pct': -13.16,
     'overshoot_rs': -1508.0, 'has_level': True, 'lot_size': 65},
])
d = digest.build(DAY)
check(d['conformance']['exits'] == 1, "the exit record is read")
check(len(d['conformance']['bad']) == 1, "a -13% fill is picked up as an outlier")
check(any('EXIT PAST ITS LEVEL' in f for f in d['flags']),
      "and flagged with the level, the fill and the rupees")
check(abs(d['conformance']['net_vs_level'] - (-1508.0)) < 1,
      "net-vs-level totals correctly")
md = digest.render(d)
check('OUTLIER' in md,
      "the render states the total is NOT the number to watch — poll granularity "
      "moves both ways and the aggregate is net favourable")

# ---------------------------------------------------------------------------
T(4, "DIGEST — degrades instead of raising")
# ---------------------------------------------------------------------------
os.remove(os.path.join(tmp, 'st_opt_positions.json'))
d = digest.build(DAY)
check('error' in (d['trades'] or {}), "a missing ledger is reported as an error field")
check(isinstance(digest.render(d), str), "and the digest still renders")
check(isinstance(digest.summary_line(d), str), "and still produces a Telegram line")

shutil.rmtree(os.path.join(tmp, 'logs'))
os.makedirs(os.path.join(tmp, 'logs'))
d = digest.build('20260101')
check(isinstance(digest.render(d), str), "a day with no data at all still renders")
check(d['cycles']['cycles'] == 0, "and reports zero cycles rather than guessing")

write_positions({})
write_obs([])
with open(os.path.join(tmp, 'logs', 'trade_obs.jsonl'), 'a', encoding='utf-8') as fh:
    fh.write('{ this is not json\n')
d = digest.build(DAY)
check(d['conformance']['torn'] == 1, "a torn observer line is counted, not fatal")
check(any('torn' in f for f in d['flags']), "and flagged")

# ---------------------------------------------------------------------------
T(5, "PREFLIGHT — verdict and severity")
# ---------------------------------------------------------------------------
now = IST.localize(datetime.datetime(2026, 8, 21, 8, 55, 0))
write_cfg(enabled=False, log_only=True)
write_positions({})

rep = pf.Report()
pf.check_mode(rep)
check(any('PAPER' in r['detail'] for r in rep.rows), "mode stated in words")
check(any(r['name'] == 'guardian' and 'DORMANT' in r['detail'] for r in rep.rows),
      "guardian posture stated every morning")

rep = pf.Report()
write_cfg(enabled=True, log_only=False)
pf.check_mode(rep)
check(any('LIVE' in r['detail'] and 'REAL ORDERS' in r['detail'] for r in rep.rows),
      "LIVE mode is impossible to misread")
write_cfg(enabled=False, log_only=True)

# Stale positions FAIL.
rep = pf.Report()
write_positions({'OLD': {'status': 'open', 'entry_date': '20260819'}})
pf.check_positions(rep, now)
check(rep.failures and 'previous day' in rep.failures[0]['detail'],
      "a position carried from a previous day FAILS the preflight")
check(rep.ready is False, "and the verdict is NOT READY")

rep = pf.Report()
write_positions({'TODAY': {'status': 'open', 'entry_date': DAY}})
pf.check_positions(rep, now)
check(rep.ready is True, "a position opened TODAY is not a carry and does not fail")

# Warnings never block.
rep = pf.Report()
pf.check_digest(rep, now)
check(rep.warnings and rep.ready is True,
      "a missing prior digest WARNS but never blocks the desk")

# Clock stepping backwards.
rep = pf.Report()
write_positions({})
future = now - datetime.timedelta(days=3)
os.utime(os.path.join(tmp, 'st_opt_positions.json'),
         (future.timestamp() + 86400 * 10, future.timestamp() + 86400 * 10))
pf.check_clock(rep, now)
check(rep.failures and 'FUTURE' in rep.failures[0]['detail'],
      "a file timestamped in the future FAILS — the Pi clock stepped backwards")

# Instruments.
rep = pf.Report()
pf.check_instruments(rep, now)
check(len(rep.failures) == 2, "both missing instrument dumps FAIL")

with open(os.path.join(tmp, 'instruments.csv'), 'w') as fh:
    fh.write('x' * 200000)
with open(os.path.join(tmp, 'op_instruments.csv'), 'w') as fh:
    fh.write('x' * 200000)
old = (now - datetime.timedelta(days=2)).timestamp()
os.utime(os.path.join(tmp, 'instruments.csv'), (old, old))
rep = pf.Report()
pf.check_instruments(rep, now)
check(any('not today' in r['detail'] for r in rep.failures),
      "a dump that did not refresh today FAILS")

# Neo is not checked on paper.
rep = pf.Report()
pf.check_neo(rep, now, {'trade_enabled': False, 'log_only': True})
check(rep.ready and any('paper mode' in r['detail'] for r in rep.rows),
      "neo session is not demanded on the paper book")
rep = pf.Report()
pf.check_neo(rep, now, {'trade_enabled': True, 'log_only': False})
check(rep.failures, "but once LIVE, a missing neo session FAILS")

# Observer silence. Set the state explicitly rather than inheriting whatever an
# earlier section left on disk — the earlier torn-line test leaves the file non-empty.
obs_path = os.path.join(tmp, 'logs', 'trade_obs.jsonl')
open(obs_path, 'w').close()                       # exists but empty
rep = pf.Report()
pf.check_instrumentation(rep, now)
check(rep.warnings and rep.ready,
      "an EMPTY observer record WARNS — silent instrumentation is how a layer dies")

os.remove(obs_path)                               # does not exist at all
rep = pf.Report()
pf.check_instrumentation(rep, now)
check(rep.warnings and 'does not exist' in rep.warnings[0]['detail'],
      "a missing observer record WARNS too, and says which of the two it is")

with open(obs_path, 'w', encoding='utf-8') as fh:  # actually recording
    fh.write('{"schema": 1, "type": "plan", "key": "K"}\n')
rep = pf.Report()
pf.check_instrumentation(rep, now)
check(not rep.warnings and not rep.failures,
      "a recording observer passes quietly")

# ---------------------------------------------------------------------------
T(6, "PREFLIGHT — renders and never raises")
# ---------------------------------------------------------------------------
rep = pf.run(now, probe=False)
check(isinstance(pf.render(rep, now), str), "text report renders")
check(isinstance(pf.render_telegram(rep, now), str), "telegram report renders")
txt = pf.render(rep, now)
check(('READY' in txt), "the verdict word appears in the first line")
tg = pf.render_telegram(rep, now)
check('MODE:' in tg, "the Telegram report leads with the mode")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
if FAILURES:
    print("FAILED (%d)" % len(FAILURES))
    for f in FAILURES:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 74)
