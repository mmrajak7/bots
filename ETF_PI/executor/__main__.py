"""ETF_PI live executor -- CLI (docs/EXECUTOR_SPEC.md; deploy: docs/EXECUTOR_RUNBOOK.md).

Run from the ETF_PI directory with the SAME interpreter as option_buy (Pi: /usr/bin/python3):
  python3 -m executor run        the daemon (systemd etf-executor.service)
  python3 -m executor watchdog   cron, every minute: stale heartbeat -> restart + Telegram (H2)
  python3 -m executor status     print state/executor/status.json
  python3 -m executor kill       no NEW entries (file KILL); open trades keep their exits
  python3 -m executor flatten    exit everything now + no new entries (file FLATTEN)
  python3 -m executor resume     remove KILL and FLATTEN
  python3 -m executor stop       STOPPED flag + systemctl stop (the watchdog leaves it stopped)
  python3 -m executor start      remove STOPPED + systemctl start
  python3 -m executor check      load config + option_buy + kotak_neo, print the plan, exit
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import logging
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
ETF_DIR = os.path.dirname(HERE)
if ETF_DIR not in sys.path:
    sys.path.insert(0, ETF_DIR)

from executor import config as C          # noqa: E402

log = logging.getLogger('executor')
LADDER_CONSTS = ('PREMIUM_SL_PCT', 'TRAIL_PCT', 'TRAIL_MIN_GAIN_PCT', 'T1_BUFFER_PCT',
                 'ADV_SL_PEAK_PCT', 'ADV_SL_DRIFT_PCT', 'COST_SL_GAP_PCT', 'MIN_T1_DISTANCE',
                 'MIN_REWARD_PCT', 'T1_GIVEBACK_KEEP_PCT')


def _logging(name: str) -> None:
    d = os.path.join(ETF_DIR, 'logs')
    os.makedirs(d, exist_ok=True)
    day = _dt.date.today().strftime('%Y-%m-%d')
    fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(name)s | %(message)s', '%H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(os.path.join(d, '%s_%s.log' % (name, day)), encoding='utf-8')
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def import_option_buy():
    """option_buy for `_ladder` + constants (F20/F21). Its import opens the PAPER
    log; detach that so the daemon never writes into it."""
    os.environ['OB_UNDERLYING'] = 'NIFTY'
    import option_buy as ob
    lg = logging.getLogger('option_buy')
    for h in list(lg.handlers):
        lg.removeHandler(h)
    lg.propagate = True
    if ob.U.name != 'NIFTY':
        raise SystemExit('option_buy imported for %s, not NIFTY' % ob.U.name)
    ob._init_bridge()                         # loads TF_TIME_SL only (D17: never a bridge)
    if not ob.TF_TIME_SL or '10minute' not in ob.TF_TIME_SL:
        raise SystemExit('config_trade.yaml time_sl has no 10minute cap: %r' % ob.TF_TIME_SL)
    return ob


def consts_digest(ob) -> str:
    vals = {k: getattr(ob, k) for k in LADDER_CONSTS}
    vals['TF_TIME_SL'] = ob.TF_TIME_SL
    blob = json.dumps(vals, sort_keys=True, default=str)
    return '%s %s' % (hashlib.sha256(blob.encode()).hexdigest()[:12], blob)


class RestQuoter:
    """Kite REST quote for the executor (review H4). option_buy.get_kite() calls
    sys.exit on a missing token and caches the client forever; this re-reads the
    token file after any failure and raises ordinary exceptions. Calls still go
    through option_buy's cross-process limiter (`_kite_call`)."""

    def __init__(self, ob):
        self.ob = ob
        self.kite = None

    def _client(self):
        if self.kite is None:
            from kiteconnect import KiteConnect
            with io.open(self.ob.TOKEN_PATH, encoding='utf-8') as f:
                td = json.load(f)
            k = KiteConnect(api_key=td['api_key'])
            k.set_access_token(td['access_token'])
            self.kite = k
        return self.kite

    def __call__(self, key):
        try:
            k = self._client()
            rec = self.ob._kite_call('quote', lambda: k.quote([key]))
            return (rec or {}).get(key)
        except Exception:
            self.kite = None                  # next call re-reads the token file
            raise


def journal_path(mode: str) -> str:
    """Review H1-b: LIVE and PAPER never share a book -- a paper restart must not
    reconcile a live lot against an empty fake exchange."""
    return os.path.join(C.STATE_DIR, 'journal_%s.jsonl' % ('live' if mode == 'live' else 'paper'))


def build(cfg):
    import market_calendar
    from executor.broker import Broker
    from executor.engine import Engine
    from executor.journal import Journal
    from executor.notify import DeadMan, Notifier
    from executor.prices import PriceFeed
    ob = import_option_buy()
    os.makedirs(C.STATE_DIR, exist_ok=True)

    def ticker(api_key, access):
        from kiteconnect import KiteTicker
        return KiteTicker(api_key, access)
    feed = PriceFeed(cfg, ob.TOKEN_PATH, C.STATE_DIR, RestQuoter(ob), ticker, async_rest=True)
    broker = Broker(cfg, paper=(cfg['mode'] != 'live'))
    cal = market_calendar.load()
    eng = Engine(cfg, broker, feed, Notifier(prefix='ETF %s' % cfg['mode'].upper(), async_send=True),
                 Journal(journal_path(cfg['mode'])), ob, C.STATE_DIR,
                 session_fn=lambda d: market_calendar.session(ob.U, d, cal),
                 eod_fn=lambda d: min(ob.U.eod_close(d), cfg['eod_time_t'] or ob.U.eod_close(d)),
                 deadman=DeadMan(DeadMan.url_from_creds(), cfg['deadman_every_s']))
    eng.consts_digest = consts_digest(ob)
    if cfg['mode'] != 'live':
        live_pos, _, _ = Journal(journal_path('live')).load()
        eng.unmanaged = [k for k, p in live_pos.items()
                         if p.get('state') in ('PENDING', 'ENTRY', 'OPEN', 'EXIT')]
    return eng, ob


def _config_or_alert(what: str):
    """Review L7: a bad config must reach Telegram, not only a log nobody reads."""
    try:
        return C.load()
    except Exception as e:
        log.error('%s: config refused: %s', what, e)
        try:
            import common
            common.sendMsgTelegram('<b>ETF EXECUTOR</b> %s: CONFIG REFUSED -- %s' % (what, e), 'E')
        except Exception:
            pass
        raise


def _single_instance():
    """Review M4: two daemons would read the same intents and buy twice."""
    os.makedirs(C.STATE_DIR, exist_ok=True)
    f = open(os.path.join(C.STATE_DIR, 'daemon.lock'), 'a+')
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        return f                              # Windows dev box: no flock
    except OSError:
        raise SystemExit('another executor daemon holds %s' % f.name)
    return f


def cmd_run(args) -> int:
    _logging('executor')
    lock = _single_instance()                 # noqa: F841 (held for the process lifetime)
    cfg = _config_or_alert('run')
    eng, ob = build(cfg)
    IST = ob.IST
    log.info('executor start: mode=%s lots=%s live_until=%s product=%s consts=%s',
             cfg['mode'], cfg['lots'], cfg['live_until'], cfg['product'], eng.consts_digest)
    stop = {'flag': False}

    def on_term(signum, frame):
        stop['flag'] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    eng.feed.start()
    while not stop['flag']:
        t0 = time.time()
        try:
            eng.step(_dt.datetime.now(IST))
        except (Exception, SystemExit) as e:           # review H4: even a library sys.exit
            log.exception('step failed')
            eng.n.throttled('step_bug', 60, 'executor step BUG %s: %s' % (type(e).__name__, e), 'E')
        time.sleep(max(0.05, cfg['loop_s'] - (time.time() - t0)))
    log.info('executor stop (signal)')
    eng.feed.stop()
    return 0


def _status():
    p = os.path.join(C.STATE_DIR, 'status.json')
    try:
        with io.open(p, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def cmd_status(args) -> int:
    st = _status()
    print(json.dumps(st, indent=1) if st else 'no status at %s' % C.STATE_DIR)
    return 0 if st else 1


def cmd_watchdog(args) -> int:
    """H2: market hours of a trading day only; a stale heartbeat -> restart + Telegram."""
    _logging('executor_watchdog')
    import market_calendar
    import underlyings
    cfg = _config_or_alert('watchdog')
    if cfg['mode'] == 'off' or os.path.exists(os.path.join(C.STATE_DIR, 'STOPPED')):
        return 0                              # review M4: an owner stop is respected
    import pytz
    now = _dt.datetime.now(pytz.timezone('Asia/Kolkata'))
    sess = market_calendar.session(underlyings.get('NIFTY'), now.date(), market_calendar.load())
    if sess is None or not (_dt.time(9, 0) <= now.time() <= _dt.time(15, 59)):
        return 0
    st = _status()
    age = time.time() - float(st.get('epoch') or 0) if st else float('inf')
    if age <= cfg['watchdog_stale_s']:
        return 0
    msg = 'executor heartbeat %s -- restarting (%s)' % (
        'missing' if st is None else '%.0f s old' % age, cfg['watchdog_restart_cmd'])
    log.error(msg)
    try:
        r = subprocess.run(cfg['watchdog_restart_cmd'].split(), timeout=60,
                           capture_output=True, text=True)
        msg += ' -> rc %s %s' % (r.returncode, (r.stderr or '').strip()[:120])
    except Exception as e:
        msg += ' -> restart FAILED: %s' % e
    try:
        import common
        common.sendMsgTelegram('<b>ETF WATCHDOG</b> ' + msg, 'E')
    except Exception as e:
        log.error('telegram failed: %s', e)
    return 1


def _flag(name: str, on: bool) -> None:
    os.makedirs(C.STATE_DIR, exist_ok=True)
    p = os.path.join(C.STATE_DIR, name)
    if on:
        with io.open(p, 'w', encoding='utf-8') as f:
            f.write(_dt.datetime.now().isoformat())
    elif os.path.exists(p):
        os.remove(p)


def cmd_kill(args) -> int:
    _flag('KILL', True)
    print('KILL set: no new entries; open trades keep their exits')
    return 0


def cmd_flatten(args) -> int:
    _flag('FLATTEN', True)
    print('FLATTEN set: every open trade exits now; no new entries')
    return 0


def cmd_resume(args) -> int:
    _flag('KILL', False)
    _flag('FLATTEN', False)
    print('KILL and FLATTEN removed')
    return 0


def cmd_stop(args) -> int:
    _flag('STOPPED', True)
    r = subprocess.run(['sudo', 'systemctl', 'stop', 'etf-executor'])
    print('STOPPED set; systemctl stop rc=%s. Open lots are NOT managed while stopped.' % r.returncode)
    return r.returncode


def cmd_start(args) -> int:
    _flag('STOPPED', False)
    r = subprocess.run(['sudo', 'systemctl', 'start', 'etf-executor'])
    print('STOPPED removed; systemctl start rc=%s' % r.returncode)
    return r.returncode


def cmd_check(args) -> int:
    _logging('executor_check')
    cfg = C.load()
    ob = import_option_buy()
    from executor.broker import import_kotak_neo
    import_kotak_neo(cfg['bots_root'])
    print('mode=%s lots=%s (ceiling %s) live_until=%s product=%s bots_root=%s' % (
        cfg['mode'], cfg['lots'], cfg['lot_ceiling'], cfg['live_until'], cfg['product'],
        cfg['bots_root']))
    print('ladder consts: %s' % consts_digest(ob))
    paper_eod = ob.U.eod_close(_dt.date.today())
    print('EOD today: live %s (paper %s); last entry %s; state dir %s' % (
        min(paper_eod, cfg['eod_time_t'] or paper_eod), paper_eod, cfg['last_entry'], C.STATE_DIR))
    return 0


def cmd_probe(args) -> int:
    """Monday 11-30 test (runbook): a deliberate 1-lot intent for `--symbol` through the
    FULL live path (gates, map, entry, ladder, exit). T1 is set 9% above the ask so it
    never triggers by itself; the owner ends it with `flatten` (or TIME_SL at 30 min)."""
    cfg = C.load()
    if cfg['mode'] == 'live' and not args.confirm_real_order:
        print('mode=live: add --confirm-real-order (this BUYS %d lot)' % cfg['lots'])
        return 2
    ob = import_option_buy()
    sym = args.symbol
    df = ob.get_instruments()
    row = df[df['tradingsymbol'] == sym]
    if row.empty:
        print('%s not in op_instruments.csv' % sym)
        return 2
    r = row.iloc[0]
    if str(r['name']) != 'NIFTY':
        print('%s is not a NIFTY option' % sym)
        return 2
    info = ob._get_instrument_info(sym)
    q = ob._quote('NFO:%s' % sym)
    ask = float(q.get('ask') or 0)
    if ask <= 0:
        print('no ask for %s' % sym)
        return 2
    now = _dt.datetime.now(ob.IST)
    key = 'PROBE_%s_%s' % (sym, now.strftime('%Y%m%d_%H%M%S'))
    it = {'key': key, 'created_at': now.isoformat(), 'underlying': 'NIFTY', 'kite_symbol': sym,
          'kite_token': ob._token_for_symbol(sym), 'exchange': 'NFO',
          'strike': float(r['strike']), 'expiry': str(r['expiry'])[:10],
          'opt_type': str(r['instrument_type']), 'signal_tf': '10minute',
          'signal_ltp': float(q.get('ltp') or ask), 'st_value': round(ask * 1.09, 2),
          't2_value': 0, 't2_tf': '', 'lot_size': info['lot_size'], 'tick_size': info['tick_size'],
          'entry_bid': q.get('bid'), 'entry_ask': ask, 'probe': True}
    d = os.path.join(C.STATE_DIR, 'intents')
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, key + '.json.tmp')
    with io.open(tmp, 'w', encoding='utf-8') as f:
        json.dump(it, f)
    os.replace(tmp, os.path.join(d, key + '.json'))
    print('probe intent written: %s (ask %.2f, T1 %.2f). End it with: python3 -m executor flatten'
          % (key, ask, it['st_value']))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='python3 -m executor')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'watchdog', 'status', 'kill', 'flatten', 'resume', 'check', 'stop', 'start'):
        sub.add_parser(name)
    pp = sub.add_parser('probe')
    pp.add_argument('--symbol', required=True, help='Kite tradingsymbol, e.g. NIFTY2612026000CE')
    pp.add_argument('--confirm-real-order', action='store_true')
    args = ap.parse_args(argv)
    return globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
