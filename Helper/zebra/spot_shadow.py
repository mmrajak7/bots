"""SHADOW ONLY: what an adverse-spot stop would have done. Measures, never acts.

## Why this exists rather than a config flip

Replaying the cohort's 5-minute value paths says an adverse-spot stop is the
best loss-side fix available: at 1.5% it cut 0 of 12 winners, caught 5 of 5
losers, and took the book's payoff from 0.79 to 1.44. That is also exactly the
shape of an overfit, and the arithmetic says so out loud.

    one TRUE stop saves  ~25.6 points of debit
    one FALSE stop costs ~60.5 points (the forgone win plus the booked stop)
    => the rule must fire correctly ~70% of the time merely to break even

Observed is 5 of 5. The 95% Clopper-Pearson lower bound on 5/5 is 54.9% —
BELOW the 70% it has to clear. Five observations is not evidence; it is the
start of a count, and the count that matters is FIRINGS, not trades. So this
module does the counting, and `spot_sl_enabled` stays False until the count
answers.

This also contradicts a standing decision (`zebra/config.py` SPOT_SL_ENABLED,
fitted on 147 pre-cohort records where a 3% stop cut 40% of winners). Those
records are out of scope by the cohort rule, but they were also measured on
DAILY CANDLE lows while this is 5-minute poll spot, and daily lows are
systematically deeper. The two numbers are not comparable, so "the cohort
contradicts the decision" is not yet established either. One instrument,
measured forward, is the way out.

## What it records

Per position, the adverse excursion (MAE) and, for each threshold, the FIRST
breach: when, at what spot, at what structure value, and whether it landed on
the session's first poll. That last flag matters more than it looks — a breach
first seen at the open is a GAP, where the stop books wherever the gap landed
and not at its level. Two of the five cohort firings were gaps.

## Contract

`observe()` returns the patch to persist, the same contract `zebra.mfe.compute`
and `zebra.depth.observe` have, so the caller folds it into the one batched
store write per cycle. It never gates anything and it never raises: this is
measurement sitting in the exit path, and a measurement that can throw is a new
way to fail an exit (`feedback_guard_the_money_system_first`).

SAFE-TO-RERUN: pure function of the record plus this poll; a breach is written
once and never overwritten, so replaying a session re-derives the same fields.

RETIRES WHEN: `spot_sl_enabled` is armed on the strength of the count, or the
count refuses it. Either way the shadow stops being the thing that decides.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

FIELD = 'spot_shadow'

#: Adverse-spot thresholds, in percent of entry spot. 1.5 and 2.0 are the two
#: the replay separates on; 3.0 is the value ALREADY stamped on every record as
#: `sl_spot`, carried so the count can speak about the live setting too.
THRESHOLDS = (1.5, 2.0, 3.0)


def _key(thr: float) -> str:
    return 'b%s' % ('%g' % thr).replace('.', '_')


def adverse_pct(trade: dict, spot: float) -> Optional[float]:
    """How far spot has moved AGAINST this position, in percent. Signed.

    Negative means favourable. CE loses as the underlying falls, PE as it
    rises, so the sign flips with direction — the one line this whole module
    would be wrong in both directions without.
    """
    try:
        entry = trade.get('entry_spot') or trade.get('trigger_spot')
        if not entry:
            return None
        sign = 1.0 if trade.get('direction') == 'CE' else -1.0
        return -sign * (float(spot) - float(entry)) / float(entry) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def observe(trade: dict, spot: Optional[float], value: Optional[float],
            reliable: bool, ts: str) -> dict:
    """This poll's contribution to the shadow, or `{}` when nothing moved.

    `value` is the structure's fill-basis value and may be None or unreliable —
    a breach is still RECORDED then, with `q` saying so, because refusing to
    record it would hide precisely the case where a real stop could not have
    booked either.
    """
    try:
        if spot is None:
            return {}
        adv = adverse_pct(trade, spot)
        if adv is None:
            return {}
        prev = trade.get(FIELD)
        cur = dict(prev) if isinstance(prev, dict) else {}
        changed = False

        # Derived here, not passed in: the caller would have to carry
        # per-trade session state it has no other use for, and a position
        # entered mid-session has a different "first poll" from the book's.
        session = ts[:10]
        first_poll_of_session = cur.get('last_date') != session
        if first_poll_of_session:
            cur['last_date'] = session
            changed = True

        if adv > float(cur.get('mae_pct', float('-inf'))):
            cur['mae_pct'] = round(adv, 3)
            cur['mae_at'] = ts
            cur['mae_spot'] = round(float(spot), 2)
            changed = True

        for thr in THRESHOLDS:
            k = _key(thr)
            if k in cur or adv < thr:
                # FIRST breach only. A stop fires once; overwriting it with a
                # later, deeper print would quietly re-price the counterfactual
                # to something no stop would have got.
                continue
            cur[k] = {
                'at': ts,
                'spot': round(float(spot), 2),
                'adverse_pct': round(adv, 3),
                'value': None if value is None else round(float(value), 2),
                'q': 'ok' if (value is not None and reliable) else 'unusable',
                # A breach first SEEN at the session's first poll is a gap: the
                # stop did not fire at its level, it fired wherever the gap
                # landed. Counting those as clean firings is how a stop flatters
                # itself.
                'gap': bool(first_poll_of_session),
            }
            changed = True
        return {FIELD: cur} if changed else {}
    except Exception as e:                              # never raise into a cycle
        logger.warning("spot shadow failed for #%s: %s", trade.get('id'), e,
                       exc_info=True)
        return {}
