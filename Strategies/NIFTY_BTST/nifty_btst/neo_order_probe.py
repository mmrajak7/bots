"""Stage 3: the order probe [Q4]. REAL ORDERS, 1 lot, on one named option.

Run by the owner at the box, on the cheapest far-OTM weekly, inside market
hours. It refuses: more than one lot, a premium above PROBE_MAX_PREMIUM_RS
(a code constant the CLI can only lower), a symbol the scrip master cannot
map, a lot size NEO and Kite disagree on, and any run without the explicit
flag and the PROBE_ARM file (which can never arm strategy entries).

Steps (`--step`):
  all   A rest-and-cancel: BUY far below the bid -> read -> cancel -> read back,
          with the cancel latency and whether `GuiOrdId` echoes. The run does
          NOT continue while A is not terminal (NEO-19).
        B buy at the ask: BUY LIMIT at the live ask, wait up to 30 s, else
          cancel and read back.
        C sell what is held: the selling guard, then SELL LIMIT at the live
          bid (re-quoted every round, NEO-24), chased.
  hold  B only, and KEEP the lot overnight. This is the measurement that
          settles NEO-16 (does the positions report list a carry-forward row
          next morning?) and NEO-06 (do yesterday's orders leave the DAY
          book?). The owner runs `sell` the next morning.
  sell  Read and PRINT positions, order book, trade book and Holdings for the
          symbol, then C on what the row shows. With the row absent the guard
          refuses unless BOTH `--allow-missing-row --owner-confirms-held` are
          given AND the journal shows the hold filled with no filled probe
          SELL since. A completed SELL on the symbol today refuses every run
          (R3-13): re-running cannot sell twice.

Every send goes through the engine's journal with position id 0 and a
per-run attempt base, so two probes in a day never share a tag (NEO-33).
Whatever happens, the last lines say what is HELD (try/finally, NEO-20); the
CLI sends them to Telegram. Before "flat" is printed, positions and live
orders are read: a resting BUY is not flat.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .broker import (Broker, BrokerAuth, BrokerRejected, BrokerUnavailable, BrokerUnclear,
                     parse_neo_time as _parse_time,
                     OrderRequest, OrderView, redact_row)
from .journal import Journal, make_tag
from .neo_symbols import ScripMap, ScripMasterError, ScripRow
from .pricing import Book
from .ticks import price_str, round_tick

PROBE_MAX_PREMIUM_RS = 1000.0
PROBE_LOTS = 1
FILL_WAIT_SEC = 30.0
CANCEL_WAIT_SEC = 20.0
UNCLEAR_WAIT_SEC = 30.0
SELL_CHASES = 10
POLL_SEC = 0.5

Out = Callable[[str], None]
QuoteFn = Callable[[], Optional[Book]]


class ProbeRefused(RuntimeError):
    pass


class OrderProbe:
    def __init__(self, broker: Broker, journal: Journal, clock: Callable[[], datetime],
                 sleep: Callable[[float], None], out: Out = print,
                 allow_missing_row: bool = False, owner_confirms_held: bool = False,
                 max_premium: float = PROBE_MAX_PREMIUM_RS):
        self.broker, self.journal, self.clock, self.sleep, self.out = broker, journal, clock, sleep, out
        self.allow_missing_row = allow_missing_row
        self.owner_confirms_held = owner_confirms_held
        self.max_premium = max_premium
        self.held = 0
        #: set the moment an order of this run cannot be read as TERMINAL:
        #: nothing is sent again in the run and the report says UNKNOWN (R2-06)
        self.stopped = ''
        self.report: List[str] = []
        #: identity strings never printed or reported (the UCC; R3 part A)
        self.redactions: List[str] = []
        base = self.journal.sends_today(self.clock().date(), include_probe=True)
        self._seq = {'ENTRY': base, 'EXIT': base}

    # -- helpers -----------------------------------------------------------
    def _say(self, text: str) -> None:
        for secret in self.redactions:
            if secret:
                text = text.replace(secret, '<redacted>')
        self.report.append(text)
        self.out(text)

    def _wait(self, order_no: str, until_terminal: bool, limit: float) -> Optional[OrderView]:
        t0 = self.clock()
        last: Optional[OrderView] = None
        while (self.clock() - t0).total_seconds() <= limit:
            try:
                o = self.broker.order(order_no)
            except (BrokerUnavailable, BrokerAuth) as exc:
                self._say('  read failed: %s' % exc)
                o = None
            if o is not None:
                last = o
                if o.terminal or not until_terminal:
                    return o
            self.sleep(POLL_SEC)
        return last

    def _send(self, row: ScripRow, side: str, qty: int, price: float, kind: str) -> Optional[str]:
        if self.stopped:
            self._say('  NOT sending (%s): the run is stopped' % self.stopped)
            return None
        if side == 'B' and price * qty > self.max_premium:
            # re-checked on the LIVE quote before every BUY (R2-17)
            self._say('  NOT buying: premium Rs %.0f above Rs %.0f' % (price * qty, self.max_premium))
            return None
        self._seq[kind] += 1
        tag = make_tag(self.clock().date(), 0, kind, self._seq[kind])
        pstr = price_str(price, row.tick_size, 'up' if side == 'B' else 'down')
        self.journal.intent(self.clock(), tag, kind, 0, row.trading_symbol, side, qty, pstr,
                            probe=True)
        req = OrderRequest(symbol=row.trading_symbol, token=row.token, side=side, qty=qty,
                           price=pstr, tag=tag)
        try:
            no = self.broker.place(req)
        except BrokerRejected as exc:
            self.journal.not_placed(self.clock(), tag, str(exc))
            self._say('  %s %s x%d @ %s REJECTED at placement: %s' % (side, row.trading_symbol,
                                                                      qty, pstr, exc))
            return None
        except (BrokerUnclear, BrokerAuth) as exc:
            self._say('  %s send UNCLEAR (%s): searching the book by tag %s for %.0fs'
                      % (side, exc, tag, UNCLEAR_WAIT_SEC))
            found = self._search(tag, row.trading_symbol)
            if found is None:
                self._say('  not in the book after %.0fs. NOT concluded placed; the tag '
                          'stays unresolved. STOPPING — check the app for %s.'
                          % (UNCLEAR_WAIT_SEC, tag))
                if side == 'B':
                    self.held += qty                 # it may be at the exchange (R3-14)
                self._stop('unclear %s send %s unresolved' % (side, tag))
                raise ProbeRefused('unclear send %s unresolved' % tag)
            no = found
            self.journal.bound(self.clock(), tag, no, 'tag')
        else:
            self.journal.placed(self.clock(), tag, no)
        self._say('  %s %s x%d @ %s -> order %s (tag %s)' % (side, row.trading_symbol, qty,
                                                            pstr, no, tag))
        return no

    def _search(self, tag: str, symbol: str) -> Optional[str]:
        t0 = self.clock()
        reads = 0
        while (self.clock() - t0).total_seconds() <= UNCLEAR_WAIT_SEC:
            try:
                hits = [o for o in self.broker.order_book() if o.tag == tag]
                reads += 1
            except (BrokerUnavailable, BrokerAuth) as exc:
                self._say('  book unreadable (%s)' % exc)
                hits = []
            if hits:
                return hits[0].order_no
            self.sleep(2.0)
        return None

    def _cancel_and_wait(self, no: str) -> Optional[OrderView]:
        t0 = self.clock()
        try:
            self.broker.cancel(no)
        except BrokerRejected as exc:
            self._say('  cancel %s refused: %s (reading back)' % (no, exc))
        except (BrokerUnclear, BrokerAuth) as exc:
            self._say('  cancel %s unclear: %s (reading back)' % (no, exc))
        o = self._wait(no, until_terminal=True, limit=CANCEL_WAIT_SEC)
        dt = (self.clock() - t0).total_seconds()
        self._say('  cancel -> %s after %.1fs (filled %d)' % (o.status if o else 'NO READ', dt,
                                                           o.filled if o else 0))
        if o is None or not o.terminal:
            self._stop('order %s could not be read as terminal after a cancel' % no)
        return o

    def _stop(self, why: str) -> None:
        """Stop the run for good: an order whose state is not known is a lot
        that may still be bought or sold; a second order on top of it is how
        a short happens (R2-06, R2-15)."""
        if not self.stopped:
            self.stopped = why
            self._say('  STOPPED: %s. Nothing more is sent in this run. State UNKNOWN: check '
                      'the app.' % why)

    def _snapshot(self, row: ScripRow) -> Dict[str, Any]:
        """What the broker shows for the symbol right now; printed with the
        identity keys redacted (R2-18)."""
        snap: Dict[str, Any] = {}
        try:
            pos = [p for p in self.broker.positions() if p.symbol == row.trading_symbol]
            snap['positions'] = [redact_row(p.raw) for p in pos]
            snap['net'] = sum(p.net_qty for p in pos)
            snap['row_present'] = bool(pos)
        except (BrokerUnavailable, BrokerAuth) as exc:
            snap['positions_error'] = str(exc)
        try:
            snap['orders'] = [redact_row(o.raw) for o in self.broker.order_book()
                              if o.symbol == row.trading_symbol]
        except (BrokerUnavailable, BrokerAuth) as exc:
            snap['orders_error'] = str(exc)
        try:
            snap['trades'] = [redact_row(o.raw) for o in self.broker.trades()
                              if o.symbol == row.trading_symbol]
        except (BrokerUnavailable, BrokerAuth) as exc:
            snap['trades_error'] = str(exc)
        try:
            h = self.broker.holdings(force=True)          # the measurement itself (R3-17)
            snap['holdings'] = None if h is None else [redact_row(x.raw) for x in h
                                                       if x.symbol == row.trading_symbol]
        except (BrokerUnavailable, BrokerAuth) as exc:
            snap['holdings_error'] = str(exc)
        return snap

    def _resolve_row(self, scrips: ScripMap, kite_row: Dict[str, Any]) -> ScripRow:
        try:
            row = scrips.find(datetime.fromisoformat(str(kite_row['expiry'])).date(),
                              float(kite_row['strike']), str(kite_row['instrument_type']))
        except ScripMasterError as exc:
            raise ProbeRefused('no NEO row for %s: %s' % (kite_row['tradingsymbol'], exc))
        if row.lot_size != int(kite_row['lot_size']):
            raise ProbeRefused('lot size NEO %d != Kite %d' % (row.lot_size, kite_row['lot_size']))
        return row

    @staticmethod
    def _usable(book: Optional[Book]) -> bool:
        return book is not None and book.bid > 0 and book.ask > 0 and book.ask >= book.bid

    # -- the probe -----------------------------------------------------------
    def run(self, scrips: ScripMap, kite_row: Dict[str, Any], quote: QuoteFn,
            max_premium: float, step: str = 'all') -> int:
        if step not in ('all', 'hold', 'sell'):
            raise ProbeRefused('step must be all, hold or sell')
        if max_premium > PROBE_MAX_PREMIUM_RS:
            raise ProbeRefused('max premium Rs %.0f above the code ceiling Rs %.0f'
                               % (max_premium, PROBE_MAX_PREMIUM_RS))
        self.max_premium = max_premium
        row = self._resolve_row(scrips, kite_row)
        qty = row.lot_size * PROBE_LOTS
        book = quote()
        if step != 'sell':
            if not self._usable(book):
                raise ProbeRefused('book not usable: %s' % (book,))
            assert book is not None
            if book.ask * qty > max_premium:
                raise ProbeRefused('premium Rs %.0f (ask %.2f x %d) above Rs %.0f: pick a cheaper '
                                   'option' % (book.ask * qty, book.ask, qty, max_premium))
        self._say('ORDER PROBE [%s] %s -> NEO %s token %s lot %d tick %.2f | %s'
                  % (step, kite_row['tradingsymbol'], row.trading_symbol, row.token,
                     row.lot_size, row.tick_size,
                     'bid %.2f ask %.2f' % (book.bid, book.ask) if book else 'no quote'))
        if step == 'all':
            # a filled SELL on the symbol today means a lot of this symbol was
            # already sold once: a second `all` would buy and then refuse to
            # sell, stranding the lot (R4-22)
            try:
                sold = self._filled_sells_today(row, self.broker.order_book() + self.broker.trades())
            except (BrokerUnavailable, BrokerAuth) as exc:
                raise ProbeRefused('cannot read the book before buying: %s' % exc)
            if sold:
                raise ProbeRefused('a SELL with a fill already exists today on %s (%s): not buying '
                                   'again' % (row.trading_symbol, ', '.join(o.order_no for o in sold)))
        rc = 1
        try:
            if step == 'all':
                rc = self._run_all(row, qty, quote)
            elif step == 'hold':
                rc = self._run_hold(row, qty, quote)
            else:
                rc = self._run_sell(row, quote)
        finally:
            if self.stopped:
                self._say('*** RUN STOPPED: %s. %d HELD BY THE BOOK RECORD; broker state UNKNOWN. '
                          'CHECK THE APP before any other probe. Do NOT re-run. ***'
                          % (self.stopped, self.held))
                rc = 1
            elif step != 'hold':
                flat = self._verify_flat(row)
                if self.held > 0 or not flat:
                    self._say('*** %d OF %s HELD BY THE BOOK RECORD; broker says %s. The reports '
                              'may be LAGGING (measurement 3): do NOT re-run this probe; check '
                              'the app, and sell there only if the lot is really held. ***'
                              % (self.held, row.trading_symbol, 'FLAT' if flat else 'NOT FLAT'))
                    rc = 1
                else:
                    self._say('probe complete, flat (positions and live orders read)')
        return rc

    def _filled_sells_today(self, row: ScripRow, rows: List[OrderView]) -> List[OrderView]:
        """SELLs with a fill on the symbol dated TODAY (R4-22); an undated row
        counts (fail closed)."""
        today = self.clock().date()
        out = []
        for o in rows:
            if o.symbol != row.trading_symbol or o.side != 'S' or o.filled <= 0:
                continue
            t = _parse_time(o.placed_at, self.clock().tzinfo)
            if t is None or t.date() == today:
                out.append(o)
        return out

    def _verify_flat(self, row: ScripRow) -> bool:
        try:
            net = self.broker.net_long(row.trading_symbol, 'NRML')
            live = [o for o in self.broker.order_book() if o.symbol == row.trading_symbol and o.live]
        except (BrokerUnavailable, BrokerAuth) as exc:
            self._say('  flat check: broker unreadable (%s): NOT verified' % exc)
            return False
        if live:
            self._say('  flat check: LIVE ORDERS still on the symbol: %s'
                      % ', '.join('%s %s(%s)' % (o.side, o.order_no, o.status) for o in live))
        if net != 0:
            self._say('  flat check: broker net %d' % net)
        return net == 0 and not live

    def _run_all(self, row: ScripRow, qty: int, quote: QuoteFn) -> int:
        book = quote()
        assert book is not None
        self._say('A) rest-and-cancel')
        rest = max(round_tick(book.bid * 0.5, row.tick_size, 'down'), row.tick_size)
        no = self._send(row, 'B', qty, rest, 'ENTRY')
        if no:
            o = self._wait(no, until_terminal=False, limit=3.0)
            self._say('  read: %s' % (o.status if o else 'NOT IN BOOK'))
            if o:
                self._say('  GuiOrdId echo: %s' % ('YES (%s)' % o.tag if o.tag else 'NO'))
            o = self._cancel_and_wait(no)
            if o is None or not o.terminal:
                return 1                                 # _cancel_and_wait stopped the run
            if o.filled > 0:
                self.held += o.filled
                self._say('  the resting order FILLED %d (price %.2f)' % (o.filled, o.avg_price))
        if self.held > 0:
            self._say('B) skipped: %d already held' % self.held)       # R2-17
        else:
            self._buy_at_ask(row, qty, quote)
        if self.stopped:
            return 1
        if self.held > 0:
            self._sell(row, quote)
        return 0 if self.held == 0 and not self.stopped else 1

    def _run_hold(self, row: ScripRow, qty: int, quote: QuoteFn) -> int:
        self._say('HOLD) buy at the ask and keep it overnight')
        self._buy_at_ask(row, qty, quote)
        if self.stopped:
            return 1
        snap = self._snapshot(row)
        self._say('  after the buy, broker shows: net %s, positions %s, orders %d'
                  % (snap.get('net'), snap.get('positions'), len(snap.get('orders') or [])))
        if self.held > 0:
            self.journal.probe_held(self.clock(), row.trading_symbol, self.held)
            self._say('HELD %d of %s overnight ON PURPOSE. Tomorrow after 09:15 run this probe '
                      'with --step sell on the same symbol.' % (self.held, row.trading_symbol))
            return 0
        self._say('nothing held; the hold step did not get a fill')
        return 1

    def _run_sell(self, row: ScripRow, quote: QuoteFn) -> int:
        self._say('SELL) what the broker shows this morning for %s:' % row.trading_symbol)
        snap = self._snapshot(row)
        for k, v in snap.items():
            self._say('  %s: %s' % (k, v))
        if 'positions_error' in snap:
            self._say('  carry-forward row UNKNOWN: positions could not be read (%s). Nothing '
                      'sent.' % snap['positions_error'])                       # R3-28
            return 1
        net = int(snap.get('net') or 0)
        self._say('  carry-forward row %s' % ('PRESENT (net %d)' % net
                                              if snap.get('row_present') else 'ABSENT'))
        if net > 0:
            self.held = net
            self._sell(row, quote)
            return 0 if self.held == 0 and not self.stopped else 1
        if net < 0:
            self._say('  the row shows a SHORT (%d). Nothing sent. Check the app.' % net)
            return 1
        # no positive row: a SELL needs the journal to show the hold filled and
        # was not sold since, AND the owner's explicit statement (R2-14)
        if not self.allow_missing_row or not self.owner_confirms_held:
            self._say('  no row. The guard needs positive evidence; nothing sold. If the app '
                      'shows the lot, re-run with --allow-missing-row --owner-confirms-held; '
                      'else sell in the app.')
            return 1
        if not self.journal.probe_hold_open(row.trading_symbol):
            self._say('  the journal shows NO filled probe hold on %s still open: not selling '
                      'a lot the record does not know.' % row.trading_symbol)
            return 1
        self.held = int(row.lot_size)
        self._sell(row, quote, allow_missing=True)
        return 0 if self.held == 0 and not self.stopped else 1

    def _buy_at_ask(self, row: ScripRow, qty: int, quote: QuoteFn) -> None:
        book = quote()
        if not self._usable(book):
            self._say('B) no usable quote; not buying')
            return
        assert book is not None
        self._say('B) buy at the ask %.2f' % book.ask)
        no = self._send(row, 'B', qty, book.ask, 'ENTRY')
        if not no:
            return
        o = self._wait(no, until_terminal=True, limit=FILL_WAIT_SEC)
        if o and o.status == 'complete':
            self.held += o.filled or qty
            self._say('  FILLED %d @ %.2f' % (o.filled or qty, o.avg_price))
            return
        self._say('  not filled in %.0fs (status %s); cancelling'
                  % (FILL_WAIT_SEC, o.status if o else 'NO READ'))
        o = self._cancel_and_wait(no)
        if o and o.filled > 0:
            self.held += o.filled
        if o is None or not o.terminal:
            self.held += qty                             # it may still fill: count it as held

    def _sell(self, row: ScripRow, quote: QuoteFn, allow_missing: bool = False) -> None:
        self._say('C) sell %d with the selling guard' % self.held)
        sold = 0
        for n in range(SELL_CHASES):
            want = self.held - sold
            try:
                net = self.broker.net_long(row.trading_symbol, 'NRML')
                book_rows = self.broker.order_book()
                trades = self.broker.trades()
            except (BrokerUnavailable, BrokerAuth) as exc:
                self._say('  guard: broker unreadable (%s); not selling this round' % exc)
                self.sleep(2.0)
                continue
            live = [o for o in book_rows if o.symbol == row.trading_symbol and o.side == 'S' and o.live]
            filled = self._filled_sells_today(row, book_rows + trades)
            if filled:
                # a completed SELL on the symbol today refuses every run,
                # whatever `net` says: positions may lag (R3-13)
                self._say('  guard REFUSED: a SELL with a fill already exists today on %s (%s). '
                          'Not selling again; the positions report may be lagging.'
                          % (row.trading_symbol, ', '.join(o.order_no for o in filled)))
                break
            ok = not live and (net >= want or (allow_missing and net == 0))    # R4-06
            if not ok:
                self._say('  guard REFUSED: net long %d, want %d, live sells %d'
                          % (net, want, len(live)))
                self.sleep(2.0)
                continue
            book = quote()
            if not self._usable(book):
                self._say('  no usable quote this round')
                self.sleep(2.0)
                continue
            assert book is not None
            no = self._send(row, 'S', want, book.bid, 'EXIT')
            if not no:
                break
            o = self._wait(no, until_terminal=True, limit=5.0)
            if o and o.status == 'complete':
                sold += o.filled or want
                self.journal.probe_sold(self.clock(), row.trading_symbol, o.filled or want)
                self._say('  SOLD %d @ %.2f' % (o.filled or want, o.avg_price))
                break
            o = self._cancel_and_wait(no)
            if o and o.filled:
                sold += o.filled
                self.journal.probe_sold(self.clock(), row.trading_symbol, o.filled)
            if o is None or not o.terminal:
                break                                    # the run is stopped; never a 2nd SELL
            if sold >= self.held:
                break
        self.held -= sold
