"""Command line: run / status / report / compare / digest.

    python -m nifty_btst run            # the session loop (cron target)
    python -m nifty_btst run --force    # also on a day the calendar calls closed
                                        # (special sessions such as Budget Sunday)
    python -m nifty_btst status         # heartbeat, today's marks, open positions
    python -m nifty_btst report         # scorecard of closed paper trades
    python -m nifty_btst compare FILE   # pair with a Volrix "Download CSV" export
    python -m nifty_btst digest         # (re)send today's EOD digest

Exit codes: 0 ok / nothing to do, 2 token problem, 3 corrupt book, 4 already running,
5 config unusable.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import IO, List, Optional

from . import compare as cmp
from . import config as config_mod
from . import report
from .engine import Engine
from .kite_io import KiteMarket, TokenError, load_token
from .notify import Notifier
from .sessions import at, is_session, now_ist
from .store import CorruptBook, Store

logger = logging.getLogger('nifty_btst')

#: consecutive unexpected step errors before a Telegram alert
STEP_ERROR_ALERT_AFTER = 5


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')


class _Lock:
    """Single instance per state dir. cron's flock does the same on the Pi;
    this covers a manual run started beside the cron one."""

    def __init__(self, path: Path):
        self.path = path
        self.fh: Optional[IO[str]] = None

    def acquire(self) -> bool:
        try:
            import fcntl  # type: ignore[import-not-found,unused-ignore]
        except ImportError:          # Windows: manual runs only, no cron
            return True
        self.fh = open(self.path, 'w')
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined,unused-ignore]
            return True
        except OSError:
            return False


def _send_digest(engine: Engine, store: Store, notifier: Notifier, today) -> None:
    day = store.day(today)
    if day.get('eod_sent'):
        return
    notifier.send(engine.eod_digest(today))
    day['eod_sent'] = True
    store.save()


def cmd_run(args: argparse.Namespace) -> int:
    try:
        cfg = config_mod.load()
    except (config_mod.ConfigError, OSError, ValueError) as exc:
        # No config means no telegram_prefix either: say it with the defaults.
        logger.error('config unusable: %s', exc)
        Notifier(config_mod.TELEGRAM_CONFIG, True, 'NIFTY-BTST (paper)').send(
            'NOT RUNNING: config unusable: %s' % exc)
        return 5
    notifier = Notifier(config_mod.TELEGRAM_CONFIG, cfg.telegram_enabled,
                        cfg.telegram_prefix)
    now = now_ist()
    today = now.date()
    # Weekdays always run, whatever the holiday calendar says: the calendar
    # has been wrong in both directions, and a missed session costs more here
    # than anywhere (the profit sits in a few big days). Whether the market is
    # live is read from the option books; the calendar only decides whether a
    # dead market is worth an alert. Weekends need --force.
    calendar_session = is_session(today)
    if today.weekday() >= 5 and not args.force:
        logger.info('%s is a weekend; nothing to do (--force for a special '
                    'session)', today)
        return 0
    lock = _Lock(config_mod.STATE_DIR / 'run.lock')
    config_mod.STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not lock.acquire():
        logger.info('another run holds the lock; exiting')
        return 4
    try:
        store = Store(config_mod.STATE_DIR)
    except CorruptBook as exc:
        # cron retries every 5 minutes: alert once a day, not 80 times.
        marker = config_mod.STATE_DIR / ('corrupt_alerted_%s' % today.strftime('%Y%m%d'))
        if not marker.exists():
            marker.touch()
            notifier.send('BOOK CORRUPT, paper trader STOPPED:\n%s' % exc)
        logger.error('%s', exc)
        return 3
    day = store.day(today)
    if day.get('closed'):
        logger.info('%s already found closed; nothing to do', today)
        return 0

    def token_alert(text: str) -> int:
        # Once a day: cron restarts a stopped run every 5 minutes. Silent on
        # a calendar holiday, when no token being generated is expected.
        logger.error('%s', text)
        if 'token_alert' not in day['flags'] and calendar_session:
            day['flags'].append('token_alert')
            store.save()
            notifier.send(text)
        return 2

    try:
        tok = load_token(config_mod.KITE_TOKEN_FILE, today)
    except TokenError as exc:
        return token_alert('NOT RUNNING: %s' % exc)
    market = KiteMarket.from_token(
        tok, underlying=cfg.underlying, exchange=cfg.option_exchange,
        cache_dir=config_mod.STATE_DIR, today=today, retries=cfg.quote_retries)
    engine = Engine(cfg, store, market, notifier,
                    session_expected=calendar_session or args.force)

    close_dt = at(today, cfg.market_close)
    if now > close_dt:
        _send_digest(engine, store, notifier, today)
        return 0
    try:
        n = len(market.options())
        logger.info('loaded %d %s options', n, cfg.underlying)
    except TokenError as exc:
        return token_alert('NOT RUNNING: %s' % exc)
    except Exception as exc:
        logger.warning('instrument preload failed (%s); the mark will retry', exc)

    open_dt = at(today, cfg.market_open)
    errors = 0
    once = getattr(args, 'once', False)
    while True:
        now = now_ist()
        if now > close_dt:
            break
        if engine.closed_today == today:
            logger.info('market closed today; exiting')
            return 0
        if now < open_dt:
            time.sleep(min(30.0, (open_dt - now).total_seconds() + 0.5))
            continue
        try:
            engine.step(now)
            errors = 0
        except TokenError as exc:
            return token_alert('STOPPED, token rejected mid-session: %s\nOpen '
                               'positions are NOT being watched.' % exc)
        except Exception:
            errors += 1
            logger.exception('step failed (%d in a row)', errors)
            if errors == STEP_ERROR_ALERT_AFTER:
                notifier.send('STEP ERRORS: %d in a row, see the log. Still '
                              'retrying.' % errors)
        if once:
            return 0
        elapsed = (now_ist() - now).total_seconds()
        time.sleep(max(0.2, cfg.poll_seconds - elapsed))
    _send_digest(engine, store, notifier, today)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = Store(config_mod.STATE_DIR)
    hb = store.read_heartbeat()
    now = now_ist()
    if hb:
        from datetime import datetime
        age = (now - datetime.fromisoformat(hb['t'])).total_seconds()
        print('heartbeat %s (%.0fs ago) %s' % (hb['t'], age,
                                                {k: v for k, v in hb.items() if k != 't'}))
    else:
        print('no heartbeat yet')
    day = store.data['days'].get(now.date().isoformat())
    if day:
        print('today %s: %s' % (now.date(), day.get('cls', 'not marked')))
        for side, m in (day.get('marks') or {}).items():
            if m:
                print('  %s %s mark %.2f trigger %.2f fired=%s%s'
                      % (side, m['symbol'], m['mark_price'], m['trigger'],
                         bool(day['fired'].get(side)),
                         ' MISSED' if side in day['missed'] else ''))
        if day['flags']:
            print('  flags:', ', '.join(day['flags']))
    print(report.render_open(store.open_positions()) or 'no open positions')
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    store = Store(config_mod.STATE_DIR)
    print(report.render(store.closed_positions(), store.open_positions()))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    store = Store(config_mod.STATE_DIR)
    from datetime import date
    since = date.fromisoformat(args.since) if args.since else None
    vol = cmp.load_volrix(Path(args.csv))
    print(cmp.render(*cmp.pair(store.data['positions'], vol, since)))
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    cfg = config_mod.load()
    store = Store(config_mod.STATE_DIR)
    notifier = Notifier(config_mod.TELEGRAM_CONFIG, cfg.telegram_enabled,
                        cfg.telegram_prefix)
    engine = Engine(cfg, store, None, notifier)   # digest never touches the market
    today = now_ist().date()
    store.day(today).pop('eod_sent', None)
    _send_digest(engine, store, notifier, today)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    _logging()
    ap = argparse.ArgumentParser(prog='nifty_btst')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--force', action='store_true',
                   help='run on a day the holiday calendar calls closed')
    r.add_argument('--once', action='store_true', help='one poll, then exit')
    sub.add_parser('status')
    sub.add_parser('report')
    c = sub.add_parser('compare')
    c.add_argument('csv')
    c.add_argument('--since', help='YYYY-MM-DD; default = first paper trade')
    sub.add_parser('digest')
    args = ap.parse_args(argv)
    return {'run': cmd_run, 'status': cmd_status, 'report': cmd_report,
            'compare': cmd_compare, 'digest': cmd_digest}[args.cmd](args)
