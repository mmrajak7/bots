"""The trail peak must survive the arming toggle in BOTH directions.

Two engines write a peak for one cohort record, under two names.
`bcs/spread_monitor.py` persists `trail_peak`; `zebra/monitor.py` persists
`mfe_mid`, and keeps persisting it while it is STOOD DOWN, because measurement
is not an exit decision.

The arming plan explicitly contemplates rollback -- `exits_managed_externally`
back to false and `--dry-run` back on, then forward again later. That toggle
was supported with an unsupported state transfer: everything zebra saw while
it held the exits lived only in `mfe_mid`, so on re-arm this engine resumed
from its own stale `trail_peak`. A lower peak means a lower trail level, i.e.
giving back more than the rule says, on the exits that exist to stop exactly
that.

Run:  cd Helper && python -m pytest bcs/tests/test_trail_peak_handover.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm        # noqa: E402


def cohort_trade(**over):
    """A cohort BCS record as `ZebraStoreAdapter.map_trade` hands it over."""
    t = {
        'id': 1, 'stock': 'TESTCO', 'status': 'entered',
        'long_symbol': 'TESTCO26SEP1000CE', 'short_symbol': 'TESTCO26SEP1040CE',
        'net_debit': 10.0, 'spread_width': 40.0,
        # zebra's own names survive `map_trade` untouched, which is what makes
        # the reconciliation possible at all.
        'debit': 10.0, 'width': 40.0,
        'trail_policy': 'gain_anchored',
        'trail_peak': 0.0, 'trail_sl': 0.0, 'trail_active': False,
    }
    t.update(over)
    return t


def bcs_trade(**over):
    """A record from one of the three original books: no `mfe_mid` anywhere."""
    t = {
        'id': 1, 'stock': 'TESTCO', 'net_debit': 10.0, 'spread_width': 50.0,
        'trail_peak': 0.0, 'trail_sl': 0.0, 'trail_active': False,
    }
    t.update(over)
    return t


# -- the peak ---------------------------------------------------------------

def test_the_peak_zebra_recorded_is_picked_up_on_re_arm():
    """The defect, stated directly.

    Max gain is 30.0, so the gain-anchored trail arms at a peak of 25.0
    (debit + half of max gain). zebra saw 28.0 while it held the exits; this
    engine's own field is stale at 22.0 from before the handover.
    """
    ts = sm.new_trail_state(cohort_trade(trail_peak=22.0, mfe_mid=28.0))
    assert ts['peak'] == 28.0


def test_this_engines_own_peak_wins_when_it_is_higher():
    """Neither engine is authoritative. This one polls every 5s and zebra
    every 5 min, so it sees more; zebra's jump gate accepts less. A peak is a
    maximum and the union of two partial views of a maximum is their max."""
    ts = sm.new_trail_state(cohort_trade(trail_peak=31.0, mfe_mid=28.0))
    assert ts['peak'] == 31.0


def test_a_book_without_mfe_mid_is_unchanged():
    """The three BCS-family books carry no `mfe_mid`. This must be a no-op for
    them, or a shared fix becomes a regression in the books that never had the
    problem."""
    ts = sm.new_trail_state(bcs_trade(trail_peak=22.0, trail_active=True,
                                      trail_sl=8.8))
    assert ts['peak'] == 22.0


@pytest.mark.parametrize('junk', [None, '', 'nan-ish', {}, []])
def test_a_malformed_peak_reads_as_zero_and_never_raises(junk):
    """A restart that raises on a malformed field abandons every stop in the
    book, which is a far worse failure than a forgotten peak."""
    assert sm.restored_peak({'trail_peak': junk, 'mfe_mid': junk}) == 0.0
    assert sm.restored_peak({'trail_peak': junk, 'mfe_mid': 26.0}) == 26.0


# -- the armed flag and the level, which the peak alone does not fix ---------

def test_a_restored_peak_past_the_engage_level_ARMS_the_trail():
    """Restoring the peak without the flag is worse than not restoring it.

    `update_trail` only arms on a LIVE reading above the engage level, so a
    position that peaked at 28.0 and has since given it back would resume
    DISARMED with a peak that passed the arm point days ago -- the give-back
    case the trail exists for, silently unprotected.
    """
    ts = sm.new_trail_state(cohort_trade(mfe_mid=28.0, trail_active=False))
    assert ts['active'] is True
    # debit 10 + half of the peak gain (18.0) = 19.0
    assert ts['trail'] == pytest.approx(19.0)


def test_a_restored_peak_BELOW_the_engage_level_does_not_arm():
    """The negative control. Max gain 30 arms at 25.0; 21.0 is short of it,
    and arming there would invent a stop the rule does not authorise."""
    ts = sm.new_trail_state(cohort_trade(mfe_mid=21.0))
    assert ts['active'] is False
    assert ts['trail'] == 0.0


def test_the_trail_level_never_moves_DOWN_on_a_restart():
    """A stop that loosens on a restart is a stop moving away from an open
    position. The stored level was computed from a peak the restored one is at
    least equal to, so `max` is the only safe reconciliation."""
    ts = sm.new_trail_state(cohort_trade(trail_peak=28.0, mfe_mid=0.0,
                                         trail_active=True, trail_sl=25.0))
    assert ts['trail'] == 25.0


def test_the_level_is_recomputed_by_the_policy_not_copied():
    """`gain_anchored` and `debit_anchored` are different rules, not the same
    rule with different numbers. A cohort record restored under the debit
    anchor would sit at 40% of peak value (11.2) instead of 19.0."""
    gain = sm.new_trail_state(cohort_trade(mfe_mid=28.0))
    debit = sm.new_trail_state(cohort_trade(mfe_mid=28.0, trail_policy=None))
    assert gain['trail'] == pytest.approx(19.0)
    assert debit['trail'] == pytest.approx(28.0 * sm.TRAIL_PERCENT)
    assert gain['trail'] != debit['trail']


# -- the other direction ----------------------------------------------------

def test_zebra_keeps_recording_the_peak_while_it_is_stood_down():
    """The rollback half. It works only because zebra's MFE write sits ABOVE
    the stand-down `continue` -- if it ever moves below, `mfe_mid` freezes at
    the moment of handover and this whole reconciliation restores a stale
    number while looking correct.
    """
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.check_entered)
    mfe_at = src.index('mfe_mod.compute(trade')
    stand_down_at = src.index('if _exits_external(trade):')
    assert mfe_at < stand_down_at, (
        'the MFE write moved below the stand-down: zebra stops recording the '
        'peak the moment it hands the exits over, and the handover back '
        'restores a frozen number')


def test_the_startup_line_reports_the_reconciled_peak():
    """The log line is what a human checks the record against by hand. Printing
    the stored field while running on a different peak is the same class of
    lie as `--list` answering 'Open: 0' with eight positions live."""
    import inspect
    src = inspect.getsource(sm.monitor_all)
    assert "peak={_ts['peak']:.2f}" in src
    assert "peak={t.get('trail_peak', 0):.2f}" not in src
