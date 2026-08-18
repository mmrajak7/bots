"""One word outside the alternation blinded the whole detector.

2026-08-17, 14:40-15:30: five `events` spawns died in about two seconds each
printing

    You've hit your weekly limit - resets 9:30pm (Asia/Kolkata)

`_BLOCK_PATTERNS` enumerated `session|usage`. "weekly" is neither, so nothing
was recognised as a refusal: no `zebra_cli_block.json` was written, the digest
reported `blocks_at: []`, and the owner's only warning was the generic
silent-channel nag at 15:30 - fifty minutes after the first dead spawn, and it
never said "out of quota until 21:30".

It landed on `events` and cost nothing. The identical miss on `entry` three
days earlier dropped HAVELLS #404 (see test_cli_block.py). The fix is to stop
enumerating tier names, because the tier names are Anthropic's to rename - so
this file pins the BEHAVIOUR (any tier word is a refusal) rather than today's
vocabulary, and replays the real 08-17 transcript verbatim.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import vet                    # noqa: E402

#: Copied byte-for-byte out of logs/vet_cli_20260817_153039_events-events.log,
#: middle dot and all. A hand-typed approximation is how a fixture drifts away
#: from what the binary actually prints.
REAL_WEEKLY = ("You've hit your weekly limit · resets 9:30pm "
               "(Asia/Kolkata)")
BANNER = ('=' * 78 + '\n=== 2026-08-17 15:30:39  events  model=sonnet  '
          'channel=events\n' + '=' * 78 + '\n')

BLOCK_AT = datetime(2026, 8, 17, 15, 30, 39)


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    return tmp_path


def _transcript(logdir, body, at=BLOCK_AT,
                name='vet_cli_20260817_153039_events-events.log'):
    """Write a refusal transcript and backdate it.

    The scanner reads the file's mtime as "when the refusal was printed", so
    leaving it at wall-clock now is how these tests pass for the wrong reason.
    """
    import os
    p = logdir / name
    p.write_text(BANNER + body + '\n', encoding='utf-8')
    os.utime(p, (at.timestamp(), at.timestamp()))
    return p


# -- 1. the transcript that actually went unnoticed -----------------------

def test_the_real_2026_08_17_weekly_transcript_is_recognised(logdir):
    _transcript(logdir, REAL_WEEKLY)
    found = vet.refresh_cli_block(BLOCK_AT)
    assert found is not None, \
        "the weekly-limit refusal is still invisible to the detector"


def test_the_weekly_reset_time_is_read_off_the_message(logdir):
    """A block the watchdog cannot date cannot alert: `health.check` gates its
    usage-limit probe on `reset_at` parsing, so an unparsed reset is a silent
    outage even once the refusal itself is seen."""
    _transcript(logdir, REAL_WEEKLY)
    vet.refresh_cli_block(BLOCK_AT)
    until = vet.cli_blocked_until(BLOCK_AT)
    assert until is not None, "no reset parsed - the watchdog stays quiet"
    assert (until.hour, until.minute) == (21, 30), until


def test_the_weekly_block_reaches_the_watchdog(logdir, monkeypatch):
    """The regex is only half the path. What the owner actually sees is
    health's fourth probe, and it fires off the state `refresh_cli_block`
    writes - so assert the alert, not just the match."""
    from zebra import health
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(health, '_state_path',
                        lambda: cfg.LOG_DIR / 'auth_health.json')
    _transcript(logdir, REAL_WEEKLY)
    vet.refresh_cli_block(BLOCK_AT)
    sent = []
    health.check(send=lambda m, dry_run=False: sent.append(m) or True,
                 now=BLOCK_AT, paths=[])
    assert sent, "the watchdog all-cleared through a quota outage"
    assert 'USAGE LIMIT' in sent[0]
    assert '21:30' in sent[0], \
        "the alert must name when the agents come back, not just that they are gone"


def test_the_weekly_block_lifts_at_its_own_reset(logdir):
    """A weekly limit is still hours, not days, on this box - and a block that
    outlives its reset is a permanent trading halt."""
    _transcript(logdir, REAL_WEEKLY)
    vet.refresh_cli_block(BLOCK_AT)
    assert vet.cli_blocked_until(datetime(2026, 8, 17, 21, 31)) is None


# -- 2. the vocabulary must not be an allow-list --------------------------

#: Every shape Claude Code is known to have printed, plus the obvious next
#: renames. The point is not this list: it is that a tier word we have never
#: seen still reads as a refusal, because the day we meet one is a day the
#: vetting layer is already down.
TIER_SPELLINGS = [
    "You've hit your session limit · resets 2:10pm (Asia/Kolkata)",
    "You've hit your weekly limit · resets 9:30pm (Asia/Kolkata)",
    "You've hit your usage limit · resets 11:00pm (Asia/Kolkata)",
    "You've hit your daily limit · resets 6:00am (Asia/Kolkata)",
    "You've hit your Opus weekly limit · resets 9:30pm (Asia/Kolkata)",
    "You've hit your 5-hour limit · resets 4:00pm (Asia/Kolkata)",
    "You've hit your limit · resets 4:00pm (Asia/Kolkata)",
    "Claude usage limit reached.",
    "Weekly limit reached.",
    "5-hour limit reached",
]


@pytest.mark.parametrize('line', TIER_SPELLINGS)
def test_every_tier_spelling_reads_as_a_refusal(logdir, line):
    _transcript(logdir, line)
    assert vet.refresh_cli_block(BLOCK_AT) is not None, line


# -- 3. and it still must not fire on a verdict -------------------------

def test_a_long_verdict_using_the_same_words_is_not_a_block(logdir):
    """The loosened pattern would match this sentence. The BODY BOUND is what
    stops it, and that is the invariant worth pinning: widening the vocabulary
    must not have widened what counts as a refusal."""
    verdict = ("**Signal 402 - VETOED.** Position sizing has hit your weekly "
               "limit for this symbol and the exchange OI limit reached on "
               "the short leg. " + 'x' * 700)
    _transcript(logdir, verdict)
    assert vet.refresh_cli_block(BLOCK_AT) is None


def test_a_refusal_shaped_line_inside_a_real_run_is_not_a_block(logdir):
    """Same guard from the other side: a genuine agent that quotes the refusal
    text while explaining why it retried is a run, not a refusal."""
    body = ("I re-quoted the pair twice. The first spawn printed \"You've hit "
            "your weekly limit\" and exited, so I ran the valuation myself. "
            + 'y' * 700)
    _transcript(logdir, body)
    assert vet.refresh_cli_block(BLOCK_AT) is None
