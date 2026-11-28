"""The live naked-long EXIT engine (naked live build P4).

Design: `docs/NAKED_LIVE_DESIGN.md` Q1, Q3, G7-G9, F12-F15, G14 and the
2026-11-27 status box (orders on Kotak NEO, a MONITORED stop, no
`close_leg`). Runs inside `bcs/spread_monitor.py`'s 5-second loop: the one
long-lived process, so a chase can be stepped every poll.

THE RULES ARE NOT HERE. They are `zebra/naked_rules.step`, the same function
the 100-trade test's `naked_long` arm runs, so the position live holds and the
arm the test scores cannot drift (Q1). This module only supplies the inputs on
the right clock and turns a verdict into a sale:

  * TP on SPOT, read every poll (Q3: "TP on the 5 s poll").
  * STOP (-50% on the long's BID, 2 confirming polls) and TIME (6 sessions
    before expiry) are evaluated ONCE per 5-minute wall-clock bucket, at the
    first poll inside it (G7) -- the cadence the shadow samples on. Between
    buckets `step` runs with the stop and the calendar switched off, so the
    debounce counts the same polls it counts in the test.
  * A value is a reliable BID or nothing (Q1's only kept guard: an unusable
    book defers; never a guess).

THE SALE. A fired exit takes the store's close lock (`begin_close`, status
`closing`) and records the trigger with the BID at that moment (`exit_touch`,
which the G14 slippage kill reads). Then:

  * rehearsal record: fills at that bid, booked at once. Nothing is sent.
  * live record: `kotak_neo.orders.chase_exit`, one step per poll -- limit sell
    at the bid, re-priced a tick lower every `naked_exit_reprice_sec`, positions
    are the authority before every sell, the chase state (with the order's tag)
    is persisted on the record BEFORE each send. A monitor restart resumes the
    chase from the record; it never starts a second one.
  * when the chase is flat: booked at the average fill (`mark_exited`), fees
    at the NEO rate (`fees.round_trip_for_trade`).

EXITS NEVER DEPEND ON THE ENTRY SWITCH. Every open live record is watched
whatever `naked_live` says now -- turning entries off must not orphan a
position. Under the monitor's `--dry-run` a live record cannot be sold: the
engine says so loudly once a day per record (the arming preflight reports the
same state as NO ENGINE).

Never raises into the monitor loop.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional

from . import config as cfg
from . import naked_rules
from . import strikes as strikes_mod
from .trade_store import ZebraStore, is_naked

logger = logging.getLogger(__name__)

OPEN_STATES = ('entered', 'closing')
#: Consecutive polls a record may raise before the owner is told it is unwatched.
RAISE_ALERT_POLLS = 3
#: A live chase with no bid for this long (nothing working, nothing selling)
#: is said out loud once: an exit that cannot price is an exit not happening.
NO_BID_ALERT_SEC = 60.0
#: Below this many sessions to expiry an open naked long is past its own TIME
#: exit and into the delivery window (F15): physically settled stock options.
DELIVERY_ALERT_SESSIONS = 4


def bucket_of(now: datetime) -> str:
    return '%s-%02d' % (now.strftime('%Y%m%d%H'), now.minute // 5)


class NakedExitEngine:
    def __init__(self, *, dry_run: bool, log: Callable = None, send: Callable = None,
                 store: Optional[ZebraStore] = None, client=None,
                 quote_fn: Optional[Callable] = None, spot_fn: Optional[Callable] = None,
                 clock: Optional[Callable] = None, now_fn: Optional[Callable] = None):
        self.dry_run = bool(dry_run)
        self.log = log if log is not None else (lambda m: logger.info(m))
        self.send = send if send is not None else (
            lambda m: logger.warning('NAKED (no sender): %s', m))
        self._store = store
        self._client = client
        self._quote_fn = quote_fn          # (kite, symbol) -> quote dict
        self._spot_fn = spot_fn            # (kite, [stocks]) -> {stock: ltp}
        self._clock = clock or time.time
        self._now = now_fn or (lambda: datetime.now(cfg.IST).replace(tzinfo=None))
        self._mtime = None
        self._buckets: Dict[int, str] = {}   # record id -> last full-bucket evaluated
        self._raises: Dict[int, int] = {}    # record id -> consecutive polls that raised
        self._nagged: Dict[int, str] = {}    # record id -> day the stuck-exit nag went out

    # -- store ------------------------------------------------------------------
    @property
    def store(self) -> ZebraStore:
        if self._store is None:
            s = ZebraStore()
            s.initialize()
            self._store = s
            self._mtime = self._file_mtime()
        return self._store

    @staticmethod
    def _file_mtime():
        try:
            return cfg.LOCAL_FILE.stat().st_mtime
        except OSError:
            return None

    def _refresh(self) -> None:
        """Re-read the local book when another process (zebra's cron) wrote it.
        A stat per poll; the 3.6 MB parse only on change."""
        m = self._file_mtime()
        if m is not None and m != self._mtime:
            try:
                self.store.reload()
            except Exception as e:
                self.log('  NAKED: store reload failed (%s) -- using the cached book' % e)
            self._mtime = m

    def records(self) -> List[dict]:
        """Every OPEN naked record, live and rehearsal, whatever the mode."""
        self._refresh()
        # WHOLE BOOK: the exit engine must see every naked record it may own.
        return [t for t in self.store.load_trades()
                if is_naked(t) and t.get('status') in OPEN_STATES]

    def open_count(self) -> int:
        try:
            return len(self.records())
        except Exception as e:
            self.log('  NAKED: cannot read the book (%s)' % e)
            return 0

    def has_active_chase(self) -> bool:
        try:
            return any(t.get('status') == 'closing' and not t.get('rehearsal')
                       for t in self.records())
        except Exception:
            return False

    def live_population(self) -> bool:
        """True when a LIVE (not rehearsal) naked record is open -- arming input."""
        try:
            return any(not t.get('rehearsal') for t in self.records())
        except Exception:
            return True                     # unknown must not certify safety

    @property
    def client(self):
        if self._client is None:
            from . import naked_neo
            self._client = naked_neo.default_client()
        return self._client

    # -- inputs ------------------------------------------------------------------
    def _quote(self, kite, sym: str) -> Optional[dict]:
        try:
            q = (self._quote_fn or strikes_mod._quote_option)(kite, sym)
        except Exception as e:
            self.log('  NAKED quote %s failed: %s' % (sym, e))
            return None
        if not q or q.get('error'):
            return None
        return q

    @staticmethod
    def _value(q: Optional[dict]) -> Optional[float]:
        """What selling the long pays now: a RELIABLE bid, or None."""
        if not q or not q.get('reliable'):
            return None
        bid = q.get('bid')
        try:
            bid = float(bid)
        except (TypeError, ValueError):
            return None
        return bid if bid > 0 else None

    def _spots(self, kite, stocks: List[str]) -> Dict[str, float]:
        if self._spot_fn is not None:
            try:
                return self._spot_fn(kite, stocks) or {}
            except Exception as e:
                self.log('  NAKED spot fetch failed: %s' % e)
                return {}
        keys = ['NSE:%s' % s for s in stocks]
        try:
            d = kite.ltp(keys)
        except Exception as e:
            self.log('  NAKED spot fetch failed: %s' % e)
            return {}
        return {k[4:]: float(v.get('last_price') or 0) for k, v in (d or {}).items()
                if v.get('last_price')}

    # -- the poll ------------------------------------------------------------------
    def poll(self, kite, now: Optional[datetime] = None,
             closing_only: bool = False) -> List[str]:
        """One poll of every open naked record. Returns action lines. Never raises.

        `closing_only` (the 15:30-15:40 extension): step exits already selling,
        evaluate no NEW trigger -- the shadow's window ends at 15:30 too."""
        now = now or self._now()
        out = []
        try:
            recs = self.records()
        except Exception as e:
            self.log('  NAKED: book unreadable (%s) -- exits NOT evaluated this poll' % e)
            return ['book unreadable']
        if closing_only:
            recs = [t for t in recs if t.get('status') == 'closing']
        if not recs:
            return out
        try:
            strikes_mod.prefetch_quotes(kite, [t['long_symbol'] for t in recs])
        except Exception as e:
            self.log('  NAKED prefetch failed: %s' % e)
        spots = self._spots(kite, sorted({t['stock'] for t in recs}))
        for t in recs:
            try:
                self._nag_stuck(t, now)
                line = (self._step_closing(kite, t, now) if t['status'] == 'closing'
                        else self._step_open(kite, t, spots.get(t['stock']), now))
                self._raises.pop(t['id'], None)
            except Exception as e:
                logger.error('NAKED #%s raised: %s', t.get('id'), e, exc_info=True)
                line = '#%s raised: %s' % (t.get('id'), e)
                n = self._raises.get(t['id'], 0) + 1
                self._raises[t['id']] = n
                if n == RAISE_ALERT_POLLS:
                    # A record that raises every poll is silently UNWATCHED.
                    self.send('\U0001F6A8 NAKED #%s %s: its exit check has FAILED %d polls '
                              'in a row (%s) -- it is NOT being watched. Check it / exit by '
                              'hand.' % (t.get('id'), t.get('stock'), n, str(e)[:120]))
            if line:
                self.log('  NAKED %s' % line)
                out.append(line)
        return out

    def _step_open(self, kite, t: dict, spot: Optional[float], now: datetime) -> str:
        q = self._quote(kite, t['long_symbol'])
        v = self._value(q)
        today = now.date()
        bucket = bucket_of(now)
        rule = dict(t.get('exit_rule') or {})
        rule.setdefault('entry_value', float(t['debit']))
        before = json.dumps(rule, sort_keys=True, default=str)
        # The bucket is remembered in memory and written to the record only with
        # a real state change (a confirm bump, a latch): a restart mid-bucket
        # after a bump sees the bucket on the record and does not count twice.
        seen = self._buckets.get(t['id']) or rule.get('bucket')
        full = seen != bucket
        left = naked_rules.sessions_left(t.get('expiry'), today)
        res = naked_rules.step(
            rule, take_tp=True,
            stop_frac=float(t.get('stop_frac') or cfg.DEBIT_SL_PCT) if full else None,
            v=v, spot=spot, direction=t['direction'], target=float(t['tp_spot']),
            now=now, today=today, left=left if full else None,
            value_armed=not naked_rules.within_open_buffer(now))
        if full:
            self._buckets[t['id']] = bucket
        self._delivery_check(t, left, today)
        if res is None:
            if json.dumps(rule, sort_keys=True, default=str) != before:
                if full:
                    rule['bucket'] = bucket
                self.store.update_trade_fields(t['id'], _drive=False, exit_rule=rule)
            return ''
        reason, value = res
        return self._begin_exit(kite, t, reason, value, q, spot, now, rule)

    def _delivery_check(self, t: dict, left: Optional[int], today: date) -> None:
        if left is None or left > DELIVERY_ALERT_SESSIONS:
            return
        if t.get('delivery_alerted') == today.isoformat():
            return
        self.send('\U0001F6A8 NAKED #%s %s %s is still OPEN %d session(s) from expiry -- '
                  'past its TIME exit and inside the physical-delivery window. Close it.'
                  % (t['id'], t['stock'], t['long_symbol'], left))
        self.store.update_trade_fields(t['id'], delivery_alerted=today.isoformat())

    # -- exits ----------------------------------------------------------------------
    def _begin_exit(self, kite, t, reason, value, q, spot, now, rule) -> str:
        live = not t.get('rehearsal')
        tag = '%s #%s %s' % ('LIVE' if live else 'REHEARSAL', t['id'], t['stock'])
        if live and self.dry_run:
            day = now.strftime('%Y-%m-%d')
            if t.get('dry_exit_alerted') != day:
                self.send('\U0001F6A8 NAKED %s: %s fired but the monitor is in --dry-run '
                          '-- NOTHING is selling a real position. Exit it in the NEO app '
                          'or take --dry-run off the crontab.' % (tag, reason.upper()))
                self.store.update_trade_fields(t['id'], dry_exit_alerted=day, exit_rule=rule)
            return '%s %s WOULD exit (dry-run)' % (tag, reason)
        if not self.store.begin_close(t['id'], 'naked:%s' % reason):
            return '%s: close lock not taken' % tag
        touch = value if value is not None else (q or {}).get('bid')
        trig = {'reason': reason, 'at': now.strftime('%Y-%m-%d %H:%M:%S'),
                'value': value, 'spot': spot, 'touch_bid': touch,
                'touch_ask': (q or {}).get('ask')}
        fields = dict(exit_rule=rule, exit_trigger=trig, exit_touch=touch)
        if not live:
            self.store.update_trade_fields(t['id'], **fields)
            return self._book(t, reason, value, spot, {'side': 'SELL', 'price': value,
                                                       'bid': touch, 'rehearsal': True})
        try:
            st = self._new_chase(t)
        except Exception as e:
            # Cannot even name the contract at NEO. The close lock is held, so
            # nothing else will try; a human must sell it.
            fields['exit_chase_error'] = str(e)[:200]
            self.store.update_trade_fields(t['id'], **fields)
            self.send('\U0001F6A8 NAKED %s %s fired but the NEO contract cannot be '
                      'resolved (%s). SELL %s x %s IN THE NEO APP.'
                      % (tag, reason.upper(), e, t['long_symbol'], t['quantity']))
            return '%s: chase could not start: %s' % (tag, e)
        fields['exit_chase'] = st.to_dict()
        self.store.update_trade_fields(t['id'], **fields)
        self.send('⚠ NAKED %s %s fired @ bid %s (spot %s) -- selling %s x %s on NEO'
                  % (tag, reason.upper(), touch, spot, t['long_symbol'], t['quantity']))
        fresh = self.store.find(t['id'])
        return self._step_closing(kite, fresh, now)

    def _new_chase(self, t: dict):
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        sym, tok = t.get('neo_symbol'), t.get('neo_token')
        if not sym or not tok:
            row = naked_neo.scrip_row(self.client, t)
            sym, tok = row.trading_symbol, str(row.token)
        return orders.ChaseState(bot=naked_neo.BOT, symbol=sym, token=str(tok),
                                 tick=float(cfg.NAKED_OPTION_TICK),
                                 remaining=int(t['quantity']))

    def _policy(self):
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        return orders.ChasePolicy(reprice_after_s=float(cfg.NAKED_EXIT_REPRICE_SEC),
                                  max_reprices=int(cfg.NAKED_EXIT_MAX_REPRICES),
                                  confirm_wait_s=0.0)

    def _step_closing(self, kite, t: dict, now: datetime) -> str:
        tag = '%s #%s %s' % ('LIVE' if not t.get('rehearsal') else 'REHEARSAL',
                             t['id'], t['stock'])
        if t.get('rehearsal'):
            # A rehearsal close is booked in the same call that began it; a
            # rehearsal record found `closing` means a crash in between. Book it
            # at the trigger's value -- there is no broker to ask.
            trig = t.get('exit_trigger') or {}
            value = trig.get('value')
            if value is None:
                # Crash before the trigger was written: price it NOW at the bid
                # rather than booking a -100% that never happened (review R7).
                value = self._value(self._quote(kite, t['long_symbol']))
                if value is None:
                    return '%s: closing (recovered), waiting for a usable bid' % tag
            return self._book(t, _reason_of(t), value, trig.get('spot'),
                              {'side': 'SELL', 'price': value, 'rehearsal': True,
                               'recovered': True})
        if self.dry_run:
            day = now.strftime('%Y-%m-%d')
            if t.get('dry_exit_alerted') != day:
                self.send('\U0001F6A8 NAKED %s is mid-EXIT but the monitor is now in DRY RUN '
                          '(kill switch / --dry-run): nothing re-prices or sells. Any working '
                          'sell rests at NEO. Finish the exit in the NEO app, then book it: '
                          'python -m zebra close %s --exit-debit <avg fill> --reason %s'
                          % (tag, t['id'], _reason_of(t)))
                self.store.update_trade_fields(t['id'], dry_exit_alerted=day)
            return '%s: closing, but --dry-run -- the chase is not stepped' % tag
        if not t.get('exit_chase'):
            # A crash (or a failed write) between `begin_close` and the chase
            # state. Start the chase now; if it cannot start, say so ONCE.
            try:
                st0 = self._new_chase(t)
            except Exception as e:
                if not t.get('exit_chase_alerted'):
                    self.send('🚨 NAKED %s is CLOSING with no exit order and the NEO '
                              'contract cannot be resolved (%s). SELL %s x %s IN THE NEO APP.'
                              % (tag, e, t['long_symbol'], t['quantity']))
                    self.store.update_trade_fields(t['id'], exit_chase_alerted=True,
                                                   exit_chase_error=str(e)[:200])
                return '%s: closing, chase cannot start: %s' % (tag, e)
            self.store.update_trade_fields(t['id'], exit_chase=st0.to_dict())
            self.send('⚠ NAKED %s was CLOSING with no exit order (restart?) -- the '
                      'chase starts now.' % tag)
            t = self.store.find(t['id'])
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        st = orders.ChaseState.from_dict(t['exit_chase'])
        st = self._overnight(t, st, now, orders)
        was = (st.status, st.rejects, st.unclears)
        touch = t.get('exit_touch')
        floor = 0.5 * float(touch) if touch else None
        quote = naked_neo.quote_bid_fn(kite, t['long_symbol'], floor=floor)
        tid = t['id']

        def persist(s):
            # Local book only: both processes on the box read it under the
            # store lock, and it is written BEFORE every send (the tag).
            self.store.update_trade_fields(tid, _drive=False, exit_chase=s.to_dict())
        st = orders.chase_exit(self.client, st, quote, self._policy(), persist=persist,
                               clock=self._clock)
        if st.status == 'done':
            if st.sold <= 0:
                self.send('\U0001F6A8 NAKED %s: the chase ended with NOTHING sold -- the '
                          'account no longer held it. Check the NEO app; record left '
                          'closing.' % tag)
                persist(st)
                return '%s: chase done with 0 sold -- human' % tag
            trig = t.get('exit_trigger') or {}
            spot = trig.get('spot')
            return self._book(t, _reason_of(t), round(st.avg_exit, 2), spot,
                              {'side': 'SELL', 'price': round(st.avg_exit, 2),
                               'bid': t.get('exit_touch'), 'fills': st.fills,
                               'reprices': st.reprices})
        self._no_bid_watch(t, st, tag)
        if st.status == 'needs_human' and was[0] != 'needs_human':
            self.send('\U0001F6A8 NAKED %s: the exit chase STOPPED (%s). Sold %d, %d left. '
                      'SELL THE REST IN THE NEO APP.' % (tag, st.note, st.sold, st.remaining))
        elif st.rejects >= 3 and was[1] < 3:
            self.send('\U0001F534 NAKED %s: 3 exit sells refused in a row (%s). Still trying.'
                      % (tag, st.note))
        elif st.unclears >= 3 and was[2] < 3:
            self.send('\U0001F534 NAKED %s: 3 exit sells UNCLEAR in a row (%s). Check the '
                      'NEO order book.' % (tag, st.note))
        return '%s: chase %s (%s)' % (tag, st.status, st.note)

    def _overnight(self, t, st, now, orders):
        """A sell still `working` from an EARLIER session (review R1): NEO's
        DAY order expired at the close and is absent from today's book, which
        the chase would count as 'vanished' and hand to a human within a
        minute. The ACCOUNT position settles it: whatever is still held is
        what remains to sell; anything that filled unseen is marked
        approximate rather than guessed."""
        if not st.working_tag or not st.working_since:
            return st
        # Both sides from the SAME clock the chase stamps `working_since` with.
        started = datetime.fromtimestamp(float(st.working_since)).date()
        if started >= datetime.fromtimestamp(float(self._clock())).date():
            return st
        held = self.client.net_held(st.symbol)        # Unavailable -> raises, retried next poll
        unseen = max(0, st.remaining - max(held, 0))
        self.log('  NAKED #%s: sell %s working since %s expired overnight; account holds %d'
                 % (t['id'], st.working_tag, started, held))
        st.working_tag = st.working_no = ''
        st.working_qty = 0
        st.working_since = 0.0
        st.cancel_sent = False
        st.troubles = 0
        if unseen:
            st.remaining -= unseen
            st.note = '%d share(s) left the account overnight at an unknown price' % unseen
            self.store.update_trade_fields(t['id'], _drive=False, exit_overnight_unseen=unseen)
        self.store.update_trade_fields(t['id'], _drive=False, exit_chase=st.to_dict())
        return st

    def end_of_session(self) -> List[str]:
        """At the monitor's final close: cancel every live working sell and read
        it back, so no order is left resting into the next session. Never raises."""
        out = []
        try:
            from . import naked_neo
            _, orders, _ = naked_neo.neo()
            recs = [t for t in self.records()
                    if t.get('status') == 'closing' and not t.get('rehearsal')]
        except Exception as e:
            return ['end_of_session: %s' % e]
        for t in recs:
            try:
                st = orders.ChaseState.from_dict(t.get('exit_chase') or {})
            except Exception:
                continue
            if not st.working_tag or not st.working_no or self.dry_run:
                continue
            try:
                res = orders.cancel_and_confirm(self.client, st.working_no, st.working_tag,
                                                wait_s=5.0, expect_qty=st.working_qty or None)
                if res.terminal:
                    orders._apply(st, res)
                    st.cancel_sent = False
                self.store.update_trade_fields(t['id'], _drive=False, exit_chase=st.to_dict())
                out.append('#%s: end of session, working sell %s' % (t['id'], res.state))
            except Exception as e:
                out.append('#%s: end-of-session cancel failed: %s' % (t['id'], e))
        return out

    def _nag_stuck(self, t, now) -> None:
        """Once a day per record whose exit needs a human (review R2): the
        one-off alert is easy to miss and a restarted monitor forgets it."""
        ch = t.get('exit_chase') or {}
        stuck = t.get('status') == 'closing' and not t.get('rehearsal') and (
            ch.get('status') == 'needs_human' or (t.get('exit_chase_error') and not ch))
        if not stuck:
            return
        day = now.strftime('%Y-%m-%d')
        if self._nagged.get(t['id']) == day or t.get('stuck_nag_day') == day:
            return
        self._nagged[t['id']] = day
        self.send('\U0001F6A8 NAKED #%s %s is STILL mid-exit and needs a human (%s). Sold %s, '
                  '%s left. Sell the rest in the NEO app, then book it: python -m zebra close '
                  '%s --exit-debit <avg fill per share> --reason %s'
                  % (t['id'], t['stock'], ch.get('note') or t.get('exit_chase_error'),
                     ch.get('sold', 0), ch.get('remaining', t.get('quantity')), t['id'],
                     _reason_of(t)))
        self.store.update_trade_fields(t['id'], _drive=False, stuck_nag_day=day)

    def _no_bid_watch(self, t, st, tag) -> None:
        stalled = (st.status == 'active' and not st.working_tag
                   and str(st.note).startswith('no bid'))
        since = t.get('exit_nobid_since')
        if not stalled:
            if since:
                self.store.update_trade_fields(t['id'], _drive=False, exit_nobid_since=None)
            return
        now = self._clock()
        if not since:
            self.store.update_trade_fields(t['id'], _drive=False, exit_nobid_since=now)
            return
        if now - float(since) >= NO_BID_ALERT_SEC and not t.get('exit_nobid_alerted'):
            self.send('🔴 NAKED %s: the exit has had NO usable bid for %.0fs -- '
                      'nothing is selling. Sold %d, %d left. Check the book / sell in the app.'
                      % (tag, now - float(since), st.sold, st.remaining))
            self.store.update_trade_fields(t['id'], exit_nobid_alerted=True)

    def _book(self, t, reason, value, spot, leg) -> str:
        tag = '%s #%s %s' % ('LIVE' if not t.get('rehearsal') else 'REHEARSAL',
                             t['id'], t['stock'])
        booked = self.store.mark_exited(t['id'], exit_spot=spot, exit_debit=value,
                                        reason=reason,   # 'tp' | 'stop' | 'time': outcomes vocabulary
                                        exit_legs={'long': leg},
                                        approximate=bool(t.get('exit_overnight_unseen')))
        self.send('%s NAKED %s %s: sold @ %s -> %+.1f%% (net Rs %s)' % (
            '\U0001F7E2' if (booked.get('pnl') or 0) > 0 else '\U0001F534', tag,
            reason.upper(), value, booked.get('pnl_pct') or 0.0, booked.get('pnl_net')))
        return '%s booked %s @ %s' % (tag, reason, value)


def _reason_of(t: dict) -> str:
    """The exit's reason from the trigger, else from the close lock
    (`naked:<reason>`), so a crash between the two never books 'unknown'."""
    r = (t.get('exit_trigger') or {}).get('reason')
    if r:
        return r
    cr = str(t.get('close_reason') or '')
    return cr.split(':', 1)[1] if cr.startswith('naked:') and ':' in cr else 'manual'


def market_really_open(kite, probe: str = 'NSE:NIFTY 50',
                       now: Optional[datetime] = None) -> Optional[bool]:
    """On a day the calendar calls a holiday: does Kite show today's prints?

    True when the index's last trade is from today (the calendar is WRONG and
    the exits must run), False when it is from an earlier day, None when it
    cannot be read. Evidence for a closure is the exchange's own prints, never
    a log line (CLAUDE.md, holiday calendar correction 2026-11-24).
    """
    now = now or datetime.now(cfg.IST).replace(tzinfo=None)
    try:
        d = kite.quote([probe])[probe]
        # The last TRADE, never the quote `timestamp`: a quote can be stamped
        # today on a closed day (review R12).
        ts = d.get('last_trade_time')
    except Exception as e:
        logger.warning('holiday probe %s failed: %s', probe, e)
        return None
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts[:19])
        except ValueError:
            return None
    return ts.date() == now.date()
