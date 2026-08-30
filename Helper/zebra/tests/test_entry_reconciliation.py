"""A signal that has already been to the order path does not go again.

THE DEFECT THIS PINS (found 2026-08-31). Every failure branch in
`_auto_enter_bcs` records an entry residue, and NOTHING read one back before
placing. So a failure AFTER the fills -- `mark_entered_bcs` raising, or the
debit coming back unpriceable -- left the signal at `triggered` with its vet
verdict still ALLOWED and its ticket claim unset, and the next five-minute
cron cycle placed ANOTHER FULL SPREAD. One per cycle until the order cutoff or
the kill switch, and none of them visible to `capital.check`, because none was
ever recorded as a position.

The same class covers a hard crash between the broker fill and the store
write: the order journal holds an intent with no result, and nothing on the
zebra entry path consulted it.

WHY TWO SOURCES. The failure being guarded against is a STORE that would not
write. A guard reading only the store is therefore blind in exactly the case
it exists for, so the order journal -- written to disk BEFORE the broker call
-- is checked as well.

Run:  cd Helper && python -m pytest zebra/tests/test_entry_reconciliation.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import order_journal                                     # noqa: E402
from zebra import config as cfg                                   # noqa: E402
from zebra import monitor                                         # noqa: E402


@pytest.fixture
def journal(tmp_path, monkeypatch):
    """Point the order journal at a scratch directory."""
    monkeypatch.setattr(order_journal, 'JOURNAL_DIR', tmp_path,
                        raising=False)
    # Honours `day`, so the "yesterday does not block forever" test really
    # reads a different file rather than the same one twice.
    monkeypatch.setattr(
        order_journal, 'journal_path',
        lambda day=None: tmp_path / ('orders_%s.jsonl' % (day or 'today')))
    return tmp_path


def _signal(**over):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'triggered'}
    t.update(over)
    return t


class _Store:
    """Only the two methods the gate touches."""

    def __init__(self):
        self.flags = set()

    def set_alert_flag_daily(self, tid, name):
        key = (tid, name)
        if key in self.flags:
            return False
        self.flags.add(key)
        return True


# -- the residue source -----------------------------------------------------

def test_a_clean_signal_is_not_blocked(journal):
    assert monitor._entry_already_in_flight(_Store(), _signal()) is None


def test_an_open_entry_residue_blocks(journal):
    why = monitor._entry_already_in_flight(
        _Store(), _signal(entry_residue={'state': 'open',
                                         'why': 'short leg did not fill'}))
    assert why and 'residue is still OPEN' in why
    assert 'short leg did not fill' in why, 'the operator needs the reason'


def test_a_resolved_residue_does_not_block(journal):
    """Resolution is what un-blocks it. A permanent block is its own outage."""
    assert monitor._entry_already_in_flight(
        _Store(), _signal(entry_residue={'state': 'resolved'})) is None


# -- the journal source -----------------------------------------------------

def test_an_unresolved_intent_blocks(journal):
    order_journal.record_intent(
        symbol='TESTCO26SEP1000CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=False,
        context={'trade_id': 7, 'leg': 'long'})
    why = monitor._entry_already_in_flight(_Store(), _signal())
    assert why and 'no recorded result' in why
    assert 'TESTCO26SEP1000CE' in why


def test_an_intent_for_a_DIFFERENT_signal_does_not_block(journal):
    order_journal.record_intent(
        symbol='OTHER26SEP900CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=False,
        context={'trade_id': 999, 'leg': 'long'})
    assert monitor._entry_already_in_flight(_Store(), _signal()) is None


def test_a_dry_run_intent_does_not_block(journal):
    """Nothing was placed, so nothing can be live at the broker."""
    order_journal.record_intent(
        symbol='TESTCO26SEP1000CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=True, context={'trade_id': 7})
    assert monitor._entry_already_in_flight(_Store(), _signal()) is None


def test_a_resolved_intent_does_not_block(journal):
    iid = order_journal.record_intent(
        symbol='TESTCO26SEP1000CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=False, context={'trade_id': 7})
    order_journal.record_result(iid, order_id='O1')
    assert monitor._entry_already_in_flight(_Store(), _signal()) is None


def test_a_string_trade_id_in_the_context_still_matches(journal):
    """It has been written both ways; a type must not defeat the guard."""
    order_journal.record_intent(
        symbol='TESTCO26SEP1000CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=False, context={'trade_id': '7'})
    assert monitor._entry_already_in_flight(_Store(), _signal()) is not None


# -- fail closed ------------------------------------------------------------

def test_an_unreadable_journal_refuses_the_entry(journal, monkeypatch):
    """An entry not placed costs one signal; a duplicate costs a position."""
    def boom(*a, **k):
        raise OSError('disk gone')
    monkeypatch.setattr(order_journal, 'unresolved_for_trade', boom)
    why = monitor._entry_already_in_flight(_Store(), _signal())
    assert why and 'could not be read' in why


def test_an_unreadable_residue_field_refuses_the_entry(journal):
    """A residue field of the wrong type must not be read as absent."""
    class Weird(dict):
        def get(self, k, default=None):
            raise RuntimeError('nope')
    why = monitor._entry_already_in_flight(_Store(), Weird())
    assert why and 'could not be read' in why


# -- yesterday is not today -------------------------------------------------

def test_yesterdays_unresolved_intent_does_not_block_forever(journal):
    """Kite regular orders are DAY orders.

    An intent left unresolved by yesterday's crash names an order that no
    longer exists. Blocking on it would turn a duplicate guard into a silent,
    unbounded refusal to trade this signal ever again.
    """
    order_journal.record_intent(
        symbol='TESTCO26SEP1000CE', txn_type='BUY', qty=100, price=30.0,
        exchange='NFO', dry_run=False, context={'trade_id': 7})
    # `unresolved_for_trade` reads TODAY's file; a different day reads another.
    assert order_journal.unresolved_for_trade(7) != []
    assert order_journal.unresolved_for_trade(7, day='20200101') == []
