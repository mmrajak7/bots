"""One malformed record must not brick every write on a book.

THE DEFECT THIS PINS (found 2026-08-31). `_merge_trades` does
`by_id[t['id']] = t` and compares `t.get('version', 0) > ...` BEFORE any
caller code runs -- and every write path refreshes through the merge. So a
single record with a missing `id`, or a `version` that is a string, made
every `update_trade_fields` / `mark_exited` / `begin_close` on that book raise
`KeyError` or `TypeError`. Exits included: the book could still be read and
alerted on, and could no longer be closed.

`_read_local` quarantined JSON-level corruption but never looked inside the
records, so the file parsed fine, the state survived every restart, and the
corruption alert never fired.

Two manufacture paths, neither a coding error: a hand edit during an incident
(this repo's history has several), and `json.dump(..., default=str)` -- which
both stores pass -- silently stringifying a non-JSON-serialisable numeric so
the NEXT read sees `"id": "419"`.

THE ASYMMETRY. Dropping a record from the working book stops it being
monitored, which for an open position is the worst outcome available. So a
lossless repair is preferred to a drop, and only what cannot be read at all is
held out -- preserved beside the book, never discarded, and alerted.

Run:  cd Helper && python -m pytest common/tests/test_unreadable_records.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import store_contract as sc                          # noqa: E402


# -- the validator itself ---------------------------------------------------

def test_a_clean_book_is_returned_untouched():
    raw = [{'id': 1, 'version': 3}, {'id': 2, 'version': 1}]
    good, bad = sc.partition_readable(raw)
    assert good == raw and bad == []


def test_a_stringified_id_is_repaired_not_dropped():
    """The `default=str` path. Lossless, so repair beats dropping a position."""
    raw = [{'id': '419', 'version': '7', 'status': 'entered'}]
    good, bad = sc.partition_readable(raw)
    assert bad == []
    assert good[0]['id'] == 419 and good[0]['version'] == 7


def test_an_uncomparable_version_becomes_zero_so_it_cannot_win_a_merge():
    """A version that cannot be compared has a SAFE reading, unlike a bad id.

    Treating it as the oldest possible means a good copy on the other side of
    a Drive merge wins, and this one can never overwrite anything.
    """
    raw = [{'id': 5, 'version': 'seven'}]
    good, bad = sc.partition_readable(raw)
    assert bad == []
    assert good[0]['version'] == 0


@pytest.mark.parametrize('bad_id', [None, 'abc', 4.5, {}, [], True, False])
def test_an_unusable_id_is_held_out(bad_id):
    good, bad = sc.partition_readable([{'id': bad_id}])
    assert good == []
    assert len(bad) == 1 and 'not an integer' in bad[0]['why']


def test_a_boolean_id_is_not_read_as_one():
    """`isinstance(True, int)` is True in Python; id True would collide with 1."""
    good, bad = sc.partition_readable([{'id': True}, {'id': 1}])
    assert [t['id'] for t in good] == [1]
    assert len(bad) == 1


def test_a_non_object_is_held_out():
    good, bad = sc.partition_readable(['junk', 42, None, {'id': 1}])
    assert [t['id'] for t in good] == [1]
    assert len(bad) == 3


def test_a_duplicate_id_is_held_out_visibly():
    """Two records with one id cannot both survive a merge keyed on it.

    Letting the merge silently pick is how the loss becomes invisible.
    """
    good, bad = sc.partition_readable([{'id': 1, 'version': 2},
                                       {'id': 1, 'version': 9}])
    assert len(good) == 1 and good[0]['version'] == 2
    assert len(bad) == 1 and 'duplicate id 1' in bad[0]['why']


def test_it_never_raises_on_anything():
    """A validator that can fail on bad data is not a validator."""
    for junk in (None, [], [None], [[]], [{'id': object()}], 'string'):
        good, bad = sc.partition_readable(junk)
        assert isinstance(good, list) and isinstance(bad, list)


def test_the_good_records_satisfy_what_the_merge_assumes():
    """The actual contract: `t['id']` and a comparable `version`."""
    raw = [{'id': '3', 'version': 'x'}, {'id': 1}, 'junk', {'id': None}]
    good, _ = sc.partition_readable(raw)
    by_id = {}
    for t in good:                       # exactly what `_merge_trades` does
        by_id[t['id']] = t
    for t in good:
        assert t.get('version', 0) >= 0


# -- wired into the real stores ---------------------------------------------

@pytest.fixture
def zebra_store(tmp_path, monkeypatch):
    from zebra import config as cfg
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    return ZebraStore, cfg


def _write(cfg, records):
    cfg.LOCAL_FILE.write_text(json.dumps(records), encoding='utf-8')


def test_a_poisoned_zebra_book_can_still_be_written(zebra_store):
    """THE HEADLINE. Pre-fix this raised on every write, exits included."""
    ZebraStore, cfg = zebra_store
    _write(cfg, [
        {'id': 1, 'version': 1, 'status': 'entered', 'stock': 'GOOD'},
        {'id': 'oops', 'version': 1, 'status': 'entered', 'stock': 'BAD'},
    ])
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s._load_local()
    # The write path that a bad record used to brick.
    s.update_trade_fields(1, notes='still writable')
    assert s.find(1)['notes'] == 'still writable'


def test_the_good_records_survive_and_the_bad_one_is_preserved(zebra_store):
    """RETIRES WHEN: the quarantine sidecar stops being a file beside the book
    -- e.g. if unreadable records move into the corruption marker itself, this
    glob has nothing left to find and the assertion belongs there instead."""
    ZebraStore, cfg = zebra_store
    _write(cfg, [
        {'id': 1, 'version': 1, 'status': 'entered', 'stock': 'GOOD'},
        {'id': None, 'stock': 'BAD'},
    ])
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s._load_local()

    assert [t['id'] for t in s._trades] == [1]
    kept = list(cfg.LOG_DIR.glob('*.unreadable.*.json'))
    assert kept, 'an unreadable record must be preserved, never discarded'
    assert json.loads(kept[0].read_text())[0]['record']['stock'] == 'BAD'


def test_it_raises_the_corruption_alert_marker(zebra_store):
    """A log line is not an alert. The monitor turns this marker into one."""
    ZebraStore, cfg = zebra_store
    _write(cfg, [{'id': 1, 'version': 1}, {'id': 'oops'}])
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s._load_local()
    assert (cfg.LOG_DIR / 'zebra_store_corrupt.json').exists()


def test_a_clean_zebra_book_raises_no_marker_and_keeps_everything(zebra_store):
    """Regression guard: the new pass must be invisible on a healthy book."""
    ZebraStore, cfg = zebra_store
    _write(cfg, [{'id': 1, 'version': 1, 'status': 'entered'},
                 {'id': 2, 'version': 4, 'status': 'exited'}])
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s._load_local()
    assert [t['id'] for t in s._trades] == [1, 2]
    assert not (cfg.LOG_DIR / 'zebra_store_corrupt.json').exists()
    assert not list(cfg.LOG_DIR.glob('*.unreadable.*.json'))


def test_the_real_cohort_book_is_clean_today():
    """Guards against shipping a fix that would quarantine live positions.

    Reads the real book on purpose: the validator's whole risk is that it is
    too strict, and the only way to know is to run it over what is actually
    there. Skips where the book is absent, so it never fails on a fresh box.

    RETIRES WHEN: `logs/zebra_trades.json` stops being the cohort book -- if
    the store moves to another format or location, this check must move with
    it rather than be silently satisfied by an absent file.
    """
    book = HELPER / 'logs' / 'zebra_trades.json'
    if not book.exists():                       # not present on the Pi's CI
        pytest.skip('no local book')
    good, bad = sc.partition_readable(json.loads(book.read_text()))
    assert bad == [], 'the live book would lose records: %s' % bad
    assert len(good) > 400
