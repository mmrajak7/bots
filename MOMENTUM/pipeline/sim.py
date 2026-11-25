"""SRS / ASRS / 1BP replay on NIFTY 5-min spot bars.

Design: README.md (pre-registered 2026-11-19).  Rules credit: Tom Hougaard.

Conventions (every one is pinned by test_sim.py):
  * bars of one day: list of dict(t, o, h, l, c); index 0 = 09:15 bar, 5 min each.
  * A bar is only known after it closes, so a signal built from bars [s, s+n)
    can first trigger in bar s+n.  No bar is ever used before it exists.
  * Stop-order fill: if the bar OPENS beyond the level -> fill at the open
    (gap); else if the range reaches the level -> fill at the level.
  * Intrabar order is unknown.  Every time the outcome depends on it, the
    simulator asks `choose()` and the caller enumerates EVERY path, then keeps
    the worst (pessimistic) or best (optimistic) day.  Events that cannot be
    ambiguous are not asked (e.g. a stop touched in the bar after the entry,
    or a stop touched in a bar whose OPEN filled the entry).
  * P&L in index points, side * (exit - entry).  R = points / planned risk,
    planned risk = |entry LEVEL - stop| (so a gap fill shows as worse than -1R).
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

DATA = Path(__file__).resolve().parent.parent / "Helper/income_analyser/data/NIFTY_5m.json"

BARS_PER_DAY = 75            # 09:15 .. 15:25
EXIT_IDX = 71                # 15:10 bar; exit at its close (before the 15:15 freeze)
LAST_ENTRY_IDX = 70          # 15:05 bar is the last bar a new entry may fill in
BUFFER_FRAC = 0.00013        # DAX 2 pts / 15,000
FIXED_STOP_FRAC = 0.0033     # DAX 40 pts / 12,000

LOT = 65
GST = 0.18
EXCH_PCT = 0.0000173         # NSE futures, per side
STAMP_PCT = 0.00002          # buy side
BROKERAGE_RS = 20.0          # per order


def stt_frac(d: str) -> float:
    """NIFTY futures STT, sell side, by trade date (ISO string)."""
    if d >= "2026-04-01":
        return 0.0005
    if d >= "2024-10-01":
        return 0.0002
    if d >= "2023-04-01":
        return 0.000125
    return 0.0001


def cost_points(px: float, d: str, slip_pts: float) -> float:
    """Round-trip futures cost in index points for one unit at price px."""
    stt = stt_frac(d) * px
    exch = 2 * EXCH_PCT * px * (1 + GST)
    stamp = STAMP_PCT * px
    brk = 2 * BROKERAGE_RS * (1 + GST) / LOT
    return stt + exch + stamp + brk + 2 * slip_pts


# ---------------------------------------------------------------- data
def valid_day(bars: list[dict]) -> bool:
    if len(bars) != BARS_PER_DAY or bars[0]["t"][11:] != "09:15":
        return False
    for i, b in enumerate(bars):
        m = 9 * 60 + 15 + 5 * i
        if b["t"][11:] != f"{m // 60:02d}:{m % 60:02d}":
            return False
        if not (b["l"] <= min(b["o"], b["c"]) and b["h"] >= max(b["o"], b["c"])):
            return False
        # A frozen bar (h == l) before the exit = circuit halt or a data gap
        # (2020-03-13, 2020-03-23, 2022-03-07).  Not a tradeable session: a
        # zero-range signal bar makes 1R ~2 pts and one trade +634R (review 11-19).
        if i <= EXIT_IDX and b["h"] == b["l"]:
            return False
    return True


def load_days(path: Path = DATA) -> tuple[dict[str, list[dict]], int]:
    raw = json.loads(path.read_text())
    days: dict[str, list[dict]] = {}
    for b in raw:
        days.setdefault(b["t"][:10], []).append(b)
    good = {d: sorted(v, key=lambda x: x["t"]) for d, v in days.items()}
    good = {d: v for d, v in good.items() if valid_day(v)}
    return dict(sorted(good.items())), len(days) - len(good)


# ---------------------------------------------------------------- paths
class Chooser:
    """Replays a prefix of decisions, defaults the rest to False, records all."""

    def __init__(self, prefix: list[bool]):
        self.prefix = prefix
        self.made: list[bool] = []

    def __call__(self) -> bool:
        k = len(self.made)
        v = self.prefix[k] if k < len(self.prefix) else False
        self.made.append(v)
        return v


def all_paths(run: Callable[[Chooser], list]) -> list[list]:
    """Every distinct result of `run` over every intrabar decision sequence."""
    out, stack = [], [[]]
    while stack:
        prefix = stack.pop()
        ch = Chooser(prefix)
        out.append(run(ch))
        for k in range(len(prefix), len(ch.made)):
            stack.append(ch.made[:k] + [not ch.made[k]])
    return out


# ---------------------------------------------------------------- trades
@dataclass
class Trade:
    side: int
    entry_i: int
    entry_px: float
    stop: float
    risk: float
    at_open: bool
    # first_event: the fill was the bar's first event, so every price on the far
    #   side of the fill from the bar's open printed AFTER the fill.
    # order_known: the bar reached both levels and this path chose this side
    #   first, so everything beyond the OPPOSITE level printed after the fill.
    first_event: bool = False
    order_known: bool = False
    exit_i: int = -1
    exit_px: float = math.nan
    reason: str = ""

    @property
    def pts(self) -> float:
        return self.side * (self.exit_px - self.entry_px)


def same_bar_stop_certain(t: Trade, bar: dict) -> bool:
    """Is a touch of t.stop inside its own entry bar known to be AFTER the fill?

    at_open      -> the whole range printed after the fill.
    order_known  -> for a long, every price <= the short level printed after
                    the fill; the stop is touched only if the low reaches it,
                    and the low is <= the short level, so after the fill.
                    (For mid stops the path crosses the stop on the way down;
                    for wide fixed stops the stop itself is below that level.)
    first_event and stop beyond the fill away from the open -> after the fill.
    close at/beyond the stop -> the close is the bar's LAST print, so the path
                    ended on the far side of the stop after the fill: touched.
                    (Spot check 11-19, 2020-03-19: a flip "survived" a bar that
                    closed beyond its stop.)
    """
    if t.at_open or t.order_known:
        return True
    if (bar["c"] <= t.stop) if t.side > 0 else (bar["c"] >= t.stop):
        return True
    return t.first_event and (t.stop - t.entry_px) * (t.entry_px - bar["o"]) > 0


@dataclass
class Spec:
    sig_start: int               # index of first signal bar
    sig_n: int                   # number of 5-min bars merged into the signal bar
    stop_mode: str = "opposite"  # opposite | mid | fixed
    max_trades: int = 1          # 1 = first trigger only; 2 = one flip
    exit_idx: int = EXIT_IDX
    last_entry_idx: int = LAST_ENTRY_IDX
    buffer_frac: float = BUFFER_FRAC
    # Time exit: close of the hold_bars-th 5-min bar AFTER the entry bar (stop
    # still active until then; the close is the bar's last print, so a stop
    # touched inside that bar comes first).  None = hold to exit_idx.
    hold_bars: int | None = None

    def __post_init__(self):
        if self.hold_bars is not None and (self.hold_bars < 1 or self.max_trades != 1):
            raise ValueError("hold_bars needs hold_bars >= 1 and max_trades == 1")


def levels(bars: list[dict], spec: Spec) -> tuple[float, float, float, float]:
    sig = bars[spec.sig_start: spec.sig_start + spec.sig_n]
    hi = max(b["h"] for b in sig)
    lo = min(b["l"] for b in sig)
    buf = spec.buffer_frac * sig[-1]["c"]
    return hi + buf, lo - buf, (hi + lo) / 2, buf


def _stop_for(side: int, fill: float, spec: Spec, el: float, es: float,
              mid: float, buf: float) -> tuple[float, float]:
    """(stop price, planned risk in points) for a new position."""
    level = el if side > 0 else es
    if spec.stop_mode == "opposite":
        stop = es if side > 0 else el
        return stop, abs(level - stop)
    if spec.stop_mode == "mid":
        stop = mid - buf if side > 0 else mid + buf
        return stop, abs(level - stop)
    if spec.stop_mode == "fixed":
        d = FIXED_STOP_FRAC * fill
        return fill - side * d, d
    raise ValueError(spec.stop_mode)


def breakout_day(bars: list[dict], spec: Spec, choose: Callable[[], bool]) -> list[Trade]:
    """One day of the bracket breakout. Returns closed trades in order."""
    el, es, mid, buf = levels(bars, spec)
    trades: list[Trade] = []
    pos: Trade | None = None
    allowed = {+1, -1}
    first = spec.sig_start + spec.sig_n
    for i in range(first, spec.exit_idx + 1):
        b = bars[i]
        o, h, l = b["o"], b["h"], b["l"]
        fresh = True          # no event has happened yet inside this bar
        cursor = o            # price at the most recent event inside this bar
        stopped_at_open = False
        while True:
            if pos is None:
                if len(trades) >= spec.max_trades or i > spec.last_entry_idx:
                    break
                long_hit = +1 in allowed and h >= el
                short_hit = -1 in allowed and l <= es
                if not (long_hit or short_hit):
                    break
                known = False
                first_ev = fresh
                if fresh:
                    if +1 in allowed and o >= el:
                        side, px, at_open = +1, o, True
                    elif -1 in allowed and o <= es:
                        side, px, at_open = -1, o, True
                    elif long_hit and short_hit:
                        side = +1 if choose() else -1
                        px, at_open, known = (el if side > 0 else es), False, True
                    else:
                        side = +1 if long_hit else -1
                        px, at_open = (el if side > 0 else es), False
                else:
                    # A flip after a stop inside this bar; price is at `cursor`.
                    side = next(iter(allowed))
                    level = el if side > 0 else es
                    beyond = cursor >= level if side > 0 else cursor <= level
                    # A stop that filled at a gap open puts the flip at the open too.
                    at_open = beyond and stopped_at_open
                    if beyond:
                        px = cursor
                    else:
                        # Did the range beyond `level` print AFTER the stop?
                        # Certain if the stopped trade dates from an earlier bar,
                        # filled at this bar's open, or its order was known (the
                        # opposite extreme printed after its fill, and reaching it
                        # meant crossing its stop first).  Else ask.
                        prev = trades[-1]
                        closes_beyond = b["c"] >= level if side > 0 else b["c"] <= level
                        certain = (prev.entry_i < i or prev.at_open or prev.order_known
                                   or closes_beyond)
                        if not certain and not choose():
                            break
                        px = level
                stop, risk = _stop_for(side, px, spec, el, es, mid, buf)
                pos = Trade(side, i, px, stop, risk, at_open, first_ev, known)
                allowed = {-side}
                fresh, cursor = False, px
                continue
            # ---- an open position: is the stop hit in this bar?
            touched = l <= pos.stop if pos.side > 0 else h >= pos.stop
            if not touched:
                break
            if pos.entry_i < i and fresh:
                px = min(o, pos.stop) if pos.side > 0 else max(o, pos.stop)
                stopped_at_open = px == o
            elif pos.entry_i == i and same_bar_stop_certain(pos, b):
                px = pos.stop
            else:
                if not choose():
                    break           # this path: the extreme printed before the entry
                px = pos.stop
            pos.exit_i, pos.exit_px, pos.reason = i, px, "stop"
            trades.append(pos)
            pos = None
            fresh, cursor = False, px
        if (pos is not None and spec.hold_bars is not None
                and i == pos.entry_i + spec.hold_bars and i < spec.exit_idx):
            pos.exit_i, pos.exit_px, pos.reason = i, b["c"], "time"
            trades.append(pos)
            pos = None
        if i == spec.exit_idx and pos is not None:
            pos.exit_i, pos.exit_px, pos.reason = i, b["c"], "eod"
            trades.append(pos)
            pos = None
    return trades


def manage(bars: list[dict], i: int, side: int, px: float, stop: float, risk: float,
           at_open: bool, choose: Callable[[], bool], exit_idx: int = EXIT_IDX) -> Trade:
    """A single position opened in bar i (as that bar's FIRST event), held to its
    stop or the exit bar.  Never used on a both-levels-hit bar (callers exclude it)."""
    t = Trade(side, i, px, stop, risk, at_open, first_event=True)
    for j in range(i, exit_idx + 1):
        b = bars[j]
        touched = b["l"] <= stop if side > 0 else b["h"] >= stop
        if touched:
            if j > i:
                t.exit_i, t.exit_px, t.reason = j, (min(b["o"], stop) if side > 0
                                                    else max(b["o"], stop)), "stop"
                return t
            if same_bar_stop_certain(t, bars[i]) or choose():
                t.exit_i, t.exit_px, t.reason = j, stop, "stop"
                return t
    t.exit_i, t.exit_px, t.reason = exit_idx, bars[exit_idx]["c"], "eod"
    return t


def onebp_day(bars: list[dict], choose: Callable[[], bool], want_up: bool = True,
              buffer_frac: float = BUFFER_FRAC) -> list[Trade]:
    """1st Bar Positive: first bar closes up (or down for the control), its low
    later breaks -> short; stop = session high before the entry bar + buffer."""
    b0 = bars[0]
    up = b0["c"] > b0["o"]
    down = b0["c"] < b0["o"]
    if (want_up and not up) or (not want_up and not down):
        return []
    buf = buffer_frac * b0["c"]
    level = b0["l"] - buf
    for i in range(1, LAST_ENTRY_IDX + 1):
        b = bars[i]
        if b["l"] <= level:
            at_open = b["o"] <= level
            px = b["o"] if at_open else level
            stop = max(x["h"] for x in bars[:i]) + buf
            return [manage(bars, i, -1, px, stop, stop - level, at_open, choose)]
    return []


# ---------------------------------------------------------------- day results
def day_net(trades: Iterable[Trade], d: str, slip: float) -> float:
    return sum(t.pts - cost_points(t.entry_px, d, slip) for t in trades)


def resolve(run: Callable[[Chooser], list[Trade]], d: str, slip: float
            ) -> tuple[list[Trade], list[Trade], bool]:
    """(pessimistic trades, optimistic trades, ambiguous?) for one day."""
    paths = all_paths(run)
    paths.sort(key=lambda ts: day_net(ts, d, slip))
    worst, best = paths[0], paths[-1]
    ambiguous = len(paths) > 1 and day_net(worst, d, slip) != day_net(best, d, slip)
    return worst, best, ambiguous


@dataclass
class Row:
    d: str
    side: int
    entry_i: int
    entry_px: float
    exit_i: int
    exit_px: float
    reason: str
    risk: float
    pts: float
    cost: float
    ambiguous: bool
    n_in_day: int = 1
    extra: dict = field(default_factory=dict)

    @property
    def net(self) -> float:
        return self.pts - self.cost

    @property
    def r_gross(self) -> float:
        return self.pts / self.risk

    @property
    def r_net(self) -> float:
        return self.net / self.risk


def rows_for(d: str, trades: list[Trade], amb: bool, slip: float) -> list[Row]:
    return [Row(d, t.side, t.entry_i, t.entry_px, t.exit_i, t.exit_px, t.reason, t.risk,
                t.pts, cost_points(t.entry_px, d, slip), amb, k + 1,
                dict(order_known=t.order_known))
            for k, t in enumerate(trades)]


def run_breakout(days: dict[str, list[dict]], spec: Spec, slip: float = 1.0,
                 pessimistic: bool = True) -> list[Row]:
    out: list[Row] = []
    for d, bars in days.items():
        worst, best, amb = resolve(lambda ch: breakout_day(bars, spec, ch), d, slip)
        out += rows_for(d, worst if pessimistic else best, amb, slip)
    return out


def run_onebp(days: dict[str, list[dict]], want_up: bool = True, slip: float = 1.0,
              pessimistic: bool = True) -> list[Row]:
    out: list[Row] = []
    for d, bars in days.items():
        worst, best, amb = resolve(lambda ch: onebp_day(bars, ch, want_up), d, slip)
        out += rows_for(d, worst if pessimistic else best, amb, slip)
    return out


def control_trades(days: dict[str, list[dict]], primary: list[Row], slip: float = 1.0
                   ) -> list[tuple[Row, Row]]:
    """For each primary first trade: (follow, fade) with the same fill, stop
    distance = planned risk, same exit.  Pessimistic paths."""
    out = []
    for r in primary:
        # Both levels hit in the trigger bar: the side was picked by the outcome
        # (pessimistic path), so a control built on it would inherit that pick.
        if r.n_in_day != 1 or r.extra.get("order_known"):
            continue
        bars = days[r.d]
        at_open = bars[r.entry_i]["o"] == r.entry_px
        pair = []
        for side in (r.side, -r.side):
            stop = r.entry_px - side * r.risk
            paths = all_paths(lambda ch: [manage(bars, r.entry_i, side, r.entry_px, stop,
                                                 r.risk, at_open, ch)])
            t = min(paths, key=lambda ts: ts[0].pts)[0]
            pair.append(rows_for(r.d, [t], len(paths) > 1, slip)[0])
        out.append((pair[0], pair[1]))
    return out


def exit_free(days: dict[str, list[dict]], primary: list[Row], exit_idx: int = EXIT_IDX
              ) -> list[dict]:
    """After each first trigger, MFE / MAE / signed move to the exit close, in R.
    The entry bar's own range is excluded (its order vs the fill is unknown);
    its close is included."""
    out = []
    for r in primary:
        if r.n_in_day != 1 or r.extra.get("order_known"):
            continue
        bars = days[r.d]
        after = bars[r.entry_i + 1: exit_idx + 1]
        eb = bars[r.entry_i]
        gap = eb["o"] == r.entry_px
        # Entry bar: a gap fill means the whole bar printed after the fill; an
        # intrabar fill means only the side BEYOND the fill (away from the open)
        # is known to be after it -- that is the favourable side.  Review L1.
        hi_e = eb["h"] if (gap or r.side > 0) else eb["c"]
        lo_e = eb["l"] if (gap or r.side < 0) else eb["c"]
        hi = max([b["h"] for b in after] + [hi_e, eb["c"]])
        lo = min([b["l"] for b in after] + [lo_e, eb["c"]])
        fav, adv = (hi - r.entry_px, r.entry_px - lo) if r.side > 0 else \
                   (r.entry_px - lo, hi - r.entry_px)
        move = r.side * (bars[exit_idx]["c"] - r.entry_px)
        out.append(dict(d=r.d, mfe=fav / r.risk, mae=adv / r.risk, move=move / r.risk))
    return out
