"""Max favourable excursion — how far a trade got in its favour.

Nothing in this store has ever recorded a peak. That makes the owner's
question — "why do we get close to target and give it all back?" — literally
unanswerable: an exit row shows where a trade ENDED, never where it had been.
Every give-back number quoted so far is spot-proxy reconstruction from daily
bars, which cannot see an intraday peak and cannot price the structure at all.

Two channels are tracked per trade:
  mfe_spot — best underlying print (direction-aware: CE up, PE down)
  mfe_mid  — best structure mid, i.e. the peak the P&L actually reached

`capture()` is called once per entered trade per monitor poll. `giveback()`
turns the resulting rows into the table that answers the question. The peak
machinery here is also what a gain-anchored trailing stop consumes, which is
why it lives in its own module rather than inside the monitor loop.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

IST = cfg.IST


_PEAK_KEYS = ('peak', 'at', 'cand_n', 'cand_v', 'cand_t')


def _peak_state(trade: dict, ch: str) -> dict:
    """Load one channel's peak state off the trade (missing fields → fresh)."""
    return {
        'peak': trade.get(f'mfe_{ch}'),
        'at': trade.get(f'mfe_{ch}_at'),
        'cand_n': int(trade.get(f'mfe_{ch}_cand_n') or 0),
        'cand_v': trade.get(f'mfe_{ch}_cand_v'),
        'cand_t': float(trade.get(f'mfe_{ch}_cand_t') or 0.0),
    }


def _peak_fields(st: dict, ch: str) -> dict:
    """The inverse of _peak_state — the patch handed to store.apply_mfe."""
    return {f'mfe_{ch}' if k == 'peak' else f'mfe_{ch}_{k}': st[k]
            for k in _PEAK_KEYS}


def _peak_update(st: dict, value: float, floor: float, jump_limit,
                 now: float, stamp: str, sign: float = 1.0) -> bool:
    """Advance a monotone peak behind a jump-plausibility gate. Mutates `st`.

    Returns True when anything changed and the state must be persisted.

    A peak is a MAX, so its failure mode is the opposite of a stop's: one
    garbage-high print corrupts it PERMANENTLY, and no later good quote can
    undo it — where a garbage LOW is transient and the DEBIT-SL debounce
    already handles it. So a proposed peak beyond `jump_limit` is not accepted
    on sight; it must repeat across MFE_CONFIRM_POLLS, and what finally gets
    stored is the most conservative reading of that window. Same design as
    bcs.update_trail, which exists because a single bad bid once poisoned trail
    state on disk and fired a delayed false stop.

    `sign` is +1 when a HIGHER reading is more favourable and -1 when a lower
    one is (a PE trade's underlying), so both directions share one
    implementation and there is no second copy to drift out of step.
    """
    before = dict(st)
    if st['peak'] is None:
        # Seed at entry: MFE of a trade that never went favourable is zero
        # excursion, not "unknown". `at` stays None until a real peak lands,
        # which is what distinguishes the two.
        st['peak'] = floor
    peak = st['peak']

    if sign * value <= sign * peak:
        st['cand_n'], st['cand_v'] = 0, None
        return st != before

    if sign * value <= sign * jump_limit(peak):
        accepted = value
        st['cand_n'], st['cand_v'] = 0, None
    else:
        # Stale windows restart, same rule as bump_confirm: confirming polls
        # must be reasonably contiguous or a bad tick could pair with an
        # unrelated one hours later and validate itself.
        if now - st['cand_t'] > cfg.CONFIRM_STALE_SEC:
            st['cand_n'], st['cand_v'] = 0, None
        st['cand_t'] = now
        if st['cand_v'] is None or sign * value < sign * st['cand_v']:
            st['cand_v'] = value
        st['cand_n'] += 1
        if st['cand_n'] < cfg.MFE_CONFIRM_POLLS:
            return st != before
        accepted = st['cand_v']
        st['cand_n'], st['cand_v'] = 0, None

    if sign * accepted > sign * peak:
        st['peak'] = accepted
        st['at'] = stamp
    return st != before


def compute(trade: dict, spot: Optional[float],
            mid: Optional[float], reliable: bool) -> dict:
    """Advance a trade's peaks and return the patch to persist ({} if none).

    Pure apart from mutating `trade` in place: it does NOT write. The caller
    batches a whole cycle's patches into ONE store write, because the store
    file is ~1 MB and this runs for every open position on every poll — a write
    per trade per poll would rewrite ~20 MB a poll on a trending day and hold
    the cross-process lock across every one of them, on a Pi that also runs the
    live-money monitor.

    The caller must flush BEFORE booking any exit: `_merge` gives DISK the tie
    on equal versions, so an unflushed patch would be silently discarded by
    mark_exited's own refresh — and mark_exited is the drive=True write that
    carries these fields off the Pi at the moment they finally matter.
    """
    tid = trade['id']
    # CE profits as the underlying rises, PE as it falls.
    sign = 1.0 if trade.get('direction') == 'CE' else -1.0
    now = time.time()
    stamp = datetime.now(IST).isoformat(timespec='seconds')
    patch, advanced = {}, []

    # `spot=None` HOLDS this channel, the same way `mid=None` holds the other.
    # The caller passes it when the cash market is in its closing auction and
    # spot is frozen by market design: the print is a repeat of the 15:15 one
    # this peak has already seen, so advancing on it would be recording the
    # same observation fifteen more times a session.
    if spot is not None:
        st = _peak_state(trade, 'spot')
        was = st['peak']
        entry_spot = float(trade.get('entry_spot') or trade.get('trigger_spot')
                           or spot)
        if _peak_update(st, float(spot), entry_spot,
                        lambda p: p + sign * abs(p) * cfg.MFE_SPOT_JUMP_PCT,
                        now, stamp, sign):
            patch.update(_peak_fields(st, 'spot'))
            if st['peak'] != was and was is not None:
                advanced.append(f"spot={st['peak']:.2f}")

    # The mid channel is the one that measures MONEY, and it is gated on a
    # reliable book for the same reason the DEBIT-SL is: this fleet has twice
    # been handed a two-sided, tight-looking book quoting an impossible price.
    debit = float(trade.get('debit') or 0.0)
    if mid is not None and reliable and debit > 0:
        st = _peak_state(trade, 'mid')
        was = st['peak']
        if _peak_update(st, float(mid), debit,
                        lambda p: p + abs(p) * (cfg.MFE_JUMP_MULT - 1.0),
                        now, stamp, 1.0):
            patch.update(_peak_fields(st, 'mid'))
            if st['peak'] != was and was is not None:
                advanced.append(f"mid={st['peak']:.2f} "
                                f"({(st['peak'] / debit - 1) * 100:+.0f}%)")

    if not patch:
        return {}
    # Keep the caller's dict in step: later code in this same cycle must not
    # read a stale peak just because the write has not been flushed yet.
    trade.update(patch)
    if advanced:
        logger.info("MFE #%d %s new peak %s", tid, trade['stock'],
                    ' '.join(advanced))
    return patch


# ── Gain-anchored trailing stop ────────────────────────────────────────────
# The measured leak: 10 of 116 closed trades got most of the way to target and
# still booked a loss, costing about twice what the whole book made. A trail is
# the direct answer, and it does NOT collide with the power-law rule: that rule
# forbids capping a tail, and a BCS caps its own upside at ENTRY
# (max value = width), so there is no tail above the short strike to protect.
# The rule still governs which spreads to ENTER — keep "no d/w floor" forever.
#
# Anchored to a FRACTION OF MAX GAIN, not to a multiple of debit. The live
# monitor's fixed 2x-debit engage sounds equivalent and is not: it lands at 43%
# of max gain on a 30% d/w spread but 82% on a 45% one, so it silently tightens
# exactly as the payoff shrinks, and it would have engaged on only 2 of 32
# closed shadows. Reusing that constant here would have shipped a trail that
# almost never fires while looking like it works.

def trail_levels(trade: dict) -> Optional[dict]:
    """Trail state for a capped-payoff structure, or None if it has none.

    Everything is DERIVED from `mfe_mid` plus config — deliberately not
    persisted. The peak is already durable, and a second stored copy of a level
    computed from it is a source that can drift out of step with the peak it
    claims to describe.

    Returns None for zebra: a back-ratio has two longs and no capped payoff, so
    "fraction of max gain" has no meaning there. Zebra is retired and its open
    positions run to expiry untrailed, which is the intended behaviour, not an
    oversight.
    """
    width = trade.get('width')
    debit = trade.get('debit')
    if width is None or debit is None:
        return None
    width, debit = float(width), float(debit)
    max_gain = width - debit
    if max_gain <= 0:                      # degenerate: debit at or above width
        return None

    peak_mid = trade.get('mfe_mid')
    if peak_mid is None:
        return None
    peak_gain = float(peak_mid) - debit
    engage_gain = max_gain * cfg.TRAIL_ENGAGE_FRAC
    # Armed off the PEAK, not the current gain. That is the entire point: a
    # position that WAS up half its max and has since given it back is exactly
    # the case this exists for, and arming off the live gain would disarm at
    # the moment it starts mattering.
    armed = peak_gain >= engage_gain
    return {
        'max_gain': round(max_gain, 2),
        'engage_gain': round(engage_gain, 2),
        'peak_gain': round(peak_gain, 2),
        'peak_pct_of_max': round(peak_gain / max_gain * 100, 1),
        'armed': armed,
        # Strictly above the entry debit while peak_gain > 0. That bounds the
        # LEVEL, not the FILL: the monitor fires on `mid <= level` and books at
        # `mid`, so a gap straight through the level books wherever the gap
        # landed — possibly below the debit, i.e. a loss tagged `trail`. Do not
        # restate this as "a fired trail always books a profit"; it was written
        # that way once and outcomes.py scored the losses as HITs.
        'level': round(debit + peak_gain * cfg.TRAIL_RETAIN_FRAC, 2),
    }


# ── Analysis ───────────────────────────────────────────────────────────────

def excursion(trade: dict) -> Optional[dict]:
    """Peak-vs-outcome for one trade, or None if it was never measured.

    Deliberately returns None rather than zeros for trades that closed before
    capture existed. A give-back table padded with fake zeros would read as
    "these trades gave nothing back", which is the opposite of unknown — and
    every trade in the book today is in that state.
    """
    if trade.get('status') != 'exited':
        return None
    peak_mid = trade.get('mfe_mid')
    debit = float(trade.get('debit') or 0.0)
    if peak_mid is None or debit <= 0:
        return None

    # The exit price is a BOOKED price, not a book quote, so it is stronger
    # evidence than anything the peak tracker saw — and the jump gate can
    # legitimately have refused the exit poll's own mid (a big final-poll move
    # has no following poll to confirm it). Without this clamp a trade that
    # closed above its recorded peak would report a NEGATIVE give-back, which
    # is not a small display bug: it is the table silently disagreeing with the
    # P&L it sits next to.
    qty = int(trade.get('quantity') or 0)
    exit_debit = trade.get('exit_debit')
    peak_mid = float(peak_mid)
    if exit_debit is not None:
        peak_mid = max(peak_mid, float(exit_debit))
    peak_gain = (peak_mid - debit) * qty
    pnl = float(trade.get('pnl') or 0.0)
    row = {
        'id': trade.get('id'),
        'stock': trade.get('stock'),
        'structure': trade.get('structure', 'zebra'),
        'reason': trade.get('exit_reason'),
        'peak_gain': round(peak_gain, 2),
        'pnl': round(pnl, 2),
        # What the peak was worth that the exit did not collect. Only
        # meaningful when the trade was actually ahead at some point.
        'given_back': round(peak_gain - pnl, 2) if peak_gain > 0 else None,
        'kept_pct': round(pnl / peak_gain * 100, 1) if peak_gain > 0 else None,
        'peak_at': trade.get('mfe_mid_at'),
    }

    # Spot progress toward target at the peak, on the same 0-1 scale the
    # give-back watch will use. tp_spot is the ST line, so this says how much
    # of the thesis actually played out before the position was closed.
    entry_spot = trade.get('entry_spot')
    tp_spot = trade.get('tp_spot')
    peak_spot = trade.get('mfe_spot')
    if None not in (entry_spot, tp_spot, peak_spot) \
            and float(tp_spot) != float(entry_spot):
        span = float(tp_spot) - float(entry_spot)
        row['tp_progress'] = round(
            (float(peak_spot) - float(entry_spot)) / span, 3)
    return row


def giveback(trades: list, min_progress: float = 0.7) -> dict:
    """Aggregate the give-back leak across measured, closed trades.

    `min_progress` is the fraction of the way to target a trade must have
    reached to count as "was winning" — 0.7 matches the reconstruction that
    first surfaced this leak, so the two are comparable once enough trades
    carry real measurements.
    """
    rows = [r for r in (excursion(t) for t in trades) if r]
    winners_lost = [r for r in rows
                    if r['pnl'] < 0 and (r.get('tp_progress') or 0) >= min_progress]
    ahead = [r for r in rows if r['peak_gain'] > 0]
    return {
        'measured': len(rows),
        'unmeasured': sum(1 for t in trades if t.get('status') == 'exited'
                          and t.get('mfe_mid') is None),
        'was_ahead': len(ahead),
        'given_back': round(sum(r['given_back'] or 0 for r in ahead), 2),
        'kept_pct_median': _median([r['kept_pct'] for r in ahead
                                    if r['kept_pct'] is not None]),
        'reached_then_lost': len(winners_lost),
        'reached_then_lost_pnl': round(sum(r['pnl'] for r in winners_lost), 2),
        'rows': sorted(rows, key=lambda r: r['given_back'] or 0, reverse=True),
    }


def _median(vals: list) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 1)
