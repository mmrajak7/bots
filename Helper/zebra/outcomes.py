"""Join Claude's decisions to what actually happened.

Without this module the decision journal is unfalsifiable: it records every
verdict and never learns whether any of them were right. `score()` reads
`outcome`, and until now nothing ever wrote one.

Two kinds of decision need two different joins:

**Allowed / unavailable entries** became real (paper) trades, so the outcome is
the realised P&L of those trades. Easy, and the honest measure.

**Vetoed entries never happened**, which is exactly why vetoes are the hardest
thing to score and the most important — a veto layer nobody can measure is a
layer you are trusting on faith. So a vetoed signal keeps a SHADOW: the spot
level at veto time, the target it was aiming at, and a symmetric adverse
barrier the same distance away. Whichever it touches first labels the signal.

Why a spot triple-barrier and not a shadow paper trade
------------------------------------------------------
A shadow trade would need live option quotes for a structure nobody holds,
every cycle, for weeks — more API load and more failure modes than the answer
justifies. And several vetoes are *about* the option book being untradeable, so
pricing the veto against that same book begs the question. Spot is the one
input that is always real. The barrier is symmetric (target distance mirrored
below entry) so the label is not tilted by an arbitrary stop width.

What this measure IS and IS NOT
-------------------------------
It answers "was the SIGNAL right — did price go where the strategy said before
it went the other way". It does NOT answer "how many rupees did the veto save",
because the vetoed structure was never priced. Both are reported, labelled by
`basis`, and never silently mixed: realised P&L carries basis='realized',
shadow labels carry basis='spot_barrier' and no `pnl` at all — so the existing
money-weighted score() simply ignores them rather than treating a proxy as cash.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from . import config as cfg
from . import vet as vet_mod

logger = logging.getLogger(__name__)

# Signal-quality labels, shared by both joins so the two are comparable.
HIT = 'hit'          # reached the target the strategy was aiming at
MISS = 'miss'        # went the other way by the same distance first
FLAT = 'flat'        # neither, before the position ran out of time

# Exit reasons mapped onto the same labels. TIME is deliberately FLAT, not a
# miss: expiring without resolution says the signal was slow, not wrong, and
# scoring it as a loss would make every veto of a slow signal look brilliant.
# TRAIL is a HIT, not a miss: the level sits above the entry debit by
# construction, so a trailed exit always books a profit and the signal did what
# it was supposed to. Scoring it MISS would punish the allow that worked and
# make the trail itself look like a source of losses.
_REASON_LABEL = {'tp': HIT, 'trail': HIT,
                 'spot_sl': MISS, 'debit_sl': MISS, 'time': FLAT}


def label_for_reason(reason) -> str:
    """Map an exit reason to a signal-quality label.

    Strips the `paper:` prefix that `_paper_auto_close` stamps on every
    auto-booked exit. Without this, PAPER mode — the only mode running today —
    labels every single allow FLAT, allow-precision is None forever, and the
    veto-vs-allow comparison this whole score exists for can never produce a
    number. report.py strips the same prefix in three places.
    """
    return _REASON_LABEL.get(str(reason or '').replace('paper:', ''), FLAT)


def _now() -> datetime:
    return datetime.now()


# ── vetoed signals: the shadow ───────────────────────────────────────────
def open_shadow(store, trade_id: int,
                entry_spot: Optional[float] = None) -> Optional[dict]:
    """Start tracking a vetoed signal. Idempotent.

    Called the first time the monitor observes a VETOED signal. The barriers
    are frozen here, at veto time, so a later drift in spot or in the ST line
    cannot move the goalposts and flatter the verdict after the fact.

    `entry_spot` is the LIVE spot at the moment the veto was observed, and is
    the right anchor: `signal_price` is the price when the signal was added to
    the watchlist, up to 5% from the ST line and possibly days earlier, so
    anchoring there would measure a move that started before the decision. It
    falls back to `trigger_spot` (set at trigger time) and only then to
    `signal_price`.
    """
    with store._mutate():
        t = store._must_find(trade_id)
        if isinstance(t.get('veto_shadow'), dict):
            return t['veto_shadow']
        entry = entry_spot or t.get('trigger_spot') or t.get('signal_price')
        target = t.get('st_value')
        if not entry or not target:
            logger.warning('Veto shadow for #%d skipped — no entry/target spot',
                           trade_id)
            return None
        entry, target = float(entry), float(target)
        distance = abs(target - entry)
        if distance <= 0:
            logger.warning('Veto shadow for #%d skipped — zero barrier width',
                           trade_id)
            return None
        adverse = entry - distance if t['direction'] == 'CE' else entry + distance
        shadow = {
            'status': 'open',
            'basis': 'spot_barrier',
            'opened_at': _now().isoformat(),
            'entry_spot': entry,
            'target_spot': target,
            'adverse_spot': round(adverse, 2),
            'direction': t['direction'],
            # Vetoed signals never picked an expiry, so the shadow needs its own
            # horizon or it would track forever and never score anything.
            'horizon_days': cfg.VETO_SHADOW_DAYS,
            'decision_id': (t.get('vet') or {}).get('decision_id'),
            'label': None,
        }
        t['veto_shadow'] = shadow
        t['version'] = t.get('version', 0) + 1
        logger.info('VETO SHADOW opened #%d %s: target %.2f / adverse %.2f',
                    trade_id, t['stock'], target, shadow['adverse_spot'])
        return shadow


def _resolve(shadow: dict, spot: float, now: datetime) -> Optional[str]:
    """Which barrier did spot touch first? None while still open."""
    up = shadow['direction'] == 'CE'
    if (up and spot >= shadow['target_spot']) or \
            (not up and spot <= shadow['target_spot']):
        return HIT
    if (up and spot <= shadow['adverse_spot']) or \
            (not up and spot >= shadow['adverse_spot']):
        return MISS
    opened = vet_mod._parse(shadow.get('opened_at'))
    if opened and (now - opened).days >= int(shadow.get('horizon_days') or 0):
        return FLAT
    return None


def track_shadows(store, ltps: dict, now: Optional[datetime] = None) -> list:
    """Advance every open veto shadow one tick. Returns those that resolved.

    Cheap by construction: it reuses the LTPs the monitor already fetched for
    its open positions, so tracking vetoes costs no extra API calls.
    """
    now = now or _now()
    resolved = []
    for t in list(store.load_trades()):
        shadow = t.get('veto_shadow')
        if not isinstance(shadow, dict) or shadow.get('status') != 'open':
            continue
        spot = ltps.get(t['stock'], 0)
        if not spot or spot <= 0:
            continue
        label = _resolve(shadow, float(spot), now)
        if label is None:
            continue
        with store._mutate():
            fresh = store.find(t['id'])
            s = fresh.get('veto_shadow') if fresh else None
            # Re-check inside the lock: another cycle may have resolved it.
            if not isinstance(s, dict) or s.get('status') != 'open':
                continue
            s['status'] = 'resolved'
            s['label'] = label
            s['resolved_at'] = now.isoformat()
            s['resolved_spot'] = float(spot)
            fresh['version'] = fresh.get('version', 0) + 1
            resolved.append(dict(fresh['veto_shadow'], trade_id=fresh['id'],
                                 stock=fresh['stock']))
    for r in resolved:
        logger.info('VETO SHADOW resolved #%d %s -> %s at %.2f',
                    r['trade_id'], r['stock'], r['label'], r['resolved_spot'])
    return resolved


# ── the join ─────────────────────────────────────────────────────────────
def _entry_outcome(store, decision: dict) -> Optional[dict]:
    """Outcome for one entry decision, or None if it is not settled yet."""
    ids = (decision.get('signal_ref') or {}).get('trade_ids') or []
    trades = [store.find(i) for i in ids]
    trades = [t for t in trades if t]
    if not trades:
        return None

    if decision.get('verdict') == 'veto':
        # Vetoed: no trade ever opened, so the shadow is the only evidence.
        shadows = [t['veto_shadow'] for t in trades
                   if isinstance(t.get('veto_shadow'), dict)]
        done = [s for s in shadows if s.get('status') == 'resolved']
        if not done:
            return None
        return {
            'basis': 'spot_barrier',
            'label': done[0]['label'],
            'resolved_spot': done[0].get('resolved_spot'),
            # Deliberately NO 'pnl': the vetoed structure was never priced, and
            # inventing one would let a proxy masquerade as cash in score().
            'note': 'signal-quality label only; no position was taken',
        }

    # Allowed / unavailable: score the real thing. Both arms share one verdict,
    # so the outcome is the sum across whichever arms actually traded.
    #
    # Arms are resolved by `shadow_of` rather than trusting `trade_ids`: under
    # vetting the BCS shadow is built at ENTRY, which happens on the tick AFTER
    # the verdict, so it cannot exist when the decision is journalled and is
    # never in trade_ids. Trusting that list would silently score the zebra arm
    # alone while claiming to cover both.
    ids = {t['id'] for t in trades}
    trades = trades + [t for t in store.load_trades()
                       if t.get('shadow_of') in ids and t['id'] not in ids]

    live = [t for t in trades if t.get('status') == 'entered']
    closed = [t for t in trades if t.get('status') == 'exited']
    if live:
        return None                      # at least one arm still open
    if not closed:
        # No arm ever opened. If the signal is terminal (drift/stale-cancelled
        # between the verdict and the entry tick) this decision can NEVER
        # settle, and leaving it pending means re-scanning it every 5 minutes
        # forever. Settle it as "not taken" — a real outcome, excluded from
        # both scores because no judgement of ours produced it.
        if all(t.get('status') == 'cancelled' for t in trades):
            return {'basis': 'not_taken',
                    'note': 'signal cancelled before entry; verdict never acted on'}
        return None                      # still watching/triggered
    pnl = round(sum(float(t.get('pnl') or 0) for t in closed), 2)
    reasons = [t.get('exit_reason') for t in closed]
    return {
        'basis': 'realized',
        'pnl': pnl,
        'pnl_pct': round(sum(float(t.get('pnl_pct') or 0)
                             for t in closed) / len(closed), 2),
        'exit_reason': reasons[0],
        'label': label_for_reason(reasons[0]),
        'arms': {t.get('structure') or 'zebra': float(t.get('pnl') or 0)
                 for t in closed},
    }


def join(store, decisions=None) -> int:
    """Write outcomes for every decision that has settled. Returns how many.

    Safe to run every cycle: decisions already carrying an outcome are skipped,
    and a decision whose trades are still open is simply left for next time.
    """
    from .decisions import get_store as get_decisions
    decisions = decisions or get_decisions()
    decisions.refresh()
    joined = 0
    for d in list(decisions.pending_outcome(kind='entry')):
        try:
            outcome = _entry_outcome(store, d)
            if outcome is None:
                continue
            decisions.set_outcome(d['id'], outcome)
            joined += 1
            logger.info('DECISION #%s outcome: %s %s', d.get('id'),
                        outcome['basis'], outcome.get('label'))
        except Exception as e:            # one bad row must not stall the rest
            logger.error('Outcome join failed for decision #%s: %s',
                         d.get('id'), e)
    return joined
