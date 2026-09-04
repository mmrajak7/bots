"""Periodic position review — judgement the trigger-driven gates cannot have.

The entry gate fires once, at entry. The exit gate fires only when a
deterministic trigger already went off. Between them sits everything that
happens to an open position while nothing is triggering: a budget, an election
result, a results date nobody logged at entry, a sector shock. This module is
the standing look at open positions that the other two cannot provide.

The hard boundary
-----------------
**A review can never close a position.** It records a recommendation and, when
that recommendation is not "hold", asks the human. Exiting stays the exclusive
job of the deterministic triggers plus the exit gate, which is the path that
has been reviewed, tested and negative-controlled. Letting a periodic LLM sweep
close positions would create a second, unreviewed exit path — a fresh way to
lose money at market open, which is exactly the failure this whole layer exists
to prevent.

Cost control
------------
Market hours are ~75 cycles/day across N positions. Reviewing everything every
cycle would burn the token budget to conclude "nothing changed" hundreds of
times. A free Python pre-filter picks what deserves a look, and each position is
reviewed at most once per day unless something new fires.

Two pre-filters, two caps, two models
-------------------------------------
The original pre-filter is REACTIVE TO PRICE — a 4% adverse move, a give-back,
an event the calendar already carries, the last cheap window before the time
stop. News that has not moved the tape yet trips none of them, and neither does
an event class the calendar has no type for. #472 ANGELONE lost 65.8% overnight
on a monthly business update: no calendar class for it, no price move by the
close, so the agent that asks exactly the right question was never asked it.

So there is a second, unconditional pre-filter: once the IST clock passes
`EOD_REVIEW_START`, every open position gets one news sweep, on the cheaper
`EOD_REVIEW_MODEL`. The two are capped INDEPENDENTLY — a position reviewed at
09:20 for a 4% adverse move still gets its 15:00 sweep, because "price moved"
and "what happened today" are different questions and satisfying one does not
answer the other. Two reviews per position per day is the ceiling.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import Optional

from common import nse_holidays

from . import config as cfg
from . import events as events_mod
from . import vet as vet_mod

logger = logging.getLogger(__name__)

ACTIONS = ('hold', 'adjust', 'exit')

#: The reason string the daily sweep flags a position with. It reaches the
#: agent verbatim (`review.why` -> `zebra review show`), which is how it knows
#: to run the news sweep rather than a price post-mortem, and VETTING.md keys
#: its "Daily scan" paragraph off exactly this text. One constant, so the
#: routing, the log line and the checklist cannot drift apart.
EOD_REASON = 'daily EOD scan'
#: Marker key holding the ISO date of the last daily sweep. Its OWN key, not
#: `reviewed_at`: sharing one would let a morning price review satisfy the
#: afternoon news sweep, which is the hole this whole feature exists to close.
EOD_SCAN_KEY = 'eod_scanned_on'

#: Marker `kind` -> agent channel. Two channels for one job, and the ONLY
#: difference is the health watchdog's bookkeeping: `record_agent_landed`
#: zeroes `spawns_since_landing` for a whole channel, so ~8 Sonnet sweeps
#: landing every session would clear the alarm for the Opus price reviews too —
#: a price review failing on every single spawn could then never reach
#: `SILENT_SPAWN_LIMIT`. The cheap sweep's success must never be the expensive
#: channel's all-clear. Both are in `cfg.DEFERRABLE_CHANNELS`, so the spawn
#: budget, the reserve and the tool grants are identical.
#:
#: NOT separated: `vet.cli_blocked_until` is a GLOBAL usage-limit marker, so one
#: Opus quota banner suppresses the Sonnet sweep too. That is correct today —
#: the limit is per account, not per model — and is recorded here so nobody
#: reads the channel split as an isolation guarantee it does not provide.
CHANNELS = {'review': 'review', 'scan': 'review_scan'}


def _now() -> datetime:
    """The exchange wall clock, naive.

    IST rather than the host clock because every session-relative rule in this
    fleet is about the exchange: a UTC-clocked box reads 15:00 IST as 09:30 and
    would run the end-of-session sweep at lunchtime. NAIVE because every stamp
    this module writes and re-reads (`reviewed_at`, `attempted_at`, `deadline`)
    is a bare ISO string, and one aware value in that set makes every
    comparison against the others raise TypeError inside a safety sweep. On the
    Pi, whose clock is IST, this is the identity on what it replaced.
    """
    return datetime.now(cfg.IST).replace(tzinfo=None)


def _ist_clock(dt: datetime) -> datetime:
    """`dt` on the exchange wall clock, naive.

    An AWARE value is converted — that is how a test pins a time of day without
    depending on the zone of the box it runs on. A NAIVE one is taken as
    already being the exchange clock, which is what `_now()` produces and what
    every stored marker holds; guessing the host's zone for it would silently
    shift markers on any box that is not IST.
    """
    return dt if dt.tzinfo is None else dt.astimezone(cfg.IST).replace(
        tzinfo=None)


def _marker(trade: dict) -> dict:
    m = trade.get('review') if isinstance(trade.get('review'), dict) else {}
    return m


# Give-back watch: reached this far toward target, then handed back this share
# of the peak progress. Both fractions of the SAME move, so the trigger means
# the same thing on a 3% target and a 12% one.
GIVEBACK_PROGRESS = 0.70
GIVEBACK_RETRACE = 1.0 / 3.0


def _is_standing_market_row(e) -> bool:
    """A market-scope `expiry` / `other` row — real information, wrong trigger.

    These reach EVERY symbol (`events.upcoming` returns symbol-less rows to
    all of them) and they sit inside `EVENT_HORIZON_DAYS` for the whole ten
    days before they land. One row saying "September F&O monthly expiry" or
    "Nifty rebalance" therefore flags the entire book, every day, for ten days
    — ~80 Opus agents to be told about a date the engine already knows, since
    the expiry is in every trade record and the TIME stop is computed from it.

    Worse than the volume: it makes `reasons` non-empty for every position, so
    the 15:00 sweep is never scan-ONLY and routes to the expensive model for
    those ten days. The cheap sweep quietly stops being cheap.

    They are still shown to the agent — `run()` puts the unfiltered
    `upcoming()` in `context`, and `zebra review show` prints it — so nothing
    is hidden; this only stops a standing market-wide date from being the
    REASON a review starts. Per-stock rows (a symbol-ed OFS, a results date)
    and the dated market decisions (`budget`, `election`, `rbi_policy`) are
    untouched: those genuinely change the distribution and are why the event
    trigger exists.
    """
    return (isinstance(e, dict)
            and e.get('type') in events_mod.EITHER_SCOPE_TYPES
            and events_mod.is_market_scope(e))


def needs_review(trade: dict, spot: float, evts: Optional[list] = None,
                 now: Optional[datetime] = None) -> tuple:
    """(needed, why). Deterministic, free, and deliberately narrow.

    The `(needed, why)` shape is the contract several callers and tests hold;
    `review_reasons` below is the same computation with the reasons still
    separated, for the one caller that has to ROUTE on them.
    """
    reasons = review_reasons(trade, spot, evts=evts, now=now)
    return bool(reasons), '; '.join(reasons)


def review_reasons(trade: dict, spot: float, evts: Optional[list] = None,
                   now: Optional[datetime] = None) -> list:
    """Every reason this position is due a review right now; `[]` if none.

    Split out of `needs_review` so `run()` can pick the MODEL off the reason
    SET rather than parsing the joined string back apart. That string is what a
    human reads in a log and what the agent is shown as `flagged_because`;
    making it load-bearing would mean a wording change silently re-routes a
    spawn to the wrong model.
    """
    # ONE clock for the whole function. Every marker stamp is a naive ISO
    # string on the exchange clock, so an aware `now` — which is how a caller
    # states a time of day unambiguously — would raise "can't compare
    # offset-naive and offset-aware" inside the daily cap, and `run()` catches
    # that per position: the sweep would go quiet rather than fail.
    now = _ist_clock(now or _now())
    reasons = []

    evts = events_mod.upcoming(trade.get('stock')) if evts is None else evts
    for e in evts:
        if _is_standing_market_row(e):
            continue
        reasons.append('%s in %dd (%s)' % (e['type'], e['days_away'],
                                           e['title'][:60]))

    entry = float(trade.get('entry_spot') or trade.get('signal_price') or 0)
    if entry > 0 and spot > 0:
        move = (spot - entry) / entry
        adverse = -move if trade.get('direction') == 'CE' else move
        if adverse >= cfg.REVIEW_ADVERSE_PCT:
            reasons.append('%.1f%% adverse from entry' % (adverse * 100))

        # ── Give-back watch ──────────────────────────────────────────────
        # The pre-filter above only ever fired on ADVERSE conditions, so a
        # position that ran most of the way to target and then handed it all
        # back never tripped it — the single most expensive pattern in the
        # book (10 of 116 closed trades, -Rs 273,446, about twice what the
        # whole book made). Retracing a big gain is not an adverse move from
        # ENTRY; it can happen entirely in profit, which is exactly why
        # nothing was watching.
        tp = float(trade.get('tp_spot') or 0)
        peak = trade.get('mfe_spot')
        if tp and peak is not None and tp != entry:
            span = tp - entry
            peak_prog = (float(peak) - entry) / span
            now_prog = (spot - entry) / span
            if peak_prog >= GIVEBACK_PROGRESS and \
                    (peak_prog - now_prog) >= peak_prog * GIVEBACK_RETRACE:
                reasons.append(
                    'gave back %.0f%% of a move that reached %.0f%% to target'
                    % ((peak_prog - now_prog) / peak_prog * 100,
                       peak_prog * 100))

        # NOTE — no separate "absurd adverse move" trigger. It was written and
        # removed the same hour: any move large enough to qualify has already
        # tripped the REVIEW_ADVERSE_PCT check three lines up, so it added a
        # second reason string and zero new coverage while doubling nothing but
        # the agent spawns. The version that WOULD add something is velocity —
        # this much in ONE session, versus the same drift over three weeks —
        # and that needs the previous close, which this loop does not carry.
        # Left undone deliberately rather than shipped as a redundant threshold.

    try:
        dte = (datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
               - now.date()).days
        # Inside the TIME-SL window the position is already being nagged daily;
        # a review there would duplicate that. Just outside it is where a
        # rolling/closing decision is still cheap and nobody is looking.
        if cfg.TIME_SL_DAYS < dte <= cfg.TIME_SL_DAYS + 3:
            reasons.append('%dd to expiry — last cheap window to adjust' % dte)
    except (KeyError, TypeError, ValueError):
        pass

    m = _marker(trade)

    # At most one PRICE/EVENT-triggered review per position per day.
    # `attempted_at` is stamped when the agent is SPAWNED, not when it reports —
    # an agent that dies (expired auth, crash, hung research) never calls
    # record(), so capping on completion would respawn it every time the
    # deadline lapsed. The reasons above are standing conditions (an event stays
    # inside the horizon for days; an adverse move persists), so that is ~35
    # spawns/day/position against a broken CLI, each with a trade-store write
    # and a Drive upload.
    #
    # This cap governs THESE reasons only. The daily sweep below has its own,
    # because a morning review of a price move does not answer "what came out
    # on the wires today".
    if reasons:
        for key in ('reviewed_at', 'attempted_at'):
            last = vet_mod._parse(m.get(key))
            if last and (now - last) < timedelta(days=1):
                reasons = []
                break

    if _eod_scan_due(trade, now):
        reasons.append(EOD_REASON)

    if not reasons:
        return []
    # An agent is already working on this position. Applies to BOTH kinds: a
    # second detached process asking the same store the same question is waste
    # at best, and at worst two verdicts racing for one `pending` marker.
    if m.get('state') == 'pending':
        deadline = vet_mod._parse(m.get('deadline'))
        if deadline and now < deadline:
            return []
    return reasons


#: `sweep_window` codes. `early` is the only one that is not worth a log line:
#: it is true on every cycle from the open to `EOD_REVIEW_START`, ~70 a day.
WINDOW_OPEN = 'open'
WINDOW_DISABLED = 'disabled'
WINDOW_NO_SESSION = 'no_session'
WINDOW_EARLY = 'early'
WINDOW_CLOSED = 'closed'


def sweep_window(ist: datetime) -> tuple:
    """(code, explanation) — may the daily sweep REQUEST a scan right now?

    One decision, computed once per cycle rather than per position, so the
    reason a whole book was skipped can be logged once instead of eight times
    or (as it was) not at all.
    """
    if not cfg.EOD_REVIEW_ENABLED:
        return WINDOW_DISABLED, 'the daily sweep is disabled by config'
    if not nse_holidays.is_session(ist.date()):
        # Not dead code and not merely cosmetic: `zebra loop` and a manual run
        # are not Mon-Fri-only, and a holiday that the calendar does NOT know
        # about reads as a session — so say which calendar answered.
        cov = nse_holidays.coverage_status(ist.date())
        state = (cov or {}).get('state')
        note = '' if state == 'ok' else \
            ' (holiday calendar is %s — the session test is degraded)' % state
        return WINDOW_NO_SESSION, ('%s is not a trading session%s'
                                   % (ist.date().isoformat(), note))
    if (ist.hour, ist.minute) < cfg.EOD_REVIEW_START:
        return WINDOW_EARLY, 'before %02d:%02d IST' % cfg.EOD_REVIEW_START
    if (ist.hour, ist.minute) > cfg.EOD_REVIEW_LAST_REQUEST:
        # NOT "the sweep is over" — the sweep may still be DELIVERING. This
        # bound is on starting new work whose answer could not be delivered
        # before the last cycle of the session.
        return WINDOW_CLOSED, ('the request window closed at %02d:%02d IST'
                               % cfg.EOD_REVIEW_LAST_REQUEST)
    return WINDOW_OPEN, ''


def _eod_scan_due(trade: dict, ist: datetime) -> bool:
    """Is this position owed today's end-of-session news sweep?

    `ist` is the exchange wall clock.
    """
    if sweep_window(ist)[0] != WINDOW_OPEN:
        return False
    m = _marker(trade)
    # NEVER while an agent is still in flight, even one whose own deadline has
    # lapsed. `VET_TIMEOUT_SEC` is when WE stop waiting, not when the child
    # dies, and a slow Opus price review that lands a minute late would find
    # its marker replaced by a scan: `record()` then applies the Opus verdict
    # to the SCAN's marker (so `reviewed_at` is never stamped and the price cap
    # is not spent), and the scan's own verdict is discarded five minutes later
    # as "no pending review". Two agents, one marker, both answers wrong.
    #
    # Bounded by the kill deadline so a marker left pending by a dead agent
    # cannot silence the sweep forever: past `deadline + CHILD_KILL_SEC` the
    # child has been SIGKILLed and no verdict can arrive. Same shape as
    # `request()`'s own overwrite rule, one grace period wider.
    if m.get('state') == 'pending':
        deadline = vet_mod._parse(m.get('deadline'))
        if deadline and ist < deadline + timedelta(seconds=cfg.CHILD_KILL_SEC):
            return False
        # Past the bound the marker is ABANDONED, not in flight. Said out loud
        # because the alternative — taking it silently — is how a review
        # channel that never reports back looks exactly like a healthy one.
        logger.warning('REVIEW marker on #%s is still PENDING past its kill '
                       'deadline — the agent never reported. The daily sweep '
                       'is taking the marker.', trade.get('id'))
    return m.get(EOD_SCAN_KEY) != ist.date().isoformat()


#: Delivery attempts before an undelivered recommendation stops BLOCKING new
#: reviews. Three failed sends across three sweeps is a Telegram that is not
#: coming back this session — a missing `config/telegram.json`, or an HTML
#: escape the API rejects with a 400 every single time.
MAX_ALERT_ATTEMPTS = 3
#: ...or this long since the verdict landed, whichever comes first. One hour is
#: about twelve cycles: long past "the network blipped", and still inside the
#: session that produced the verdict.
UNDELIVERED_HOLD_SEC = 3600


def _undelivered_verdict(m: dict) -> Optional[dict]:
    """The marker's own non-`hold` verdict, if it has not been sent yet.

    `request()` REPLACES the marker wholesale, so writing over one of these
    destroys the recommendation before the human ever sees it. The window is
    real and routine: `_claim_alert` releases the flag when a Telegram send
    fails so the message retries next sweep, and the daily scan now guarantees
    a fresh request at 15:00 on every open position — so a morning `exit`
    recommendation whose send failed at 09:25 was silently overwritten at 15:00
    with `action: None, why: 'daily EOD scan'`. The only trace was one ERROR
    line about a failed send.

    `hold` is excluded because it is never delivered and there is nothing to
    lose; `alerted` True means the human already has it.
    """
    if (m.get('state') == 'done'
            and m.get('action') in ('adjust', 'exit')
            and not m.get('alerted')):
        return m
    return None


def _holds_an_undelivered_recommendation(m: dict,
                                         now: Optional[datetime] = None
                                         ) -> bool:
    """Should an undelivered verdict still BLOCK a fresh request?

    BOUNDED, because the first cut of this guard was not. With `send()`
    failing permanently — no `config/telegram.json`, or a 400 the message will
    never stop earning — the marker froze at `done/exit/alerted:False` and
    every subsequent request was refused FOR THE LIFE OF THE TRADE. Price
    reviews included: a protection against losing one recommendation had
    turned into a permanent outage of the layer that produces them, on the one
    position already flagged as needing an exit.

    So it blocks only while delivery is still plausibly in progress. Past
    either bound the request proceeds and the verdict is STASHED under `prev`,
    where the delivery pass keeps retrying it — preserved, not preferred.
    """
    if _undelivered_verdict(m) is None:
        return False
    if int(m.get('alert_attempts') or 0) >= MAX_ALERT_ATTEMPTS:
        return False
    landed = vet_mod._parse(m.get('landed_at'))
    # Normalised, like every other clock comparison here: the stamps are
    # naive exchange-clock ISO strings and a caller may hand in an aware
    # instant.
    now = _ist_clock(now) if now is not None else _now()
    if landed and (now - landed).total_seconds() >= UNDELIVERED_HOLD_SEC:
        return False
    return True


def _stash(m: dict) -> Optional[dict]:
    """The undelivered verdict, reduced to what re-sending it needs."""
    v = _undelivered_verdict(m)
    if v is None:
        return None
    return {'action': v.get('action'), 'reasons': list(v.get('reasons') or []),
            'decision_id': v.get('decision_id'),
            'reviewed_at': v.get('reviewed_at'),
            'landed_at': v.get('landed_at'),
            'alerted': False,
            'alert_attempts': int(v.get('alert_attempts') or 0)}


def request(store, trade_id: int, why: str, context: dict,
            spawn: bool = True, model: Optional[str] = None,
            eod_day: Optional[str] = None, kind: str = 'review',
            outcome: Optional[dict] = None) -> bool:
    """Mark a position under review and spawn the agent. True if spawned.

    `model` None means `cfg.VET_MODEL` — the behaviour every existing caller
    has, spelled as a default rather than duplicated at each call site.

    `eod_day` is the ISO date to stamp into `eod_scanned_on`, and is set ONLY
    when this request carries the daily sweep. Stamping it here, before the
    spawn, is the same discipline as `attempted_at` and for the same reason: an
    agent that dies never calls `record()`, and a cap keyed on completion would
    respawn it every cycle until the close. It is released the same way too,
    when the spawn never happened at all.

    `kind` says WHICH cap this request spends, and 'scan' — the sweep with
    nothing else to say — spends only its own. That is not tidiness: the price
    cap is a rolling 24 HOURS, so a sweep stamping `attempted_at` at 15:00
    every session would suppress every price-triggered review from 15:00 to
    15:00, permanently. The independence of the two caps is the feature.
    `outcome` is an optional dict the caller owns; this fills in
    `{'result': 'spawned' | 'refused' | 'deferred' | 'in_flight'}`. The boolean
    return says only "did an agent start", and the sweep's per-cycle budget
    needs to tell a REFUSAL (the shared agent pool is full, so no other
    scan-only position can succeed this cycle either) from a per-position
    deferral. Reading it out of a dict keeps the bool contract every existing
    caller and test holds.
    """
    if kind not in ('review', 'scan'):
        raise ValueError("kind must be 'review' or 'scan', not %r" % (kind,))
    outcome = outcome if outcome is not None else {}
    scan_only = kind == 'scan'
    fresh = True
    undelivered = False
    stashed = None
    with store._mutate():
        t = store._must_find(trade_id)
        m = _marker(t)
        deadline = vet_mod._parse(m.get('deadline'))
        if m.get('state') == 'pending' and deadline and _now() < deadline:
            fresh = False
            outcome['result'] = 'in_flight'
        elif _holds_an_undelivered_recommendation(m):
            fresh = False
            undelivered = True
            outcome['result'] = 'deferred'
        else:
            # PRESERVE, then proceed. Past the delivery bound the request goes
            # ahead, but the verdict the human never saw is kept under `prev`
            # where the delivery pass keeps retrying it. Losing it here would
            # be the R1 defect back, arriving an hour later.
            _prev = m.get('prev')
            stashed = _stash(m) or (
                _prev if isinstance(_prev, dict) and not _prev.get('alerted')
                else None)
            t['review'] = {
                'state': 'pending',
                # Which question the agent was asked. `record()` reads it to
                # decide whether the landing spends the price cap.
                'kind': kind,
                'requested_at': _now().isoformat(),
                'deadline': (_now() + timedelta(
                    seconds=cfg.VET_TIMEOUT_SEC)).isoformat(),
                'why': why,
                'context': context,
                'action': None,
                # Both survive the wholesale overwrite on purpose: they are the
                # daily caps, and dropping them would re-arm the sweep instantly.
                'reviewed_at': m.get('reviewed_at'),
                'attempted_at': (m.get('attempted_at') if scan_only
                                 else _now().isoformat()),
                # Carried forward on the same principle as `reviewed_at`: this
                # dict REPLACES the marker wholesale, so an un-carried key
                # re-arms its cap instantly — here that would mean a price
                # review at 15:05 handing the position a second news sweep at
                # 15:10, and every cycle after it.
                EOD_SCAN_KEY: eod_day or m.get(EOD_SCAN_KEY),
                # The newest recommendation that has not reached the human.
                # Dropped once delivered, so it is not a growing history.
                'prev': stashed,
            }
            t['version'] = t.get('version', 0) + 1
    if stashed and _stash(m):
        logger.error('REVIEW #%d: the %s recommendation from %s was NEVER '
                     'DELIVERED after %d attempt(s). It is preserved under '
                     'review.prev and delivery keeps retrying; the position '
                     'is being re-reviewed rather than left locked.',
                     trade_id, stashed.get('action'), stashed.get('landed_at'),
                     int(stashed.get('alert_attempts') or 0))
    if undelivered:
        # Deferred one cycle, not dropped: the delivery pass at the end of this
        # very sweep sends the recommendation and sets `alerted`, so the next
        # cycle proceeds. Nothing was stamped here, so nothing needs releasing.
        logger.info('REVIEW #%d deferred — an undelivered %s recommendation '
                    'is still on the marker', trade_id, m.get('action'))
        return False
    if not fresh:
        return False
    outcome['result'] = 'spawned'
    logger.info('REVIEW REQUESTED #%d — %s', trade_id, why)
    if spawn:
        import sys
        prompt = cfg.REVIEW_PROMPT_TEMPLATE.format(
            trade_id=trade_id, python=sys.executable,
            vetting_doc=cfg.VETTING_DOC)
        if vet_mod._spawn_generic(prompt, model or cfg.VET_MODEL,
                                  'review #%d' % trade_id,
                                  channel=CHANNELS[kind]) is None:
            # GIVE THE DAY'S SLOT BACK. `attempted_at` is stamped above, before
            # the spawn, so a crashing agent cannot re-trigger every cycle —
            # right for a crash, wrong for a spawn that never happened. With
            # `review` capped at MAX_CONCURRENT_AGENTS - AGENT_RESERVE, a
            # 24-position sweep starts 3 and refuses 21, and each refusal was
            # locking that position out for 24 hours. Tomorrow the same
            # arithmetic repeats, so most positions were never reviewed at all
            # while the log claimed 24 requests a day.
            #
            # Release the SWEEP's slot with it. `eod_day` is only ever passed
            # when this request stamped it fresh (a position already scanned
            # today never reaches here), so this cannot erase an earlier
            # sweep's stamp.
            outcome['result'] = 'refused'
            _release_request(store, trade_id, eod=bool(eod_day),
                             attempt=not scan_only)
            logger.info('REVIEW #%d not started (no agent slot) — request '
                        'withdrawn, retried next cycle', trade_id)
            return False
    return True


def _release_request(store, trade_id: int, eod: bool = False,
                     attempt: bool = True) -> None:
    """Withdraw the whole request when the spawn never happened.

    The daily caps AND the in-flight marker. Releasing only the caps left
    `state: 'pending'` with a live `deadline` on a position no agent was ever
    started for, so the next cycle's `review_reasons` returned `[]` and the
    position was locked out for `VET_TIMEOUT_SEC` — which is the opposite of
    what the log line said. It matters most in exactly the case that produces
    refusals: a quota-blocked CLI refuses every spawn, so the WHOLE book locks
    at once, and the sweep's request window is only fifteen minutes wide.

    Cap keys are released only if THIS request wrote them: a sweep-only request
    left `attempted_at` alone, so clearing it would hand back a slot belonging
    to a price review that really did run this morning.
    """
    try:
        with store._mutate():
            t = store.find(trade_id)
            m = (t or {}).get('review') if t else None
            if not isinstance(m, dict) or m.get('state') != 'pending':
                return
            m['state'] = None
            m['deadline'] = None
            m['why'] = None
            if attempt:
                m['attempted_at'] = None
            if eod:
                m[EOD_SCAN_KEY] = None
            t['version'] = t.get('version', 0) + 1
    except Exception as e:                       # pragma: no cover - paranoia
        logger.warning('Could not withdraw the review request for #%d: %s',
                       trade_id, e)


def record(store, trade_id: int, action: str, reasons=None,
           decision_id: Optional[int] = None) -> str:
    """Land a review recommendation. Never mutates the position itself."""
    if action not in ACTIONS:
        raise ValueError('action must be one of %s' % (ACTIONS,))
    applied = False
    with store._mutate():
        t = store._must_find(trade_id)
        m = _marker(t)
        if m.get('state') == 'pending':
            landed = {'state': 'done', 'action': action,
                      'reasons': list(reasons or []),
                      'decision_id': decision_id}
            # The OTHER half of the cap split, and the half that is easy to
            # miss: `reviewed_at` is a price-cap key too, so a sweep landing at
            # 15:02 would stamp it and suppress price-triggered reviews for the
            # next 24 hours — every session, forever. A sweep's record of
            # itself is `eod_scanned_on`, written at request time.
            if m.get('kind') != 'scan':
                landed['reviewed_at'] = _now().isoformat()
            # ALWAYS stamped, on both kinds. `reviewed_at` is a price-cap key
            # and a scan must not spend it, so it cannot double as "when did
            # this verdict arrive" — which is what bounds how long an
            # undelivered recommendation may block a fresh review.
            landed['landed_at'] = _now().isoformat()
            landed['alert_attempts'] = 0
            m.update(landed)
            t['review'] = m
            t['version'] = t.get('version', 0) + 1
            applied = True
    if not applied:
        logger.warning('REVIEW result for #%d discarded — no pending review',
                       trade_id)
        return 'discarded (no pending review)'
    logger.info('REVIEW #%d -> %s', trade_id, action)
    return 'applied'


def format_alert(trade: dict, action: str, reasons: list) -> str:
    """Recommendation for the human. Explicit that nothing has been done."""
    icon = '🟠' if action == 'exit' else '🔧'
    body = '\n'.join('• %s' % html.escape(str(r)[:200]) for r in reasons[:4])
    return (
        f"{icon} <b>POSITION REVIEW — {html.escape(action.upper())}</b>  "
        f"<code>{html.escape(str(trade.get('stock')))}</code> "
        f"({html.escape(str(trade.get('direction')))})\n"
        f"entry {trade.get('entry_spot')} | debit {trade.get('debit')} | "
        f"expiry {html.escape(str(trade.get('expiry')))}\n"
        f"{body}\n"
        f"<i>NOTHING HAS BEEN CLOSED. Review only — the automated exits are "
        f"unchanged. Act with <code>zebra close {trade.get('id')}</code> "
        f"if you agree.</i>"
    )


def unscanned_today(store, ist: datetime) -> list:
    """Open positions the daily sweep OWED a look today and did not give one.

    A position ENTERED after the request window closed is not owed one: it was
    not open when the sweep ran, so reporting it as "never scanned today"
    turns the incomplete-sweep alarm into a false one that fires on every
    cycle to the close, on a healthy box, for a trade that entered ten minutes
    ago. It gets its first sweep tomorrow like any other position.
    """
    day = ist.date().isoformat()
    out = []
    for t in store.get_entered():
        if _marker(t).get(EOD_SCAN_KEY) == day:
            continue
        if _entered_after_the_window(t, ist):
            continue
        out.append(t['id'])
    return out


def _entered_after_the_window(trade: dict, ist: datetime) -> bool:
    """Did this position enter TODAY, past `EOD_REVIEW_LAST_REQUEST`?

    Unknown or unparseable timing answers NO — an unreadable stamp must not be
    able to hide a position from the incomplete-sweep alarm, which is the only
    thing that reports the sweep failing.
    """
    if trade.get('entry_date') != ist.date().isoformat():
        return False
    hm = cfg._parse_hhmm(str(trade.get('entry_time') or '')[:5])
    return hm is not None and hm > cfg.EOD_REVIEW_LAST_REQUEST


def _log_window_state(store, now: datetime) -> None:
    """Say once per cycle why the sweep is not requesting, when that is news.

    `early` is deliberately silent — it is true on ~70 cycles a day and a line
    for it would bury the two that matter. `disabled` is silent too: the
    startup banner already says so, on every process, once.

    A CLOSED window with positions still unscanned is the one that has to be
    loud. That state means the day's sweep did not finish, and the only other
    evidence of it is an absence — no `EOD SCAN requested` lines — which is
    indistinguishable from a quiet, fully-swept book.
    """
    code, why = sweep_window(now)
    if code == WINDOW_NO_SESSION:
        logger.info('EOD SCAN skipped: %s', why)
        return
    if code != WINDOW_CLOSED:
        return
    try:
        left = unscanned_today(store, now)
    except Exception as e:                       # pragma: no cover - paranoia
        logger.warning('EOD SCAN: %s; could not count what is unscanned: %s',
                       why, e)
        return
    if left:
        logger.warning('EOD SCAN INCOMPLETE: %s and %d open position(s) were '
                       'never scanned today (%s). They get no news check '
                       'before the close.', why, len(left),
                       ', '.join('#%s' % i for i in left))


def run(store, ltps: dict, send=None, dry_run: bool = False,
        spawn: bool = True, now: Optional[datetime] = None) -> list:
    """One review sweep over open positions. Returns the ids reviewed.

    `send` is injected so the monitor owns Telegram and this module stays
    testable without patching a network call. `now` is injected for the same
    reason: the daily sweep is a time-of-day rule, and a test of it must not
    depend on the hour the suite happens to run at.
    """
    if not cfg.VET_ENABLED:
        return []
    now = _ist_clock(now or _now())
    today = now.date().isoformat()
    requested = []
    # Two counters, deliberately. `scan_budget` is what the stagger spends and
    # counts SCAN-ONLY spawns; `scan_spawns` is what the summary reports and
    # counts every spawn that carried the sweep, so it agrees with the
    # per-spawn lines above it in the log.
    scan_budget = scan_spawns = scan_deferred = 0

    # WHY WE COLLECT BEFORE SPAWNING: `get_entered()` is in id order, the two
    # kinds share one agent pool, and the pool is small. Three quiet low-id
    # scans took every deferrable slot and the 6% adverse move on a higher id
    # was refused in the same cycle — the cheap sweep outbidding the expensive
    # judgement, with no alarm anywhere because a refusal is (correctly) not a
    # failure. Price-triggered reviews go FIRST; the per-cycle scan cap is the
    # other half of the fix and lives in config.
    due = []
    for trade in list(store.get_entered()):
        try:
            reasons = review_reasons(trade, float(ltps.get(trade['stock'], 0)
                                                  or 0), now=now)
            if reasons:
                due.append((reasons == [EOD_REASON], trade, reasons))
        except Exception as e:
            logger.error('Review pre-filter failed for #%s: %s',
                         trade.get('id'), e)
    due.sort(key=lambda row: row[0])          # False (price) sorts before True

    for scan_only, trade, reasons in due:
        spot = ltps.get(trade['stock'], 0)
        try:
            scanning = EOD_REASON in reasons
            if scan_only and scan_budget >= cfg.EOD_REVIEW_MAX_PER_CYCLE:
                # Deferred, not dropped: nothing is stamped for this position,
                # so it re-qualifies unchanged on the next 5-minute cycle.
                scan_deferred += 1
                continue
            model = cfg.EOD_REVIEW_MODEL if scan_only else cfg.VET_MODEL
            out = {}
            ok = request(store, trade['id'], '; '.join(reasons), {
                'spot': spot,
                'entry_spot': trade.get('entry_spot'),
                'debit': trade.get('debit'),
                'expiry': trade.get('expiry'),
                'events': events_mod.upcoming(trade.get('stock')),
            }, spawn=spawn, model=model,
                eod_day=today if scanning else None,
                kind='scan' if scan_only else 'review', outcome=out)
            if ok:
                requested.append(trade['id'])
                if scanning:
                    scan_spawns += 1
                    logger.info('EOD SCAN requested #%d %s model=%s',
                                trade['id'], trade.get('stock'), model)
            # A REFUSAL SPENDS THE BUDGET TOO. It means the shared agent pool
            # is full, so no other scan-only position can succeed this cycle
            # either — and the budget counted successes, so a quota-blocked box
            # walked the WHOLE book: 8 request() calls and 16 store mutations
            # (a local write plus a Drive upload each), every cycle until
            # 15:15, to start nothing. Counting attempts bounds a refused cycle
            # at `cap` of them. A per-position `deferred` is NOT counted: that
            # says nothing about the pool.
            if scan_only and (ok or out.get('result') == 'refused'):
                scan_budget += 1
        except Exception as e:
            # One position's review must never stall the sweep, and a review
            # failure must never touch trading.
            logger.error('Review sweep failed for #%s: %s', trade.get('id'), e)
    if scan_spawns or scan_deferred:
        # Only when the sweep actually did something. A 0/0 line every five
        # minutes for the last half-hour of every session is noise, and noise
        # is what trains a reader to skim the line that matters.
        logger.info('EOD SCAN: %d requested, %d deferred to next cycle',
                    scan_spawns, scan_deferred)
    _log_window_state(store, now)
    # Deliver any recommendation that landed since the last sweep.
    for trade in list(store.get_entered()):
        try:
            target = _undelivered_target(trade)
            if target is None or not send:
                continue
            where, action, reasons = target
            # CLAIM the flag before sending, atomically. Sending first lets an
            # overlapping cron and `zebra loop` both see it un-alerted and both
            # send (flock covers the store, not the cycle). Same discipline as
            # the exit escalation; released below if the send fails, so a
            # Telegram outage retries rather than losing the message.
            if not _claim_alert(store, trade['id'], where=where):
                continue
            if not send(format_alert(trade, action, reasons), dry_run=dry_run):
                attempts = _claim_alert(store, trade['id'], value=False,
                                        where=where, count=True)
                logger.error('REVIEW alert FAILED to send for #%d — flag '
                             'released, retrying next sweep (attempt %s of '
                             '%d before it stops blocking new reviews)',
                             trade['id'], attempts, MAX_ALERT_ATTEMPTS)
        except Exception as e:
            # One undeliverable recommendation must not stop the others.
            logger.error('Review delivery failed for #%s: %s',
                         trade.get('id'), e)
    return requested


def _undelivered_target(trade: dict):
    """(where, action, reasons) for the newest unsent non-`hold` verdict.

    Two places can hold one: the marker itself, and `prev` — where a verdict
    that outlived its delivery bound was STASHED so a fresh review could
    proceed without destroying it. The marker's own verdict is the newer of
    the two and wins; `prev` is what keeps an old one from being lost
    silently, which is the whole point of stashing it rather than dropping it.
    """
    m = _marker(trade)
    if _undelivered_verdict(m) is not None:
        return 'marker', m.get('action'), list(m.get('reasons') or [])
    prev = m.get('prev')
    if isinstance(prev, dict) and _undelivered_verdict(
            dict(prev, state='done')) is not None:
        return 'prev', prev.get('action'), list(prev.get('reasons') or [])
    return None


def _claim_alert(store, trade_id: int, value: bool = True,
                 where: str = 'marker', count: bool = False):
    """Test-and-set the `alerted` flag on the marker or on its `prev` stash.

    Returns the claim result when setting, and the running attempt count when
    releasing with `count` — the caller logs it, and
    `_holds_an_undelivered_recommendation` reads it to decide when an
    undelivered verdict stops blocking new reviews.
    """
    result = False
    with store._mutate():
        t = store.find(trade_id)
        m = t.get('review') if t and isinstance(t.get('review'), dict) else None
        if where == 'prev':
            m = m.get('prev') if isinstance(m, dict) else None
            m = m if isinstance(m, dict) else None
        if m is not None and bool(m.get('alerted')) != value:
            m['alerted'] = value
            if count:
                m['alert_attempts'] = int(m.get('alert_attempts') or 0) + 1
            t['version'] = t.get('version', 0) + 1
            result = int(m.get('alert_attempts') or 0) if count else True
        elif m is not None and count:
            result = int(m.get('alert_attempts') or 0)
    return result
