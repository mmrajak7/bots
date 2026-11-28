"""The live naked-long EXIT engine (naked live build P4, review fixes P7e).

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
    book defers; never a guess). A position that stays BLIND (no usable bid
    or no spot) for `naked_blind_alert_sec` is said out loud (review F1).

THE SALE. A fired exit takes the store's close lock (`begin_close`, status
`closing`) and records the trigger with the BID at that moment (`exit_touch`,
which the G14 slippage kill reads) -- both LOCAL-only writes, so no Drive
upload stands between a stop and its first sell (review A5). Then:

  * rehearsal record: fills at that bid, booked at once. Nothing is sent.
  * live record: `kotak_neo.orders.chase_exit`, one step per poll -- limit sell
    at the bid, re-priced a tick lower every `naked_exit_reprice_sec`, at most
    `naked_exit_max_offset_ticks` under the bid. Positions are the authority
    before every sell, and the chase state (with the order's tag) is persisted
    on the record BEFORE each send. A monitor restart resumes the chase from
    the record; it never starts a second one.
  * THE CHASE NEVER GIVES UP (review H1/A6/F4, 2026-11-28). A minute of NEO
    blips used to hand a real stop to a human for good. Now troubles and
    re-prices only ALERT (at `naked_exit_alert_steps`, again every
    `naked_exit_alert_repeat_sec`) while the chase keeps going.
  * WHAT A WIDE BOOK MAY BE SOLD INTO (owner delegated to the trader,
    2026-11-28). A RELIABLE book is always sold at its bid -- that is what the
    paper arm books. An UNRELIABLE one only at a bid >= `naked_exit_floor_stop`
    (0.5) x the trigger bid for a STOP, and >= `naked_exit_floor_target` (0.9)
    for TP/TIME: a winner is not dumped into a pulled book. The TP/TIME floor
    decays to the stop's after `naked_exit_floor_decay_sec`, and at once inside
    the delivery window, so no exit can wait forever.
  * A CHASE CARRIED OVERNIGHT waits out the opening buffer (the same 15
    minutes the value triggers stand down) and starts the session with its
    re-price count reset (review M3).
  * when the chase is flat: booked at the average fill (`mark_exited`), fees
    at the NEO rate (`fees.round_trip_for_trade`).

A HAND SALE STOPS THE BOT SELLING THOSE SHARES (trader re-review N1). Because
the chase never gives up, an owner who sells in the app while it runs must not
be followed by a second bot sell sized off a positions API that lags the fill
-- that is a NAKED SHORT. So every step reads the ORDER BOOK (which shows a
fill at once) for sells on the contract NOT tagged by zebra: their filled
quantity comes off `remaining`, any working bot sell is cancelled, and the
record is left for a human to book at the true average (`zebra close`).

NO WORKING SELL IS EVER ORPHANED (review A1/A2). When the chase cannot step --
the monitor is in dry run or the kill switch tripped -- its working sell is
cancelled and read back, so a human selling in the app cannot be followed by
the bot's old order filling. `zebra close` on a naked record refuses while a
sell is working or the account still holds the lot (`hand_close_problem`).

EXITS NEVER DEPEND ON THE ENTRY SWITCH. Every open live record is watched
whatever `naked_live` says now -- turning entries off must not orphan a
position. Under the monitor's `--dry-run` a live record cannot be sold: the
engine says so loudly once a day per record (the arming preflight reports the
same state as NO ENGINE).

Every alert writes its once-flag BEFORE it sends (review F12): a failing write
must not turn a once-a-day alert into one every 5 seconds.

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


def bucket_of(now: datetime) -> str:
    return '%s-%02d' % (now.strftime('%Y%m%d%H'), now.minute // 5)


def _due(last: Optional[float], now: float) -> bool:
    """A repeating alert is due: never sent, or sent a repeat-interval ago."""
    return last is None or now - float(last) >= float(cfg.NAKED_EXIT_ALERT_REPEAT_SEC)


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
        self._raise_alerted: Dict[int, float] = {}
        self._blind: Dict[int, float] = {}   # record id -> clock when it went blind
        self._blind_alerted: Dict[int, float] = {}
        self._nagged: Dict[int, str] = {}    # record id -> day the stuck-exit nag went out
        # Import the NEO code NOW (review F9): a process started before a pull
        # must not meet newer NEO code for the first time at the day's first stop.
        self.neo_error = None
        try:
            from . import naked_neo
            naked_neo.neo()
        except Exception as e:
            # Review A3: remembered, so the heartbeat can say this engine
            # CANNOT sell (the live entry gate then refuses) instead of
            # claiming an armed engine.
            self.neo_error = str(e) or type(e).__name__
            self.log('  NAKED: NEO package not importable (%s) -- live exits cannot sell' % e)

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
        if m is not None and m != self._mtime and m == getattr(self.store, 'saved_mtime', None):
            # Our OWN last save (review A1): the cache already holds it.
            self._mtime = m
            return
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
        """Open naked records. An UNREADABLE book counts as one (review F8):
        the monitor reads 0 as "nothing to watch" and exits, and an unknown
        book must keep it alive and loud, never quietly gone."""
        try:
            return len(self.records())
        except Exception as e:
            self.log('  NAKED: cannot read the book (%s) -- staying alive' % e)
            return 1

    def has_active_chase(self) -> bool:
        try:
            return any(t.get('status') == 'closing' and not t.get('rehearsal')
                       for t in self.records())
        except Exception:
            return True                     # unknown: keep the post-close loop alive

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
                self._raise_alerted.pop(t['id'], None)
            except Exception as e:
                logger.error('NAKED #%s raised: %s', t.get('id'), e, exc_info=True)
                line = '#%s raised: %s' % (t.get('id'), e)
                n = self._raises.get(t['id'], 0) + 1
                self._raises[t['id']] = n
                clk = self._clock()
                if n >= cfg.NAKED_RAISE_ALERT_POLLS and _due(
                        self._raise_alerted.get(t['id']), clk):
                    # A record that raises every poll is silently UNWATCHED.
                    self._raise_alerted[t['id']] = clk
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
        stop_frac = float(t.get('stop_frac') or cfg.DEBIT_SL_PCT)
        res = naked_rules.step(
            rule, take_tp=True,
            stop_frac=stop_frac if full else None,
            v=v, spot=spot, direction=t['direction'], target=float(t['tp_spot']),
            now=now, today=today, left=left if full else None,
            value_armed=not naked_rules.within_open_buffer(now))
        if full:
            self._buckets[t['id']] = bucket
            # One POLL line per position per bucket (review F11): "why did the
            # stop not fire at 11:20" must have an answer in the log.
            self.log('  NAKED POLL #%s %s bid %s ask %s%s spot %s tp %s stop<=%.2f '
                     'confirms %s left %s' % (
                         t['id'], t['long_symbol'], (q or {}).get('bid'), (q or {}).get('ask'),
                         '' if v is not None else ' [UNUSABLE]', spot, t.get('tp_spot'),
                         stop_frac * float(t['debit']),
                         ((rule.get('confirm') or {}).get('stop') or {}).get('n', 0),
                         left))
        self._blind_watch(t, v, spot)
        self._delivery_check(t, left, today)
        if res is None:
            if json.dumps(rule, sort_keys=True, default=str) != before:
                if full:
                    rule['bucket'] = bucket
                self.store.update_trade_fields(t['id'], _drive=False, exit_rule=rule)
            return ''
        reason, value = res
        return self._begin_exit(kite, t, reason, value, q, spot, now, rule)

    def _blind_watch(self, t: dict, v: Optional[float], spot: Optional[float]) -> None:
        """Review F1: a position with no usable bid (stop and TIME dark) or no
        spot (TP dark) for `naked_blind_alert_sec` is said out loud, again every
        repeat interval while it lasts. In memory: a restart re-times it."""
        tid, clk = t['id'], self._clock()
        if v is not None and spot is not None:
            self._blind.pop(tid, None)
            self._blind_alerted.pop(tid, None)
            return
        since = self._blind.setdefault(tid, clk)
        if clk - since < float(cfg.NAKED_BLIND_ALERT_SEC):
            return
        if not _due(self._blind_alerted.get(tid), clk):
            return
        self._blind_alerted[tid] = clk
        if t.get('rehearsal'):
            self.log('  NAKED REHEARSAL #%s %s blind for %.0f min (logged, not alerted)'
                     % (tid, t['stock'], (clk - since) / 60.0))
            return
        what = ' and '.join(w for w, bad in (('no usable bid (stop/TIME dark)', v is None),
                                            ('no spot (TP dark)', spot is None)) if bad)
        self.send('\U0001F534 NAKED %s #%s %s is BLIND for %.0f min: %s. Its exits cannot '
                  'fire. Check Kite / the book.' % (
                      'LIVE' if not t.get('rehearsal') else 'REHEARSAL', tid, t['stock'],
                      (clk - since) / 60.0, what))

    def _delivery_check(self, t: dict, left: Optional[int], today: date) -> None:
        if left is None or left > cfg.NAKED_DELIVERY_ALERT_SESSIONS:
            return
        if t.get('delivery_alerted') == today.isoformat():
            return
        # The flag FIRST (review F12): a failing write must not re-send every poll.
        self.store.update_trade_fields(t['id'], _drive=False,
                                       delivery_alerted=today.isoformat())
        if t.get('rehearsal'):
            # Review L3: a rehearsal record holds nothing at a broker.
            self.log('  NAKED REHEARSAL #%s %s inside the delivery window (%d left; logged)'
                     % (t['id'], t['stock'], left))
            return
        self.send('\U0001F6A8 NAKED #%s %s %s is still OPEN %d session(s) from expiry -- '
                  'past its TIME exit and inside the physical-delivery window. Close it.'
                  % (t['id'], t['stock'], t['long_symbol'], left))

    # -- exits ----------------------------------------------------------------------
    def _begin_exit(self, kite, t, reason, value, q, spot, now, rule) -> str:
        live = not t.get('rehearsal')
        tag = '%s #%s %s' % ('LIVE' if live else 'REHEARSAL', t['id'], t['stock'])
        if live and self.dry_run:
            day = now.strftime('%Y-%m-%d')
            if t.get('dry_exit_alerted') != day:
                self.store.update_trade_fields(t['id'], _drive=False,
                                               dry_exit_alerted=day, exit_rule=rule)
                self.send('\U0001F6A8 NAKED %s: %s fired but the monitor is in --dry-run '
                          '-- NOTHING is selling a real position. Exit it in the NEO app '
                          'or take --dry-run off the crontab.' % (tag, reason.upper()))
            return '%s %s WOULD exit (dry-run)' % (tag, reason)
        # ONE local write up to the first sell (reviews A5, A1): the close lock,
        # the trigger and the chase state together. The Drive copy catches up
        # on the booking write.
        touch = value if value is not None else (q or {}).get('bid')
        trig = {'reason': reason, 'at': now.strftime('%Y-%m-%d %H:%M:%S'),
                'value': value, 'spot': spot, 'touch_bid': touch,
                'touch_ask': (q or {}).get('ask')}
        fields = dict(exit_rule=rule, exit_trigger=trig, exit_touch=touch,
                      exit_chase_day=now.date().isoformat())
        alert = None
        if live:
            try:
                fields['exit_chase'] = self._new_chase(t).to_dict()
            except Exception as e:
                # Cannot even name the contract at NEO. The close lock is held,
                # so nothing else will try; a human must sell it.
                fields['exit_chase_error'] = str(e)[:200]
                fields['exit_chase_alerted'] = True
                alert = ('\U0001F6A8 NAKED %s %s fired but the NEO contract cannot be '
                         'resolved (%s). SELL %s x %s IN THE NEO APP.'
                         % (tag, reason.upper(), e, t['long_symbol'], t['quantity']))
        if not self.store.begin_close(t['id'], 'naked:%s' % reason, _drive=False,
                                      fields=fields):
            return '%s: close lock not taken' % tag
        if not live:
            return self._book(t, reason, value, spot, {'side': 'SELL', 'price': value,
                                                       'bid': touch, 'rehearsal': True})
        if alert:
            self.send(alert)
            return '%s: chase could not start: %s' % (tag, fields['exit_chase_error'])
        fresh = self.store.find(t['id'])
        line = self._step_closing(kite, fresh, now)     # the first sell, before any alert
        self.send('⚠ NAKED %s %s fired @ bid %s (spot %s) -- selling %s x %s on NEO'
                  % (tag, reason.upper(), touch, spot, t['long_symbol'], t['quantity']))
        return line

    def _new_chase(self, t: dict):
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        sym, tok = t.get('neo_symbol'), t.get('neo_token')
        tick = t.get('neo_tick')
        if not sym or not tok:
            row = naked_neo.scrip_row(self.client, t)
            sym, tok = row.trading_symbol, str(row.token)
            tick = tick or getattr(row, 'tick_size', None)
        return orders.ChaseState(bot=naked_neo.BOT, symbol=sym, token=str(tok),
                                 tick=float(tick or cfg.NAKED_OPTION_TICK),
                                 remaining=int(t['quantity']))

    def _policy(self):
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        # None/None: the chase NEVER hands itself to a human (review H1). This
        # engine alerts on the counts instead, and keeps selling.
        return orders.ChasePolicy(reprice_after_s=float(cfg.NAKED_EXIT_REPRICE_SEC),
                                  max_reprices=None, max_troubles=None,
                                  confirm_wait_s=0.0)

    @staticmethod
    def _price_fn(bid: float, reprices: int, tick: float) -> float:
        """At most `naked_exit_max_offset_ticks` under the CURRENT bid: a limit
        far below the bid fills at the bid anyway, and an unbounded walk would
        eventually be refused by the exchange's price protection."""
        return bid - min(int(reprices), int(cfg.NAKED_EXIT_MAX_OFFSET_TICKS)) * tick

    def _floor(self, t: dict, now: datetime) -> Optional[float]:
        """The lowest WIDE-book bid this exit may sell into (module docstring)."""
        left = naked_rules.sessions_left(t.get('expiry'), now.date())
        if left is not None and left <= 2:
            # Trader re-review: two sessions from a physically settled expiry
            # any bid beats delivery. No floor at all -- checked FIRST (review
            # N4/A10): a missing trigger bid must not block an expiry-day sale.
            return 0.0
        touch = t.get('exit_touch')
        try:
            touch = float(touch)
        except (TypeError, ValueError):
            return None
        if touch <= 0:
            return None
        frac = float(cfg.NAKED_EXIT_FLOOR_STOP)
        if _reason_of(t) != 'stop':
            frac = float(cfg.NAKED_EXIT_FLOOR_TARGET)
            try:
                at = datetime.strptime((t.get('exit_trigger') or {}).get('at', ''),
                                       '%Y-%m-%d %H:%M:%S')
                waited = (now - at).total_seconds()
            except (TypeError, ValueError):
                waited = float('inf')               # no trigger time: do not wait on it
            if (waited >= float(cfg.NAKED_EXIT_FLOOR_DECAY_SEC)
                    or (left is not None and left <= cfg.NAKED_DELIVERY_ALERT_SESSIONS)):
                frac = float(cfg.NAKED_EXIT_FLOOR_STOP)
        return frac * touch

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
        from . import naked_neo
        _, orders, _ = naked_neo.neo()
        self._delivery_check(t, naked_rules.sessions_left(t.get('expiry'), now.date()),
                             now.date())
        if self.dry_run:
            return self._closing_dry_run(t, tag, now, orders)
        if not t.get('exit_chase'):
            # A crash (or a failed write) between `begin_close` and the chase
            # state. Start the chase now; if it cannot start, say so ONCE.
            try:
                st0 = self._new_chase(t)
            except Exception as e:
                if not t.get('exit_chase_alerted'):
                    self.store.update_trade_fields(t['id'], _drive=False,
                                                   exit_chase_alerted=True,
                                                   exit_chase_error=str(e)[:200])
                    self.send('🚨 NAKED %s is CLOSING with no exit order and the NEO '
                              'contract cannot be resolved (%s). SELL %s x %s IN THE NEO APP.'
                              % (tag, e, t['long_symbol'], t['quantity']))
                return '%s: closing, chase cannot start: %s' % (tag, e)
            self.store.update_trade_fields(t['id'], _drive=False, exit_chase=st0.to_dict())
            self.send('⚠ NAKED %s was CLOSING with no exit order (restart?) -- the '
                      'chase starts now.' % tag)
            t = self.store.find(t['id'])
        st = orders.ChaseState.from_dict(t['exit_chase'])
        if st.status == 'needs_human':
            # Written by an earlier build that gave up. This one never does.
            st.status, st.note = 'active', 'resumed (this build never hands a chase off)'
            self.log('  NAKED %s: a needs_human chase resumed' % tag)
        today = now.date().isoformat()
        trig_day = str((t.get('exit_trigger') or {}).get('at', ''))[:10]
        if (st.status == 'active' and trig_day and trig_day < today
                and naked_rules.within_open_buffer(now)):
            # Review M3: a chase carried overnight does not sell into the
            # opening prints -- the same 15 minutes the value triggers wait.
            return '%s: carried exit waits out the opening buffer' % tag
        st = self._overnight(t, st, now, orders)
        if t.get('exit_chase_day') != today:
            st.reprices = 0                          # a new session starts at the bid
            self.store.update_trade_fields(t['id'], _drive=False, exit_chase_day=today,
                                           exit_reprice_alert_at=None,
                                           exit_chase=st.to_dict())
            t = self.store.find(t['id'])
        tid = t['id']
        if t.get('exit_touch') in (None, 0, 0.0):
            # Review N4: no bid was saved at the trigger (an unpriced backstop,
            # or a crash before the trigger was written). Anchor the wide-book
            # floor on the first RELIABLE bid seen (re-review F6: a stub bid of
            # a dead book would set a floor of nothing), so it can sell at all.
            # Until then only a reliable book is sold (`_floor` is None).
            bid0 = self._value(self._quote(kite, t['long_symbol'])) or 0.0
            if bid0 > 0:
                self.store.update_trade_fields(tid, _drive=False, exit_touch=bid0,
                                               exit_touch_late=True)
                t = self.store.find(tid)
        try:
            hand = self._hand_sales(t, st, today, orders, tag)
        except Exception as e:
            # Unreadable book: nothing may be sent blind (a hand sale may be hidden).
            orders._trouble(st, self._policy(), 'order book unreadable before a sell: %s' % e)
            self.store.update_trade_fields(tid, _drive=False, exit_chase=st.to_dict())
            self._trouble_watch(self.store.find(tid), st, tag)
            return '%s: book unreadable -- nothing sent' % tag
        t = self.store.find(tid)
        was = (st.status, st.rejects, st.unclears)
        quote = naked_neo.quote_bid_fn(kite, t['long_symbol'], floor=self._floor(t, now))
        seen_held = []

        def held_fn():
            # The chase's own position read, captured (no extra NEO call) so
            # a SHORT account and a lot that already left are seen below.
            h = int(self.client.net_held(st.symbol))
            seen_held.append(h)
            return h

        def persist(s):
            # Local book only: both processes on the box read it under the
            # store lock, and it is written BEFORE every send (the tag).
            self.store.update_trade_fields(tid, _drive=False, exit_chase=s.to_dict())
        st = orders.chase_exit(self.client, st, quote, self._policy(), persist=persist,
                               clock=self._clock, price_fn=self._price_fn, held_fn=held_fn)
        held = seen_held[-1] if seen_held else None
        if held is not None and held < 0:
            self._short_watch(self.store.find(tid), tag, held, st)
        self._zero_held_watch(self.store.find(tid), st, held, tag)
        if st.status == 'done':
            t = self.store.find(tid)
            if t.get('exit_needs_human'):
                # STICKY: once handed to a human (short, hand sale, nothing
                # sold) a later poll -- which reads no position, the chase
                # being done -- must never book it as a normal exit.
                return '%s: chase done -- waiting for a human to book it' % tag
            short = held is not None and held < 0
            # Only when a HAND count took shares off: positions LAG a fill, so
            # on an ordinary exit the step that settles the last fill can still
            # read the lot as held, and that must book normally.
            still = bool(hand) and held is not None and held > 0
            # Re-review F3: a SHORT account is never booked as a normal exit,
            # and (F2) neither is one that still HOLDS shares after the hand
            # count took them off: both go to a human, said plainly.
            if st.sold <= 0 or hand or short or still:
                why = []
                if hand:
                    why.append('%d counted as sold BY HAND' % hand)
                if st.sold <= 0 and not hand:
                    why.append('the rest left the account (by hand, or overnight)')
                if short:
                    state = 'the account is SHORT %d -- BUY IT BACK FIRST' % -held
                elif still:
                    # Positions LAG fills: this may be a lot the owner already
                    # sold. Never "sell it" -- CHECK first, or a second sale
                    # makes the account short.
                    state = ('the account still READS %d held after a hand sale -- the bot '
                             'stopped. CHECK THE NEO APP: sell only what it really holds'
                             % held)
                elif held is None:
                    state = 'the account position could not be read -- check the NEO app'
                else:
                    state = 'the position is flat'
                if not t.get('exit_zero_alerted'):
                    self.store.update_trade_fields(tid, _drive=False, exit_zero_alerted=True,
                                                   exit_needs_human=True)
                    self.send('\U0001F6A8 NAKED %s: %s -- the engine sold %d, %s. Book it at '
                              'the true average: python -m zebra close %s --exit-debit <avg '
                              'fill per share> --reason %s'
                              % (tag, state, st.sold, '; '.join(why) or 'nothing else',
                                 tid, _reason_of(t)))
                return '%s: chase done (bot sold %d, hand %d) -- human books it' % (
                    tag, st.sold, hand)
            trig = t.get('exit_trigger') or {}
            spot = trig.get('spot')
            return self._book(t, _reason_of(t), round(st.avg_exit, 2), spot,
                              {'side': 'SELL', 'price': round(st.avg_exit, 2),
                               'bid': t.get('exit_touch'), 'fills': st.fills,
                               'reprices': st.reprices})
        t = self.store.find(tid)
        self._no_bid_watch(t, st, tag)
        self._trouble_watch(t, st, tag)
        self._refusal_watch(t, st, tag, was)
        return '%s: chase %s (%s)' % (tag, st.status, st.note)

    def _refusal_watch(self, t, st, tag, was) -> None:
        """Review R2: refused / unclear exit sells alert at 3 in a row and then
        AGAIN every repeat interval while they last -- a stop every sell of
        which is refused must never go quiet after one message."""
        clk = self._clock()
        for n, before, key, text in (
                (st.rejects, was[1], 'exit_reject_alert_at',
                 'exit sells REFUSED in a row (%s). Still trying -- if it keeps refusing, '
                 'sell in the NEO app (cancel the bot order first).'),
                (st.unclears, was[2], 'exit_unclear_alert_at',
                 'exit sells UNCLEAR in a row (%s). Check the NEO order book.')):
            if n < 3 or (st.working_tag and key == 'exit_reject_alert_at'):
                # Re-review F10: a sell now WORKING was accepted; the refusal
                # run is over even though `rejects` only resets on a fill.
                if t.get(key) is not None:
                    self.store.update_trade_fields(t['id'], _drive=False, **{key: None})
                continue
            if before < 3 or _due(t.get(key), clk):
                self.store.update_trade_fields(t['id'], _drive=False, **{key: clk})
                self.send('\U0001F534 NAKED %s: %d %s' % (tag, n, text % st.note))

    def _short_watch(self, t, tag, held, st) -> None:
        """Trader review N3: a hand sale and a bot sale that fill within one poll
        leave the account SHORT an option -- unlimited risk on a call. Never
        read as 'flat'; red, and again every repeat interval."""
        clk = self._clock()
        if not _due(t.get('exit_short_alert_at'), clk):
            return
        self.store.update_trade_fields(t['id'], _drive=False, exit_short_alert_at=clk)
        self.send('\U0001F6A8\U0001F6A8 NAKED %s: the NEO ACCOUNT IS SHORT %d of %s -- BUY IT '
                  'BACK NOW in the app. (bot sold %d; a hand sale probably filled too.)'
                  % (tag, -held, st.symbol, st.sold))

    def _zero_held_watch(self, t, st, held, tag) -> None:
        """Flow review R4, re-review F1: the account READS 0 while the chase
        still has shares to sell and no bot sell is working -- sold in the app
        on an earlier day, squared off, or a positions row that does not show
        a carried lot (unmeasured until the Monday probe). It is NEVER settled
        here: a 0 can be a bad read of a lot still held. After
        `naked_exit_zero_held_sec` of continuous zero reads it says so (and
        again every repeat interval) and the chase keeps going, so a row that
        comes back is sold. Replaces the generic STUCK alert for this case."""
        zero = (st.status == 'active' and not st.working_tag and held == 0
                and st.remaining > 0)
        since = t.get('exit_zero_held_since')
        clk = self._clock()
        if not zero:
            if since is not None or t.get('exit_zero_held_alert_at') is not None:
                self.store.update_trade_fields(t['id'], _drive=False,
                                               exit_zero_held_since=None,
                                               exit_zero_held_alert_at=None)
            return
        if since is None:
            self.store.update_trade_fields(t['id'], _drive=False, exit_zero_held_since=clk)
            return
        if clk - float(since) < float(cfg.NAKED_EXIT_ZERO_HELD_SEC):
            return
        if not _due(t.get('exit_zero_held_alert_at'), clk):
            return
        self.store.update_trade_fields(t['id'], _drive=False, exit_zero_held_alert_at=clk)
        self.send('\U0001F6A8 NAKED %s: the NEO account READS 0 of %s for %.0f min while the '
                  'exit still has %d to sell (bot sold %d). VERIFY IN THE NEO APP: if it was '
                  'sold by hand / squared off, book it: python -m zebra close %s --exit-debit '
                  '<avg fill> --reason %s. If the app shows it HELD, sell it there. The engine '
                  'keeps watching.' % (tag, st.symbol, (clk - float(since)) / 60.0,
                                       st.remaining, st.sold, t['id'], _reason_of(t)))

    def _hand_sales(self, t, st, today: str, orders, tag) -> int:
        """Shares of this contract sold TODAY by orders zebra did not tag (the
        NEO app, another tool), from the ORDER BOOK (only this contract's rows
        are parsed). New ones come off `remaining` and cancel any working bot
        sell. Returns the total hand-sold quantity over the record's life.
        Raises when the book is unreadable.

        NO BASELINE (re-review F2): a hand sale of an EARLIER record on the same
        contract the same day is counted here too (review A5). That is the
        SAFE error -- the bot stops short and the done-branch says the account
        still holds the lot -- while any baseline can swallow a hand sale of
        THIS lot and send the bot selling into a short."""
        c = self.client
        today_hand = int(c.foreign_filled(st.symbol, 'S'))
        seen = dict(t.get('exit_hand_sold') or {})
        new = today_hand - int(seen.get(today, 0))
        if new > 0:
            if st.working_tag and st.working_no:
                res = orders.cancel_and_confirm(c, st.working_no, st.working_tag, wait_s=0.0,
                                                expect_qty=st.working_qty or None)
                if res.terminal:
                    orders._apply(st, res)
                    st.cancel_sent = False
                else:
                    st.cancel_sent = True
            st.remaining = max(0, st.remaining - new)
            st.note = '%d share(s) sold by hand' % new
            seen[today] = today_hand
            self.store.update_trade_fields(t['id'], _drive=False, exit_hand_sold=seen,
                                           exit_chase=st.to_dict())
            self.send('\U0001F534 NAKED %s: a HAND sale of %d detected in the NEO book -- the '
                      'bot will not sell those. %d left for the bot.%s'
                      % (tag, new, st.remaining,
                         (' Its working sell %s could NOT be confirmed cancelled -- check the '
                          'app.' % st.working_no) if st.working_tag else ''))
        return sum(int(v) for v in seen.values())

    def _closing_dry_run(self, t, tag, now, orders) -> str:
        """Mid-exit but unable to step (dry run / kill switch). The working sell
        is CANCELLED and read back (review A1): the alert tells a human to sell
        in the app, and the bot's old order must not fill after they do."""
        st = None
        if t.get('exit_chase'):
            try:
                st = orders.ChaseState.from_dict(t['exit_chase'])
            except Exception:
                st = None
        working = ''
        if st is not None and st.working_tag and st.working_no \
                and t.get('dry_cancel_tried') == st.working_no:
            working = st.working_no                 # tried once; the alert names it
        elif st is not None and st.working_tag and st.working_no:
            self.store.update_trade_fields(t['id'], _drive=False,
                                           dry_cancel_tried=st.working_no)
            try:
                res = orders.cancel_and_confirm(self.client, st.working_no, st.working_tag,
                                                wait_s=0.0, expect_qty=st.working_qty or None)
                if res.terminal:
                    orders._apply(st, res)
                    st.cancel_sent = False
                else:
                    working = st.working_no
                self.store.update_trade_fields(t['id'], _drive=False, exit_chase=st.to_dict())
            except Exception as e:
                working = st.working_no
                self.log('  NAKED %s: cancel of working sell %s failed: %s'
                         % (tag, st.working_no, e))
        day = now.strftime('%Y-%m-%d')
        if t.get('dry_exit_alerted') != day:
            self.store.update_trade_fields(t['id'], _drive=False, dry_exit_alerted=day)
            self.send('\U0001F6A8 NAKED %s is mid-EXIT but the monitor is now in DRY RUN '
                      '(kill switch / --dry-run): nothing re-prices or sells.%s The bot '
                      'has sold %s; %s left per its record -- CHECK THE NEO POSITION before '
                      'selling. Then book it: python -m zebra close %s --exit-debit '
                      '<avg fill> --reason %s'
                      % (tag, (' ORDER %s IS STILL WORKING -- CANCEL IT FIRST.' % working)
                         if working else ' No bot sell is working.',
                         st.sold if st is not None else 0,
                         st.remaining if st is not None else t.get('quantity'),
                         t['id'], _reason_of(t)))
        return '%s: closing, but --dry-run -- the chase is not stepped' % tag

    def _trouble_watch(self, t, st, tag) -> None:
        """Troubles / re-prices past `naked_exit_alert_steps` ALERT (never stop),
        then again every repeat interval while they last."""
        clk = self._clock()
        working = (' Order %s is WORKING -- if you sell by hand, cancel it first.'
                   % st.working_no) if st.working_no else ''
        zero_case = 'account holds 0' in str(st.note) and not st.working_tag
        if st.troubles >= cfg.NAKED_EXIT_ALERT_STEPS and not zero_case:
            if _due(t.get('exit_trouble_alert_at'), clk):
                self.store.update_trade_fields(t['id'], _drive=False,
                                               exit_trouble_alert_at=clk)
                self.send('\U0001F6A8 NAKED %s: the exit is STUCK (%d steps: %s). Sold %d, '
                          '%d left. The engine keeps trying.%s'
                          % (tag, st.troubles, st.note, st.sold, st.remaining, working))
        elif t.get('exit_trouble_alert_at') is not None:
            self.store.update_trade_fields(t['id'], _drive=False, exit_trouble_alert_at=None)
        if st.reprices >= cfg.NAKED_EXIT_ALERT_STEPS and _due(
                t.get('exit_reprice_alert_at'), clk):
            self.store.update_trade_fields(t['id'], _drive=False, exit_reprice_alert_at=clk)
            self.send('\U0001F534 NAKED %s: %d re-prices without getting flat (%s). Sold %d, '
                      '%d left. Still selling, at most %d ticks under the bid.%s'
                      % (tag, st.reprices, st.note, st.sold, st.remaining,
                         cfg.NAKED_EXIT_MAX_OFFSET_TICKS, working))

    def _overnight(self, t, st, now, orders):
        """A sell still `working` from an EARLIER session (review R1): NEO's
        DAY order expired at the close and is absent from today's book, which
        the chase would count as 'vanished'. The ACCOUNT position settles it:
        whatever is still held is what remains to sell; anything that filled
        unseen is marked approximate rather than guessed."""
        if not st.working_tag or not st.working_since:
            return st
        # Both sides from the SAME clock the chase stamps `working_since` with.
        started = datetime.fromtimestamp(float(st.working_since)).date()
        vanished = (st.working_no and 'vanished' in str(st.note)
                    and st.troubles >= cfg.NAKED_EXIT_ALERT_STEPS)
        if started >= datetime.fromtimestamp(float(self._clock())).date() and not vanished:
            return st
        # Architect re-review (c): a working sell that VANISHED from today's book
        # used to loop all session now that the chase never hands off. After
        # the alert threshold it is settled the same way: from the ACCOUNT.
        if vanished:
            live = self.client.live_orders(st.symbol, 'S')
            if live:                                  # something IS working: not ours to settle
                return st
        held = self.client.net_held(st.symbol)        # Unavailable -> raises, retried next poll
        unseen = max(0, st.remaining - max(held, 0))
        self.log('  NAKED #%s: sell %s working since %s %s; account holds %d'
                 % (t['id'], st.working_tag, started,
                    'vanished from the book' if vanished else 'expired overnight', held))
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
            if not st.working_tag or not st.working_no:
                continue
            try:
                res = orders.cancel_and_confirm(self.client, st.working_no, st.working_tag,
                                                wait_s=5.0, expect_qty=st.working_qty or None)
                if res.terminal:
                    orders._apply(st, res)
                    st.cancel_sent = False
                self.store.update_trade_fields(t['id'], _drive=False, exit_chase=st.to_dict())
                out.append('#%s: end of session, working sell %s' % (t['id'], res.state))
                if not res.terminal:
                    self.send('\U0001F6A8 NAKED #%s %s: the working exit sell %s could NOT be '
                              'confirmed cancelled at the close (%s). Check the NEO order book.'
                              % (t['id'], t['stock'], st.working_no, res.reason))
            except Exception as e:
                out.append('#%s: end-of-session cancel failed: %s' % (t['id'], e))
                self.send('\U0001F6A8 NAKED #%s %s: cancelling the working exit sell %s at the '
                          'close FAILED (%s). Check the NEO order book.'
                          % (t['id'], t['stock'], st.working_no, str(e)[:120]))
        return out

    def _nag_stuck(self, t, now) -> None:
        """Once a day per record whose exit needs a human (review R2): the
        one-off alert is easy to miss and a restarted monitor forgets it."""
        ch = t.get('exit_chase') or {}
        stuck = t.get('status') == 'closing' and not t.get('rehearsal') and (
            (t.get('exit_chase_error') and not ch)
            or (ch.get('status') == 'done'
                and (not ch.get('sold') or t.get('exit_needs_human'))))
        if not stuck:
            return
        day = now.strftime('%Y-%m-%d')
        if self._nagged.get(t['id']) == day or t.get('stuck_nag_day') == day:
            return
        self._nagged[t['id']] = day
        self.store.update_trade_fields(t['id'], _drive=False, stuck_nag_day=day)
        self.send('\U0001F6A8 NAKED #%s %s is STILL mid-exit and needs a human (%s). Sold %s, '
                  '%s left. Sell the rest in the NEO app, then book it: python -m zebra close '
                  '%s --exit-debit <avg fill per share> --reason %s'
                  % (t['id'], t['stock'], ch.get('note') or t.get('exit_chase_error'),
                     ch.get('sold', 0), ch.get('remaining', t.get('quantity')), t['id'],
                     _reason_of(t)))

    def _no_bid_watch(self, t, st, tag) -> None:
        stalled = (st.status == 'active' and not st.working_tag
                   and str(st.note).startswith('no bid'))
        since = t.get('exit_nobid_since')
        if not stalled:
            if since or t.get('exit_nobid_alerted'):
                # Reset BOTH: a later stall on this record must be said too.
                self.store.update_trade_fields(t['id'], _drive=False, exit_nobid_since=None,
                                               exit_nobid_alerted=False)
            return
        now = self._clock()
        if not since:
            self.store.update_trade_fields(t['id'], _drive=False, exit_nobid_since=now)
            return
        last = t.get('exit_nobid_alerted')
        last = float(last) if isinstance(last, (int, float)) and not isinstance(last, bool) \
            else None
        # Repeats every interval while it lasts (ops re-review N1): a stall on a
        # pulled book used to alert once and then stay silent across days.
        if now - float(since) >= float(cfg.NAKED_NO_BID_ALERT_SEC) and _due(last, now):
            self.store.update_trade_fields(t['id'], _drive=False, exit_nobid_alerted=now)
            fl = self._floor(t, self._now())
            self.send('🔴 NAKED %s: the exit has had NO sellable bid for %.0f min -- '
                      'nothing is selling. Sold %d, %d left. A wide book is sold into only '
                      'at a bid >= %s (the floor for this exit). Check the book.'
                      % (tag, (now - float(since)) / 60.0, st.sold, st.remaining,
                         '%.2f' % fl if fl is not None else 'any'))

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


def hand_close_problem(t: dict, client=None) -> str:
    """Why `zebra close` must NOT book this live naked record by hand, or ''.

    Review A2: booking a record while the bot's exit sell still works at NEO
    orphans that order (the engine stops looking at an `exited` record), and if
    it fills after a hand sale the account is net SHORT. So: no working sell,
    and the account flat in the contract. Rehearsal records hold no position."""
    if t.get('rehearsal') or t.get('structure') != 'naked_long':
        return ''
    ch = t.get('exit_chase') or {}
    if ch.get('working_tag'):
        return ('the exit chase has a sell WORKING at NEO (order %s, tag %s). Cancel it '
                'in the NEO app (or wait for the engine), then book.'
                % (ch.get('working_no') or '?', ch.get('working_tag')))
    sym = t.get('neo_symbol')
    if not sym:
        return ('the record names no NEO symbol, so the account cannot be checked flat')
    try:
        if client is None:
            from . import naked_neo
            client = naked_neo.default_client()
        held = int(client.net_held(sym))
        # Architect re-review: the chase state is a LOCAL write on the Pi, so a
        # `zebra close` run elsewhere cannot see a working sell on the record.
        # NEO's own book can.
        live = client.live_orders(sym, 'S')
    except Exception as e:
        return 'cannot read the NEO position / order book for %s (%s)' % (sym, e)
    if live:
        return ('NEO shows a sell WORKING on %s (order %s). Cancel it in the app (or wait '
                'for the engine), then book.' % (sym, ', '.join(o.order_no for o in live)))
    if held > 0:
        return ('the NEO account still HOLDS %d of %s. Sell it first; the exit engine '
                'is still watching this record.' % (held, sym))
    if held < 0:
        return 'the NEO account is SHORT %d of %s -- fix that first' % (-held, sym)
    return ''


def health_lines(trades: list, mode: str, today: date) -> List[str]:
    """`zebra naked-check`: ONE read-only answer to "is the naked path safe
    right now" (ops review runbook item 1). No network beyond the local files:
    the heartbeat, the NEO session file, the halt file and the book."""
    from .monitor import read_exit_engine_heartbeat
    from . import naked_entry, naked_halt
    out = ['naked_live: %s' % mode]
    hb = read_exit_engine_heartbeat()
    beat = hb.get('beat') or {}
    out.append('exit monitor: %s (%s); beat state %s, naked engine %s' % (
        hb.get('state'), hb.get('detail'), beat.get('state'), beat.get('naked_engine')))
    gate = naked_entry._beat_problem(mode) if mode != 'off' else 'naked_live is off'
    out.append('live entry gate: %s' % (gate or 'OPEN (an armed exit engine is polling)'))
    try:
        from . import naked_neo
        naked_neo._ensure_path()
        from kotak_neo import session as neo_session
        s = neo_session.current()
        out.append('NEO session: generation %d, trading day %s%s' % (
            s.generation, s.trading_day,
            '' if s.trading_day == today.isoformat() else '  <-- NOT TODAY'))
    except Exception as e:
        out.append('NEO session: UNAVAILABLE (%s)' % e)
    if mode != 'off':
        ok, why = naked_halt.entries_allowed(mode, today)
        out.append('halt: %s' % why)
    # WHOLE BOOK: health -- every open naked record and every unresolved order.
    opened = [t for t in trades if is_naked(t) and t.get('status') in OPEN_STATES]
    out.append('open naked: %d' % len(opened))
    for t in opened:
        ch = t.get('exit_chase') or {}
        line = '  #%s %s %s %s %s' % (t['id'], 'REHEARSAL' if t.get('rehearsal') else 'LIVE',
                                       t.get('stock'), t.get('long_symbol'), t.get('status'))
        if t.get('status') == 'closing':
            line += ' | chase %s sold %s left %s working %s troubles %s reprices %s | %s' % (
                ch.get('status'), ch.get('sold'), ch.get('remaining'),
                ch.get('working_no') or '-', ch.get('troubles'), ch.get('reprices'),
                ch.get('note') or t.get('exit_chase_error') or '')
        out.append(line)
    stuck = naked_entry.unresolved_twins(trades)
    out.append('unresolved naked orders: %d%s' % (
        len(stuck), '  <-- NEW ENTRIES BLOCKED' if stuck else ''))
    for p in stuck:
        tw = p.get(naked_entry.TWIN_FIELD) or {}
        out.append('  parent #%s %s: %s since %s tag %s -- zebra naked-resolve %s '
                   '--filled <px> | --none' % (p['id'], p.get('stock'), tw.get('state'),
                                               tw.get('at'), tw.get('tag'), p['id']))
    return out


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
