"""A consume-once exit claim must not outlive the process that made it.

THE DEFECT (found 2026-08-31). `_claim_exit_alert` persists the claim and the
booking happens after it, with a Telegram POST of up to ten seconds in between.
Every IN-PROCESS failure across that window already gives the claim back --
`_paper_auto_close`'s defer paths, `_send_exit_alert`'s failed-send path. What
nothing covered was the process CEASING between the two: a SIGKILL, an OOM,
power loss, or 15:30 arriving mid-cycle. zebra's cron process is one-shot and
exits between cycles, so there is no in-memory state left to notice; the claim
is durable and the release was not.

For `debit_sl` that silently disarms the position's ONLY loss-side stop for the
rest of its life -- `spot_sl_enabled` is False, so on this book the value stops
are all there is. Every later cycle's `set_alert_flag` returns False and
short-circuits the branch, and the position rides to max loss or expiry with
nothing logged. "Protection that looks armed and is not" is the shape of both
real-money incidents.

WHY THE INFERENCE IS SOUND rather than a heuristic: a claim can only have been
made by a cycle that was about to book, a booking leaves the record `exited`,
and every in-process failure releases. So `entered` + a claim older than a
couple of cycles is, by construction, a claim whose holder is gone.

Run:  cd Helper && python -m pytest zebra/tests/test_stranded_exit_claim.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                                  # noqa: E402
from zebra import monitor as mon                                 # noqa: E402


class _Store:
    """Records which claims were handed back."""

    def __init__(self, raises=False):
        self.cleared = []
        self.raises = raises

    def clear_alert_flag(self, trade_id, kind, persist=True):
        if self.raises:
            raise RuntimeError('store is locked')
        self.cleared.append((trade_id, kind))


def _stamp(seconds_ago):
    return (datetime.now() - timedelta(seconds=seconds_ago)).isoformat()


def _trade(**extra):
    t = {'id': 7, 'stock': 'INFY', 'status': 'entered', 'paper': True}
    t.update(extra)
    return t


@pytest.mark.parametrize('kind', ['tp', 'trail', 'spot_sl', 'debit_sl'])
def test_a_stale_claim_on_an_open_position_is_released(kind):
    """THE DEFECT. Pre-fix this flag stayed set for the life of the trade."""
    store = _Store()
    mon._release_stranded_claims(
        store, _trade(**{'%s_alerted_at' % kind: _stamp(4000)}))
    assert store.cleared == [(7, kind)]


def test_a_FRESH_claim_is_left_alone():
    """A cycle that is merely slow must not be robbed of its claim mid-flight
    -- that would let a second cycle fire the same exit while the first is
    still inside its Telegram POST."""
    store = _Store()
    mon._release_stranded_claims(store, _trade(debit_sl_alerted_at=_stamp(5)))
    assert store.cleared == []


def test_the_slack_is_at_least_two_cycles():
    """The bound is stated in cycles, not in a magic number, so it follows the
    poll interval if that is ever retuned."""
    store = _Store()
    just_inside = 2 * cfg.MONITOR_INTERVAL_SEC - 30
    mon._release_stranded_claims(
        store, _trade(debit_sl_alerted_at=_stamp(max(1, just_inside))))
    assert store.cleared == [], 'released a claim younger than two cycles'


def test_a_LIVE_record_is_never_swept():
    """A live record's claim is DAILY, not once-ever, so it re-arms tomorrow on
    its own. Releasing it here would turn a deliberate once-a-day nag into one
    every ten minutes -- the alert fatigue the daily throttle exists to cure."""
    store = _Store()
    mon._release_stranded_claims(
        store, _trade(paper=False, debit_sl_alerted_at=_stamp(4000)))
    assert store.cleared == []


def test_an_unparseable_stamp_is_left_alone():
    """Refuse rather than guess: a stamp this cannot read is not evidence that
    the claim is stranded, and clearing it would re-fire a live exit."""
    store = _Store()
    mon._release_stranded_claims(store, _trade(tp_alerted_at='not a date'))
    assert store.cleared == []
    store2 = _Store()
    mon._release_stranded_claims(store2, _trade(tp_alerted_at=None))
    assert store2.cleared == []


def test_TIME_is_not_swept():
    """TIME's flag is daily and its close is retried every cycle, so it was
    never at risk and must not be disturbed."""
    store = _Store()
    mon._release_stranded_claims(store, _trade(time_alerted_at=_stamp(9000)))
    assert store.cleared == []


def test_several_stranded_kinds_are_all_released():
    store = _Store()
    mon._release_stranded_claims(store, _trade(
        tp_alerted_at=_stamp(4000), debit_sl_alerted_at=_stamp(4000)))
    assert sorted(store.cleared) == [(7, 'debit_sl'), (7, 'tp')]


def test_a_store_failure_cannot_stop_the_exit_checks():
    """Housekeeping. It runs at the top of the per-position body, so raising
    here would cost that position its whole exit check -- the very outcome the
    sweep exists to prevent."""
    store = _Store(raises=True)
    mon._release_stranded_claims(store, _trade(tp_alerted_at=_stamp(4000)))


def test_the_sweep_actually_runs_in_check_entered():
    """A guard nothing calls is decorative. Pins the wiring, not just the
    helper -- the repo's own 'wire into the live path' rule.

    RETIRES WHEN: the claim is taken and released by one context manager, so
    there is no window for a claim to be stranded in.
    """
    import inspect
    src = inspect.getsource(mon.check_entered)
    assert '_release_stranded_claims(store, trade)' in src, (
        'check_entered no longer sweeps stranded claims')
