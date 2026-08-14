"""A Claude usage limit is a REFUSAL, not a hang — and must not read as one.

2026-08-14, 12:55-14:10: the CLI started, printed one line and exited in about
two seconds, ten times. zebra waited the full 600s deadline for each dead
process, charged HAVELLS #404 an attempt it never got, and dropped the entry at
14:05 blaming "no agent slot in 60 min" — while the slot budget held exactly one
entry and the real blocker lifted at 14:10.

Three separate failures, so three separate families of test below:
  1. we can TELL a refusal from a hang, and from a verdict that mentions limits
  2. a refusal costs no attempt and holds the queue to the stated reset
  3. the alert names the usage limit, never a slot
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import vet                    # noqa: E402
from zebra.trade_store import ZebraStore  # noqa: E402

# The exact line Claude Code printed into logs/vet_cli_20260814_130033_entry-vet-404.log
REAL = "You've hit your session limit · resets 2:10pm (Asia/Kolkata)"
BANNER = ('=' * 78 + '\n=== 2026-08-14 13:00:33  vet #404  model=opus  '
          'channel=entry\n' + '=' * 78 + '\n')

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
CONTEXT = {'stock': 'TESTCO', 'debit': 14.0}


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    return tmp_path


@pytest.fixture
def store(logdir, monkeypatch):
    monkeypatch.setattr(cfg, 'LOCAL_FILE', logdir / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', logdir / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    return s


#: Every timestamp in these tests is synthetic, so the transcript's mtime — the
#: thing the scanner reads as "when the refusal was printed" — has to be set
#: explicitly. Leaving it at real wall-clock time is what made the first draft
#: of these tests pass for the wrong reason.
BLOCK_AT = datetime(2026, 8, 14, 13, 0, 33)
RESET_AT = datetime(2026, 8, 14, 14, 10)


def _transcript(logdir, body, at=BLOCK_AT,
                name='vet_cli_20260814_130033_entry-vet-404.log'):
    import os
    p = logdir / name
    p.write_text(BANNER + body + '\n', encoding='utf-8')
    os.utime(p, (at.timestamp(), at.timestamp()))
    return p


@pytest.fixture
def clock(monkeypatch):
    """Pin the module clock so deadlines and blocks share one timeline."""
    holder = {'t': BLOCK_AT}
    monkeypatch.setattr(vet, '_now', lambda: holder['t'])
    return holder


# ── 1. telling a refusal from everything else ────────────────────────────

def test_the_real_2026_08_14_transcript_is_recognised(logdir):
    """Replayed verbatim. A hand-written fixture can drift from what the CLI
    actually prints; this line is the one that cost an entry."""
    _transcript(logdir, REAL)
    found = vet.refresh_cli_block(BLOCK_AT)
    assert found is not None, "the refusal that dropped HAVELLS went unnoticed"
    assert found['reset_at'], "no reset time parsed — the queue cannot be held"


def test_the_reset_time_is_read_off_the_message(logdir):
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    until = vet.cli_blocked_until(BLOCK_AT)
    assert until is not None
    assert (until.hour, until.minute) == (14, 10), until


def test_the_block_lifts_by_itself_at_the_reset(logdir):
    """A block that outlives its own reset is a permanent trading halt."""
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    assert vet.cli_blocked_until(datetime(2026, 8, 14, 14, 11)) is None


def test_a_verdict_that_merely_discusses_limits_is_not_a_block(logdir):
    """The agents write about limits constantly — debit limits, OI limits, the
    trade's own loss limit. Matching on words alone would let one veto silence
    the whole vetting layer."""
    verdict = ("**Signal 376 — VETOED.** The book is thin and the position "
               "limit reached on that strike is one lot; we have hit your "
               "session limit of usable depth. " + 'x' * 700)
    _transcript(logdir, verdict)
    assert vet.refresh_cli_block(BLOCK_AT) is None


def test_an_old_refusal_is_not_resurrected(logdir, monkeypatch):
    """Yesterday's limit must not suppress today's spawns."""
    old = BLOCK_AT - timedelta(seconds=cfg.CLI_BLOCK_SCAN_WINDOW_SEC + 600)
    _transcript(logdir, REAL, at=old)
    assert vet.refresh_cli_block(BLOCK_AT) is None


def test_a_block_with_no_stated_reset_extends_nothing(logdir):
    """Reported, but it may not hold a queue open: an unknown end is exactly
    how a bounded wait becomes a silent permanent halt."""
    _transcript(logdir, 'Claude usage limit reached.')
    found = vet.refresh_cli_block(BLOCK_AT)
    assert found is not None, "the refusal itself must still be seen"
    assert vet.cli_blocked_until(BLOCK_AT) is None


# ── 2. what the block does to the queue ──────────────────────────────────

def test_a_blocked_spawn_is_never_charged_an_attempt(store, logdir, clock):
    """The counter means "agents that ran and failed". A process that died on
    quota tested nothing about this signal; charging it turns a two-attempt
    queue into a zero-attempt one exactly when the layer is already down."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    _transcript(logdir, REAL)
    # Well past the 600s deadline, so the sweep would otherwise call it a
    # timeout and charge attempt 1 — which is what happened to HAVELLS #404.
    clock['t'] = datetime(2026, 8, 14, 13, 20)
    vet.expire_stale(store, now=clock['t'])
    t = store.find(1)
    assert vet.is_queued(t), t['vet']['state']
    assert int(t['vet'].get('attempts') or 0) == 0, \
        "a spawn that never ran was charged as an attempt"
    assert 'usage limit' in (t['vet'].get('queued_because') or '')


def test_the_queue_is_held_to_the_stated_reset(store, logdir, clock):
    """HAVELLS #404 was dropped at 14:05 for a reset at 14:10."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    t = store.find(1)
    t['vet']['queued_at'] = datetime(2026, 8, 14, 13, 0).isoformat()
    t['vet']['drop_after'] = datetime(2026, 8, 14, 14, 0).isoformat()
    # Past the original hour, but the blocker named 14:10.
    assert vet.queue_exhausted(t, datetime(2026, 8, 14, 14, 5)) is False
    # ...and it is not open-ended: reset + grace and no further.
    beyond = RESET_AT + timedelta(seconds=cfg.CLI_BLOCK_GRACE_SEC + 60)
    assert vet.queue_exhausted(t, beyond) is True


def test_the_hold_still_respects_the_attempt_ceiling(store, logdir, clock):
    """A block must not resurrect a signal two real agents already failed."""
    vet.request_entry_vet(store, 1, CONTEXT, spawn=False)
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    t = store.find(1)
    t['vet']['attempts'] = cfg.ENTRY_VET_MAX_ATTEMPTS
    assert vet.queue_exhausted(t, datetime(2026, 8, 14, 13, 30)) is True


def test_no_spawn_is_burned_into_a_known_block(logdir):
    """Ten processes were started, printed one line and died, and the budget
    carried them as if agents were working.

    Asserted on the SOURCE rather than by calling `_spawn_generic`: conftest
    replaces that function fleet-wide so a test can never spawn a real agent
    (twice this suite Telegrammed the owner). The thing that must hold is
    ORDER — the block check has to come before the slot is reserved, or a
    refusal still consumes budget on its way to returning None."""
    # Read the FILE, not the live object: conftest's rail has already replaced
    # the function, so inspect.getsource would happily assert against the stub.
    text = (HELPER / 'zebra' / 'vet.py').read_text(encoding='utf-8')
    start = text.index('def _spawn_generic(')
    src = text[start:text.index('\ndef ', start + 1)]
    assert 'cli_blocked_until()' in src, "spawns no longer notice a block"
    assert src.index('cli_blocked_until()') < src.index('_spawn_budget_ok('), \
        "a slot is reserved before the block is checked"
    # ...and the condition that guard reads is genuinely true in this state.
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    assert vet.cli_blocked_until(BLOCK_AT) is not None


# ── 3. the alert has to name the real blocker ────────────────────────────

def test_the_owner_is_told_about_the_limit_not_a_slot(logdir):
    _transcript(logdir, REAL)
    vet.refresh_cli_block(BLOCK_AT)
    msg = vet.cli_block_reason(BLOCK_AT)
    assert 'usage limit' in msg and '14:10' in msg, msg
    assert 'slot' not in msg.lower(), \
        "still blaming the agent budget for a quota refusal"


def test_the_drop_message_states_attempts_that_actually_ran(store, logdir):
    """"attempts: 1" was printed for a signal whose only spawn never ran."""
    src = (HELPER / 'zebra' / 'vet.py').read_text(encoding='utf-8')
    assert 'attempts that actually ran' in src
    assert 'no agent slot in %d min' not in src, \
        "the misleading 2026-08-14 wording is back"


# ── 4. wiring: a queued vet must be reachable out of the trigger band ────

def test_the_out_of_band_drain_is_wired_into_check_watching():
    """The gate that drains the queue sits ~140 lines below the `gap >
    TRIGGER_GAP_MAX` continue, so a signal that drifted back into the watch
    band could never be retried. Grep the band check itself: a whole-file grep
    passes on the helper's definition alone."""
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.check_watching)
    assert '_drain_queued_out_of_band(' in src, \
        "a queued signal outside the trigger band is unreachable again"
    assert 'is_queued' in src


def test_the_out_of_band_drain_never_raises(monkeypatch):
    """It runs inside the per-trade loop: an exception here costs every signal
    after it its cycle, including its own drift-cancel check."""
    from zebra import monitor
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    monitor._drain_queued_out_of_band(
        None, {'id': 1, 'stock': 'X', 'direction': 'CE'}, None, 100.0, 4.4)


# ── a misparsed reset must not hold the queue open ───────────────────────

def test_a_duration_is_not_read_as_a_clock_time(logdir):
    """"resets in 5 minutes" would parse as 05:00 and hand back a reset up to a
    day out — holding every queued entry open on a misparse."""
    _transcript(logdir, "You've hit your session limit · resets in 5 minutes")
    found = vet.refresh_cli_block(BLOCK_AT)
    assert found is not None, "the refusal itself must still be seen"
    assert vet.cli_blocked_until(BLOCK_AT) is None, \
        "a duration was read as a wall-clock reset and extended the queue"


def test_an_implausibly_distant_reset_is_refused(logdir):
    """A usage limit resets in hours. Anything further out is a misparse, and a
    misparse here is indistinguishable from a day-long trading halt."""
    _transcript(logdir, "You've hit your session limit · resets 11:30am (Asia/Kolkata)")
    # 11:30 is BEHIND the 13:00 detection, so it rolls to tomorrow — ~22h out.
    vet.refresh_cli_block(BLOCK_AT)
    assert vet.cli_blocked_until(BLOCK_AT) is None


def test_a_reset_a_few_hours_out_is_still_honoured(logdir):
    """The other half: the clamp must not refuse ordinary resets."""
    _transcript(logdir, "You've hit your session limit · resets 6:00pm (Asia/Kolkata)")
    vet.refresh_cli_block(BLOCK_AT)
    until = vet.cli_blocked_until(BLOCK_AT)
    assert until is not None and (until.hour, until.minute) == (18, 0), until
