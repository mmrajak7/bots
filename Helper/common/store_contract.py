"""What each trade store may do to a record, given the state it is in.

WHY THIS IS A MODULE AND NOT A TEST
-----------------------------------
This table lived in `bcs/tests/test_store_contract.py` until 2026-08-30, and it
was the best state-machine specification in the repository -- the only place
that said, in one artefact, which of `begin_close` / `update_trade_exit` /
`recover_closing_trade` / `begin_recovery` each of the four books accepts from
each of the four record states. The design reviewer's finding was that it was
in the wrong file: a spec that only a test reads is a spec production is free
to disagree with, and it did. The test recorded the disagreement in a function
called `test_the_bcs_family_is_laxer_than_zebra_on_booking` and left it there.

So the table moved here and the stores now ASK it. The test still asserts the
same things; what changed is that it is now checking an implementation against
its specification rather than two implementations against each other.

WHAT THE TABLE SAYS, AND WHY
----------------------------
Four ROLES, because the four books use different words for the same states --
`bcs`/`bear_put`/`fallen_hero` say 'open' and 'closed', `zebra` says 'entered'
and 'exited'. Comparing raw status strings would compare the wrong things, so
each store declares its own name for each role and the rules are stated once.

  OPEN      a live position this engine manages
  CLOSING   a close is in flight; the consume-once lock is held
  FROZEN    `partial_close` -- legs are live at the broker and NOTHING is
            monitoring them. Terminal by design.
  TERMINAL  booked closed

FROZEN refuses everything except `begin_recovery`. That is the whole reason
`begin_recovery` is a separate verb rather than a widened `begin_close`: the
difference between the two verbs is exactly this column, and stating it as
data is what makes it checkable. M14's recovery path is reduce-only,
cause-gated, count-limited and journalled; nothing else may put an order on
top of a frozen record.

TERMINAL refuses everything because that is what idempotence IS -- the
guarantee that two processes racing to close cannot both book.

THE TIGHTENING THIS COST
------------------------
The three BCS-family stores had NO status check on `update_trade_exit` at all:
they would stamp 'closed' onto a record that was already closed, or onto one
frozen at `partial_close` with live legs. Only `begin_close` stood between that
and a real order. They enforce the table now, which is a behaviour change on
the money path and is the point of the item -- a specification nothing consults
is documentation.
"""
from __future__ import annotations

#: The four states a close path can find a record in.
OPEN = 'open'
CLOSING = 'closing'
FROZEN = 'frozen'
TERMINAL = 'terminal'
ROLES = (OPEN, CLOSING, FROZEN, TERMINAL)

#: The four verbs that move a record between them.
BEGIN_CLOSE = 'begin_close'
UPDATE_TRADE_EXIT = 'update_trade_exit'
RECOVER_CLOSING = 'recover_closing_trade'
BEGIN_RECOVERY = 'begin_recovery'
METHODS = (BEGIN_CLOSE, UPDATE_TRADE_EXIT, RECOVER_CLOSING, BEGIN_RECOVERY)

#: THE CONTRACT. True = the call is accepted and the record moves.
CONTRACT = {
    (BEGIN_CLOSE, OPEN): True,
    (BEGIN_CLOSE, CLOSING): False,
    (BEGIN_CLOSE, FROZEN): False,
    (BEGIN_CLOSE, TERMINAL): False,

    (UPDATE_TRADE_EXIT, OPEN): True,
    (UPDATE_TRADE_EXIT, CLOSING): True,
    (UPDATE_TRADE_EXIT, FROZEN): False,
    (UPDATE_TRADE_EXIT, TERMINAL): False,

    (RECOVER_CLOSING, OPEN): False,
    (RECOVER_CLOSING, CLOSING): True,
    (RECOVER_CLOSING, FROZEN): False,
    (RECOVER_CLOSING, TERMINAL): False,

    # The exact inverse of `begin_close`, which is the point of it being its
    # own verb: FROZEN is the only state it accepts, and the only state
    # `begin_close` must never accept.
    (BEGIN_RECOVERY, OPEN): False,
    (BEGIN_RECOVERY, CLOSING): False,
    (BEGIN_RECOVERY, FROZEN): True,
    (BEGIN_RECOVERY, TERMINAL): False,
}

#: Each book's own word for each role, ROLE -> status. Callers that need to
#: BUILD a record (tests, fixtures) read it this way round.
BCS_FAMILY_STATUSES = {OPEN: 'open', CLOSING: 'closing',
                       FROZEN: 'partial_close', TERMINAL: 'closed'}
ZEBRA_STATUSES = {OPEN: 'entered', CLOSING: 'closing',
                  FROZEN: 'partial_close', TERMINAL: 'exited'}

#: The same maps inverted, status -> ROLE, which is the direction a guard
#: needs. Derived rather than typed twice: two hand-maintained copies of one
#: mapping is the bug shape this whole module exists to remove.
BCS_FAMILY_ROLES = {v: k for k, v in BCS_FAMILY_STATUSES.items()}
ZEBRA_ROLES = {v: k for k, v in ZEBRA_STATUSES.items()}

#: BOTH vocabularies at once. `bcs/tests/fakes.MemoryStore` stands in for the
#: cohort store as well as for the BCS family (`bcs/tests/replay.py` hands it
#: to `_open_zebra_store`), so it must recognise either word for a role. The
#: merge is unambiguous: no status string means different things in the two
#: schemas -- 'closing' and 'partial_close' are shared and identical, and the
#: rest are disjoint.
ANY_ROLES = dict(BCS_FAMILY_ROLES)
ANY_ROLES.update(ZEBRA_ROLES)


def role_of(status, statuses=BCS_FAMILY_STATUSES):
    """This store's status string as a ROLE, or None if it names none.

    Accepts either direction of map -- ROLE -> status (what the stores hold)
    or status -> ROLE (`*_ROLES` above) -- because both are natural to pass and
    a caller that got it backwards would otherwise get a silent None, i.e. a
    refusal that looks like an unknown state.

    None rather than a guess. An unrecognised status is a record this table
    cannot speak for, and `allows` refuses it: a hand-edited or future-schema
    record must not be silently treated as OPEN, which is the only role that
    permits anything interesting.
    """
    if status in statuses and statuses[status] in ROLES:
        return statuses[status]              # status -> role
    for role, name in statuses.items():
        if status == name:
            return role                      # role -> status
    return None


def allows(method, status, statuses=BCS_FAMILY_STATUSES) -> bool:
    """May `method` act on a record in this store-specific `status`?

    Unknown status or unknown method -> False. Both are the conservative
    reading, and for the same reason: this function guards the only code path
    in the fleet that can place a real order, so "I do not recognise this"
    must never mean "go ahead".
    """
    role = role_of(status, statuses)
    if role is None:
        return False
    return CONTRACT.get((method, role), False)


def refusal(method, status, trade_id, statuses=BCS_FAMILY_STATUSES) -> str:
    """The log line for a refusal, naming the state rather than the rule.

    Whoever reads this is looking at a record that did not move, and what they
    need is which state it was in -- the rule is in this file and does not
    change.
    """
    role = role_of(status, statuses)
    if role is None:
        return ("Trade #%s has status %r, which this store's contract does "
                "not recognise — refusing %s. An unknown state is not an open "
                "one." % (trade_id, status, method))
    return ("Trade #%s is %s (%s); the store contract does not allow %s from "
            "that state." % (trade_id, status, role, method))
