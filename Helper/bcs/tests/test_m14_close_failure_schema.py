"""M14 step 1 - every frozen close records WHY, WHEN and WHAT it is owed.

A `partial_close` record drops out of `get_open_trades()`. It is then never
retried, never re-monitored, never re-alerted: one Telegram at freeze time is
the entire lifecycle of a position that may be live at the broker with its
stops dead. That is the unwatched-position failure that has cost this account
real money twice, and M14's sweep is what fixes it.

The sweep cannot act on a record that does not say when it froze, what caused
it, or how many attempts it has already spent. This file pins the schema, and
- more importantly - pins that EVERY freeze site writes it. A freeze that
skips the stamp is invisible to the sweep in exactly the way a `partial_close`
is invisible to the monitor today, which is the bug one level up.

Nothing here places an order or changes any behaviour. The stamp is inert
until the sweep ships.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_close_failure_schema.py -v
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402


# == the cause vocabulary ====================================================

def test_a_leg_that_was_never_tradeable_is_not_the_same_as_one_nobody_filled():
    """`unreadable_book` says we could not look; `unfilled` says we looked and
    nobody traded. They earn different recovery treatment, and collapsing them
    is the `feedback_never_asked_is_not_failed` mistake."""
    assert sm._close_failure_cause(None) == 'unreadable_book'
    assert sm._close_failure_cause(
        {'status': 'PARTIAL', 'filled_quantity': 0}) == 'unfilled'


def test_rejected_is_recognised_because_it_is_the_one_that_stops_recovery():
    """The only cause that CHANGES behaviour: the sweep escalates immediately
    and places zero orders. A broker's rejection reason - margin, price band,
    frozen scrip - will repeat, and re-firing into it is the Feb-2026 "firing
    into a broken state" failure."""
    assert sm._close_failure_cause({'status': 'REJECTED'}) == 'rejected'
    assert sm._close_failure_cause({'status': 'rejected'}) == 'rejected'


def test_an_empty_result_dict_is_not_silently_rejected():
    """A dict with no status is a malformed result, not a rejection. Guessing
    `rejected` would disable recovery on a record that deserves it."""
    assert sm._close_failure_cause({}) == 'unfilled'


def test_the_cause_vocabulary_is_closed():
    assert set(sm.CLOSE_FAILURE_CAUSES) == {
        'unfilled', 'rejected', 'unreadable_book', 'exception', 'flipped'}


def test_an_unknown_cause_is_refused_at_construction():
    """Not stored and reported later - refused. A cause outside the vocabulary
    reaches the sweep as an unrecognised value, and the sweep must then treat
    the record as `escalated`; better to never write it."""
    with pytest.raises(AssertionError):
        sm._close_failure(cause='probably_fine', leg='short', reason='TP')


# == the record shape ========================================================

def test_the_record_carries_everything_the_sweep_needs():
    rec = sm._close_failure(cause='unfilled', leg='short', reason='SL_SPREAD')
    assert set(rec) == {'frozen_at', 'cause', 'leg', 'reason', 'state',
                        'attempts', 'next_attempt_after', 'recovery_fills'}
    assert rec['cause'] == 'unfilled'
    assert rec['leg'] == 'short'
    assert rec['reason'] == 'SL_SPREAD'


def test_the_clock_starts_at_the_freeze_not_at_the_telegram():
    """A dropped send must not postpone recovery, and acting is the safe
    direction when nobody was told. So `frozen_at` is stamped by the writer,
    in the same call that freezes the trade."""
    rec = sm._close_failure(cause='unfilled', leg='short', reason='TP')
    assert rec['frozen_at'], 'a freeze with no timestamp can never age out'
    # parseable, not just truthy
    from datetime import datetime
    datetime.fromisoformat(rec['frozen_at'])


def test_a_fresh_freeze_has_spent_no_attempts_and_is_owed_no_wait():
    rec = sm._close_failure(cause='unfilled', leg='long', reason='TP')
    assert rec['attempts'] == 0
    assert rec['next_attempt_after'] is None
    assert rec['recovery_fills'] == {}


def test_state_starts_frozen_not_recovering():
    """`recovering` would mean an attempt is in flight. Starting there would
    let a restart believe an order it never placed is outstanding."""
    assert sm._close_failure(cause='unfilled', leg='long',
                             reason='TP')['state'] == 'frozen'


def test_each_record_is_independent():
    """A shared mutable default here would let one incident's attempts count
    against another's - the classic and very expensive version of this bug."""
    a = sm._close_failure(cause='unfilled', leg='short', reason='TP')
    b = sm._close_failure(cause='unfilled', leg='long', reason='TP')
    a['recovery_fills']['short'] = 1.0
    a['attempts'] = 3
    assert b['recovery_fills'] == {} and b['attempts'] == 0


# == THE guard: no freeze site may skip the stamp ============================

def _partial_close_writes(*fns):
    """Every `set_trade_status(..., 'partial_close', ...)` call in `fns`.

    Source-derived rather than enumerated, because the failure mode is a NEW
    freeze site added later without the stamp - which a hand-written list of
    known sites cannot notice by construction.
    """
    out = []
    for fn in fns:
        tree = ast.parse(inspect.getsource(fn).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute)
                    and f.attr == 'set_trade_status'):
                continue
            args = [a for a in node.args
                    if isinstance(a, ast.Constant) and a.value == 'partial_close']
            if args:
                out.append((fn.__name__, node))
    return out


CLOSE_FNS = (sm._close_spread_inner, sm._close_fh_inner,
             sm._freeze_fh_leg_failure, sm.close_spread)


def test_the_guard_actually_finds_the_freeze_sites():
    """A source guard that matches nothing passes forever."""
    found = _partial_close_writes(*CLOSE_FNS)
    assert len(found) >= 8, [n for n, _ in found]


def test_every_freeze_site_stamps_close_failure():
    """THE regression. A freeze the sweep cannot see is a position nothing
    watches - the exact failure M14 exists to end, reintroduced one call site
    at a time."""
    missing = []
    for fname, node in _partial_close_writes(*CLOSE_FNS):
        kw = {k.arg for k in node.keywords if k.arg}
        if 'close_failure' not in kw:
            missing.append('%s line %d' % (fname, node.lineno))
    assert not missing, (
        'these freeze sites write `partial_close` without a `close_failure` '
        'record, so M14\'s sweep cannot see them: %s' % missing)


def test_every_FREEZE_stamp_is_built_by_the_one_helper():
    """Not hand-rolled per site. A literal dict at a freeze site is how the
    schema drifts, and the sweep then reads a key that is sometimes absent.

    Scoped to the `set_trade_status(..., 'partial_close', ...)` calls, not to
    every mention of the keyword in the module: the recovery sweep legitimately
    writes an UPDATED record back through `update_trade_fields`, and a guard
    that could not tell a freeze from an update would forbid the sweep from
    recording its own progress.
    """
    bad = []
    for fname, node in _partial_close_writes(*CLOSE_FNS):
        for kw in node.keywords:
            if kw.arg != 'close_failure':
                continue
            ok = (isinstance(kw.value, ast.Call)
                  and isinstance(kw.value.func, ast.Name)
                  and kw.value.func.id == '_close_failure')
            if not ok:
                bad.append('%s line %d' % (fname, node.lineno))
    assert not bad, (
        'these freeze sites build `close_failure` by hand instead of calling '
        '_close_failure(): %s' % bad)


def test_the_helper_is_used_at_every_site_the_guard_found():
    found = _partial_close_writes(*CLOSE_FNS)
    stamped = sum(1 for _, node in found
                  for kw in node.keywords if kw.arg == 'close_failure')
    assert len(found) == stamped, (
        'count of freeze sites and count of stamps disagree - one site is '
        'either stamping twice or not at all')


def test_the_sweep_may_still_update_a_record_it_did_not_freeze():
    """The inverse review. Narrowing the guard above must not have narrowed it
    to nothing, and the sweep's own progress writes must stay legal."""
    src = inspect.getsource(sm._persist_recovery)
    assert 'close_failure=cf' in src
    assert 'update_trade_fields' in src
