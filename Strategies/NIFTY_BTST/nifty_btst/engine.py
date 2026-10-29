"""The NIFTY BTST Rs100 rule, as a per-poll state machine. Paper only.

THE RULE (Volrix "nifty-weekly-btst-rs100-premium-momentum-both-sides",
traded AS PUBLISHED so every paper trade has a backtest twin):
  09:20       mark the nearest weekly CE and PE whose premium is closest to
              Rs 100 (on expiry day: the NEXT weekly). trigger = 1.5 x mark.
  09:21-15:25 a marked option trading at or above its trigger is bought,
              one lot, at most once per side per day.
  stop        50% of the entry price, watched while the market is open —
              including the next morning's 09:15-09:25.
  exit        09:25 the next trading session. No target.

PAPER FILL RULES (house rules, not Volrix's):
  * BUY at the ASK, SELL at the BID, only on a reliable book. An unreliable
    book defers; a triggered EXIT stays latched until it can be booked.
  * A trigger seen while we were not watching (late start) is MISSED, not
    back-filled: we never claim a fill we could not have had.
  * Every trade is stamped with its DTE and class (expiry_day / day_before /
    other) so the split in 02_nifty_btst_expiry_analysis can be re-asked
    forward without changing the rule.

Each `step(now)` is one poll: quote once, manage open positions, mark if due,
check entries, save. It never sleeps and never reads the wall clock, so a test
can drive a whole simulated session through it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

from . import selection
from .config import Config
from .kite_io import Market, QuoteError, TokenError
from .notify import Notifier
from .pricing import Book, charges, reliable, traded_on
from .sessions import at, sessions_between
from .store import Store

logger = logging.getLogger(__name__)

SIDES = ('CE', 'PE')

#: seconds between attempts after a failed mark
MARK_RETRY_SEC = 60


def trade_class(expiry_day_today: bool, dte_sessions: int) -> str:
    if expiry_day_today:
        return 'expiry_day'
    if dte_sessions == 1:
        return 'day_before'
    return 'other'


class Engine:
    def __init__(self, cfg: Config, store: Store, market: Optional[Market],
                 notifier: Notifier, session_expected: bool = True):
        # market is None only for `digest`, which never quotes.
        # session_expected: what the holiday calendar says about today. It
        # only decides whether a dead market is ALARMING; whether the market
        # is live is always read from the option books themselves.
        self.session_expected = session_expected
        self.closed_today: Optional[date] = None
        self.cfg = cfg
        self.store = store
        self.market: Market = market  # type: ignore[assignment]
        self.notify = notifier
        self.fail_streak = 0
        self._last_poll_log: Optional[datetime] = None
        self._expired_checked: Optional[date] = None
        self._mark_retry_at: Optional[datetime] = None
        self._resume_checked: Optional[date] = None

    # ------------------------------------------------------------ helpers
    def key(self, sym: str) -> str:
        return '%s:%s' % (self.cfg.option_exchange, sym)

    def _alert_once(self, day: Dict[str, Any], flag: str, text: str) -> None:
        if flag not in day['flags']:
            day['flags'].append(flag)
            self.notify.send(text)

    # --------------------------------------------------------------- step
    def step(self, now: datetime) -> None:
        cfg, today, t = self.cfg, now.date(), now.time()
        if t < cfg.market_open or t > cfg.market_close:
            return
        day = self.store.day(today)
        if day.get('closed'):
            self.closed_today = today
        if self.closed_today == today:
            return
        if self._expired_checked != today:
            self.settle_expired(now)
            self._expired_checked = today

        keys = [cfg.index_quote_symbol]
        keys += [self.key(p['symbol']) for p in self.store.open_positions()
                 if p['expiry'] >= today.isoformat()]
        if t <= cfg.entry_end:
            for side in SIDES:
                m = day['marks'].get(side)
                if m and not day['fired'].get(side):
                    keys.append(self.key(m['symbol']))
        try:
            books = self.market.quote(keys)
        except TokenError:
            raise
        except QuoteError as exc:
            self.fail_streak += 1
            logger.warning('quote failed (%d in a row): %s', self.fail_streak, exc)
            if self.fail_streak == cfg.quote_fail_alert_after:
                self.notify.send('QUOTES FAILING: %d polls in a row. Open '
                                 'positions and triggers are NOT being '
                                 'watched.\n%s' % (self.fail_streak, exc))
                self.store.event(now, 'ERROR', what='quote', streak=self.fail_streak,
                                 error=str(exc))
            self.store.save()
            return
        if self.fail_streak >= cfg.quote_fail_alert_after:
            self.notify.send('Quotes recovered after %d failed polls.'
                             % self.fail_streak)
        self.fail_streak = 0

        self.manage_open(now, books)

        if day.get('mark_done') and self._resume_checked != today:
            # First poll of THIS process on a day already marked: a restart.
            self._resume_checked = today
            if t > cfg.entry_start:
                self.resume_check(now, day)
        if (not day.get('mark_done') and cfg.mark_time <= t <= cfg.entry_end
                and (self._mark_retry_at is None or now >= self._mark_retry_at)):
            try:
                self.mark(now, day, books)
                self._resume_checked = today
            except TokenError:
                raise
            except Exception as exc:
                # A late mark costs ~120 historical calls: never retry it per
                # poll. A live mark retries on the next poll; after
                # late_mark_after the retry rebuilds exact 09:20 prices.
                late = t > cfg.late_mark_after
                self._mark_retry_at = now + timedelta(
                    seconds=MARK_RETRY_SEC if late else 0)
                logger.exception('mark failed')
                self.store.event(now, 'ERROR', what='mark', error=str(exc))
                self._alert_once(day, 'mark_failed',
                                 'MARK FAILED at %s: %s\nRetrying every %ds '
                                 'until %s.' % (now.strftime('%H:%M:%S'), exc,
                                                MARK_RETRY_SEC, cfg.entry_end))

        if self.closed_today == today:
            self.store.save()
            return
        if cfg.entry_start <= t <= cfg.entry_end:
            self.entries(now, day, books)

        self._poll_log(now, day, books)
        self.store.heartbeat(now, fail_streak=self.fail_streak,
                             open=len(self.store.open_positions()),
                             marked=bool(day.get('mark_done')))
        self.store.save()

    # --------------------------------------------------------------- mark
    def mark(self, now: datetime, day: Dict[str, Any],
             polled: Optional[Dict[str, Book]] = None) -> None:
        cfg, today = self.cfg, now.date()
        opts = self.market.options()
        exps = selection.expiries(opts)
        expiry = selection.pick_expiry(exps, today)
        exp_today = selection.is_expiry_day(exps, today)
        dte_s = sessions_between(today, expiry)
        cls = trade_class(exp_today, dte_s)
        late = now.time() > cfg.late_mark_after
        mark_dt = at(today, cfg.mark_time)

        highs: Dict[str, float] = {}
        if late:
            spot_c = self.market.minute_candles(cfg.index_token, mark_dt,
                                                mark_dt + timedelta(minutes=1))
            if not spot_c:
                if not self.session_expected:
                    self._close_day(now, day, 'no 09:20 NIFTY candle; calendar '
                                    'agrees it is not a session')
                    return
                raise RuntimeError('late start: no 09:20 NIFTY candle')
            spot = spot_c[0]['open']
            cands = selection.candidates(opts, expiry, spot, cfg.strike_band_points)
            books: Dict[str, Book] = {}
            for c in cands:
                rows = self.market.minute_candles(c['instrument_token'], mark_dt, now)
                at_mark = [r for r in rows if r['t'] >= mark_dt]
                if not at_mark:
                    continue
                ts = at_mark[0]['t'].isoformat()
                books[c['tradingsymbol']] = Book(at_mark[0]['open'], 0.0, 0.0,
                                                 0, 0, ts, ltt=ts)
                entry_from = at(today, cfg.entry_start)
                hs = [r['high'] for r in rows if r['t'] >= entry_from]
                if hs:
                    highs[c['tradingsymbol']] = max(hs)
        else:
            ib = (polled or {}).get(cfg.index_quote_symbol)
            if ib is None or ib.ltp <= 0:
                ib = self.market.quote([cfg.index_quote_symbol]).get(cfg.index_quote_symbol)
            if ib is None or ib.ltp <= 0:
                raise RuntimeError('no NIFTY spot quote')
            spot = ib.ltp
            cands = selection.candidates(opts, expiry, spot, cfg.strike_band_points)
            raw = self.market.quote([self.key(c['tradingsymbol']) for c in cands])
            books = {k.split(':', 1)[1]: v for k, v in raw.items()
                     if traded_on(v, today)}
            if raw and not books:
                if not self.session_expected:
                    self._close_day(now, day, 'no option traded today; calendar '
                                    'agrees it is not a session')
                    return
                raise RuntimeError('no %s option has traded today: market not '
                                   'live?' % cfg.underlying)
            if books and not self.session_expected:
                self._alert_once(day, 'calendar_wrong',
                                 'Holiday calendar calls %s closed, but options '
                                 'are trading. Trading the session.' % today)

        if not cands:
            raise RuntimeError('no %s strikes within %.0f of spot %.2f for %s'
                               % (cfg.underlying, cfg.strike_band_points, spot, expiry))
        lines = []
        for side in SIDES:
            pick = selection.pick_nearest_premium(cands, books, side,
                                                  cfg.target_premium, spot)
            if pick is None:
                day['marks'][side] = None
                lines.append('%s: nothing priced' % side)
                continue
            b = books[pick['tradingsymbol']]
            m = {
                'side': side, 'symbol': pick['tradingsymbol'],
                'token': pick['instrument_token'], 'strike': pick['strike'],
                'expiry': pick['expiry'], 'lot_size': pick['lot_size'],
                'mark_price': round(b.ltp, 2), 'mark_bid': b.bid,
                'mark_ask': b.ask, 'mark_at': now.isoformat(timespec='seconds'),
                'trigger': round(b.ltp * cfg.trigger_mult, 2),
                'spot': round(spot, 2), 'late': late,
                'dte_sessions': dte_s, 'dte_days': (expiry - today).days,
                'cls': cls,
            }
            if selection.edge_of_band(pick, cands):
                m['edge_of_band'] = True
                logger.warning('%s pick %s is the outermost strike in the band; '
                               'widen strike_band_points', side, pick['tradingsymbol'])
            day['marks'][side] = m
            lines.append('%s %s @ %.2f -> trigger %.2f%s'
                         % (side, pick['tradingsymbol'], m['mark_price'],
                            m['trigger'], ' [late mark]' if late else ''))
            hi = highs.get(pick['tradingsymbol'])
            if late and hi is not None and hi >= m['trigger']:
                day['fired'][side] = True
                day['missed'][side] = {'high_since_entry_start': hi,
                                       'seen_at': now.isoformat(timespec='seconds')}
                self.store.event(now, 'MISSED', side=side, symbol=m['symbol'],
                                 trigger=m['trigger'], high=hi)
                lines.append('  %s MISSED: traded %.2f >= trigger before we '
                             'were watching' % (side, hi))
        day['mark_done'] = True
        day['expiry'] = expiry.isoformat()
        day['cls'] = cls
        self.store.event(now, 'MARK', expiry=expiry.isoformat(), spot=spot,
                         cls=cls, dte_sessions=dte_s, late=late,
                         marks=day['marks'], missed=day['missed'])
        self.notify.send('MARK %s  spot %.2f  expiry %s (%s, DTE %d)\n%s'
                         % (today, spot, expiry, cls, dte_s, '\n'.join(lines)))

    def _close_day(self, now: datetime, day: Dict[str, Any], why: str) -> None:
        day['closed'] = True
        self.closed_today = now.date()
        self.store.event(now, 'CLOSED', why=why)
        logger.info('%s: market closed (%s)', now.date(), why)

    def resume_check(self, now: datetime, day: Dict[str, Any]) -> None:
        """After a restart, a side whose option crossed its trigger while no
        process was watching is MISSED: buying it now, at whatever it trades,
        would be a fill the rule (and its backtest twin) never had."""
        start = at(now.date(), self.cfg.entry_start)
        # Only the gap: the previous process watched up to its last heartbeat,
        # and a 1-minute high it saw no LTP for is not a missed fill.
        hb = self.store.read_heartbeat()
        if hb and str(hb.get('t', ''))[:10] == now.date().isoformat():
            last = datetime.fromisoformat(hb['t']) - timedelta(seconds=60)
            start = max(start, last)
        for side in SIDES:
            m = day['marks'].get(side)
            if not m or day['fired'].get(side):
                continue
            try:
                rows = self.market.minute_candles(m['token'], start, now)
            except Exception as exc:
                logger.warning('resume check for %s failed (%s); trading on', side, exc)
                continue
            hi = max((r['high'] for r in rows), default=0.0)
            if hi >= m['trigger']:
                day['fired'][side] = True
                day['missed'][side] = {'high_since_entry_start': hi,
                                       'seen_at': now.isoformat(timespec='seconds'),
                                       'why': 'restart'}
                self.store.event(now, 'MISSED', side=side, symbol=m['symbol'],
                                 trigger=m['trigger'], high=hi, why='restart')
                self.notify.send('%s MISSED after a restart: %s traded %.2f >= '
                                 'trigger %.2f while nothing was watching'
                                 % (side, m['symbol'], hi, m['trigger']))

    # ------------------------------------------------------------ entries
    def entries(self, now: datetime, day: Dict[str, Any],
                books: Dict[str, Book]) -> None:
        cfg = self.cfg
        for side in SIDES:
            m = day['marks'].get(side)
            if not m or day['fired'].get(side):
                continue
            b = books.get(self.key(m['symbol']))
            if not traded_on(b, now.date()) or b is None or b.ltp < m['trigger']:
                continue
            if 'trigger_seen_at' not in m:
                m['trigger_seen_at'] = now.isoformat(timespec='seconds')
                m['trigger_seen_ltp'] = b.ltp
            ok, why = reliable(b, cfg.max_spread_abs, cfg.max_spread_pct)
            if not ok:
                m['entry_defers'] = m.get('entry_defers', 0) + 1
                self.store.event(now, 'DEFER', what='entry', side=side,
                                 symbol=m['symbol'], why=why)
                continue
            qty = m['lot_size'] * cfg.lots
            fill = round(b.ask, 2)
            pos = self.store.add_position({
                'status': 'open', 'side': side, 'symbol': m['symbol'],
                'token': m['token'], 'strike': m['strike'],
                'expiry': m['expiry'], 'lot_size': m['lot_size'],
                'lots': cfg.lots, 'qty': qty,
                'entry_date': now.date().isoformat(),
                'entry_time': now.isoformat(timespec='seconds'),
                'mark_price': m['mark_price'], 'trigger': m['trigger'],
                'entry_price': fill, 'entry_ltp': b.ltp, 'entry_bid': b.bid,
                'entry_ask': b.ask, 'entry_ask_qty': b.ask_qty,
                'thin_touch': b.ask_qty < qty,
                'slip_vs_trigger': round(fill - m['trigger'], 2),
                'stop_level': round(fill * cfg.stop_frac, 2),
                'cls': m['cls'], 'dte_sessions': m['dte_sessions'],
                'dte_days': m['dte_days'], 'spot_at_entry': None,
                'mfe_ltp': b.ltp, 'mae_ltp': b.ltp, 'exit_defers': 0,
            })
            ib = books.get(cfg.index_quote_symbol)
            if ib is not None:
                pos['spot_at_entry'] = ib.ltp
            day['fired'][side] = True
            self.store.event(now, 'ENTRY', id=pos['id'], side=side,
                             symbol=pos['symbol'], fill=fill, ltp=b.ltp,
                             bid=b.bid, ask=b.ask, trigger=m['trigger'])
            self.notify.send(
                'ENTRY #%d BUY %s x%d @ %.2f (ask)\ntrigger %.2f, ltp %.2f, '
                'slip %+.2f\nstop %.2f | exit 09:25 next session | %s DTE %d'
                % (pos['id'], pos['symbol'], qty, fill, m['trigger'], b.ltp,
                   pos['slip_vs_trigger'], pos['stop_level'], pos['cls'],
                   pos['dte_sessions']))

    # -------------------------------------------------------- open/exits
    def manage_open(self, now: datetime, books: Dict[str, Book]) -> None:
        cfg, today, t = self.cfg, now.date(), now.time()
        exit_dt = at(today, cfg.next_day_exit)
        for p in self.store.open_positions():
            if p['expiry'] < today.isoformat():
                continue                        # settle_expired owns it
            b = books.get(self.key(p['symbol']))
            if not traded_on(b, today):
                b = None                   # yesterday's book: never acted on
            if b is not None and b.ltp > 0:
                p['last_ltp'] = b.ltp
                p['mfe_ltp'] = max(p.get('mfe_ltp', b.ltp), b.ltp)
                p['mae_ltp'] = min(p.get('mae_ltp', b.ltp), b.ltp)
            overnight = p['entry_date'] < today.isoformat()
            if overnight and 'next_open_ltp' not in p and b is not None and b.ltp > 0:
                p['next_open_ltp'] = b.ltp
                p['next_open_at'] = now.isoformat(timespec='seconds')
            reason = p.get('exit_pending')
            if reason is None:
                if overnight and t >= cfg.next_day_exit:
                    late = (now - exit_dt).total_seconds() > cfg.next_day_exit_grace_sec
                    reason = 'next_day_late' if late else 'next_day_0925'
                elif b is not None and 0 < b.ltp <= p['stop_level']:
                    reason = 'stop_next_morning' if overnight else 'stop'
            if reason is None:
                continue
            ok, why = reliable(b, cfg.max_spread_abs, cfg.max_spread_pct)
            if b is None:
                why = 'no trade yet today'
            if not ok:
                if p.get('exit_pending') is None:
                    p['exit_pending'] = reason
                    p['exit_pending_since'] = now.isoformat(timespec='seconds')
                p['exit_defers'] = p.get('exit_defers', 0) + 1
                self.store.event(now, 'DEFER', what='exit', id=p['id'],
                                 reason=reason, why=why)
                continue
            assert b is not None            # reliable() refuses a missing book
            self._close(p, now, b.bid, reason, book=b, orders=2)

    def _close(self, p: Dict[str, Any], now: datetime, price: float,
               reason: str, book: Optional[Book], orders: int) -> None:
        rates = self.cfg.fee_rates
        buy_v = p['entry_price'] * p['qty']
        sell_v = price * p['qty']
        ch = charges(buy_v, sell_v, rates, orders=orders)
        ch_ex = charges(buy_v, sell_v, rates, orders=orders, include_brokerage=False)
        gross = sell_v - buy_v
        p.update({
            'status': 'closed', 'exit_reason': reason,
            'exit_date': now.date().isoformat(),
            'exit_time': now.isoformat(timespec='seconds'),
            'exit_price': round(price, 2),
            'exit_bid': book.bid if book else None,
            'exit_ask': book.ask if book else None,
            'exit_ltp': book.ltp if book else None,
            'gross_pnl': round(gross, 2), 'charges': ch,
            'net_pnl': round(gross - ch['total'], 2),
            # Volrix includes charges but NOT brokerage: this is the twin's basis.
            'net_ex_brokerage': round(gross - ch_ex['total'], 2),
            'held_sessions': sessions_between(date.fromisoformat(p['entry_date']),
                                              now.date()),
        })
        p.pop('exit_pending', None)
        self.store.event(now, 'EXIT', id=p['id'], reason=reason,
                         price=p['exit_price'], net=p['net_pnl'])
        self.notify.send('EXIT #%d SELL %s @ %.2f (%s)\nentry %.2f -> net Rs %+.0f '
                         '(gross %+.0f, charges %.0f)'
                         % (p['id'], p['symbol'], p['exit_price'], reason,
                            p['entry_price'], p['net_pnl'], gross, ch['total']))

    def settle_expired(self, now: datetime) -> None:
        """A position still open after its expiry: the box missed the exit.

        Settled at intrinsic off the NIFTY close of expiry day, with one order
        (the buy) — and loudly, because the rule would have sold at 09:25.
        """
        today = now.date()
        for p in self.store.open_positions():
            if p['expiry'] >= today.isoformat():
                continue
            exp = date.fromisoformat(p['expiry'])
            try:
                close = self.market.day_close(self.cfg.index_token, exp)
            except Exception as exc:
                close = None
                logger.warning('index close for %s unavailable: %s', exp, exc)
            if close is None:
                day = self.store.day(today)
                self._alert_once(day, 'expired_unpriced_%d' % p['id'],
                                 'POSITION #%d %s EXPIRED %s while open and the '
                                 'NIFTY close is unavailable. Not booked.'
                                 % (p['id'], p['symbol'], exp))
                continue
            k = float(p['strike'])
            intrinsic = max(0.0, close - k) if p['side'] == 'CE' else max(0.0, k - close)
            self._close(p, now, intrinsic, 'expired_settled', book=None, orders=1)
            p['settle_index_close'] = close

    # ------------------------------------------------------------ evidence
    def _poll_log(self, now: datetime, day: Dict[str, Any],
                  books: Dict[str, Book]) -> None:
        if (self._last_poll_log is not None and
                (now - self._last_poll_log).total_seconds() < self.cfg.poll_log_every_sec):
            return
        self._last_poll_log = now
        snap = {k: [b.ltp, b.bid, b.ask] for k, b in books.items()}
        self.store.event(now, 'POLL', books=snap)

    # -------------------------------------------------------------- digest
    def eod_digest(self, today: date) -> str:
        day = self.store.day(today)
        iso = today.isoformat()
        pos = self.store.data['positions']
        entered = [p for p in pos if p['entry_date'] == iso]
        exited = [p for p in pos if p.get('exit_date') == iso]
        still = self.store.open_positions()
        closed = self.store.closed_positions()
        lines = ['EOD %s (%s)' % (iso, day.get('cls', 'no mark'))]
        for side in SIDES:
            m = day['marks'].get(side)
            if m:
                state = ('MISSED' if side in day['missed'] else
                         'BOUGHT' if day['fired'].get(side) else 'no trigger')
                lines.append('%s %s mark %.2f trig %.2f: %s'
                             % (side, m['symbol'], m['mark_price'], m['trigger'], state))
        if not day.get('mark_done'):
            lines.append('NO MARK TODAY')
        lines.append('entered %d, exited %d, carried %d'
                     % (len(entered), len(exited), len(still)))
        today_net = sum(p['net_pnl'] for p in exited)
        lines.append('today net Rs %+.0f | book net Rs %+.0f over %d closed'
                     % (today_net, sum(p['net_pnl'] for p in closed), len(closed)))
        return '\n'.join(lines)
