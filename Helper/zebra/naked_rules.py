"""The exit rules of a naked long -- ONE copy, run by the shadow and by live.

Design: `docs/NAKED_LIVE_DESIGN.md` Q1 (frozen 2026-10-26, owner "agree"):
live exits must be the SAME rules the 100-trade test measures, or the test
says nothing about live. Before this module the rules lived inline in
`structure_shadow.poll_one`, and a live engine would have had to re-implement
them -- the exact drift that already existed between the live BCS monitor and
the shadow (stop confirm 3 polls vs 2, TIME at 15:15 vs after 09:30, an exit
vet the shadow does not have). One function, two callers, removes the
possibility rather than policing it.

The rules, in the live ORDER (TP, STOP, TIME):

- TP on SPOT at the target -- including inside the cash closing auction, where
  spot is frozen; live reads it there too. Not dark in the opening buffer.
  A TP that fires on a poll with no usable price LATCHES and books at the next
  usable price; the latch lapses at the end of its own session.
- STOP when the long's value (its BID, reliable book only) is at or below
  `stop_frac` x entry value, confirmed over `DEBIT_SL_CONFIRM_POLLS`
  consecutive polls no more than `CONFIRM_STALE_SEC` apart. A poll with no
  usable price, or inside the opening buffer, FREEZES the count; a usable
  non-breaching poll resets it.
- TIME at `TIME_SL_DAYS` sessions before expiry: fires on the calendar, books
  only after the opening buffer on a usable price; past expiry with no price
  it books UNPRICED so a position cannot stay open past its own contract.
- No trail (defined on max gain, which a naked long has not got), no exit vet,
  no spot corroboration, no intrinsic floor.

`step` mutates only the state dict it is given (`pending`, `confirm`,
`tp_latch_expired`); valuation and booking stay with the caller. Every clock
is an argument, so a replay judges staleness on observation times.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from common.nse_holidays import sessions_between

from . import config as cfg


def tp_hit(direction: str, spot: float, target: float) -> bool:
    return spot >= target if direction == 'CE' else spot <= target


def within_open_buffer(now: datetime) -> bool:
    """Inside the first VALUE_TRIGGER_OPEN_BUFFER_SEC of the session, where
    value exits stand down (both money-losing incidents were on the first
    prints after the open)."""
    open_h, open_m = cfg.MARKET_OPEN
    since = (now.hour * 3600 + now.minute * 60 + now.second) \
        - (open_h * 3600 + open_m * 60)
    return 0 <= since < cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC


def bump(c: dict, kind: str, now: datetime) -> int:
    """`ZebraStore.bump_confirm` on a plain dict: a streak whose last hit is
    older than CONFIRM_STALE_SEC restarts, so a breach at 15:25 cannot pair
    with one at 09:30 the next morning."""
    t = now.timestamp()
    n = c.get(kind) or {}
    if t - float(n.get('t', 0.0)) > cfg.CONFIRM_STALE_SEC:
        n = {}
    c[kind] = {'n': int(n.get('n', 0)) + 1, 't': t}
    return c[kind]['n']


def confirmed(a: dict, kind: str, breached: Optional[bool], now: datetime) -> bool:
    """The debounce: a value exit needs DEBIT_SL_CONFIRM_POLLS breaching polls.
    `breached=None` (no usable price, or the opening buffer) FREEZES the count
    rather than resetting it, so a flickering book cannot block a genuine exit
    for ever; a non-breaching usable poll resets it."""
    c = a.setdefault('confirm', {})
    if breached is None:
        return False
    if not breached:
        c.pop(kind, None)
        return False
    return bump(c, kind, now) >= cfg.DEBIT_SL_CONFIRM_POLLS


def sessions_left(expiry, today: date) -> Optional[int]:
    try:
        return sessions_between(today, date.fromisoformat(str(expiry)[:10]))
    except Exception:
        return None


def step(a: dict, *, take_tp: bool, stop_frac: Optional[float],
         v: Optional[float], spot: Optional[float], direction: str,
         target: float, now: datetime, today: date,
         left: Optional[int], value_armed: bool) -> Optional[tuple]:
    """Advance one naked position by one poll. Returns `(reason, value)` to
    EXIT -- `value` None only for the expiry backstop -- or None to hold.

    a            state: `entry_value`, and the rule's own `pending` /
                 `confirm` / `tp_latch_expired`, which this mutates.
    take_tp      False for an arm with no target (`naked_runner`).
    stop_frac    None for an arm with no value stop (`naked_hold`).
    v            what selling the long pays now (its BID), or None when the
                 book is unusable -- never a guess.
    left         sessions to expiry (`sessions_left`), None if unknown.
    value_armed  False inside the opening buffer.
    """
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    spot_ok = spot is not None and spot > 0
    expired = left is not None and left <= 0

    # A TRIGGER THAT FIRED BUT COULD NOT BE PRICED IS NOT AN EXIT YET. TP and
    # TIME fire on spot and on the calendar, so either can land on a poll with
    # a dead book; the trigger latches and books at the next usable price.
    pend = a.get('pending')
    if pend and pend['reason'] == 'tp' and str(pend.get('since'))[:10] != today.isoformat():
        # The TP latch arms for its OWN SESSION only (owner, 2026-08-28).
        a.setdefault('tp_latch_expired', []).append(pend.get('since'))
        a.pop('pending', None)
        pend = None
    if pend and pend['reason'] == 'time':
        # TIME is re-evaluated by the cascade every poll, after TP and STOP;
        # the latch only records when it first fired and never blocks a TP.
        pend = None
    if pend:
        if v is not None:
            return (pend['reason'], v)
        if expired:
            return (pend['reason'], None)
        return None

    if take_tp and spot_ok and tp_hit(direction, spot, target):
        if v is not None:
            return ('tp', v)
        a['pending'] = {'reason': 'tp', 'since': ts}
        return None

    usable = v is not None and value_armed
    if stop_frac is not None:
        if confirmed(a, 'stop', (v <= a['entry_value'] * stop_frac)
                     if usable else None, now):
            return ('stop', v)

    if left is not None and left <= cfg.TIME_SL_DAYS:
        if v is not None and value_armed:
            return ('time', v)
        if expired and v is None:
            return ('time', None)
        if not a.get('pending'):
            a['pending'] = {'reason': 'time', 'since': ts}
    return None
