"""Which alerts reach the phone, and why. ONE place, readable in one sitting.

Written 2026-08-28. The owner's morning looked like this: a take-profit
reported as a −Rs 16,316 loss, the same take-profit reported as +Rs 7,790, and
a third close reporting "P&L: not computed". All three were the system working
correctly — an old process, zebra's real booking, and the new code refusing to
invent a price — but reading a phone at 09:30 there is no way to know that. His
instruction:

    "i dont need unwanted messages like these ... need only important alerts in
    telegram"

**The principle.** Telegram is for what a human must SEE or ACT ON. It is not
the evidence channel. Evidence lives in `logs/spread_monitor_cron_*.log` and in
the order journal, both of which this policy leaves completely untouched — so
silencing a message costs nothing that the dry-run evidence week depends on.
Suppression here means "not on the phone", never "not recorded".

**Default is SEND.** An alert with no class is delivered. Silence has to be a
deliberate entry in `SILENT` below, because the failure mode of the opposite
default is an alert nobody knew had stopped arriving — and this system's whole
history is guards that were wired in, looked deployed, and could never fire.

**A shadow is the thing being silenced, and only that.** During the dry-run
evidence week this monitor walks every close it would have made, for positions
it cannot trade, that zebra books a few minutes later with the real P&L. Two
messages per position, one of them carrying no number. That is the noise. The
LIVE twin of the same event — armed, and a real close that could not be priced
or placed — is the single most important message this system sends, and it is
classified SAFETY so that no future edit can sweep it up with the rehearsals.
Do not collapse those two: the words are nearly identical and the required
reaction is opposite.
"""

from __future__ import annotations

from typing import Optional, Tuple

# ── The classes ─────────────────────────────────────────────────────────────

#: A human must look, and probably act. Manual intervention, a frozen or
#: partial position, a refusal that leaves money exposed, a dead token, a
#: tripped kill switch, an unavailable vet, monitoring gone blind. NEVER
#: silenceable — see `NEVER_SILENT`.
SAFETY = 'safety'

#: A real position really changed: an entry ticket, a real exit with a real
#: P&L. The owner asked for these by name. Also never silenceable — an exit
#: that happened and was not reported is indistinguishable from one that did
#: not happen.
BOOKING = 'booking'

#: Informational, not urgent, but wanted: expiry proximity, delivery-margin
#: warnings, startup/heartbeat notices. Sent today; a candidate for a digest
#: later, which is why it has a name of its own rather than being unclassified.
NOTICE = 'notice'

#: A REHEARSAL. The monitor is in dry run — or the record is a paper record
#: that another engine books — so the event being described did not happen and
#: no order was placed. Log only.
SHADOW = 'shadow'

#: The same event as SHADOW's counterpart but ARMED and REAL. Exists as a
#: distinct name purely so the difference is visible in the source at the call
#: site, where the mistake would be made.
SAFETY_LIVE_REFUSAL = SAFETY

ALL_CLASSES = frozenset({SAFETY, BOOKING, NOTICE, SHADOW})

# ── The policy ──────────────────────────────────────────────────────────────

#: Classes withheld from Telegram. Everything else — including an alert with
#: no class at all — is sent.
SILENT = frozenset({SHADOW})

#: Classes that may NEVER be added to SILENT. Asserted at import, so a commit
#: that tries cannot even load the module, and pinned by a test so the reason
#: survives the next refactor.
NEVER_SILENT = frozenset({SAFETY, BOOKING})

_OVERLAP = SILENT & NEVER_SILENT
if _OVERLAP:                                     # pragma: no cover - import guard
    raise RuntimeError(
        'alert_policy: %s is in both SILENT and NEVER_SILENT. A safety or '
        'booking alert can never be suppressed — silencing one of these is '
        'how a system stops reporting the only events it exists to report.'
        % ', '.join(sorted(_OVERLAP)))

_WHY = {
    SHADOW: ('dry-run rehearsal — no order was placed and another engine '
             'books this position; the log and the order journal have the '
             'full record'),
}


def should_send(alert_class: Optional[str]) -> Tuple[bool, str]:
    """(send?, why) for one alert.

    Never raises and never guesses: an unknown class is SENT, with a reason
    that says it was unknown, because a typo in a class name must not silence
    an alert.
    """
    if alert_class is None:
        return True, 'unclassified — default is to send'
    if alert_class in NEVER_SILENT:
        return True, '%s alerts are never suppressible' % alert_class
    if alert_class in SILENT:
        return False, _WHY.get(alert_class, 'suppressed by policy')
    if alert_class not in ALL_CLASSES:
        return True, 'unknown class %r — default is to send' % alert_class
    return True, '%s is not in SILENT' % alert_class


def close_alert_class(dry_run: bool, is_paper_record: bool) -> str:
    """The class of a close/trigger alert, from the two facts that decide it.

    Three cases, and the middle one is the one that is easy to get wrong:

    * **a PAPER record — SHADOW.** No legs exist at any broker and zebra books
      it, so this monitor's message is a duplicate of one the owner gets a few
      minutes later carrying the real P&L. This is the noise he asked to lose.
      True whether or not the monitor is armed; armed, `close_spread` refuses
      the record outright and raises its OWN SAFETY alert saying the bridge
      should never have been handed it, which is a different message.
    * **a REAL record while the monitor is in DRY RUN — SAFETY, not shadow.**
      A live position's stop just fired and the only engine that could close it
      is disarmed. Nothing books this and nothing else will report it. Calling
      it a rehearsal because `dry_run` is set would silence the one dry-run
      message that actually needs a human. `dry_run` alone is NOT what makes an
      alert noise; having another engine that books is.
    * otherwise a real position really closed — BOOKING.
    """
    if is_paper_record:
        return SHADOW
    if dry_run:
        return SAFETY
    return BOOKING
