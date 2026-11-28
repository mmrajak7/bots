"""The adoption sweep for live naked entries (naked live build P3c, design G2).

WHY. `naked_entry` stamps a parent `sending`, `unclear` or `unrecorded` whenever
an order MAY be at the broker with no record. Gate 4 then refuses every new
naked entry, which is the safe direction -- but nothing would ever lift it.
This sweep reads NEO's book by the parent's TAG and settles each one:

  book says                          action
  ---------------------------------  -------------------------------------------
  FILLED (whole lot)                 ADOPT: write the `naked_long` record at the
                                     book's average price, red alert. A real
                                     position must never be left unrecorded.
  REJECTED / CANCELLED, 0 filled     nothing was bought: stamp `resolved_none`
  RESTING / PARTIAL, not terminal    an order is still working: cancel it, read
                                     the cancel back, settle on the next sweep
  ABSENT, older than the grace,      it never reached the book: `resolved_none`
    and this bot holds nothing
  ABSENT (young) / UNKNOWN           leave it; the block stays (an unknown book)
  PARTIAL, terminal                  a part-lot position: stop, red alert, a
                                     human files it (impossible at 1 lot)

`unrecorded` (filled, store write failed) needs no broker: the fill price is on
the stamp, so the record is rebuilt from it.

Runs from zebra's 5-minute cycle (`monitor.run_cycle`), never from the 5-second
exit monitor -- WHATEVER `naked_live` says now: an order sent in `live` is
settled as a live order even after the switch moved to `rehearsal` or `off`.
Each stamp carries the mode it was sent in, and that mode decides. NEVER raises.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from . import config as cfg
from .naked_entry import TWIN_FIELD, UNRESOLVED_STATES, unresolved_twins
from .trade_store import ZebraStore, build_naked_record, is_naked

logger = logging.getLogger(__name__)


def _age_s(stamp: dict, now: datetime) -> Optional[float]:
    try:
        at = datetime.strptime(str(stamp.get('at')), '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    return (now.replace(tzinfo=None) - at).total_seconds()


def _stamp(store: ZebraStore, pid, twin: dict, **changes) -> None:
    rec = dict(twin)
    rec.update(changes)
    store.update_trade_fields(pid, **{TWIN_FIELD: rec})


def _file_record(store, parent, twin, fill: float, order_id, now, m) -> dict:
    """Write the naked record for a fill the entry path could not record."""
    rec = build_naked_record(parent, long_symbol=parent['long_symbol'],
                             long_strike=parent.get('long_strike'),
                             lot_size=int(parent['lot_size']), fill_price=float(fill),
                             now=now, quote={'ask': twin.get('ask') or twin.get('limit')},
                             rehearsal=(m == 'rehearsal'), override=cfg.NAKED_OVERRIDE,
                             order_ids=[order_id] if order_id else [])
    rec['adopted'] = True
    for k in ('tag', 'neo_symbol', 'neo_token'):
        if twin.get(k):
            rec['entry_tag' if k == 'tag' else k] = twin[k]
    return store.add_naked_live(rec)


def sweep(store: ZebraStore, *, now: Optional[datetime] = None, client=None,
          send: Optional[Callable] = None, dry_run: bool = False) -> list:
    """Settle every unresolved naked twin. Returns one line per action."""
    now = now or datetime.now(cfg.IST)
    send = _send_default if send is None else send
    out = []
    try:
        # WHOLE BOOK: liveness -- any parent carrying an unresolved naked order.
        stuck = unresolved_twins(store.load_trades())
    except Exception as e:
        logger.error('naked adopt: cannot read the store: %s', e)
        return ['store unreadable: %s' % e]
    for parent in stuck:
        try:
            m = (parent.get(TWIN_FIELD) or {}).get('mode') or cfg.NAKED_LIVE
            if m not in ('live', 'rehearsal'):
                # A stamp with no usable mode: assume the dangerous one -- a
                # live book is READ, never assumed empty.
                m = 'live'
            line = _settle(store, parent, now, m, client, send, dry_run)
        except Exception as e:                       # one parent never blocks the rest
            logger.error('naked adopt #%s raised: %s', parent.get('id'), e, exc_info=True)
            line = '#%s raised: %s' % (parent.get('id'), e)
        if line:
            logger.warning('NAKED ADOPT %s', line)
            out.append(line)
    return out


def _send_default(msg: str, dry_run: bool = False) -> bool:
    from .monitor import _send_telegram
    return _send_telegram(msg, dry_run=dry_run)


def _existing_twin(store, pid) -> Optional[dict]:
    # WHOLE BOOK: dedup -- a naked record for this parent in any state.
    return next((t for t in store.load_trades()
                 if is_naked(t) and t.get('parent_id') == pid), None)


def _settle(store, parent, now, m, client, send, dry_run) -> str:
    pid = parent['id']
    twin = dict(parent.get(TWIN_FIELD) or {})
    state = twin.get('state')
    if state not in UNRESOLVED_STATES:
        return ''
    have = _existing_twin(store, pid)
    if have is not None:
        # The record exists (written by an earlier sweep, or by the entry path
        # just before its stamp failed): the stamp is merely stale.
        _stamp(store, pid, twin, state='entered', id=have['id'],
               reason='record #%s exists' % have['id'], settled_at=_ts(now))
        return '#%s: stale %s stamp cleared (record #%s exists)' % (pid, state, have['id'])

    if state == 'unrecorded':
        fill = twin.get('fill')
        if not fill:
            return '#%s: unrecorded without a fill price -- file it by hand' % pid
        stored = _file_record(store, parent, twin, fill, twin.get('order_id'), now, m)
        _stamp(store, pid, twin, state='entered', id=stored['id'], adopted=True,
               settled_at=_ts(now), reason='adopted from the unrecorded stamp')
        send('\U0001F534 NAKED %s #%s %s: the fill @ %.2f is now RECORDED as #%s '
             '(adopted). New entries resume.' % (m.upper(), pid, parent.get('stock'),
                                                 float(fill), stored['id']), dry_run=dry_run)
        return '#%s: unrecorded fill adopted as #%s' % (pid, stored['id'])

    if m != 'live':
        # A rehearsal order never reached a broker, so there is no book to
        # read. `sending` here means the process died mid-attempt: nothing
        # happened anywhere.
        _stamp(store, pid, twin, state='resolved_none', settled_at=_ts(now),
               reason='rehearsal %s with no broker -- nothing to adopt' % state)
        return '#%s: rehearsal %s cleared' % (pid, state)

    tag = twin.get('tag')
    if not tag:
        # `unclear` from a broker that raised before it produced a tag. The
        # NeoBroker always tags before sending, so this is "never sent".
        age = _age_s(twin, now)
        if age is not None and age >= cfg.NAKED_ADOPT_ABSENT_GRACE_SEC:
            _stamp(store, pid, twin, state='resolved_none', settled_at=_ts(now),
                   reason='no tag: the order was never sent')
            return '#%s: untagged %s cleared after %.0fs' % (pid, state, age)
        return ''

    from . import naked_neo
    _, orders, _ = naked_neo.neo()
    c = client or naked_neo.default_client()
    qty = int(twin.get('qty') or parent.get('lot_size') or 0)
    res = orders.resolve(c, tag, wait_s=0, until_terminal=False, expect_qty=qty or None)

    if res.state == orders.FILLED and res.terminal and res.avg_price > 0:
        stored = _file_record(store, parent, twin, res.avg_price, res.order_no, now, m)
        _stamp(store, pid, twin, state='entered', id=stored['id'], adopted=True,
               settled_at=_ts(now), order_id=res.order_no,
               reason='adopted: the book shows the order filled')
        send('\U0001F534 NAKED LIVE #%s %s: an UNCLEAR order FILLED @ %.2f x %d -- '
             'recorded as #%s (adopted). Its exits are now watched.'
             % (pid, parent.get('stock'), res.avg_price, res.filled, stored['id']),
             dry_run=dry_run)
        return '#%s: adopted as #%s @ %.2f' % (pid, stored['id'], res.avg_price)

    if res.state in (orders.REJECTED, orders.CANCELLED) and res.terminal and res.filled == 0:
        _stamp(store, pid, twin, state='resolved_none', settled_at=_ts(now),
               order_id=res.order_no, reason='book: %s (%s)' % (res.state, res.reason))
        return '#%s: %s -- nothing bought' % (pid, res.state)

    if res.state == orders.PARTIAL and res.terminal:
        if not twin.get('partial_alerted'):
            send('\U0001F6A8 NAKED LIVE #%s %s: a PART-LOT fill (%d of %d) -- this book '
                 'cannot hold it. Close or file it by hand; no new entries until then.'
                 % (pid, parent.get('stock'), res.filled, qty), dry_run=dry_run)
            _stamp(store, pid, twin, partial_alerted=_ts(now))
        return '#%s: terminal PARTIAL %d of %d -- human' % (pid, res.filled, qty)

    if res.state in (orders.RESTING, orders.PARTIAL) and not res.terminal and res.order_no:
        r2 = orders.cancel_and_confirm(c, res.order_no, tag, wait_s=0, expect_qty=qty or None)
        return '#%s: order %s still working -- cancel sent (%s)' % (pid, res.order_no, r2.state)

    if res.state == orders.ABSENT:
        age = _age_s(twin, now)
        if age is None or age < cfg.NAKED_ADOPT_ABSENT_GRACE_SEC:
            return ''                                  # may still surface
        sym = twin.get('neo_symbol')
        if not sym:
            return '#%s: absent tag and no NEO symbol on the stamp -- human' % pid
        try:
            # The ACCOUNT position, not today's order book: an entry left
            # unclear overnight is absent from the next day's book while the
            # lot it bought is still held.
            held = c.net_held(sym)
        except Exception as e:
            return '#%s: absent, but the position is unreadable (%s)' % (pid, e)
        if held:
            if not twin.get('held_alerted'):
                send('🚨 NAKED LIVE #%s %s: order %s never found in the book, but '
                     'the account HOLDS %d of %s. File or close it by hand; no new '
                     'entries until then.' % (pid, parent.get('stock'), tag, held, sym),
                     dry_run=dry_run)
                _stamp(store, pid, twin, held_alerted=_ts(now))
            return '#%s: absent tag but the account holds %d of %s -- human' % (pid, held, sym)
        _stamp(store, pid, twin, state='resolved_none', settled_at=_ts(now),
               reason='tag never reached the book in %.0fs; nothing held' % age)
        return '#%s: absent after %.0fs -- nothing bought' % (pid, age)

    return ''                                          # UNKNOWN: leave the block


def _ts(now: datetime) -> str:
    return now.strftime('%Y-%m-%d %H:%M:%S')
