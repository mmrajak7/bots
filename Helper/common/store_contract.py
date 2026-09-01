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



# -- What makes a record READABLE at all -------------------------------------
#
# Everything above says what may be done to a record in a given state. This
# says whether the record can be looked at without taking the whole book down
# with it, which is a strictly earlier question.
#
# THE DEFECT (found 2026-08-31). `_merge_trades` does `by_id[t['id']] = t` and
# then compares `t.get('version', 0) > ...` -- before any caller code runs,
# and on EVERY write path, because every write refreshes through the merge. So
# ONE record with a missing `id`, or a string `version`, made every
# `update_trade_fields` / `mark_exited` / `begin_close` on that book raise
# TypeError or KeyError. Exits included. `_read_local` quarantines JSON-level
# corruption but never looked inside the records, so the state survived every
# restart and the corruption alert never fired.
#
# Two realistic manufacture paths, neither of them a coding error:
#   * a hand edit during an incident -- this repo's history has several;
#   * `json.dump(..., default=str)`, which both stores pass, silently
#     stringifying any non-JSON-serialisable numeric (a numpy int, say) so the
#     NEXT read sees `"id": "419"`.
#
# The response is deliberately asymmetric, because the two available mistakes
# are not equally bad. Dropping a record from the working book stops it being
# monitored, which for an open position is the worst outcome in the system. So:
#
#   COERCE when it is lossless -- an integer written as a decimal string is
#   unambiguous, and repairing it is strictly safer than dropping a live
#   position over a type. Logged as a WARNING so the cause stays visible.
#
#   QUARANTINE only what cannot be read at all -- not a dict, no usable `id`,
#   or a duplicate id. Those are RETURNED to the caller rather than discarded,
#   so the store preserves them and raises the corruption alert instead of
#   going quietly on with a short book.
#
# `bool` is excluded explicitly: `isinstance(True, int)` is True in Python, so
# a record whose id is `True` would otherwise collide with id 1.

def _as_int(value):
    """`value` as an int if that is lossless, else None.

    Accepts an int (not a bool) and a string holding an exact integer literal.
    Rejects floats outright -- 419.0 is almost certainly a real id that went
    through a float, but 419.7 is not, and a rule that has to inspect the
    fractional part to decide is one that will be got wrong later.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def partition_readable(trades, log=None):
    """Split a raw trade list into (readable, unreadable), repairing in place.

    Every `readable` record is guaranteed to have an int `id`, an int
    `version` if it carries one, and an id no other readable record shares --
    which is exactly what `_merge_trades` assumes and never checked.

    `unreadable` is a list of `{'record', 'why'}`, returned rather than
    dropped so the caller can preserve it and alert. Order is preserved, and
    the first copy of a duplicated id wins so a re-read is deterministic.

    Never raises: a validator that can itself fail on bad data is not one.
    """
    readable, unreadable, seen = [], [], {}
    say = log or (lambda *a: None)
    for t in trades or ():
        if not isinstance(t, dict):
            unreadable.append({'record': t, 'why': 'not an object'})
            continue
        tid = _as_int(t.get('id'))
        if tid is None:
            unreadable.append(
                {'record': t,
                 'why': 'id is %r, which is not an integer' % (t.get('id'),)})
            continue
        if t.get('id') != tid:
            say('record id %r read as %d - repairing the type in memory; '
                'something wrote this id as a non-integer', t.get('id'), tid)
            t['id'] = tid
        if 'version' in t:
            ver = _as_int(t.get('version'))
            if ver is None:
                # Unlike a bad id, an uncomparable version has a safe reading:
                # treat the record as the OLDEST possible, so a good copy on
                # the other side of a merge wins and this one can never
                # overwrite anything.
                say('record #%d has version %r, which is not an integer - '
                    'treating it as version 0 so it can never win a merge',
                    tid, t.get('version'))
                t['version'] = 0
            elif t.get('version') != ver:
                say('record #%d version %r read as %d - repairing the type',
                    tid, t.get('version'), ver)
                t['version'] = ver
        if tid in seen:
            # Two records with one id cannot both survive a merge keyed on it.
            # Quarantining the later one makes the loss VISIBLE; letting the
            # merge pick is what makes it silent.
            unreadable.append(
                {'record': t,
                 'why': 'duplicate id %d - the first copy (version %s) was '
                        'kept' % (tid, seen[tid].get('version'))})
            continue
        seen[tid] = t
        readable.append(t)
    return readable, unreadable



# -- Resolving one id held by two replicas -----------------------------------
#
# THE DEFECT (found 2026-08-31). Both merges resolve by `version`, and a
# version is a per-record counter each replica increments on its OWN writes.
# It is not a conflict detector, and treating it as one produces two silent
# failures:
#
#   1. A TIE with different content. `_merge_trades` keeps base on a tie and
#      the divergence check compares VERSION MAPS only, so local and Drive
#      disagree forever, no re-upload is triggered, and nothing is logged.
#
#   2. A BOOKED EXIT UN-BOOKED. Local `exited` at version 7, Drive `entered`
#      at version 8 -- two alert-flag bumps on the other machine are enough to
#      outrun a close. Higher version wins, the exit record is erased, and the
#      trade REOPENS as a position nobody entered.
#
# (2) is the one that costs money, and it has a real invariant to appeal to
# rather than a heuristic: closing is MONOTONIC. `CONTRACT` above already says
# TERMINAL refuses every method, "because that is what idempotence IS -- the
# guarantee that two processes racing to close cannot both book". A merge that
# can walk a record back out of TERMINAL contradicts the table the same file
# declares, so the rule here is not a new policy; it is the existing one
# applied to the one code path that was not asking.
#
# (1) is left resolving as it always did -- base wins -- because changing
# WHICH side wins is a bigger behavioural change than announcing that the
# question was asked. What changes is that it is no longer silent.

#: Stamped by `zebra.restore_snapshot.rebuild` on a record it deliberately
#: reopens. A restore is the ONE authorised way a closed record goes back to
#: open, and it is a human decision made in front of an incident -- so it is
#: named explicitly here rather than inferred from a version counter, which is
#: the mistake this whole function exists to stop making.
RESTORE_MARKER = 'restored_from_snapshot_at'

#: Statuses that END a record's life without being TERMINAL in `CONTRACT`'s
#: sense. zebra's `cancelled` is the whole list: a signal that never entered,
#: so it has no exit to book and never appears in the table above -- which
#: means `role_of` returns None for it and, before 2026-08-31, version
#: resolution walked it straight back to `triggered`. A resurrected cancel
#: re-occupies its dedup slot and can re-alert and re-enter.
SETTLED_EXTRA = ('cancelled',)


def is_settled(trade, statuses=BCS_FAMILY_STATUSES) -> bool:
    """Is this record's life over, so that a COUNTER may not walk it back?

    TERMINAL (a booked exit) plus `SETTLED_EXTRA` (a cancel). FROZEN is
    deliberately NOT here: it is protected by its own rule below, because it
    is protected in only ONE direction -- a completed recovery on the other
    replica must still be able to land on top of it.
    """
    try:
        if role_of(trade.get('status'), statuses) == TERMINAL:
            return True
        return trade.get('status') in SETTLED_EXTRA
    except Exception:                            # pragma: no cover - defensive
        return False

#: The two things the corruption marker can mean. They are NOT the same event
#: and must not share alert text: a QUARANTINE means the book failed to parse,
#: went empty, and open positions stopped being monitored; a MERGE_CONFLICT
#: means two writers touched one record and the book is entirely intact. The
#: marker carried no kind until 2026-08-31, so the alerting layers read a
#: missing kind as QUARANTINE -- that is what every marker written before this
#: change actually was.
MARKER_QUARANTINE = 'quarantine'
MARKER_MERGE_CONFLICT = 'merge_conflict'

#: How many differing field names a conflict note will list before giving up
#: and saying how many more there were. A note is a Telegram line, not a diff.
_MAX_DIFF_KEYS = 6


def diff_keys(a, b):
    """The field names on which two copies of one record disagree.

    'different content' with no diff is unactionable -- diagnosing the first
    real occurrence meant downloading the Drive copy by hand to discover the
    argument was over a single `review` field. Returns a short human string.
    """
    try:
        keys = sorted(set(a) | set(b))
        differing = [k for k in keys if a.get(k) != b.get(k)]
        if not differing:
            return 'no field-level difference (ordering or type only)'
        shown = ', '.join(differing[:_MAX_DIFF_KEYS])
        extra = len(differing) - _MAX_DIFF_KEYS
        return shown + (' and %d more' % extra if extra > 0 else '')
    except Exception:                            # pragma: no cover - defensive
        return 'field-level difference could not be computed'


def _only_unversioned(base_trade, incoming_trade, fields, prefixes,
                      suffixes=()):
    """Do the two copies differ ONLY on fields written without a version bump?

    Some writes are deliberately local-only and deliberately do NOT increment
    the counter -- zebra's `apply_mfe` batches the per-poll peak, spot-
    corroboration and depth fields this way, because pushing them to Drive
    every five minutes would churn the network for data nobody reads until the
    trade closes. They ride to Drive later on the next versioned write.

    The consequence is structural, not accidental: for every open position, on
    every poll, this replica's disk and Drive hold the SAME version with
    DIFFERENT content. That is the tie condition exactly, so a detector that
    does not know about these fields reports a split brain once per position
    per cycle forever -- and drowns the real one it exists to catch.

    Returns False when nothing differs, so a genuine identical pair is left to
    the caller's own `!=` check rather than being classed as unversioned.
    """
    try:
        differing = [k for k in set(base_trade) | set(incoming_trade)
                     if base_trade.get(k) != incoming_trade.get(k)]
        if not differing:
            return False
        # `startswith(())` is False for every string, so an empty prefix tuple
        # correctly matches nothing. An accidental `('',)` would match ALL of
        # them and silence every conflict in the system -- pinned by a test.
        # `startswith(())` / `endswith(())` are False for every string, so an
        # empty tuple correctly matches nothing.
        pre, suf = tuple(prefixes), tuple(suffixes)
        return all(k in fields or str(k).startswith(pre) or str(k).endswith(suf)
                   for k in differing)
    except Exception:                            # pragma: no cover - defensive
        return False


#: What makes two copies the SAME trade, beyond sharing an id. Deliberately
#: only fields fixed at entry: a status, a version or a fill moves over a
#: record's life, and comparing those would call every ordinary update a
#: different trade. Legs are checked across BOTH vocabularies because one
#: function serves all four books.
_IDENTITY_FIELDS = ('stock', 'long_symbol', 'short_symbol', 'entry_date',
                    'long_put_symbol', 'short_put_symbol', 'short_call_symbol')


def _identity_str(t) -> str:
    try:
        parts = [str(t.get(f)) for f in _IDENTITY_FIELDS if t.get(f)]
        return ' '.join(parts) or '(no identity fields)'
    except Exception:                            # pragma: no cover - defensive
        return '(unreadable)'


def _identities_conflict(a, b) -> bool:
    """Do these two copies name DIFFERENT trades?

    True only when both sides state the same identity field and the values
    differ. Absent on either side is "cannot tell", which resolves as no
    conflict -- an older record missing `entry_date` must not be read as a
    collision with its own newer copy.
    """
    try:
        for f in _IDENTITY_FIELDS:
            av, bv = a.get(f), b.get(f)
            if av and bv and av != bv:
                return True
        return False
    except Exception:                            # pragma: no cover - defensive
        return False


def resolve_merge(base_trade, incoming_trade, statuses=BCS_FAMILY_STATUSES,
                  same_replica=False, unversioned_fields=frozenset(),
                  unversioned_prefixes=(), unversioned_suffixes=()):
    """Which copy of one record survives, and what to say about it.

    Returns `(winner, note)`. `note` is None on an ordinary resolution and a
    human sentence on anything a reader needs to know about -- the caller logs
    it and raises its corruption alert.

    `same_replica=True` says the two inputs are THIS replica's disk and THIS
    process's own cache, not two replicas. A version tie is then routine rather
    than alarming: another process on the same box (zebra's spawned vet /
    review / postmortem CLIs -- see `ZebraStore._mutate`, which documents the
    two writers as by-design) wrote the record while we held a cache, and
    absorbing that write is the entire purpose of the refresh. Resolution is
    unchanged either way; only whether the question is announced changes.

    `unversioned_fields` / `unversioned_prefixes` name the fields a store
    writes WITHOUT bumping the counter, so a tie confined to them is by design
    rather than a divergence. Both default to empty: a store with no such write
    -- the whole BCS family -- must not inherit the exemption. See
    `_only_unversioned`.

    Never raises: a merge that can fail on odd data strands the whole book.
    """
    try:
        b_ver = base_trade.get('version', 0)
        i_ver = incoming_trade.get('version', 0)
        b_role = role_of(base_trade.get('status'), statuses)
        i_role = role_of(incoming_trade.get('status'), statuses)

        # A BOOKED CLOSE IS NOT WALKED BACK BY A COUNTER.
        #
        # The case: local `exited` at version 7, the other replica `entered` at
        # version 8. Two alert-flag bumps over there are enough to outrun a
        # close, and higher-version-wins then ERASES the exit and reopens the
        # trade as a position nobody entered.
        #
        # `CONTRACT` above already says TERMINAL refuses every method, "because
        # that is what idempotence IS". A merge that can walk a record back out
        # of TERMINAL contradicts the table in this same file; the rule here is
        # that table applied to the one path that was not asking.
        #
        # The carve-out is `restore_snapshot`, and it is the reason this is
        # keyed on an EXPLICIT marker rather than on the transition. A restore
        # is exactly a deliberate un-booking of a wrongly-booked close -- the
        # 2026-08-30 `deploy_server.sh` incident force-closed six live
        # positions at -100%, and the recovery works by out-versioning those
        # exits. Refusing it as "a stale replica reopening a closed trade"
        # would break the only tool that has ever had to fix this book.
        # TWO DIFFERENT TRADES WEARING ONE ID (found 2026-08-31).
        #
        # Id allocation is per-replica `max(live, sidecar) + 1`, so during a
        # sync gap -- five minutes normally, arbitrarily long in Drive-down
        # local-only mode -- two machines can both mint id 15 for DIFFERENT
        # trades. The owner hand-captures BCS trades on Windows while the Pi
        # writes the same books, so this topology is supported rather than
        # hypothetical; the merge docstring says so.
        #
        # Everything below then resolves them as ONE record: version picks a
        # winner and the other trade ceases to exist, with at most a note
        # describing it as a field conflict. `partition_readable`'s duplicate-id
        # quarantine cannot help -- it only looks WITHIN one file.
        #
        # Identity is the stock plus the legs. Where both sides name them and
        # they DISAGREE, this is not one record at two versions, and no
        # automatic resolution is right: keep base, say so loudly, and let a
        # human re-id one of them. Announcing it is the whole point -- the
        # silent version pick is what loses a trade.
        if _identities_conflict(base_trade, incoming_trade):
            return base_trade, (
                'id #%s names DIFFERENT TRADES on the two replicas (%s vs %s). '
                'Two machines allocated the same id during a sync gap, so this '
                'is not one record at two versions -- the merge is keeping the '
                'local copy and the other trade is NOT in this book. Re-id one '
                'of them by hand before anything acts on either.'
                % (base_trade.get('id'), _identity_str(base_trade),
                   _identity_str(incoming_trade)))

        b_settled = is_settled(base_trade, statuses)
        i_settled = is_settled(incoming_trade, statuses)

        if b_settled and not i_settled:
            if incoming_trade.get(RESTORE_MARKER):
                return incoming_trade, (
                    'record #%s was %s locally and is being REOPENED by a '
                    'snapshot restore (%s). Taking the restored copy.'
                    % (base_trade.get('id'), base_trade.get('status'),
                       incoming_trade.get(RESTORE_MARKER)))
            if i_ver > b_ver:
                return base_trade, (
                    'record #%s is %s locally (version %s) and %s on the '
                    'other replica at a HIGHER version %s. Keeping the CLOSED '
                    'copy: a booked exit is not undone by a version counter, '
                    'which is a per-replica write count and not a clock. The '
                    'other replica is running against a stale book and the '
                    'two will not converge on their own.'
                    % (base_trade.get('id'), base_trade.get('status'), b_ver,
                       incoming_trade.get('status'), i_ver))
            return base_trade, None

        # THE SAME RULE, THE OTHER WAY ROUND (found 2026-08-31, second review).
        #
        # The branch above reads `base` as the closed copy, because that is
        # the direction the incident arrived from. But `base` is simply
        # whichever side the caller passed first -- local disk on the Drive
        # merge, the process cache on a sibling refresh -- and the stale
        # replica is just as often the LOCAL one. Local `entered` at version
        # 8 against Drive `exited` at version 7 is the identical fault with
        # the arguments swapped, and it fell through to plain version
        # resolution: base wins, the booked exit is DISCARDED, and the
        # divergence check then re-uploads the reopened copy over the good
        # one. Monotonic in one direction is not monotonic.
        #
        # Bounded to `i_ver <= b_ver`, which is the only region the ordinary
        # resolution below gets wrong -- a settled copy at a HIGHER version
        # already wins there, and leaving that case alone is what lets a
        # completed close on the other replica land normally.
        #
        # The restore carve-out is mirrored too, and has to be: `rebuild`
        # stamps the marker and sets version = max+1, so a restored record is
        # exactly a non-settled BASE at a HIGHER version than the exit it is
        # undoing. Without this check the mirror would re-book the very close
        # the restore was run to reverse.
        if i_settled and not b_settled and i_ver <= b_ver:
            if base_trade.get(RESTORE_MARKER):
                return base_trade, None
            return incoming_trade, (
                'record #%s is %s locally (version %s) and %s on the other '
                'replica at a LOWER-or-equal version %s. Taking the CLOSED '
                'copy: a booked exit is not undone by a version counter, '
                'which is a per-replica write count and not a clock. This '
                'replica is the stale one.'
                % (base_trade.get('id'), base_trade.get('status'), b_ver,
                   incoming_trade.get('status'), i_ver))

        # A FREEZE IS NOT WALKED BACK BY A COUNTER EITHER.
        #
        # `partial_close` means legs are live at the broker and NOTHING is
        # monitoring them; `CONTRACT` says FROZEN refuses every verb except
        # `begin_recovery`. Version resolution undid that: two alert-flag
        # bumps on a stale replica returned the record to `entered`, which
        # re-arms every auto-exit trigger against a position whose legs are
        # in an unknown state -- an order placed on top of live legs.
        #
        # One direction only. A recovery that RAN on the other replica ends
        # settled, and a settled copy at a higher version still wins above,
        # so a finished recovery propagates. What is refused here is a copy
        # that is merely NEWER, and the refusal is always announced -- a
        # frozen record is human-owned, and silence is what let this one sit.
        if (b_role == FROZEN and i_role != FROZEN and not i_settled
                and i_ver > b_ver):
            if incoming_trade.get(RESTORE_MARKER):
                return incoming_trade, (
                    'record #%s was frozen at partial_close locally and is '
                    'being REOPENED by a snapshot restore (%s).'
                    % (base_trade.get('id'),
                       incoming_trade.get(RESTORE_MARKER)))
            return base_trade, (
                'record #%s is FROZEN at partial_close locally (version %s) '
                'and %s on the other replica at a HIGHER version %s. Keeping '
                'the frozen copy: legs may be live at the broker with nothing '
                'monitoring them, and a version counter is not evidence that '
                'they were recovered. Resolve this record by hand.'
                % (base_trade.get('id'), b_ver,
                   incoming_trade.get('status'), i_ver))

        # Ordinary version resolution, unchanged.
        if i_ver > b_ver:
            return incoming_trade, None
        if i_ver == b_ver and incoming_trade != base_trade:
            # A TIE WITH DIFFERENT CONTENT between two REPLICAS is a split
            # brain, not a conflict either side can resolve: the counters are
            # per-replica, so equal versions carry no information about which
            # write came first. Base keeps winning, as it always has -- what
            # changes is that the question is no longer asked silently. The
            # divergence check that triggers a re-upload compares VERSION MAPS,
            # so this state used to produce no log line, no alert, no re-upload.
            #
            # Between this replica's DISK and this process's own CACHE it is
            # none of those things -- it is a concurrent write by a sibling
            # process on the same box, which the refresh exists to absorb. On
            # 2026-08-31, the detector's first live session, that case produced
            # 11 CRITICAL lines and 4 false corruption alerts against a book
            # that was fully intact and reconverged on its own.
            if same_replica:
                return base_trade, None
            # Fields written local-only and deliberately without a version
            # bump differ at an equal version BY DESIGN, once per open
            # position per poll. Base wins, which is also correct: base is the
            # local disk on the Drive merge, and it holds the fresher poll
            # data that has not been pushed yet.
            if _only_unversioned(base_trade, incoming_trade,
                                 unversioned_fields, unversioned_prefixes,
                                 unversioned_suffixes):
                return base_trade, None
            return base_trade, (
                'record #%s is at version %s on BOTH replicas with DIFFERENT '
                'content (differs on: %s). Versions are per-replica counters, '
                'so neither side can resolve this. Keeping the local copy; the '
                'two books will not converge on their own.'
                % (base_trade.get('id'), b_ver,
                   diff_keys(base_trade, incoming_trade)))
        return base_trade, None
    except Exception as e:                       # pragma: no cover - defensive
        try:
            tid = base_trade.get('id')
        except Exception:
            tid = '?'
        return base_trade, ('record #%s could not be resolved against the '
                            'other replica (%s); keeping the local copy'
                            % (tid, e))
