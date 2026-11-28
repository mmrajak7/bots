"""Live naked-long ENTRY (naked live build P3).

Design: `docs/NAKED_LIVE_DESIGN.md` Q1-Q10, G1-G4, F4; limits in
`docs/NAKED_100_TRADE_TEST.md` section 11. Plan: `tasks/todo.md` P3.

WHAT IT DOES. When the paper cohort enters a BCS on a signal (the test's
population, unchanged), this buys ONE lot of the same LONG leg with real money
-- or, in `rehearsal`, with a fake fill that is journalled and recorded exactly
as a real one would be. It never touches the paper record's money and never
blocks the paper path: every failure here is logged, stamped on the parent and
alerted, and the cycle carries on.

THE ORDER OF THE GATES IS THE DESIGN (cheapest and most absolute first; nothing
reaches a broker before every one has passed):

  1. `naked_live` is not 'off'                     (the arming switch)
  2. the halt was evaluated THIS cycle and allows  (`naked_halt.evaluate`)
  3. the M2 entry budget is open and nothing for this signal is in flight
  4. no unresolved naked order anywhere            (an UNCLEAR order = unknown book)
  5. fewer than `naked_max_open` open naked records
  6. no open naked record on this stock
  7. before `naked_entry_cutoff`                   (G1: late -> `skipped: late`)
  8. a fresh, reliable quote with ask x lot <= `naked_max_premium` (skip, never resize)
  9. dry-construct the record and validate it      (F4: BEFORE any order)
 10. a broker exists for this mode                 (live: the NEO adapter)

THE BROKER IS A SEAM, NOT A BRANCH. Orders go through an object with one
method, `buy(symbol, qty, limit, ctx) -> BuyResult`. `RehearsalBroker` fills at
the limit. The live broker is Kotak NEO (owner, 2026-11-27: zero brokerage,
same broker as BTST) and is NOT wired yet -- it waits on the shared `BOTS/kotak_neo`
package via `zebra/naked_neo.NeoBroker` (P3b, 2026-11-27). Nothing here names
a broker beyond `broker_for`.

THE TAG IS THE WITNESS. Before a live order is sent the broker calls
`ctx['on_tag']`, which stamps the parent `naked_twin = {state: 'sending', tag}`.
`sending` is an UNRESOLVED state: if the process dies between the send and the
reply, gate 4 refuses every new entry and `naked_adopt` resolves the order from
NEO's book by that tag.

A BuyResult says one of four things, and each has exactly one handling:

  filled    -> write the record (premium over the cap after the fill: record
               it anyway, it is a real position, and alert red -- G3)
  rejected  -> `retry=True` (price/band/stale): re-quote, re-gate the premium,
               send ONE more order. `retry=False` (margin/funds/RMS/permission/
               IP/unknown): STOP and alert red. Never more than
               `naked_max_orders_per_signal` orders for one signal.
  unfilled  -> the broker CONFIRMED nothing filled and nothing rests. Skip.
  unclear   -> the order MAY be at the broker. The journal intent is left
               WITHOUT a result on purpose (that is the witness), the parent is
               stamped `unclear`, and gate 4 then refuses EVERY new naked entry
               until the adoption sweep (with the broker's order book) resolves
               it. A duplicate costs a real position; a missed entry costs
               nothing (`feedback_no_rush_to_enter`).

The outcome of every attempt is stamped on the PAPER parent as `naked_twin`
({state, reason, at, ...}). That stamp is how a reader sees why a signal has
no live twin, and it is the in-store half of gate 4.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from . import config as cfg
from . import naked_halt
from . import strikes as strikes_mod
from .trade_store import ZebraStore, build_naked_record, validate_naked_record

logger = logging.getLogger(__name__)

#: `naked_twin.state` values that mean an order MAY exist at the broker with
#: no record. Gate 4 refuses every new entry while any parent carries one.
UNRESOLVED_STATES = ('sending', 'unclear', 'unrecorded')

TWIN_FIELD = 'naked_twin'


# -- the broker seam ------------------------------------------------------------

@dataclass
class BuyResult:
    status: str                      # filled | rejected | unfilled | unclear
    fill_price: Optional[float] = None
    order_id: Optional[str] = None
    message: str = ''
    retry: bool = False              # rejected for a price/band/stale reason
    tag: Optional[str] = None        # the broker's order tag (NEO GuiOrdId)
    neo_symbol: Optional[str] = None # the broker's trading symbol for the leg
    neo_token: Optional[str] = None  # the broker's instrument token (the exit needs it)
    filled_qty: int = 0              # shares the book shows filled (unclear only)

    STATUSES = ('filled', 'rejected', 'unfilled', 'unclear')

    def __post_init__(self):
        if self.status not in self.STATUSES:
            raise ValueError('BuyResult status %r' % self.status)
        if self.status == 'filled':
            try:
                ok = float(self.fill_price) > 0
            except (TypeError, ValueError):
                ok = False
            if not ok:
                # A "fill" with no usable price cannot be recorded, so it is
                # not a fill this code can act on: it is an unknown book.
                raise ValueError('filled without a positive fill_price')


class RehearsalBroker:
    """Fills every order at its limit. Places nothing anywhere."""
    name = 'rehearsal'

    def __init__(self):
        self.orders = []

    def buy(self, symbol: str, qty: int, limit: float, ctx: dict) -> BuyResult:
        n = len(self.orders) + 1
        self.orders.append({'symbol': symbol, 'qty': qty, 'limit': limit,
                            'ctx': dict(ctx)})
        return BuyResult('filled', fill_price=float(limit),
                         order_id='REHEARSAL-%s-%d' % (ctx.get('trade_id'), n),
                         message='rehearsal fill at the ask')


def broker_for(mode: str):
    """The broker for this mode, or None when none is wired.

    'live' is Kotak NEO on the shared session (`naked_neo.NeoBroker`). Tests
    inject theirs.
    """
    if mode == 'rehearsal':
        return RehearsalBroker()
    if mode == 'live':
        from .naked_neo import NeoBroker
        return NeoBroker()
    return None


# -- per-cycle halt verdict --------------------------------------------------------

#: The halt verdict of THIS cycle. Set by `begin_cycle`, cleared by
#: `end_cycle`. The entry hook refuses when it is None: a verdict from an
#: earlier cycle (or none at all) is not a verdict about now.
_verdict: Optional[dict] = None


def mode() -> str:
    return cfg.NAKED_LIVE


def _send_default(msg: str, dry_run: bool = False) -> bool:
    from .monitor import _send_telegram          # lazy: monitor imports us
    return _send_telegram(msg, dry_run=dry_run)


def _marks(kite, records: list) -> dict:
    """{id: BID} for open naked records whose book is reliable right now."""
    open_ = [t for t in records if t.get('status') != 'exited']
    if not open_ or kite is None:
        return {}
    try:
        strikes_mod.prefetch_quotes(kite, [t['long_symbol'] for t in open_])
    except Exception as e:
        logger.warning('naked marks prefetch failed: %s', e)
    out = {}
    for t in open_:
        try:
            q = strikes_mod._quote_option(kite, t['long_symbol'])
        except Exception as e:
            logger.warning('naked mark #%s %s failed: %s', t.get('id'),
                           t.get('long_symbol'), e)
            continue
        if q.get('reliable') and (q.get('bid') or 0) > 0:
            out[str(t['id'])] = float(q['bid'])
    return out


def _format_event(m: str, ev: dict) -> str:
    if ev.get('event') == 'kill':
        return ('\U0001F6D1 NAKED %s KILLED (%s)\n%s\nNo NEW entries. Open '
                'positions keep their exits.\nRestart (Telegram, read by the '
                'zebra cycle while killed): /nresume %s %s\nor at the box: '
                'python -m zebra resume %s --mode %s'
                % (m.upper(), ev.get('kind'), ev.get('reason'),
                   m, ev.get('code'), ev.get('code'), m))
    return ('⏸ NAKED %s PAUSED\n%s\nNo new entries through %s.'
            % (m.upper(), ev.get('reason'), ev.get('through') or '?'))


def begin_cycle(store: ZebraStore, kite, now: Optional[datetime] = None,
                send: Optional[Callable] = None, dry_run: bool = False) -> Optional[dict]:
    """Evaluate the halt once for this cycle. Never raises.

    The ONLY caller is zebra's 5-minute cycle (`naked_halt` caller contract):
    the 5-second monitor must never evaluate, or the peak would be sampled at
    a different rate from the one the limits were chosen on.
    """
    global _verdict
    _verdict = None
    m = mode()
    if m == 'off':
        return None
    now = now or datetime.now(cfg.IST)
    send = _send_default if send is None else send
    try:
        # WHOLE BOOK: the halt's own `book()` narrows to this mode's naked records.
        recs = naked_halt.book(store.load_trades(), m)
        v = naked_halt.evaluate(recs, _marks(kite, recs), now, m)
    except Exception as e:                       # evaluate never raises; belt and braces
        v = {'allowed': False, 'reason': 'HALTED (fails closed): %s' % e,
             'events': []}
    for ev in v.get('events') or []:
        try:
            send(_format_event(m, ev), dry_run=dry_run)
        except Exception as e:
            logger.error('naked halt alert failed: %s', e)
    logger.info('NAKED %s halt: %s | equity %s peak %s dd %s%%',
                m.upper(), 'CLEAR' if v.get('allowed') else v.get('reason'),
                v.get('equity'), v.get('peak'), v.get('drawdown_pct'))
    _verdict = v
    return v


def end_cycle() -> None:
    global _verdict
    _verdict = None


# -- the entry hook -----------------------------------------------------------------

@dataclass
class Outcome:
    state: str                       # entered | skipped | refused | unclear | unrecorded | off
    reason: str = ''
    record: Optional[dict] = None
    orders: int = 0
    extra: dict = field(default_factory=dict)


def _stamp(store: ZebraStore, parent_id, out: Outcome, now: datetime) -> None:
    """Write the attempt's outcome on the PAPER parent. Never raises."""
    rec = {'state': out.state, 'reason': out.reason[:300], 'mode': mode(),
           'at': now.strftime('%Y-%m-%d %H:%M:%S'), 'orders': out.orders}
    if out.record:
        rec['id'] = out.record.get('id')
    rec.update(out.extra)
    try:
        store.update_trade_fields(parent_id, **{TWIN_FIELD: rec})
    except Exception as e:
        logger.error('naked twin stamp on #%s failed: %s', parent_id, e)


def unresolved_twins(trades) -> list:
    """Parents whose naked order may be at the broker with no record."""
    return [t for t in trades
            if (t.get(TWIN_FIELD) or {}).get('state') in UNRESOLVED_STATES]


def _cutoff_passed(now: datetime) -> bool:
    h, mi = cfg.NAKED_ENTRY_CUTOFF
    return (now.hour, now.minute) >= (h, mi)


def _quote(kite, symbol: str) -> tuple:
    """(quote, problem). A quote the order may be priced from, or why not."""
    try:
        q = strikes_mod._quote_option(kite, symbol)
    except Exception as e:
        return None, 'quote failed: %s' % e
    if q.get('error'):
        return None, 'quote error: %s' % q.get('error')
    if not q.get('reliable'):
        return None, 'unreliable book: %s' % q.get('unreliable_reason')
    ask = q.get('ask') or 0
    if ask <= 0 or (q.get('ask_qty') or 0) <= 0:
        return None, 'no ask at the touch'
    return q, ''


def _premium_problem(ask: float, lot: int) -> str:
    prem = ask * lot
    if prem > cfg.NAKED_MAX_PREMIUM + 1e-9:
        return 'premium Rs %.0f (ask %.2f x %d) over the Rs %.0f cap -- skip, never resize' % (
            prem, ask, lot, cfg.NAKED_MAX_PREMIUM)
    return ''


def exit_engine_problem(m: str) -> str:
    """Why no exit engine could SELL a position this mode would buy, or ''.

    The fail-closed backstop that `broker_for('live') -> None` used to be
    (design Q9: live needs the monitor's --dry-run removed). Read from the
    5-second monitor's heartbeat: fresh, armed, and running the naked engine.
    Rehearsal fills are fake, so a dry-run monitor may book them -- it must
    still be alive and running the engine.
    """
    from .monitor import read_exit_engine_heartbeat     # lazy: monitor imports us
    hb = read_exit_engine_heartbeat()
    beat = hb.get('beat') or {}
    ok_states = ('ok',) if m == 'live' else ('ok', 'dry_run')
    if hb.get('state') not in ok_states:
        return 'no exit engine able to sell it: %s (%s)' % (hb.get('state'), hb.get('detail'))
    if not beat.get('naked_engine'):
        return 'the exit monitor is running without the naked exit engine'
    return ''


_real_exit_engine_problem = exit_engine_problem


def _gates(store: ZebraStore, kite, parent: dict, now: datetime, m: str):
    """Gates 2-9. Returns (Outcome, None) to stop, or (None, (quote, dry_rec))."""
    from .monitor import _entry_already_in_flight, entry_budget_open   # lazy

    v = _verdict
    if v is None:
        return Outcome('refused', 'halt not evaluated this cycle (fails closed)'), None
    why = exit_engine_problem(m)
    if why:
        return Outcome('refused', why), None
    if not v.get('allowed'):
        return Outcome('refused', 'halt: %s' % v.get('reason')), None
    if not entry_budget_open():
        return Outcome('skipped', 'entry budget for this cycle spent'), None
    why = _entry_already_in_flight(store, parent)
    if why:
        return Outcome('refused', 'in flight: %s' % why), None

    # WHOLE BOOK: liveness -- an unresolved order on ANY parent is an unknown book.
    trades = store.load_trades()
    stuck = unresolved_twins(trades)
    if stuck:
        return Outcome('refused', 'unresolved naked order on #%s (%s) -- no new '
                       'entries until it is resolved'
                       % (stuck[0]['id'], stuck[0][TWIN_FIELD].get('state'))), None
    open_ = [t for t in naked_halt.book(trades, m) if t.get('status') != 'exited']
    if len(open_) >= cfg.NAKED_MAX_OPEN:
        return Outcome('skipped', 'cap: %d open naked (max %d)'
                       % (len(open_), cfg.NAKED_MAX_OPEN)), None
    same = [t for t in open_ if t.get('stock') == parent.get('stock')]
    if same:
        return Outcome('skipped', 'already holding %s as #%s'
                       % (parent.get('stock'), same[0].get('id'))), None
    if _cutoff_passed(now):
        return Outcome('skipped', 'late: past %02d:%02d' % cfg.NAKED_ENTRY_CUTOFF), None

    sym, lot = parent.get('long_symbol'), int(parent.get('lot_size') or 0)
    if not sym or lot <= 0:
        return Outcome('refused', 'parent has no long_symbol/lot_size'), None
    q, prob = _quote(kite, sym)
    if q is None:
        return Outcome('skipped', prob), None
    prob = _premium_problem(float(q['ask']), lot)
    if prob:
        return Outcome('skipped', prob, extra={'ask': q['ask']}), None

    dry = build_naked_record(parent, long_symbol=sym,
                             long_strike=parent.get('long_strike'), lot_size=lot,
                             fill_price=float(q['ask']), now=now, quote=q,
                             rehearsal=(m == 'rehearsal'),
                             override=cfg.NAKED_OVERRIDE)
    probs = validate_naked_record(dry)
    if probs:
        return Outcome('refused', 'record would not validate: ' + '; '.join(probs)), None
    return None, (q, dry)


def after_paper_entry(store: ZebraStore, kite, parent: dict, *,
                      now: Optional[datetime] = None, broker=None,
                      send: Optional[Callable] = None,
                      dry_run: bool = False) -> Outcome:
    """The P3 hook: called right after the paper BCS `parent` entered.

    NEVER raises -- the paper path must not depend on the money path.
    """
    m = mode()
    if m == 'off':
        return Outcome('off')
    now = now or datetime.now(cfg.IST)
    send = _send_default if send is None else send
    pid = parent.get('id')
    try:
        if dry_run:
            out = Outcome('skipped', 'zebra --dry-run: no naked entry')
            logger.info('NAKED #%s %s: %s', pid, parent.get('stock'), out.reason)
            return out
        out = _attempt(store, kite, parent, now, m, broker, send, dry_run)
    except Exception as e:
        logger.error('NAKED entry on #%s raised: %s', pid, e, exc_info=True)
        out = _after_raise(store, pid, e)
    _stamp(store, pid, out, now)
    level = logging.WARNING if out.state in ('refused', 'unclear', 'unrecorded') else logging.INFO
    logger.log(level, 'NAKED %s #%s %s: %s -- %s', m.upper(), pid,
               parent.get('stock'), out.state.upper(), out.reason)
    return out


def _after_raise(store, pid, e) -> Outcome:
    """The outcome when the attempt raised. If the `sending` witness is on the
    parent, an order MAY exist: keep it UNRESOLVED with its tag, never turn
    it into a resolved `refused` (the sweep would then never look)."""
    try:
        twin = dict((store.find(pid) or {}).get(TWIN_FIELD) or {})
    except Exception:
        twin = {}
    if twin.get('state') == 'sending' and twin.get('tag'):
        extra = {k: v for k, v in twin.items() if k not in ('state', 'reason', 'at')}
        return Outcome('unclear', 'raised after the send: %s' % e,
                       orders=int(twin.get('orders') or 0), extra=extra)
    return Outcome('refused', 'raised: %s' % e)


def _attempt(store, kite, parent, now, m, broker, send, dry_run) -> Outcome:
    from bcs import order_journal                 # lazy: bcs imports zebra

    stop, ok = _gates(store, kite, parent, now, m)
    if stop is not None:
        if stop.state == 'refused':
            send('⚠ NAKED %s #%s %s not entered: %s'
                 % (m.upper(), parent.get('id'), parent.get('stock'), stop.reason),
                 dry_run=dry_run)
        return stop
    q, _dry = ok
    broker = broker if broker is not None else broker_for(m)
    if broker is None:
        out = Outcome('refused', 'no %s broker wired' % m)
        send('\U0001F534 NAKED %s #%s %s not entered: %s'
             % (m.upper(), parent.get('id'), parent.get('stock'), out.reason),
             dry_run=dry_run)
        return out

    sym, lot = parent['long_symbol'], int(parent['lot_size'])
    orders = 0
    while True:
        limit = float(q['ask'])
        ctx = {'trade_id': parent['id'], 'stock': parent.get('stock'),
               'strategy': 'NAKED', 'reason': 'ENTRY', 'leg': 'naked_long',
               'mode': m, 'order': orders + 1}
        intent = order_journal.record_intent(
            symbol=sym, txn_type='BUY', qty=lot, price=limit, exchange='NFO',
            dry_run=(m != 'live'), context=ctx, log=logger)
        orders += 1
        n_order = orders
        sent_tag = {}

        def on_tag(tag, neo_symbol, _n=n_order, _limit=limit, _sent=sent_tag):
            _sent.update(tag=tag, neo_symbol=neo_symbol)
            # Persisted BEFORE the send; raises if it cannot be written, and
            # the broker then sends nothing.
            store.update_trade_fields(parent['id'], **{TWIN_FIELD: {
                'state': 'sending', 'mode': m, 'tag': tag, 'neo_symbol': neo_symbol,
                'limit': _limit, 'qty': lot, 'orders': _n, 'intent_id': intent,
                'at': now.strftime('%Y-%m-%d %H:%M:%S'),
                'reason': 'order %d being sent' % _n}})
        ctx = dict(ctx, parent=parent, on_tag=on_tag)
        try:
            res = broker.buy(sym, lot, limit, ctx)
        except Exception as e:
            # The broker raised: we cannot know whether the order reached the
            # exchange. That IS unclear -- never retried, intent left open.
            res = BuyResult('unclear', message='broker raised: %s' % e,
                            tag=sent_tag.get('tag'), neo_symbol=sent_tag.get('neo_symbol'))
        tag_extra = {k: v for k, v in (('tag', res.tag), ('neo_symbol', res.neo_symbol),
                                        ('order_id', res.order_id)) if v}
        if res.status != 'unclear':
            order_journal.record_result(
                intent, order_id=res.order_id,
                error=None if res.status == 'filled' else '%s: %s' % (res.status, res.message),
                dry_run=(m != 'live'), log=logger)

        if res.status == 'filled':
            return _record_fill(store, parent, res, q, now, m, orders, send, dry_run)
        if res.status == 'unclear':
            out = Outcome('unclear', 'order may be at the broker: %s' % res.message,
                          orders=orders, extra=dict(tag_extra, intent_id=intent,
                                                    qty=lot, limit=limit,
                                                    filled_qty=res.filled_qty))
            send('\U0001F534 NAKED %s #%s %s: ORDER STATE UNKNOWN (%s). No new '
                 'naked entries until it is resolved against the order book.'
                 % (m.upper(), parent['id'], parent.get('stock'), res.message),
                 dry_run=dry_run)
            return out
        if res.status == 'unfilled':
            return Outcome('skipped', 'not filled at %.2f (%s)' % (limit, res.message),
                           orders=orders, extra=tag_extra)
        # rejected
        if not res.retry or orders >= cfg.NAKED_MAX_ORDERS_PER_SIGNAL:
            out = Outcome('refused', 'rejected: %s' % res.message, orders=orders,
                          extra=tag_extra)
            send('\U0001F534 NAKED %s #%s %s REJECTED: %s%s'
                 % (m.upper(), parent['id'], parent.get('stock'), res.message,
                    '' if res.retry else ' -- not a price reject, stopped'),
                 dry_run=dry_run)
            return out
        q, prob = _quote(kite, sym)
        if q is None:
            return Outcome('skipped', 'retry re-quote: %s' % prob, orders=orders)
        prob = _premium_problem(float(q['ask']), lot)
        if prob:
            return Outcome('skipped', 'retry: %s' % prob, orders=orders)


def _record_fill(store, parent, res, q, now, m, orders, send, dry_run) -> Outcome:
    lot = int(parent['lot_size'])
    rec = build_naked_record(parent, long_symbol=parent['long_symbol'],
                             long_strike=parent.get('long_strike'), lot_size=lot,
                             fill_price=float(res.fill_price), now=now, quote=q,
                             rehearsal=(m == 'rehearsal'),
                             override=cfg.NAKED_OVERRIDE,
                             order_ids=[res.order_id] if res.order_id else [])
    # What the EXIT needs to sell this exact contract on the same broker.
    for k, v in (('entry_tag', res.tag), ('neo_symbol', res.neo_symbol),
                 ('neo_token', res.neo_token)):
        if v:
            rec[k] = v
    try:
        stored = store.add_naked_live(rec)
    except Exception as e:
        # A REAL position with no record. Loudest alert this module has; the
        # `unrecorded` stamp makes gate 4 refuse everything until a human (or
        # the adoption sweep) files it.
        send('\U0001F6A8 NAKED %s #%s %s FILLED %s @ %.2f x %d BUT THE RECORD '
             'WAS NOT WRITTEN (%s). File it by hand; no new naked entries until '
             'then.' % (m.upper(), parent['id'], parent.get('stock'),
                        parent['long_symbol'], res.fill_price, lot, e),
             dry_run=dry_run)
        return Outcome('unrecorded', 'filled but not recorded: %s' % e, orders=orders,
                       extra={'order_id': res.order_id, 'fill': res.fill_price,
                              'tag': res.tag, 'neo_symbol': res.neo_symbol,
                              'neo_token': res.neo_token, 'ask': q.get('ask')})
    prem = float(res.fill_price) * lot
    over = prem > cfg.NAKED_MAX_PREMIUM + 1e-9
    send('%s NAKED %s #%d %s %s BOUGHT %s @ %.2f x %d = Rs %.0f (ask %.2f)%s'
         % ('\U0001F534' if over else '\U0001F7E2', m.upper(), stored['id'],
            stored['stock'], stored['direction'], stored['long_symbol'],
            res.fill_price, lot, prem, float(q['ask']),
            '\nFILLED OVER THE Rs %.0f CAP -- recorded, it is a real position'
            % cfg.NAKED_MAX_PREMIUM if over else ''),
         dry_run=dry_run)
    return Outcome('entered', 'filled @ %.2f' % res.fill_price, record=stored,
                   orders=orders, extra={'over_cap': over} if over else {})
