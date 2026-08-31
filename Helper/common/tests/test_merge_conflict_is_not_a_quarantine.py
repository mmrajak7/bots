"""A conflict is not a quarantine, and a cache is not a replica.

THE DEFECT THIS PINS (2026-08-31, the split-brain detector's FIRST live
session on the Pi). Commit `1a6d403` added a same-version-different-content
detector and wired it to `_flag_corruption` -- the marker that already existed
for the QUARANTINE path. Three things went wrong at once:

  1. THE ALERT LIED. The monitor's Telegram text is hardcoded for a
     quarantine: "failed to parse", "was quarantined", "may have restarted
     EMPTY", "exit monitoring is off on every open position", "restore from
     the backup". Every clause was false. The book had 466 records, parsed
     fine, agreed with Drive on 465 of them, and all seven positions were
     being polled in the same log. `backup: None` was not a missing backup --
     it was the tell that no quarantine had happened.

  2. IT FIRED ON A CACHE REFRESH. `_mutate` and `_sync_from_drive` both merge
     THIS replica's disk against THIS process's own cache before touching
     anything. `_mutate`'s docstring already documents two writer processes by
     design (the zebra cron and the Claude vet/review/postmortem CLIs it
     spawns), so a version tie there means a sibling wrote while we held a
     cache -- exactly what the refresh exists to absorb. Calling that "BOTH
     replicas" is a category error. 11 CRITICAL lines and 4 false alerts in
     one morning against a healthy book.

  3. THE NOTE NAMED NO FIELDS. Diagnosing the first occurrence meant
     downloading the Drive copy by hand to learn the argument was over one
     `review` key.

WHY THIS IS NOT COSMETIC. This repo has already paid for a warning that cried
wolf: the per-leg bid-ask flag fired on 68% of trades, carried no signal, and
its only real effect was "training the reader to ignore the warning marker,
which is how the OI flag on COCHINSHIP got waved through" (CLAUDE.md). A
CRITICAL that is false in every clause is the same mistake with the alarm that
means the stops are dead.

The RETURN VALUE of `bcs.spread_monitor.alert_store_corruption` matters for
the same reason: callers read a True as "this book may have gone empty
underneath us" and use it to refuse to exit and to blame a quarantine. A
conflict must alert without claiming that.

Run:  cd Helper && python -m pytest \
        common/tests/test_merge_conflict_is_not_a_quarantine.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402

Z = sc.ZEBRA_STATUSES


def _rec(tid=1, status='entered', version=1, **extra):
    r = {'id': tid, 'status': status, 'version': version}
    r.update(extra)
    return r


# -- (2) a cache is not a replica -------------------------------------------

def test_a_version_tie_between_two_replicas_is_still_announced():
    """The genuine split brain must NOT be silenced by this fix."""
    a = _rec(version=3, note='local')
    b = _rec(version=3, note='drive')
    winner, note = sc.resolve_merge(a, b, Z)
    assert winner is a
    assert note and 'BOTH replicas' in note


def test_a_version_tie_on_the_cache_refresh_says_nothing():
    """THE DEFECT. Disk vs this process's own cache is not a divergence.

    Pre-fix this produced a CRITICAL line and a corruption alert every time a
    sibling writer touched a record mid-cycle -- which is by design.
    """
    a = _rec(version=3, note='disk')
    b = _rec(version=3, note='cache')
    winner, note = sc.resolve_merge(a, b, Z, same_replica=True)
    assert winner is a, 'resolution must be unchanged; only the note goes'
    assert note is None


def test_same_replica_does_not_silence_the_reopened_exit():
    """The money case is still announced on the refresh path.

    `same_replica` suppresses ONE branch. A booked exit being outrun by a
    version counter is a real defect wherever it is seen, and the refresh path
    is not exempt from it.
    """
    local_closed = _rec(status='exited', version=7, exit_reason='paper:tp')
    stale_open = _rec(status='entered', version=8)
    winner, note = sc.resolve_merge(local_closed, stale_open, Z,
                                    same_replica=True)
    assert winner is local_closed
    assert note and 'not undone by a version counter' in note


def test_same_replica_defaults_to_false():
    """A caller that does not think about it gets the loud, safe behaviour."""
    winner, note = sc.resolve_merge(_rec(version=2, x=1),
                                    _rec(version=2, x=2), Z)
    assert note is not None


# -- (3) the note names the fields ------------------------------------------

def test_the_conflict_note_names_the_differing_fields():
    """'different content' with no diff is unactionable."""
    _, note = sc.resolve_merge(_rec(version=3, review='a', spot=1),
                               _rec(version=3, review='b', spot=1), Z)
    assert 'review' in note
    assert 'spot' not in note, 'fields that AGREE must not be listed'


def test_diff_keys_reports_a_field_present_on_only_one_side():
    assert 'review' in sc.diff_keys({'id': 1}, {'id': 1, 'review': 'x'})


def test_diff_keys_caps_a_runaway_list():
    a = {'k%d' % i: i for i in range(40)}
    b = {'k%d' % i: i + 1 for i in range(40)}
    out = sc.diff_keys(a, b)
    assert 'more' in out
    assert len(out) < 300, 'a note is a Telegram line, not a full diff'


def test_diff_keys_never_raises():
    assert sc.diff_keys(None, {'id': 1})
    assert sc.diff_keys({'id': 1}, None)


# -- (1) the two marker kinds are distinct ----------------------------------

def test_the_two_marker_kinds_are_not_the_same_string():
    assert sc.MARKER_QUARANTINE != sc.MARKER_MERGE_CONFLICT


class _Store:
    """Minimal concrete `LockedStore` so `_flag_corruption` can be driven."""

    def __init__(self, tmp_path):
        self._path = tmp_path / 'book.json'

    def _data_path(self):
        return self._path

    def _lock_path(self):
        return self._path.with_suffix('.lock')


def _marker_for(tmp_path, **kw):
    from common.locked_store import LockedStoreMixin

    class S(_Store, LockedStoreMixin):
        pass

    s = S(tmp_path)
    s._flag_corruption('boom', None, **kw)
    return json.loads(s._corrupt_marker_path().read_text(encoding='utf-8'))


def test_flag_corruption_defaults_to_quarantine(tmp_path):
    """Back-compat: every existing caller means quarantine, and so did every
    marker written before the kind existed."""
    assert _marker_for(tmp_path)['kind'] == sc.MARKER_QUARANTINE


def test_flag_corruption_records_the_merge_conflict_kind(tmp_path):
    m = _marker_for(tmp_path, kind=sc.MARKER_MERGE_CONFLICT)
    assert m['kind'] == sc.MARKER_MERGE_CONFLICT


# -- (1) the alert text matches what happened -------------------------------

@pytest.fixture
def msg():
    from zebra.monitor import _store_corruption_message
    return _store_corruption_message


#: The claims that are TRUE of a quarantine and FALSE of a merge conflict.
#: Every one of these was sent on 2026-08-31 about an intact book.
_QUARANTINE_ONLY = ('failed to parse', 'quarantined', 'EMPTY',
                    'monitoring is off', 'Restore from')


def test_the_quarantine_alert_still_says_the_catastrophic_thing(msg):
    out = msg({'at': 'T', 'error': 'e', 'backup': '/tmp/b.json',
               'kind': sc.MARKER_QUARANTINE})
    for claim in _QUARANTINE_ONLY:
        assert claim in out


def test_a_marker_with_no_kind_is_read_as_a_quarantine(msg):
    """Markers written before the split genuinely were quarantines."""
    out = msg({'at': 'T', 'error': 'e', 'backup': 'None'})
    assert 'failed to parse' in out


def test_the_merge_conflict_alert_claims_none_of_it(msg):
    """THE DEFECT, stated as the assertion that would have caught it."""
    out = msg({'at': 'T', 'error': '6 merge conflict(s): ...',
               'backup': 'None', 'kind': sc.MARKER_MERGE_CONFLICT})
    for claim in _QUARANTINE_ONLY:
        assert claim not in out, (
            'the conflict alert repeated the quarantine claim %r about a book '
            'that is intact' % claim)
    assert 'INTACT' in out
    assert 'merge conflict' in out.lower()


def test_the_merge_conflict_alert_does_not_send_the_reader_to_a_backup(msg):
    """`backup: None` on a conflict is not a missing backup -- nothing was
    quarantined, so there is nothing to restore and saying so wastes the one
    reading it during an incident."""
    out = msg({'at': 'T', 'error': 'x', 'backup': 'None',
               'kind': sc.MARKER_MERGE_CONFLICT})
    assert 'backup' not in out.lower() or 'no backup is needed' in out.lower()


def test_the_alert_escapes_html_in_both_kinds(msg):
    """The body is sent with HTML parse mode; an unescaped `<` is a silent
    Telegram 400. See CLAUDE.md on the HTML-escape 400 that vanished."""
    for kind in (sc.MARKER_QUARANTINE, sc.MARKER_MERGE_CONFLICT):
        out = msg({'at': 'T', 'error': 'a <b> & c', 'backup': 'n',
                   'kind': kind})
        assert '&lt;b&gt;' in out and 'a <b>' not in out


# -- the BCS monitor: alert, but do not claim the book went empty -----------

@pytest.fixture
def sm_spy(monkeypatch):
    """The BCS monitor's Telegram, captured. Imported lazily: `spread_monitor`
    is a heavy module and the contract tests above must not depend on it."""
    import bcs.spread_monitor as sm
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sm, 'log', lambda *a, **k: None)
    return sm, sent


class _FakeStore:
    """Mirrors `bcs/tests/test_b7_quarantine_and_ids.py::_FakeStore`."""
    CORRUPT_REALERT_SEC = 3600

    def __init__(self, marker=None):
        self._marker = marker or {}
        self.stamped = 0

    def read_corruption_marker(self):
        return dict(self._marker)

    def corruption_due_for_alert(self):
        return dict(self._marker)

    def note_corruption_alerted(self):
        self.stamped += 1


def _marker(kind=None):
    m = {'at': '2026-08-31T09:25:52', 'store': 'zebra_trades.json',
         'error': '6 merge conflict(s): record #423 ...', 'backup': 'None',
         'alerted_at': None}
    if kind:
        m['kind'] = kind
    return m


def test_a_quarantine_still_reports_true(sm_spy):
    """Negative control. The caller uses True to refuse to exit on an empty
    book; a real quarantine must keep doing that."""
    sm, sent = sm_spy
    st = _FakeStore(_marker(sc.MARKER_QUARANTINE))
    assert sm.alert_store_corruption([('BCS', st)]) is True
    assert any('QUARANTIN' in m for m in sent)


def test_a_marker_with_no_kind_still_reports_true(sm_spy):
    """Back-compat: pre-split markers were quarantines."""
    sm, _ = sm_spy
    assert sm.alert_store_corruption([('BCS', _FakeStore(_marker()))]) is True


def test_a_merge_conflict_alerts_but_reports_false(sm_spy):
    """THE DEFECT at the caller. A True here makes the monitor log 'Book is
    empty because a store was QUARANTINED' about an intact, merely-quiet book,
    and refuse to exit for a reason that never happened."""
    sm, sent = sm_spy
    st = _FakeStore(_marker(sc.MARKER_MERGE_CONFLICT))
    assert sm.alert_store_corruption([('BCS', st)]) is False
    assert sent, 'a conflict must still be reported — silence is not the fix'
    assert any('INTACT' in m for m in sent)
    assert not any('UNMONITORED' in m for m in sent)
    assert st.stamped == 1, 'and it must still disarm, or it repeats hourly'


def test_a_real_quarantine_beside_a_conflict_still_reports_true(sm_spy):
    """One intact book must not mask another that genuinely went empty."""
    sm, _ = sm_spy
    stores = [('ZEBRA', _FakeStore(_marker(sc.MARKER_MERGE_CONFLICT))),
              ('BCS', _FakeStore(_marker(sc.MARKER_QUARANTINE)))]
    assert sm.alert_store_corruption(stores) is True


# -- writes that deliberately skip the version bump -------------------------
#
# FOUND IN PRODUCTION 2026-08-31, after the first fix went live: the alert was
# now correct in its wording and still firing every cycle, on
# `corrob_spot, corrob_t, corrob_value, exit_depth`.
#
# Those are `apply_mfe`'s batched poll fields. It writes them LOCAL ONLY and
# deliberately does NOT bump the version — pushing a peak to Drive every five
# minutes would churn the network for data nobody reads until the trade
# closes, and they ride along on the next versioned write. So for every open
# position, on every poll, local disk and Drive hold the SAME version with
# DIFFERENT content. That is the tie condition exactly.
#
# The detector could not tell that from a split brain, so it reported one per
# position per cycle — which is the same drowning problem the first fix was
# about, arriving by a different route.

_UNV = frozenset({'corrob_spot', 'corrob_value', 'corrob_t', 'exit_depth'})
_PRE = ('mfe_',)


def _tie(**incoming):
    base = _rec(version=17, corrob_spot=100.0, mfe_peak=5.0, stock='X')
    inc = dict(base)
    inc.update(incoming)
    return sc.resolve_merge(base, inc, Z, unversioned_fields=_UNV,
                            unversioned_prefixes=_PRE)


def test_a_tie_only_on_batched_poll_fields_is_not_a_conflict():
    """THE DEFECT. Fired once per open position per cycle, forever."""
    _, note = _tie(corrob_spot=101.5, corrob_t=123.0)
    assert note is None


def test_a_tie_only_on_mfe_fields_is_not_a_conflict():
    """Same write, same reason — matched by prefix rather than by name."""
    _, note = _tie(mfe_peak=6.0)
    assert note is None


def test_the_local_copy_wins_so_the_fresher_poll_data_survives():
    """Base is the local disk on the Drive merge, and it holds the poll data
    that has not been pushed yet. Silencing the note must not hand the record
    to the stale side."""
    base = _rec(version=17, corrob_spot=100.0)
    winner, _ = sc.resolve_merge(base, _rec(version=17, corrob_spot=99.0), Z,
                                 unversioned_fields=_UNV,
                                 unversioned_prefixes=_PRE)
    assert winner is base


def test_a_real_field_differing_alongside_them_is_still_a_conflict():
    """The exemption is for ties confined to the unversioned set. One real
    field in the diff and it is a divergence again — otherwise a genuine split
    brain hides behind a corroboration timestamp."""
    _, note = _tie(corrob_spot=101.5, debit=99.9)
    assert note is not None
    assert 'debit' in note


def test_the_exemption_is_off_by_default():
    """A caller that passes no allowlist gets the loud behaviour. The BCS
    family has no batched write and must not inherit this."""
    _, note = sc.resolve_merge(_rec(version=17, corrob_spot=1.0),
                               _rec(version=17, corrob_spot=2.0), Z)
    assert note is not None


def test_identical_records_are_not_classed_as_unversioned():
    """`_only_unversioned` must answer False when nothing differs, or an
    ordinary equal pair takes the wrong branch."""
    assert sc._only_unversioned(_rec(), _rec(), _UNV, _PRE) is False


def test_an_empty_prefix_tuple_does_not_match_everything():
    """`str.startswith(())` is False for every string, but an accidental
    `('',)` matches ALL of them and would silence every conflict."""
    _, note = sc.resolve_merge(_rec(version=1, debit=1.0),
                               _rec(version=1, debit=2.0), Z,
                               unversioned_fields=frozenset(),
                               unversioned_prefixes=())
    assert note is not None


def test_the_zebra_store_passes_its_own_batched_allowlist():
    """One source of truth: the set the merge exempts must BE the set
    `apply_mfe` permits, or a field added to the batched write starts a false
    alarm the next time it is polled.

    RETIRES WHEN: the batched-write allowlist and the merge exemption are the
    same named constant by construction rather than by this assertion.
    """
    from zebra.trade_store import _BATCHED_POLL_FIELDS
    assert {'corrob_spot', 'corrob_value', 'corrob_t',
            'exit_depth'} <= _BATCHED_POLL_FIELDS
