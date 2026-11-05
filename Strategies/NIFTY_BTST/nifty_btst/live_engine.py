"""The live BTST engine: the paper rule, with a broker where the fill used to be.

`LiveEngine` is `Engine` with four methods replaced — `mark`, `entries`,
`manage_open`, `settle_expired` — and two added — `start` and
`service_orders`. The mark, the holiday logic, the late-start MISSED rule, the
quote plumbing and the evidence log are inherited untouched, so the live book
sees exactly the trades the paper book sees and `engine.py` is not edited.

THE RULE THAT GOVERNS EVERY DOUBT (review NEO-01/02/04/06): **absence is not
proof.** A book that does not show an order, or a position row that is not
there, is never on its own a reason to send again. A send with no clear
answer stays UNKNOWN until the broker shows it, or until `unknown_grace_sec`
has passed with at least two positively valid reads AND the positions
report agrees nothing happened. An order we hold a number for is never
dropped on the same day; on a later day it is resolved only against
positions and the trade book (DAY validity ended at the close). Every BUY and
every SELL reads positions and the order book first. A short, an extra lot,
or an order carrying a tag we had written off trips the kill and shouts every
minute — and nothing is ever auto-traded to repair it.

WHAT IS DIFFERENT FROM PAPER, AND WHY
  * A fill is a broker fact, not a quote. An entry is a LIMIT BUY at the ask,
    capped at trigger x (1 + entry_cap_pct) [Q2]; an exit is a LIMIT SELL at
    the bid, chased. The stop level is 50% of the REAL fill (`avgPrc`), and
    fires on LTP or on a two-sided bid at or below it (NEO-30).
  * Every send is journalled first (`journal.py`).
  * The kill [Q3] stops NEW entries only; exits always run; only the owner
    resets it, through a RESUME request the running loop applies (NEO-17).
  * No order is ever cancelled on a restart and nothing is auto-flattened.
  * The order ceiling never stops an exit (NEO-21): past it the kill trips
    (no new entries) and exits keep their minimum send interval.

STATE OF ONE LIVE POSITION (`p['live']['state']`)
  ENTRY_WAIT  triggered, no order live (above the cap, or an attempt cancelled)
  ENTRY_SENT  intent journalled, no order number yet (UNKNOWN until resolved)
  ENTRY_OPEN  order number known, not terminal
  FILLED      holding (p['status'] == 'open')
  EXIT_DUE    exit triggered (stop / 09:25 / late), no order live — LATCHED
  EXIT_SENT   as ENTRY_SENT, for the sell
  EXIT_OPEN   as ENTRY_OPEN, for the sell
  p['status'] == 'closed' ends it, with `exit_reason` saying how. A record
  that never filled carries `never_filled: true` and zero P&L fields.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from dataclasses import replace
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .broker import (Broker, BrokerAuth, BrokerRejected, BrokerUnavailable, BrokerUnclear,
                     parse_neo_time as _parse_neo_time,
                     OrderRequest, OrderView)
from .config import Config
from .engine import SIDES, Engine
from .journal import Journal, make_tag, parse_tag
from .kite_io import Market, QuoteError
from .live_config import MAX_LIVE_LOTS, NeoConfig
from .neo_symbols import ScripMap, ScripMasterError
from .notify import Notifier
from .pricing import Book, traded_on
from .sessions import at
from .store import CorruptBook, Store
from .ticks import price_str, round_tick

logger = logging.getLogger(__name__)

ENTRY_WAIT, ENTRY_SENT, ENTRY_OPEN, FILLED = 'ENTRY_WAIT', 'ENTRY_SENT', 'ENTRY_OPEN', 'FILLED'
EXIT_DUE, EXIT_SENT, EXIT_OPEN = 'EXIT_DUE', 'EXIT_SENT', 'EXIT_OPEN'
IN_FLIGHT = (ENTRY_SENT, ENTRY_OPEN, EXIT_SENT, EXIT_OPEN)

#: cancels we will repeat on one order before only alerting
MAX_CANCEL_ATTEMPTS = 20
#: criteria match window for an unclear send, around the intent time
CRITERIA_WINDOW_BEFORE = timedelta(seconds=60)
CRITERIA_WINDOW_AFTER = timedelta(seconds=180)
#: the owner's resume request, written by `live-resume`, applied by the loop
RESUME_FILE = 'RESUME'

ZERO_PNL = {'gross_pnl': 0.0, 'net_pnl': 0.0, 'net_ex_brokerage': 0.0, 'exit_price': None,
            'charges': {'total': 0.0}, 'held_sessions': 0, 'never_filled': True}


def _iso(now: datetime) -> str:
    return now.isoformat(timespec='seconds')


class LiveStore(Store):
    """Paper's store, plus a `pending` status (entry in flight), the kill
    record, and a validation of every live record at load: a record the
    engine cannot read is refused up front, with an alert, not a traceback
    five minutes into the session (NEO-09)."""

    def __init__(self, state_dir: Path):
        super().__init__(state_dir)
        for p in self.data.get('positions', []):
            if p.get('status') in ('open', 'pending'):
                lv = p.get('live')
                bad = (not isinstance(lv, dict) or not lv.get('neo_symbol')
                       or not lv.get('state') or not isinstance(p.get('qty'), int)
                       or not isinstance(p.get('expiry'), str)
                       or not isinstance(p.get('entry_date'), str))
                if p.get('status') == 'open' and not bad:
                    bad = (not isinstance(p.get('entry_price'), (int, float))
                           or not isinstance(p.get('stop_level'), (int, float)))
                if bad:
                    raise CorruptBook('%s: position #%s is %s but has no usable live record'
                                      % (self.path, p.get('id'), p.get('status')))

    _saved_digest: Optional[str] = None
    saves = 0
    skipped_saves = 0

    def save(self, force: bool = False) -> None:
        # Only a CHANGED book is written: the loop saves every tick, and two
        # fsyncs a second on an SD card is wear and latency for nothing (R3-37).
        digest = hashlib.sha1(json.dumps(self.data, sort_keys=True, default=str)
                              .encode('utf-8')).hexdigest()
        if not force and digest == self._saved_digest:
            self.skipped_saves += 1
            return
        super().save()
        self._saved_digest = digest
        self.saves += 1
        # fsync the DIRECTORY too: after a power cut the rename itself can be
        # lost while the journal (fsynced) survives, and a book older than its
        # journal would reuse an attempt number and tag (R2-25). POSIX only.
        try:
            fd = os.open(str(self.dir), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def owner_confirmed_held(self, pos_id: int, today: date) -> bool:
        conf = self.data.get('owner_confirmations') or {}
        return conf.get(str(pos_id)) == today.isoformat()

    def confirm_held(self, pos_id: int, today: date, by: str) -> None:
        self.data.setdefault('owner_confirmations', {})[str(pos_id)] = today.isoformat()
        self.data.setdefault('owner_actions', []).append(
            {'at': today.isoformat(), 'by': by, 'action': 'confirm_held', 'id': pos_id})

    def void_confirmation(self, pos_id: int, why: str) -> None:
        """A confirmation vouches only while every SELL sent since it reads
        terminal with zero fill (R3-29): a fill or an unreadable state voids it."""
        conf = self.data.get('owner_confirmations') or {}
        if str(pos_id) in conf:
            del conf[str(pos_id)]
            self.data.setdefault('owner_actions', []).append(
                {'by': 'engine', 'action': 'void_confirmation', 'id': pos_id, 'why': why})

    def open_positions(self) -> List[Dict[str, Any]]:
        # Quoted every poll: holdings AND entries still being worked.
        return [p for p in self.data['positions'] if p['status'] in ('open', 'pending')]

    def holding(self) -> List[Dict[str, Any]]:
        return [p for p in self.data['positions'] if p['status'] == 'open']

    def pending(self) -> List[Dict[str, Any]]:
        return [p for p in self.data['positions'] if p['status'] == 'pending']

    def in_flight(self) -> List[Dict[str, Any]]:
        return [p for p in self.open_positions()
                if p.get('live', {}).get('state') in IN_FLIGHT]

    def closed_positions(self) -> List[Dict[str, Any]]:
        """Closed AND once filled: the report's population. Entries that never
        filled are `never_filled()` (NEO-10)."""
        return [p for p in self.data['positions']
                if p['status'] == 'closed' and not p.get('never_filled')]

    def never_filled(self) -> List[Dict[str, Any]]:
        return [p for p in self.data['positions'] if p.get('never_filled')]

    def kill(self) -> Dict[str, Any]:
        k: Dict[str, Any] = self.data.setdefault(
            'kill', {'tripped': False, 'reason': None, 'at': None, 'peak': None,
                     'equity': None, 'resets': [], 'loud': False})
        return k


class LiveEngine(Engine):
    def __init__(self, cfg: Config, neo: NeoConfig, store: LiveStore, market: Optional[Market],
                 notifier: Notifier, broker: Broker, journal: Journal,
                 scrip_loader: Callable[[], ScripMap], session_expected: bool = True,
                 arm_reader: Optional[Callable[[date], Optional[date]]] = None,
                 mode: str = 'live', paper_book: Optional[Path] = None):
        # NEO brokerage (Rs 0 on the API plan) replaces the paper rate so the
        # live book's net is the live economics.
        cfg = replace(cfg, fee_rates={**cfg.fee_rates,
                                      'brokerage_per_order': neo.brokerage_per_order})
        super().__init__(cfg, store, market, notifier, session_expected=session_expected)
        self.neo = neo
        self.store: LiveStore = store
        self.broker = broker
        self.journal = journal
        self.mode = mode
        self._scrip_loader = scrip_loader
        self._scrips: Optional[ScripMap] = None
        #: re-read every minute while running: removing the ARM file disarms
        #: a running process (NEO-18); PASS is checked inside it (NEO-07)
        self._arm_reader = arm_reader
        self._arm_cache: Tuple[Optional[datetime], Optional[date]] = (None, None)
        self.paper_book = paper_book
        self.session_ok = False
        self.session_error = ''
        self._session_try_at: Optional[datetime] = None
        self.reconcile_ok = False
        self._last_reconcile: Optional[datetime] = None
        self._last_settle_try: Optional[datetime] = None
        self._rejection_streak = 0
        self._alert_at: Dict[str, datetime] = {}
        self._books: Dict[str, Book] = {}
        self.started = False
        #: False = exits-only run: a real record exists but the configuration
        #: would have refused or run another mode (R2-04)
        self.entries_enabled = True
        self._mismatch_seen: Dict[str, int] = {}
        self._scrips_thread: Optional[threading.Thread] = None
        self._scrips_result: Optional[Tuple[Optional[ScripMap], Optional[str]]] = None
        self._scrips_retry_at: Optional[datetime] = None
        self._scrip_symbols: Set[str] = set()
        self._proven_trading_day: Optional[str] = None
        self._proof_checked_at: Optional[datetime] = None
        #: an owner command run with no loop alive (R4-14): nothing may be sent
        #: and a record with an order in flight waits for the loop
        self.offline = False
        self._surplus_seen: Dict[str, int] = {}
        self._short_seen: Dict[str, int] = {}
        self.last_tick_ms = 0.0

    # ------------------------------------------------------------ utils
    def _throttled(self, key: str, now: datetime, every_sec: float, text: str) -> None:
        last = self._alert_at.get(key)
        if last is None or (now - last).total_seconds() >= every_sec:
            self._alert_at[key] = now
            self.notify.send(text)

    def _live(self, p: Dict[str, Any]) -> Dict[str, Any]:
        lv: Dict[str, Any] = p['live']
        return lv

    def _remaining(self, p: Dict[str, Any]) -> int:
        return int(p['qty']) - sum(int(f['qty']) for f in self._live(p).get('exit_fills', []))

    def _expected_long(self, sym: str) -> int:
        return sum(self._remaining(p) for p in self.store.holding()
                   if self._live(p)['neo_symbol'] == sym)

    def _set_state(self, p: Dict[str, Any], state: str, now: datetime) -> None:
        lv = self._live(p)
        if lv.get('state') != state:
            lv['state'] = state
            lv['state_at'] = _iso(now)
            self.store.event(now, 'STATE', id=p['id'], state=state)

    def scrips(self) -> ScripMap:
        if self._scrips is None:
            self._set_scrips(self._scrip_loader())
        assert self._scrips is not None
        return self._scrips

    def _set_scrips(self, smap: ScripMap) -> None:
        self._scrips = smap
        self._scrip_symbols = {r.trading_symbol for rows in smap._by_key.values() for r in rows}

    def _scrips_async_step(self, now: datetime) -> Optional[str]:
        """Load the scrip master in a WORKER so a slow download can never
        block the tick that watches the exits (R2-12). Returns an error text
        when the last attempt failed, None otherwise. The retry time is set
        from the clock AFTER the failure is observed."""
        if self._scrips is not None:
            return None
        t = self._scrips_thread
        if t is not None and t.is_alive():
            return None
        if self._scrips_result is not None:
            smap, err = self._scrips_result
            self._scrips_result = None
            self._scrips_thread = None
            if smap is not None:
                self._set_scrips(smap)
                return None
            self._scrips_retry_at = now + timedelta(seconds=60)
            return err
        if self._scrips_retry_at is not None and now < self._scrips_retry_at:
            return None

        def work() -> None:
            try:
                self._scrips_result = (self._scrip_loader(), None)
            except Exception as exc:                        # noqa: BLE001
                self._scrips_result = (None, '%s: %s' % (type(exc).__name__, exc))
        self._scrips_thread = threading.Thread(target=work, name='scrips', daemon=True)
        self._scrips_thread.start()
        return None

    def armed_today(self, today: date, now: Optional[datetime] = None) -> bool:
        if self.mode == 'rehearsal':
            return True
        if self._arm_reader is None:
            return False
        at_, until = self._arm_cache
        if now is None or at_ is None or (now - at_).total_seconds() >= 60:
            until = self._arm_reader(today)
            self._arm_cache = (now, until)
        return until is not None and today <= until

    def _known_order_nos(self) -> Set[str]:
        nos: Set[str] = set()
        for p in self.store.data['positions']:
            lv = p.get('live') or {}
            leg = lv.get('leg')
            if leg and leg.get('order_no'):
                nos.add(str(leg['order_no']))
            for f in lv.get('exit_fills', []):
                if f.get('order_no'):
                    nos.add(str(f['order_no']))
            if lv.get('entry_order_no'):
                nos.add(str(lv['entry_order_no']))
            for h in lv.get('leg_history', []):
                if h.get('order_no'):
                    nos.add(str(h['order_no']))
        for line in self.journal.lines():
            if line.get('ev') in ('PLACED', 'BOUND') and line.get('order_no'):
                nos.add(str(line['order_no']))
        return nos

    def _trip(self, now: datetime, reason: str, loud: bool = False) -> None:
        k = self.store.kill()
        if k.get('tripped'):
            if loud and not k.get('loud'):
                k['loud'] = True
                k['reason'] = '%s; %s' % (k.get('reason'), reason)
            return
        k.update({'tripped': True, 'reason': reason, 'at': _iso(now), 'loud': loud})
        self.store.event(now, 'KILL', reason=reason, loud=loud)
        self.notify.send('KILL TRIPPED: %s\nNo new entries. Exits still run. Clear with '
                         '`python -m nifty_btst live-resume --yes` when you have looked.' % reason)

    def resume(self, now: datetime, who: str = 'owner') -> None:
        k = self.store.kill()
        k.setdefault('resets', []).append({'at': _iso(now), 'by': who, 'was': k.get('reason'),
                                           'peak_before': k.get('peak'),
                                           'equity': k.get('equity')})
        k.update({'tripped': False, 'reason': None, 'at': None, 'peak': k.get('equity'),
                  'loud': False})
        self.store.event(now, 'KILL_RESET', by=who)
        self._rejection_streak = 0
        self.store.save()

    # ------------------------------------------------------------ start
    def start(self, now: datetime) -> None:
        """Before the first poll: session, journal recovery, reconcile.
        Never places or cancels anything."""
        self.started = True
        self._ensure_session(now)
        if now.time() < self.cfg.market_open:
            try:
                self.scrips()           # before 09:15 a blocking load costs nothing
            except Exception as exc:    # noqa: BLE001 - the worker retries from entries()
                self._scrips_retry_at = now
                logger.warning('scrip master not loaded at start (%s); a worker will retry', exc)
        else:
            # a mid-session restart must reach its first exit check at once:
            # the scrip master loads in the worker (R3-35)
            self._scrips_async_step(now)
        self._recover_journal(now)
        self._reconcile(now, startup=True)
        k = self.store.kill()
        if k.get('tripped'):
            self.notify.send('KILL is TRIPPED (%s at %s): no new entries until '
                             '`live-resume`. Exits still run.' % (k.get('reason'), k.get('at')))
        self.store.save()

    def _ensure_session(self, now: datetime) -> bool:
        if self.session_ok:
            return True
        if (self._session_try_at is not None
                and (now - self._session_try_at).total_seconds() < 60):
            return False
        self._session_try_at = now
        try:
            self.broker.ensure_session()
            self.session_ok, self.session_error = True, ''
            self.store.event(now, 'SESSION', ok=True)
            return True
        except BrokerAuth as exc:
            self.session_ok, self.session_error = False, str(exc)
            self.store.event(now, 'SESSION', ok=False, error=str(exc))
            holding = self.store.holding() + self.store.pending()
            every = 60.0 if holding else 600.0
            self._throttled('login', now, every,
                            'NEO LOGIN FAILED: %s\n%s' % (
                                exc, 'OPEN/PENDING POSITION(S) UNPROTECTED: %s — exit in the app '
                                'if needed.' % ', '.join('#%d %s (%s)' % (p['id'], p['symbol'],
                                                                           p['status'])
                                                         for p in holding)
                                if holding else 'No position. Retrying each minute; no entries.'))
            return False

    def _recover_journal(self, now: datetime) -> None:
        """Marry the journal to the book after a restart. A PLACED/BOUND line
        whose order number never reached the book is applied; an unresolved
        INTENT is left to `service_orders`, which searches the broker."""
        placed: Dict[str, str] = {}
        for line in self.journal.lines():
            if line.get('ev') in ('PLACED', 'BOUND') and line.get('tag'):
                placed[str(line['tag'])] = str(line['order_no'])
        legs: Dict[str, Dict[str, Any]] = {}
        for p in self.store.in_flight():
            leg = self._live(p).get('leg')
            if leg:
                legs[str(leg['tag'])] = leg
                if not leg.get('order_no') and leg['tag'] in placed:
                    leg['order_no'] = placed[leg['tag']]
                    self._set_state(p, ENTRY_OPEN if leg['kind'] == 'ENTRY' else EXIT_OPEN, now)
                    self.store.event(now, 'RECOVER', id=p['id'], tag=leg['tag'],
                                     order_no=leg['order_no'], how='journal')
        for intent in self.journal.unresolved():
            tag = str(intent.get('tag'))
            if tag in legs or intent.get('probe'):
                continue            # the leg is in the book; service_orders resolves it
            self.notify.send('JOURNAL: intent %s has no position leg in the book. It '
                             'will be searched at the broker and reported, never '
                             're-sent.' % tag)
            self._search_orphan(now, intent)

    def _search_orphan(self, now: datetime, intent: Dict[str, Any]) -> None:
        tag = str(intent.get('tag'))
        try:
            book = self.broker.order_book()
        except (BrokerUnavailable, BrokerAuth) as exc:
            self._throttled('orphan_' + tag, now, 300,
                            'JOURNAL: cannot search the book for %s (%s)' % (tag, exc))
            return
        hits = [o for o in book if o.tag == tag]
        if hits:
            self.journal.bound(now, tag, hits[0].order_no, 'orphan')
            self._trip(now, 'UNTRACKED own-tag order %s (%s %s x%d, %s)' % (
                hits[0].order_no, hits[0].side, hits[0].symbol, hits[0].qty, hits[0].status),
                loud=True)
        else:
            # Not concluded "not placed": the tag stays unresolved and reconcile
            # keeps looking for it. The owner is told.
            self._throttled('orphan_' + tag, now, 600,
                            'JOURNAL: intent %s is not in the book. It stays open; if it '
                            'ever appears the kill trips.' % tag)

    # ---------------------------------------------------------- service
    def service_orders(self, now: datetime) -> None:
        """The 1-second tick: NEO only, never Kite. Orders in flight, the
        session, the owner's resume request, the periodic reconcile."""
        if not self.started:
            self.start(now)
        self._apply_owner_requests(now)
        k = self.store.kill()
        if k.get('tripped') and k.get('loud'):
            self._throttled('kill_loud', now, 60,
                            'KILL (LOUD): %s. Check the app NOW. Nothing is traded to repair '
                            'it.' % k.get('reason'))
        if not self._ensure_session(now):
            return
        for p in self.store.in_flight():
            try:
                self._service_leg(p, now)
            except (BrokerUnavailable, BrokerAuth) as exc:
                self._throttled('svc_' + str(p['id']), now, 60,
                                '#%d: broker unreadable while an order is in flight (%s). '
                                'Not resending.' % (p['id'], exc))
                if isinstance(exc, BrokerAuth):
                    self.session_ok = False
        if (self._last_reconcile is None
                or (now - self._last_reconcile).total_seconds() >= self.neo.reconcile_sec):
            self._reconcile(now, startup=False)
        self._settle_owner_pending(now)
        self.store.save()

    def _settle_owner_pending(self, now: datetime) -> None:
        """A book-external refused while an order was live (R3-03) completes
        itself the moment that order has been read terminal: with no fill the
        owner's statement is applied; with a fill the book already moved and
        the owner is told to look again."""
        for p in self.store.data['positions']:
            lv = p.get('live') or {}
            op = lv.get('owner_pending')
            if not op or lv.get('leg'):
                continue
            lv['owner_pending'] = None
            if p['status'] == 'closed':
                self.notify.send('#%d: the order resolved by itself (%s); your book-external '
                                 'request is dropped. If you ALSO sold in the app the broker may '
                                 'be SHORT: check the app; reconcile trips the kill on its next '
                                 'read.' % (p['id'], p.get('exit_reason')))
                continue
            if p['status'] != op.get('status'):
                # the order FILLED during the cancel (R4-17): the lot is held;
                # the owner's price described a sale that may not have happened
                self.notify.send('#%d: order %s FILLED during the cancel; the record is now %s '
                                 '(qty %s) and the lot is HELD. Your book-external request is NOT '
                                 'applied. If you really sold it in the app, re-issue '
                                 '`live-book-external %d --price X --yes`; check the app for a '
                                 'SHORT.' % (p['id'], op.get('order_no'), p['status'],
                                             p.get('qty'), p['id']))
                continue
            try:
                self.book_external(int(p['id']), now, op.get('price'), by=str(op.get('by') or 'owner'))
                self.notify.send('#%d: order %s read terminal with no fill; your book-external '
                                 'request is now APPLIED (%s%s).'
                                 % (p['id'], op.get('order_no'), p.get('exit_reason'),
                                    ' at %.2f' % float(op['price']) if op.get('price') is not None
                                    else ''))
            except (ValueError, BrokerUnavailable, BrokerAuth) as exc:
                self.notify.send('#%d: book-external after the cancel REFUSED: %s. The order may '
                                 'have FILLED; check the app and re-issue the right command.'
                                 % (p['id'], exc))

    def _service_leg(self, p: Dict[str, Any], now: datetime) -> None:
        lv = self._live(p)
        leg = lv.get('leg')
        if not leg:
            # state says in flight but no leg: a torn transition; drop to the
            # resting state rather than invent an order
            self._set_state(p, ENTRY_WAIT if p['status'] == 'pending' else EXIT_DUE, now)
            return
        if not leg.get('order_no'):
            self._resolve_unknown(p, leg, now)
            return
        o = self.broker.order(leg['order_no'])
        if o is not None:
            leg.pop('absent_since', None)
            self._apply_order(p, leg, o, now)
            return
        self._known_order_absent(p, leg, now)

    # -- an order we have a NUMBER for, that the book does not show --------
    def _known_order_absent(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        """Never dropped on its day (NEO-04). The trade book is asked first;
        on a LATER day the DAY validity has ended, so POSITIVE evidence
        decides (NEO-06, R2-02/03): a row that exists, a holdings row, or a
        trade. No row at all is UNKNOWN: hold, alert, tell the owner."""
        leg.setdefault('absent_since', _iso(now))
        leg['valid_reads'] = min(3, int(leg.get('valid_reads', 0)) + 1)   # capped: no save per tick (R4-21)
        trade = self._trade_for(leg)
        if trade is not None:
            self._on_fill(p, leg, trade, now, trade.filled, partial=trade.filled < int(leg['qty']))
            return
        sent_day = datetime.fromisoformat(leg['sent_at']).date()
        if sent_day >= now.date():
            self._throttled('absent_' + leg['tag'], now, 60,
                            '#%d: order %s (%s) is ABSENT from the book. Holding its state; '
                            'nothing is re-sent. Check the app.' % (p['id'], leg['order_no'],
                                                                   leg['tag']))
            return
        verdict = self._corroborate(p, leg, now)
        if verdict == 'absent':
            self.journal.not_placed(now, leg['tag'], 'DAY order %s ended the session unfilled'
                                   % leg['order_no'])
            self.store.event(now, 'DAY_EXPIRED', id=p['id'], tag=leg['tag'],
                             order_no=leg['order_no'])
            self._leg_done(p, leg, now)
        elif verdict == 'unknown':
            self._throttled('absent_' + leg['tag'], now, 60,
                            '#%d %s: order %s from %s is absent and nothing positive settles it '
                            '(no position row / holdings / trade). HOLDING. In the app: check '
                            'whether %s is held; then `live-confirm-held %d --yes` if it is, or '
                            '`live-book-external %d --price X --yes` if you closed it.'
                            % (p['id'], p['symbol'], leg['order_no'], sent_day,
                               self._live(p)['neo_symbol'], p['id'], p['id']))

    def _trade_for(self, leg: Dict[str, Any]) -> Optional[OrderView]:
        try:
            trades = self.broker.trades()
        except (BrokerUnavailable, BrokerAuth):
            return None
        for t in trades:
            if t.filled > 0 and ((leg.get('order_no') and t.order_no == leg['order_no'])
                                 or (t.tag and t.tag == leg['tag'])):
                return t
        return None

    def _corroborate(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> str:
        """'absent' | 'filled' | 'unknown', from POSITIVE sources only: a
        trade for the leg, a position row that exists, a holdings row. For a
        leg from an EARLIER day, no row means UNKNOWN (R2-01/02/03): an entry
        is never abandoned and an exit never adopted on absence. 'filled' has
        already been booked (adopted) when this returns."""
        trade = self._trade_for(leg)
        if trade is not None:
            self._on_fill(p, leg, trade, now, trade.filled, partial=trade.filled < int(leg['qty']))
            return 'filled'
        sym = self._live(p)['neo_symbol']
        expected = self._expected_long(sym)
        if leg['kind'] == 'EXIT' and p['expiry'] < now.date().isoformat():
            # a DAY order from expiry day cannot fill any more; the exchange
            # settled the contract and settle_expired owns it
            return 'absent'
        same_day = datetime.fromisoformat(leg['sent_at']).date() == now.date()
        net, source = self._positive_long(sym, p, now)
        if net is None:
            if same_day and leg['kind'] == 'ENTRY':
                net = 0                 # a fill TODAY would have made a row
            else:
                return 'unknown'
        if leg['kind'] == 'ENTRY':
            if net == expected:
                return 'absent'
            if net > expected:
                self._adopt(p, leg, now, min(net - expected, int(leg['qty'])))
                return 'filled'
            return 'unknown'
        if net == expected:
            return 'absent'
        if net < expected and source == 'positions row':
            # the row EXISTS and shows the sell: positive evidence
            self._adopt(p, leg, now, min(expected - net, int(leg['qty'])))
            return 'filled'
        return 'unknown'

    def _sells_today(self, sym: str, now: Optional[datetime] = None) -> List[OrderView]:
        """SELLs on `sym` TODAY that COUNT against a vouched lot (R3-01): one
        with a fill, one still live, or one whose state cannot be read as
        terminal. A SELL read as terminal with zero fill — our own cancelled
        or rejected attempt — does not, so the chase can go on. A row dated
        another day is not today's (R4-22); an undated one counts."""
        rows = self.broker.order_book() + self.broker.trades()
        return [o for o in rows if o.symbol == sym and o.side == 'S'
                and (o.filled > 0 or o.live) and self._is_today(o, now)]

    def _is_today(self, o: OrderView, now: Optional[datetime]) -> bool:
        if now is None:
            return True
        t = _parse_neo_time(o.placed_at, now.tzinfo)
        return t is None or t.date() == now.date()

    def _filled_sells_today(self, sym: str, now: datetime) -> Dict[str, int]:
        """Every SELL with a fill on `sym` today, OURS OR FOREIGN, by order
        number (R4-02/03): the positive evidence a short is read from. The
        ORDER BOOK alone: it carries the fill quantity, and a trade-book
        outage must not block a stop (R2-05)."""
        out: Dict[str, int] = {}
        for o in self.broker.order_book():
            if o.symbol == sym and o.side == 'S' and o.filled > 0 and self._is_today(o, now):
                out[o.order_no] = max(out.get(o.order_no, 0), o.filled)
        return out

    def _ours_sold_today(self, sym: str, now: datetime) -> int:
        """Shares of `sym` this book records as sold today: its own exit fills,
        and a lot it booked as closed outside the engine (adopted from a
        filled SELL on the row, or `live-book-external`)."""
        today = now.date().isoformat()
        total = 0
        for p in self.store.data['positions']:
            lv = self._live(p)
            if lv.get('neo_symbol') != sym:
                continue
            fills = [int(f['qty']) for f in lv.get('exit_fills', [])
                     if str(f.get('at', ''))[:10] == today]
            if fills:
                total += sum(fills)
            elif (p.get('exit_date') == today and p['status'] == 'closed'
                  and not p.get('never_filled')):
                total += int(p.get('qty') or 0)
        return total

    def _positive_long(self, sym: str, p: Dict[str, Any], now: datetime
                       ) -> Tuple[Optional[int], str]:
        """What the broker POSITIVELY says we hold of `sym`: (shares, source)
        or (None, why) when no source exists. Raises BrokerUnavailable /
        BrokerAuth. A row with an impossible quantity trips the kill."""
        row = self.broker.position_row(sym, self.neo.product)
        if row is not None:
            if not self._qty_sane(p, row.net_qty, now):
                return None, 'position row with an impossible quantity'
            return row.net_qty, 'positions row'
        holdings = self.broker.holdings()
        if holdings is not None:
            hits = [h for h in holdings if h.symbol == sym]
            if hits:
                net = sum(h.net_qty for h in hits)
                if not self._qty_sane(p, net, now):
                    return None, 'holdings row with an impossible quantity'
                return net, 'holdings'
        if self.store.owner_confirmed_held(int(p['id']), now.date()):
            return self._remaining(p), 'owner confirmation'
        return None, 'no position row, no holdings row, no owner confirmation for today'

    def _qty_sane(self, p: Dict[str, Any], net: int, now: datetime) -> bool:
        """Units check (measurement item 6): a quantity that is not a multiple
        of the lot, or that reads as LOTS where shares are expected (or the
        reverse), is a configuration error, never a flat position."""
        lot = int(p.get('lot_size') or 0)
        if lot <= 0 or net == 0:
            return True
        expected = self._remaining(p)
        if expected and abs(net) != expected and abs(net) * lot == expected:
            self._trip(now, 'UNIT MISMATCH: %s shows %d where %d shares are expected: the '
                       'report is in LOTS (set neo.position_qty_unit)'
                       % (self._live(p)['neo_symbol'], net, expected), loud=True)
            return False
        if net % lot != 0:
            self._trip(now, 'IMPOSSIBLE QUANTITY: %s shows %d, not a multiple of lot %d (check '
                       'neo.position_qty_unit)' % (self._live(p)['neo_symbol'], net, lot), loud=True)
            return False
        return True

    def _adopt(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime, qty: int) -> None:
        """Book a fill the order book never showed but positions prove. The
        price is the leg's limit, flagged — never invented from a quote."""
        view = OrderView(order_no=str(leg.get('order_no') or 'ADOPTED-' + leg['tag']),
                         status='complete', symbol=self._live(p)['neo_symbol'],
                         side='B' if leg['kind'] == 'ENTRY' else 'S', qty=int(leg['qty']),
                         filled=qty, avg_price=0.0, price=float(leg['limit']), rej_reason='',
                         tag=leg['tag'], placed_at=leg['sent_at'], product=self.neo.product)
        self.notify.send('#%d: %s %s was FILLED (%d) though the book never showed it: '
                         'positions prove it. Booked at the limit %s, price FLAGGED. Check '
                         'the app.' % (p['id'], leg['kind'], leg['tag'], qty, leg['limit']))
        self.journal.bound(now, leg['tag'], view.order_no, 'adopted')
        self._on_fill(p, leg, view, now, qty, partial=qty < int(leg['qty']))

    # -- a send with NO order number ---------------------------------------
    def _resolve_unknown(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        """Search by tag, then by criteria. Absence concludes NOTHING until
        `unknown_grace_sec` has passed with >= 2 positively valid reads AND
        positions corroborate (NEO-01, NEO-02)."""
        book = self.broker.order_book()           # BrokerUnavailable -> caller: HALT
        hit, how = None, ''
        for o in book:
            if o.tag and o.tag == leg['tag']:
                hit, how = o, 'tag'
                break
        if hit is None:
            known = self._known_order_nos()
            sent = datetime.fromisoformat(leg['sent_at'])
            side = 'B' if leg['kind'] == 'ENTRY' else 'S'
            for o in book:
                if (o.symbol != self._live(p)['neo_symbol'] or o.side != side
                        or o.qty != int(leg['qty']) or o.order_no in known):
                    continue
                t = _parse_neo_time(o.placed_at, sent.tzinfo)
                if t is None or not (sent - CRITERIA_WINDOW_BEFORE <= t <= sent + CRITERIA_WINDOW_AFTER):
                    continue
                hit, how = o, 'criteria'
                break
        if hit is not None:
            leg['order_no'] = hit.order_no
            leg['bound_by'] = how
            self.journal.bound(now, leg['tag'], hit.order_no, how)
            self._set_state(p, ENTRY_OPEN if leg['kind'] == 'ENTRY' else EXIT_OPEN, now)
            self.store.event(now, 'BOUND', id=p['id'], tag=leg['tag'], order_no=hit.order_no, how=how)
            self.notify.send('#%d: unclear send %s found in the book as %s (by %s), status %s'
                             % (p['id'], leg['tag'], hit.order_no, how, hit.status))
            self._apply_order(p, leg, hit, now)
            return
        leg['valid_reads'] = min(3, int(leg.get('valid_reads', 0)) + 1)   # capped: no save per tick (R4-21)
        leg.setdefault('unknown_first_at', _iso(now))
        age = (now - datetime.fromisoformat(leg['sent_at'])).total_seconds()
        if age < self.neo.unknown_grace_sec or leg['valid_reads'] < 2:
            self._throttled('unknown_' + leg['tag'], now, 60,
                            '#%d: %s is UNKNOWN (sent %.0fs ago, not in the book). Nothing is '
                            're-sent for %.0fs. Check the app.' % (p['id'], leg['tag'], age,
                                                                    self.neo.unknown_grace_sec))
            return
        verdict = self._corroborate(p, leg, now)
        if verdict == 'absent':
            if leg['kind'] == 'EXIT' and self.store.owner_confirmed_held(int(p['id']), now.date()):
                # a SELL sent on the owner's word whose state could not be read
                # voids the word: the owner must look again (R3-29)
                self.store.void_confirmation(int(p['id']), 'unreadable SELL %s' % leg['tag'])
                self.notify.send('#%d: SELL %s could not be read for %.0fs; your confirmation is '
                                 'VOID. Check the app, then `live-confirm-held %d --yes` again if '
                                 'the lot is still held.' % (p['id'], leg['tag'], age, p['id']))
            self.journal.not_placed(now, leg['tag'], 'absent after %d reads over %.0fs; positions '
                                   'agree' % (leg['valid_reads'], age))
            self.store.event(now, 'NOT_PLACED', id=p['id'], tag=leg['tag'])
            self.notify.send('#%d: %s was NOT placed (absent %.0fs, positions agree). May be '
                             're-sent as a new attempt.' % (p['id'], leg['tag'], age))
            self._leg_done(p, leg, now)
        elif verdict == 'unknown':
            self._throttled('unknown_' + leg['tag'], now, 60,
                            '#%d: %s still UNKNOWN after %.0fs and positions do not settle '
                            'it. Holding. Check the app.' % (p['id'], leg['tag'], age))

    def _leg_done(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        """No order live for this position any more; back to its resting state."""
        lv = self._live(p)
        lv['leg'] = None
        lv.setdefault('leg_history', []).append(leg)
        if leg['kind'] == 'ENTRY':
            self._set_state(p, ENTRY_WAIT, now)
        else:
            self._set_state(p, EXIT_DUE, now)

    def _apply_order(self, p: Dict[str, Any], leg: Dict[str, Any], o: OrderView,
                     now: datetime) -> None:
        leg['last_status'] = o.status
        if not o.known_status:
            self._throttled('status_' + o.order_no, now, 600,
                            '#%d: order %s has an UNKNOWN status %r. Treated as live (no resend); '
                            'if it is really final, cancel/sell in the app.'
                            % (p['id'], o.order_no, o.status))
        if o.status == 'complete' or (o.filled >= int(leg['qty']) and o.filled > 0):
            self._on_fill(p, leg, o, now, o.filled or int(leg['qty']))
        elif o.status == 'rejected':
            self._on_rejected(p, leg, now, o.rej_reason or 'rejected (no reason given)',
                              placed=True)
        elif o.status == 'cancelled':
            if o.filled > 0:
                self._on_fill(p, leg, o, now, o.filled, partial=True)
            else:
                if not leg.get('cancel_requested_at'):
                    self.notify.send('#%d: %s %s was cancelled by the exchange/broker, unfilled.'
                                     % (p['id'], leg['tag'], o.order_no))
                self.store.event(now, 'CANCELLED', id=p['id'], tag=leg['tag'],
                                 order_no=o.order_no, filled=0)
                self._leg_done(p, leg, now)
        else:
            self._maybe_cancel(p, leg, now)

    def _fill_qty_sane(self, p: Dict[str, Any], leg: Dict[str, Any], o: OrderView,
                       now: datetime, filled: Optional[int] = None) -> bool:
        """Every fill quantity gets the unit check, from the order book and
        from the trade book alike (R3-11, R4-05): not a whole multiple of the
        lot, or larger than what we sent, is never booked and never becomes
        a SELL quantity. Kill, loud; the leg is QUARANTINED until the owner
        states the outcome with `live-book-external` (the way out)."""
        qty = o.filled if filled is None else int(filled)
        lot = int(p.get('lot_size') or 0)
        bad = (lot > 0 and qty % lot != 0) or qty > int(leg['qty'])
        if not bad:
            return True
        if not leg.get('quarantined'):
            leg['quarantined'] = _iso(now)
            self._trip(now, 'ORDER QUANTITY UNIT: order %s reports a fill of %d for a %d-share '
                       'order (lot %d). Not booked. The order/trade book may report in LOTS '
                       '(unmeasured: the Stage 3 probe prints it). Check the app.'
                       % (o.order_no, qty, int(leg['qty']), lot), loud=True)
        self._throttled('quarantine_' + leg['tag'], now, 60,
                        '#%d: order %s QUARANTINED (fill %d vs sent %d). No order goes out on this '
                        'position; its stop is watched and alerted only. In the app: if the lot '
                        'is held and you sold it, `live-book-external %d --price X --yes`; if '
                        'it never filled, `live-book-external %d --not-filled --yes`.'
                        % (p['id'], o.order_no, qty, int(leg['qty']), p['id'], p['id']))
        return False

    def _maybe_cancel(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        sent = datetime.fromisoformat(leg['sent_at'])
        age = (now - sent).total_seconds()
        lv = self._live(p)
        if leg['kind'] == 'ENTRY':
            due = age >= self.neo.entry_timeout_sec or now.time() > self.cfg.entry_end
        else:
            # In slow mode a SELL is cancelled only when the next send is due,
            # so an order is working almost all the time (R2-10).
            slow = int(lv.get('exit_attempts', 0)) >= self.neo.exit_chase_fast_max
            due = age >= (self.neo.exit_retry_slow_sec if slow else self.neo.exit_chase_sec)
        if not due:
            return
        last = leg.get('cancel_requested_at')
        n = int(leg.get('cancel_attempts', 0))
        wait = self.neo.unknown_recheck_sec if n < MAX_CANCEL_ATTEMPTS else 60.0
        if last and (now - datetime.fromisoformat(last)).total_seconds() < wait:
            return                               # give the last cancel time to show
        if n >= MAX_CANCEL_ATTEMPTS:
            # never give up: once a minute from here, and say so (R2-09)
            self._throttled('cancel_' + leg['tag'], now, 60,
                            '#%d: order %s has not cancelled after %d attempts; retrying once a '
                            'minute. Check the app.' % (p['id'], leg['order_no'], n))
        leg['cancel_attempts'] = n + 1
        leg['cancel_requested_at'] = _iso(now)
        try:
            self.broker.cancel(leg['order_no'])
            self.store.event(now, 'CANCEL', id=p['id'], order_no=leg['order_no'], ok=True)
        except BrokerRejected as exc:
            # 1006 "already cancelled or executed": the next read decides.
            self.store.event(now, 'CANCEL', id=p['id'], order_no=leg['order_no'],
                             ok=False, code=exc.code, msg=exc.msg)
        except BrokerUnclear as exc:
            self.store.event(now, 'CANCEL', id=p['id'], order_no=leg['order_no'],
                             ok=None, error=str(exc))

    # ------------------------------------------------------------ fills
    def _on_fill(self, p: Dict[str, Any], leg: Dict[str, Any], o: OrderView, now: datetime,
                 filled: int, partial: bool = False) -> None:
        lv = self._live(p)
        if not self._fill_qty_sane(p, leg, o, now, filled):
            return                      # quarantined: never booked, never resent (R4-05)
        price = o.avg_price if o.avg_price > 0 else float(leg['limit'])
        if o.avg_price <= 0:
            lv['avg_price_missing'] = True
            logger.error('#%d: fill with no avgPrc; using the limit %s', p['id'], leg['limit'])
        lv['leg'] = None
        lv.setdefault('leg_history', []).append(dict(leg, order_no=o.order_no))
        if leg['kind'] == 'ENTRY':
            if partial:
                self.notify.send('#%d: entry PARTIAL: %d of %d filled. Holding %d.'
                                 % (p['id'], filled, leg['qty'], filled))
            fill = round(price, 2)
            # A DAY order fills on the day it was sent, never later: a fill
            # adopted the next morning still belongs to yesterday, or the
            # 09:25 exit would never come due (found by the NEO-06 test).
            sent_day = datetime.fromisoformat(leg['sent_at']).date().isoformat()
            p.update({
                'status': 'open', 'qty': filled, 'entry_price': fill,
                'entry_time': _iso(now) if sent_day == now.date().isoformat() else leg['sent_at'],
                'entry_date': sent_day,
                'stop_level': round(fill * self.cfg.stop_frac, 2),
                'slip_vs_trigger': round(fill - p['trigger'], 2),
                'mfe_ltp': p.get('mfe_ltp') or fill, 'mae_ltp': p.get('mae_ltp') or fill,
            })
            lv['entry_order_no'] = o.order_no
            lv['entry_tag'] = leg['tag']
            p['exit_rule_time'] = str(self.cfg.next_day_exit)      # frozen on the record (R4-04)
            self._set_state(p, FILLED, now)
            self._rejection_streak = 0
            self.store.event(now, 'ENTRY', id=p['id'], side=p['side'], symbol=p['symbol'],
                             fill=fill, qty=filled, order_no=o.order_no, tag=leg['tag'],
                             limit=leg['limit'], trigger=p['trigger'])
            self.notify.send('ENTRY #%d BUY %s x%d @ %.2f FILLED (order %s)\nlimit %s, '
                             'trigger %.2f, slip %+.2f\nstop %.2f | exit 09:25 next session | '
                             '%s DTE %d' % (p['id'], p['symbol'], filled, fill, o.order_no,
                                            leg['limit'], p['trigger'], p['slip_vs_trigger'],
                                            p['stop_level'], p['cls'], p['dte_sessions']))
        else:
            lv.setdefault('exit_fills', []).append({'qty': filled, 'price': price,
                                                    'order_no': o.order_no, 'tag': leg['tag'],
                                                    'at': _iso(now)})
            self.store.event(now, 'EXIT_FILL', id=p['id'], qty=filled, price=price,
                             order_no=o.order_no, partial=partial)
            remaining = self._remaining(p)
            if remaining > 0:
                self._set_state(p, EXIT_DUE, now)
                self.notify.send('#%d: exit PARTIAL: %d filled @ %.2f, %d still held. Chasing.'
                                 % (p['id'], filled, price, remaining))
                return
            fills = lv['exit_fills']
            tot = sum(int(f['qty']) for f in fills)
            avg = sum(float(f['price']) * int(f['qty']) for f in fills) / tot
            orders = 1 + len(fills)
            self._close(p, now, avg, lv.get('exit_reason', 'exit'), book=self._books.get(
                self.key(p['symbol'])), orders=orders)
            lv['state'] = 'CLOSED'

    def _on_rejected(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime,
                     reason: str, placed: bool, code: str = '') -> None:
        lv = self._live(p)
        lv['leg'] = None
        lv.setdefault('leg_history', []).append(dict(leg, rejected=reason))
        try:
            retryable = int(code) in self.neo.retry_stcodes
        except (TypeError, ValueError):
            retryable = False
        if retryable:
            # a wrong-IP refusal or a throttle: definitely NOT placed, retried
            # at the next poll with an alert, no streak, no side lost (R3-09)
            self.store.event(now, 'REFUSED', id=p['id'], tag=leg['tag'], reason=reason)
            key = 'entry_attempts' if leg['kind'] == 'ENTRY' else 'exit_attempts'
            lv[key] = max(0, int(lv.get(key, 0)) - 1)     # not an attempt (R4-10)
            self._throttled('refused_code_%d' % p['id'], now, 60,
                            '#%d %s %s REFUSED by the gateway (%s). Retrying at the next poll. '
                            'If this is 100008, the box IP is not whitelisted.'
                            % (p['id'], leg['kind'], leg['tag'], reason))
            self._leg_done(p, leg, now)
            return
        self._rejection_streak += 1
        self.store.event(now, 'REJECTED', id=p['id'], tag=leg['tag'], reason=reason,
                         placed=placed)
        if leg['kind'] == 'ENTRY':
            # One rejection ends the side for the day: a reason we do not
            # understand is not something to retry into (flow review F3).
            p.update(dict(ZERO_PNL, status='closed', exit_reason='entry_rejected',
                          exit_date=now.date().isoformat(), exit_time=_iso(now),
                          rej_reason=reason))
            lv['state'] = 'REJECTED'
            self.notify.send('ENTRY #%d %s REJECTED: %s\nSide %s is done for today.'
                             % (p['id'], p['symbol'], reason, p['side']))
        else:
            lv['exit_rejections'] = int(lv.get('exit_rejections', 0)) + 1
            self._set_state(p, EXIT_DUE, now)
            self.notify.send('EXIT #%d %s REJECTED: %s\nRetrying (%s). If this persists, '
                             'EXIT IN THE APP.' % (p['id'], p['symbol'], reason,
                                                   'fast' if lv.get('exit_attempts', 0)
                                                   < self.neo.exit_chase_fast_max
                                                   else 'once a minute'))
        if self._rejection_streak >= 2:
            self._trip(now, '2 consecutive rejections: ' + reason)

    # ------------------------------------------------------------- send
    def _send(self, p: Dict[str, Any], kind: str, side: str, qty: int, price: float,
              now: datetime) -> bool:
        lv = self._live(p)
        total = self.journal.sends_today(now.date())
        if total >= self.neo.max_orders_per_day and not self.store.kill().get('tripped'):
            # The brake: no NEW entries past the ceiling. Exits are never
            # stopped by it (NEO-21); they keep their minimum interval.
            self._trip(now, 'order ceiling %d reached' % total)
        if kind == 'ENTRY' and total >= self.neo.max_orders_per_day:
            return False
        attempts_key = 'entry_attempts' if kind == 'ENTRY' else 'exit_attempts'
        attempt = int(lv.get(attempts_key, 0)) + 1
        lv[attempts_key] = attempt
        tag = make_tag(now.date(), int(p['id']), kind, attempt)
        pstr = price_str(price, lv['tick'], 'up' if side == 'B' else 'down')
        leg = {'kind': kind, 'tag': tag, 'attempt': attempt, 'order_no': None, 'limit': pstr,
               'qty': int(qty), 'sent_at': _iso(now), 'cancel_requested_at': None,
               'valid_reads': 0}
        lv['leg'] = leg
        lv['last_send_at'] = _iso(now)
        self._set_state(p, ENTRY_SENT if kind == 'ENTRY' else EXIT_SENT, now)
        self.store.save()                       # the book knows BEFORE the wire
        self.journal.intent(now, tag, kind, int(p['id']), lv['neo_symbol'], side, int(qty),
                            pstr, attempt=attempt)
        req = OrderRequest(symbol=lv['neo_symbol'], token=lv['neo_token'], side=side,
                           qty=int(qty), price=pstr, tag=tag)
        try:
            order_no = self.broker.place(req)
        except BrokerRejected as exc:
            self.journal.not_placed(now, tag, '%s %s' % (exc.code, exc.msg))
            self._on_rejected(p, leg, now, 'stCode %s: %s' % (exc.code, exc.msg), placed=False,
                              code=exc.code)
            self.store.save()
            return False
        except BrokerUnclear as exc:
            self.store.event(now, 'UNCLEAR', id=p['id'], tag=tag, error=str(exc))
            self.notify.send('#%d: %s send UNCLEAR (%s). Searching the book before anything '
                             'else.' % (p['id'], tag, exc))
            self.store.save()
            return False
        except BrokerAuth as exc:
            self.session_ok, self.session_error = False, str(exc)
            self.store.event(now, 'UNCLEAR', id=p['id'], tag=tag, error=str(exc))
            self.notify.send('#%d: %s send UNCLEAR, session dead (%s).' % (p['id'], tag, exc))
            self.store.save()
            return False
        leg['order_no'] = order_no
        self.journal.placed(now, tag, order_no)
        self._set_state(p, ENTRY_OPEN if kind == 'ENTRY' else EXIT_OPEN, now)
        self.store.event(now, 'SENT', id=p['id'], leg=kind, tag=tag, order_no=order_no,
                         side=side, qty=int(qty), limit=pstr)
        self.store.save()
        return True

    # ------------------------------------------------------------- step
    def step(self, now: datetime) -> None:
        cfg, t, today = self.cfg, now.time(), now.date()
        if self.market is None:
            # No Kite token yet (NEO-08): no quotes, but the loop runs — the
            # exits that are due by TIME are latched and the owner is told.
            self._outage_watch(now, 'no Kite token')
            return
        super().step(now)
        if self.closed_today == today and self.store.holding():
            self._closed_day_watch(now)
        elif self.fail_streak > 0 and cfg.market_open <= t <= cfg.market_close:
            self._outage_watch(now, 'quotes failing (%d polls)' % self.fail_streak)
        if (self.fail_streak == 0 and cfg.market_open <= t <= cfg.market_close
                and self.closed_today != today):
            day = self.store.day(today)
            self.store.heartbeat(now, fail_streak=0, open=len(self.store.holding()),
                                 pending=len(self.store.pending()),
                                 marked=bool(day.get('mark_done')), mode=self.mode,
                                 session=self.session_ok,
                                 kill=bool(self.store.kill().get('tripped')))
        # Engine.settle_expired runs once per day per process; an unreadable
        # broker then waits for the next restart (NEO-32). Retry each minute.
        if any(p['expiry'] < today.isoformat() for p in self.store.holding()):
            if (self._last_settle_try is None
                    or (now - self._last_settle_try).total_seconds() >= 60):
                self._last_settle_try = now
                self.settle_expired(now)
                self.store.save()

    def _outage_watch(self, now: datetime, why: str) -> None:
        """No usable quotes: latch the time-based exits so they fire on the
        first book, and say so every minute per holding (NEO-15)."""
        cfg, today, t = self.cfg, now.date(), now.time()
        for p in self.store.holding():
            lv = self._live(p)
            exit_time = self._exit_time(p)
            exit_dt = at(today, exit_time)
            overnight = p['entry_date'] < today.isoformat()
            # A time exit is latched only on a day SHOWN to be trading (R3-10):
            # with quotes out, positive evidence is a book that traded today
            # earlier in this session, or NEO order/trade activity today. With
            # none, alert rather than latch; when the day proves itself the
            # 09:25 rule applies from then on, never before.
            trading = (self.session_expected and self.closed_today != today
                       and self._day_proven_trading(now))
            if (trading and overnight and t >= exit_time
                    and lv.get('exit_reason') is None):
                late = (now - exit_dt).total_seconds() > cfg.next_day_exit_grace_sec
                lv['exit_reason'] = 'next_day_late' if late else 'next_day_0925'
                lv['exit_triggered_at'] = _iso(now)
                p['exit_pending'] = lv['exit_reason']
                self._set_state(p, EXIT_DUE, now)
                self.store.event(now, 'EXIT_DUE', id=p['id'], reason=lv['exit_reason'],
                                 ltp=None, why=why)
            due = lv.get('exit_reason')
            if (due is None and overnight and t >= exit_time and self.session_expected
                    and self.closed_today != today and not trading):
                self._throttled('noproof_%d' % p['id'], now, 60,
                                '#%d %s: 09:25 has passed but nothing has traded today that this '
                                'engine can see (quotes out, no NEO activity). Not latching the '
                                'exit; an unlisted holiday? It applies from 09:25 the moment the '
                                'day proves itself.' % (p['id'], p['symbol']))
            self._throttled('outage_%d' % p['id'], now, 60,
                            'NO QUOTES (%s): #%d %s is %s. Stop not watched; %s. Exit in the '
                            'app if needed.' % (why, p['id'], p['symbol'],
                                                'DUE TO EXIT (%s)' % due if due else 'open',
                                                'the exit fires on the first usable book'
                                                if due else 'no exit is due yet'))
        self.store.save()

    def _exit_time(self, p: Dict[str, Any]) -> dtime:
        """The next-session exit time FROZEN on the record at its fill (R4-04);
        the configured one for records that predate the field."""
        raw = p.get('exit_rule_time')
        if raw:
            try:
                return datetime.strptime(str(raw), '%H:%M:%S').time()
            except ValueError:
                pass
        return self.cfg.next_day_exit

    def _day_proven_trading(self, now: datetime) -> bool:
        """Positive evidence that today is a session: a book traded today was
        seen in this process, or the broker shows an order/trade dated today.
        Cached once true."""
        today = now.date().isoformat()
        if self._proven_trading_day == today:
            return True
        # a negative answer is re-checked once a minute, not every 1 s tick:
        # two NEO reads per holding per second on an outage day is load on
        # exactly the day the broker may be slow too
        last = self._proof_checked_at
        if last is not None and (now - last).total_seconds() < 60:
            return False
        self._proof_checked_at = now
        try:
            rows = self.broker.order_book() + self.broker.trades()
        except (BrokerUnavailable, BrokerAuth):
            return False
        for o in rows:
            t = _parse_neo_time(o.placed_at, now.tzinfo)
            if t is not None and t.date().isoformat() == today:
                self._proven_trading_day = today
                return True
        return False

    def _closed_day_watch(self, now: datetime) -> None:
        """The mark found the market closed, but a position is open: the
        calendar has been wrong before (NEO-26). Keep quoting the holdings
        and running the exits; say so every ten minutes."""
        self._throttled('closed_watch', now, 600,
                        'Day marked CLOSED but %d position(s) are open. Still quoting and '
                        'watching them.' % len(self.store.holding()))
        keys = [self.key(p['symbol']) for p in self.store.holding()
                if p['expiry'] >= now.date().isoformat()]
        try:
            books = self.market.quote(keys) if keys else {}
        except QuoteError:
            return
        self.manage_open(now, books)
        self.store.save()

    # ------------------------------------------------------------- mark
    def mark(self, now: datetime, day: Dict[str, Any],
             polled: Optional[Dict[str, Book]] = None) -> None:
        self._note_traded_books(now, polled)
        super().mark(now, day, polled)
        if not day.get('mark_done'):
            return
        if self.closed_today != now.date():
            # a completed mark means the underlying and an option traded
            # today: the day is SHOWN trading (R3-10)
            self._proven_trading_day = now.date().isoformat()
        self._map_marks(now, day)
        self._compare_with_paper(now, day)

    def _note_traded_books(self, now: datetime, books: Optional[Dict[str, Book]]) -> None:
        """Any polled book that traded today is positive evidence that the day
        is a session (R3-10), whatever else fails afterwards."""
        today = now.date()
        if self._proven_trading_day == today.isoformat() or not books:
            return
        if any(traded_on(b, today) for b in books.values()):
            self._proven_trading_day = today.isoformat()

    def _map_marks(self, now: datetime, day: Dict[str, Any]) -> None:
        """Attach the NEO row to each mark. A row that is absent or disagrees
        on lot size is PERMANENT for the day (side disabled, alerted once); a
        scrip-master load failure is transient and is retried every minute
        from `entries()` until the side triggers."""
        for side in SIDES:
            m = day['marks'].get(side)
            if not m or m.get('neo') or m.get('neo_error'):
                continue
            retry_at = m.get('neo_retry_at')
            if retry_at and now < datetime.fromisoformat(retry_at):
                continue
            if self._scrips is None:
                err = self._scrips_async_step(now)
                if err:
                    self._throttled('scrips', now, 600,
                                    'NEO scrip master unavailable (%s). Retrying every minute in '
                                    'the background; no live entry until it loads.' % err)
                if self._scrips is None:
                    continue                            # the worker is loading it
            try:
                row = self.scrips().find_for_mark(m)
            except ScripMasterError as exc:
                m['neo_error'] = str(exc)
                self._alert_once(day, 'neo_map_' + side,
                                 '%s %s has NO NEO MAPPING (%s). That side cannot trade live '
                                 'today.' % (side, m['symbol'], exc))
                continue
            m.pop('neo_retry_at', None)
            if row.lot_size != int(m['lot_size']):
                m['neo_error'] = 'lot size NEO %d != Kite %d' % (row.lot_size, m['lot_size'])
                self._alert_once(day, 'neo_lot_' + side,
                                 '%s %s: LOT SIZE MISMATCH NEO %d vs Kite %d. Side disabled.'
                                 % (side, m['symbol'], row.lot_size, m['lot_size']))
                continue
            m['neo'] = {'symbol': row.trading_symbol, 'token': row.token,
                        'lot_size': row.lot_size, 'tick': row.tick_size}
            self.store.event(now, 'NEO_MAP', side=side, kite=m['symbol'],
                             neo=row.trading_symbol, token=row.token)

    def _compare_with_paper(self, now: datetime, day: Dict[str, Any]) -> None:
        """Report-only: the two books mark independently; a different pick
        means the live trade will not be paper's twin."""
        if not self.paper_book or not self.paper_book.exists():
            return
        try:
            with open(self.paper_book, encoding='utf-8') as f:
                pday = json.load(f).get('days', {}).get(now.date().isoformat())
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(pday, dict) or not pday.get('mark_done'):
            return
        for side in SIDES:
            ours, theirs = day['marks'].get(side), (pday.get('marks') or {}).get(side)
            if ours and theirs and ours['symbol'] != theirs['symbol']:
                self._alert_once(day, 'paper_diff_' + side,
                                 '%s mark differs from PAPER: live %s vs paper %s. The live '
                                 'trade will not be the paper twin.'
                                 % (side, ours['symbol'], theirs['symbol']))

    # ---------------------------------------------------------- entries
    def _pending_for(self, side: str, today: date) -> Optional[Dict[str, Any]]:
        for p in self.store.pending():
            if p['side'] == side and p['entry_date'] == today.isoformat():
                return p
        return None

    def entries(self, now: datetime, day: Dict[str, Any], books: Dict[str, Book]) -> None:
        cfg, today = self.cfg, now.date()
        self._map_marks(now, day)
        for side in SIDES:
            m = day['marks'].get(side)
            if not m:
                continue
            b = books.get(self.key(m['symbol']))
            pend = self._pending_for(side, today)
            if pend is not None:
                if self._live(pend)['state'] == ENTRY_WAIT and b is not None and traded_on(b, today):
                    self._try_entry(pend, now, b)
                continue
            if day['fired'].get(side):
                continue
            if not traded_on(b, today) or b is None or b.ltp < m['trigger']:
                continue
            if 'trigger_seen_at' not in m:
                m['trigger_seen_at'] = _iso(now)
                m['trigger_seen_ltp'] = b.ltp
            if not m.get('neo'):
                self._alert_once(day, 'no_map_trigger_' + side,
                                 '%s TRIGGERED at %.2f but has no NEO mapping: NOT traded live.'
                                 % (side, b.ltp))
                day['fired'][side] = True
                continue
            if b.bid <= 0 or b.ask <= 0 or b.ask < b.bid:
                m['entry_defers'] = m.get('entry_defers', 0) + 1
                self.store.event(now, 'DEFER', what='entry', side=side, symbol=m['symbol'],
                                 why='no two-sided book (bid %.2f / ask %.2f)' % (b.bid, b.ask))
                continue
            qty = int(m['neo']['lot_size']) * cfg.lots
            ib = books.get(cfg.index_quote_symbol)
            pos = self.store.add_position({
                'status': 'pending', 'side': side, 'symbol': m['symbol'], 'token': m['token'],
                'strike': m['strike'], 'expiry': m['expiry'], 'lot_size': m['lot_size'],
                'lots': cfg.lots, 'qty': qty, 'entry_date': today.isoformat(),
                'entry_time': None, 'mark_price': m['mark_price'], 'trigger': m['trigger'],
                'trigger_ltp': b.ltp, 'trigger_bid': b.bid, 'trigger_ask': b.ask,
                'trigger_at': _iso(now), 'entry_price': None, 'stop_level': None,
                'cls': m['cls'], 'dte_sessions': m['dte_sessions'], 'dte_days': m['dte_days'],
                'spot_at_entry': (ib.ltp if ib is not None else None),
                'mfe_ltp': b.ltp, 'mae_ltp': b.ltp, 'exit_defers': 0,
                'live': {'state': ENTRY_WAIT, 'mode': self.mode,
                         'neo_symbol': m['neo']['symbol'], 'neo_token': m['neo']['token'],
                         'tick': m['neo']['tick'], 'leg': None, 'entry_attempts': 0,
                         'exit_attempts': 0, 'exit_fills': []},
            })
            day['fired'][side] = True
            self.store.event(now, 'TRIGGER', id=pos['id'], side=side, symbol=m['symbol'],
                             ltp=b.ltp, bid=b.bid, ask=b.ask, trigger=m['trigger'])
            self._try_entry(pos, now, b)

    def _entry_blocked(self, p: Dict[str, Any], now: datetime) -> Optional[str]:
        """A reason NOT to send an entry right now, or None. These are waits,
        not abandonments: the side keeps watching until entry_end."""
        if self.store.kill().get('tripped'):
            return 'kill tripped'
        if not self.armed_today(now.date(), now):
            return 'not armed for entries today (ARM file + PASS)'
        if not self.entries_enabled:
            return 'exits-only run (a real record exists under a refusing configuration)'
        if not self.session_ok:
            return 'no NEO session'
        if not self.reconcile_ok:
            return 'startup reconcile not clean'
        if int(self.cfg.lots) > MAX_LIVE_LOTS:
            return 'lots %d > MAX_LIVE_LOTS %d' % (self.cfg.lots, MAX_LIVE_LOTS)
        return None

    def _abandon(self, p: Dict[str, Any], now: datetime, reason: str, text: str) -> None:
        """Only for a position with NOTHING in flight: a leg whose fill status
        is unknown is never abandoned (NEO-06)."""
        assert not self._live(p).get('leg'), 'abandon with a leg in flight'
        p.update(dict(ZERO_PNL, status='closed', exit_reason=reason,
                      exit_date=now.date().isoformat(), exit_time=_iso(now)))
        self._live(p)['state'] = 'ABANDONED'
        self.store.event(now, 'ABANDON', id=p['id'], reason=reason)
        self.notify.send('#%d %s %s: %s' % (p['id'], p['side'], p['symbol'], text))

    def _try_entry(self, p: Dict[str, Any], now: datetime, b: Book) -> None:
        cfg, neo, lv = self.cfg, self.neo, self._live(p)
        if now.time() > cfg.entry_end:
            self._abandon(p, now, 'entry_abandoned', 'not filled by %s; no entry today'
                          % cfg.entry_end)
            return
        blocked = self._entry_blocked(p, now)
        if blocked == 'not armed for entries today (ARM file + PASS)':
            self._arm_cache = (None, None)              # one failed read is not "unarmed" (R4-13)
            blocked = self._entry_blocked(p, now)
        if blocked:
            if blocked in ('kill tripped', 'not armed for entries today (ARM file + PASS)'):
                # Owner-reviewable default (R3-24): a side that triggered while
                # the owner's switches said no is ABANDONED now, so that arming
                # at 14:30 never buys a 10:00 trigger the paper twin took then.
                self._abandon(p, now, 'entry_abandoned',
                              'triggered while entries were blocked (%s): abandoned now, never '
                              'bought later (R3-24)' % blocked)
                return
            self._throttled('blocked_%d' % p['id'], now, 600,
                            '#%d %s triggered but entry is BLOCKED: %s' % (p['id'], p['side'], blocked))
            return
        if b.bid <= 0 or b.ask <= 0 or b.ask < b.bid:
            self.store.event(now, 'DEFER', what='entry', id=p['id'],
                             why='no two-sided book (bid %.2f / ask %.2f)' % (b.bid, b.ask))
            return
        tick = lv['tick']
        cap = round_tick(float(p['trigger']) * (1.0 + neo.entry_cap_pct / 100.0), tick, 'down')
        ask = round_tick(b.ask, tick, 'up')              # a BUY never prices below the ask
        if ask > cap + 1e-9:
            if lv.get('above_cap_since') is None:
                lv['above_cap_since'] = _iso(now)
                self.store.event(now, 'ABOVE_CAP', id=p['id'], ask=ask, cap=cap)
                self._throttled('cap_%d' % p['id'], now, 600,
                                '#%d %s ask %.2f above cap %.2f (trigger %.2f +%.0f%%): no '
                                'order, watching.' % (p['id'], p['side'], ask, cap,
                                                      p['trigger'], neo.entry_cap_pct))
            return
        lv['above_cap_since'] = None
        qty = int(p['qty'])
        if ask * qty > neo.max_premium_rs:
            self._abandon(p, now, 'entry_skipped_premium',
                          'premium Rs %.0f > ceiling Rs %.0f: SKIPPED (never resized)'
                          % (ask * qty, neo.max_premium_rs))
            return
        if int(lv.get('entry_attempts', 0)) >= neo.max_entry_attempts:
            self._abandon(p, now, 'entry_abandoned',
                          '%d entry attempts without a fill: giving up on this side'
                          % lv['entry_attempts'])
            return
        if self.journal.sends_today(now.date(), 'ENTRY') >= neo.max_entry_orders_per_day:
            self._trip(now, 'entry order ceiling %d' % neo.max_entry_orders_per_day)
            self._abandon(p, now, 'entry_abandoned', 'entry order ceiling reached')
            return
        allowed, why = self._buy_allowed(p, qty, now)
        if not allowed:
            self.store.event(now, 'BUY_REFUSED', id=p['id'], why=why)
            self._throttled('buy_refused_%d' % p['id'], now, 60,
                            'BUY REFUSED #%d %s: %s. Not sending.' % (p['id'], p['symbol'], why))
            return
        try:
            funds = self.broker.available_funds()
        except (BrokerUnavailable, BrokerAuth) as exc:
            self._throttled('funds_%d' % p['id'], now, 60,
                            '#%d: funds check unavailable (%s); no BUY this poll.' % (p['id'], exc))
            return
        need = ask * qty * (1.0 + neo.funds_margin_pct / 100.0)
        if funds is None:
            self._throttled('funds_shape', now, 600,
                            'LIMITS response has no usable %r field: BUY refused (fails '
                            'closed). Fix neo.limits_net_field from the probe output.'
                            % neo.limits_net_field)
            return
        if funds < need:
            self._abandon(p, now, 'entry_skipped_funds',
                          'available Rs %.0f < needed Rs %.0f: SKIPPED' % (funds, need))
            return
        self._send(p, 'ENTRY', 'B', qty, ask, now)

    def _buy_allowed(self, p: Dict[str, Any], qty: int, now: datetime) -> Tuple[bool, str]:
        """The BUY guard, mirror of the selling guard (NEO-02): no live BUY on
        the symbol, and the broker holds exactly what the book expects. More
        than expected is an EXTRA LOT: kill, loud, never traded away."""
        sym = self._live(p)['neo_symbol']
        try:
            book = self.broker.order_book()
            net = self.broker.net_long(sym, self.neo.product)
        except (BrokerUnavailable, BrokerAuth) as exc:
            if isinstance(exc, BrokerAuth):
                self.session_ok = False
            return False, 'broker unreadable: %s' % exc
        live_buys = [o for o in book if o.symbol == sym and o.side == 'B' and o.live]
        if live_buys:
            return False, 'another BUY is live on %s: %s' % (
                sym, ', '.join('%s(%s)' % (o.order_no, o.status) for o in live_buys))
        expected = self._expected_long(sym)
        if net > expected:
            self._trip(now, 'EXTRA LOT at the broker: %s net %d, book expects %d'
                       % (sym, net, expected), loud=True)
            return False, 'broker holds %d of %s, book expects %d' % (net, sym, expected)
        if net < expected:
            return False, 'broker holds %d of %s, book expects %d (mismatch)' % (net, sym, expected)
        return True, ''

    # ------------------------------------------------------------ exits
    def manage_open(self, now: datetime, books: Dict[str, Book]) -> None:
        self._note_traded_books(now, books)
        cfg, neo, today, t = self.cfg, self.neo, now.date(), now.time()
        self._books = books
        # An entry still being worked after entry_end, or left from a previous
        # day, is abandoned here — only with NOTHING in flight. A leg whose
        # fill is unknown stays until the broker settles it (NEO-06).
        for p in list(self.store.pending()):
            if self._live(p)['state'] == ENTRY_WAIT and (
                    p['entry_date'] < today.isoformat() or t > cfg.entry_end):
                self._abandon(p, now, 'entry_abandoned',
                              'not filled by %s; no entry' % cfg.entry_end)
        for p in self.store.holding():
            lv = self._live(p)
            if p['expiry'] < today.isoformat():
                continue                        # settle_expired owns it
            b = books.get(self.key(p['symbol']))
            if not traded_on(b, today):
                b = None
            lv['mark_fresh'] = False
            if b is not None and b.ltp > 0:
                p['last_ltp'] = b.ltp
                p['mfe_ltp'] = max(p.get('mfe_ltp') or b.ltp, b.ltp)
                p['mae_ltp'] = min(p.get('mae_ltp') or b.ltp, b.ltp)
                if b.bid > 0:
                    if lv.get('mark') != b.bid:
                        lv['mark_at'] = _iso(now)   # stamped on CHANGE (R3-37)
                    lv['mark'] = b.bid          # what a SELL would fetch
                    lv['mark_fresh'] = True
            overnight = p['entry_date'] < today.isoformat()
            if overnight and 'next_open_ltp' not in p and b is not None and b.ltp > 0:
                p['next_open_ltp'] = b.ltp
                p['next_open_at'] = _iso(now)
            if p['expiry'] == today.isoformat() and t >= neo.expiry_backstop_time:
                self._throttled('expiry_%d' % p['id'], now, 60,
                                'EXPIRY DAY BACKSTOP: #%d %s still OPEN at %s. Exit must '
                                'happen today.' % (p['id'], p['symbol'], now.strftime('%H:%M')))
            exit_time = self._exit_time(p)
            exit_dt = at(today, exit_time)
            leg = lv.get('leg')
            if leg:
                if leg.get('quarantined') and b is not None and (
                        (0 < b.ltp <= float(p['stop_level']))
                        or (0 < b.bid <= float(p['stop_level']) and b.ask > 0)):
                    # watched, never traded, while the owner decides (R4-05)
                    self._throttled('qstop_%d' % p['id'], now, 60,
                                    'STOP HIT #%d %s (ltp %.2f, stop %.2f) while order %s is '
                                    'QUARANTINED: no SELL is sent. Sell in the app if the lot is '
                                    'held, then `live-book-external %d --price X --yes`.'
                                    % (p['id'], p['symbol'], b.ltp, float(p['stop_level']),
                                       leg.get('order_no'), p['id']))
                continue                        # an order is in flight
            reason = lv.get('exit_reason')
            if (reason is None and overnight and t >= exit_time and b is None
                    and self.session_expected and self.closed_today != today
                    and self._proven_trading_day != today.isoformat()):
                self._throttled('noproof_%d' % p['id'], now, 60,
                                '#%d %s: 09:25 has passed but nothing has traded today. Not '
                                'latching the exit; an unlisted holiday? It applies from 09:25 '
                                'the moment the day proves itself.' % (p['id'], p['symbol']))
            if reason is None:
                stop = float(p['stop_level'])
                # the time exit needs a day SHOWN to be trading (R2-07, R3-10):
                # this book traded today, or something did earlier today
                if overnight and t >= exit_time and (
                        b is not None or (self.closed_today != today
                                          and self._proven_trading_day == today.isoformat())):
                    late = (now - exit_dt).total_seconds() > cfg.next_day_exit_grace_sec
                    reason = 'next_day_late' if late else 'next_day_0925'
                elif b is not None and (
                        (0 < b.ltp <= stop)
                        # the owner's wording: "sells when the bid hits 50% of
                        # the fill" — a two-sided bid at the level fires too
                        or (0 < b.bid <= stop and b.ask > 0)):
                    reason = 'stop_next_morning' if overnight else 'stop'
                if reason is None:
                    continue
                lv['exit_reason'] = reason
                lv['exit_triggered_at'] = _iso(now)
                p['exit_pending'] = reason
                self._set_state(p, EXIT_DUE, now)
                self.store.event(now, 'EXIT_DUE', id=p['id'], reason=reason,
                                 ltp=b.ltp if b else None, bid=b.bid if b else None)
                # the order goes out BEFORE the alert: an alert must never
                # delay an order (R3-06)
                self._try_exit(p, now, b)
                self.notify.send('EXIT DUE #%d %s (%s)%s' % (
                    p['id'], p['symbol'], reason,
                    ' ltp %.2f bid %.2f stop %.2f' % (b.ltp, b.bid, stop) if b else ''))
                continue
            self._try_exit(p, now, b)
        self._update_kill(now)

    def _try_exit(self, p: Dict[str, Any], now: datetime, b: Optional[Book]) -> None:
        neo, lv = self.neo, self._live(p)
        attempts = int(lv.get('exit_attempts', 0))
        last = lv.get('last_send_at')
        if attempts >= neo.exit_chase_fast_max and last:
            since = (now - datetime.fromisoformat(last)).total_seconds()
            self._throttled('exit_slow_%d' % p['id'], now, 60,
                            'EXIT #%d %s STILL OPEN after %d attempts (%s). Retrying once a '
                            'minute. Consider exiting in the app.'
                            % (p['id'], p['symbol'], attempts, lv.get('exit_reason')))
            if since < neo.exit_retry_slow_sec:
                return
        # A live SELL needs a bid that exists today, nothing more: a wide book
        # is what a fall looks like, and refusing it strands the stop (NEO-14).
        if b is None or b.bid <= 0:
            why = 'no trade yet today' if b is None else 'no bid'
            p['exit_defers'] = int(p.get('exit_defers', 0)) + 1
            self.store.event(now, 'DEFER', what='exit', id=p['id'],
                             reason=lv.get('exit_reason'), why=why)
            self._throttled('exit_defer_%d' % p['id'], now, neo.exit_defer_alert_sec,
                            'EXIT #%d %s due but no usable bid (%s). Waiting.'
                            % (p['id'], p['symbol'], why))
            return
        if b.ask > 0 and b.ask - b.bid > self.cfg.max_spread_abs:
            self.store.event(now, 'WIDE_BOOK', id=p['id'], bid=b.bid, ask=b.ask)
        qty = self._remaining(p)
        allowed, guard_why = self._sell_allowed(p, qty, now)
        if not allowed:
            if not lv.get('sell_refused_since'):
                lv['sell_refused_since'] = _iso(now)
            held = (now - datetime.fromisoformat(str(lv['sell_refused_since']))).total_seconds()
            self.store.event(now, 'SELL_REFUSED', id=p['id'], why=guard_why, held_sec=held)
            if held >= neo.sell_refused_alert_sec:
                self._throttled('refused_%d' % p['id'], now, 60,
                                'SELL REFUSED #%d %s for %.0fs: %s. Not sending. Check the app.'
                                % (p['id'], p['symbol'], held, guard_why))
            return
        lv['sell_refused_since'] = None
        self._send(p, 'EXIT', 'S', qty, round_tick(b.bid, lv['tick'], 'down'), now)

    def _sell_allowed(self, p: Dict[str, Any], qty: int, now: datetime) -> Tuple[bool, str]:
        """The selling guard. POSITIVE evidence only (R2-01): a position row,
        a holdings row, or the owner's confirmation for today — and never
        another SELL live on the symbol. A read failure is a refusal. With no
        source at all the SELL is refused and the owner is told what to do."""
        lv = self._live(p)
        sym = lv['neo_symbol']
        if qty <= 0:
            return False, 'nothing left to sell'
        try:
            book = self.broker.order_book()
        except (BrokerUnavailable, BrokerAuth) as exc:
            if isinstance(exc, BrokerAuth):
                self.session_ok = False
            return False, 'broker unreadable: %s' % exc
        live_sells = [o for o in book if o.symbol == sym and o.side == 'S' and o.live]
        if live_sells:
            return False, 'another SELL is live on %s: %s' % (
                sym, ', '.join('%s(%s)' % (o.order_no, o.status) for o in live_sells))
        try:
            known = self._known_order_nos()
            foreign = [no for no in self._filled_sells_today(sym, now) if no not in known]
        except (BrokerUnavailable, BrokerAuth) as exc:
            if isinstance(exc, BrokerAuth):
                self.session_ok = False
            return False, 'broker unreadable: %s' % exc
        if foreign:
            # a SELL this book did not send FILLED today: whatever the row says
            # (it may lag), selling again could go SHORT (R4-03)
            return False, ('a SELL this book did not send FILLED today on %s (%s). Not selling: '
                           'check the app for a SHORT; if you sold it, `live-book-external %d '
                           '--price X --yes`' % (sym, ', '.join(foreign), p['id']))
        try:
            net, source = self._positive_long(sym, p, now)
        except (BrokerUnavailable, BrokerAuth) as exc:
            if isinstance(exc, BrokerAuth):
                self.session_ok = False
            return False, 'broker unreadable: %s' % exc
        if net is None:
            return False, ('%s. NOT selling on an assumption. In the app: if %s is held, run '
                           '`live-confirm-held %d --yes`; if you sold it, `live-book-external '
                           '%d --price X --yes`; or sell it in the app.'
                           % (source, sym, p['id'], p['id']))
        if net < 0:
            self._trip(now, 'SHORT at the broker: %s net %d' % (sym, net), loud=True)
            return False, 'broker is SHORT %d on %s' % (-net, sym)
        if net < qty:
            return False, 'broker %s shows %d < sell qty %d (could go SHORT)' % (source, net, qty)
        if source in ('owner confirmation', 'holdings'):
            # the owner, or Holdings, vouches for the lot; the broker must show
            # no COUNTING sell on it today (filled, live or unreadable), so
            # neither source can sell twice (R3-01, R3-18)
            try:
                counting = self._sells_today(sym, now)
            except (BrokerUnavailable, BrokerAuth) as exc:
                return False, 'broker unreadable: %s' % exc
            if counting:
                if source == 'owner confirmation':
                    self.store.void_confirmation(int(p['id']), 'a SELL with a fill or live state '
                                                 'exists: %s' % counting[0].order_no)
                return False, ('%s lot but a SELL on %s exists today with a fill or still live '
                               '(%s)' % (source, sym, ', '.join(o.order_no for o in counting)))
        return True, ''

    # ------------------------------------------------------------- kill
    def _update_kill(self, now: datetime) -> None:
        k = self.store.kill()
        realised = sum(float(p.get('net_pnl') or 0.0) for p in self.store.closed_positions())
        unreal, fresh = 0.0, True
        for p in self.store.holding():
            lv = self._live(p)
            mark = lv.get('mark')
            if mark is None:
                fresh = False
                continue
            if not lv.get('mark_fresh'):
                fresh = False
            unreal += (float(mark) - float(p['entry_price'])) * self._remaining(p)
        if not math.isfinite(realised + unreal):
            self._trip(now, 'NON-FINITE EQUITY (%r + %r): a price in the book is not a number'
                       % (realised, unreal), loud=True)
            return
        equity = round(realised + unreal, 2)
        if k.get('equity') != equity or k.get('marks_fresh') != fresh or 'equity_at' not in k:
            k['equity_at'] = _iso(now)          # stamped on CHANGE, not every tick (R3-37)
        k['equity'] = equity
        k['marks_fresh'] = fresh
        # The peak only advances on fresh marks: a stale high must not raise
        # the bar the kill is measured from.
        if k.get('peak') is None or (fresh and equity > float(k['peak'])):
            k['peak'] = equity
        dd = float(k['peak']) - equity
        k['drawdown'] = round(dd, 2)
        if not k.get('tripped') and dd >= self.neo.kill_drawdown_rs:
            self._trip(now, 'drawdown Rs %.0f from peak Rs %.0f (equity Rs %.0f)'
                       % (dd, float(k['peak']), equity))

    # -------------------------------------------------------- reconcile
    def _reconcile(self, now: datetime, startup: bool) -> None:
        """Broker vs book, summed by symbol (NEO-27). A short, an extra lot,
        an own-tag order we do not know, a tag we had written off that
        appears, or an unexplained long of our family: KILL, loud. NO ROW for
        a previous-day holding is UNKNOWN (R2-03), not a mismatch: alert and
        wait for positive evidence or the owner. A shortfall newer than the
        positions lag, or seen only once, or before the open, is deferred
        (R2-08, R2-20). Nothing is flattened."""
        self._last_reconcile = now
        try:
            positions = self.broker.positions()
            book = self.broker.order_book()
        except (BrokerUnavailable, BrokerAuth) as exc:
            if isinstance(exc, BrokerAuth):
                self.session_ok = False
            self.reconcile_ok = False
            self._throttled('reconcile', now, 300,
                            'RECONCILE could not read the broker (%s). No new entries until it '
                            'can.' % exc)
            return
        pre_open = now.time() < self.cfg.market_open
        net: Dict[str, int] = {}
        for pos in positions:
            if pos.product and pos.product != self.neo.product:
                continue
            net[pos.symbol] = net.get(pos.symbol, 0) + pos.net_qty
        clean = True
        surplus_now: Set[str] = set()
        expected: Dict[str, int] = {}
        for p in self.store.holding():
            if p['expiry'] < now.date().isoformat():
                continue                        # settled by the exchange; settle_expired owns it
            sym = self._live(p)['neo_symbol']
            expected[sym] = expected.get(sym, 0) + self._remaining(p)
        in_flight_syms = {self._live(p)['neo_symbol'] for p in self.store.in_flight()}
        for sym, exp in expected.items():
            holders = [p for p in self.store.holding() if self._live(p)['neo_symbol'] == sym]
            if sym not in net:
                # NO ROW. Today's fill would have made one, so for a position
                # entered today this is a shortfall; for an earlier day it is
                # unknown and the owner is told what to do — unless the owner
                # already confirmed it for today (R3-25).
                if all(p['entry_date'] < now.date().isoformat() for p in holders):
                    if all(self.store.owner_confirmed_held(int(p['id']), now.date())
                           for p in holders):
                        continue                    # known on the owner's word
                    clean = False
                    ids = ', '.join('#%d' % p['id'] for p in holders)
                    self._throttled('norow_' + sym, now, 60,
                                    'NO POSITION ROW for %s (%s) this morning. Not read as flat. '
                                    'Exits wait for positive evidence. In the app: if it is held, '
                                    '`live-confirm-held <id> --yes`; if you sold it, '
                                    '`live-book-external <id> --price X --yes`.' % (sym, ids))
                    continue
                have = 0
            else:
                have = net[sym]
                if not all(self._qty_sane(p, have, now) for p in holders[:1]):
                    clean = False
                    continue
            if have == exp:
                self._mismatch_seen.pop(sym, None)
                continue
            if have > exp:
                if sym in in_flight_syms:
                    continue                    # an unknown entry leg: adoption settles it
                surplus_now.add(sym)
                verdict = self._surplus_verdict(sym, now, pre_open)
                if verdict == 'lag':
                    continue
                clean = False
                if verdict == 'confirmed':
                    self._trip(now, 'EXTRA LOT: broker holds %d of %s, book expects %d'
                               % (have, sym, exp), loud=True)
                else:
                    self._throttled('surplus_' + sym, now, 60,
                                    'EXTRA LOT READ ONCE: broker shows %d of %s, book expects %d. '
                                    'A second read confirms it.' % (have, sym, exp))
                continue
            # have < exp
            legs = any(self._live(p).get('leg') for p in holders)
            if legs:
                self.store.event(now, 'RECONCILE_DEFER', symbol=sym, have=have, expected=exp)
                continue
            ext = [o for o in book if o.symbol == sym and o.side == 'S' and o.filled > 0
                   and o.order_no not in self._known_order_nos()]
            if have == 0 and ext and sum(o.filled for o in ext) >= exp:
                # The owner sold it in the app: book what the broker shows,
                # never a price we invent. The books agree again afterwards.
                price = sum(o.avg_price * o.filled for o in ext) / sum(o.filled for o in ext)
                for p in holders:
                    self._close(p, now, price, 'closed_externally',
                                book=self._books.get(self.key(p['symbol'])), orders=2)
                    self._live(p)['state'] = 'CLOSED'
                    self._live(p)['closed_by_orders'] = [o.order_no for o in ext]
                self.notify.send('%s was CLOSED OUTSIDE this engine (orders %s) at ~%.2f. Booked '
                                 'as closed_externally.' % (sym, ', '.join(o.order_no for o in ext),
                                                           price))
                continue
            clean = False
            newest = max((str(p.get('entry_time') or '') for p in holders), default='')
            fresh = False
            if newest:
                try:
                    fresh = (now - datetime.fromisoformat(newest)).total_seconds() < self.neo.positions_lag_sec
                except ValueError:
                    fresh = False
            seen = self._mismatch_seen.get(sym, 0) + 1
            self._mismatch_seen[sym] = seen
            if fresh or seen < 2 or pre_open:
                self.store.event(now, 'RECONCILE_DEFER', symbol=sym, have=have, expected=exp,
                                 fresh=fresh, seen=seen, pre_open=pre_open)
                continue
            self._throttled('mismatch_' + sym, now, 60,
                            'RECONCILE MISMATCH: book holds %d of %s, broker shows %d. Nothing '
                            'is flattened automatically. Check the app.' % (exp, sym, have))
            self._trip(now, 'reconcile mismatch on %s (book %d, broker %d)' % (sym, exp, have))
        probe_lots = self.journal.probe_open_symbols()
        seen_syms = set()
        # POSITIVE evidence first (R4-02): every SELL with a fill today on a
        # symbol of ours, ours or foreign, summed; more sold than this book
        # held is a real short whatever the row says, and it is loud at once.
        closed_syms = {str(self._live(p).get('neo_symbol')) for p in self.store.data['positions']
                       if p.get('exit_date') == now.date().isoformat()}
        for sym in set(net) | set(expected) | closed_syms:
            if not self._looks_like_ours(sym) and sym not in expected:
                continue
            total = sum(self._filled_sells_today(sym, now).values())
            held = self._ours_sold_today(sym, now) + expected.get(sym, 0)
            if total > held:
                clean = False
                self._trip(now, 'SHORT: %d sold today on %s (every SELL with a fill, ours or '
                           'not) against %d this book held' % (total, sym, held), loud=True)
        for sym, q in net.items():
            seen_syms.add(sym)
            if q < 0:
                # A short on ANY symbol in this product is the owner's problem
                # now, whatever its family: no auto-trade, loud until seen. But
                # a row that shows ONLY today's SELL of a lot this book sold is
                # an unmeasured row shape, not a short (R3-20): resolved by the
                # book record plus today's filled SELL, and never on one read.
                if self._row_explained_by_sales(sym, q, now, expected.get(sym, 0)):
                    self._throttled('rowshape_' + sym, now, 3600,
                                    'NOTE: %s reads net %d and equals the SELLs filled today on a '
                                    'lot this book held: the row carries no carried quantity '
                                    '(measurement 1). Not a short. If YOU sold it, '
                                    '`live-book-external <id> --price X --yes`.' % (sym, q))
                    continue
                clean = False
                n = self._short_seen.get(sym, 0) + 1
                self._short_seen[sym] = n
                if n < 2:
                    self._throttled('short_' + sym, now, 60,
                                    'SHORT READ ONCE: %s net %d at the broker. A second read '
                                    'confirms it.' % (sym, q))
                    continue
                self._trip(now, 'SHORT at the broker: %s net %d' % (sym, q), loud=True)
            elif q > 0 and sym not in expected and self._looks_like_ours(sym):
                if sym in in_flight_syms:
                    continue
                if sym in probe_lots:
                    self._throttled('probe_lot_' + sym, now, 3600,
                                    'NOTE: broker holds %d of %s: the order probe\'s journalled '
                                    'overnight lot. Not touched.' % (q, sym))
                    continue
                surplus_now.add(sym)
                verdict = self._surplus_verdict(sym, now, pre_open)
                if verdict == 'lag':
                    continue                    # a lot this book just sold, inside the lag (R3-05)
                clean = False
                if verdict != 'confirmed':
                    self._throttled('surplus_' + sym, now, 60,
                                    'UNTRACKED LONG READ ONCE: broker shows %d of %s. A second '
                                    'read confirms it.' % (q, sym))
                    continue
                self._trip(now, 'UNEXPLAINED LONG: broker holds %d of %s which this book does '
                           'not track' % (q, sym), loud=True)
        for sym in list(self._short_seen):
            if sym not in seen_syms or net.get(sym, 0) >= 0:
                self._short_seen.pop(sym, None)
        for sym in list(self._surplus_seen):
            if sym not in surplus_now:
                self._surplus_seen.pop(sym, None)          # a clean read resets the count (R4-08)
        known = self._known_order_nos()
        written_off = set(self.journal.not_placed_tags())
        legs_by_tag = {str(self._live(p)['leg']['tag']): p for p in self.store.in_flight()
                       if self._live(p).get('leg')}
        for o in book:
            if o.tag in legs_by_tag and o.order_no not in known:
                # The order of a leg still UNKNOWN: bind it here and now.
                p = legs_by_tag[o.tag]
                leg = self._live(p)['leg']
                leg['order_no'] = o.order_no
                leg['bound_by'] = 'reconcile'
                self.journal.bound(now, o.tag, o.order_no, 'reconcile')
                self._set_state(p, ENTRY_OPEN if leg['kind'] == 'ENTRY' else EXIT_OPEN, now)
                known.add(o.order_no)
                continue
            if o.tag in written_off and o.order_no not in known:
                if o.terminal and o.filled == 0:
                    # a rejection or an unfilled cancel with our tag: the
                    # write-off was right; note it, no kill (R2-23)
                    self.journal.bound(now, o.tag, o.order_no, 'late-terminal')
                    self.store.event(now, 'LATE_TERMINAL', tag=o.tag, order_no=o.order_no,
                                     status=o.status)
                    continue
                clean = False
                self._trip(now, 'LATE ARRIVAL: order %s carries tag %s that was written off '
                           'as not placed (status %s)' % (o.order_no, o.tag, o.status), loud=True)
                self.journal.bound(now, o.tag, o.order_no, 'late')
            elif parse_tag(o.tag) and o.order_no not in known and not self._is_probe_tag(o.tag):
                clean = False
                self._trip(now, 'UNTRACKED own-tag order %s (%s, status %s)'
                           % (o.order_no, o.tag, o.status), loud=True)
                self.journal.bound(now, o.tag, o.order_no, 'reconcile')
        self.reconcile_ok = clean
        self.store.event(now, 'RECONCILE', startup=startup, clean=clean,
                         broker=net, ours=expected)

    def _recently_closed(self, sym: str, now: datetime) -> bool:
        """This book sold `sym` within the positions lag: the report may still
        show the lot."""
        for p in self.store.closed_positions():
            if self._live(p).get('neo_symbol') != sym:
                continue
            try:
                t = datetime.fromisoformat(str(p.get('exit_time') or ''))
            except ValueError:
                continue
            if (now - t).total_seconds() < self.neo.positions_lag_sec:
                return True
        return False

    def _surplus_verdict(self, sym: str, now: datetime, pre_open: bool) -> str:
        """A surplus (extra lot / unexplained long) becomes a verdict only
        outside the positions lag of a recent exit, after the open, and on the
        SECOND such read (R3-05), like the shortfall side. Returns 'lag'
        (inside the lag: clean), 'first' (reported, not clean, no verdict) or
        'confirmed'."""
        if self._recently_closed(sym, now):
            self._surplus_seen.pop(sym, None)
            return 'lag'
        if pre_open:
            return 'first'
        n = self._surplus_seen.get(sym, 0) + 1
        self._surplus_seen[sym] = n
        return 'confirmed' if n >= 2 else 'first'

    def _row_explained_by_sales(self, sym: str, net: int, now: datetime, expected: int) -> bool:
        """A negative row is the row shape without carried quantities, not a
        short, only when it equals every SELL filled today on the symbol
        (ours or foreign) AND those sales do not exceed what this book held
        today (R4-02): the sum matches exactly what we held. More sold than
        held is a short and is tripped from the positive evidence."""
        total = sum(self._filled_sells_today(sym, now).values())
        held = self._ours_sold_today(sym, now) + expected
        return total > 0 and -net == total and total <= held

    @staticmethod
    def _is_probe_tag(tag: str) -> bool:
        parsed = parse_tag(tag)
        return parsed is not None and parsed['id'] == 0

    def _looks_like_ours(self, sym: str) -> bool:
        """Our family: the underlying's prefix, or any symbol the scrip
        master lists (the NEO spelling need not start with the underlying)."""
        return (bool(re.match(r'^%s' % re.escape(self.cfg.underlying), sym.upper()))
                or sym in self._scrip_symbols)

    # ------------------------------------------------------- owner actions
    def confirm_held(self, pos_id: int, now: datetime, by: str = 'owner') -> Dict[str, Any]:
        """The owner states the lot is held TODAY (checked in the app). It is
        the positive source the selling guard accepts when the broker shows
        no row; it expires tonight and never sells twice. On a PENDING record
        whose entry may have filled overnight it OPENS the position (R3-04):
        at the real fill when the trade book shows it, else at the entry
        limit marked as an estimate, so the stop and the exit run."""
        p = self._find(pos_id)
        lv = self._live(p)
        if p['status'] == 'pending':
            leg = lv.get('leg')
            if not leg:
                raise ValueError('#%d is pending with no order in flight: nothing to confirm; use '
                                 '`live-book-external %d --not-filled --yes` to drop it'
                                 % (pos_id, pos_id))
            if leg.get('order_no') and self.offline:
                raise ValueError('#%d: BUY %s must be read at the broker first and NO ENGINE IS '
                                 'RUNNING: nothing was read or sent. The request is kept for the '
                                 'loop (next cron tick)' % (pos_id, leg['order_no']))
            if leg.get('order_no'):
                o = self.broker.order(leg['order_no'])      # read the order first (R4-09)
                if o is not None and not o.terminal:
                    raise ValueError('#%d: BUY %s is %s at the broker: a confirmation cannot '
                                     'replace a working order; wait for it or cancel it in the app'
                                     % (pos_id, leg['order_no'], o.status))
                if o is not None:
                    self._apply_order(p, leg, o, now)
                    if p['status'] == 'pending':
                        raise ValueError('#%d: BUY %s read %s with no fill: nothing to confirm'
                                         % (pos_id, leg['order_no'], o.status))
            if p['status'] == 'open':
                pass                                    # the read booked the fill
            else:
                self._confirm_pending(p, leg, now)
        elif p['status'] != 'open':
            raise ValueError('#%d is %s, not an open holding' % (pos_id, p['status']))
        self.store.confirm_held(pos_id, now.date(), by)
        self.journal.owner(now, pos_id, 'confirm_held', symbol=lv['neo_symbol'])
        self.store.event(now, 'OWNER', id=pos_id, action='confirm_held', by=by)
        self.store.save()
        return p

    def _confirm_pending(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        """The owner says the entry filled and the lot is held (R3-04): book
        the real fill when the trade book shows it, else the entry limit
        marked as an estimate. Both go through the unit check (R4-05)."""
        lv = self._live(p)
        if True:
            trade = self._trade_for(leg)
            if trade is not None:
                self._on_fill(p, leg, trade, now, trade.filled)
            else:
                view = OrderView(order_no=str(leg.get('order_no') or 'CONFIRMED-' + leg['tag']),
                                 status='complete', symbol=lv['neo_symbol'], side='B',
                                 qty=int(leg['qty']), filled=int(leg['qty']),
                                 avg_price=float(leg['limit']), price=float(leg['limit']),
                                 rej_reason='', tag=leg['tag'],
                                 placed_at=leg['sent_at'], product=self.neo.product)
                self.journal.bound(now, leg['tag'], view.order_no, 'owner-confirmed')
                self._on_fill(p, leg, view, now, int(leg['qty']))
                lv['entry_price_estimated'] = True
        if lv.get('leg'):
            raise ValueError('#%d: the fill quantity failed the unit check (QUARANTINED): use '
                             '`live-book-external %d --price X --yes` or `--not-filled`'
                             % (p['id'], p['id']))

    def book_external(self, pos_id: int, now: datetime, price: Optional[float],
                      by: str = 'owner') -> Dict[str, Any]:
        """The owner closed it in the app (R2-19). Refused while an order of
        this record is LIVE at the broker (R3-03): the engine requests its
        cancel, records the request and completes it once the order reads
        terminal with no fill; a live order is never written off. A day order
        from an earlier session cannot be live and is cleared on the owner's
        word. A pending record with a price books a real round trip (entry
        at the limit, estimated) so the loss reaches the kill and the report
        (R3-04). The price must be finite and sensible (R3-15)."""
        p = self._find(pos_id)
        lv = self._live(p)
        if price is not None:                           # validated FIRST (R4-17)
            if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
                raise ValueError('price must be a finite positive number, got %r' % (price,))
            ref = float(p.get('entry_price') or 0) or float(p.get('trigger') or 0)
            if ref and not (ref / 50.0 <= price <= ref * 20.0):
                raise ValueError('price %.2f is not sensible against entry/trigger %.2f'
                                 % (price, ref))
        leg = lv.get('leg')
        dead_leg: Optional[Dict[str, Any]] = None
        if leg and leg.get('quarantined'):
            # the owner states the outcome of a fill the engine could not read
            # in a sane unit: that word clears the quarantine (R4-05)
            self.journal.owner(now, pos_id, 'clear_quarantine', order_no=leg.get('order_no'))
            lv.setdefault('leg_history', []).append(dict(leg, cleared_by_owner=_iso(now)))
            lv['leg'] = None
            leg = None
        if leg:
            if not leg.get('order_no'):
                raise ValueError('#%d has an UNKNOWN send %s in flight: wait for it to resolve'
                                 % (pos_id, leg['tag']))
            o = self.broker.order(leg['order_no'])      # BrokerUnavailable propagates
            sent_day = str(leg.get('sent_at') or '')[:10]
            if o is None and sent_day and sent_day < now.date().isoformat():
                dead_leg = leg                          # a DAY order from an earlier session
                lv.setdefault('leg_history', []).append(dict(leg, written_off=_iso(now)))
                lv['leg'] = None
            elif o is None or not o.terminal:
                if self.offline:
                    raise ValueError('#%d has order %s in flight and NO ENGINE IS RUNNING: '
                                     'nothing was sent to the broker. The request is kept for '
                                     'the loop (next cron tick), which cancels it first'
                                     % (pos_id, leg['order_no']))
                lv['owner_pending'] = {'action': 'book_external', 'price': price, 'by': by,
                                       'order_no': leg['order_no'], 'requested_at': _iso(now),
                                       'status': p['status'], 'qty': int(p.get('qty') or 0)}
                leg['cancel_requested_at'] = None
                self._maybe_cancel_now(p, leg, now)
                raise ValueError('#%d has order %s %s at the broker: cancel requested; the '
                                 'write-off is applied once it reads terminal with no fill (a '
                                 'live order is never written off)'
                                 % (pos_id, leg['order_no'], o.status if o else 'UNREADABLE'))
            else:
                self._apply_order(p, leg, o, now)
                if lv.get('leg'):
                    raise ValueError('#%d: order %s still unresolved' % (pos_id, leg['order_no']))
        if p['status'] == 'open':
            if price is None:
                raise ValueError('a sale price is required to book an external close')
            self._close(p, now, float(price), 'closed_externally',
                        book=self._books.get(self.key(p['symbol'])), orders=2)
            lv['state'] = 'CLOSED'
        elif p['status'] == 'pending':
            if price is None:
                if dead_leg:
                    self.journal.not_placed(now, dead_leg['tag'], 'owner: never filled (day order '
                                            'sent %s, absent from the book)' % dead_leg['sent_at'][:10])
                    self.store.event(now, 'NOT_PLACED', id=p['id'], tag=dead_leg['tag'], by=by)
                self._abandon(p, now, 'entry_abandoned', 'owner confirmed the entry never filled')
            else:
                # it filled and the owner sold it: a real round trip, entry at
                # the last limit (estimated), so the loss is real in the book
                hist = lv.get('leg_history') or []
                limit = float(hist[-1]['limit']) if hist else float(p['trigger'])
                if dead_leg:
                    self.journal.bound(now, dead_leg['tag'], str(dead_leg['order_no']),
                                       'owner: filled and sold in the app')
                p.update({'status': 'open', 'entry_price': round(limit, 2),
                          'entry_time': _iso(now), 'stop_level': round(limit * self.cfg.stop_frac, 2),
                          'slip_vs_trigger': round(limit - p['trigger'], 2)})
                lv['entry_price_estimated'] = True
                self._close(p, now, float(price), 'closed_externally',
                            book=self._books.get(self.key(p['symbol'])), orders=2)
                lv['state'] = 'CLOSED'
        else:
            raise ValueError('#%d is already %s (%s). If you ALSO sold in the app the broker may '
                             'be SHORT: check the app; reconcile trips the kill on its next read'
                             % (pos_id, p['status'], p.get('exit_reason')))
        self.journal.owner(now, pos_id, 'book_external', price=price, status=p['status'])
        self.store.event(now, 'OWNER', id=pos_id, action='book_external', price=price, by=by)
        self.store.save()
        return p

    def _maybe_cancel_now(self, p: Dict[str, Any], leg: Dict[str, Any], now: datetime) -> None:
        leg['cancel_attempts'] = int(leg.get('cancel_attempts', 0)) + 1
        leg['cancel_requested_at'] = _iso(now)
        try:
            self.broker.cancel(leg['order_no'])
            self.store.event(now, 'CANCEL', id=p['id'], order_no=leg['order_no'], ok=True,
                             by='owner')
        except (BrokerRejected, BrokerUnclear) as exc:
            self.store.event(now, 'CANCEL', id=p['id'], order_no=leg['order_no'], ok=None,
                             error=str(exc), by='owner')

    def _find(self, pos_id: int) -> Dict[str, Any]:
        for p in self.store.data['positions']:
            if int(p['id']) == int(pos_id):
                pos: Dict[str, Any] = p
                return pos
        raise ValueError('no position #%d' % pos_id)

    def _apply_owner_requests(self, now: datetime) -> None:
        """Owner commands reach a RUNNING loop as request files, like RESUME
        (NEO-17). Each is a JSON object stamped by the CLI with the day it
        was written, the position id and the symbol; one from another day,
        or whose id and symbol do not match, is REFUSED (R3-02). A bad file is
        moved aside once (R3-22). The outcome is written beside it as
        `<name>.result` for the CLI to echo (R3-23)."""
        for req in sorted(self.store.dir.glob('CONFIRM_HELD_*')) + sorted(
                self.store.dir.glob('BOOK_EXTERNAL_*.json')) + sorted(
                self.store.dir.glob('RESUME')):
            if req.suffix in ('.result', '.bad', '.rejected', '.tmp'):
                continue                                # the CLI's own temp name (R4-16)
            kind = ('resume' if req.name == 'RESUME' else
                    'confirm' if req.name.startswith('CONFIRM') else 'book')
            result = {'applied': False, 'text': ''}
            try:
                body = json.loads(req.read_text(encoding='utf-8'))
                if not isinstance(body, dict):
                    raise ValueError('not an object')
            except (OSError, ValueError) as exc:
                self._move_aside(req, 'bad')
                self.notify.send('owner request %s is unreadable (%s): moved aside; re-issue it.'
                                 % (req.name, exc))
                continue
            try:
                if str(body.get('date')) != now.date().isoformat():
                    raise ValueError('written on %s, today is %s: stale, not applied'
                                     % (body.get('date'), now.date()))
                if kind == 'resume':
                    if self.store.kill().get('tripped'):
                        self.resume(now, who='owner (request file)')
                        result = {'applied': True, 'text': 'KILL CLEARED by the owner. Peak reset to '
                                  'equity Rs %s. Entries may resume.' % self.store.kill().get('peak')}
                    else:
                        result = {'applied': False, 'text': 'the kill was not tripped'}
                else:
                    pid = int(body['id'])
                    p = self._find(pid)
                    if str(body.get('symbol')) not in (p['symbol'], self._live(p)['neo_symbol']):
                        raise ValueError('symbol %r does not match #%d (%s / %s)'
                                         % (body.get('symbol'), pid, p['symbol'],
                                            self._live(p)['neo_symbol']))
                    if kind == 'confirm':
                        self.confirm_held(pid, now, by='owner (request file)')
                        result = {'applied': True, 'text': 'CONFIRMED #%d %s x%d held today (voided '
                                  'by any SELL with a fill)' % (pid, self._live(p)['neo_symbol'],
                                                                self._remaining(p))}
                    else:
                        price = body.get('price')
                        self.book_external(pid, now, float(price) if price is not None else None,
                                           by='owner (request file)')
                        result = {'applied': True, 'text': 'BOOKED #%d %s as %s%s' % (
                            pid, self._live(p)['neo_symbol'], p.get('exit_reason'),
                            ' at %.2f' % float(price) if price is not None else '')}
            except (ValueError, KeyError, TypeError, BrokerUnavailable, BrokerAuth) as exc:
                result = {'applied': False, 'text': 'REFUSED: %s' % exc}
            self.notify.send('OWNER REQUEST %s: %s' % (req.name, result['text']))
            keep = self.offline and 'NO ENGINE IS RUNNING' in str(result.get('text', ''))
            try:
                req.with_name(req.name + '.result').write_text(json.dumps(result), encoding='utf-8')
                if not keep:
                    req.unlink()                        # kept for the loop otherwise (R4-14)
            except OSError:
                self._move_aside(req, 'rejected')

    @staticmethod
    def _move_aside(req: Path, suffix: str) -> None:
        try:
            target = req.with_name(req.name + '.' + suffix)
            if target.exists():
                target.unlink()
            req.rename(target)
        except OSError:
            try:
                req.unlink()
            except OSError:
                pass

    # ----------------------------------------------------------- expiry
    def settle_expired(self, now: datetime) -> None:
        """A live position still in the book after its expiry. The exchange
        cash-settles it; the book is closed only once the broker shows it
        flat, at intrinsic off the NIFTY close (bookkeeping), loudly."""
        today = now.date()
        for p in self.store.holding():
            if p['expiry'] >= today.isoformat():
                continue
            lv = self._live(p)
            try:
                have = self.broker.net_long(lv['neo_symbol'], self.neo.product)
            except (BrokerUnavailable, BrokerAuth) as exc:
                self._throttled('expired_%d' % p['id'], now, 600,
                                '#%d %s EXPIRED while open; broker unreadable (%s).' % (
                                    p['id'], p['symbol'], exc))
                continue
            if have > 0:
                self._throttled('expired_%d' % p['id'], now, 600,
                                '#%d %s EXPIRED %s and the broker still shows %d. Waiting for '
                                'settlement.' % (p['id'], p['symbol'], p['expiry'], have))
                continue
            exp = date.fromisoformat(p['expiry'])
            close = None
            if self.market is not None:
                try:
                    close = self.market.day_close(self.cfg.index_token, exp)
                except Exception as exc:                # noqa: BLE001
                    logger.warning('index close for %s unavailable: %s', exp, exc)
            if close is None:
                self._throttled('expired_%d' % p['id'], now, 600,
                                '#%d %s EXPIRED and flat at the broker, but no NIFTY close to '
                                'settle against. Not booked.' % (p['id'], p['symbol']))
                continue
            k = float(p['strike'])
            intrinsic = max(0.0, close - k) if p['side'] == 'CE' else max(0.0, k - close)
            self._close(p, now, intrinsic, 'expired_settled', book=None, orders=1)
            p['settle_index_close'] = close
            lv['state'] = 'CLOSED'
            self.notify.send('#%d %s EXPIRED while open (the 09:25 exit was MISSED). Settled '
                             'at intrinsic %.2f off NIFTY %.2f.' % (p['id'], p['symbol'],
                                                                   intrinsic, close))

    # ----------------------------------------------------------- digest
    def eod_digest(self, today: date) -> str:
        text = super().eod_digest(today)
        iso = today.isoformat()
        unfilled: Dict[str, str] = {p['side']: ('TRIGGERED, NOT BOUGHT (%s)' % p.get('exit_reason')
                                if p.get('never_filled') else 'TRIGGERED, order not filled yet')
                    for p in self.store.data['positions']
                    if p.get('entry_date') == iso and (p.get('never_filled') or p['status'] == 'pending')}
        filled = len([p for p in self.store.data['positions']
                      if p.get('entry_date') == iso and p['status'] != 'pending'
                      and not p.get('never_filled')])
        out = []
        for ln in text.split('\n'):
            side = ln.split(' ')[0]
            if ln.endswith(': BOUGHT') and side in unfilled:
                ln = ln[:-len('BOUGHT')] + unfilled[side]
            if ln.startswith('entered '):
                ln = re.sub(r'^entered \d+,', 'entered %d (filled),' % filled, ln)
            out.append(ln)
        text = '\n'.join(out)
        k = self.store.kill()
        pend = self.store.pending()
        lines = [text, 'LIVE (%s): equity Rs %s peak Rs %s dd Rs %s kill %s'
                 % (self.mode, k.get('equity'), k.get('peak'), k.get('drawdown'),
                    'TRIPPED: %s' % k['reason'] if k.get('tripped') else 'ok'),
                 'sends today %d (entries %d) | session %s | reconcile %s'
                 % (self.journal.sends_today(today), self.journal.sends_today(today, 'ENTRY'),
                    'ok' if self.session_ok else 'DOWN', 'ok' if self.reconcile_ok else 'NOT CLEAN')]
        if pend:
            lines.append('PENDING entries (unfilled): %s'
                         % ', '.join('#%d %s %s' % (p['id'], p['side'], self._live(p)['state'])
                                     for p in pend))
        nf = [p for p in self.store.never_filled() if p.get('exit_date') == today.isoformat()]
        if nf:
            lines.append('never filled today: %s'
                         % ', '.join('#%d %s (%s)' % (p['id'], p['side'], p.get('exit_reason'))
                                     for p in nf))
        return '\n'.join(lines)
