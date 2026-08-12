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
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta
from typing import Optional

from . import config as cfg
from . import events as events_mod
from . import vet as vet_mod

logger = logging.getLogger(__name__)

ACTIONS = ('hold', 'adjust', 'exit')


def _now() -> datetime:
    return datetime.now()


def _marker(trade: dict) -> dict:
    m = trade.get('review') if isinstance(trade.get('review'), dict) else {}
    return m


# Give-back watch: reached this far toward target, then handed back this share
# of the peak progress. Both fractions of the SAME move, so the trigger means
# the same thing on a 3% target and a 12% one.
GIVEBACK_PROGRESS = 0.70
GIVEBACK_RETRACE = 1.0 / 3.0


def needs_review(trade: dict, spot: float, evts: Optional[list] = None,
                 now: Optional[datetime] = None) -> tuple:
    """(needed, why). Deterministic, free, and deliberately narrow."""
    now = now or _now()
    reasons = []

    evts = events_mod.upcoming(trade.get('stock')) if evts is None else evts
    for e in evts:
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

    if not reasons:
        return False, ''

    # At most one review per position per day. `attempted_at` is stamped when
    # the agent is SPAWNED, not when it reports — an agent that dies (expired
    # auth, crash, hung research) never calls record(), so capping on
    # completion would respawn it every time the deadline lapsed. The review
    # reasons here are standing conditions (an event stays inside the horizon
    # for days; an adverse move persists), so that is ~35 spawns/day/position
    # against a broken CLI, each with a trade-store write and a Drive upload.
    m = _marker(trade)
    for key in ('reviewed_at', 'attempted_at'):
        last = vet_mod._parse(m.get(key))
        if last and (now - last) < timedelta(days=1):
            return False, ''
    if m.get('state') == 'pending':
        deadline = vet_mod._parse(m.get('deadline'))
        if deadline and now < deadline:
            return False, ''
    return True, '; '.join(reasons)


def request(store, trade_id: int, why: str, context: dict,
            spawn: bool = True) -> bool:
    """Mark a position under review and spawn the agent. True if spawned."""
    fresh = True
    with store._mutate():
        t = store._must_find(trade_id)
        m = _marker(t)
        deadline = vet_mod._parse(m.get('deadline'))
        if m.get('state') == 'pending' and deadline and _now() < deadline:
            fresh = False
        else:
            t['review'] = {
                'state': 'pending',
                'requested_at': _now().isoformat(),
                'deadline': (_now() + timedelta(
                    seconds=cfg.VET_TIMEOUT_SEC)).isoformat(),
                'why': why,
                'context': context,
                'action': None,
                # Both survive the wholesale overwrite on purpose: they are the
                # daily caps, and dropping them would re-arm the sweep instantly.
                'reviewed_at': m.get('reviewed_at'),
                'attempted_at': _now().isoformat(),
            }
            t['version'] = t.get('version', 0) + 1
    if not fresh:
        return False
    logger.info('REVIEW REQUESTED #%d — %s', trade_id, why)
    if spawn:
        import sys
        prompt = cfg.REVIEW_PROMPT_TEMPLATE.format(
            trade_id=trade_id, python=sys.executable,
            vetting_doc=cfg.VETTING_DOC)
        vet_mod._spawn_generic(prompt, cfg.VET_MODEL, 'review #%d' % trade_id,
                               channel='review')
    return True


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
            m.update({'state': 'done', 'action': action,
                      'reasons': list(reasons or []),
                      'decision_id': decision_id,
                      'reviewed_at': _now().isoformat()})
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


def run(store, ltps: dict, send=None, dry_run: bool = False,
        spawn: bool = True) -> list:
    """One review sweep over open positions. Returns the ids reviewed.

    `send` is injected so the monitor owns Telegram and this module stays
    testable without patching a network call.
    """
    if not cfg.VET_ENABLED:
        return []
    requested = []
    for trade in list(store.get_entered()):
        spot = ltps.get(trade['stock'], 0)
        try:
            needed, why = needs_review(trade, float(spot or 0))
            if not needed:
                continue
            ok = request(store, trade['id'], why, {
                'spot': spot,
                'entry_spot': trade.get('entry_spot'),
                'debit': trade.get('debit'),
                'expiry': trade.get('expiry'),
                'events': events_mod.upcoming(trade.get('stock')),
            }, spawn=spawn)
            if ok:
                requested.append(trade['id'])
        except Exception as e:
            # One position's review must never stall the sweep, and a review
            # failure must never touch trading.
            logger.error('Review sweep failed for #%s: %s', trade.get('id'), e)
    # Deliver any recommendation that landed since the last sweep.
    for trade in list(store.get_entered()):
        try:
            m = _marker(trade)
            if m.get('state') != 'done' or m.get('alerted'):
                continue
            action = m.get('action')
            if not action or action == 'hold' or not send:
                continue
            # CLAIM the flag before sending, atomically. Sending first lets an
            # overlapping cron and `zebra loop` both see it un-alerted and both
            # send (flock covers the store, not the cycle). Same discipline as
            # the exit escalation; released below if the send fails, so a
            # Telegram outage retries rather than losing the message.
            if not _claim_alert(store, trade['id']):
                continue
            if not send(format_alert(trade, action, m.get('reasons') or []),
                        dry_run=dry_run):
                _claim_alert(store, trade['id'], value=False)
                logger.error('REVIEW alert FAILED to send for #%d — flag '
                             'released, retrying next sweep', trade['id'])
        except Exception as e:
            # One undeliverable recommendation must not stop the others.
            logger.error('Review delivery failed for #%s: %s',
                         trade.get('id'), e)
    return requested


def _claim_alert(store, trade_id: int, value: bool = True) -> bool:
    """Test-and-set the review `alerted` flag inside the store lock."""
    claimed = False
    with store._mutate():
        t = store.find(trade_id)
        m = t.get('review') if t and isinstance(t.get('review'), dict) else None
        if m is not None and bool(m.get('alerted')) != value:
            m['alerted'] = value
            t['version'] = t.get('version', 0) + 1
            claimed = True
    return claimed
