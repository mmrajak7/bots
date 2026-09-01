"""Two defects found 2026-08-31, both in `monitor_all` -- the entrypoint the
Pi actually runs -- and both of the "copy you did not open" shape.

1. A DRY RUN WROTE THE COHORT BOOK. Both trail call sites persisted
   `trail_active` / `trail_peak` / `trail_sl` unconditionally, so under today's
   `--dry-run` crontab this engine edited `zebra_trades.json` (and Drive) for
   every cohort record it merely watched. `tp_armed` refuses exactly this, in
   this same file, for the reason it states: "a dry run mutating the live
   cohort record would be this engine arming a trigger it is not allowed to
   pull." The take-profit latch got the guard; the trail -- a STOP level --
   did not. Worse, the values are fill-basis and the cohort book is mid-basis,
   so the write moves a live position's stop level underneath it.

2. THE SL_SPOT STREAK NEVER RESET. `monitor()` has had
   `else: confirm['sl_spot'] = 0` ("only contiguous hits count") since the
   debounce was written. `monitor_all` never got it, so the counter only ever
   rose: two unrelated one-poll dips inside CONFIRM_STALE_SEC reached 2 and
   fired an URGENT, pay-through close. A debounce that certifies precisely the
   single transient print it exists to reject.

Run:  cd Helper && python -m pytest bcs/tests/test_dryrun_writes_and_sl_spot_streak.py -v
"""
import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                             # noqa: E402


class _Store:
    def __init__(self, raises=False):
        self.writes = []
        self.raises = raises

    def update_trade_fields(self, tid, **fields):
        if self.raises:
            raise RuntimeError('drive is down')
        self.writes.append((tid, fields))


def _ts():
    return {'active': True, 'peak': 12.5, 'trail': 9.0, 'cand_count': 0}


def _trade(**extra):
    t = {'id': 5, 'stock': 'INFY', 'quantity': 700}
    t.update(extra)
    return t


# -- 1. the dry run must write nothing ---------------------------------------

def test_a_dry_run_does_not_write_the_trail():
    """THE DEFECT. Today's crontab carries --dry-run."""
    store = _Store()
    assert sm.persist_trail(store, _trade(), _ts(), dry_run=True) is False
    assert store.writes == [], 'a dry run wrote to the live book'


def test_a_paper_record_is_not_written_even_in_a_live_run():
    """The record decides as well as the flag: this engine books nothing for a
    record whose legs never reached a broker, so it must not arm one either."""
    store = _Store()
    assert sm.persist_trail(store, _trade(paper=True), _ts(),
                            dry_run=False) is False
    assert store.writes == []


def test_a_real_record_in_a_live_run_IS_written():
    """The guard must not disable the feature it is guarding -- a live record
    in a live run still needs a durable trail across a restart."""
    store = _Store()
    assert sm.persist_trail(store, _trade(paper=False), _ts(),
                            dry_run=False) is True
    assert store.writes == [(5, {'trail_active': True,
                                 'trail_peak': 12.5, 'trail_sl': 9.0})]


def test_an_unstamped_bcs_record_is_written():
    """`_record_says_paper` reads the flag POSITIVELY on purpose: the
    BCS-family books hold real positions and carry no `paper` key at all, so
    absence must not be read as paper here."""
    store = _Store()
    assert sm.persist_trail(store, _trade(), _ts(), dry_run=False) is True
    assert len(store.writes) == 1


def test_a_store_failure_is_reported_and_not_raised():
    """The level is still live in memory this poll; only durability is lost.
    Raising here would abort the position's whole poll."""
    store = _Store(raises=True)
    assert sm.persist_trail(store, _trade(paper=False), _ts(),
                            dry_run=False) is False


def test_no_store_is_handled():
    assert sm.persist_trail(None, _trade(paper=False), _ts(),
                            dry_run=False) is False


@pytest.mark.parametrize('fn_name', ['monitor', 'monitor_all'])
def test_both_call_sites_go_through_the_guard(fn_name):
    """The guard exists because there were TWO copies of this write and only
    one of anything ever gets fixed. Pins that neither site writes directly.

    RETIRES WHEN: trail persistence has exactly one call site.
    """
    src = inspect.getsource(getattr(sm, fn_name))
    assert 'persist_trail(' in src, f'{fn_name} no longer uses the guard'
    assert 'trail_active=True' not in src, (
        f'{fn_name} writes the trail directly again, bypassing the dry-run '
        f'and paper-record guard')


# -- 2. the SL_SPOT streak must break when spot recovers ---------------------

def test_monitor_all_resets_the_sl_spot_streak_when_spot_recovers():
    """The behavioural claim is about a 5-second loop that needs a live Kite,
    so this pins the reset structurally -- which is what was missing.

    RETIRES WHEN: the two monitor entrypoints share one trigger-evaluation
    function, so there is no second copy to forget.
    """
    src = inspect.getsource(sm.monitor_all)
    assert "confirm_state[close_key]['sl_spot'] = 0" in src, (
        'monitor_all no longer resets the SL_SPOT confirm streak; two '
        'non-contiguous one-poll dips will fire an urgent close again')


def test_the_reference_implementation_still_resets_too():
    """If `monitor()` ever loses its reset, the pin above is comparing against
    nothing -- the negative control for the structural assertion.

    RETIRES WHEN: the two monitor entrypoints share one trigger-evaluation
    function, so there is only one reset to assert about and no reference
    implementation to compare against.
    """
    src = inspect.getsource(sm.monitor)
    assert "confirm['sl_spot'] = 0" in src


def test_a_broken_streak_really_does_need_the_reset():
    """Why the reset is load-bearing: `bump_confirm` only restarts a streak
    that is STALE. Two dips inside CONFIRM_STALE_SEC reach the confirm
    threshold with no intervening reset, which is the false trigger."""
    confirm = {}
    assert sm.bump_confirm(confirm, 'sl_spot') == 1
    # spot recovers here -- without the reset the counter simply carries
    assert sm.bump_confirm(confirm, 'sl_spot') == 2
    assert 2 >= sm.SL_SPOT_CONFIRM_POLLS, (
        'two non-contiguous hits would not have been enough to fire; '
        're-point this test at the real threshold')
    # with the reset in between, the same two dips stay below the threshold
    confirm2 = {}
    sm.bump_confirm(confirm2, 'sl_spot')
    confirm2['sl_spot'] = 0
    assert sm.bump_confirm(confirm2, 'sl_spot') == 1
