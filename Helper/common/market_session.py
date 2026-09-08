"""When the CASH market stops printing, and why that is not the same as closed.

## The observation

Across every session on record, the NSE cash-market LTP for every open
position freezes solid at **15:15** and does not move again until a single new
price arrives at ~15:29-15:31:

    date        longest identical-spot run   from        to
    2026-08-27  24 polls                     15:15:05    15:28:04   (12m)
    2026-08-28  25 polls                     15:15:32    15:28:58   (13m)
    2026-09-01  29 polls                     15:15:02    15:29:48   (14m)
    2026-09-02  28 polls                     15:15:18    15:29:33   (14m)
    2026-09-03  26 polls                     15:15:24    15:28:33   (13m)

(5-second polls, from `logs/spread_monitor_cron_*.log`.) On the 5-minute path
record the same window is identical at 15:15/15:20/15:25 on **125 of 125**
position-sessions, against 0 of 122 for 12:15/12:20/12:25.

**And the OPTION books kept moving on 125 of 125.** The two segments come down
the same feed, so a broken feed cannot produce that; a cash-market CALL AUCTION
can, and does. Continuous cash trading ends, order collection runs with no
matching so no trade prints, and the auction uncrosses into one closing price.
In February 2026 the same box saw spot moving normally at 15:14-15:20
(`logs/cron_bcs.log`), so this is a change in market structure, not a property
of the venue to be assumed permanent.

## What it is NOT

It is not the market closing. **The DERIVATIVES segment goes on trading until
15:40**, and the option book moved between the 15:25 and 15:30 polls on 125 of
125 position-sessions. A first cut of this work gated BOOKING at 15:30 on the
theory that "no live order fills there"; that was false, and a guard that
refuses a genuine exit is the expensive direction — it pushes the position
overnight, which is how this book's worst loss happened. Only SPOT dies early.

## Why any of this is a bug rather than a curiosity

Because two guards in this fleet infer something from spot STILLNESS, and
during the auction stillness is guaranteed by market design:

* **`_spot_corroborates`** (in BOTH engines) vetoes an exit when structure
  value collapses >= 35% while spot moves < 0.4%. Between 15:15 and 15:30 spot
  moves exactly 0.00%, so a genuine collapse in the last fifteen minutes is
  refused as "uncorroborated" — the NHPC signature — for a reason that has
  nothing to do with the market. It has never fired in that window yet; the
  exposure is latent, and the cost when it lands is holding a collapsing
  position overnight, which is how the cohort's worst loss happened.
* Anything reporting or storing spot in that window is quoting a price up to
  fifteen minutes old as if it were live.

Options DO still trade until 15:30 (that is the 125/125 above), so VALUE-driven
triggers stay legitimate right up to the close. It is only SPOT that dies
early, and only spot-derived inference that has to change.

## The window

Held as config rather than measured per-poll on purpose. An empirical "has this
price stopped moving" test cannot tell a call auction from an illiquid stock
that simply has not traded, and it would be at its least reliable exactly where
it matters — a thin name in the last quarter hour. The auction is market-wide
and starts on a clock, so a clock is the right instrument.

SAFE-TO-RERUN: pure functions of the clock, no state, no I/O.

RETIRES WHEN: the engines take their spot from a source that reports its own
staleness (a `last_trade_time` on the quote), at which point the window is
inferred from the tick rather than declared here.
"""
from __future__ import annotations

import math
from datetime import datetime, time as dtime, timedelta, timezone

#: Both engines define their own IST; this module must not become a third
#: opinion about it, so it accepts an aware datetime and only falls back to its
#: own when the caller has none.
IST = timezone(timedelta(hours=5, minutes=30))

#: Continuous CASH trading ends here and the closing auction begins. Measured
#: at 15:15:02-15:15:32 across every session on record (see the module
#: docstring). NOT the same as the close: the derivatives segment goes on
#: quoting until MARKET_CLOSE.
CASH_AUCTION_START = dtime(15, 15)

#: The auction uncrosses. Kept equal to both engines' MARKET_CLOSE;
#: `assert_agrees_with` pins that rather than trusting it.
CASH_AUCTION_END = dtime(15, 30)

#: When the DERIVATIVES segment actually stops, and the reason this module
#: exists at all rather than a simple "the day is over at 15:30".
#:
#: CAS went live 2026-08-03 (SEBI circular 2026-01-16, NSE SOP 2026-03-18) for
#: Category I — F&O-eligible — names, and F&O continuous trading was EXTENDED
#: from 15:30 to 15:40 at the same time, so option traders can react once the
#: cash uncrossing is known. Corroborated here rather than taken on trust: the
#: option book MOVED between the 15:25 and 15:30 polls on **125 of 125**
#: position-sessions and was identical on 0, with all three of the exits once
#: suspected of being unfillable showing live, repriced two-way books
#: (ADANIGREEN long 48.00/48.80 -> 63.55/65.95).
#:
#: SECONDARY-SOURCED. The go-live date and the 15:40 extension are consistently
#: reported by brokers and the financial press, and match our own feed exactly,
#: but neither circular's raw text has been read. The sub-windows (order entry
#: 15:20-15:25, limit-only 15:25-15:30 with a randomised close, matching
#: 15:30-15:35) are secondary-only and are NOT relied on by any code here.
#:
#: NOT YET ACTED ON: both engines still stop at 15:30, so the fleet is blind to
#: the last ten minutes of a tradeable options market. That is a gap to close
#: deliberately — it moves MARKET_CLOSE, which the EOD sweep window, the digest
#: and both loop exits are all keyed to — not a one-line change.
FNO_CLOSE = dtime(15, 40)


def cash_price_is_frozen(now: datetime = None) -> bool:
    """Is the cash market in its closing auction, so spot CANNOT print?

    True from CASH_AUCTION_START up to (not including) CASH_AUCTION_END. The
    end is exclusive because the uncrossing price arrives around then and the
    session is over anyway — by CASH_AUCTION_END the question the callers are
    asking has become "is anything executable", which is a different one.

    Weekends and holidays are deliberately NOT considered. A caller reaching
    this function is already inside a market-hours loop, and adding a second,
    weaker opinion about whether the market is open is how two calendars come
    to disagree.
    """
    now = now or datetime.now(IST)
    return CASH_AUCTION_START <= now.time() < CASH_AUCTION_END


def spot_staleness_note(now: datetime = None) -> str:
    """One clause for a log line or an alert, or '' outside the window.

    Exists so the two engines cannot describe the same condition in two
    different ways — the reader of a POLL line and the reader of a Telegram
    are usually the same person at different hours.
    """
    if not cash_price_is_frozen(now):
        return ''
    return ('spot STALE: cash market in its closing auction since %02d:%02d, '
            'no new print until the uncrossing' % (CASH_AUCTION_START.hour,
                                                   CASH_AUCTION_START.minute))


def auction_watch(ref, spot, now: datetime = None) -> tuple:
    """Did the cash price MOVE while we believe it cannot? Log-only.

    Returns ``(looks_wrong, patch, compared_to)``. `patch` is a new reference
    to persist, or None when the stored one still stands; `compared_to` is the
    numeric reference the verdict was reached against, for the log line.

    The window above is a declaration, and a declaration nothing checks is the
    shape this codebase keeps paying for (`feedback_stale_scripts_and_docs`:
    junk is not the problem, an unchecked ASSERTION is). If NSE moves the
    auction, or moves it back, every guard keyed to 15:15 silently starts
    answering the wrong question and nothing anywhere looks broken.

    So: inside the window, spot must not change. If it does, the window is
    wrong.

    THE REFERENCE MUST BE TAKEN INSIDE THE WINDOW, and this is the whole
    subtlety. The first cut compared against `corrob_spot`, on the reasoning
    that the corroboration veto stands down at 15:15 so its reference still
    holds the last PRE-auction price. It does — and that is precisely why it is
    the wrong thing to compare against. The market trades normally right up to
    15:15, so the 15:10 price and the frozen 15:15 price differ for entirely
    ordinary reasons. Measured on 2026-09-07: spot differed between the 15:10
    and 15:15 polls on **8 of 8** open positions, so the check fired on all of
    them, three times each, 24 warnings in a session where the window behaved
    perfectly (identical at 15:15/15:20/15:25 on 8 of 8). A detector whose
    false-positive rate is ~100% by construction is worse than none: it teaches
    the reader to skip the line, which is how the OI flag on COCHINSHIP got
    waved through.

    Hence a dedicated reference, stamped with the DAY it was taken. The day
    stamp rules out a CROSS-SESSION stale reference — the previous cut read
    `corrob_spot` with no regard to `corrob_t` at all, so a position whose book
    had been unreliable could be judged against a price from an earlier session
    and report a fabricated move of any size. It does NOT rule out a
    same-session one: see the note on `t` below.

    This is the cheap half of the check. The expensive half — does the freeze
    start EARLIER than 15:15 — needs per-position history, and was measured
    offline on 2026-09-07: consecutive polls were identical on 0-2 of 7-8
    positions everywhere from 14:30 to 15:10 (ordinary noise, including 2 of 8
    at 15:05->15:10) and on 8 of 8 at both 15:15->15:20 and 15:20->15:25. The
    declared start is right; nothing is hiding before it.

    Deliberately NOT a veto and NOT a trigger. It returns a fact for a log
    line; nothing about the day's trading should change because a session-time
    assumption is stale, and a detector that could halt the engine would be a
    worse bug than the one it watches for.
    """
    now = now or datetime.now(IST)
    if not cash_price_is_frozen(now):
        return False, None, None
    try:
        spot = float(spot)
        # `nan <= 0` is False, so a NaN used to sail through and BECOME the
        # reference — and `nan != nan` is True, so it then reported a finding
        # on every remaining cycle of the day. `isfinite` also rejects inf.
        if not math.isfinite(spot) or spot <= 0:
            return False, None, None
    except (TypeError, ValueError):
        return False, None, None
    day = now.date().isoformat()
    stored = None
    if isinstance(ref, dict) and ref.get('day') == day:
        try:
            stored = float(ref['spot'])
            if not math.isfinite(stored):
                stored = None
        except (TypeError, ValueError, KeyError):
            stored = None
    if stored is None:
        # First reading inside today's window: this IS the reference. It is
        # never a finding — there is nothing yet to have moved away from.
        #
        # `t` is stamped because the reference can still, rarely, be a LIVE
        # print: the freeze onset is measured at 15:15:02-15:15:32 and zebra's
        # 15:15 cycle stamps its POLL lines at 15:15:15-15:15:41, so the two
        # ranges overlap. On such a day the 15:20 comparison would differ for
        # the ordinary reason and fire. It has not happened in ~140 measured
        # position-sessions, but without the time on the record a firing could
        # not be told from a real one — so the log line prints it and any
        # future firing diagnoses itself.
        return False, {'spot': spot, 'day': day, 't': now.strftime('%H:%M:%S')}, None
    # The compared value comes BACK rather than the caller re-reading the
    # stored dict: a ref holding `spot: "100"` compares fine through float()
    # and then blew up `%.2f` inside logging, which swallows the error and
    # LOSES the warning entirely.
    return stored != spot, None, stored


def assert_agrees_with(market_close) -> None:
    """Fail loudly if an engine's own close disagrees with the auction end.

    `market_close` is that engine's MARKET_CLOSE, as a `time` or an (h, m)
    tuple. Both engines keep their own copy, and a third copy here that
    silently drifted from them would make this module's answer wrong in the
    one direction nobody checks — reporting a live price as frozen, or worse,
    a frozen one as live.
    """
    if isinstance(market_close, tuple):
        market_close = dtime(*market_close)
    if market_close != CASH_AUCTION_END:
        raise ValueError(
            'MARKET_CLOSE %s disagrees with CASH_AUCTION_END %s — the closing '
            'auction window and the session close have drifted apart; fix both '
            'together or this module reports the wrong thing at the wrong time'
            % (market_close, CASH_AUCTION_END))
