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
contradicts the decision" is not established either. One instrument, measured
forward, is the way out.

## Three ways a shadow flatters itself, and what is done about each

**A price the engine would not have acted on.** The value stored beside a
breach is only as good as the book it came from. `reliable` off the raw quote
is NOT enough: by the time the monitor calls this, four separate things may
have already made that price unusable — the VALUE BOUND clamp (which returns
0.0 or `width` with `reliable=True`, and is what turns the cohort's overnight
measure from +1.3% into -12.1%), the 15-minute open buffer, the spot-corrob
veto, and the closing print. So the caller passes the POST-GATE usability and
the quality string says which, rather than a bare ok/unusable.

**A gap counted as a stop.** A breach first SEEN at a session's first poll is
one the rule did not catch at its level — it booked wherever the gap landed.
Two of the cohort's five firings were gaps. `gap` is therefore derived from the
CLOCK (inside the open buffer), not from "we have no earlier record", which
would also fire on a mid-session entry, on the first poll after deploy, and
after any dropped write — three things that are not gaps at all.

**A one-print stop no armed rule would take.** Every value trigger in this
engine is debounced (`DEBIT_SL_CONFIRM_POLLS`), and the standing rule is never
to act on a single top-of-book quote. So a breach records `confirmed_at` on the
next poll that still holds beyond the threshold, and the reader separates
one-print firings from confirmed ones. Counting the former would authorise a
rule nobody would ship.

## Contract

`observe()` returns the patch to persist, the same contract `zebra.mfe.compute`
and `zebra.depth.observe` have, so the caller folds it into the one batched
store write per cycle. It never gates anything and it never raises: this is
measurement sitting in the exit path, and a measurement that can throw is a new
way to fail an exit (`feedback_guard_the_money_system_first`).

SAFE-TO-RERUN: pure function of the record plus this poll; a breach is written
once and never re-priced, so replaying a session re-derives the same fields.

RETIRES WHEN: `spot_sl_enabled` is armed on the strength of the count, or the
count refuses it. Either way the shadow stops being the thing that decides.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

FIELD = 'spot_shadow'

#: Adverse-spot thresholds, in percent of entry spot. 1.5 and 2.0 are the two
#: the replay separates on; 3.0 is the value ALREADY stamped on every record as
#: `sl_spot`, carried so the count can speak about the live setting too.
THRESHOLDS = (1.5, 2.0, 3.0)


def breach_key(thr: float) -> str:
    """Field name for one threshold's first breach. Public: the reader needs
    it, and a CLI reaching into a module's private helper is how two spellings
    of the same key end up in the codebase."""
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


def _within_open_buffer(ts: str) -> Optional[bool]:
    """Is this poll inside the first `VALUE_TRIGGER_OPEN_BUFFER_SEC` of the
    session? None when the stamp cannot be parsed.

    The same window the value triggers are already dark for, reused rather than
    re-chosen: a breach the engine could not have acted on and a breach that
    arrived as a gap are the same fact seen from two sides.
    """
    try:
        t = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return None
    open_h, open_m = cfg.MARKET_OPEN
    since_open = (t.hour * 3600 + t.minute * 60 + t.second) - (open_h * 3600 + open_m * 60)
    return 0 <= since_open < cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC


def observe(trade: dict, spot: Optional[float], value: Optional[float],
            usable: bool, ts: str, quality: str = '') -> dict:
    """This poll's contribution to the shadow, or `{}` when nothing moved.

    `usable` is the caller's POST-GATE verdict on the price, not the raw book's
    `reliable` flag — see the module docstring. `quality` names the reason when
    it is False, so the count can be re-run excluding any one cause.
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

        if 'since' not in cur:
            # When this record STARTED being shadowed. Anything with a `since`
            # after its own entry has an unobserved head, so its MAE is a lower
            # bound and its first breach may already have happened — the reader
            # marks those PARTIAL rather than counting them.
            cur['since'] = ts
            changed = True

        if adv > float(cur.get('mae_pct', float('-inf'))):
            cur['mae_pct'] = round(adv, 3)
            cur['mae_at'] = ts
            cur['mae_spot'] = round(float(spot), 2)
            changed = True

        for thr in THRESHOLDS:
            k = breach_key(thr)
            b = cur.get(k)
            if b is None:
                if adv < thr:
                    continue
                gap = _within_open_buffer(ts)
                cur[k] = {
                    'at': ts,
                    'spot': round(float(spot), 2),
                    'adverse_pct': round(adv, 3),
                    'value': None if value is None else round(float(value), 2),
                    'q': 'ok' if usable else (quality or 'unusable'),
                    # None when the stamp could not be parsed: unknown, which
                    # the reader must not read as "not a gap".
                    'gap': gap,
                    # Set on the NEXT poll that still holds beyond the
                    # threshold. Absent means one print only — no armed rule
                    # in this engine would have acted on that.
                    'confirmed_at': None,
                }
                changed = True
            elif b.get('confirmed_at') is None and adv >= thr \
                    and b.get('at') != ts:
                b = dict(b)
                b['confirmed_at'] = ts
                cur[k] = b
                changed = True
            # A recorded breach is NEVER re-priced. A stop fires once;
            # overwriting it with a later, deeper print re-prices the
            # counterfactual to something no stop would have got.
        return {FIELD: cur} if changed else {}
    except Exception as e:                              # never raise into a cycle
        logger.warning("spot shadow failed for #%s: %s", trade.get('id'), e,
                       exc_info=True)
        return {}
