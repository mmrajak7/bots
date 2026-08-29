"""One arithmetic for what a spread is worth. Shared by both exit engines.

WHY VALUATION AND NOT THE TRIGGERS
----------------------------------
`zebra/monitor.py` and `bcs/spread_monitor.py` implement one policy twice, and
the design reviewer of 2026-08-29 asked for a shared valuation/trigger module
before arming, "since arming is when divergence becomes a live-money property".

The TRIGGERS are not merged, and must not be. The two engines' exit rules
differ ON PURPOSE and the differences are measured: zebra runs with the spot
stop OFF (a 3% stop cut 31 of 78 winners for a Rs 8.9L giveaway), anchors its
trail to max gain rather than to 2x debit, and stops on trading sessions rather
than on expiry day. `bcs/zebra_adapter.ZEBRA_EXIT_POLICY` carries those rules
with the TRADE for exactly that reason. Merging them would delete a stop
somebody measured.

The VALUATION is a different thing entirely. What a vertical is worth at a
given spot is arithmetic, not policy, and there is no defensible reason for the
two engines to disagree about it. They did.

THE DIVERGENCE THIS FILE CLOSES
-------------------------------
`bcs.spread_intrinsic_floor` and `zebra.monitor._intrinsic_floor` computed the
same quantity by different arithmetic, and the money path's version was the
better one on the part that matters:

* **the allowance ladder.** bcs derives the short leg's entry extrinsic from
  the entry price less its intrinsic at the entry spot, and when it has no
  basis at all it DISABLES the floor. zebra fell back to `0.3 * debit`, which
  B17 measured as TIGHTER than the truth on the real ICICI record -- 4.07
  against 7.65 -- so a healthy book fell below the floor and every valuation
  was refused for the rest of the session, taking SL_SPREAD, SL_TRAIL and the
  trail dark with it. That is the exact opposite of what the guard is for, and
  it is still live in the paper engine that values the cohort.
* **where the direction comes from.** bcs reads CE/PE off the leg SYMBOLS,
  because a bear put spread holds the HIGHER strike long and call arithmetic
  makes the floor inert for it -- the B21 finding, which cost the bear-put book
  months of an unguarded valuation. zebra reads a `direction` FIELD, which is a
  label, and a label can be wrong about what was actually bought.

**One claim that did NOT survive checking.** The obvious third bullet was
"zebra clamps the floor at zero and the money path does not, so the guard is
inert there in the loss region". `test_the_intrinsic_floor_is_inert_below_the_long_strike_by_construction`
refused it: both engines already clamp the VALUE to >= 0, so a floor at or
below zero rejects nothing either way and the clamp changes no outcome. It is
kept here because CLAUDE.md documents it and it costs nothing, not because it
fixes anything. The test caught the mistake, which is what it was written for.

THE ALLOWANCE LADDER, AND WHY IT ENDS IN None
---------------------------------------------
Ignorance must WIDEN the benefit of the doubt, never narrow it: a floor that is
too generous fails to catch one bad quote, and a floor that is too tight blinds
the engine to every quote. So the ladder runs from the best-known basis to the
loosest bound, and when there is no basis at all it returns None -- no floor --
rather than inventing `0.3 * debit`.
"""
from __future__ import annotations

from typing import Optional

from common import option_symbols as _sym

#: How much headroom the short leg's entry-time extrinsic is given. Extrinsic
#: peaks around ATM, which is roughly where the short leg is sold, and 1.5x
#: covers an IV spike. Deliberately generous: this guard fires on the
#: impossible, never on the merely unfavourable.
EXTRINSIC_HEADROOM = 1.5


def _legs(trade: dict):
    """(long strike, short strike, 'CE'|'PE') or None.

    The SYMBOLS decide, and a `direction` field is the fallback. Symbols first
    because they are the contract: `bcs/spread_monitor.py` used to infer calls
    unconditionally and the bear put book ran for months with a floor that
    could never fire. A `direction` on the record is a label, and a label can
    be wrong about what was actually bought.
    """
    k_l = _sym.strike(trade.get('long_symbol'))
    k_s = _sym.strike(trade.get('short_symbol'))
    opt_l = _sym.option_type(trade.get('long_symbol'))
    opt_s = _sym.option_type(trade.get('short_symbol'))
    if k_l is not None and k_s is not None and opt_l and opt_l == opt_s:
        return float(k_l), float(k_s), opt_l
    # zebra's schema carries strikes and a direction rather than symbols on
    # some records. Same arithmetic, weaker source, so it is second.
    try:
        k_l = float(trade['long_strike'])
        k_s = float(trade['short_strike'])
    except (KeyError, TypeError, ValueError):
        return None
    opt = str(trade.get('direction') or '').upper()
    if opt not in ('CE', 'PE'):
        return None
    return k_l, k_s, opt


def _allowance(trade: dict, k_s: float, opt: str) -> Optional[float]:
    """The short leg's extrinsic at entry, by the best basis available.

    Ordered best-first. Every rung is an UPPER bound on the true extrinsic or
    better, because being ignorant must widen the floor's benefit of the doubt
    rather than narrow it. None means no basis at all.
    """
    # 1. Measured and stored at entry.
    v = trade.get('short_extrinsic_entry')
    if v is not None:
        try:
            return max(float(v), 0.0)
        except (TypeError, ValueError):
            pass
    # 2. The alert pair this trade was entered from (zebra's records).
    for p in trade.get('alert_strikes') or []:
        try:
            if abs(float(p.get('k_s', -1)) - k_s) < 1e-6 \
                    and p.get('short_extrinsic') is not None:
                return max(float(p['short_extrinsic']), 0.0)
        except (TypeError, ValueError):
            continue
    # 3. Derived: what was received for the short leg, less whatever of it was
    #    intrinsic at the entry spot.
    short_px = trade.get('entry_short_price')
    if short_px is None:
        short_px = trade.get('short_bid_entry')
    if short_px is None:
        # NO BASIS. `0.3 * debit` was the old zebra fallback and B17 measured
        # it as TIGHTER than the truth on a real record, which blinded the
        # monitor for a session. No floor is a known gap; a wrong floor is a
        # guard that refuses healthy books.
        return None
    try:
        short_px = float(short_px)
    except (TypeError, ValueError):
        return None
    entry_spot = trade.get('entry_spot')
    if entry_spot is None:
        # The whole premium is a strict upper bound on the extrinsic, so this
        # is the generous reading -- which is the right direction when ignorant.
        return max(short_px, 0.0)
    try:
        es = float(entry_spot)
    except (TypeError, ValueError):
        return max(short_px, 0.0)
    intrinsic_at_entry = max(es - k_s, 0.0) if opt == 'CE' \
        else max(k_s - es, 0.0)
    return max(short_px - intrinsic_at_entry, 0.0)


def intrinsic_floor(trade: dict, spot: float,
                    long_multiplier: float = 1.0) -> Optional[float]:
    """No-arbitrage floor for a debit structure at `spot`, or None.

    A structure cannot be worth less than it could be unwound for, so a quote
    below this is proof of a broken book rather than an unlucky price. July
    2026: ABB #242 booked a -50% stop at a mid of 335 when pure intrinsic at
    the recorded spot was 1,020.

    NEVER BELOW ZERO, though not for the reason it looks like. Zero IS a real
    floor -- a debit structure is never worth less than nothing, because it can
    always be left to expire -- and CLAUDE.md's valuation-bounds table says to
    clamp it. What the clamp does NOT do is make the guard fire where it
    otherwise would not: both engines clamp the VALUE to >= 0 first, so a floor
    at or below zero rejects nothing either way. See the module docstring.

    `long_multiplier` is 2 for the retired back-ratio (two long legs) and 1 for
    every vertical. It is an argument rather than something read off the record
    because the record's own vocabulary for it differs between books, and a
    reader that guessed would mis-price the whole structure rather than fail.

    Returns None when the floor cannot be computed. None is not zero: no floor
    means the guard stands down, which is the safe direction for a check whose
    false positives blind the engine.
    """
    try:
        legs = _legs(trade)
        if legs is None:
            return None
        k_l, k_s, opt = legs
        spot = float(spot)
        mult = float(long_multiplier)
        if opt == 'CE':
            intrinsic = mult * max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
        else:
            intrinsic = mult * max(k_l - spot, 0.0) - max(k_s - spot, 0.0)
        allowance = _allowance(trade, k_s, opt)
        if allowance is None:
            return None
        return max(0.0, round(intrinsic - EXTRINSIC_HEADROOM * allowance, 2))
    except Exception:
        # A valuation guard that raises is a new way to fail an exit that has
        # already been decided. It may refuse; it may never interfere.
        return None
