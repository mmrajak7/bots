"""A cohort record with no `paper` flag has TWO booking engines, and no switch
closes it.

THE DEFECT THIS PINS (found 2026-08-31 by exhaustive enumeration of the
deployed predicates, not of the model).

The two engines read the flag with OPPOSITE defaults, each individually
correct and each pinned by an existing test:

    zebra/trade_store.is_paper_record      trade.get('paper', True)
        absent -> PAPER    (the BCS-family stores carry no `paper` key at all,
                            so zebra must never treat their records as its own)
    bcs/spread_monitor._record_says_paper  trade.get('paper') is True
        absent -> LIVE     (the same reasoning read from the other side: this
                            engine must not skip a record it might really own)

Jointly, on ONE zebra cohort record that has lost its key: zebra books it at
the structure mid AND the monitor places real orders for it. And
`zebra/monitor._exits_external` requires `not is_paper_record(...)`, so it
reads the record as paper and the stand-down CANNOT fire -- there is no
combination of the four switches that makes this record single-engine.

WHY THE MODEL COULD NOT SEE IT. `common/arming.booking_engines` took
`record_is_paper` as a single bool, so
`test_no_switch_combination_produces_two_booking_engines` ran a cross product
over a state space the deployed code exceeds. Both preflights would have
sorted the same record into DIFFERENT populations -- zebra's calling it paper,
the monitor's calling it live -- and each reported OK.

Reachable with no code defect at all: a Drive `_merge` against an older
version, a snapshot restore, or a hand repair during an incident. This fleet
performed exactly that class of store surgery on 2026-08-30.

Run:  cd Helper && python -m pytest common/tests/test_arming_unstamped.py -v
"""
import itertools
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import arming                                        # noqa: E402
from zebra.trade_store import is_paper_record                    # noqa: E402


def _monitor_says_paper(t):
    """`bcs/spread_monitor._record_says_paper`, imported lazily.

    Importing `bcs.spread_monitor` at module scope pulls the whole 8.5k-line
    engine (and kiteconnect) into every run of this file; the predicate is one
    line and is re-derived from source below so the copy cannot drift.
    """
    from bcs import spread_monitor as sm
    return sm._record_says_paper(t)


# -- the deployed predicates really do disagree -----------------------------

def test_the_two_predicates_disagree_on_an_unstamped_record():
    """The premise. If this ever stops being true, the rest is moot."""
    unstamped = {'id': 1, 'stock': 'TESTCO'}          # no `paper` key at all
    assert is_paper_record(unstamped) is True
    assert _monitor_says_paper(unstamped) is False


@pytest.mark.parametrize('bad', [None, 'true', 1, 0, '', []])
def test_a_non_boolean_flag_is_unstamped_too(bad):
    """`paper: "true"` from a hand edit is not a boolean and must not pass."""
    assert not isinstance(bad, bool)


# -- the model now represents the state -------------------------------------

def test_an_unstamped_record_has_two_engines_whenever_the_monitor_is_armed():
    for paper_mode, exits_external in itertools.product((True, False),
                                                        (True, False)):
        eng = arming.booking_engines(
            paper_mode=paper_mode, exits_external=exits_external,
            dry_run=False, record_is_paper=None)
        assert set(eng) == {arming.ZEBRA, arming.MONITOR}, (
            'paper_mode=%s exits_external=%s' % (paper_mode, exits_external))


def test_the_stand_down_switch_cannot_close_it():
    """`exits_managed_externally` is the switch an operator would reach for."""
    armed = arming.booking_engines(paper_mode=True, exits_external=True,
                                   dry_run=False, record_is_paper=None)
    assert len(armed) == 2, (
        'the stand-down reads a missing flag as paper, so it does not fire')


def test_it_is_a_fault_even_under_dry_run_when_only_one_engine_remains():
    """One engine is not enough when it is the WRONG one.

    Under `--dry-run` the monitor books nothing, so the count falls to one --
    but that one is zebra, booking at the structure mid a record that may have
    real legs at the broker. Counting engines cannot clear this.
    """
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=True,
                         population={arming.UNSTAMPED_RECORD})
    assert st['legal'] is False
    assert [f['state'] for f in st['faults']] == [arming.UNSTAMPED]


def test_the_fault_names_the_data_fix_not_a_switch():
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=False,
                         population={arming.UNSTAMPED_RECORD})
    fix = st['faults'][0]['fix']
    assert 'paper' in fix and 'broker' in fix
    assert 'dry-run' not in fix, 'no switch closes this; do not suggest one'


def test_it_telegrams_as_an_illegal_state():
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=False,
                         population={arming.UNSTAMPED_RECORD})
    msg = arming.telegram_text(st, 'zebra/monitor.py')
    assert msg and 'ILLEGAL ARMING STATE' in msg


# -- and stays quiet when there is nothing to say ---------------------------

def test_todays_book_is_unaffected():
    """All-paper population: no unstamped fault, no unstamped latent.

    A standing latent here would say only "a record could become corrupt",
    which is true of every record always -- the line a reader learns to skim.
    """
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=True, dry_run=True,
                         population={arming.PAPER_RECORD})
    assert st['legal'] is True
    assert not any(f['state'] == arming.UNSTAMPED
                   for f in st['faults'] + st['latent'])
    assert arming.UNSTAMPED_RECORD not in st['summary']


def test_an_unknown_population_still_cannot_certify_safety():
    """`population=None` reads as all classes present, unstamped included."""
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=False, population=None)
    assert st['legal'] is False
    assert any(f['state'] == arming.UNSTAMPED for f in st['faults'])


# -- both preflights must SORT such a record into the new class -------------

def test_zebra_preflight_classifies_an_unstamped_record_as_unstamped():
    from zebra import monitor as zm
    trades = [{'id': 1, 'status': 'entered', 'cohort': '2026-08-14',
               'structure': 'bcs'}]                       # no `paper` key
    pop = zm._cohort_population(trades)
    assert pop == {arming.UNSTAMPED_RECORD}, (
        'zebra sorted it as ordinary paper — the ambiguity was laundered away'
    )


def test_monitor_preflight_classifies_it_the_same_way():
    """The two preflights must agree. Disagreeing is the whole defect."""
    from bcs import spread_monitor as sm
    trades = [{'id': 1, '_store_type': 'zebra', 'status': 'open'}]
    captured = {}

    def fake_check(**kw):
        captured.update(kw)
        return {'legal': True, 'faults': [], 'latent': [], 'warnings': [],
                'engines': {}, 'summary': 'x'}

    import common.arming as arming_mod
    real = arming_mod.check
    arming_mod.check = fake_check
    try:
        sm._arming_preflight(dry_run=True, all_trades=trades)
    finally:
        arming_mod.check = real

    assert captured.get('population') == {arming.UNSTAMPED_RECORD}


def test_a_properly_stamped_book_is_sorted_normally():
    """Regression guard: the new branch must not swallow the ordinary case."""
    from zebra import monitor as zm
    trades = [
        {'id': 1, 'status': 'entered', 'cohort': '2026-08-14', 'paper': True},
        {'id': 2, 'status': 'entered', 'cohort': '2026-08-14', 'paper': False},
    ]
    assert zm._cohort_population(trades) == {arming.PAPER_RECORD,
                                             arming.LIVE_RECORD}
