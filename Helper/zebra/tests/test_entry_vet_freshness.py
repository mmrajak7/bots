"""An entry verdict is about the book in front of us, not a standing permit.

THE DEFECT, from production on 2026-09-01. Signal #472 ANGELONE:

    triggered   2026-08-31 14:05:19
    vetted      2026-08-31 14:07:06   (decision #109, verdict `allow`)
    ...sat at `triggered` overnight...
    ENTERED     2026-09-01 13:50:17   -- on a verdict 23h45m old

The alert said "Vetted by Claude (decision #109)" and it was true, which is
what made it hard to see. `_marker_fresh` bounds a TERMINAL verdict on the
EXIT side and there was no equivalent here at all: an `allowed` entry marker
never expired.

VETTING.md already states the discipline -- "Your verdict covers this episode,
not this trade... An `allow` is never a standing permission" -- about exits.
Owner's ruling, 2026-09-01: *"vet has to be realtime"*. It applies to entries.

WHAT THIS IS NOT ABOUT. The mechanical gates (d/w <= 45%, entry cost <= 15%,
OI, DTE) are re-run against a fresh book at entry, and they were: #472 entered
at a freshly computed d/w of 39.5%. Price drift was always caught. What was
stale is the AGENT's judgement -- and its own recorded red flag was
"only one lot sitting at the short leg's best bid RIGHT NOW", a statement about
a book twenty-four hours gone.

EXPIRY RE-REQUESTS, NEVER VETOES. Clearing the marker returns the record to
the `state is None` path the gate already handles, so a stale opinion costs one
re-vet and never a lost signal. A missed entry costs nothing; an unqualified
one costs capital.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_entry_vet_freshness.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                                  # noqa: E402
from zebra import vet                                            # noqa: E402

NOW = datetime(2026, 9, 1, 13, 50, 17)


def _rec(minutes_old, state='allowed', **extra):
    v = {'state': state,
         'decided_at': (NOW - timedelta(minutes=minutes_old)).isoformat()}
    v.update(extra)
    return {'id': 472, 'stock': 'ANGELONE', 'status': 'triggered', 'vet': v}


# ── the production case ────────────────────────────────────────────────────

def test_the_real_472_verdict_is_expired():
    """23h45m. THE DEFECT, to the minute."""
    assert vet.entry_allow_expired(_rec(23 * 60 + 45), now=NOW) is True


def test_the_normal_path_is_untouched():
    """trigger -> agent answers in 2-5 min -> enter on the next 5-minute
    cycle. A TTL that fired here would re-vet every entry and burn the quota
    the layer runs on."""
    for age in (0, 2, 5, 10, 20):
        assert vet.entry_allow_expired(_rec(age), now=NOW) is False, age


def test_the_boundary_is_the_configured_ttl():
    ttl_min = cfg.ENTRY_VET_TTL_SEC // 60
    assert vet.entry_allow_expired(_rec(ttl_min - 1), now=NOW) is False
    assert vet.entry_allow_expired(_rec(ttl_min + 1), now=NOW) is True


def test_it_cannot_survive_a_session_boundary():
    """The case that actually matters: a verdict from yesterday."""
    assert vet.entry_allow_expired(_rec(18 * 60), now=NOW) is True


# ── only an ALLOW is aged out ──────────────────────────────────────────────

@pytest.mark.parametrize('state', ['vetoed', 'queued', 'pending',
                                   'unavailable', 'starved', 'abandoned'])
def test_no_other_state_is_touched(state):
    """A VETO must not age into a re-vet -- that would turn a refusal into a
    retry loop and eventually into an entry. Every other state has its own
    bound (`drop_after`, the deadline, the queue) and this must not disturb
    them."""
    assert vet.entry_allow_expired(_rec(99999, state=state), now=NOW) is False


def test_a_record_with_no_marker_is_not_expired():
    """`state is None` already means "never requested"; reporting it as
    expired would log a discard that never happened."""
    assert vet.entry_allow_expired({'id': 1}, now=NOW) is False
    assert vet.entry_allow_expired({'id': 1, 'vet': None}, now=NOW) is False


def test_a_corrupt_marker_is_not_expired():
    """`vet_state` treats a corrupt marker as never-requested so one bad
    record cannot kill the watching loop. This must agree with it."""
    assert vet.entry_allow_expired({'id': 1, 'vet': 'nonsense'}, now=NOW) is False
    assert vet.entry_allow_expired('not a dict', now=NOW) is False


def test_an_ALLOW_with_no_readable_timestamp_is_STALE():
    """Entries fail closed: "cannot be shown fresh" is stale, not fresh."""
    assert vet.entry_allow_expired(
        {'id': 1, 'vet': {'state': 'allowed'}}, now=NOW) is True
    assert vet.entry_allow_expired(
        {'id': 1, 'vet': {'state': 'allowed', 'decided_at': 'junk'}},
        now=NOW) is True


def test_requested_at_is_accepted_when_decided_at_is_absent():
    """Older markers predate `decided_at`; they must age, not be discarded
    outright as unreadable."""
    r = {'id': 1, 'vet': {'state': 'allowed',
                          'requested_at': (NOW - timedelta(minutes=5)).isoformat()}}
    assert vet.entry_allow_expired(r, now=NOW) is False


# ── clearing it re-requests rather than losing the signal ──────────────────

class _Store:
    def __init__(self, trade):
        self.t = trade

    def find(self, tid):
        return self.t

    def _must_find(self, tid):
        return self.t

    def _mutate(self, **kw):
        import contextlib
        return contextlib.nullcontext()


def test_clearing_puts_the_record_back_on_the_request_path():
    """`state is None` is the path the gate already handles: it requests a
    fresh vet, and every bound applies again from the start."""
    t = _rec(23 * 60 + 45)
    store = _Store(t)
    assert vet.clear_entry_vet(store, 472, 'too old') is True
    assert t['vet'] is None
    assert vet.vet_state(t) is None


def test_clearing_keeps_an_audit_trail():
    """A discarded verdict that leaves no trace is indistinguishable from one
    that was never asked for."""
    t = _rec(23 * 60 + 45)
    vet.clear_entry_vet(_Store(t), 472, 'too old')
    hist = t['vet_expired_history']
    assert len(hist) == 1
    assert hist[0]['was'] == 'allowed' and 'too old' in hist[0]['why']


def test_the_audit_trail_is_bounded():
    """A signal that re-vets many times must not grow the record without
    limit -- the store is synced to Drive on every versioned write."""
    t = _rec(60)
    store = _Store(t)
    for _ in range(12):
        t['vet'] = {'state': 'allowed',
                    'decided_at': (NOW - timedelta(hours=2)).isoformat()}
        vet.clear_entry_vet(store, 472, 'too old')
    assert len(t['vet_expired_history']) <= 5


def test_a_store_failure_does_not_enter_on_the_stale_verdict():
    """`clear_entry_vet` returns False and the caller must `continue`. The
    gate is wired to do exactly that."""
    class _Bad(_Store):
        def _mutate(self, **kw):
            raise RuntimeError('lock timeout')
    assert vet.clear_entry_vet(_Bad(_rec(999)), 472, 'too old') is False


def test_the_gate_actually_calls_it():
    """A guard nothing calls is decorative -- the repo's own rule. Pins the
    wiring, not just the helper.

    RETIRES WHEN: the entry gate consults one `entry_gate()` policy function
    the way the exit side consults `exit_gate`, so freshness cannot be skipped
    at a call site.
    """
    import ast
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.check_watching)
    assert 'clear_entry_vet' in src, (
        'the entry gate no longer discards a stale verdict')

    # THE CALL MUST BE THE CONDITION, not merely present in the file. A
    # substring check passes `if False and entry_allow_expired(...)`, which is
    # exactly how this test was first written and exactly what the negative
    # control caught -- a guard that reads as wired and decides nothing.
    tree = ast.parse(src)          # module-level def: already at column 0
    calls_in_tests = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            for sub in ast.walk(node.test):
                if isinstance(sub, ast.Call) and \
                        getattr(sub.func, 'attr', None) == 'entry_allow_expired':
                    # A `False and ...` conjunction neuters it.
                    consts = [v for v in ast.walk(node.test)
                              if isinstance(v, ast.Constant) and v.value is False]
                    calls_in_tests.append(not consts)
    assert calls_in_tests, (
        'entry_allow_expired is not the test of any `if` in the entry gate')
    assert any(calls_in_tests), (
        'the freshness check is short-circuited by a constant — it is wired '
        'in name only')
