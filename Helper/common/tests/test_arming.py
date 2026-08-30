"""The arming invariant: exactly one engine may book each cohort record.

`common/arming.py` replaces the hand-maintained switch table in `CLAUDE.md`.
The tests that matter here are not the ones that check its output strings --
they are the ones that PIN ITS RESTATEMENT to the deployed predicates. A
validator that models the code incorrectly is worse than the table it replaced,
because the table at least admitted it was prose.

Run:  cd Helper && python -m pytest common/tests/test_arming.py -v
"""
import itertools
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import arming                              # noqa: E402

PAPER, LIVE = arming.PAPER_RECORD, arming.LIVE_RECORD
BOTH = {PAPER, LIVE}
SWITCHES = list(itertools.product((True, False), (True, False),
                                  (True, False, None)))


def _classify(p, x, d, population=BOTH, auto_entry=False):
    return arming.classify(paper_mode=p, exits_external=x,
                           auto_entry=auto_entry, dry_run=d,
                           population=population)


# -- the invariant itself ---------------------------------------------------

def test_a_paper_record_always_has_exactly_one_engine():
    """No switch can strand or double-own a paper record.

    That is not a happy accident: zebra books it because no broker was ever
    involved, and the order path refuses it for the same reason. The two
    predicates are opposite readings of ONE fact, so their disagreement is
    structurally impossible -- which is why every fault below is about the
    other record class.
    """
    for p, x, d in SWITCHES:
        eng = arming.booking_engines(paper_mode=p, exits_external=x,
                                     dry_run=d, record_is_paper=True)
        assert eng == (arming.ZEBRA,), (p, x, d, eng)


def test_a_live_record_has_one_engine_unless_the_monitor_is_disarmed():
    for p, x, d in SWITCHES:
        eng = arming.booking_engines(paper_mode=p, exits_external=x,
                                     dry_run=d, record_is_paper=False)
        assert eng == (() if d is True else (arming.MONITOR,)), (p, x, d, eng)


def test_no_switch_combination_produces_two_booking_engines():
    """The TWO_ENGINES branch is unreachable, and that is the finding.

    It was reachable until 2026-08-29, through `_paper_auto_close`'s
    `cfg.PAPER_MODE or is_paper_record(trade)`: a hand-placed live trade in a
    paper-mode store was bookable at mid here and at the broker there. Closing
    that gate is what makes this assertion true, so this test is the proof
    that the fix holds across the whole cross product rather than in the one
    case somebody thought of.
    """
    for p, x, d in SWITCHES:
        for is_paper in (True, False):
            eng = arming.booking_engines(paper_mode=p, exits_external=x,
                                         dry_run=d, record_is_paper=is_paper)
            assert len(eng) <= 1, (p, x, d, is_paper, eng)


def test_paper_mode_does_not_appear_in_the_booking_decision():
    """`paper_mode` says how NEW entries are made. It cannot license a mid
    close of a position with real legs, and the whole stale table existed
    because it was once allowed to."""
    for x, d in itertools.product((True, False), (True, False, None)):
        for is_paper in (True, False):
            on = arming.booking_engines(paper_mode=True, exits_external=x,
                                        dry_run=d, record_is_paper=is_paper)
            off = arming.booking_engines(paper_mode=False, exits_external=x,
                                         dry_run=d, record_is_paper=is_paper)
            assert on == off, (x, d, is_paper, on, off)


# -- the restatement is pinned to the deployed code -------------------------

def test_the_zebra_predicate_matches_the_deployed_one():
    """`booking_engines` restates `zebra.monitor._exits_external` in terms of
    switches. Drive the real function over the same cross product."""
    from zebra import config as zcfg
    from zebra import monitor

    for x, in_cohort_, is_paper in itertools.product((True, False), repeat=3):
        trade = {'id': 1, 'cohort': zcfg.COHORT_START if in_cohort_
                 else '1999-01-01', 'paper': is_paper}
        real = monitor._exits_external.__wrapped__(trade) \
            if hasattr(monitor._exits_external, '__wrapped__') else None
        old = zcfg.EXITS_MANAGED_EXTERNALLY
        try:
            zcfg.EXITS_MANAGED_EXTERNALLY = x
            monitor.cfg.EXITS_MANAGED_EXTERNALLY = x
            real = monitor._exits_external(trade)
        finally:
            zcfg.EXITS_MANAGED_EXTERNALLY = old
            monitor.cfg.EXITS_MANAGED_EXTERNALLY = old
        modelled = (x and in_cohort_ and not is_paper)
        assert real is modelled, (x, in_cohort_, is_paper, real, modelled)


def test_zebra_books_only_records_whose_legs_never_reached_a_broker():
    """The other half of the model, read off the deployed source.

    Asserted on the SOURCE because no input distinguishes the two readings
    once the record is paper: `cfg.PAPER_MODE or is_paper_record(t)` and
    `is_paper_record(t)` agree on every paper record and differ only on a live
    one in paper mode -- which is the exact state that was wrong, and which a
    behavioural test would have to construct to notice.

    RETIRES WHEN: `_paper_auto_close` takes the booking decision as an
    argument from `common.arming` instead of re-deriving it, so the two
    cannot disagree and there is no predicate here to pin.
    """
    import inspect
    from zebra import monitor
    # CODE ONLY. The comment above that line explains the bug by quoting it,
    # so a raw substring search matches the explanation and passes whatever
    # the code does -- which would be this file committing the same error it
    # is testing for.
    code = '\n'.join(
        ln.split('#', 1)[0] for ln in
        inspect.getsource(monitor._paper_auto_close).splitlines())
    assert 'cfg.PAPER_MODE or is_paper_record' not in code, (
        'the mode is licensing a mid-price close again: a record with real '
        'legs would be bookable both here and at the broker')
    assert 'not is_paper_record(trade)' in code


def test_the_monitor_refuses_paper_records():
    """The order path's half. `_record_says_paper` reads the flag POSITIVELY
    (absence means NOT paper), which is the opposite default from zebra's and
    is deliberate -- the three BCS-family books carry no flag at all."""
    from bcs import spread_monitor as sm
    assert sm._record_says_paper({'paper': True}) is True
    assert sm._record_says_paper({'paper': False}) is False
    assert sm._record_says_paper({}) is False


# -- fault vs latent --------------------------------------------------------

def test_todays_deployed_state_is_legal_and_says_what_it_is_one_record_from():
    """paper_mode on, monitor in dry run, eight paper positions.

    This must NOT read as ILLEGAL. It is the deliberate, safe configuration,
    and an alarm on every cycle for a state nobody intends to change is how a
    reader learns to skim the log the arming decision is read from.
    """
    st = _classify(True, False, True, population={PAPER})
    assert st['legal'] is True
    assert st['faults'] == []
    assert len(st['latent']) == 1
    assert st['latent'][0]['state'] == arming.NO_ENGINE
    assert st['latent'][0]['record_class'] == LIVE
    assert 'none open' in st['summary']


def test_the_same_switches_are_ILLEGAL_once_a_live_record_exists():
    """No switch moved. One `zebra enter` on a hand-placed trade did."""
    st = _classify(True, False, True, population={PAPER, LIVE})
    assert st['legal'] is False
    assert [f['state'] for f in st['faults']] == [arming.NO_ENGINE]


def test_an_unknown_population_is_read_as_both_present():
    """A caller that cannot see the book must not be able to certify safety."""
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=True, population=None)
    assert st['legal'] is False


def test_the_no_engine_fix_names_the_crontab_and_not_the_config():
    """One switch, named. Offering `exits_managed_externally=false` as an
    alternative would send the operator to the switch that makes the log look
    healthy -- zebra cannot book a live record at all, whatever it says."""
    st = _classify(True, True, True, population={LIVE})
    fix = st['faults'][0]['fix']
    assert '--dry-run' in fix
    assert 'exits_managed_externally=false' not in fix


def test_the_arming_flag_and_the_stand_down_are_reported_separately():
    """Both roads to no-engine reach the same verdict with different detail,
    because the reader needs to know which one they are on."""
    stood_down = _classify(True, True, True, population={LIVE})
    not_stood_down = _classify(True, False, True, population={LIVE})
    assert not stood_down['legal'] and not not_stood_down['legal']
    assert 'stood down' in stood_down['faults'][0]['detail']
    assert 'stood down' not in not_stood_down['faults'][0]['detail']


# -- warnings ---------------------------------------------------------------

def test_auto_entry_under_paper_mode_is_reported_as_inert():
    """`zebra/monitor.py` consults auto-entry only under `if not
    cfg.PAPER_MODE`, so the switch does nothing here. Silence would leave
    someone believing entries are armed."""
    st = _classify(True, False, False, population={PAPER}, auto_entry=True)
    assert st['legal'] is True
    assert any('INERT' in w for w in st['warnings'])
    off = _classify(False, True, False, population={LIVE}, auto_entry=True)
    assert not any('INERT' in w for w in off['warnings'])


def test_a_duplicate_cascade_is_a_warning_not_a_fault():
    """Both engines evaluating every trigger double-spends vet markers and
    sends two alerts. Booking is still single, so it does not strand or
    double-close anything -- it is expensive, not dangerous."""
    st = _classify(False, False, False, population={LIVE})
    assert st['legal'] is True
    assert any('double-spends' in w for w in st['warnings'])


def test_an_unknown_monitor_state_warns_rather_than_alarms():
    st = _classify(True, False, None, population={PAPER})
    assert st['legal'] is True
    assert any('UNKNOWN' in w for w in st['warnings'])


# -- the announcement -------------------------------------------------------

def test_check_never_raises_even_when_telegram_does():
    """A process already in a state it cannot fix must not also crash."""
    def boom(_msg):
        raise RuntimeError('telegram is down')
    st = arming.check(paper_mode=True, exits_external=True, auto_entry=False,
                      dry_run=True, population={LIVE}, engine='test',
                      log=None, telegram=boom)
    assert st['legal'] is False


def test_a_legal_state_sends_nothing():
    sent = []
    arming.check(paper_mode=True, exits_external=False, auto_entry=False,
                 dry_run=True, population={PAPER}, engine='test',
                 telegram=sent.append)
    assert sent == []


def test_describe_prints_the_latent_finding_without_alarming():
    st = _classify(True, False, True, population={PAPER})
    text = arming.describe(st)
    assert text.startswith('ARMING: OK')
    assert 'latent no_engine' in text
    assert '***' not in text


def test_describe_shouts_on_a_fault():
    st = _classify(True, False, True, population={PAPER, LIVE})
    text = arming.describe(st)
    assert text.startswith('ARMING: ILLEGAL')
    assert 'FIX:' in text


# -- wiring -----------------------------------------------------------------

def test_both_engines_call_it():
    """Announced from BOTH, deliberately: these are exactly the states in
    which the other engine may be absent, so a single sender would go quiet
    when it matters. Read off the source, because the alternative is running
    two market-hours processes.

    RETIRES WHEN: both engines are started through one entrypoint that runs
    the preflight itself, so an engine cannot start without it.
    """
    import inspect
    from bcs import spread_monitor as sm
    from zebra import monitor
    assert '_arming_preflight' in inspect.getsource(monitor.run_cycle)
    assert '_arming_preflight' in inspect.getsource(sm.monitor_all)


def test_the_kill_switch_transition_re_states_the_arming():
    """Tripping the kill switch to be SAFE is how the no-engine state is most
    often reached: the monitor does not stop, it stops BOOKING.

    RETIRES WHEN: `dry_run` becomes observable state that the arming verdict
    recomputes from on read, so no transition needs to remember to
    re-announce.
    """
    import inspect
    from bcs import spread_monitor as sm
    src = inspect.getsource(sm.monitor_all)
    i = src.index('KILL SWITCH: trading.enabled=false')
    assert '_arming_preflight' in src[i:i + 2000]


@pytest.mark.parametrize('state,expected', [
    ({'state': 'dry_run'}, True),
    ({'state': 'ok'}, False),
    # CHANGED 2026-08-31, from False. `no_cohort_book` means the peer is
    # polling and armed but its adapter onto the COHORT book failed -- so it
    # cannot see a cohort record, let alone book one. Answering "armed" named
    # it as the live records' engine on the strength of a beat that says it
    # cannot reach them, which is the same error as reading a missing beat as
    # healthy. UNKNOWN is the honest answer, and `classify` now turns
    # unknown-plus-live-records into a fault rather than into silence.
    ({'state': 'no_cohort_book'}, None),
    ({'state': 'missing'}, None),
    ({'state': 'stale'}, None),
    ({'state': 'unreadable'}, None),
])
def test_zebra_reads_the_peers_arming_from_the_heartbeat(monkeypatch, state,
                                                         expected):
    """A missing or stale beat is UNKNOWN, never "armed". An unknown that
    answers "armed" would certify the one state this exists to catch."""
    from zebra import monitor
    monkeypatch.setattr(monitor, 'read_exit_engine_heartbeat',
                        lambda *a, **k: state)
    assert monitor._monitor_dry_run() is expected


def test_an_unverifiable_peer_with_LIVE_records_open_is_a_FAULT():
    """THE STATE THAT LOOKED HEALTHY FROM EVERY LOG (found 2026-08-31).

    With `dry_run=None` the monitor is listed as POSSIBLY booking, so a live
    record came back with exactly one engine and was certified `ARMING: OK` --
    while the reason its heartbeat is unreadable may be that it is dead. The
    `dry_run is None` no-engine branch never ran, because a one-engine class
    never reaches it, so the finding was not recorded even as latent.
    """
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=None,
                         population={arming.LIVE_RECORD})
    assert st['legal'] is False
    assert [f['state'] for f in st['faults']] == [arming.UNVERIFIED]
    assert 'heartbeat' in st['faults'][0]['detail']


def test_an_unverifiable_peer_with_only_PAPER_records_is_still_fine():
    """Today's book. zebra books its own paper records, peer or no peer, so
    an alarm here would be the noise that trains the reader to skim."""
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=None,
                         population={arming.PAPER_RECORD})
    assert st['legal'] is True
    assert not any(f['state'] == arming.UNVERIFIED for f in st['faults'])


def test_the_unverified_fault_does_not_send_the_operator_to_a_switch():
    """There is no switch for "the peer might be dead". Say what to check."""
    st = arming.classify(paper_mode=True, exits_external=False,
                         auto_entry=False, dry_run=None,
                         population={arming.LIVE_RECORD})
    fix = st['faults'][0]['fix']
    assert 'spread_monitor' in fix and 'heartbeat' in fix
    assert 'UNWATCHED' in fix
