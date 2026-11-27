"""Pure exit/entry rules for the live executor (docs/EXECUTOR_SPEC.md §4-§6, §13).

No I/O, no clock, no broker: every function takes the quote, the position dict and
`now`, so each rule is unit-tested exactly. The paper ladder itself is NOT copied:
`minute_sample` / `tick_arm` call option_buy's `_ladder` (one copy of the rules) and
the tick thresholds read option_buy's constants (Fable F21) via `consts`.
"""
from __future__ import annotations

import datetime as _dt
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Minute-sample exits taken from `_ladder` (Fable F12): the rest of what the ladder
#: returns on an LTP print is ARMING only -- floors, TRAIL, PREM_SL and TP fire from
#: the tick rules on the BID with persistence.
MINUTE_EXITS = ('TIME_SL', 'ADV_SL')


@dataclass
class Quote:
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    bids: List[Tuple[float, int]] = field(default_factory=list)   # (price, qty) best first
    asks: List[Tuple[float, int]] = field(default_factory=list)
    ts: float = 0.0            # local epoch seconds when received
    exch_ts: Optional[float] = None
    source: str = ''           # 'ws' | 'rest'

    def valid(self) -> bool:
        """F13: a usable two-sided book. Anything else is NO price."""
        return self.bid > 0 and self.ask > 0 and self.bid <= self.ask

    def age(self, now_epoch: float) -> float:
        return max(0.0, now_epoch - self.ts) if self.ts else math.inf


def round_down(px: float, tick: float) -> float:
    n = int(px / tick + 1e-9)
    return round(max(n, 1) * tick, 2)


def round_up(px: float, tick: float) -> float:
    n = int(math.ceil(px / tick - 1e-9))
    return round(max(n, 1) * tick, 2)


def depth_walk(levels: List[Tuple[float, int]], qty: int) -> Optional[float]:
    """Price of the first level whose cumulative quantity covers `qty` (the
    worst price a marketable limit for qty must reach). None on an empty side;
    the deepest level when the shown depth is thinner than qty."""
    cum, last = 0, None
    for px, q in levels:
        if px <= 0:
            continue
        cum += max(0, int(q))
        last = px
        if cum >= qty:
            return px
    return last


def entry_cap(signal_ltp: float, st: float, tick: float, cfg: Dict[str, Any],
              consts: Any) -> float:
    """E2: the highest price an entry may pay."""
    slip = max(cfg['entry_cap_ticks'] * tick, signal_ltp * cfg['entry_cap_pct'] / 100.0)
    caps = [signal_ltp + slip,
            st * (1 - consts.MIN_REWARD_PCT / 100.0),
            st - consts.MIN_T1_DISTANCE]
    return round_down(min(caps), tick)


def entry_limit(q: Quote, qty: int, cap: float, tick: float) -> Optional[float]:
    """E3: depth-walked ask for qty, clipped to the cap; None = do not buy
    (no ask, or the best ask is already above the cap)."""
    if not q.valid() or q.ask > cap + 1e-9:
        return None
    walk = depth_walk(q.asks, qty) or q.ask
    return round_down(min(max(walk, q.ask), cap), tick)


def trail_level(pos: Dict[str, Any], consts: Any) -> float:
    """The paper TRAIL level (option_buy._ladder layer 4) for the CURRENT peak:
    0 until peak gain >= TRAIL_MIN_GAIN_PCT. Recomputed only at the minute
    sample (F14), so the tick rule never trails a tick-level peak."""
    entry = float(pos.get('entry_premium') or 0)
    peak = float(pos.get('peak_premium') or entry)
    if entry <= 0:
        return 0.0
    gain = (peak - entry) / entry * 100.0
    if gain < consts.TRAIL_MIN_GAIN_PCT:
        return 0.0
    trail = entry + (peak - entry) * consts.TRAIL_PCT / 100.0
    if pos.get('cost_sl_active') and float(pos.get('cost_sl_level') or 0) > trail:
        trail = float(pos['cost_sl_level'])
    return trail


def floor_level(pos: Dict[str, Any]) -> Tuple[float, str]:
    """(binding floor, the exit type it reports): max(armed floor, trail)."""
    fl = float(pos.get('cost_sl_level') or 0) if pos.get('cost_sl_active') else 0.0
    tr = float(pos.get('trail_level') or 0)
    if tr > fl and tr > 0:
        return tr, 'TRAIL'
    return fl, str(pos.get('sl_reason') or 'COST_SL')


def tick_exit(pos: Dict[str, Any], q: Quote, now_epoch: float, cfg: Dict[str, Any],
              consts: Any) -> Optional[str]:
    """X2-X5 on one VALID quote. Mutates pos['floor_breach_since'] (X3 timer).
    Returns the exit type, or None."""
    if not q.valid():
        return None
    bid = q.bid
    entry = float(pos.get('entry_premium') or 0)
    st = float(pos.get('st_value') or 0)
    t2 = float(pos.get('t2_value') or 0)
    target = t2 if t2 > 0 else st
    if target > 0 and bid >= target:                                   # X2
        pos['floor_breach_since'] = None
        return 'TP'
    if entry > 0 and bid <= entry * (1 - consts.PREMIUM_SL_PCT / 100.0):   # X4
        return 'PREM_SL'
    peak = float(pos.get('peak_premium') or entry)
    if entry > 0:
        peak_gain = (peak - entry) / entry * 100.0
        if peak_gain < consts.ADV_SL_PEAK_PCT and \
                bid <= entry * (1 - cfg['adv_backstop_pct'] / 100.0):     # X5
            return 'ADV_SL'
    level, kind = floor_level(pos)                                      # X3
    if level <= 0:
        pos['floor_breach_since'] = None
        return None
    if bid <= level * (1 - cfg['floor_instant_pct'] / 100.0):
        return kind
    if bid <= level:
        since = pos.get('floor_breach_since')
        if since is None:
            pos['floor_breach_since'] = now_epoch
            return None
        if now_epoch - float(since) >= cfg['floor_persist_s']:
            return kind
        return None
    pos['floor_breach_since'] = None
    return None


def minute_sample(key: str, pos: Dict[str, Any], q: Quote, now: _dt.datetime,
                  ladder: Callable, consts: Any, say: Any = None) -> Optional[str]:
    """X1: peak from LTP, then option_buy._ladder exactly as paper; ARMING kept,
    exit taken only for MINUTE_EXITS (F12). Recomputes the trail level."""
    ltp = q.ltp if q.ltp > 0 else q.bid
    if ltp <= 0:
        return None
    if ltp > float(pos.get('peak_premium') or 0):
        pos['peak_premium'] = ltp
    exit_type, _ = ladder(key, pos, ltp, now, cap='config', flatten=False, say=say)
    pos['trail_level'] = trail_level(pos, consts)
    pos['last_sample_min'] = now.strftime('%Y%m%d%H%M')
    return exit_type if exit_type in MINUTE_EXITS else None


def tick_arm(key: str, pos: Dict[str, Any], q: Quote, now: _dt.datetime,
             ladder: Callable, say: Any = None) -> bool:
    """X6: a tick LTP at/above T1 (T2 pending, not yet hit) arms the give-back
    floor via _ladder. ARMING ONLY (F12): any exit it returns is discarded --
    the tick rules decide on the bid. True when it armed."""
    st = float(pos.get('st_value') or 0)
    t2 = float(pos.get('t2_value') or 0)
    if pos.get('t1_hit') or t2 <= 0 or st <= 0 or q.ltp <= 0 or q.ltp < st:
        return False
    ladder(key, pos, q.ltp, now, cap='config', flatten=False, say=say)
    return bool(pos.get('t1_hit'))


def exit_price(bid: float, n: int, tick: float, cfg: Dict[str, Any], stale: bool,
               first: Optional[float] = None) -> float:
    """X11 / F15: the limit for re-price number n (0 = first send).
    n=0: `first` (depth walk / TP bid) or the bid; 1..k: bid x (1 - steps[n-1]%);
    beyond: bid x (1 - tail%) on a live book, CUMULATIVE on a stale one."""
    steps = [float(x) for x in cfg['exit_steps_pct']]
    if n <= 0:
        px = first if first and first > 0 else bid
    elif n <= len(steps):
        px = bid * (1 - steps[n - 1] / 100.0)
    else:
        tail = float(cfg['exit_tail_pct'])
        pct = tail * (n - len(steps)) if stale else tail
        px = bid * (1 - min(pct, 90.0) / 100.0)
    return round_down(px, tick)


def now_epoch() -> float:
    return time.time()
