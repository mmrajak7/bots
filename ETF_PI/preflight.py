#!/usr/bin/env python3
"""Is the desk ready to trade? Run at 08:55, report to Telegram.

WHAT THIS IS FOR
----------------
"Claude should verify if Neo is ready to trade by 9am and report in Telegram." This is
the deterministic half of that, and it is deliberately the whole thing for now: every
check below is a file stat, a config read or one API probe. None of it needs judgement,
so none of it needs an agent — and because it needs no agent it still runs on a morning
the CLI is down.

THE FAILURE IT EXISTS TO PREVENT
--------------------------------
Believing the desk is ready when it is not. A stale Kite token, an instrument dump that
did not refresh, a position left open from yesterday, or a half-flipped `trade_enabled`
/ `log_only` pair are all silent at 09:15 and expensive by 09:30. Each is one stat call
to check and none of them were being checked.

CHECK SEVERITY
--------------
  FAIL  — the desk is not ready; trading today will misbehave.
  WARN  — degraded; trading will work but something needs attention.
  INFO  — stated for the record, never a reason to hold the desk.

The verdict is NOT READY if anything FAILs. Warnings never block, because a preflight
that cries wolf is a preflight nobody reads.

    python preflight.py            # check and print
    python preflight.py --send     # also push the report to Telegram
"""
import argparse
import datetime
import json
import os
import shutil
import sys

import pytz

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, CURRENT_DIR)

IST = pytz.timezone('Asia/Kolkata')

FAIL, WARN, INFO, OK = 'FAIL', 'WARN', 'INFO', 'OK'

#: Below this the Pi is close enough to full that a state write can tear.
MIN_FREE_MB = 200


class Report(object):
    def __init__(self):
        self.rows = []

    def add(self, severity, name, detail):
        self.rows.append({'severity': severity, 'name': name, 'detail': detail})

    @property
    def failures(self):
        return [r for r in self.rows if r['severity'] == FAIL]

    @property
    def warnings(self):
        return [r for r in self.rows if r['severity'] == WARN]

    @property
    def ready(self):
        return not self.failures


def _age_hours(path, now):
    try:
        return (now - datetime.datetime.fromtimestamp(
            os.path.getmtime(path), IST)).total_seconds() / 3600.0
    except OSError:
        return None


def _today(now):
    return now.strftime('%Y%m%d')


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_mode(rep):
    """E5: state the trade_enabled/log_only pair in words, every morning."""
    try:
        import yaml
        with open(os.path.join(CURRENT_DIR, 'config_trade.yaml')) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as e:
        rep.add(FAIL, 'config', 'config_trade.yaml unreadable: %s' % e)
        return None
    enabled = bool(cfg.get('trade_enabled', False))
    log_only = bool(cfg.get('log_only', True))
    if not enabled:
        rep.add(INFO, 'mode', 'PAPER — trade_enabled: false, no orders will be placed')
    elif log_only:
        rep.add(INFO, 'mode', 'PAPER — log_only: true, orders logged not placed')
    else:
        rep.add(INFO, 'mode', 'LIVE — REAL ORDERS WILL BE PLACED')
    rep.add(INFO, 'guardian',
            'ARMED (broker-truth interlocks live)' if (enabled and not log_only)
            else 'DORMANT by design — arms when log_only goes false')
    for key, floor in (('max_loss_per_day', 1), ('num_lots', 1)):
        v = cfg.get(key)
        if v is None:
            rep.add(WARN, 'config', '%s missing from config_trade.yaml' % key)
        elif isinstance(v, (int, float)) and v < floor:
            rep.add(WARN, 'config', '%s is %s' % (key, v))
    return cfg


def check_kite(rep, now, probe=True):
    """The token kite_login refreshes each morning (08:40), and whether it actually works."""
    import platform
    path = (r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
            if platform.system() == 'Windows'
            else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')
    if not os.path.exists(path):
        rep.add(FAIL, 'kite token', 'missing at %s' % path)
        return
    age = _age_hours(path, now)
    stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path), IST)
    if stamp.date() != now.date():
        rep.add(FAIL, 'kite token',
                'last refreshed %s (%.1fh ago) — kite_login has not run today'
                % (stamp.strftime('%Y-%m-%d %H:%M'), age or -1))
    else:
        rep.add(OK, 'kite token', 'refreshed %s' % stamp.strftime('%H:%M'))
    if not probe:
        return
    # A fresh FILE is not a working token. One LTP call settles it, and this is the
    # single check most worth its API cost: everything downstream assumes it.
    try:
        import option_buy as ob
        k = ob.get_kite()
        r = k.ltp(['NSE:NIFTY 50'])
        px = (r.get('NSE:NIFTY 50') or {}).get('last_price')
        if px:
            rep.add(OK, 'kite probe', 'NIFTY %.2f' % px)
        else:
            rep.add(FAIL, 'kite probe', 'LTP call returned no price')
    except Exception as e:
        rep.add(FAIL, 'kite probe', '%s: %s' % (type(e).__name__, str(e)[:100]))


def check_instruments(rep, now):
    """The dumps cron writes at 08:50/08:52. A stale dump resolves stale strikes."""
    for name in ('instruments.csv', 'op_instruments.csv'):
        path = os.path.join(CURRENT_DIR, name)
        if not os.path.exists(path):
            rep.add(FAIL, name, 'missing')
            continue
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path), IST)
        size_mb = os.path.getsize(path) / 1e6
        if stamp.date() != now.date():
            rep.add(FAIL, name, 'dated %s, not today — the dump did not refresh'
                    % stamp.strftime('%Y-%m-%d %H:%M'))
        elif size_mb < 0.1:
            rep.add(FAIL, name, 'suspiciously small (%.2f MB)' % size_mb)
        else:
            rep.add(OK, name, '%s, %.1f MB' % (stamp.strftime('%H:%M'), size_mb))


def check_neo(rep, now, cfg):
    """Only meaningful once the guardian is armed; stated as dormant otherwise."""
    enabled = bool((cfg or {}).get('trade_enabled', False))
    log_only = bool((cfg or {}).get('log_only', True))
    session = os.path.join(CURRENT_DIR, 'neo', 'data', 'session.json')
    if not (enabled and not log_only):
        rep.add(INFO, 'neo session',
                'not checked — paper mode, no Neo session required')
        return
    if not os.path.exists(session):
        rep.add(FAIL, 'neo session', 'missing — neo_login has not run today')
        return
    stamp = datetime.datetime.fromtimestamp(os.path.getmtime(session), IST)
    if stamp.date() != now.date():
        rep.add(FAIL, 'neo session', 'dated %s — neo_login has not run today'
                % stamp.strftime('%Y-%m-%d %H:%M'))
    else:
        rep.add(OK, 'neo session', 'refreshed %s' % stamp.strftime('%H:%M'))


def check_positions(rep, now):
    """Anything still open from a previous day is an unmonitored carry."""
    check_positions_other(rep, now)
    path = os.path.join(CURRENT_DIR, 'st_opt_positions.json')
    try:
        with open(path) as fh:
            positions = json.load(fh)
    except Exception as e:
        rep.add(FAIL, 'positions', 'ledger unreadable: %s' % e)
        return
    today = _today(now)
    stale = [k for k, p in positions.items()
             if p.get('status') == 'open' and p.get('entry_date') != today]
    if stale:
        rep.add(FAIL, 'positions',
                '%d position(s) still OPEN from a previous day: %s'
                % (len(stale), ', '.join(stale[:4])))
    else:
        rep.add(OK, 'positions', 'flat — nothing carried from a previous day')


def check_positions_other(rep, now):
    """Non-NIFTY ledgers + shadow stores (review-4 R4-12): carries are FAIL, leftover shadows WARN."""
    try:
        import underlyings
    except Exception:
        return
    today = _today(now)
    for name, u in underlyings.UNDERLYINGS.items():
        if not u.file_suffix:
            continue
        p = os.path.join(CURRENT_DIR, 'st_opt_positions%s.json' % u.file_suffix)
        if os.path.exists(p):
            try:
                with open(p) as fh:
                    pos = json.load(fh)
                stale = [k for k, v in pos.items() if isinstance(v, dict) and v.get('status') == 'open'
                         and v.get('entry_date') != today]
                if stale:
                    rep.add(FAIL, 'positions %s' % name, '%d OPEN from a previous day: %s'
                            % (len(stale), ', '.join(stale[:3])))
            except Exception as e:
                rep.add(FAIL, 'positions %s' % name, 'ledger unreadable: %s' % e)
        sp = os.path.join(CURRENT_DIR, 'shadow_positions%s.json' % u.file_suffix)
        if os.path.exists(sp):
            try:
                with open(sp) as fh:
                    n = len(json.load(fh))
                if n:
                    rep.add(WARN, 'shadows %s' % name, '%d shadow(s) still open before the session '
                            '(EOD close missed; they close out as STALE at the first scan)' % n)
            except Exception as e:
                rep.add(WARN, 'shadows %s' % name, 'shadow store unreadable: %s' % e)


def check_digest(rep, now):
    """Did yesterday's digest get written? Its absence is how a dead layer hides."""
    for back in range(1, 6):
        d = (now - datetime.timedelta(days=back))
        if d.weekday() >= 5:
            continue
        day = d.strftime('%Y%m%d')
        path = os.path.join(CURRENT_DIR, 'logs', 'digest_%s.md' % day)
        if os.path.exists(path):
            rep.add(OK, 'last digest', '%s written' % day)
        else:
            rep.add(WARN, 'last digest',
                    'no digest for %s — the EOD report did not run' % day)
        return
    rep.add(INFO, 'last digest', 'no prior weekday to check')


def check_disk(rep):
    try:
        free_mb = shutil.disk_usage(CURRENT_DIR).free / 1e6
    except Exception as e:
        rep.add(WARN, 'disk', 'could not check: %s' % e)
        return
    if free_mb < MIN_FREE_MB:
        rep.add(FAIL, 'disk', '%.0f MB free — a state write can tear below %d MB'
                % (free_mb, MIN_FREE_MB))
    else:
        rep.add(OK, 'disk', '%.0f MB free' % free_mb)


def check_writable(rep):
    """logs/ must be writable or the observer, tripwire and digest all vanish."""
    logs = os.path.join(CURRENT_DIR, 'logs')
    try:
        os.makedirs(logs, exist_ok=True)
        probe = os.path.join(logs, '.preflight_probe')
        with open(probe, 'w') as fh:
            fh.write('ok')
        os.remove(probe)
        rep.add(OK, 'logs writable', logs)
    except Exception as e:
        rep.add(FAIL, 'logs writable', '%s: %s' % (type(e).__name__, e))


def check_clock(rep, now):
    """A Pi has no RTC. A clock behind reality makes every dedup and latch wrong."""
    future = []
    for name in ('st_opt_positions.json', 'st_opt_state.json'):
        path = os.path.join(CURRENT_DIR, name)
        if not os.path.exists(path):
            continue
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path), IST)
        if stamp > now + datetime.timedelta(minutes=5):
            future.append('%s (%s)' % (name, stamp.strftime('%Y-%m-%d %H:%M')))
    if future:
        rep.add(FAIL, 'clock', 'file timestamps are in the FUTURE: %s — the clock '
                               'has stepped backwards' % ', '.join(future))
    else:
        rep.add(OK, 'clock', now.strftime('%Y-%m-%d %H:%M:%S %Z'))


def check_instrumentation(rep, now):
    """Is the flight recorder actually recording? Silence here is the classic failure."""
    obs = os.path.join(CURRENT_DIR, 'logs', 'trade_obs.jsonl')
    if not os.path.exists(obs):
        rep.add(WARN, 'observer', 'logs/trade_obs.jsonl does not exist yet — no trade '
                                  'has been recorded since it was installed')
        return
    size = os.path.getsize(obs)
    stamp = datetime.datetime.fromtimestamp(os.path.getmtime(obs), IST)
    if size == 0:
        rep.add(WARN, 'observer', 'record file is EMPTY — no trade has been observed')
    else:
        rep.add(OK, 'observer', '%d bytes, last write %s'
                % (size, stamp.strftime('%Y-%m-%d %H:%M')))




# ---------------------------------------------------------------------------
# Multi-instrument checks (2026-10-06, spec SPEC_multi_instrument_2026-10-06 §9/§12)
# ---------------------------------------------------------------------------

#: Signal chains per underlying: NIFTY missing is fatal; a missing paper instrument only
#: stops that instrument (its process logs "Resolve failed"), so it is a warning.
REQUIRED_CHAINS = {'NIFTY': FAIL, 'BANKNIFTY': WARN, 'CRUDEOILM': WARN, 'NATGASMINI': WARN,
                   'GOLDM': WARN, 'SILVERM': WARN}


def _enabled(name):
    """underlyings.py `enabled` for name; unknown/unimportable -> True so the check still runs."""
    try:
        import underlyings
        return underlyings.get(name).enabled
    except Exception:
        return True
#: The Pi has no RTC; event windows are absolute times, so drift is a real risk (review-1 M4).
MAX_CLOCK_DRIFT_S = 60


def check_manifest(rep):
    """Every deployed file matches MANIFEST.sha256 (deploys were verified by size only before)."""
    import hashlib
    path = os.path.join(CURRENT_DIR, 'MANIFEST.sha256')
    if not os.path.exists(path):
        rep.add(INFO, 'manifest', 'no MANIFEST.sha256 — deploy not hash-verified')
        return
    bad, n = [], 0
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            want, name = parts[0], parts[1].lstrip('*').strip()
            n += 1
            fp = os.path.join(CURRENT_DIR, name)
            if not os.path.exists(fp):
                bad.append('%s missing' % name)
                continue
            with open(fp, 'rb') as f:
                if hashlib.sha256(f.read()).hexdigest() != want:
                    bad.append(name)
    if bad:
        rep.add(FAIL, 'manifest', 'differs from MANIFEST.sha256: %s' % ', '.join(bad))
    else:
        rep.add(OK, 'manifest', '%d file(s) match' % n)


def check_clock_drift(rep, now, probe=True):
    """Local clock vs the Kite API server's HTTP Date header."""
    if not probe:
        rep.add(INFO, 'clock drift', 'not probed')
        return
    try:
        import email.utils
        import requests
        r = requests.head('https://api.kite.trade', timeout=5)
        server = email.utils.parsedate_to_datetime(r.headers['Date'])
        drift = (datetime.datetime.now(datetime.timezone.utc) - server).total_seconds()
    except Exception as e:
        rep.add(WARN, 'clock drift', 'could not compare with Kite server time: %s' % e)
        return
    if abs(drift) > MAX_CLOCK_DRIFT_S:
        rep.add(FAIL, 'clock drift', 'local clock is %+.0f s off Kite server time — event '
                                     'windows and EOD are absolute times' % drift)
    else:
        rep.add(OK, 'clock drift', '%+.1f s vs Kite server' % drift)


def check_chains(rep, now):
    """Each underlying's option chain (+ MCX futures for the ATM reference) is in today's dump."""
    try:
        import pandas as pd
        df = pd.read_csv(os.path.join(CURRENT_DIR, 'op_instruments.csv'))
        df['expiry'] = pd.to_datetime(df['expiry'])
        live = df[df['expiry'].dt.date >= now.date()]
        live = live[['name', 'instrument_type']]
    except Exception as e:   # a malformed dump must be a FAIL row, never a preflight crash
        rep.add(FAIL, 'chains', 'op_instruments.csv unreadable / malformed: %r' % (e,))
        return
    missing = {}
    checked = [n for n in REQUIRED_CHAINS if _enabled(n)]   # 2026-11-05: disabled = not traded
    for name in checked:
        sev = REQUIRED_CHAINS[name]
        opts = live[(live['name'] == name) & live['instrument_type'].isin(['CE', 'PE'])]
        need_fut = name not in ('NIFTY', 'BANKNIFTY')
        futs = live[(live['name'] == name) & (live['instrument_type'] == 'FUT')]
        if opts.empty or (need_fut and futs.empty):
            missing.setdefault(sev, []).append(name)
    for sev in (FAIL, WARN):
        if missing.get(sev):
            rep.add(sev, 'chains', 'missing option chain / futures: %s' % ', '.join(missing[sev]))
    if not missing:
        rep.add(OK, 'chains', 'all %d enabled underlyings resolvable' % len(checked))


def check_calendars(rep, now):
    """Holiday YAML coverage (missing year / December reminder) and event-calendar health."""
    try:
        import market_calendar
        cal = market_calendar.load(CURRENT_DIR)
        msg = market_calendar.coverage_problem(now.date(), cal)
        if msg and msg.startswith('MISSING'):
            rep.add(WARN, 'holidays', msg)
        elif msg:
            rep.add(INFO, 'holidays', msg)
        else:
            rep.add(OK, 'holidays', 'market_holidays.yaml covers %d' % now.year)
    except Exception as e:
        rep.add(WARN, 'holidays', 'market_holidays.yaml unusable (%s) — bots assume normal '
                                  'sessions; the liquidity freshness gate is the backstop' % e)
    event_us = [n for n in ('CRUDEOILM', 'NATGASMINI') if _enabled(n)]
    if not event_us:   # 2026-11-05: only NIFTY trades; the EIA calendar gates nothing
        rep.add(INFO, 'event calendar', 'not used — no enabled underlying has event windows')
        return
    try:
        import importlib   # EIA schedule parser deps: missing -> UNVERIFIED -> fail-closed every event day
        for mod in ('bs4', 'requests'):
            importlib.import_module(mod)
    except ImportError as e:
        rep.add(FAIL, 'event deps', '%s — event verification cannot run; CRUDEOILM/NATGASMINI would '
                                    'be fail-closed every Wed/Thu evening' % e)
    try:
        import event_calendar
        for name in event_us:
            w = event_calendar.windows_for(name, now.date(), CURRENT_DIR)
            if any(x.get('kind') == 'fail_closed' for x in w):
                rep.add(WARN, 'events %s' % name, 'FAIL-CLOSED today (19:00->close blocked): event '
                                                  'not verified — run `event_calendar.py build`')
            elif w:
                rep.add(INFO, 'events %s' % name, '; '.join(
                    '%s %s' % (x.get('kind'), x.get('T').strftime('%H:%M') if x.get('T') else '19:00-close')
                    for x in w))
        rep.add(OK, 'event calendar', 'readable')
    except Exception as e:
        rep.add(WARN, 'event calendar', 'unusable (%s) — CRUDEOILM/NATGASMINI run FAIL-CLOSED '
                                        'Wed/Thu/Fri evenings' % e)


# ---------------------------------------------------------------------------
# Run / render
# ---------------------------------------------------------------------------

def run(now=None, probe=True):
    now = now or datetime.datetime.now(IST)
    rep = Report()
    cfg = check_mode(rep)
    check_clock(rep, now)
    check_kite(rep, now, probe=probe)
    check_instruments(rep, now)
    check_neo(rep, now, cfg)
    check_positions(rep, now)
    check_disk(rep)
    check_writable(rep)
    check_instrumentation(rep, now)
    check_digest(rep, now)
    check_manifest(rep)
    check_clock_drift(rep, now, probe=probe)
    check_chains(rep, now)
    check_calendars(rep, now)
    return rep


def render(rep, now=None):
    now = now or datetime.datetime.now(IST)
    verdict = 'READY' if rep.ready else 'NOT READY'
    L = ['Preflight %s — %s' % (now.strftime('%Y-%m-%d %H:%M'), verdict), '']
    for r in rep.rows:
        mark = {OK: '[ok]  ', FAIL: '[FAIL]', WARN: '[warn]', INFO: '[info]'}[
            r['severity']]
        L.append('%s %-16s %s' % (mark, r['name'], r['detail']))
    if rep.failures:
        L += ['', 'NOT READY — %d blocking issue(s).' % len(rep.failures)]
    return '\n'.join(L)


def render_telegram(rep, now=None):
    now = now or datetime.datetime.now(IST)
    head = ('<b>%s Preflight %s</b>'
            % ('✅' if rep.ready else '🚨', now.strftime('%Y-%m-%d')))
    mode = next((r['detail'] for r in rep.rows if r['name'] == 'mode'), '?')
    guard = next((r['detail'] for r in rep.rows if r['name'] == 'guardian'), '?')
    L = [head, '<b>MODE:</b> %s' % mode, 'Guardian: %s' % guard]
    if rep.failures:
        L.append('')
        L.append('<b>NOT READY — %d blocking:</b>' % len(rep.failures))
        L += ['• %s: %s' % (r['name'], r['detail']) for r in rep.failures]
    else:
        L.append('')
        L.append('All checks passed — desk is ready.')
    if rep.warnings:
        L.append('')
        L.append('<i>%d warning(s):</i>' % len(rep.warnings))
        L += ['• %s: %s' % (r['name'], r['detail']) for r in rep.warnings]
    return '\n'.join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--send', action='store_true', help='push the report to Telegram')
    ap.add_argument('--no-probe', action='store_true',
                    help='skip the live Kite LTP call')
    args = ap.parse_args()
    now = datetime.datetime.now(IST)
    rep = run(now, probe=not args.no_probe)
    text = render(rep, now)
    enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    sys.stdout.write(text.encode(enc, errors='replace').decode(enc) + '\n')
    if args.send:
        try:
            import common
            common.sendMsgTelegram(render_telegram(rep, now), 'I')
        except Exception as e:
            print('Telegram send failed: %s' % e, file=sys.stderr)
    # Non-zero exit on NOT READY so cron/a wrapper can react without parsing text.
    return 0 if rep.ready else 1


if __name__ == '__main__':
    sys.exit(main())
