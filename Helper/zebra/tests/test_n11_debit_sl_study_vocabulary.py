"""N11 - the debit-SL study matched its own exit reason by SUBSTRING.

`zebra/debit_sl_study.py` selected its population with

    'debit_sl' in (t.get('exit_reason') or '')

which matches the PAPER engine's `paper:debit_sl` and misses every name the
ORDER engine writes for the same trigger - `SL_SPREAD` and its recovery-path
variant `ALREADY_FLAT_SL_SPREAD`. Two engines close a cohort position and they
do not share a vocabulary; this was the sixth private copy of the translation,
and the only one left that was still a raw string test.

**Why it mattered more than the other five.** This is the study behind "the 50%
debit stop SAVED Rs 42,369 over 20 exits" - the measured basis for keeping the
stop. It was latent while the book held only `paper:*` reasons, and it starts
under-counting **at the exact moment the bridge books its first real stop**:
the arming decision the study informs. A study that silently sees fewer stops
than happened is worse than no study, because it reads as evidence.

The regression that matters is therefore not "the fix works today" - today's
book cannot tell the two apart. It is `test_the_bridged_names_are_counted`,
which uses the names the order engine will start writing the day exits are
armed.

A recovery-path close (`already_flat_sl_spread`) COUNTS here, deliberately, and
that differs from the arming gate. The gate asks "were orders transacted"; this
study asks "was the position closed on a debit-stop trigger, and what would
holding to expiry have paid instead". Same vocabulary, different predicate -
which is the point of having the vocabulary in one place.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_n11_debit_sl_study_vocabulary.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import outcomes                                         # noqa: E402


def _is_debit_sl(reason):
    """The predicate the study now uses, isolated."""
    return outcomes.classify(reason)['kind'] == outcomes.DEBIT_SL


# == the defect, on the names that will actually appear =====================

@pytest.mark.parametrize('reason', [
    'SL_SPREAD',                # bcs/spread_monitor.py, as written
    'sl_spread',                # after bcs/zebra_adapter.py lowercases it
    'ALREADY_FLAT_SL_SPREAD',   # the recovery path
    'already_flat_sl_spread',
])
def test_the_bridged_names_are_counted(reason):
    """THE regression. Every one of these fails the old substring test."""
    assert 'debit_sl' not in reason.lower(), \
        'if this ever becomes true the substring bug stops being demonstrable'
    assert _is_debit_sl(reason)


def test_the_paper_name_is_still_counted():
    """The fix must not trade one blind spot for another."""
    assert _is_debit_sl('paper:debit_sl')
    assert _is_debit_sl('debit_sl')


@pytest.mark.parametrize('reason', [
    'paper:tp', 'tp', 'already_flat_tp',
    'paper:trail', 'sl_trail',
    'paper:spot_sl', 'sl_spot',
    'paper:time', 'paper:expiry', 'expiry_force_close',
    'manual', None, '',
])
def test_nothing_else_is_swept_in(reason):
    """The inverse review. A predicate that is merely WIDER is not a fix."""
    assert not _is_debit_sl(reason)


# == the study's own selection, end to end ==================================

def _select(book):
    """Mirror of the study's population filter. Kept tiny on purpose - the
    source-derived guard below is what stops it drifting from production."""
    return [t for t in book
            if t.get('status') == 'exited' and _is_debit_sl(t.get('exit_reason'))]


def test_a_mixed_book_finds_both_engines():
    book = [
        {'id': 1, 'status': 'exited', 'exit_reason': 'paper:debit_sl'},
        {'id': 2, 'status': 'exited', 'exit_reason': 'sl_spread'},
        {'id': 3, 'status': 'exited', 'exit_reason': 'already_flat_sl_spread'},
        {'id': 4, 'status': 'exited', 'exit_reason': 'paper:tp'},
        {'id': 5, 'status': 'entered', 'exit_reason': 'sl_spread'},
    ]
    assert [t['id'] for t in _select(book)] == [1, 2, 3]


def test_the_old_substring_test_would_have_found_only_one_of_them():
    """The size of the miss, stated rather than asserted abstractly: two of the
    three real debit stops vanish, and both are the LIVE-money ones."""
    book = [
        {'id': 1, 'status': 'exited', 'exit_reason': 'paper:debit_sl'},
        {'id': 2, 'status': 'exited', 'exit_reason': 'sl_spread'},
        {'id': 3, 'status': 'exited', 'exit_reason': 'already_flat_sl_spread'},
    ]
    old = [t for t in book if 'debit_sl' in (t.get('exit_reason') or '')]
    assert len(old) == 1 and len(_select(book)) == 3


# == the fix is wired into production, not just available ===================

def _executable_lines(path):
    """Source with comment-only lines removed.

    Needed because the fix's own comment QUOTES the defective line, so the next
    reader can see what was wrong without digging through git. A guard that
    cannot tell code from a comment forbids documenting the bug it protects
    against - a worse trade than the guard is worth.
    """
    return '\n'.join(l for l in path.read_text(encoding='utf-8').splitlines()
                     if not l.strip().startswith('#'))


def test_the_study_uses_the_shared_vocabulary_and_no_substring_test():
    """Source-derived, because "the helper exists" is not "the study calls it".
    Fails if anyone reintroduces a raw string test on `exit_reason` here."""
    code = _executable_lines(HELPER / 'zebra' / 'debit_sl_study.py')
    assert "'debit_sl' in (t.get('exit_reason')" not in code, \
        'the substring selection is back'
    assert 'outcomes.classify' in code and 'outcomes.DEBIT_SL' in code


def test_the_guard_above_can_still_see_a_real_regression():
    """The inverse review: a filter that strips too much would pass on
    anything. Prove the comment filter drops ONLY comments."""
    defective = ("    dsl = [t for t in book "
                 "if 'debit_sl' in (t.get('exit_reason') or '')]")
    commented = '    # was: ' + defective.strip()
    kept = [l for l in (defective, commented)
            if not l.strip().startswith('#')]
    assert kept == [defective]
    assert "'debit_sl' in (t.get('exit_reason')" in '\n'.join(kept), \
        'the guard must still fire when the defective line is real code'


def test_the_study_reports_reasons_no_reader_understands():
    """An unclassifiable exit makes the DENOMINATOR uncertain, so it cannot be
    dropped in silence - the same rule the digest follows."""
    src = (HELPER / 'zebra' / 'debit_sl_study.py').read_text(encoding='utf-8')
    assert 'unrecognised' in src


# == the live book: the fix must be a no-op on it, today ====================

def test_the_fix_changes_nothing_on_the_current_book():
    """Today every reason is `paper:*`, so old and new must agree exactly. If
    this ever fails, the bridge has started writing real stops - which is the
    arming evidence, and the study's numbers move for a real reason."""
    p = HELPER / 'logs' / 'zebra_trades.json'
    if not p.exists():                       # pragma: no cover - CI without logs
        pytest.skip('no local trade store')
    book = json.loads(p.read_text(encoding='utf-8'))
    ex = [t for t in book if t.get('status') == 'exited']
    old = {t['id'] for t in ex if 'debit_sl' in (t.get('exit_reason') or '')}
    new = {t['id'] for t in _select(book)}
    assert old == new, (
        'old and new disagree on the live book: gained %s, lost %s'
        % (sorted(new - old), sorted(old - new)))
    # SCOPED TO THE STUDY POPULATION, 2026-09-01.
    #
    # This pinned `len(new) == 70` against the WHOLE book, and on 2026-09-01
    # the cohort produced its first two real `paper:debit_sl` exits, taking it
    # to 72. That is the assertion firing for exactly the right reason -- and
    # the fix is to scope it, not to relax it, because the 70 are PRE-COHORT
    # records and the cohort-only rule says they are a different population
    # (`bcs_cohort_only_scope_rule`). Mixing the two is what would corrupt the
    # finding; separating them is what preserves it.
    study = {t['id'] for t in _select(book) if not t.get('cohort')}
    assert len(study) == 70, (
        'the 70 PRE-COHORT debit-SL exits behind bcs_debit_sl_saves_money; if '
        'this moved, the finding it supports needs re-reading, not this '
        'assertion relaxing')


def test_every_reason_in_the_live_book_is_recognised():
    """The study's `unrecognised` warning should be silent today. This is what
    makes that warning trustworthy when it does fire."""
    p = HELPER / 'logs' / 'zebra_trades.json'
    if not p.exists():                       # pragma: no cover - CI without logs
        pytest.skip('no local trade store')
    book = json.loads(p.read_text(encoding='utf-8'))
    unknown = {t.get('exit_reason') for t in book
               if t.get('status') == 'exited'
               and not outcomes.classify(t.get('exit_reason'))['known']}
    assert not unknown, unknown
