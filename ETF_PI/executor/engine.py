"""The live executor's state machine (docs/EXECUTOR_SPEC.md; rule IDs cited inline).

One `Engine.step(now)` = one loop: day roll, control files, intents, every position's
state machine one step, periodic reconcile, status. Nothing blocks: broker reads are
wait-0 (F10) and each position advances at most one order action per step.

Position states:  PENDING -> ENTRY -> OPEN -> EXIT -> CLOSED
                  side exits: SKIPPED (never sent), UNFILLED (sent, nothing held)
A position holding lots after `stop_placing` stays in EXIT with `carry=True`; the next
trading day re-bases it as a carried lot and exits it from `carry_resume` (F16, R6).
"""
from __future__ import annotations

import datetime as _dt
import glob
import io
import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from . import rules
from .config import live_allowed

log = logging.getLogger('executor.engine')

ACTIVE = ('PENDING', 'ENTRY', 'OPEN', 'EXIT')
TERMINAL = ('CLOSED', 'SKIPPED', 'UNFILLED')


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec='seconds')


class Engine:
    def __init__(self, cfg: Dict[str, Any], broker: Any, feed: Any, notifier: Any, journal: Any,
                 ob: Any, state_dir: str, session_fn: Callable[[_dt.date], Any],
                 eod_fn: Callable[[_dt.date], _dt.time], deadman: Any = None,
                 epoch: Callable[[], float] = time.time):
        self.cfg = cfg
        self.b = broker
        self.feed = feed
        self.n = notifier
        self.j = journal
        self.ob = ob                      # option_buy: _ladder + constants (F20/F21)
        self.state_dir = state_dir
        self.intent_dir = os.path.join(state_dir, 'intents')
        self.done_dir = os.path.join(self.intent_dir, 'done')
        os.makedirs(self.done_dir, exist_ok=True)
        self.session_fn = session_fn
        self.eod_fn = eod_fn
        self.deadman = deadman
        self.epoch = epoch
        self.pos, rows, self.corrupt = journal.load()
        self.seen = journal.keys_seen(rows)
        self.day: Optional[_dt.date] = None
        self.faults = 0
        self.halt = ''                    # why entries are off for today ('' = allowed)
        self._restore_day(rows)
        self.reconciled = False           # R5: no new order before the first reconcile
        self._last_reconcile = 0.0
        self._last_status = 0.0
        self._map_try = 0.0
        self._now: Optional[_dt.datetime] = None
        self._short_reads: Dict[str, int] = {}
        self._last_own_fill = 0.0                # Q7: reconcile keeps watching after a close
        self.neo_stats: Dict[str, List[float]] = {}   # what -> [n, total_ms, max_ms]
        if self.corrupt:
            self.n.send('JOURNAL CORRUPT: %s -- NO new entries; exits continue. Repair %s'
                        % (self.corrupt, journal.path), 'E')
        feed.on_update = self._on_quote
        self.unmanaged: List[str] = []         # live lots in the LIVE journal while mode != live
        for p in self.pos.values():
            p['floor_breach_since'] = None     # review L4: persistence restarts after a restart
            if p.get('state') in ACTIVE:
                feed.watch(p['kite_symbol'], p.get('kite_token'))

    # -- persistence ---------------------------------------------------------------------
    def _jrow(self, ev: str, p: Optional[Dict[str, Any]] = None, **kw) -> None:
        row = {'ts': _iso(self._now) if self._now else '', 'ev': ev}
        if p is not None:
            row['key'] = p['key']
            row['pos'] = p
        row.update(kw)
        self.j.append(row)

    def _restore_day(self, rows: List[Dict[str, Any]]) -> None:
        last = None
        for r in rows:
            if r.get('ev') == 'DAY':
                last = r
        if last:
            self.day = _dt.date.fromisoformat(last['day'])
            self.faults = int(last.get('faults') or 0)
            self.halt = str(last.get('halt') or '')

    def _save_day(self) -> None:
        self._jrow('DAY', day=self.day.isoformat(), faults=self.faults, halt=self.halt)

    def _fault(self, why: str) -> None:
        """R2 / F22: a definite/RMS entry reject, an unclear entry place, an exit
        chasing longer than exit_fault_s. >= max_fault_streak -> no entries today."""
        self.faults += 1
        if self.faults >= int(self.cfg['max_fault_streak']) and not self.halt:
            self.halt = 'fault streak %d (last: %s)' % (self.faults, why)
            self.n.send('NO NEW ENTRIES today: %s' % self.halt, 'E')
        self._save_day()

    # -- helpers ------------------------------------------------------------------------------
    def _file(self, name: str) -> bool:
        return os.path.exists(os.path.join(self.state_dir, name))

    def _realised_today(self) -> float:
        d = self.day.isoformat() if self.day else ''
        return sum(float(p.get('realised') or 0) for p in self.pos.values()
                   if p.get('closed_day') == d)

    def _active(self) -> List[Dict[str, Any]]:
        return [p for p in self.pos.values() if p.get('state') in ACTIVE]

    def _carried_base(self, symbol: str) -> int:
        d = self.day.isoformat() if self.day else ''
        return sum(int(p.get('carried_qty') or 0) for p in self.pos.values()
                   if p.get('symbol') == symbol and p.get('carry_day') == d)

    def _on_quote(self, kite_symbol: str, q: rules.Quote) -> None:
        """Every price update reaches the paper exchange (no-op live)."""
        for p in list(self.pos.values()):
            if p.get('kite_symbol') == kite_symbol and p.get('state') in ACTIVE:
                self.b.on_quote(p['symbol'], q.bid, q.ask)
                break

    def _quote(self, p: Dict[str, Any]) -> Optional[rules.Quote]:
        q = self.feed.fresh(p['kite_symbol'], p.get('exchange') or 'NFO')
        if q is None or not q.valid() or q.age(self.epoch()) > self.cfg['price_stale_s']:
            return None
        return q

    def _neo(self, fn: Callable, what: str, p: Optional[Dict[str, Any]] = None):
        """Every broker call (F9): (ok, value). Any NeoError / bug = (False, None) +
        a throttled alert. Never raises into the loop."""
        t0 = time.time()
        try:
            out = fn()
            self._neo_time(what, t0)
            return True, out
        except Exception as e:
            self._neo_time(what + '!', t0)
            key = 'neo_%s_%s' % (what, p['key'] if p else '')
            self.n.throttled(key, 60, '%s%s: %s: %s' % (
                (p['key'] + ' ') if p else '', what, type(e).__name__, e), 'E')
            log.warning('%s failed: %s', what, e, exc_info=not hasattr(e, 'code'))
            return False, None

    def _neo_time(self, what: str, t0: float) -> float:
        """Lag study (traceability review Part C): every NEO call's wall time, aggregated
        per kind in status.json; a slow one is logged on its own."""
        ms = (time.time() - t0) * 1000.0
        st = self.neo_stats.setdefault(what, [0, 0.0, 0.0])
        st[0] += 1
        st[1] += ms
        st[2] = max(st[2], ms)
        if ms >= float(self.cfg.get('neo_slow_ms', 300)):
            log.info('NEO slow: %s %.0f ms', what, ms)
        return ms

    def _qsnap(self, q: Optional[rules.Quote]) -> Optional[Dict[str, Any]]:
        if q is None:
            return None
        ep = self.epoch()
        return {'bid': q.bid, 'ask': q.ask, 'ltp': q.ltp, 'src': q.source,
                'age_s': round(max(0.0, ep - q.ts), 2) if q.ts else None,
                'exch_lag_s': round(q.ts - q.exch_ts, 2) if q.ts and q.exch_ts else None,
                'bid_qty': q.bids[0][1] if q.bids else None,
                'ask_qty': q.asks[0][1] if q.asks else None}

    def _charges(self, buy_turnover: float, sell_turnover: float) -> float:
        c = self.cfg.get('charges') or {}
        stt = sell_turnover * float(c.get('stt_sell_pct', 0)) / 100.0
        exch = (buy_turnover + sell_turnover) * float(c.get('exch_pct', 0)) / 100.0
        sebi = (buy_turnover + sell_turnover) * float(c.get('sebi_per_crore', 0)) / 1e7
        stamp = buy_turnover * float(c.get('stamp_buy_pct', 0)) / 100.0
        gst = (exch + sebi) * float(c.get('gst_pct', 0)) / 100.0
        return round(stt + exch + sebi + stamp + gst, 2)

    # -- the loop ------------------------------------------------------------------------------
    def step(self, now: _dt.datetime) -> None:
        self._now = now
        today = now.date()
        if self.day != today:
            self._new_day(today)
        sess = self.session_fn(today)
        if sess is not None and now.time() >= _dt.time(8, 58) and not self.b.map_ready(today):
            if self.epoch() - self._map_try >= 60:
                self._map_try = self.epoch()
                ok, _ = self._neo(lambda: self.b.load_map(today), 'scrip map')
                if ok:
                    log.info('scrip map loaded for %s', today)
        in_session = sess is not None and sess[0] <= now.time() < sess[1]
        if in_session or self._active():
            self.feed.maybe_restart()
        if in_session and (not self.reconciled or
                           self.epoch() - self._last_reconcile >= self.cfg['reconcile_s']):
            recent_fill = self.epoch() - self._last_own_fill < float(self.cfg['cover_window_s'])
            if self._active() or not self.reconciled or self._watch_tags() or recent_fill:
                self._reconcile()
        if in_session:
            self._intents(now)
        for p in list(self._active()):
            try:
                self._advance(p, now, in_session)
            except Exception as e:                       # a bug must not stop other positions
                log.exception('advance %s', p.get('key'))
                self.n.throttled('bug_' + p['key'], 60, '%s: BUG %s: %s -- position still '
                                 'managed each loop' % (p['key'], type(e).__name__, e), 'E')
        if sess is not None and now.time() >= sess[1]:
            for p in self._active():
                if int(p.get('qty') or 0) > 0:
                    self.n.throttled('after_' + p['key'], self.cfg['after_close_alert_s'],
                                     '%s STILL HOLDS %d %s after the close -- carried; exits '
                                     'from %s next session' % (p['key'], p['qty'], p['symbol'],
                                                               self.cfg['carry_resume']), 'E')
        if self.unmanaged:
            self.n.throttled('unmanaged', 300, 'LIVE LOT(S) NOT MANAGED: %s are open in the LIVE '
                             'journal but mode=%s. Set mode: live and restart, or exit them in the '
                             'app.' % (', '.join(self.unmanaged), self.cfg['mode']), 'E')
        self._status(now)

    # -- day roll (R6 / F16) ----------------------------------------------------------------
    def _new_day(self, today: _dt.date) -> None:
        prev = self.day
        self.day = today
        self.faults, self.halt = 0, ''
        for p in self._active():
            if p['state'] in ('PENDING', 'ENTRY'):
                p['state'], p['note'] = 'UNFILLED', 'entry left open over a day roll'
                self._jrow('ENTRY_DAYROLL', p)
                self.n.send('%s: an entry was still open at the day roll -- marked UNFILLED; '
                            'check the app for a stray order' % p['key'], 'E')
                continue
            qty = int(p.get('qty') or 0)
            if qty <= 0:
                continue
            p['carried_qty'] = qty
            p['carry_day'] = today.isoformat()
            p['carry'] = True
            p['state'] = 'EXIT'
            p['exit_type'] = p.get('exit_type') or 'CARRIED'
            p['exit_started'] = None
            p['chase'] = self.b.chase_state(p['symbol'], p['token'], p['tick'], qty).to_dict()
            self._jrow('CARRY_IN', p)
            self.n.send('%s: %d %s CARRIED from %s -- selling from %s' % (
                p['key'], qty, p['symbol'], prev, self.cfg['carry_resume']), 'E')
        self._save_day()

    # -- intents (§2, E1-E3, F18) -------------------------------------------------------------
    def _intents(self, now: _dt.datetime) -> None:
        for path in sorted(glob.glob(os.path.join(self.intent_dir, '*.json'))):
            try:
                with io.open(path, encoding='utf-8') as f:
                    it = json.load(f)
                key = str(it['key'])
            except (OSError, ValueError, KeyError) as e:
                self.n.send('bad intent file %s (%s) -- ignored' % (os.path.basename(path), e), 'E')
                self._done(path, '.bad')
                continue
            if key in self.seen or key in self.pos:
                self._done(path)
                continue
            self.seen.add(key)
            try:
                self._accept(it, now)
            except Exception as e:
                log.exception('intent %s', key)
                self.n.send('%s: intent handling BUG %s: %s -- skipped' % (key, type(e).__name__, e), 'E')
                self._jrow('INTENT_BUG', key=key, error=str(e))
            self._done(path)

    def _done(self, path: str, suffix: str = '') -> None:
        try:
            os.replace(path, os.path.join(self.done_dir, os.path.basename(path) + suffix))
        except OSError as e:
            log.warning('cannot move intent %s: %s', path, e)

    def _skip(self, it: Dict[str, Any], why: str, kind: str = 'I', quiet: bool = False) -> None:
        self._jrow('SKIP', key=str(it.get('key')), why=why)
        if not quiet:
            self.n.send('SKIP %s %s %s: %s' % (it.get('opt_type', ''), it.get('strike', ''),
                                               it.get('key', ''), why), kind)

    def _gate(self, it: Dict[str, Any], now: _dt.datetime) -> Optional[str]:
        """E1: the first failing gate, or None."""
        cfg = self.cfg
        mode = cfg['mode']
        if mode == 'off':
            return 'mode off'
        if mode == 'live' and not live_allowed(cfg, now.date()):
            return 'live_until %s passed' % cfg['live_until']
        if self._file('KILL') or self._file('FLATTEN'):
            return 'KILL/FLATTEN file present'
        if self.corrupt:
            return 'journal corrupt'
        if not self.reconciled:
            return 'first reconcile not done'
        if self.halt:
            return 'halted: %s' % self.halt
        lim = -float(cfg['daily_loss_per_lot']) * cfg['lots']
        if self._realised_today() <= lim:
            self.halt = 'daily loss %.0f <= %.0f' % (self._realised_today(), lim)
            self._save_day()
            self.n.send('NO NEW ENTRIES today: %s' % self.halt, 'E')
            return 'halted: %s' % self.halt
        if len(self._active()) >= int(cfg['max_open']):
            return 'max_open %d reached' % cfg['max_open']
        t = now.time()
        if t.replace(second=0, microsecond=0) > cfg['last_entry_time']:     # whole minute, as paper
            return 'after last entry %s' % cfg['last_entry']
        if self.ob._in_no_entry_window(t):                # belt and braces: paper blocks it too
            return 'inside a blocked entry window'
        try:
            created = _dt.datetime.fromisoformat(str(it['created_at']))
            age = (now - created).total_seconds()
        except (KeyError, ValueError, TypeError):
            return 'intent has no usable created_at'
        if age > cfg['intent_max_age_s']:
            return 'intent %.0fs old (> %ss)' % (age, cfg['intent_max_age_s'])
        if str(it.get('signal_tf')) != '10minute' or str(it.get('underlying')) != 'NIFTY':
            return 'not a NIFTY 10M intent'
        return None

    def _accept(self, it: Dict[str, Any], now: _dt.datetime) -> None:
        why = self._gate(it, now)
        if why:
            self._skip(it, why, quiet=(why == 'mode off'))
            return
        ok, m = self._neo(lambda: self.b.map_intent(it), 'symbol map')       # F18
        if not ok:
            self._skip(it, 'NEO symbol mapping failed', 'E')
            return
        if any(p['symbol'] == m['symbol'] for p in self._active()):
            self._skip(it, 'already active in %s' % m['symbol'])
            return
        if self.cfg.get('skip_foreign_strikes', True):
            # BTST coexistence (BTST session, 2026-11-27): NEO's positions row is per symbol +
            # product and UNTAGGED. A lot of ours on a strike another bot holds in the same
            # product reads to that bot as an "EXTRA LOT" (its kill). Never enter such a strike.
            ok1, acct = self._neo(lambda: self.b.account_net(m['symbol']), 'foreign check')
            ok2, own = self._neo(lambda: self.b.own_net(m['symbol']), 'foreign check')
            if not (ok1 and ok2):
                self._skip(it, 'cannot read positions to check for another bot on the strike', 'E')
                return
            foreign = (int(acct) if acct is not None else 0) - int(own) - self._carried_base(m['symbol'])
            if foreign != 0:
                self._skip(it, '%s already holds %d not ours (%s) -- left to that bot'
                           % (m['symbol'], foreign, self.cfg['product']))
                return
        if int(m['lot_size']) != int(it['lot_size']):
            self._skip(it, 'lot size NEO %s != Kite %s' % (m['lot_size'], it['lot_size']), 'E')
            return
        tick = float(m['tick'] or it['tick_size'])
        cap = rules.entry_cap(float(it['signal_ltp']), float(it['st_value']), tick, self.cfg, self.ob)
        p = {
            'key': str(it['key']), 'state': 'PENDING', 'day': now.date().isoformat(),
            'mode': self.cfg['mode'], 'kite_symbol': it['kite_symbol'],
            'kite_token': it.get('kite_token'), 'exchange': it.get('exchange') or 'NFO',
            'symbol': m['symbol'], 'token': m['token'], 'lot_size': int(m['lot_size']),
            'tick': tick, 'tick_size': tick, 'strike': it['strike'], 'expiry': it['expiry'],
            'opt_type': it['opt_type'], 'signal_tf': it['signal_tf'],
            'signal_ltp': float(it['signal_ltp']), 'st_value': float(it['st_value']),
            't2_value': float(it.get('t2_value') or 0), 't2_tf': it.get('t2_tf') or '',
            'qty_target': int(self.cfg['lots']) * int(m['lot_size']), 'cap': cap,
            'entry': {'attempt': 0, 'tags': [], 'working': '', 'order_no': '', 'since': 0.0,
                      'unclear_since': None, 'fills': {}, 'recheck_at': None,
                      'settled': [], 'first_seen': self.epoch()},
            'qty': 0, 'entry_premium': 0.0, 'peak_premium': 0.0, 't1_hit': False,
            'cost_sl_active': False, 'cost_sl_level': 0, 'sl_reason': None, 'trail_level': 0.0,
            'floor_breach_since': None, 'entry_time': None, 'exit_type': None, 'chase': None,
            'sold': 0, 'proceeds': 0.0, 'realised': 0.0, 'carried_qty': 0, 'carry_day': None,
            'carry': False, 'no_price_since': None, 'stale_exit': False, 'last_fill_epoch': 0.0,
            'exit_fault': False, 'note': '', 'signal_at': it.get('created_at'),
        }
        self.pos[p['key']] = p
        self.feed.watch(p['kite_symbol'], p['kite_token'])
        self._jrow('ACCEPT', p, intent=it)

    # -- one position, one step --------------------------------------------------------------
    def _advance(self, p: Dict[str, Any], now: _dt.datetime, in_session: bool) -> None:
        if p['state'] in ('PENDING', 'ENTRY'):
            self._entry(p, now)
        elif p['state'] == 'OPEN':
            self._open(p, now)
        elif p['state'] == 'EXIT':
            chase = p.get('chase') or {}
            if not self._exit_window(p, now) and not chase.get('working_tag'):
                # review H5: no NEO reads / journal rows / alerts all night. A working sell is
                # still settled (cancelled) above; nothing new can be placed out here anyway.
                if now.time() >= self.cfg['stop_placing_time'] and not p.get('carry_noted') \
                        and int(chase.get('remaining') or 0) > 0:
                    p['carry_noted'] = True
                    self._jrow('CARRY_OUT', p)
                    self.n.send('%s: %d %s NOT SOLD by %s -- carried; next session it is sold '
                                'from %s ONLY if the account still shows it (MIS: the broker may '
                                'have squared it off)' % (p['key'], int(chase.get('remaining') or 0),
                                                          p['symbol'], self.cfg['stop_placing'],
                                                          self.cfg['carry_resume']), 'E')
                return
            self._exit(p, now)

    def _exit_window(self, p: Dict[str, Any], now: _dt.datetime) -> bool:
        t = now.time()
        sess = self.session_fn(now.date())
        if sess is None or t < sess[0] or t >= self.cfg['stop_placing_time']:
            return False
        return not (p.get('carry') and t < self.cfg['carry_resume_time'])

    # ---- entry (E2-E7, F17, F24) ----
    def _entry(self, p: Dict[str, Any], now: _dt.datetime) -> None:
        e = p['entry']
        ep = self.epoch()
        if not e['working']:
            stop = now.time() >= self.cfg['stop_placing_time'] or self._file('KILL') \
                or self._file('FLATTEN')
            if stop:
                return self._entry_end(p, 'stopped before a send (KILL/FLATTEN/close)')
            if self.ob._in_no_entry_window(now.time()):    # a retry must not cross into a window
                return self._entry_end(p, 'a blocked entry window began')
            if e['recheck_at'] is not None:              # F24: one more read after a cancel
                return
            max_age = (float(self.cfg['intent_max_age_s']) + 10.0 +
                       int(self.cfg['entry_attempts']) * float(self.cfg['entry_reprice_s']))
            if ep - float(e['first_seen']) > max_age:     # review M2 (e.g. after a restart)
                return self._entry_end(p, 'entry %.0f s old -- too late to send' % (ep - float(e['first_seen'])))
            if ep < float(e.get('next_try') or 0):        # review M3: 429 back-off
                return
            remaining = p['qty_target'] - self._entry_filled(p)
            q = self._quote(p)
            if q is None:
                if ep - float(e['first_seen']) > 10:
                    return self._entry_end(p, 'no live price for 10 s')
                return
            lim = rules.entry_limit(q, remaining, p['cap'], p['tick'])
            if lim is None:
                return self._entry_end(p, 'ask %.2f above cap %.2f (price ran)' % (q.ask, p['cap']))
            if e['attempt'] == 0 and self.cfg['mode'] == 'live':
                ok, funds = self._neo(self.b.funds, 'funds', p)
                need = q.ask * remaining * float(self.cfg['funds_margin'])
                if not ok or funds is None or funds < need:
                    return self._entry_end(p, 'funds %s < needed %.0f' % (funds, need))
            tag = self.b.new_tag()
            e['attempt'] += 1
            e['tags'].append(tag)
            e['working'], e['order_no'], e['since'], e['unclear_since'] = tag, '', ep, None
            e['limit'] = lim
            p['state'] = 'ENTRY'
            try:
                sig_age = round(now.timestamp() - _dt.datetime.fromisoformat(
                    str(p.get('signal_at'))).timestamp(), 2)
            except (TypeError, ValueError):
                sig_age = None
            self._jrow('ENTRY_SEND', p, tag=tag, limit=lim, qty=remaining,      # E4: on disk first
                       quote=self._qsnap(q), signal_age_s=sig_age)
            t0 = time.time()
            try:
                e['order_no'] = self.b.place(p['symbol'], p['token'], 'B', remaining, lim, tag)
                self._jrow('ENTRY_PLACED', p, tag=tag, order_no=e['order_no'],
                           place_ms=round(self._neo_time('place buy', t0)))
            except Exception as ex:
                self._neo_time('place buy!', t0)
                self._entry_send_failed(p, ex)
            return
        ok, res = self._neo(lambda: self.b.resolve(e['working'], expect_qty=None), 'entry read', p)
        if not ok:
            return
        if res.order_no and not e['order_no']:
            e['order_no'] = res.order_no
        self._entry_apply(p, e['working'], res)
        st = res.state
        if res.terminal:
            if st == 'rejected':
                self._fault('entry rejected: %s' % res.reason)
            filled = self._entry_filled(p)
            if filled > 0:
                return self._opened(p, now)
            if e['recheck_at'] is None and st != 'rejected':
                e['recheck_at'] = ep + 1.0               # F24: read once more after 1 s
                return
            if e['recheck_at'] is not None and ep < e['recheck_at']:
                return
            e['recheck_at'] = None
            e['settled'].append(e['working'])
            e['working'] = ''
            if st == 'rejected' or e['attempt'] >= int(self.cfg['entry_attempts']):
                return self._entry_end(p, 'unfilled after %d attempt(s) (%s)' % (e['attempt'], res.reason))
            self._jrow('ENTRY_RETRY', p)
            return
        if st in ('resting', 'partial'):
            if ep - float(e['since']) >= self.cfg['entry_reprice_s'] and e['order_no']:
                ok, r2 = self._neo(lambda: self.b.cancel_confirm(e['order_no'], e['working']),
                                   'entry cancel', p)
                if ok and r2 is not None:
                    self._entry_apply(p, e['working'], r2)
                    if not r2.terminal:
                        e['since'] = ep - self.cfg['entry_reprice_s'] + 1.0   # re-cancel in 1 s
            return
        if st == 'absent':
            if e['order_no']:
                self.n.throttled('absent_' + p['key'], 60, '%s: entry order %s is not in the '
                                 'book -- nothing re-sent; watching its tag' % (p['key'], e['order_no']), 'E')
            since = e['unclear_since'] or e['since']
            if ep - float(since) >= self.cfg['unclear_grace_s']:
                e['settled'].append(e['working'])        # stays on the watch list (E6)
                e['working'] = ''
                return self._entry_end(p, 'unclear entry never reached the book (%ss)'
                                       % self.cfg['unclear_grace_s'])
            return
        self.n.throttled('unk_' + p['key'], 60, '%s: entry state unknown: %s' % (p['key'], res.reason), 'E')
        if self._entry_filled(p) > 0:                     # review L1: never leave fills unmanaged
            e.setdefault('unknown_since', ep)
            if ep - float(e['unknown_since']) > 10:
                self.n.send('%s: entry status unreadable but %d filled -- managing the fill'
                            % (p['key'], self._entry_filled(p)), 'E')
                return self._opened(p, now)

    def _entry_send_failed(self, p: Dict[str, Any], ex: Exception) -> None:
        e = p['entry']
        name = type(ex).__name__
        code = str(getattr(ex, 'code', ''))
        if name == 'Unclear':
            e['unclear_since'] = self.epoch()
            self._jrow('ENTRY_UNCLEAR', p, error=str(ex))
            self._fault('unclear entry place: %s' % ex)
            self.n.send('%s: entry place UNCLEAR (%s) -- resolving by tag, never re-sent'
                        % (p['key'], ex), 'E')
            return
        tag = e['working']
        e['settled'].append(tag)
        e['working'] = ''
        if name == 'Rejected' and code == '429':          # F22: transient, no fault
            e['attempt'] -= 1
            e['r429'] = int(e.get('r429') or 0) + 1
            e['next_try'] = self.epoch() + float(self.cfg['reject_backoff_s'])   # review M3
            self._jrow('ENTRY_429', p)
            if e['r429'] > 3:
                self._entry_end(p, 'NEO rate-limited the entry 4 times')
            return
        if name == 'Rejected':
            self._fault('entry rejected: %s' % ex)
        self._entry_end(p, 'entry not placed: %s: %s' % (name, ex))

    def _entry_apply(self, p: Dict[str, Any], tag: str, res: Any) -> None:
        if res is None:
            return
        f = p['entry']['fills']
        prev = f.get(tag) or [0, 0.0]
        if res.filled > prev[0] or (res.filled == prev[0] and res.avg_price and not prev[1]):
            f[tag] = [int(res.filled), float(res.avg_price)]
            p['last_fill_epoch'] = self.epoch()
            self._last_own_fill = self.epoch()
            self._jrow('ENTRY_FILL', p, tag=tag, filled=res.filled, avg=res.avg_price)

    @staticmethod
    def _entry_filled(p: Dict[str, Any]) -> int:
        return sum(int(v[0]) for v in p['entry']['fills'].values())

    @staticmethod
    def _entry_avg(p: Dict[str, Any]) -> float:
        q = sum(int(v[0]) for v in p['entry']['fills'].values())
        return (sum(int(v[0]) * float(v[1]) for v in p['entry']['fills'].values()) / q) if q else 0.0

    def _entry_end(self, p: Dict[str, Any], why: str) -> None:
        p['state'] = 'UNFILLED' if p['entry']['tags'] else 'SKIPPED'
        p['note'] = why
        p['closed_day'] = self.day.isoformat()
        self._jrow('ENTRY_END', p, why=why)
        self.n.send('%s %s %s: NO ENTRY -- %s' % (p['opt_type'], p['strike'], p['key'], why),
                    'E' if p['state'] == 'UNFILLED' and 'unclear' in why else 'I')
        if not self._watch_tags_of(p):
            self.feed.unwatch(p['kite_symbol'])

    def _opened(self, p: Dict[str, Any], now: _dt.datetime, late: bool = False) -> None:
        e = p['entry']
        qty, avg = self._entry_filled(p), self._entry_avg(p)
        if qty % int(p['lot_size']):
            self.n.send('%s: filled %d is not a whole number of lots (%d)' % (
                p['key'], qty, p['lot_size']), 'E')
        e['working'] = ''
        e['recheck_at'] = None
        if avg <= 0:                                      # review L6
            avg = float(e.get('limit') or p['signal_ltp'])
            self.n.send('%s: fill reported with avg price 0 -- using the limit %.2f' % (p['key'], avg), 'E')
        p['qty'] = qty
        p['entry_premium'] = round(avg, 4)                # E7: re-based on the FILL
        p['peak_premium'] = round(avg, 4)
        p['entry_time'] = now.isoformat()
        p['state'] = 'OPEN'
        p['last_sample_min'] = None
        p['entry_slip'] = round(avg - float(p['signal_ltp']), 4)          # per unit, vs signal LTP
        self._jrow('OPEN', p)
        slip = avg - p['signal_ltp']
        self.n.send('BOUGHT %d %s @ %.2f (signal %.2f, slip %+.2f) | T1 %.2f%s%s' % (
            qty, p['symbol'], avg, p['signal_ltp'], slip, p['st_value'],
            (' T2 %.2f' % p['t2_value']) if p['t2_value'] else '',
            ' | LATE FILL -> exiting now' if late else ''))
        if late:
            self._start_exit(p, now, 'LATE_FILL')

    # ---- open (X1-X9) ----
    def _open(self, p: Dict[str, Any], now: _dt.datetime) -> None:
        if self._file('FLATTEN'):
            return self._start_exit(p, now, 'FLATTEN')
        if now.time() >= self.eod_fn(now.date()):
            return self._start_exit(p, now, 'EOD')
        q = self._quote(p)
        ep = self.epoch()
        if q is None:                                     # X8 / F13
            since = p.get('no_price_since') or ep
            p['no_price_since'] = since
            gap = ep - since
            if gap >= self.cfg['no_price_flatten_s']:
                p['stale_exit'] = True
                self.n.send('%s: NO PRICE for %.0f s -- flattening from the last bid' % (p['key'], gap), 'E')
                return self._start_exit(p, now, 'NO_PRICE')
            if gap >= self.cfg['no_price_alert_s']:
                self.n.throttled('noprice_' + p['key'], self.cfg['no_price_alert_s'],
                                 '%s: no price for %.0f s (ws: %s)' % (p['key'], gap, self.feed.ws_error()), 'E')
            return
        p['no_price_since'] = None
        p['last_bid'] = q.bid
        say = log
        try:
            if now.second >= int(self.cfg['minute_sample_second']) and \
                    p.get('last_sample_min') != now.strftime('%Y%m%d%H%M'):
                ex = rules.minute_sample(p['key'], p, q, now, self.ob._ladder, self.ob, say)   # X1
                self._jrow('SAMPLE', p, ltp=q.ltp, bid=q.bid)
                if ex:
                    return self._start_exit(p, now, ex)
            was = bool(p.get('t1_hit'))
            if rules.tick_arm(p['key'], p, q, now, self.ob._ladder, say) and not was:    # X6
                self._jrow('T1_ARM', p, ltp=q.ltp)
                self.n.send('T1 HIT %s @ %.2f -- floor %.2f, holding for T2 %.2f' % (
                    p['symbol'], q.ltp, float(p.get('cost_sl_level') or 0), p['t2_value']))
        except Exception as e:
            log.exception('ladder %s', p['key'])
            self.n.throttled('ladder_' + p['key'], 60, '%s: ladder error %s -- tick rules '
                             'still run' % (p['key'], e), 'E')
        ex = rules.tick_exit(p, q, ep, self.cfg, self.ob)                                   # X2-X5
        if ex:
            p['trigger_bid'] = q.bid
            return self._start_exit(p, now, ex)

    # ---- exit (X10-X14, F3/F5/F6/F8/F15/F16) ----
    def _start_exit(self, p: Dict[str, Any], now: _dt.datetime, why: str) -> None:
        p['state'] = 'EXIT'
        p['exit_type'] = why
        p['exit_started'] = self.epoch()
        p['chase'] = self.b.chase_state(p['symbol'], p['token'], p['tick'], int(p['qty'])).to_dict()
        if why in ('EOD', 'FLATTEN', 'NO_PRICE', 'LATE_FILL', 'CARRIED') or not p.get('exit_detail'):
            p['exit_detail'] = {'rule': why, 'how': 'clock_or_control'}
        p['exit_detail']['at'] = now.isoformat()
        self._jrow('EXIT_START', p, why=why, bid=p.get('last_bid'), quote=self._qsnap(self._quote(p)))
        self._exit(p, now)                                # review M1: the sell first
        self.n.send('EXIT %s %s: %s (bid %s, entry %.2f)' % (
            p['symbol'], p['key'], why, p.get('last_bid'), p['entry_premium']),
            'E' if why in ('NO_PRICE', 'LATE_FILL', 'FLATTEN') else 'I')

    def _held(self, p: Dict[str, Any]) -> int:
        """F5/F6: what this position may sell. Own order-book net (+ carried
        lots); the account net lowers it only when it has stayed below for
        longer than positions_lag_s after our last fill. Raises on unreadable."""
        own = self._carried_base(p['symbol']) + int(self.b.own_net(p['symbol']))
        if own <= 0:
            return own
        carry = bool(p.get('carry'))
        try:
            acct = self.b.account_net(p['symbol'])
        except Exception as e:
            if carry:
                raise                                     # review C1: never sell a carry blind
            log.warning('%s: account net unreadable (%s) -- own net only', p['key'], e)
            return own
        if acct is None:
            if carry:
                # review C1: overnight the journal is NOT proof. MIS lots are squared off by
                # the broker; selling a lot that is gone is a naked short. No row -> sell nothing.
                p.setdefault('acct_low_since', self.epoch())
                self.n.throttled('carry_norow_' + p['key'], 60, '%s: carried %d %s but the account '
                                 'shows NO %s row -- nothing sold (broker square-off?)' % (
                                     p['key'], own, p['symbol'], self.cfg['product']), 'E')
                return 0
            log.info('%s: no positions row for %s -- own net %d', p['key'], p['symbol'], own)
            return own                                    # same day: unknown, never 0
        acct = int(acct)
        if acct >= own:
            p.pop('acct_low_since', None)
            return own
        if not carry and self.epoch() - float(p.get('last_fill_epoch') or 0) < self.cfg['positions_lag_s']:
            return own
        p.setdefault('acct_low_since', self.epoch())
        self.n.throttled('acct_' + p['key'], 60, '%s: the ACCOUNT holds %d of %s but our own '
                         'orders say %d -- someone sold our lots? selling at most %d' % (
                             p['key'], acct, p['symbol'], own, max(acct, 0)), 'E')
        return max(acct, 0)

    def _exit_quote(self, p: Dict[str, Any], now: _dt.datetime) -> Optional[Dict[str, float]]:
        t = now.time()
        if t >= self.cfg['stop_placing_time']:
            return None                                   # F16: settle only, place nothing
        if p.get('carry') and t < self.cfg['carry_resume_time']:
            return None
        sess = self.session_fn(now.date())
        if sess is None or t < sess[0]:
            return None
        q = self._quote(p)
        ep = self.epoch()
        if q is not None:
            p['last_bid'] = q.bid
            p['_bids'] = q.bids
            p.pop('exit_noprice_since', None)
            return {'bid': q.bid}
        since = float(p.setdefault('exit_noprice_since', ep))
        if not p.get('stale_exit') and ep - since >= self.cfg['no_price_alert_s']:
            p['stale_exit'] = True                        # review H3: cumulative from the last bid
            self.n.send('%s: no price for %.0f s while selling -- stepping down from the last '
                        'bid %s' % (p['key'], ep - since, p.get('last_bid')), 'E')
        if p.get('stale_exit') and float(p.get('last_bid') or 0) > 0:
            return {'bid': float(p['last_bid'])}
        return None

    def _exit_price(self, p: Dict[str, Any]) -> Callable[[float, int, float], float]:
        def price(bid: float, n: int, tick: float) -> float:
            first = None
            if n == 0:
                if p.get('exit_type') == 'TP':
                    first = bid
                else:
                    first = rules.depth_walk(p.get('_bids') or [], int(p['chase']['remaining'])) or bid
                    first = min(first, bid)
            return rules.exit_price(bid, n, tick, self.cfg, bool(p.get('stale_exit')), first)
        return price

    def _exit(self, p: Dict[str, Any], now: _dt.datetime) -> None:
        st = self.b.chase_from(p['chase'])
        before_sold = st.sold

        def persist(s):
            p['chase'] = s.to_dict()
            sig = [s.working_tag, s.working_no, s.remaining, s.sold, s.reprices, s.status, s.note]
            if sig != p.get('_persist_sig'):              # review H5: no 120 rows a minute
                p['_persist_sig'] = sig
                self._jrow('EXIT_STEP', p, note=s.note)
        t_step = time.time()
        ok, st2 = self._neo(lambda: self.b.chase_step(st, lambda: self._exit_quote(p, now),
                                                      lambda: self._held(p), self._exit_price(p),
                                                      persist, self.epoch), 'exit chase', p)
        if not ok:
            return
        st = st2
        p['chase'] = st.to_dict()
        if st.sold > before_sold:
            p['last_fill_epoch'] = self.epoch()
            self._last_own_fill = self.epoch()
            p['sold'] = st.sold
            p['proceeds'] = st.proceeds
            p['qty'] = st.remaining
            self._jrow('EXIT_FILL', p, sold=st.sold, avg=st.avg_exit,
                       step_ms=round((time.time() - t_step) * 1000.0),
                       trigger_to_fill_s=round(self.epoch() - float(p.get('exit_started') or self.epoch()), 2))
        ep = self.epoch()
        started = float(p.get('exit_started') or ep)
        if st.status == 'done' or st.remaining <= 0:
            return self._closed(p, now)
        if getattr(st, 'rejects', 0) or getattr(st, 'unclears', 0):      # peer review N3/N4
            self.n.throttled('rej_' + p['key'], 60, '%s: exit sell refused/unclear (rejects %d, '
                             'unclears %d): %s -- still trying' % (
                                 p['key'], st.rejects, st.unclears, st.note), 'E')
        if st.troubles >= int(self.cfg['trouble_alert_after']):
            self.n.throttled('trouble_' + p['key'], 60, '%s: exit chase trouble x%d: %s' % (
                p['key'], st.troubles, st.note), 'E')
        if p.get('acct_low_since') and ep - float(p['acct_low_since']) > 30 and \
                'holds 0' in (st.note or ''):
            return self._closed(p, now, external=True)
        if ep - started >= self.cfg['exit_fault_s'] and not p.get('exit_fault') and not p.get('carry'):
            p['exit_fault'] = True
            self._fault('exit chasing > %ss' % self.cfg['exit_fault_s'])
        if ep - started >= self.cfg['exit_alert_every_s']:
            self.n.throttled('chasing_' + p['key'], self.cfg['exit_alert_every_s'],
                             '%s: still selling %d %s (%s, %d re-prices; %s)' % (
                                 p['key'], st.remaining, p['symbol'], p['exit_type'],
                                 st.reprices, st.note), 'E')

    def _closed(self, p: Dict[str, Any], now: _dt.datetime, external: bool = False) -> None:
        st = p['chase'] or {}
        sold, proceeds = int(st.get('sold') or 0), float(st.get('proceeds') or 0.0)
        entry = float(p['entry_premium'])
        p['realised'] = round(proceeds - entry * sold, 2)
        unsold = int(st.get('remaining') if st else p.get('qty') or 0)
        if external and unsold > 0:
            bid = float(p.get('last_bid') or 0)          # review M5: R1 must see the loss
            p['realised'] = round(p['realised'] + (bid - entry) * unsold, 2)
            p['realised_estimated'] = True
        p['exit_avg'] = round(proceeds / sold, 4) if sold else 0.0
        buy_t = entry * (sold + (unsold if external else 0))
        p['charges_est'] = self._charges(buy_t, proceeds)
        p['net_est'] = round(p['realised'] - p['charges_est'], 2)
        lvl = (p.get('exit_detail') or {}).get('level')
        if sold and lvl:
            p['exit_slip_vs_level'] = round(p['exit_avg'] - float(lvl), 4)   # per unit
        p['state'] = 'CLOSED'
        p['qty'] = 0
        p['closed_day'] = now.date().isoformat()
        p['closed_at'] = now.isoformat()
        if external:
            p['exit_type'] = (p.get('exit_type') or '') + '+EXTERNAL'
        self._jrow('CLOSED', p)
        if not p.get('exit_fault') and not external:
            if self.faults:
                self.faults = 0                            # R2: a clean round trip resets
                self._save_day()
        self.feed.unwatch(p['kite_symbol'])
        self.n.send('SOLD %d %s @ %.2f (%s) | entry %.2f | P&L Rs %+.0f | today Rs %+.0f%s' % (
            sold, p['symbol'], p['exit_avg'], p['exit_type'], entry, p['realised'],
            self._realised_today(), ' | CLOSED OUTSIDE THE BOT' if external else ''),
            'E' if external else 'I')

    # -- reconcile (R3-R5, F7, F17) ---------------------------------------------------------
    def _watch_tags_of(self, p: Dict[str, Any]) -> List[str]:
        if p.get('state') != 'UNFILLED' or p.get('day') != (self.day.isoformat() if self.day else ''):
            return []
        done = set(p['entry'].get('adopted') or [])
        return [t for t in p['entry']['tags'] if t not in done]

    def _watch_tags(self) -> bool:
        return any(self._watch_tags_of(p) for p in self.pos.values())

    def _reconcile(self) -> None:
        ok, views = self._neo(self.b.own_orders, 'reconcile')
        if not ok:
            return
        self._last_reconcile = self.epoch()
        first = not self.reconciled
        self.reconciled = True
        by_tag: Dict[str, list] = {}
        for v in views:
            by_tag.setdefault(v.tag, []).append(v)
        now = self._now
        # F17: an unclear / settled entry tag that surfaces
        for p in list(self.pos.values()):
            for tag in self._watch_tags_of(p):
                vs = by_tag.get(tag) or []
                live = [v for v in vs if not v.terminal]
                for v in live:
                    self._neo(lambda v=v: self.b.cancel_confirm(v.order_no, tag), 'late cancel', p)
                    self.n.send('%s: entry %s surfaced RESTING -- cancelled' % (p['key'], tag), 'E')
                filled = sum(v.filled for v in vs)
                if filled > 0 and not live:
                    avg = sum(v.filled * v.avg_price for v in vs) / filled
                    p['entry']['fills'][tag] = [filled, avg]
                    p['entry'].setdefault('adopted', []).append(tag)
                    self._fault('late entry fill %s' % tag)
                    self.feed.watch(p['kite_symbol'], p.get('kite_token'))
                    self._jrow('ADOPT', p, tag=tag, filled=filled, avg=avg)
                    self._opened(p, now, late=True)
        # own nets per symbol vs the book
        nets: Dict[str, int] = {}
        for v in views:
            if v.side in ('B', 'S'):
                nets[v.symbol] = nets.get(v.symbol, 0) + (v.filled if v.side == 'B' else -v.filled)
        for sym in set(list(nets) + [p['symbol'] for p in self._active()]):
            held = self._carried_base(sym) + nets.get(sym, 0)
            mine = [p for p in self._active() if p['symbol'] == sym]
            book = sum(int(p.get('qty') or 0) for p in mine if p['state'] in ('OPEN', 'EXIT'))
            working = any(p['state'] in ('ENTRY', 'EXIT') for p in mine)
            if held < 0:
                self._oversold(sym, -held, views)
                continue
            self._short_reads.pop(sym, None)
            if working or held == book:
                for p in mine:
                    p.pop('recon_low', None)
                continue
            if held < book:
                ok, acct = self._neo(lambda: self.b.account_net(sym), 'reconcile positions')
                for p in mine:
                    if p['state'] != 'OPEN':
                        continue
                    p['recon_low'] = int(p.get('recon_low') or 0) + 1
                    # review H1: one empty / wrong read never abandons a lot. Adopt only on 2
                    # consecutive reads AND a positions row that agrees.
                    agree = ok and acct is not None and int(acct) <= max(held, 0)
                    self.n.throttled('recon_' + p['key'], 120, '%s: own orders show %d of %s, '
                                     'book %d, account %s -- %s' % (
                                         p['key'], held, sym, book, acct,
                                         'adopting' if agree and p['recon_low'] >= 2 else 'holding'), 'E')
                    if not (agree and p['recon_low'] >= 2):
                        continue
                    p['qty'] = max(0, held)
                    if p['qty'] == 0:
                        p['chase'] = self.b.chase_state(p['symbol'], p['token'], p['tick'], 0).to_dict()
                        self._closed(p, now, external=True)
                    else:
                        self._jrow('RECON_QTY', p, held=held)
            elif held > book:
                self.n.throttled('recon_long_' + sym, 300, 'own etf- orders hold %d of %s but the '
                                 'book has %d -- UNEXPLAINED; check the app' % (held, sym, book), 'E')
        if first:
            self.n.send('executor up (%s, %d lot): reconciled %d own orders today; %d active' % (
                self.cfg['mode'], self.cfg['lots'], len(views), len(self._active())))

    def _oversold(self, sym: str, short: int, views: list) -> None:
        """R4 / Q7 / F7: own net < 0 = we sold more than we bought. Cover only on 2
        consecutive reads, a whole number of lots, no live etf- order, the last own
        sell < cover_window_s ago, at most once per position; else alert only."""
        n = self._short_reads.get(sym, 0) + 1
        self._short_reads[sym] = n
        ps = [p for p in self.pos.values() if p.get('symbol') == sym]
        p = max(ps, key=lambda x: x.get('closed_at') or x.get('day') or '') if ps else None
        msg = 'OVERSOLD %d of %s (own etf- orders)' % (short, sym)
        live = [v for v in views if not v.terminal]
        lot = int(p['lot_size']) if p else 0
        recent = p is not None and self.epoch() - float(p.get('last_fill_epoch') or 0) <= self.cfg['cover_window_s']
        if n < 2 or live or not p or short % lot or p.get('covered') or not recent:
            self.n.throttled('short_' + sym, 60, msg + (' -- buy-to-cover pending a second read'
                                                        if n < 2 else ' -- ALERT ONLY: cover by hand'), 'E')
            return
        q = self._quote(p)                                # fresh + valid + age-checked
        if q is None:
            self.n.throttled('short_' + sym, 60, msg + ' -- no fresh price to cover', 'E')
            return
        tag = self.b.new_tag()
        p['covered'] = tag
        self._jrow('COVER_SEND', p, tag=tag, qty=short, limit=q.ask)
        ok, no = self._neo(lambda: self.b.place(sym, p['token'], 'B', short,
                                                rules.round_up(q.ask, p['tick']), tag), 'cover', p)
        self.n.send(msg + (' -- BUY-TO-COVER sent %s' % no if ok else ' -- cover NOT sent'), 'E')

    # -- status / heartbeat (H1, H4) -----------------------------------------------------------
    def _status(self, now: _dt.datetime) -> None:
        ep = self.epoch()
        if ep - self._last_status < self.cfg['status_every_s']:
            return
        self._last_status = ep
        st = {'at': now.isoformat(), 'epoch': ep, 'pid': os.getpid(), 'mode': self.cfg['mode'],
              'lots': self.cfg['lots'], 'live_until': self.cfg['live_until'],
              'day': self.day.isoformat() if self.day else None,
              'realised_today': self._realised_today(), 'faults': self.faults, 'halt': self.halt,
              'kill': self._file('KILL'), 'flatten': self._file('FLATTEN'),
              'journal_corrupt': self.corrupt, 'reconciled': self.reconciled,
              'ws_ok': self.feed.ws_healthy(), 'ws_error': self.feed.ws_error(),
              'consts': getattr(self, 'consts_digest', ''),
              'neo_ms': {k: {'n': v[0], 'avg': round(v[1] / v[0]) if v[0] else 0, 'max': round(v[2])}
                         for k, v in self.neo_stats.items()},
              'active': [{k: p.get(k) for k in ('key', 'state', 'symbol', 'qty', 'entry_premium',
                                                 'exit_type', 'carry')} for p in self._active()]}
        tmp = os.path.join(self.state_dir, 'status.json.tmp')
        try:
            with io.open(tmp, 'w', encoding='utf-8') as f:
                json.dump(st, f, indent=1, default=str)
            os.replace(tmp, os.path.join(self.state_dir, 'status.json'))
        except OSError as e:
            log.warning('status write failed: %s', e)
        if self.deadman is not None:
            self.deadman.ping()
