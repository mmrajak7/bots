"""Reconcile-first order helpers every bot shares.

The broker's answer to a place is a request id, not a fill: RMS rejected an
SL-M a moment AFTER NEO returned its number (2026-11-27). So nothing here
trusts a reply; everything reads the order BOOK, by the bot's own TAG.

  resolve(c, tag)              what happened to the order(s) carrying `tag`
  cancel_and_confirm(c, no)    cancel, then read back until terminal; the
                               FINAL filled quantity is what counts
  chase_exit(c, st, quote)     one step of a sell-to-flat chase. Call it every
                               poll (5-s monitor, a long loop, or once a
                               minute). It NEVER has two sells working at once
                               and never sells more than is still held.

All waits take `sleep`/`clock` so tests run instantly.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, NamedTuple, Optional

from .client import NeoClient, OrderRequest, OrderView, Rejected, Unavailable, Unclear

logger = logging.getLogger(__name__)

FILLED, PARTIAL, RESTING, REJECTED, CANCELLED, ABSENT, UNKNOWN = (
    'filled', 'partial', 'resting', 'rejected', 'cancelled', 'absent', 'unknown')


class Resolution(NamedTuple):
    state: str             # one of the constants above
    filled: int            # shares filled (final when terminal)
    avg_price: float
    order_no: str
    reason: str
    terminal: bool


def _summarise(views: List[OrderView]) -> Resolution:
    """One tag -> one Resolution. More than one order on a tag is legal only
    if all but one are terminal (a resend under the same tag is a bug)."""
    filled = sum(v.filled for v in views)
    avg = (sum(v.filled * v.avg_price for v in views) / filled) if filled else 0.0
    live = [v for v in views if not v.terminal]
    last = views[-1]
    unknown = [v for v in views if not v.known_status]
    if unknown:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          'unrecognised status %r' % unknown[0].status, False)
    if len(live) > 1:
        return Resolution(UNKNOWN, filled, avg, last.order_no,
                          '%d live orders share one tag' % len(live), False)
    if live:
        v = live[0]
        return Resolution(PARTIAL if filled else RESTING, filled, avg, v.order_no, v.status,
                          False)
    want = sum(v.qty for v in views if v.status != 'rejected') or last.qty
    if any(v.status == 'complete' for v in views) and filled >= want:
        return Resolution(FILLED, filled, avg, last.order_no, 'complete', True)
    if filled:
        return Resolution(PARTIAL, filled, avg, last.order_no, last.status, True)
    if all(v.status == 'rejected' for v in views):
        return Resolution(REJECTED, 0, 0.0, last.order_no, last.rej_reason or 'rejected', True)
    return Resolution(CANCELLED, 0, 0.0, last.order_no, last.status, True)


def resolve(c: NeoClient, tag: str, wait_s: float = 10.0, poll_s: float = 0.5,
            until_terminal: bool = True, sleep: Callable = time.sleep,
            clock: Callable = time.time) -> Resolution:
    """What happened to `tag`. Waits up to `wait_s` for the order to appear
    and (if asked) to become terminal. ABSENT only if the book was READ and the
    tag never showed; an unreadable book at the end is UNKNOWN -- never absent."""
    deadline = clock() + max(0.0, wait_s)
    last: Optional[Resolution] = None
    readable = False
    while True:
        try:
            views = c.orders_tagged(tag)
            readable = True
            if views:
                last = _summarise(views)
                if last.terminal or not until_terminal:
                    return last
        except Unavailable as e:
            readable = False
            logger.warning('resolve %s: book unreadable: %s', tag, e)
        if clock() >= deadline:
            break
        sleep(poll_s)
    if last is not None:
        return last
    if readable:
        return Resolution(ABSENT, 0, 0.0, '', 'tag never appeared in a readable book', True)
    return Resolution(UNKNOWN, 0, 0.0, '', 'order book unreadable', False)


def cancel_and_confirm(c: NeoClient, order_no: str, tag: str, wait_s: float = 10.0,
                       poll_s: float = 0.5, sleep: Callable = time.sleep,
                       clock: Callable = time.time) -> Resolution:
    """Cancel, then read back until terminal. A cancel that NEO calls unclear
    or rejects (e.g. already filled) is still settled by the BOOK."""
    try:
        c.cancel(order_no)
    except Rejected as e:
        logger.info('cancel %s rejected (%s) -- reading the book', order_no, e)
    except Unclear as e:
        logger.warning('cancel %s unclear (%s) -- reading the book', order_no, e)
    return resolve(c, tag, wait_s=wait_s, poll_s=poll_s, sleep=sleep, clock=clock)


# -- the exit chase ----------------------------------------------------------------------
@dataclass
class ChasePolicy:
    reprice_after_s: float = 10.0   # a working sell older than this is cancelled + re-priced
    step_ticks: int = 1             # each re-price goes this many ticks below the bid
    max_rounds: int = 12            # then NEEDS_HUMAN (alert: exit in the app)
    confirm_wait_s: float = 8.0
    unclear_grace_s: float = 20.0   # an UNCLEAR sell may surface late; never re-send before this


@dataclass
class ChaseState:
    """Serialisable (asdict) so a cron caller can persist it between calls."""
    bot: str
    symbol: str
    token: str
    tick: float
    remaining: int                  # shares still to sell
    sold: int = 0
    proceeds: float = 0.0
    working_tag: str = ''
    working_no: str = ''
    working_since: float = 0.0
    rounds: int = 0
    status: str = 'active'          # active | done | needs_human
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
        st.sold += res.filled
        st.proceeds += res.filled * res.avg_price
        st.remaining -= res.filled
        st.fills.append({'tag': st.working_tag, 'order_no': res.order_no, 'qty': res.filled,
                         'avg': res.avg_price})
    st.working_tag = st.working_no = ''
    st.working_since = 0.0


def chase_exit(c: NeoClient, st: ChaseState, quote: Callable[[], Optional[Dict[str, float]]],
               policy: Optional[ChasePolicy] = None, sleep: Callable = time.sleep,
               clock: Callable = time.time) -> ChaseState:
    """ONE step. Returns the (mutated) state; persist it if the caller is a cron.

    Invariants:
      * at most ONE sell working; a new one only after the previous is TERMINAL
        in the book with its final filled quantity applied;
      * the next sell is for `remaining` only, so the chase can never over-sell;
      * anything the book cannot settle -> `needs_human`, and the chase stops
        sending (a stuck exit is alerted, never guessed through).
    """
    p = policy or ChasePolicy()
    if st.status != 'active':
        return st
    if st.working_tag:
        res = resolve(c, st.working_tag, wait_s=0, until_terminal=False, sleep=sleep, clock=clock)
        if res.order_no and not st.working_no:
            st.working_no = res.order_no                # an unclear send has surfaced
        if res.state == UNKNOWN or (res.state == ABSENT and st.working_no):
            st.status, st.note = 'needs_human', 'working sell %s: %s' % (st.working_tag, res.reason)
            return st
        if res.state == ABSENT and clock() - st.working_since < p.unclear_grace_s:
            return st          # an UNCLEAR send may still surface (visibility lag): never resend early
        if res.terminal:
            _apply(st, res)
        elif clock() - st.working_since >= p.reprice_after_s:
            res = cancel_and_confirm(c, st.working_no, st.working_tag, wait_s=p.confirm_wait_s,
                                     sleep=sleep, clock=clock)
            if not res.terminal:
                st.status, st.note = 'needs_human', 'cancel of %s not confirmed: %s' % (
                    st.working_no, res.reason)
                return st
            _apply(st, res)
        else:
            return st                                   # let it work
    if st.remaining <= 0:
        st.status = 'done'
        return st
    if st.rounds >= p.max_rounds:
        st.status, st.note = 'needs_human', '%d rounds without getting flat' % st.rounds
        return st
    q = quote() or {}
    bid = float(q.get('bid') or 0)
    if bid <= 0:
        st.note = 'no bid -- waiting'                   # nothing to sell into; try next poll
        return st
    px = _round_down(bid - st.rounds * p.step_ticks * st.tick, st.tick)
    tag = c.new_tag()
    st.working_tag, st.working_since = tag, clock()
    st.rounds += 1
    try:
        st.working_no = c.place(OrderRequest(st.symbol, st.token, 'S', st.remaining,
                                             '%.2f' % px, tag))
    except Rejected as e:
        st.working_tag = st.working_no = ''
        st.note = 'sell rejected: %s' % e
    except Unclear as e:
        # May be working at the exchange: keep the tag, settle it from the book
        # next step. working_no unknown -> if the book never shows it, it is ABSENT.
        st.note = 'sell unclear: %s' % e
    return st
