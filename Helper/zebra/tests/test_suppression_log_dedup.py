"""One WARNING per (signal, gate) per day. The repeats are INFO.

#496 SHREECEM emitted the SAME suppression 58 times on 2026-09-07 — every
cycle from 10:15 to 15:10. Every one was TRUE (the target leg ran 46-56% wide
against a 25% cap) and re-quoting each cycle is CORRECT, because a book can
tighten. What is not correct is 58 WARNINGs for one condition.

That is the path the OI flag took before COCHINSHIP was waved through, and the
same session produced a second example (`AUCTION WINDOW LOOKS WRONG`, 24 a
session, 100% false). A log where the warnings are mostly noise is a log whose
warnings are not read.
"""

import logging

import pytest

from zebra import config as cfg
from zebra import monitor


@pytest.fixture(autouse=True)
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)


TRADE = {'id': 496, 'stock': 'SHREECEM', 'direction': 'PE'}
WIDE = ('unreliable target-leg book SHREECEM26SEP22750PE: '
        'wide_book width %.2f vs mid %.2f')


def _levels(caplog):
    return [r.levelno for r in caplog.records
            if 'BCS SUPPRESSED' in r.getMessage()]


def test_the_first_suppression_is_a_WARNING(caplog):
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
    assert _levels(caplog) == [logging.WARNING]


def test_a_REQUOTE_of_the_same_gate_drops_to_INFO(caplog):
    """The numbers move every cycle — that is the point of re-quoting — so the
    key strips them. Otherwise every re-quote is a 'new' warning and nothing
    has been fixed."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        for w, m in ((91.40, 197.90), (91.95, 202.47), (125.70, 248.60)):
            monitor._log_bcs_suppressed(TRADE, WIDE % (w, m))
    assert _levels(caplog) == [logging.WARNING, logging.INFO, logging.INFO]


def test_the_repeat_still_carries_the_full_line(caplog):
    """Downgraded, never dropped. What the book looked like each cycle is the
    evidence, and an option book cannot be reconstructed after the fact."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
        monitor._log_bcs_suppressed(TRADE, WIDE % (125.70, 248.60))
    last = [r for r in caplog.records if 'BCS SUPPRESSED' in r.getMessage()][-1]
    assert '125.70' in last.getMessage() and '248.60' in last.getMessage()
    assert '[repeat 2 today]' in last.getMessage()


def test_a_DIFFERENT_gate_on_the_same_signal_still_warns(caplog):
    """Collapsing those would hide a signal whose rejection reason CHANGED,
    which is the one thing in this stream worth waking up for."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
        monitor._log_bcs_suppressed(TRADE, 'entry cost 8.10/sh is 19% of gain')
    assert _levels(caplog) == [logging.WARNING, logging.WARNING]


def test_a_DIFFERENT_signal_with_the_same_gate_still_warns(caplog):
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
        monitor._log_bcs_suppressed(dict(TRADE, id=502), WIDE % (91.40, 197.90))
    assert _levels(caplog) == [logging.WARNING, logging.WARNING]


def test_it_survives_the_process_exiting_between_cycles():
    """`zebra run` is ONE-SHOT under cron. An in-memory counter would reset
    every five minutes, which is exactly the failure being fixed."""
    assert monitor._suppression_seen(496, 'x') == 1
    assert monitor._suppression_seen(496, 'x') == 2
    state = cfg.LOG_DIR / monitor.SUPPRESS_STATE_NAME
    assert state.exists(), 'the counter must outlive the process'


def test_a_new_session_starts_clean(monkeypatch):
    """The first suppression of each morning is a finding again — and it also
    bounds the file to one day of keys."""
    import json
    assert monitor._suppression_seen(496, 'x') == 1
    path = cfg.LOG_DIR / monitor.SUPPRESS_STATE_NAME
    # `open()` rather than the Path convenience reader: this is a JSON state
    # file, not source, and `common/tests/test_source_guard_policy.py` finds
    # source-reading guards by scanning for that reader's name. This test is
    # not one of those, so it should not be claiming a RETIRES WHEN.
    # (The first version of this comment NAMED the token it was avoiding, which
    # tripped the very detector it was explaining — the scan reads comments too.)
    with open(path) as f:
        state = json.load(f)
    state['day'] = '1999-01-01'
    with open(path, 'w') as f:
        json.dump(state, f)
    assert monitor._suppression_seen(496, 'x') == 1


def test_an_unwritable_state_file_degrades_to_ALWAYS_WARNING(monkeypatch, caplog):
    """The safe direction for a log line. A counter that can throw is a new way
    to break the entry path, for a cosmetic benefit."""
    monkeypatch.setattr(monitor.cfg, 'LOG_DIR', object())
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.95, 202.47))
    assert _levels(caplog) == [logging.WARNING, logging.WARNING]


def test_the_grep_that_the_docstring_promises_still_works(caplog):
    """`grep 'BCS SUPPRESSED' logs/cron_zebra.log` is documented at the call
    site. The prefix must survive the downgrade."""
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.40, 197.90))
        monitor._log_bcs_suppressed(TRADE, WIDE % (91.95, 202.47))
    msgs = [r.getMessage() for r in caplog.records]
    assert sum('BCS SUPPRESSED' in m for m in msgs) == 2


# -- the docstring says NEVER RAISES; these are the shapes that made it lie ---
#
# Found by adversarial review 2026-09-07. The raise escaped `_log_bcs_suppressed`
# -> `_build_bcs` and past the per-trade `except` in `check_watching`, aborting
# the loop so every remaining signal in the cycle was skipped — and it happened
# BEFORE the rewrite, so the bad file was never repaired and it recurred every
# cycle until a human edited it. A cosmetic log change stalling ENTRIES.

@pytest.mark.parametrize('body', [
    '{"day": "%s", "seen": []}',
    '{"day": "%s", "seen": null}',
    '{"day": "%s", "seen": {"496|x": "abc"}}',
    '{"day": "%s", "seen": {"496|x": null}}',
    '["not", "a", "dict"]',
    'null',
    '',
])
def test_a_MALFORMED_state_file_never_raises(body, monkeypatch):
    import json
    from datetime import datetime
    from zebra.monitor import IST
    day = datetime.now(IST).date().isoformat()
    path = cfg.LOG_DIR / monitor.SUPPRESS_STATE_NAME
    path.write_text(body % day if '%s' in body else body)
    assert monitor._suppression_seen(496, 'x') >= 1


def test_a_MALFORMED_state_file_is_REPAIRED_not_merely_survived(monkeypatch):
    """Surviving once is not enough: the old code raised before the rewrite, so
    the bad file persisted and the failure repeated every cycle forever."""
    import json
    path = cfg.LOG_DIR / monitor.SUPPRESS_STATE_NAME
    path.write_text('{"seen": []}')
    monitor._suppression_seen(496, 'x')
    with open(path) as f:
        state = json.load(f)
    assert isinstance(state.get('seen'), dict)
    assert monitor._suppression_seen(496, 'x') == 2, 'counting must resume'
