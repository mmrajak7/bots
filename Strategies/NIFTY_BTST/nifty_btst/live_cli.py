"""Live / rehearsal commands. Registered by `cli.main`; the paper commands
are untouched and the paper loader never reads a live key.

    python -m nifty_btst live-run             # the live (or rehearsal) session loop
    python -m nifty_btst live-run --once      # one poll, then exit
    python -m nifty_btst live-status          # live book: heartbeat, kill, pending, open
    python -m nifty_btst live-report          # scorecard of the live book
    python -m nifty_btst live-resume --yes    # OWNER ONLY: clear a tripped kill
    python -m nifty_btst live-digest          # (re)send today's live EOD digest
    python -m nifty_btst neo-probe [--symbol] # Stage 0: read-only NEO checks
    python -m nifty_btst neo-order-probe --symbol SYM --confirm-real-order [--step all|hold|sell]

Which state directory: `state_live/` when `execution` is `live`,
`state_rehearsal/` when it is `rehearsal`. Never `state/` (paper).

The loop ticks every `neo.order_poll_sec` (1 s) for NEO order servicing and
every `neo.poll_seconds` (3 s) for the Kite quote + rule step, so order
polling costs Kite nothing. A missing or dead Kite token never stops the
process (NEO-08): the engine starts, reconciles and services NEO regardless,
and the token is re-read each minute. Any unexpected exception at startup is
alerted with the open positions named (NEO-09).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import arming, config as config_mod, live_config, report
from .broker import Broker, FakeBroker, NeoBroker
from .cli import STEP_ERROR_ALERT_AFTER, _Lock
from .journal import Journal
from .kite_io import KiteMarket, Market, TokenError, load_token
from .live_config import LIVE_OVERLAY_FILE, NeoConfig, load_live
from .live_engine import RESUME_FILE, LiveEngine, LiveStore
from .neo_client import (CredentialsError, EnvelopePolicy, NeoReader, NeoTrader,
                         NeoUnavailable, load_credentials, pin_ipv4)
from .neo_order_probe import PROBE_MAX_PREMIUM_RS, OrderProbe, ProbeRefused
from .neo_probe import run_probe
from .neo_symbols import ScripMap, ScripMasterError, parse_scrip_master
from .alerts import AsyncNotifier
from .notify import Notifier
from .sessions import at, is_session, now_ist
from .store import CorruptBook

logger = logging.getLogger('nifty_btst.live')

EXIT_REFUSED = 6
FAKE_BROKER_FILE = 'fake_broker.json'


def state_dir(execution: str) -> Path:
    env = os.environ.get('BTST_LIVE_STATE_DIR')
    if env:
        return Path(env)
    return config_mod.APP_DIR / ('state_live' if execution == 'live' else 'state_rehearsal')


def _load() -> Optional[Tuple[config_mod.Config, str, NeoConfig]]:
    """Live's rule values come from the TRACKED DEFAULTS plus live's OWN
    overlay (`btst_live_config.json`), never the paper overlay (R4-04): a
    paper edit cannot move a live exit, and the R2-04 paper-overlay fallback
    is gone with the path it guarded. Plus the live config (execution, neo)."""
    try:
        cfg = config_mod.load(overlay_path=live_config.LIVE_OVERLAY_FILE)
        execution, neo = load_live()
    except (config_mod.ConfigError, OSError, ValueError, TypeError) as exc:
        logger.error('config unusable: %s', exc)
        Notifier(config_mod.TELEGRAM_CONFIG, True, 'NIFTY-BTST (LIVE)').send(
            'NOT RUNNING: live config unusable (%s): %s' % (LIVE_OVERLAY_FILE.name, exc))
        return None
    return cfg, execution, neo


def _paper_cfg() -> Optional[config_mod.Config]:
    """PAPER's effective config (defaults + paper overlay), read only to report
    where live's rules differ from the twin it is paired with (R4-04)."""
    try:
        return config_mod.load()
    except (config_mod.ConfigError, OSError, ValueError, TypeError) as exc:
        logger.warning('paper config unreadable (%s): pairing cannot be checked', exc)
        return None


def open_records(sdir: Path) -> List[str]:
    """Names of open/pending records in a live book, read BEFORE any check
    that could refuse to start (R2-04). Unreadable = 'UNREADABLE BOOK'."""
    path = sdir / 'book.json'
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return ['#%s %s' % (p.get('id'), p.get('symbol')) for p in data.get('positions', [])
                if isinstance(p, dict) and p.get('status') in ('open', 'pending')]
    except (OSError, ValueError, AttributeError):
        return ['UNREADABLE BOOK']


RETRY_CLEARED = -1          # _alert_until_close: the condition cleared, carry on
REQUEST_ECHO_WAIT_SEC = 20.0
TICK_TIMING_EVERY_SEC = 60.0


def _notifier(cfg: config_mod.Config, execution: str, neo: NeoConfig) -> Notifier:
    """Live has its OWN switch (`neo.telegram_enabled`, R3-07) and posts from
    a worker so an alert can never delay an order (R3-06)."""
    prefix = neo.telegram_prefix_live if execution == 'live' else neo.telegram_prefix_rehearsal
    return AsyncNotifier(config_mod.TELEGRAM_CONFIG, neo.telegram_enabled, prefix)


def _flush(notifier: Notifier) -> None:
    flush = getattr(notifier, 'flush', None)
    if callable(flush):
        flush()


def make_scrip_loader(reader: NeoReader, neo: NeoConfig, sdir: Path, today: date,
                      underlying: str) -> Callable[[], ScripMap]:
    """Downloads the nse_fo scrip master once a day into the state dir."""
    def load() -> ScripMap:
        cache = sdir / ('scrip_master_nse_fo_%s.csv' % today.strftime('%Y%m%d'))
        text: Optional[str] = None
        if cache.exists():
            age_h = (time.time() - cache.stat().st_mtime) / 3600.0
            if age_h <= neo.scrip_master_max_age_hours:
                text = cache.read_text(encoding='utf-8')
        if text is None:
            if not reader.ready:
                # the loader runs in a WORKER: it never logs in itself (R3-34);
                # the loop's own session step does, on the main thread
                raise NeoUnavailable('no NEO session yet; the loop logs in, the worker waits')
            fo = ScripMap.pick_fo_path(reader.scrip_master_paths())
            if not fo:
                raise NeoUnavailable('no nse_fo scrip master link')
            text = reader.fetch_text(fo)
            smap = ScripMap(parse_scrip_master(text, underlying))   # PARSE before caching (R3-12)
            tmp = cache.with_suffix('.tmp')
            tmp.write_text(text, encoding='utf-8')
            tmp.replace(cache)
            for old in sdir.glob('scrip_master_nse_fo_*.csv'):
                if old != cache:
                    old.unlink()
            return smap
        try:
            return ScripMap(parse_scrip_master(text, underlying))
        except ScripMasterError:
            cache.unlink()                      # a cached file that no longer parses is dropped
            raise
    return load


def _policy(neo: NeoConfig) -> EnvelopePolicy:
    return EnvelopePolicy(neo.empty_report_stcode, neo.empty_report_emsg,
                          neo.empty_ok_without_data, neo.auth_stcodes, neo.transient_stcodes,
                          neo.rejected_stcodes, neo.empty_per_report)


def _reader(neo: NeoConfig, trader: bool) -> NeoReader:
    creds = load_credentials(neo.credentials_file)
    cls = NeoTrader if trader else NeoReader
    return cls(creds, neo.login_url, neo.http_timeout_sec, neo.login_timeout_sec,
               policy=_policy(neo))


def build_broker(execution: str, neo: NeoConfig, sdir: Path, clock: Callable[[], datetime]
                 ) -> Tuple[Broker, NeoReader]:
    """live -> NeoBroker on a NeoTrader. rehearsal -> a FakeBroker PERSISTED
    under the state dir (so a restart and an overnight keep its orders and
    positions, NEO-12), with a real NeoReader for login + scrip master."""
    if execution == 'live':
        trader = _reader(neo, trader=True)
        assert isinstance(trader, NeoTrader)
        return NeoBroker(trader, neo.exchange_segment, neo.product, neo.limits_net_field,
                         qty_unit=neo.position_qty_unit,
                         holdings_enabled=neo.holdings_source_enabled), trader
    reader = _reader(neo, trader=False)
    return FakeBroker(clock, fill_delay=neo.fake_fill_delay_sec, path=sdir / FAKE_BROKER_FILE), reader


def _holdings_text(sdir: Path) -> str:
    """Best effort, for an alert when the engine itself could not start."""
    try:
        data = json.loads((sdir / 'book.json').read_text(encoding='utf-8'))
        names = [str(p.get('symbol')) for p in data.get('positions', [])
                 if isinstance(p, dict) and p.get('status') in ('open', 'pending')]
    except Exception:                                   # noqa: BLE001
        return 'open positions UNKNOWN (book unreadable)'
    return 'open/pending: %s' % (', '.join(names) or 'none')


# ---------------------------------------------------------------- live-run
def cmd_live_run(args: argparse.Namespace) -> int:
    live_dir = state_dir('live')
    records = open_records(live_dir)          # FIRST: a real record outranks every setting
    fallback = Notifier(config_mod.TELEGRAM_CONFIG, True, 'NIFTY-BTST (LIVE)')
    loaded = _load()
    if loaded is None:
        if not records:
            return 5
        # a real position and no usable config: alert each minute, and re-read
        # the config each minute so a repaired file is picked up (R3-08)
        box: Dict[str, Any] = {}

        def retry() -> bool:
            box['loaded'] = _load()
            return box['loaded'] is not None
        rc = _alert_until_close(fallback, records, 'live config unusable', args, retry=retry)
        if rc != RETRY_CLEARED:
            return rc
        loaded = box['loaded']
    cfg, execution, neo = loaded
    exits_only = False
    if records and execution != 'live':
        # A rehearsal (or paper) setting must never hide a real position: the
        # live book is run exits-only and the owner is told, every start.
        fallback.send('REAL LIVE RECORDS EXIST (%s) but execution is %r. Running the LIVE book '
                      'exits-only; %s is suspended until the live book is flat.'
                      % (', '.join(records), execution, execution))
        execution, exits_only = 'live', True
    notifier = _notifier(cfg, execution, neo)
    sdir = state_dir(execution)
    try:
        return _live_run(args, cfg, execution, neo, notifier, sdir, records, exits_only)
    except CorruptBook as exc:
        # while a position MAY exist (the book is unreadable, so it may) the
        # alert repeats at every cron tick (R3-33); once a day otherwise
        marker = sdir / ('corrupt_alerted_%s' % now_ist().strftime('%Y%m%d'))
        if records or not marker.exists():
            marker.touch()
            notifier.send('LIVE BOOK CORRUPT, live engine STOPPED. Open positions are NOT '
                          'watched (%s). SELL IN THE APP if needed:\n%s'
                          % (', '.join(records) or 'none known', exc))
        logger.error('%s', str(exc))
        return 3
    except Exception as exc:                            # noqa: BLE001 - the last line of defence
        logger.exception('live run crashed')
        notifier.send('LIVE RUN CRASHED (%s: %s). Nothing is watched until the next cron tick. '
                      '%s. Exit in the app if needed.' % (type(exc).__name__, exc,
                                                          _holdings_text(sdir)))
        return 1
    finally:
        _flush(notifier)


def _live_run(args: argparse.Namespace, cfg: config_mod.Config, execution: str,
              neo: NeoConfig, notifier: Notifier, sdir: Path, records: List[str],
              exits_only: bool) -> int:
    now = now_ist()
    today = now.date()
    paper_book = config_mod.STATE_DIR / 'book.json'
    rep = arming.check(cfg, execution, neo, sdir, today, arming.read_paper_positions(paper_book),
                       paper_cfg=_paper_cfg())
    for line in rep.lines:
        logger.info('ARMING | %s', line)
    if not rep.may_run:
        if not records:
            notifier.send('LIVE RUN REFUSED:\n' + rep.text())
            return EXIT_REFUSED
        # Refusals block ENTRIES only: the open records still get their exits.
        exits_only = True
        notifier.send('LIVE RUN WOULD BE REFUSED (%s) but records are open: %s. Running '
                      'EXITS-ONLY.' % ('; '.join(rep.problems), ', '.join(records)))
    calendar_session = is_session(today)
    if today.weekday() >= 5 and not args.force:
        logger.info('%s is a weekend; nothing to do', today)
        return 0
    sdir.mkdir(parents=True, exist_ok=True)      # only a run that may start owns a state dir
    lock = _Lock(sdir / 'run.lock')
    if not lock.acquire():
        logger.info('another live run holds the lock; exiting')
        return 4
    try:
        return _live_session(args, cfg, execution, neo, notifier, sdir, records, exits_only,
                             rep, now, today, calendar_session, paper_book)
    finally:
        lock.release()      # never left to the GC: a logged exception keeps frames alive (R3-32)


def _live_session(args: argparse.Namespace, cfg: config_mod.Config, execution: str,
                  neo: NeoConfig, notifier: Notifier, sdir: Path, records: List[str],
                  exits_only: bool, rep: arming.ArmReport, now: datetime, today: date,
                  calendar_session: bool, paper_book: Path) -> int:
    store = LiveStore(sdir)
    day = store.day(today)
    if day.get('closed') and not store.holding():
        logger.info('%s already found closed; nothing to do', today)
        return 0
    if 'arming_said' not in day['flags']:
        day['flags'].append('arming_said')
        notifier.send('START %s%s\n%s' % (today, ' (EXITS-ONLY)' if exits_only else '', rep.text()))
        store.save()
    if execution == 'live':
        pin_ipv4()
    while True:
        try:
            broker, reader = build_broker(execution, neo, sdir, now_ist)
            break
        except CredentialsError as exc:
            if not records:
                notifier.send('NOT RUNNING: %s' % exc)
                return EXIT_REFUSED
            # a real position: alert each minute and re-read the credentials
            # file each minute, so a repaired file is picked up (R3-08)
            rc = _alert_until_close(notifier, records, 'NEO credentials unusable: %s' % exc,
                                    args, retry=lambda: bool(load_credentials(neo.credentials_file)))
            if rc != RETRY_CLEARED:
                return rc
    notifier.redactions = [str(reader.creds.get('ucc', ''))]       # never in an alert (R2-18)
    if execution == 'rehearsal':
        try:
            reader.login()
        except Exception as exc:                        # noqa: BLE001
            notifier.send('REHEARSAL: real NEO login failed (%s); the scrip master cannot load '
                          'until it does.' % exc)
    market = _market(cfg, sdir, today)
    if market is None:
        _token_alert(store, notifier, calendar_session, today,
                     'NO KITE QUOTES at start (token missing/stale/corrupt). The engine runs on '
                     'NEO alone: exits due by time are latched, stops are NOT watched. %s'
                     % _holdings_text(sdir))
    engine = LiveEngine(cfg, neo, store, market, notifier, broker, Journal(sdir),
                        make_scrip_loader(reader, neo, sdir, today, cfg.underlying),
                        session_expected=calendar_session or args.force,
                        arm_reader=arming.make_arm_reader(neo, sdir, paper_book),
                        mode=execution, paper_book=paper_book)
    engine.entries_enabled = not exits_only
    close_dt = at(today, cfg.market_close)
    if now > close_dt:
        _send_digest(engine, store, notifier, today)
        return 0
    if market is not None:
        try:
            logger.info('loaded %d %s options', len(market.options()), cfg.underlying)
        except Exception as exc:                        # noqa: BLE001
            logger.warning('instrument preload failed (%s); the mark will retry', exc)
    engine.start(now_ist())
    return _loop(engine, store, notifier, cfg, neo, sdir, today, close_dt,
                 getattr(args, 'once', False), calendar_session)


def _alert_until_close(notifier: Notifier, records: List[str], why: str,
                       args: argparse.Namespace, retry: Optional[Callable[[], bool]] = None
                       ) -> int:
    """Nothing can be serviced, but a real position exists: say so every
    minute, naming it, until the close (R2-04). The alert says that owner
    request files are NOT being read in this state. Each minute `retry` is
    tried; when it succeeds the condition has cleared and RETRY_CLEARED is
    returned so the caller carries on (R3-08)."""
    cfg_close = None
    try:
        cfg_close = config_mod.load(overlay_path=live_config.LIVE_OVERLAY_FILE).market_close
    except Exception:                               # noqa: BLE001
        pass
    while True:
        now = now_ist()
        notifier.send('LIVE POSITION UNSERVICED: %s. %s. Nothing can place an exit and owner '
                      'request files (confirm-held / book-external / resume) are NOT being '
                      'read: SELL IN THE APP if needed.' % (', '.join(records), why))
        if getattr(args, 'once', False) or cfg_close is None or now.time() > cfg_close:
            return 1
        time.sleep(60)
        if retry is not None:
            try:
                if retry():
                    notifier.send('%s: cleared on re-read; starting the engine.' % why)
                    return RETRY_CLEARED
            except Exception as exc:                # noqa: BLE001
                why = 'still unusable: %s' % exc


def _market(cfg: config_mod.Config, sdir: Path, today: date) -> Optional[Market]:
    try:
        tok = load_token(config_mod.KITE_TOKEN_FILE, today)
    except (TokenError, OSError, ValueError) as exc:
        logger.error('Kite token: %s', exc)
        return None
    return KiteMarket.from_token(tok, underlying=cfg.underlying, exchange=cfg.option_exchange,
                                 cache_dir=sdir, today=today, retries=cfg.quote_retries)


def _token_alert(store: LiveStore, notifier: Notifier, calendar_session: bool, today: date,
                 text: str) -> None:
    day = store.day(today)
    logger.error('%s', text)
    if 'token_alert' not in day['flags'] and calendar_session:
        day['flags'].append('token_alert')
        store.save()
        notifier.send(text)


def _loop(engine: LiveEngine, store: LiveStore, notifier: Notifier, cfg: config_mod.Config,
          neo: NeoConfig, sdir: Path, today: date, close_dt: datetime, once: bool,
          calendar_session: bool) -> int:
    open_dt = at(today, cfg.market_open)
    errors = 0
    error_alert_at: Optional[datetime] = None
    last_step: Optional[datetime] = None
    token_dead_at: Optional[datetime] = None
    timing: Dict[str, float] = {'n': 0, 'sum': 0.0, 'max': 0.0, 'said': time.monotonic()}

    def failed(what: str, now: datetime) -> None:
        # R2-11: say so every minute while it continues, naming the holdings
        nonlocal errors, error_alert_at
        errors += 1
        logger.exception('live %s failed (%d in a row)', what, errors)
        if errors >= STEP_ERROR_ALERT_AFTER and (
                error_alert_at is None or (now - error_alert_at).total_seconds() >= 60):
            error_alert_at = now
            notifier.send('LIVE TICK ERRORS: %d in a row (%s failing), see the log. Still '
                          'retrying. %s' % (errors, what, _holdings_text(sdir)))

    while True:
        now = now_ist()
        if now > close_dt:
            break
        if engine.closed_today == today and not store.holding():
            logger.info('market closed today and nothing held; exiting')
            return 0
        if now < open_dt:
            store.heartbeat(now, state='pre-open', opens_at=open_dt.isoformat(),
                            open=len(store.holding()), pending=len(store.pending()))
            time.sleep(min(30.0, (open_dt - now).total_seconds() + 0.5))
            continue
        ok = True
        try:                                           # NEO: orders in flight, reconcile
            engine.service_orders(now)
        except Exception:                              # noqa: BLE001
            ok = False
            failed('service_orders', now)
        try:                                           # Kite: the rule, in its OWN try (R2-11)
            if engine.market is None and (token_dead_at is None
                                          or (now - token_dead_at).total_seconds() >= 60):
                token_dead_at = now
                market = _market(cfg, sdir, today)
                if market is not None:
                    engine.market = market
                    token_dead_at = None
                    notifier.send('Kite token works again; quotes resumed.')
            due = last_step is None or (now - last_step).total_seconds() >= neo.poll_seconds
            if due:
                last_step = now
                try:
                    engine.step(now)
                except TokenError as exc:
                    engine.market = None     # type: ignore[assignment]
                    token_dead_at = now
                    _token_alert(store, notifier, calendar_session, today,
                                 'KITE TOKEN REJECTED mid-session: %s\nNo quotes: stops and '
                                 'triggers are NOT watched. Re-reading the token each minute.'
                                 % exc)
        except Exception:                              # noqa: BLE001
            ok = False
            failed('step', now)
        if ok:
            errors = 0
            error_alert_at = None
        elapsed = (now_ist() - now).total_seconds()
        engine.last_tick_ms = elapsed * 1000.0
        timing['n'] += 1
        timing['sum'] += elapsed
        timing['max'] = max(timing['max'], elapsed)
        if time.monotonic() - timing['said'] >= TICK_TIMING_EVERY_SEC or once:
            # the Pi's own evidence for R3-37: how long a tick really takes
            logger.info('TICK timing: last %.0f ms, mean %.0f ms, max %.0f ms over %d ticks; '
                        'book saves %d (skipped unchanged %d); telegram pending %s dropped %s',
                        elapsed * 1000, timing['sum'] / timing['n'] * 1000, timing['max'] * 1000,
                        int(timing['n']), store.saves, store.skipped_saves,
                        getattr(notifier, 'pending', '-'), getattr(notifier, 'dropped', '-'))
            timing.update(n=0, sum=0.0, max=0.0, said=time.monotonic())
        if once:
            return 0
        time.sleep(max(0.1, neo.order_poll_sec - elapsed))
    try:
        engine.service_orders(now_ist())          # one read-back after the close
    except Exception:                              # noqa: BLE001
        logger.exception('post-close read-back failed')
    _send_digest(engine, store, notifier, today)
    return 0


def _send_digest(engine: LiveEngine, store: LiveStore, notifier: Notifier, today: date) -> None:
    day = store.day(today)
    if day.get('eod_sent'):
        return
    try:
        text = engine.eod_digest(today)
    except Exception as exc:                        # noqa: BLE001 - a digest must never hide a day
        logger.exception('digest failed')
        text = 'EOD %s: digest FAILED (%s: %s). %s' % (today, type(exc).__name__, exc,
                                                      _holdings_text(store.dir))
    notifier.send(text)
    day['eod_sent'] = True
    store.save()


# ------------------------------------------------------------- the others
def _effective_execution(execution: str) -> str:
    """Real records outrank settings for EVERY owner command (R3-16): with a
    live record open, status/report/resume/digest address the LIVE book
    whatever `execution` says, and say so."""
    if execution != 'live':
        records = open_records(state_dir('live'))
        if records:
            print('NOTE: live records exist (%s); addressing the LIVE book, not %r.'
                  % (', '.join(records), execution))
            return 'live'
    return execution


def _open_engine_offline(cfg: config_mod.Config, execution: str, neo: NeoConfig
                         ) -> Tuple[LiveEngine, LiveStore]:
    sdir = state_dir(execution)
    store = LiveStore(sdir)
    notifier = _notifier(cfg, execution, neo)

    def no_scrips() -> ScripMap:
        raise ScripMasterError('offline')
    engine = LiveEngine(cfg, neo, store, None, notifier, FakeBroker(now_ist), Journal(sdir),
                        no_scrips, mode=execution)
    engine.offline = True           # nothing may be sent from here (R4-14)
    return engine, store


def cmd_live_status(args: argparse.Namespace) -> int:
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    execution = _effective_execution(execution)
    sdir = state_dir(execution)
    store = LiveStore(sdir)
    now = now_ist()
    rep = arming.check(cfg, execution, neo, sdir, now.date(),
                       arming.read_paper_positions(config_mod.STATE_DIR / 'book.json'),
                       paper_cfg=_paper_cfg())
    print(rep.text())
    hb = store.read_heartbeat()
    if hb:
        age = (now - datetime.fromisoformat(hb['t'])).total_seconds()
        print('heartbeat %s (%.0fs ago) %s' % (hb['t'], age, {k: v for k, v in hb.items() if k != 't'}))
    else:
        print('no heartbeat yet')
    k = store.kill()
    print('kill: %s | equity %s peak %s drawdown %s'
          % ('TRIPPED (%s at %s)' % (k.get('reason'), k.get('at')) if k.get('tripped') else 'ok',
             k.get('equity'), k.get('peak'), k.get('drawdown')))
    day = store.data['days'].get(now.date().isoformat())
    if day:
        for side, m in (day.get('marks') or {}).items():
            if m:
                print('  %s %s mark %.2f trigger %.2f fired=%s neo=%s'
                      % (side, m['symbol'], m['mark_price'], m['trigger'],
                         bool(day['fired'].get(side)), (m.get('neo') or {}).get('symbol')
                         or m.get('neo_error') or 'unmapped'))
        if day['flags']:
            print('  flags:', ', '.join(day['flags']))
    for p in store.pending():
        lv = p['live']
        print('PENDING #%d %s %s state %s attempts %s leg %s'
              % (p['id'], p['side'], p['symbol'], lv['state'], lv.get('entry_attempts'),
                 (lv.get('leg') or {}).get('tag')))
    print(report.render_open(store.holding()) or 'no open positions')
    for p in store.holding():
        lv = p['live']
        print('  #%d state %s exit_reason %s attempts %s leg %s'
              % (p['id'], lv['state'], lv.get('exit_reason'), lv.get('exit_attempts'),
                 (lv.get('leg') or {}).get('tag')))
    unresolved = Journal(sdir).unresolved()
    if unresolved:
        print('UNRESOLVED INTENTS: %s' % ', '.join(str(u.get('tag')) for u in unresolved))
    if (sdir / RESUME_FILE).exists():
        print('RESUME request pending (the loop applies it on its next tick)')
    for bad in sorted(sdir.glob('*.bad')) + sorted(sdir.glob('*.rejected')):
        print('REFUSED REQUEST FILE moved aside: %s' % bad.name)
    return 0


def cmd_live_report(args: argparse.Namespace) -> int:
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    execution = _effective_execution(execution)
    store = LiveStore(state_dir(execution))
    print(report.render(store.closed_positions(), store.holding()))
    nf = store.never_filled()
    if nf:
        print('never filled: %d (%s)' % (len(nf), ', '.join(
            '#%d %s' % (p['id'], p.get('exit_reason')) for p in nf)))
    return 0


def cmd_live_resume(args: argparse.Namespace) -> int:
    """Write a RESUME request. The running loop applies it on its next tick
    (NEO-17); when no loop holds the lock it is applied here directly."""
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    execution = _effective_execution(execution)
    sdir = state_dir(execution)
    store = LiveStore(sdir)
    k = store.kill()
    if not k.get('tripped'):
        print('kill is not tripped; nothing to do')
        return 0
    if not args.yes:
        print('kill TRIPPED: %s at %s. Re-run with --yes to clear it and reset the peak to '
              'the current equity (%s).' % (k.get('reason'), k.get('at'), k.get('equity')))
        return 1
    return _owner_request(args, RESUME_FILE, {'action': 'resume'}, execution=execution)


def _owner_request(args: argparse.Namespace, filename: str, body: Dict[str, Any],
                   execution: str = 'live') -> int:
    """Write a request file for the running loop; apply directly when no
    loop holds the lock (the same shape as RESUME, NEO-17). The file is
    STAMPED with today's date and written atomically (R3-02); the outcome is
    read back from `<name>.result` and echoed truthfully, naming what was
    applied (R3-23); when nothing reads the request within the wait, the
    command says so instead of 'requested'."""
    loaded = _load()
    if loaded is None:
        return 5
    cfg, _execution, neo = loaded
    sdir = state_dir(execution)
    sdir.mkdir(parents=True, exist_ok=True)
    req = sdir / filename
    result = req.with_name(req.name + '.result')
    if result.exists():
        result.unlink()
    stamped = dict(body, date=now_ist().date().isoformat(), written_at=now_ist().isoformat())
    tmp = req.with_name(req.name + '.tmp')
    tmp.write_text(json.dumps(stamped), encoding='utf-8')
    tmp.replace(req)
    lock = _Lock(sdir / 'run.lock')
    if not lock.acquire():
        store = LiveStore(sdir)
        hb = store.read_heartbeat() or {}
        if hb.get('state') == 'pre-open' and now_ist().isoformat() < str(hb.get('opens_at')):
            # the loop is alive and waiting for the open: the request is read
            # on its first tick (R4-07)
            print('QUEUED: the engine is running and waiting for the open; %s applies at %s '
                  '(its first tick). Nothing else to do.' % (req.name, str(hb.get('opens_at'))[11:19]))
            return 0
        age = (now_ist() - datetime.fromisoformat(hb['t'])).total_seconds() if hb else None
        deadline = time.monotonic() + REQUEST_ECHO_WAIT_SEC
        while time.monotonic() < deadline and not result.exists():
            time.sleep(0.5)
        return _echo_result(result, req, age)
    try:
        engine, store = _open_engine_offline(cfg, execution, neo)
        try:
            engine._apply_owner_requests(now_ist())
        finally:
            store.save()
            _flush(engine.notify)
    finally:
        lock.release()
    return _echo_result(result, req, None)


def _echo_result(result: Path, req: Path, heartbeat_age: Optional[float]) -> int:
    if result.exists():
        try:
            body = json.loads(result.read_text(encoding='utf-8'))
            result.unlink()
        except (OSError, ValueError) as exc:
            print('result file unreadable (%s): check `live-status` and the journal' % exc)
            return 1
        if body.get('applied'):
            print('APPLIED: %s' % body.get('text'))
            return 0
        print('NOT APPLIED: %s' % body.get('text'))
        return EXIT_REFUSED
    if req.exists():
        print('NOT APPLIED: the running loop has not read %s within %.0fs%s. It is still on '
              'disk; the loop applies it when it next ticks, or it is refused as stale '
              'tomorrow. Check `live-status`.'
              % (req.name, REQUEST_ECHO_WAIT_SEC,
                 '' if heartbeat_age is None else ' (heartbeat %.0fs old%s)'
                 % (heartbeat_age, '; the engine is NOT reading requests' if heartbeat_age > 120
                    else '')))
        return 1
    print('UNKNOWN: the request was taken but no result was written; check `live-status` and '
          'the journal before re-issuing it.')
    return 1


def _request_body(pos_id: int, execution: str = 'live') -> Optional[Dict[str, Any]]:
    """The record's own id and symbol go into the request, so the engine can
    refuse a mismatch (R3-02)."""
    try:
        store = LiveStore(state_dir(execution))
    except CorruptBook as exc:
        print('REFUSED: live book unreadable (%s)' % exc)
        return None
    for p in store.data.get('positions', []):
        if int(p.get('id', -1)) == pos_id:
            return {'id': pos_id, 'symbol': p.get('symbol'),
                    'neo_symbol': (p.get('live') or {}).get('neo_symbol'),
                    'status': p.get('status')}
    print('REFUSED: no position #%d in the %s book' % (pos_id, execution))
    return None


def cmd_live_confirm_held(args: argparse.Namespace) -> int:
    """OWNER: 'I checked the app; position ID is held today.' The positive
    source the selling guard accepts when the broker shows no row (R2-01).
    Expires tonight; voided by any SELL with a fill or still live (R3-01/29). On
    a PENDING record it opens the position at the real fill, else at the
    entry limit marked as an estimate (R3-04)."""
    if not args.yes:
        print('This tells the engine the lot is HELD at the broker today, on your word. '
              'Re-run with --yes after checking the app.')
        return 1
    loaded = _load()
    if loaded is None:
        return 5
    execution = _effective_execution(loaded[1])
    body = _request_body(args.id, execution)
    if body is None:
        return EXIT_REFUSED
    body['action'] = 'confirm_held'
    return _owner_request(args, 'CONFIRM_HELD_%d' % args.id, body, execution=execution)


def cmd_live_book_external(args: argparse.Namespace) -> int:
    """OWNER: book position ID as closed outside the engine at --price
    (R2-19), or a pending entry as never filled (--not-filled). Journalled."""
    if not args.yes:
        print('This closes the book record without any order. Re-run with --yes.')
        return 1
    if args.price is None and not args.not_filled:
        print('REFUSED: give --price X (the sale price in the app) or --not-filled')
        return EXIT_REFUSED
    loaded = _load()
    if loaded is None:
        return 5
    execution = _effective_execution(loaded[1])
    body = _request_body(args.id, execution)
    if body is None:
        return EXIT_REFUSED
    body.update(action='book_external', price=None if args.not_filled else args.price)
    return _owner_request(args, 'BOOK_EXTERNAL_%d.json' % args.id, body, execution=execution)


def cmd_live_digest(args: argparse.Namespace) -> int:
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    execution = _effective_execution(execution)
    sdir = state_dir(execution)
    lock = _Lock(sdir / 'run.lock')
    if not lock.acquire():
        print('the live loop is running; it sends the digest itself after the close')
        return 4
    try:
        engine, store = _open_engine_offline(cfg, execution, neo)
        today = now_ist().date()
        store.day(today).pop('eod_sent', None)
        _send_digest(engine, store, engine.notify, today)
        _flush(engine.notify)
    finally:
        lock.release()
    return 0


# ----------------------------------------------------------------- probes
def _kite_row(cfg: config_mod.Config, today: date, symbol: Optional[str], cache_dir: Path
              ) -> Tuple[Optional[Dict[str, Any]], Optional[KiteMarket]]:
    """The Kite instrument row for `symbol` (from the live cache dir or Kite)."""
    if not symbol:
        return None, None
    tok = load_token(config_mod.KITE_TOKEN_FILE, today)
    market = KiteMarket.from_token(tok, underlying=cfg.underlying, exchange=cfg.option_exchange,
                                   cache_dir=cache_dir, today=today, retries=cfg.quote_retries)
    for o in market.options():
        if o['tradingsymbol'] == symbol:
            return o, market
    raise SystemExit('%s is not a %s option in the Kite instrument list' % (symbol, cfg.underlying))


def cmd_neo_probe(args: argparse.Namespace) -> int:
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    today = now_ist().date()
    sdir = state_dir('live')
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        row, _ = _kite_row(cfg, today, args.symbol, sdir)
    except (TokenError, OSError, ValueError) as exc:
        print('Kite token: %s (the NEO probe continues without a Kite row)' % exc)
        row = None
    try:
        reader = _reader(neo, trader=False)
    except CredentialsError as exc:
        print('REFUSED: %s' % exc)
        return EXIT_REFUSED
    lock = _Lock(sdir / 'run.lock')             # held for the whole probe (R3-31)
    if not lock.acquire():
        print('REFUSED: a live-run holds %s; whether a second login disturbs it is unmeasured. '
              'Stop the live cron first.' % (sdir / 'run.lock'))
        return EXIT_REFUSED
    try:
        pin_ipv4()
        return run_probe(reader, cfg.underlying, today, kite_row=row,
                         net_field=neo.limits_net_field)
    finally:
        lock.release()


def cmd_neo_order_probe(args: argparse.Namespace) -> int:
    loaded = _load()
    if loaded is None:
        return 5
    cfg, execution, neo = loaded
    now = now_ist()
    today = now.date()
    sdir = state_dir('live')
    if execution != 'live':
        print('REFUSED: execution is %r; the order probe places REAL orders and needs "live"'
              % execution)
        return EXIT_REFUSED
    if not args.confirm_real_order:
        print('REFUSED: pass --confirm-real-order (this places real orders)')
        return EXIT_REFUSED
    if not arming.read_probe_arm(sdir, today):
        print('REFUSED: write today\'s date (%s) to %s. The strategy ARM does not arm the probe.'
              % (today, sdir / arming.PROBE_ARM_FILE))
        return EXIT_REFUSED
    if args.max_premium > PROBE_MAX_PREMIUM_RS:
        print('REFUSED: --max-premium above the code ceiling Rs %.0f' % PROBE_MAX_PREMIUM_RS)
        return EXIT_REFUSED
    if not (cfg.market_open <= now.time() <= cfg.market_close):
        print('REFUSED: outside market hours')
        return EXIT_REFUSED
    sdir.mkdir(parents=True, exist_ok=True)
    lock = _Lock(sdir / 'run.lock')             # a NAMED lock, held for the run (R3-31)
    if not lock.acquire():
        # whether a second login kills the first is unmeasured: never probe
        # beside a running live loop (R2-26)
        print('REFUSED: a live-run holds %s. Stop the live cron first (see README Stage 3).'
              % (sdir / 'run.lock'))
        return EXIT_REFUSED
    try:
        return _order_probe_locked(args, cfg, execution, neo, sdir, today)
    finally:
        lock.release()


def _order_probe_locked(args: argparse.Namespace, cfg: config_mod.Config, execution: str,
                        neo: NeoConfig, sdir: Path, today: date) -> int:
    try:
        row, market = _kite_row(cfg, today, args.symbol, sdir)
    except (TokenError, OSError, ValueError) as exc:
        print('REFUSED: %s' % exc)
        return 2
    assert row is not None and market is not None
    key = '%s:%s' % (cfg.option_exchange, args.symbol)

    def quote() -> Optional[Any]:
        try:
            return market.quote([key]).get(key)
        except Exception as exc:                        # noqa: BLE001
            print('  quote failed: %s' % exc)
            return None
    if quote() is None:
        print('REFUSED: no Kite quote for %s' % args.symbol)
        return EXIT_REFUSED
    pin_ipv4()
    notifier = _notifier(cfg, execution, neo)
    try:
        broker, reader = build_broker(execution, neo, sdir, now_ist)
        notifier.redactions = [str(reader.creds.get('ucc', ''))]
        broker.ensure_session()
        scrips = make_scrip_loader(reader, neo, sdir, today, cfg.underlying)()
    except Exception as exc:                            # noqa: BLE001
        print('REFUSED: %s' % exc)
        return EXIT_REFUSED
    probe = OrderProbe(broker, Journal(sdir), now_ist, time.sleep,
                       allow_missing_row=args.allow_missing_row,
                       owner_confirms_held=args.owner_confirms_held,
                       max_premium=args.max_premium)
    probe.redactions = list(notifier.redactions)
    rc = 1
    try:
        rc = probe.run(scrips, row, quote, args.max_premium, step=args.step)
    except ProbeRefused as exc:
        probe.report.append('REFUSED/STOPPED: %s' % exc)
        print('REFUSED/STOPPED: %s' % exc)
    finally:
        notifier.send('ORDER PROBE [%s] %s\n%s' % (
            args.step, 'OK' if rc == 0 else 'ATTENTION',
            '\n'.join(probe.report)[-3500:]))
        _flush(notifier)
    return rc


def register(sub: Any) -> Dict[str, Callable[[argparse.Namespace], int]]:
    r = sub.add_parser('live-run', help='live/rehearsal session loop')
    r.add_argument('--force', action='store_true')
    r.add_argument('--once', action='store_true')
    sub.add_parser('live-status')
    sub.add_parser('live-report')
    res = sub.add_parser('live-resume', help='owner: clear a tripped kill')
    res.add_argument('--yes', action='store_true')
    sub.add_parser('live-digest')
    pr = sub.add_parser('neo-probe', help='Stage 0 read-only NEO probe')
    pr.add_argument('--symbol', help='a Kite option symbol to map (e.g. the marked weekly)')
    op = sub.add_parser('neo-order-probe', help='Stage 3: REAL 1-lot order probe')
    op.add_argument('--symbol', required=True, help='exact Kite symbol of a cheap far-OTM weekly')
    op.add_argument('--confirm-real-order', action='store_true')
    op.add_argument('--step', choices=('all', 'hold', 'sell'), default='all')
    op.add_argument('--allow-missing-row', action='store_true',
                    help='sell step: sell on the journalled hold when the positions row is absent '
                         '(needs --owner-confirms-held too)')
    op.add_argument('--owner-confirms-held', action='store_true',
                    help='sell step: the owner has checked the app and the lot is held')
    ch = sub.add_parser('live-confirm-held', help='owner: the lot of position ID is held today')
    ch.add_argument('id', type=int)
    ch.add_argument('--yes', action='store_true')
    be = sub.add_parser('live-book-external', help='owner: book position ID as closed in the app')
    be.add_argument('id', type=int)
    be.add_argument('--price', type=float)
    be.add_argument('--not-filled', action='store_true')
    be.add_argument('--yes', action='store_true')
    op.add_argument('--max-premium', type=float, default=500.0,
                    help='refuse if ask x lot exceeds this (code ceiling Rs %.0f)' % PROBE_MAX_PREMIUM_RS)
    return {'live-run': cmd_live_run, 'live-status': cmd_live_status,
            'live-report': cmd_live_report, 'live-resume': cmd_live_resume,
            'live-digest': cmd_live_digest, 'neo-probe': cmd_neo_probe,
            'neo-order-probe': cmd_neo_order_probe,
            'live-confirm-held': cmd_live_confirm_held,
            'live-book-external': cmd_live_book_external}


if __name__ == '__main__':      # pragma: no cover
    sys.exit(1)
