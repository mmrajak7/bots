"""B7 — a quarantine was inaudible, and the ids it freed got handed out again.

Two defects that compound into one silent data loss:

1. `_read_local` backs up a corrupt file, returns `[]`, and logs one CRITICAL
   line. The monitor cannot tell that from "the book is empty because
   everything closed", so it logged "All trades closed... exiting" and stopped
   watching every open position. The total failure was precisely the case that
   could not report itself.

2. `next_trade_id` was `max(live) + 1`, which is an allocator only while the
   list is COMPLETE. After (1) empties the book it hands out 1, 2, 3 again —
   and `_merge_trades` resolves by `id` with the higher `version` winning, so
   when Drive returns, the recycled id REPLACES the original trade. The
   original is then gone from disk (quarantined) and from Drive (overwritten);
   the only copy left is a `.corrupt.*.json` file nobody knows to open.

Every store-level test runs against all three books, from the roster in
conftest.

Run:  cd Helper && python -m pytest bcs/tests/test_b7_quarantine_and_ids.py -v
"""
import json
import re
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                        # noqa: E402
from bcs.tests.conftest import seed_trades                  # noqa: E402
from bcs.tests.fakes import TelegramSpy                     # noqa: E402

GARBAGE = '{"not": "a list"'          # truncated AND wrong type


# ── B7a: the id allocator survives a quarantine ──────────────────────────────

def test_ids_still_start_at_one_on_a_fresh_book(book):
    """Negative control: the sidecar must not perturb the ordinary case."""
    book.data.write_text('[]', encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()
    assert store.add_trade(book.payload)['id'] == 1


def test_an_id_is_never_reissued_after_a_quarantine(book):
    """The whole bug. Three trades exist, the file is corrupted, the book
    reopens empty — and the next trade must not be #1."""
    book.make(seed_ids=(1, 2, 3))
    book.data.write_text(GARBAGE, encoding='utf-8')

    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()
    assert store.load_trades() == [], "the fixture did not actually quarantine"

    assert store.add_trade(book.payload)['id'] == 4, (
        "the allocator reissued an id the quarantined book already used; when "
        "Drive returns, the merge will resolve both records to one")


def test_the_high_water_mark_only_ever_climbs(book):
    store = book.make(seed_ids=(1, 2, 3))
    store.allocate_id()                          # 4
    store._write_high_water(2)                   # a stale writer
    assert store._read_high_water() == 4


def test_a_missing_sidecar_falls_back_to_the_live_max(book):
    """Advisory, not load-bearing: no sidecar must be no worse than before."""
    store = book.make(seed_ids=(1, 2, 5))
    store._high_water_path().unlink(missing_ok=True)
    assert store.allocate_id() == 6


def test_an_unreadable_sidecar_falls_back_rather_than_raising(book):
    store = book.make(seed_ids=(1, 2))
    store._high_water_path().write_text('}{ not json', encoding='utf-8')
    assert store.allocate_id() == 3


def test_a_sidecar_that_cannot_be_written_does_not_fail_the_trade(book):
    """A trade must never fail to save because bookkeeping could not.

    The sidecar path is made a DIRECTORY rather than stubbing the writer, so
    the real `write_text` really does raise and the real handler really does
    swallow it.
    """
    store = book.make(seed_ids=(1, 2))
    store._high_water_path().unlink(missing_ok=True)
    store._high_water_path().mkdir()

    added = store.add_trade(book.payload)        # must not raise

    assert added['id'] == 3
    assert store._read_high_water() == 0, "the fixture did not block the write"
    assert any(t['id'] == 3 for t in book.read()), "the trade was not saved"


def test_the_sidecar_belongs_to_one_book(book):
    """A directory-wide sequence would make three books share an id space, so
    opening one would silently skip ids in the others."""
    store = book.make()
    assert store._high_water_path().name.startswith(book.stem)
    assert store._high_water_path() != store._data_path()
    assert store._high_water_path().parent == book.tmp


# ── B7b: the quarantine is audible ───────────────────────────────────────────

def test_a_corrupt_file_leaves_a_marker(book):
    book.make(seed_ids=(1, 2))
    book.data.write_text(GARBAGE, encoding='utf-8')

    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()

    marker = store.read_corruption_marker()
    assert marker, "quarantine left no marker; the monitor cannot see it"
    assert marker['store'] == book.data.name
    assert marker['error']
    assert marker['backup'], "the marker must name the backup or it is useless"


def test_the_backup_named_in_the_marker_holds_the_corrupt_bytes(book):
    book.make(seed_ids=(1, 2))
    book.data.write_text(GARBAGE, encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()

    backup = Path(store.read_corruption_marker()['backup'])
    assert backup.exists(), "the marker points at a file that is not there"
    assert backup.read_text(encoding='utf-8') == GARBAGE


def test_a_healthy_book_has_no_marker(book):
    """Negative control: every test above would pass on a store that flagged
    corruption unconditionally."""
    store = book.make(seed_ids=(1, 2))
    assert store.read_corruption_marker() == {}
    assert store.corruption_due_for_alert() == {}


def test_the_alert_re_arms_hourly_and_not_sooner(book):
    book.make(seed_ids=(1,))
    book.data.write_text(GARBAGE, encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()

    assert store.corruption_due_for_alert(), "first look must be due"
    store.note_corruption_alerted()
    assert store.corruption_due_for_alert() == {}, (
        "the cron relaunches every 5 min; this would Telegram 12x an hour")

    marker = store.read_corruption_marker()
    marker['alerted_at'] = time.time() - (store.CORRUPT_REALERT_SEC + 1)
    store._corrupt_marker_path().write_text(json.dumps(marker), encoding='utf-8')
    assert store.corruption_due_for_alert(), "the alert never re-armed"


def test_the_marker_survives_being_alerted(book):
    """The condition is unresolved until a human reads the backup. Clearing
    the marker on alert would make the next cycle look healthy."""
    book.make(seed_ids=(1,))
    book.data.write_text(GARBAGE, encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()
    store.note_corruption_alerted()
    assert store.read_corruption_marker(), "the marker was cleared"


def test_an_unreadable_alerted_stamp_shouts_rather_than_staying_quiet(book):
    book.make(seed_ids=(1,))
    book.data.write_text(GARBAGE, encoding='utf-8')
    store = book.cls(config={'google_drive': {'enabled': False}})
    store.initialize()
    m = store.read_corruption_marker()
    m['alerted_at'] = 'yesterday'
    store._corrupt_marker_path().write_text(json.dumps(m), encoding='utf-8')
    assert store.corruption_due_for_alert(), (
        "a junk timestamp silenced the highest-consequence alert in the system")


# ── The monitor turns a marker into a Telegram ───────────────────────────────

class _FakeStore:
    CORRUPT_REALERT_SEC = 3600

    def __init__(self, marker=None, raises=False):
        self._marker = marker or {}
        self._raises = raises
        self.stamped = 0

    def read_corruption_marker(self):
        if self._raises:
            raise OSError('disk gone')
        return dict(self._marker)

    def corruption_due_for_alert(self):
        return dict(self._marker)

    def note_corruption_alerted(self):
        self.stamped += 1


MARKER = {'at': '2026-08-24T10:00:00', 'store': 'bcs_trades.json',
          'error': 'Expecting value: line 1 column 1', 'backup': '/x/b.json',
          'alerted_at': None}


@pytest.fixture
def spy(monkeypatch):
    return TelegramSpy().install(monkeypatch, sm)


def test_a_flagged_store_alerts_and_reports_true(spy):
    st = _FakeStore(MARKER)
    assert sm.alert_store_corruption([('BCS', st)]) is True
    assert spy.any('QUARANTIN')
    assert spy.any('UNMONITORED'), (
        "the alert must say what it COSTS, not just that a file broke")
    assert spy.any('/x/b.json'), "the alert must name the backup"
    assert st.stamped == 1


def test_healthy_stores_alert_nothing(spy):
    """Negative control: the guard must be conditional."""
    stores = [('BCS', _FakeStore()), ('FH', _FakeStore())]
    assert sm.alert_store_corruption(stores) is False
    assert spy.sent == []


def test_a_flagged_store_still_reports_true_when_the_alert_is_suppressed(spy):
    """Hourly suppression must not make the book look healthy — the CALLER
    uses the return value to decide whether an empty book is benign."""
    st = _FakeStore(MARKER)
    st.corruption_due_for_alert = lambda: {}
    assert sm.alert_store_corruption([('BCS', st)]) is True
    assert spy.sent == []


def test_one_broken_store_does_not_stop_the_others(spy):
    stores = [('BCS', _FakeStore(raises=True)), ('FH', _FakeStore(MARKER))]
    assert sm.alert_store_corruption(stores) is True
    assert spy.any('QUARANTIN')


def test_the_check_never_raises_into_the_monitor(spy):
    """It runs on the path that decides whether to keep monitoring. If the
    alerting code can throw, it can be the thing that stops the loop."""
    assert sm.alert_store_corruption([('BCS', _FakeStore(raises=True))]) is False
    assert sm.alert_store_corruption([('X', object())]) is False


# ── Both exit points are guarded ─────────────────────────────────────────────

def _call_pos(src, name):
    """Position of a real CALL to `name`, ignoring mentions in comments."""
    for m in re.finditer(re.escape(name) + r'\(', src):
        line_start = src.rfind('\n', 0, m.start()) + 1
        if '#' not in src[line_start:m.start()]:
            return m.start()
    return -1


@pytest.mark.parametrize('exit_line', [
    'No open trades and no active watchlist alerts. Nothing to monitor.',
    'All trades closed and no active watchlist alerts. Cron monitor exiting.',
])
def test_every_all_closed_exit_checks_for_a_quarantine_first(exit_line):
    """A source test because the alternative is driving the whole cron loop.

    Both exits treat an empty book as success. Either one reached without the
    check is the original bug, intact.
    """
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert exit_line in src, "the exit line moved; re-anchor this test"
    at_exit = src.index(exit_line)

    # The guard must be the nearest preceding call, within the same block.
    window = src[max(0, at_exit - 1400):at_exit]
    assert _call_pos(window, 'alert_store_corruption') >= 0, (
        f"'{exit_line[:40]}...' can be reached without checking whether the "
        f"book is empty because it was QUARANTINED")


def test_every_call_site_passes_all_three_books():
    """`any` is the wrong quantifier here, and the mutation run proved it.

    The first version of this test asked whether each label appeared in ANY
    call — which stayed green when FH was dropped from the STARTUP call site,
    because the in-loop one still named it. Both sites decide whether to stop
    monitoring; a book missing from either has a silent quarantine on that
    path.
    """
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    sites = [m.start() for m in re.finditer(r'alert_store_corruption\(', src)
             if src[max(0, m.start() - 4):m.start()] != 'def ']
    assert len(sites) == 2, (
        f"expected the two 'empty book' exits to be guarded, found {len(sites)} "
        f"call sites — re-check the wiring, not this test")

    for start in sites:
        depth, i = 0, src.index('(', start)
        for i in range(i, len(src)):
            depth += (src[i] == '(') - (src[i] == ')')
            if depth == 0:
                break
        call = src[start:i + 1]
        for label in ("'BCS'", "'BPS'", "'FH'"):
            assert label in call, (
                f"{label} is missing from this alert_store_corruption call, so "
                f"that book's quarantine is silent on this path: {call}")
