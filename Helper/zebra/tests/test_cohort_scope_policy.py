"""Every read of the whole book is either SCOPED to this engine or says why not.

THE RULE (owner, 2026-08-31): *"the old records are not to be used any more.
in early August we changed significantly and all code rules shud stick to
cohort bcs only. forget old trades and results."*

The engine changed substantially in early August: the back ratio was dropped
for a Bull Call Spread, pricing moved from mid-mid to fill on 2026-08-12,
entry vetting was armed, and the cohort opened 2026-08-14. The 448 records
before that describe a DIFFERENT strategy. Folding them into a number about
this one does not enlarge the sample, it voids the number -- and since every
one of them is mid-priced, it voids it in a known, optimistic direction.

WHY A TEST AND NOT A CONVENTION. `zebra/trade_store.in_cohort` has existed and
been correct all along; the failure was never the predicate, it was the 29
call sites that read `load_trades()` without it. A rule enforced by everyone
remembering is a rule with a half-life. This is the same mechanism as
`common/tests/test_script_safety.py` (`SAFE-TO-RERUN:`) and
`test_source_guard_policy.py` (`RETIRES WHEN:`), and for the same stated
reason: the value is in having THOUGHT about which question this reader is
asking, and the marker is the evidence that somebody did.

THE THREE POPULATIONS, all in `zebra/trade_store.py`:

    scored()     RESULTS -- stamped cohort positions. Anything producing a
                 P&L, a win rate, a scorecard or a gate reads this.
    decided()    DECISIONS -- adds this engine's vetoes and cancels, which
                 never enter and so never carry a stamp. The post-mortem
                 layer needs these; it exists to learn from vetoes.
    in_flight()  LIVE -- what still needs work.

A reader that legitimately needs everything (the store's own accessors, the
recovery sweeps, dedup, capital, the dashboard) says `WHOLE BOOK:` followed by
the reason, in the function.

Run:  cd Helper && python -m pytest zebra/tests/test_cohort_scope_policy.py -v
"""
import ast
import io
import os
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

PACKAGES = ('zebra', 'bcs', 'common')
SKIP_DIRS = {'tests', '_research', '__pycache__'}

MARKER = 'WHOLE BOOK:'

#: Names that scope a population to this engine. `_reportable` is `report.py`'s
#: own one-line wrapper over `scored`, pinned below so it cannot quietly become
#: something else.
SCOPE_CALLS = ('scored(', 'decided(', 'in_flight(', 'in_cohort(',
               'from_current_engine(', '_reportable(', '_cohort_population(')


def _sources():
    for pkg in PACKAGES:
        for root, dirs, names in os.walk(HELPER / pkg):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in sorted(names):
                if n.endswith('.py'):
                    p = Path(root) / n
                    yield p.relative_to(HELPER).as_posix(), p


def _reads():
    """(file, line, function, scoped, marked) for every `.load_trades()` call."""
    out = []
    for rel, path in _sources():
        src = io.open(path, encoding='utf-8').read()
        try:
            tree = ast.parse(src)
        except SyntaxError:                      # pragma: no cover
            continue
        lines = src.splitlines()
        funcs = [(n.lineno, getattr(n, 'end_lineno', n.lineno), n.name)
                 for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == 'load_trades'):
                continue
            # innermost enclosing function
            owner, span = '<module>', (1, len(lines))
            best = -1
            for s, e, name in funcs:
                if s <= node.lineno <= e and s > best:
                    best, owner, span = s, name, (s, e)
            body = '\n'.join(lines[span[0] - 1:span[1]])
            out.append((rel, node.lineno, owner,
                        any(c in body for c in SCOPE_CALLS),
                        MARKER in body))
    return out


# -- the rule ---------------------------------------------------------------

def test_every_whole_book_read_is_scoped_or_declares_why_not():
    """THE RULE. A new unscoped reader fails here rather than in a report."""
    reads = _reads()
    assert reads, 'the AST walk found nothing — this test would pass blindly'
    offenders = [(f, ln, fn) for f, ln, fn, scoped, marked in reads
                 if not (scoped or marked)]
    assert not offenders, (
        'these read the WHOLE trade book without scoping it to this engine.\n'
        'Either narrow the population with scored() / decided() / in_flight()\n'
        'from zebra.trade_store, or add a "%s <why this reader genuinely\n'
        'needs the retired engine\'s records too>" line inside the function:\n%s'
        % (MARKER, '\n'.join('  %s:%d  %s' % o for o in sorted(offenders))))


def test_the_marker_is_a_sentence_not_a_flag():
    """`WHOLE BOOK:` with nothing after it is a box ticked, not a decision."""
    thin = []
    for rel, path in _sources():
        for i, line in enumerate(io.open(path, encoding='utf-8'), 1):
            if MARKER not in line:
                continue
            tail = line.split(MARKER, 1)[1].strip()
            if len(tail) < 15:
                thin.append('%s:%d' % (rel, i))
    assert not thin, (
        'these declare %s without saying why, in the same line:\n  %s'
        % (MARKER, '\n  '.join(thin)))


# -- the populations mean what the rule says they mean ----------------------

def test_scored_is_stamped_positions_only():
    from zebra.trade_store import scored
    book = [
        {'id': 1, 'status': 'exited', 'cohort': '2026-08-14'},
        {'id': 2, 'status': 'entered', 'cohort': '2026-08-14'},
        {'id': 3, 'status': 'exited'},                       # legacy position
        {'id': 4, 'status': 'cancelled', 'signal_date': '2026-08-20'},
        {'id': 5, 'status': 'watching', 'signal_date': '2026-08-26'},
    ]
    assert [t['id'] for t in scored(book)] == [1, 2]


def test_decided_adds_this_engines_vetoes_but_not_the_old_ones():
    """A veto never enters, so it never carries a stamp. It is still ours."""
    from zebra.trade_store import decided
    book = [
        {'id': 1, 'status': 'exited', 'cohort': '2026-08-14'},
        {'id': 2, 'status': 'cancelled', 'signal_date': '2026-08-20'},
        {'id': 3, 'status': 'cancelled', 'signal_date': '2026-05-12'},
        {'id': 4, 'status': 'exited'},                       # legacy position
    ]
    assert [t['id'] for t in decided(book)] == [1, 2]


def test_an_unstamped_position_is_legacy_however_it_is_dated():
    """`in_cohort` says absence IS the answer for a position. Unchanged.

    A date test may only be applied where no stamp could ever have existed.
    """
    from zebra.trade_store import from_current_engine
    assert from_current_engine(
        {'id': 9, 'status': 'entered', 'signal_date': '2026-08-30'}) is False


def test_the_report_scope_is_not_a_config_option():
    """`_reportable` deferred to `alerts_cohort_only` until 2026-08-31.

    A switch that can widen a P&L report back over the retired engine makes
    cohort-only a default, not a rule.

    RETIRES WHEN: `_reportable` stops existing, i.e. reports take their
    population from the caller rather than narrowing it themselves.
    """
    import inspect
    from zebra import report
    src = inspect.getsource(report._reportable)
    assert 'ALERTS_COHORT_ONLY' not in src
    assert 'scored(' in src


def test_the_digest_scopes_at_the_source():
    """The digest is the paper run's dated record AND the arming gate reads it.

    RETIRES WHEN: `digest.build` stops reading the store directly.
    """
    import inspect
    from zebra import digest
    src = inspect.getsource(digest.build)
    assert 'decided(store.load_trades())' in src


def test_the_postmortem_layer_uses_decided_not_scored():
    """Scoping post-mortems to stamped records drops every veto, which rebuilds
    the exact bias the module exists to prevent.

    RETIRES WHEN: the cohort stamp is applied at signal time, at which point
    `scored` and `decided` agree and the distinction is unnecessary.
    """
    import inspect
    from zebra import postmortem
    for fn in (postmortem.pending, postmortem.precedents):
        assert 'decided(' in inspect.getsource(fn), fn.__name__


# -- and it is really true of the live book ---------------------------------

def test_the_real_book_narrows_to_the_cohort():
    """RETIRES WHEN: `logs/zebra_trades.json` stops being the cohort book."""
    import json
    from zebra.trade_store import scored, decided
    book_path = HELPER / 'logs' / 'zebra_trades.json'
    if not book_path.exists():
        pytest.skip('no local book')
    book = json.loads(book_path.read_text())
    s, d = scored(book), decided(book)
    assert len(book) > 400, 'the legacy records are still on disk, as intended'
    assert len(s) < 50, 'scored() is not narrowing — %d of %d' % (len(s),
                                                                 len(book))
    assert all(t.get('cohort') for t in s)
    assert len(d) >= len(s), 'decisions are a superset of results'
    assert all(t.get('status') != 'exited' or t.get('cohort') for t in d), (
        'a legacy exited position leaked into the decision population')
