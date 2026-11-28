#!/usr/bin/env python3
"""One trading day, reduced to something a human can read in a minute.

WHAT THIS IS
------------
The deterministic half of the end-of-day report. It reads the day's ledger, the trade
observer's flight recorder, the turbulence tripwire and the bot's own log, and states
what happened. It does not speculate about why — that discipline is borrowed from
zebra's digest and it is load-bearing: a report that mixes fact and theory trains the
reader to skim both.

It NEVER raises. A missing digest is an inconvenience; it must not be able to matter
more than that, and it must certainly never be able to affect trading. It also never
needs Claude: an agent may later append its observations to the same file, but the
facts below are computed here, so the report still lands on a day the CLI is down.

FLAGS ARE THE POINT
-------------------
Sections 1-6 are context. Section 7 is the part that earns the read: facts that deserve
a look, each naming what happened and nothing more. If a day produces no flags, that is
itself the message and the digest says so in one line.

    python digest.py                 # today
    python digest.py --day 20260819  # a specific day
    python digest.py --send          # also push a summary to Telegram
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

import pytz

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, CURRENT_DIR)

IST = pytz.timezone('Asia/Kolkata')

#: Cron runs option_buy every minute from 09:00; the session ends 15:30.
SESSION_START = datetime.time(9, 0)
SESSION_END = datetime.time(15, 30)

#: A gap longer than this between cycle markers is a MISSED cycle, not jitter. Cron
#: fires each minute but a slow Kite call can push a run past the next minute boundary,
#: so 2 minutes is the first gap that means a run genuinely did not happen.
MISSED_CYCLE_SEC = 150

#: Which underlying's files this digest reads (2026-10-06, one bot process per underlying).
#: Set by --underlying; NIFTY keeps its original, unsuffixed file names and output.
UNDERLYING = 'NIFTY'
SFX = ''


def _f(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _safe(fn):
    """A section that fails must not take the digest down with it."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as e:
            return {'error': '%s: %s' % (type(e).__name__, e)}
    wrapper.__name__ = fn.__name__
    return wrapper


def _mtime(path):
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
            '%Y-%m-%d %H:%M')
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _log_path(day):
    iso = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
    return os.path.join(CURRENT_DIR, 'logs', 'option_buy%s_%s.log' % (SFX, iso))


def _read_log(day):
    """[(datetime, level, message)] for one day, or [] if the file is gone."""
    path = _log_path(day)
    if not os.path.exists(path):
        return []
    rows = []
    base = datetime.datetime.strptime(day, '%Y%m%d')
    pat = re.compile(r'^(\d{2}):(\d{2}):(\d{2}) \| (\w+)\s*\| (.*)$')
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            m = pat.match(line.rstrip('\n'))
            if not m:
                continue
            h, mi, s, lvl, msg = m.groups()
            rows.append((base.replace(hour=int(h), minute=int(mi), second=int(s)),
                         lvl.strip(), msg))
    return rows


def _read_jsonl(path, day=None):
    recs, torn = [], 0
    if not os.path.exists(path):
        return recs, torn
    with open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                torn += 1
                continue
            if day:
                ts = str(r.get('ts') or '')
                rday = r.get('day') or (ts[:10].replace('-', '') if ts else '')
                if rday != day:
                    continue
            recs.append(r)
    return recs, torn


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _executor_cfg():
    """The executor's merged config (defaults + the box's executor_local.yaml), or None."""
    try:
        from executor import config as XC
        return XC.load()
    except Exception:
        return None


@_safe
def _mode():
    """PAPER or LIVE, in words. E5: a half-flipped config must be impossible to miss.

    2026-11-28: the old neo/ bridge (trade_enabled) is retired; live orders come ONLY from the
    executor daemon, so its config decides the word. The paper ledger below is always paper."""
    x = _executor_cfg()
    if x is None:
        return {'mode': 'UNKNOWN (executor config unreadable)', 'guardian': '?'}
    if x['mode'] == 'live':
        word = 'LIVE — executor, %d lot(s), %s, live_until %s' % (x['lots'], x['product'],
                                                                 x['live_until'])
    elif x['mode'] == 'paper':
        word = 'PAPER — executor simulating fills on live prices (no real orders)'
    else:
        word = 'PAPER only — executor off'
    return {'mode': word, 'executor_mode': x['mode'], 'lots': x['lots'],
            'guardian': 'ARMED' if x['mode'] == 'live' else 'DORMANT'}


@_safe
def _executor(day):
    """The executor's book for `day` beside the paper ledger, per key (the same key).

    Per closed trade: live entry/exit fills, the rule that fired and how, P&L gross and net of
    estimated charges, and the gap to the PAPER row of the same signal -- the number the
    'study and improve' loop is run on. Plus every SKIP with its reason."""
    x = _executor_cfg()
    if x is None or x['mode'] == 'off':
        return {'mode': (x or {}).get('mode', '?'), 'trades': [], 'skips': []}
    jp = os.path.join(CURRENT_DIR, 'state', 'executor',
                      'journal_%s.jsonl' % ('live' if x['mode'] == 'live' else 'paper'))
    iso = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
    last, skips = {}, []
    if os.path.exists(jp):
        with open(jp, encoding='utf-8') as fh:
            for ln in fh:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if not str(r.get('ts') or '').startswith(iso):
                    continue
                if r.get('ev') == 'SKIP':
                    skips.append({'key': r.get('key'), 'why': r.get('why')})
                if isinstance(r.get('pos'), dict) and r.get('key'):
                    last[r['key']] = r['pos']
    try:
        with open(os.path.join(CURRENT_DIR, 'st_opt_positions%s.json' % SFX)) as fh:
            paper = json.load(fh)
    except (OSError, ValueError):
        paper = {}
    out = []
    for k, p in sorted(last.items()):
        pp = paper.get(k) or {}
        lot = int(p.get('lot_size') or 65)
        row = {'key': k, 'state': p.get('state'), 'exit_type': p.get('exit_type'),
               'how': (p.get('exit_detail') or {}).get('how'),
               'qty': p.get('sold') or p.get('qty'), 'entry': p.get('entry_premium'),
               'exit': p.get('exit_avg'), 'realised': p.get('realised'),
               'charges': p.get('charges_est'), 'net': p.get('net_est'),
               'entry_slip': p.get('entry_slip'), 'note': p.get('note'),
               'paper_entry': pp.get('entry_premium'), 'paper_exit': pp.get('exit_premium'),
               'paper_exit_type': pp.get('exit_type')}
        if pp.get('exit_premium') is not None and pp.get('entry_premium') is not None:
            row['paper_pnl'] = round((float(pp['exit_premium']) - float(pp['entry_premium'])) * lot, 2)
        out.append(row)
    closed = [r for r in out if r['state'] == 'CLOSED']
    return {'mode': x['mode'], 'trades': out, 'skips': skips,
            'gross': round(sum(float(r['realised'] or 0) for r in closed), 2),
            'net': round(sum(float(r['net'] or 0) for r in closed), 2),
            'paper_same_keys': round(sum(float(r.get('paper_pnl') or 0) for r in closed), 2)}


@_safe
def _cycles(rows, day):
    """Cycle count and — the number that matters — MISSED cycles.

    A missed cycle is a minute in which no scan ran: no exit could fire, no entry could
    be taken. On a bot whose stops are evaluated once a minute that is the difference
    between a rule and a hope.
    """
    starts = [t for t, lvl, m in rows if m == '%s OPTION MAGNET' % UNDERLYING]
    completes = [t for t, lvl, m in rows if m == 'COMPLETE']
    scans = [t for t, lvl, m in rows if m == '--- LTP SCAN ---']
    # main() sys.exit()s before COMPLETE on weekends and outside 09:15-15:30, so those
    # runs are SUPPOSED to end early. Counting them as incomplete reported 15 phantom
    # failures on the first real day this ran — the 09:00-09:14 warm-up runs.
    early = len([m for t, lvl, m in rows
                 if m.startswith('Off hours') or m == 'Weekend'
                 or m.startswith('Market closed today') or m.endswith('not enabled in this build — exiting')])
    gaps = []
    for a, b in zip(starts, starts[1:]):
        delta = (b - a).total_seconds()
        if delta > MISSED_CYCLE_SEC:
            gaps.append({'from': a.strftime('%H:%M:%S'),
                         'to': b.strftime('%H:%M:%S'),
                         'minutes': round(delta / 60.0, 1)})
    return {'cycles': len(starts), 'completed': len(completes), 'scans': len(scans),
            'early_exit': early,
            'incomplete': max(len(starts) - len(completes) - early, 0),
            'first': starts[0].strftime('%H:%M:%S') if starts else None,
            'last': starts[-1].strftime('%H:%M:%S') if starts else None,
            'gaps': gaps}


@_safe
def _trades(day):
    """The day's closed and still-open positions from the ledger."""
    path = os.path.join(CURRENT_DIR, 'st_opt_positions%s.json' % SFX)
    with open(path) as fh:
        positions = json.load(fh)
    closed, still_open = [], []
    for key, p in positions.items():
        if p.get('entry_date') != day:
            continue
        entry = _f(p.get('entry_premium'))
        lot = int(_f(p.get('lot_size'), 0))
        rec = {'key': key, 'opt_type': p.get('opt_type'), 'tf': p.get('signal_tf'),
               'strike': p.get('strike'), 'entry': entry, 'lot': lot,
               'entry_time': (p.get('entry_time') or '')[11:19],
               't1': _f(p.get('st_value')), 't2': _f(p.get('t2_value')),
               't1_hit': bool(p.get('t1_hit')), 'half': p.get('session_half')}
        if p.get('status') == 'closed':
            ex = _f(p.get('exit_premium'))
            rec.update(exit=ex, exit_type=p.get('exit_type'),
                       exit_time=(p.get('exit_time') or '')[11:19],
                       pnl=(ex - entry) * lot)
            closed.append(rec)
        else:
            still_open.append(rec)
    pnls = [t['pnl'] for t in closed]
    gross = sum(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    return {'closed': closed, 'open': still_open, 'gross': gross,
            'n': len(closed), 'wins': len(wins),
            'wr': (100.0 * len(wins) / len(pnls)) if pnls else None,
            'pf': (sum(wins) / sum(losses)) if losses else (None if not wins else float('inf')),
            'by_exit': dict(collections.Counter(t['exit_type'] for t in closed)),
            'by_half': _by_half(closed)}


def _by_half(closed):
    """Morning vs evening (ledger session_half, split at underlyings.SESSION_SPLIT): the data the
    MCX session rule is decided on (2026-10-27). {half: {'n', 'gross', 'wins'}}."""
    out = {}
    for t in closed:
        h = out.setdefault(t.get('half') or '?', {'n': 0, 'gross': 0.0, 'wins': 0})
        h['n'] += 1
        h['gross'] += t.get('pnl', 0)
        h['wins'] += 1 if t.get('pnl', 0) > 0 else 0
    return out


def _half_text(by_half):
    """'pre17 2 trades Rs -2112 (0 won) · 17+ 3 trades Rs +2850 (1 won)', morning first."""
    order = ('pre17', '17+')
    return ' · '.join('%s %d trades Rs %+.0f (%d won)' % (h, v['n'], v['gross'], v['wins'])
                      for h, v in sorted(by_half.items(),
                                         key=lambda kv: order.index(kv[0]) if kv[0] in order else 9))


@_safe
def _conformance(day):
    """What the trade observer saw: fills vs the levels the rules named."""
    recs, torn = _read_jsonl(os.path.join(CURRENT_DIR, 'logs', 'trade_obs%s.jsonl' % SFX), day)
    plans = [r for r in recs if r.get('type') == 'plan']
    ticks = [r for r in recs if r.get('type') == 'tick']
    exits = [r for r in recs if r.get('type') == 'exit']
    overs = []
    for e in exits:
        op = e.get('overshoot_pct')
        if op is None:
            continue
        overs.append({'key': e.get('key'), 'exit_type': e.get('exit_type'),
                      'level_desc': e.get('named_level_desc'),
                      'level': e.get('named_level'), 'fill': e.get('exit_premium'),
                      'pct': op, 'rs': e.get('overshoot_rs')})
    bad = sorted([o for o in overs if o['pct'] < -1.0], key=lambda o: o['pct'])
    ticks_by_key = collections.Counter(t.get('key') for t in ticks)
    thin = [e.get('key') for e in exits if ticks_by_key.get(e.get('key'), 0) < 2]
    return {'plans': len(plans), 'ticks': len(ticks), 'exits': len(exits),
            'torn': torn, 'overshoots': overs, 'bad': bad, 'thin': thin,
            'net_vs_level': sum(_f(o['rs']) for o in overs if o['rs'] is not None)}


@_safe
def _gates(rows):
    """What was suppressed today, and by which rule."""
    pats = [
        ('no_entry_window', r'^NO-ENTRY WINDOW:'),
        ('eod_runway', r'^EOD RUNWAY:'),
        ('min_t1_distance', r'^MIN T1 DIST:'),
        ('watch_gate', r'^WATCH GATE:'),
        ('turbulence', r'^TURBULENCE GATE:'),
        ('sticky_drop', r'^STICKY DROP:'),
        ('liquidity', r'^LIQ_BLOCK:'),
        ('event_block', r'^EVENT_BLOCK:'),
        ('event_flat', r': EVENT_FLAT'),
        ('event_fail_closed', r'^EVENT FAIL-CLOSED'),
        ('magnet_touch', r'^MAGNET TOUCH'),
        ('shadow', r'^SHADOW:'),
    ]
    out = {}
    for name, pat in pats:
        rx = re.compile(pat)
        hits = [m for t, lvl, m in rows if rx.search(m)]
        if hits:
            out[name] = {'count': len(hits), 'examples': hits[:3]}
    return out


@_safe
def _guardian(rows, day):
    """Broker-truth interlocks: what they said today."""
    armed = [m for t, lvl, m in rows if m.startswith('Guardian (broker-truth')]
    return {
        'startup_line': armed[-1] if armed else None,
        'orphans': [m for t, lvl, m in rows if 'ORPHAN POSITION' in m],
        'disputed': [m for t, lvl, m in rows if 'DISPUTED REJECTION' in m],
        'broker_conflict': [m for t, lvl, m in rows if 'BROKER CONFLICT' in m],
        'unreachable': len([m for t, lvl, m in rows
                            if 'broker UNREACHABLE' in m or 'Broker position fetch failed' in m]),
    }


@_safe
def _turbulence(day):
    if SFX:   # the NIFTY+VIX tripwire runs in the NIFTY process only
        return {'observed': False}
    recs, _ = _read_jsonl(os.path.join(CURRENT_DIR, 'logs', 'turbulence.jsonl'), day)
    if not recs:
        return {'observed': False}
    import turbulence as tb
    worst = max(recs, key=lambda r: tb._RANK.get(r.get('verdict'), 0))
    return {'observed': True, 'worst': worst.get('verdict'),
            'reasons': worst.get('reasons', []),
            'blocked': bool(worst.get('block_enabled')),
            'vix': worst.get('vix'), 'gap_pct': worst.get('gap_pct'),
            'range_pct': worst.get('range_pct'),
            'signals_ok': worst.get('signals_ok')}


@_safe
def _problems(rows):
    """Errors and warnings, grouped by shape so 200 identical lines read as one."""
    def shape(m):
        return re.sub(r'\d+([.,]\d+)?', 'N', m)[:110]
    errs = collections.Counter(shape(m) for t, lvl, m in rows if lvl == 'ERROR')
    warns = collections.Counter(shape(m) for t, lvl, m in rows if lvl == 'WARNING')
    # E6: every sendMsgTelegram caller swallows its exception, so a Telegram outage is
    # invisible except as these log lines. Counting them is the only way the owner
    # learns that alerts he never received were in fact sent and lost.
    alert_fails = sum(c for msg, c in list(errs.items()) + list(warns.items())
                      if 'alert' in msg.lower() or 'telegram' in msg.lower())
    return {'errors': errs.most_common(8), 'warnings': warns.most_common(8),
            'n_errors': sum(errs.values()), 'n_warnings': sum(warns.values()),
            'alert_failures': alert_fails}


# ---------------------------------------------------------------------------
# Flags — the part that earns the read
# ---------------------------------------------------------------------------

def _flags(d):
    """Facts that deserve a look. Each names WHAT happened, never why."""
    out = []
    cyc, tr, cf = d.get('cycles', {}), d.get('trades', {}), d.get('conformance', {})
    gd, tb_, pr = d.get('guardian', {}), d.get('turbulence', {}), d.get('problems', {})

    for g in (cyc.get('gaps') or []):
        out.append("MISSED CYCLES: no scan between %s and %s (%.1f min) — no exit "
                   "could fire in that window" % (g['from'], g['to'], g['minutes']))
    if cyc.get('incomplete'):
        out.append("%d cycle(s) started but never logged COMPLETE"
                   % cyc['incomplete'])

    for o in (cf.get('bad') or []):
        out.append("EXIT PAST ITS LEVEL: %s %s named %s=%.2f, filled %.2f (%+.1f%%, "
                   "%+.0f Rs vs the level)" % (o['key'], o['exit_type'],
                                               o['level_desc'], _f(o['level']),
                                               _f(o['fill']), _f(o['pct']),
                                               _f(o['rs'])))
    if cf.get('torn'):
        out.append("%d torn line(s) in the observer record" % cf['torn'])
    if cf.get('thin'):
        out.append("%d trade(s) recorded fewer than 2 ticks — too thin to judge a "
                   "late fire" % len(cf['thin']))
    if tr.get('n') and cf.get('exits') is not None and cf['exits'] < tr['n']:
        out.append("LEDGER/OBSERVER MISMATCH: %d closed trade(s) but %d observer exit "
                   "record(s)" % (tr['n'], cf['exits']))

    for m in (gd.get('orphans') or []):
        out.append("ORPHAN: %s" % m)
    for m in (gd.get('disputed') or []):
        out.append("DISPUTED REJECTION: %s" % m)
    for m in (gd.get('broker_conflict') or []):
        out.append("BROKER CONFLICT: %s" % m)
    if gd.get('unreachable'):
        out.append("broker unreachable on %d occasion(s)" % gd['unreachable'])

    if tb_.get('observed') and tb_.get('worst') and tb_['worst'] != 'CALM':
        out.append("TURBULENCE %s: %s (%s)" % (
            tb_['worst'], '; '.join(tb_.get('reasons') or []) or 'no reason recorded',
            'entries blocked' if tb_.get('blocked') else 'recorded only'))

    if tr.get('open'):
        out.append("%d position(s) still OPEN at digest time: %s"
                   % (len(tr['open']), ', '.join(t['key'] for t in tr['open'])))
    if pr.get('alert_failures'):
        out.append("%d Telegram/alert failure(s) — alerts may have been lost"
                   % pr['alert_failures'])
    if pr.get('n_errors'):
        out.append("%d ERROR line(s) in the day log" % pr['n_errors'])
    return out


# ---------------------------------------------------------------------------
# Build / render
# ---------------------------------------------------------------------------

def build(day=None):
    day = day or datetime.datetime.now(IST).strftime('%Y%m%d')
    rows = _read_log(day)
    d = {
        'day': day,
        'built_at': datetime.datetime.now(IST).isoformat(),
        'mode': _mode(),
        'cycles': _cycles(rows, day),
        'trades': _trades(day),
        'executor': _executor(day),
        'conformance': _conformance(day),
        'gates': _gates(rows),
        'guardian': _guardian(rows, day),
        'turbulence': _turbulence(day),
        'problems': _problems(rows),
        'sources': {
            'day_log': _mtime(_log_path(day)),
            'positions': _mtime(os.path.join(CURRENT_DIR, 'st_opt_positions%s.json' % SFX)),
            'trade_obs': _mtime(os.path.join(CURRENT_DIR, 'logs', 'trade_obs%s.jsonl' % SFX)),
            'turbulence': _mtime(os.path.join(CURRENT_DIR, 'logs', 'turbulence.jsonl')),
        },
    }
    d['flags'] = _flags(d)
    return d


def render(d):
    """Markdown, dense, built to be read rather than skimmed."""
    day, L = d['day'], []
    iso = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
    mode = d.get('mode') or {}
    tr = d.get('trades') or {}
    cyc = d.get('cycles') or {}
    cf = d.get('conformance') or {}
    tb_ = d.get('turbulence') or {}

    L.append('# Option Magnet — digest %s' % iso)
    L.append('')
    L.append('**MODE: %s** · Guardian %s' % (mode.get('mode', '?'),
                                             mode.get('guardian', '?')))
    L.append('')

    # --- Flags first. If a day has one thing worth reading, it is here.
    L.append('## Flags')
    if d.get('flags'):
        for f in d['flags']:
            L.append('- %s' % f)
    else:
        L.append('- None. Nothing today departed from what the rules describe.')
    L.append('')

    # --- Executor (live or paper-executor) beside the paper ledger, per key
    ex = d.get('executor') or {}
    if ex.get('mode') not in (None, 'off') and not ex.get('error'):
        L.append('## Executor (%s)' % ex['mode'].upper())
        if not ex.get('trades'):
            L.append('No executor trades today.')
        else:
            L.append('gross Rs %+.0f · net of est. charges Rs %+.0f · paper on the same keys Rs %+.0f'
                     % (ex.get('gross', 0), ex.get('net', 0), ex.get('paper_same_keys', 0)))
            L.append('')
            L.append('| key | state | exit | how | qty | entry (slip) | exit | P&L | net | paper entry/exit | paper P&L |')
            L.append('|---|---|---|---|---|---|---|---|---|---|---|')
            for r in ex['trades']:
                L.append('| %s | %s | %s | %s | %s | %s (%s) | %s | %s | %s | %s / %s | %s |' % (
                    r['key'], r['state'], r.get('exit_type') or '-', r.get('how') or '-',
                    r.get('qty') or '-', r.get('entry') or '-', r.get('entry_slip'),
                    r.get('exit') or '-', r.get('realised'), r.get('net'),
                    r.get('paper_entry'), r.get('paper_exit'), r.get('paper_pnl')))
        if ex.get('skips'):
            L.append('')
            L.append('Skipped intents: ' + '; '.join('%s (%s)' % (s_['key'], s_['why'])
                                                    for s_ in ex['skips']))
        L.append('')

    # --- Trades
    L.append('## Trades')
    if tr.get('error'):
        L.append('_ledger unreadable: %s_' % tr['error'])
    elif not tr.get('closed') and not tr.get('open'):
        L.append('No positions opened today.')
    else:
        pf = tr.get('pf')
        L.append('%d closed · gross **Rs %+.0f** · WR %s · PF %s'
                 % (tr.get('n', 0), tr.get('gross', 0),
                    ('%.0f%%' % tr['wr']) if tr.get('wr') is not None else '-',
                    ('%.2f' % pf) if isinstance(pf, float) and pf != float('inf')
                    else ('inf' if pf == float('inf') else '-')))
        # Non-NIFTY only: NIFTY never trades after 17:00 and its digest text stays unchanged.
        if SFX and tr.get('by_half'):
            L.append('By session half: ' + _half_text(tr['by_half']))
        L.append('')
        L.append('| time | pos | tf | entry | exit | layer | P&L |')
        L.append('|---|---|---|---:|---:|---|---:|')
        for t in tr.get('closed', []):
            L.append('| %s | %s %s | %s | %.2f | %.2f | %s | %+.0f |' % (
                t.get('entry_time', '-'), t.get('opt_type'), t.get('strike'),
                t.get('tf', '').replace('minute', 'M'), t.get('entry', 0),
                t.get('exit', 0), t.get('exit_type'), t.get('pnl', 0)))
        for t in tr.get('open', []):
            L.append('| %s | %s %s | %s | %.2f | — | **OPEN** | — |' % (
                t.get('entry_time', '-'), t.get('opt_type'), t.get('strike'),
                t.get('tf', '').replace('minute', 'M'), t.get('entry', 0)))
    L.append('')

    # --- Conformance
    L.append('## Did the exits do what the rules say?')
    if cf.get('error'):
        L.append('_observer record unreadable: %s_' % cf['error'])
    elif not cf.get('exits'):
        L.append('No observer exit records for this day.')
        if tr.get('n'):
            L.append('')
            L.append('_%d trade(s) closed with no flight recording — the observer was '
                     'not running for them._' % tr['n'])
    else:
        L.append('%d plan(s), %d tick(s), %d exit(s) recorded.'
                 % (cf.get('plans', 0), cf.get('ticks', 0), cf.get('exits', 0)))
        L.append('')
        L.append('| trade | layer | named level | filled | vs level |')
        L.append('|---|---|---:|---:|---:|')
        for o in cf.get('overshoots', []):
            L.append('| %s | %s | %s %.2f | %.2f | %+.2f%% (%+.0f Rs) |' % (
                o['key'], o['exit_type'], o['level_desc'], _f(o['level']),
                _f(o['fill']), _f(o['pct']), _f(o['rs'])))
        L.append('')
        L.append('Net vs the levels the rules named: **Rs %+.0f**'
                 % cf.get('net_vs_level', 0))
        L.append('')
        L.append('_A stop always fills at or below its level and a TP at or above it — '
                 'the scan is once a minute, so the fill lands wherever price was when '
                 'the tick ran. The number to watch is an OUTLIER, not the total._')
    L.append('')

    # --- Gates
    L.append('## What was suppressed')
    g = d.get('gates') or {}
    if not g or g.get('error'):
        L.append('Nothing was gated today.')
    else:
        for name, info in sorted(g.items()):
            L.append('- **%s** × %d' % (name, info['count']))
            for ex in info['examples']:
                L.append('  - `%s`' % ex[:150])
    L.append('')

    # --- Session health
    L.append('## Session')
    if cyc.get('error'):
        L.append('_log unreadable: %s_' % cyc['error'])
    else:
        L.append('%d cycles (%d completed, %d exited early off-hours) · %s → %s · '
                 '%d scan(s)'
                 % (cyc.get('cycles', 0), cyc.get('completed', 0),
                    cyc.get('early_exit', 0),
                    cyc.get('first') or '-', cyc.get('last') or '-',
                    cyc.get('scans', 0)))
        if cyc.get('gaps'):
            L.append('')
            for gp in cyc['gaps']:
                L.append('- gap %s → %s (%.1f min)' % (gp['from'], gp['to'],
                                                       gp['minutes']))
    if tb_.get('observed'):
        L.append('')
        L.append('Turbulence: **%s**%s' % (
            tb_.get('worst'),
            (' — ' + '; '.join(tb_.get('reasons') or [])) if tb_.get('reasons') else ''))
        if tb_.get('signals_ok') == 0:
            L.append('')
            L.append('_The tripwire saw NO usable inputs today — this is "blind", not '
                     '"calm"._')
    L.append('')

    # --- Problems
    pr = d.get('problems') or {}
    if pr.get('n_errors') or pr.get('n_warnings'):
        L.append('## Errors and warnings')
        for m, c in pr.get('errors', []):
            L.append('- ERROR ×%d `%s`' % (c, m))
        for m, c in pr.get('warnings', []):
            L.append('- WARN ×%d `%s`' % (c, m))
        L.append('')

    # --- Provenance. Never quote a number without saying where it came from.
    L.append('---')
    L.append('')
    L.append('_Sources (last modified): %s_' % ' · '.join(
        '%s %s' % (k, v or 'MISSING') for k, v in sorted(
            (d.get('sources') or {}).items())))
    L.append('')
    L.append('_Built %s. Facts only — no interpretation. On the desktop copy the '
             'ledger can lag the Pi; check the trade count before quoting this in a '
             'review._' % (d.get('built_at') or '')[:19])
    return '\n'.join(L)


def summary_line(d):
    """The compact form for Telegram: what happened, and whether to open the file."""
    tr, mode = d.get('trades') or {}, d.get('mode') or {}
    n, gross = tr.get('n', 0), tr.get('gross', 0)
    flags = d.get('flags') or []
    head = ('<b>📋 Digest %s%s</b> · %s\n%d trade(s), Rs %+.0f'
            % ((UNDERLYING + ' ') if SFX else '', d['day'], mode.get('mode', '?'), n, gross))
    ex = d.get('executor') or {}
    if ex.get('trades'):
        head += ('\nExecutor (%s): %d trade(s), gross Rs %+.0f, net Rs %+.0f (paper same keys Rs %+.0f)'
                 % (ex.get('mode'), len([r for r in ex['trades'] if r['state'] == 'CLOSED']),
                    ex.get('gross', 0), ex.get('net', 0), ex.get('paper_same_keys', 0)))
    if SFX and tr.get('by_half'):   # the MCX session rule is decided on this split (2026-10-27)
        head += '\n' + _half_text(tr['by_half'])
    if not flags:
        return head + '\nNo flags — nothing departed from the rules.'
    body = '\n'.join('• %s' % f[:160] for f in flags[:6])
    more = ('\n…and %d more' % (len(flags) - 6)) if len(flags) > 6 else ''
    return head + '\n<b>%d flag(s):</b>\n%s%s' % (len(flags), body, more)


def write(day=None, send=False):
    d = build(day)
    md = render(d)
    out_dir = os.path.join(CURRENT_DIR, 'logs')
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, 'digest%s_%s.md' % (SFX, d['day']))
    js_path = os.path.join(out_dir, 'digest%s_%s.json' % (SFX, d['day']))
    with open(md_path, 'w', encoding='utf-8') as fh:
        fh.write(md)
    with open(js_path, 'w', encoding='utf-8') as fh:
        json.dump(d, fh, indent=1, default=str)
    if send:
        try:
            import common
            common.sendMsgTelegram(summary_line(d), 'I')
        except Exception as e:
            print('Telegram send failed: %s' % e, file=sys.stderr)
    return md_path, d


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--day', help='YYYYMMDD (default: today)')
    ap.add_argument('--underlying', default='NIFTY',
                    help='which bot process to digest (default NIFTY = original file names)')
    ap.add_argument('--send', action='store_true', help='push a summary to Telegram')
    ap.add_argument('--print', dest='show', action='store_true',
                    help='print the markdown instead of only writing it')
    args = ap.parse_args()
    global UNDERLYING, SFX
    UNDERLYING = args.underlying.strip().upper()
    SFX = '' if UNDERLYING == 'NIFTY' else '_' + UNDERLYING
    path, d = write(args.day, send=args.send)
    if args.show:
        # The digest FILE is always UTF-8. Printing it is a different problem: a
        # Windows console is cp1252 and raises on the arrows and box characters, which
        # would make `--print` look like a digest bug rather than a terminal limit.
        text = render(d)
        enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
        sys.stdout.write(text.encode(enc, errors='replace').decode(enc) + '\n')
    else:
        print('digest written: %s' % path)
        print('flags: %d' % len(d.get('flags') or []))
    return 0


if __name__ == '__main__':
    sys.exit(main())
