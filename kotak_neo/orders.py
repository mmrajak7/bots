"""Reconcile-first order helpers every bot shares.

The broker's answer to a place is a request id, not a fill: RMS rejected an
SL-M a moment AFTER NEO returned its number (2026-11-27). So nothing here
trusts a reply; outcomes are read from the BOOK by the bot's own TAG, and
before any SELL the account's POSITION is the authority on what may be sold.

  resolve(c, tag)              what happened to the order(s) carrying `tag`
  cancel_and_confirm(c, no)    cancel, then read back until terminal
  chase_exit(c, st, quote)     one step of a sell-to-flat chase

The chase's guarantees (independent review F1/F2/F4/F6/F7/F13, 2026-11-27):
  * never sells more than the ACCOUNT holds (positions read before every sell;
    unreadable -> nothing is sent);
  * at most ONE sell working; a new one only after the last is terminal with
    its final filled quantity applied;
  * the tag is PERSISTED (caller's `persist`) BEFORE the send, and each step
    holds a per-(bot, symbol) lock, so a crash or a second process (cron +
    monitor) can never send a second sell blind;
  * an UNCLEAR sell is never re-sent on time alone: only after `unclear_grace_s`
    AND when the book shows no live sell on the symbol AND the position still
    holds the lots (residual risk: an order invisible AND unfilled past the
    grace -- see ChasePolicy);
  * a single bad read never strands the exit: troubles are counted, and only
    `max_troubles` in a row hand it to a human -- while nothing is sent.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from . import session as S
from .client import (Auth, NeoClient, NotOwner, OrderRequest, OrderView, Rejected,
                     Unavailable, Unclear)

logger = logging.getLogger(__name__)

FILLED, PARTIAL, RESTING, REJECTED, CANCELLED, ABSENT, UNKNOWN = (
    'filled', 'partial', 'resting', 'rejected', 'cancelled', 'absent', 'unknown')


class Resolution(NamedTuple):
    state: str             # one of the constants above
    filled: int            # shares filled (final only when terminal)
    avg_price: float
    order_no: str
    reason: str
    terminal: bool         # nothing more will happen. ABSENT is NOT terminal:
                           # an unclear order may still surface (review F8)


def _summarise(views: List[OrderView], expect_qty: Optional[int] = None) -> Resolution:
    last = views[-1]
    filled = sum(v.filled for v in views)
    avg = (sum(v.filled * v.avg_price for v in views) / filled) if filled else 0.0
    if len({(v.side, v.symbol) for v in views}) > 1:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          'one tag on different sides/symbols (tag collision)', False)
    if expect_qty is not None and any(v.qty != expect_qty for v in views):
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          'book qty %s != sent %d (units?)' % ([v.qty for v in views], expect_qty),
                          False)
    unknown = [v for v in views if not v.known_status]
    if unknown:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          'unrecognised status %r' % unknown[0].status, False)
    live = [v for v in views if not v.terminal]
    if len(live) > 1:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          '%d live orders share one tag' % len(live), False)
    if live:
        v = live[0]
        return Resolution(PARTIAL if filled else RESTING, filled, avg, v.order_no, v.status,
                          False)
    bad = [v for v in views if v.status == 'complete' and v.filled != v.qty]
    if bad:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          'complete with filled %d of %d' % (bad[0].filled, bad[0].qty), False)
    want = sum(v.qty for v in views if v.status != 'rejected') or last.qty
    if any(v.status == 'complete' for v in views) and filled >= want:
        return Resolution(FILLED, filled, avg, last.order_no, 'complete', True)
    if filled:
        return Resolution(PARTIAL, filled, avg, last.order_no, last.status, True)
    if all(v.status == 'rejected' for v in views):
        return Resolution(REJECTED, 0, 0.0, last.order_no, last.rej_reason or 'rejected', True)
    return Resolution(CANCELLED, 0, 0.0, last.order_no, last.status, True)


def resolve(c: NeoClient, tag: str, wait_s: float = 10.0, poll_s: float = 0.5,
            until_terminal: bool = True, expect_qty: Optional[int] = None,
            sleep: Callable = time.sleep, clock: Callable = time.time) -> Resolution:
    """What happened to `tag`. ABSENT only if the book was READ and the tag
    never showed -- and it is NOT terminal (settle it from positions); an
    unreadable book is UNKNOWN."""
    deadline = clock() + max(0.0, wait_s)
    last: Optional[Resolution] = None
    readable = False
    while True:
        try:
            views = c.orders_tagged(tag)
            readable = True
            if views:
                last = _summarise(views, expect_qty)
                if last.terminal or not until_terminal:
                    return last
        except (Unavailable, Auth) as e:              # N5: Auth = unreadable, never escapes
            readable = False
            logger.warning('resolve %s: book unreadable: %s', tag, e)
        if clock() >= deadline:
            break
        sleep(poll_s)
    if last is not None:
        return last
    if readable:
        return Resolution(ABSENT, 0, 0.0, '', 'tag not in a readable book', False)
    return Resolution(UNKNOWN, 0, 0.0, '', 'order book unreadable', False)


def cancel_and_confirm(c: NeoClient, order_no: str, tag: str, wait_s: float = 10.0,
                       poll_s: float = 0.5, expect_qty: Optional[int] = None,
                       sleep: Callable = time.sleep, clock: Callable = time.time) -> Resolution:
    """Cancel, then read back until terminal. A cancel that NEO calls unclear
    or rejects (e.g. already filled) is still settled by the BOOK."""
    try:
        c.cancel(order_no)
    except Rejected as e:
        logger.info('cancel %s rejected (%s) -- reading the book', order_no, e)
    except (Unclear, Auth) as e:
        logger.warning('cancel %s unclear (%s) -- reading the book', order_no, e)
    return resolve(c, tag, wait_s=wait_s, poll_s=poll_s, expect_qty=expect_qty,
                   sleep=sleep, clock=clock)


# -- the exit chase ----------------------------------------------------------------------
@dataclass
class ChasePolicy:
    reprice_after_s: float = 10.0   # a working sell older than this is cancelled + re-priced
    step_ticks: int = 1             # each re-price goes this many ticks below the bid
    max_reprices: Optional[int] = 12  # then NEEDS_HUMAN (alert: exit in the app).
                                    # None = never: the chase keeps going and the
                                    # CALLER alerts (ETF_PI, owner: no manual step)
    confirm_wait_s: float = 0.0     # 0 for a 5-s monitor: settle the cancel on the next poll
    unclear_grace_s: float = 60.0   # an UNCLEAR sell may surface late; settle it after this.
                                    # RESIDUAL RISK, stated: an unclear sell that stays
                                    # INVISIBLE in the book and UNFILLED for longer than
                                    # this is re-sent. (Filled-but-invisible is caught by the
                                    # position check.) Measured visibility: immediate.
    max_troubles: Optional[int] = 12  # consecutive unreadable/unsettled steps -> NEEDS_HUMAN
                                    # (12 x a 5-s poll = 1 min: positions can lag a fill).
                                    # None = never (the caller alerts on `troubles`)
    reject_backoff_s: float = 15.0  # after a definite refusal, wait before the next try


@dataclass
class ChaseState:
    """Serialisable (to_dict / from_dict). The caller persists it -- and MUST
    pass `persist` so the tag is on disk before the order is sent."""
    bot: str
    symbol: str
    token: str
    tick: float
    remaining: int                  # shares still to sell (this chase's own count)
    sold: int = 0
    proceeds: float = 0.0
    working_tag: str = ''
    working_no: str = ''
    working_qty: int = 0
    working_since: float = 0.0
    reprices: int = 0
    troubles: int = 0
    next_try_at: float = 0.0
    status: str = 'active'          # active | done | needs_human
    rejects: int = 0                # N3: definite refusals in a row (reset by a fill)
    unclears: int = 0               # N4: unclear sends in a row (reset by a fill)
    note: str = ''
    fills: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ChaseState':
        return cls(**d)

    @property
    def avg_exit(self) -> float:
        return self.proceeds / self.sold if self.sold else 0.0


def _round_down(px: float, tick: float) -> float:
    n = int(px / tick + 1e-9)
    return round(max(n, 1) * tick, 2)


def _apply(st: ChaseState, res: Resolution) -> None:
    if res.filled:
        st.rejects = st.unclears = 0
        st.sold += res.filled
        st.proceeds += res.filled * res.avg_price
        st.remaining -= res.filled
        st.fills.append({'tag': st.working_tag, 'order_no': res.order_no, 'qty': res.filled,
                         'avg': res.avg_price})
    st.working_tag = st.working_no = ''
    st.working_qty = 0
    st.working_since = 0.0


def _trouble(st: ChaseState, p: ChasePolicy, why: str) -> ChaseState:
    st.troubles += 1
    st.note = why
    if p.max_troubles is not None and st.troubles >= p.max_troubles:
        st.status = 'needs_human'
        st.note = '%s (%d steps in a row)' % (why, st.troubles)
    return st


def _settle_working(c: NeoClient, st: ChaseState, p: ChasePolicy, sleep, clock,
                    held_fn: Callable[[], int]) -> bool:
    """Settle the working sell. True = no sell is working any more (the step
    may place); False = leave the step (still working, or trouble noted)."""
    res = resolve(c, st.working_tag, wait_s=0, until_terminal=False,
                  expect_qty=st.working_qty or None, sleep=sleep, clock=clock)
    if res.order_no and not st.working_no:
        st.working_no = res.order_no                    # an unclear send has surfaced
    if res.state == UNKNOWN:
        _trouble(st, p, 'working sell %s: %s' % (st.working_tag, res.reason))
        return False
    if res.state == ABSENT:
        if st.working_no:
            _trouble(st, p, 'working sell %s (%s) vanished from the book'
                     % (st.working_tag, st.working_no))
            return False
        if clock() - st.working_since < p.unclear_grace_s:
            return False                                # may still surface: never resend early
        # F1: after the grace, re-send ONLY if no sell is live on the symbol
        # (any tag; a NON-exclusive bot: any of ITS tags -- another bot's sell
        # on a shared strike is not ours to wait for) and the lots are held.
        try:
            if getattr(c, 'exclusive', True):
                live = c.live_orders(st.symbol, 'S')
            else:
                live = [o for o in c.own_orders()
                        if o.symbol == st.symbol and o.side == 'S' and not o.terminal]
            if live:
                _trouble(st, p, 'unclear sell absent but a live sell exists on %s' % st.symbol)
                return False
            held = held_fn()
        except (Unavailable, Auth) as e:
            _trouble(st, p, 'cannot settle unclear sell: %s' % e)
            return False
        if held < st.remaining:
            _trouble(st, p, 'unclear sell absent but the position fell to %d (expected %d)'
                     % (held, st.remaining))
            return False
        logger.warning('%s %s: unclear sell %s never reached the book; position holds %d',
                       st.bot, st.symbol, st.working_tag, held)
        _apply(st, Resolution(ABSENT, 0, 0.0, '', 'never placed', True))
        return True
    if res.terminal:
        _apply(st, res)
        return True
    if clock() - st.working_since < p.reprice_after_s:
        return False                                    # let it work
    res = cancel_and_confirm(c, st.working_no, st.working_tag, wait_s=p.confirm_wait_s,
                             expect_qty=st.working_qty or None, sleep=sleep, clock=clock)
    if not res.terminal:
        st.note = 'cancel of %s pending (%s)' % (st.working_no, res.reason)
        return False                                    # settle it on the next step
    _apply(st, res)
    st.reprices += 1
    return True


def chase_exit(c: NeoClient, st: ChaseState, quote: Callable[[], Optional[Dict[str, float]]],
               policy: Optional[ChasePolicy] = None, persist: Optional[Callable] = None,
               sleep: Callable = time.sleep, clock: Callable = time.time,
               held_fn: Optional[Callable[[], int]] = None,
               price_fn: Optional[Callable[[float, int, float], float]] = None) -> ChaseState:
    """ONE step. Mutates and returns `st`; calls `persist(st)` after every
    change that matters, and BEFORE any order is sent.

    `held_fn()` -> shares this chase may sell at most (default: the ACCOUNT's
    net position). A non-exclusive bot passes min(own tagged net, account
    net) so a shared strike's other lots are never sold. Raises Unavailable
    when unreadable (nothing is sent).
    `price_fn(bid, reprices, tick)` -> the limit (default: bid minus
    `reprices * step_ticks` ticks). Always rounded DOWN to the tick here."""
    p = policy or ChasePolicy()
    save = persist or (lambda s: None)
    held_of = held_fn or (lambda: c.net_held(st.symbol))
    if st.status != 'active':
        return st
    with S.file_lock('chase_%s_%s.lock' % (st.bot, st.symbol), 0.0) as got:
        if not got:
            st.note = 'another process is stepping this chase'
            return st
        if st.working_tag:
            if not _settle_working(c, st, p, sleep, clock, held_of):
                save(st)
                return st
            st.troubles = 0                             # the working sell is settled
        try:
            held = held_of()
        except (Unavailable, Auth) as e:
            save(_trouble(st, p, 'positions unreadable: %s -- nothing sent' % e))
            return st
        if st.remaining <= 0:
            st.status = 'done'
            if getattr(c, 'exclusive', True):
                try:
                    c.release_if_flat(st.symbol)
                except Unavailable as e:
                    logger.warning('release %s after exit: %s', st.symbol, e)
            save(st)
            return st
        qty = min(st.remaining, held)                   # F4: never more than the account holds
        if qty <= 0:
            save(_trouble(st, p, 'chase expects %d but the account holds %d'
                          % (st.remaining, held)))
            return st
        st.troubles = 0                                 # the book and the account agree
        if p.max_reprices is not None and st.reprices >= p.max_reprices:
            st.status, st.note = 'needs_human', '%d re-prices without getting flat' % st.reprices
            save(st)
            return st
        if clock() < st.next_try_at:
            return st
        q = quote() or {}
        bid = float(q.get('bid') or 0)
        if bid <= 0:
            st.note = 'no bid -- waiting'
            save(st)
            return st
        try:                                            # N2: a resting stop / stray sell
            if getattr(c, 'exclusive', True):
                others = c.live_orders(st.symbol, 'S')
            else:
                others = [o for o in c.own_orders()
                          if o.symbol == st.symbol and o.side == 'S' and not o.terminal]
        except (Unavailable, Auth) as e:
            save(_trouble(st, p, 'order book unreadable before a sell: %s -- nothing sent' % e))
            return st
        if others:
            save(_trouble(st, p, 'another live sell on %s (%s) -- nothing sent' % (
                st.symbol, ', '.join(o.order_no for o in others))))
            return st
        raw = (price_fn(bid, st.reprices, st.tick) if price_fn
               else bid - st.reprices * p.step_ticks * st.tick)
        px = _round_down(raw, st.tick)
        tag = c.new_tag()
        st.working_tag, st.working_qty, st.working_since = tag, qty, clock()
        st.working_no, st.note = '', 'selling %d @ %.2f' % (qty, px)
        save(st)                                        # F2: the tag is on disk BEFORE the send
        try:
            st.working_no = c.place(OrderRequest(st.symbol, st.token, 'S', qty, '%.2f' % px, tag),
                                    reduce_only=True)
        except Rejected as e:
            _apply(st, Resolution(REJECTED, 0, 0.0, '', str(e), True))
            st.next_try_at = clock() + p.reject_backoff_s
            st.rejects += 1                             # N3: the caller escalates on this
            st.note = 'sell rejected (%d in a row): %s' % (st.rejects, e)
        except (NotOwner, Auth, Unavailable, ValueError) as e:
            _apply(st, Resolution(CANCELLED, 0, 0.0, '', str(e), True))   # nothing was sent
            _trouble(st, p, 'sell not sent: %s' % e)
        except Unclear as e:
            st.unclears += 1                            # N4: the caller escalates on this
            st.note = 'sell unclear (%d in a row): %s' % (st.unclears, e)   # settle from the book
        save(st)
        return st
