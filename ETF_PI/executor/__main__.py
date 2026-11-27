"""ETF_PI live executor -- CLI (docs/EXECUTOR_SPEC.md; deploy: docs/EXECUTOR_RUNBOOK.md).

Run from the ETF_PI directory with the SAME interpreter as option_buy (Pi: /usr/bin/python3):
  python3 -m executor run        the daemon (systemd etf-executor.service)
  python3 -m executor watchdog   cron, every minute: stale heartbeat -> restart + Telegram (H2)
  python3 -m executor status     print state/executor/status.json
  python3 -m executor kill       no NEW entries (file KILL); open trades keep their exits
  python3 -m executor flatten    exit everything now + no new entries (file FLATTEN)
  python3 -m executor resume     remove KILL and FLATTEN
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


def build(cfg):
    import market_calendar
    from executor.broker import Broker
    from executor.engine import Engine
    from executor.journal import Journal
    from executor.notify import DeadMan, Notifier
    from executor.prices import PriceFeed
    ob = import_option_buy()
    os.makedirs(C.STATE_DIR, exist_ok=True)

    def rest_quote(key):
        rec = ob._kite_call('quote', lambda: ob.get_kite().quote([key]))
        return (rec or {}).get(key)

    def ticker(api_key, access):
        from kiteconnect import KiteTicker
        return KiteTicker(api_key, access)
    feed = PriceFeed(cfg, ob.TOKEN_PATH, C.STATE_DIR, rest_quote, ticker)
    broker = Broker(cfg, paper=(cfg['mode'] != 'live'))
    cal = market_calendar.load()
    eng = Engine(cfg, broker, feed, Notifier(prefix='ETF %s' % cfg['mode'].upper()),
                 Journal(os.path.join(C.STATE_DIR, 'journal.jsonl')), ob, C.STATE_DIR,
                 session_fn=lambda d: market_calendar.session(ob.U, d, cal),
                 eod_fn=lambda d: min(ob.U.eod_close(d), cfg['eod_time_t'] or ob.U.eod_close(d)),
                 deadman=DeadMan(DeadMan.url_from_creds(), cfg['deadman_every_s']))
    eng.consts_digest = consts_digest(ob)
    return eng, ob


def cmd_run(args) -> int:
    _logging('executor')
    cfg = C.load()
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
        except Exception as e:                         # the loop itself must survive
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
    cfg = C.load()
    if cfg['mode'] == 'off':
        return 0
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
    print('EOD today: %s; state dir %s' % (ob.U.eod_close(_dt.date.today()), C.STATE_DIR))
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
    for name in ('run', 'watchdog', 'status', 'kill', 'flatten', 'resume', 'check'):
        sub.add_parser(name)
    pp = sub.add_parser('probe')
    pp.add_argument('--symbol', required=True, help='Kite tradingsymbol, e.g. NIFTY2612026000CE')
    pp.add_argument('--confirm-real-order', action='store_true')
    args = ap.parse_args(argv)
    return globals()['cmd_' + args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
