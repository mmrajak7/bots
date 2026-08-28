"""The digest must read the engine that places orders.

Until 2026-08-28 `zebra/digest.py` read `cron_zebra_<day>.log` and the vet
transcripts and nothing else. `bcs/spread_monitor.py` — the process that
places orders — writes `spread_monitor_cron_<day>.log`, and the digest had
never opened it. So the end-of-day record, which the owner designed as the
place failures get strong action, could not see a failed close, a hard-cutoff
miss, a `partial_close` freeze, a flipped or naked position, retry
exhaustion, or that morning's 70 `Too many requests` failures.

What these tests protect, in order of how badly each has bitten before:

1. **That it reads the file at all.** The instrument was not reading the thing
   it exists to hold to account (same shape as H1).
2. **That silence is never clean.** An absent log, a log that parses to
   nothing, and a failure-shaped line with no name are all REPORTED. Returning
   nothing quietly is the defect being fixed; it must not be reachable by a
   second route.
3. **That a count is a named event.** "17 ERROR lines" is what existed and is
   not actionable.
4. **That a blip is not a failed close.** Rate limits outnumber real failures
   50:1 and would bury one if both were tallied together.
5. **That the sections which already worked are untouched.**
"""
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg           # noqa: E402
from zebra import digest                  # noqa: E402
from zebra import engine_log              # noqa: E402

DAY = '2026-08-28'
STAMP = DAY.replace('-', '')


@pytest.fixture
def logs(monkeypatch):
    """cfg.LOG_DIR is already redirected to a tmp dir by the autouse rail."""
    d = cfg.LOG_DIR
    monkeypatch.setattr(cfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text('[]', encoding='utf-8')
    return d


def _mon(logs, lines):
    p = logs / ('spread_monitor_cron_%s.log' % STAMP)
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return p


def _zeb(logs, lines):
    p = logs / ('cron_zebra_%s.log' % STAMP)
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return p


def _status(t, who, spot, spread=None, tag=''):
    """One monitor status line, priced or not, in the engine's own format."""
    body = '  %s: Spot=%.2f | TP:100.0(+1.0) | SL:90.0(+1.0)' % (who, spot)
    if spread is not None:
        body += ' | Spread:%.2f | P&L: Rs +100' % spread
    return '[%s] %s%s' % (t, body, tag)


# ── 1. it reads the file at all ─────────────────────────────────────────────

def test_the_digest_sees_a_failed_close_in_the_order_engine_log(logs):
    """THE defect. This log line existed on disk and the digest could not see
    it, because it only ever opened zebra's log."""
    _mon(logs, [
        '[10:02:11] !!! CRITICAL: SHORT LEG CLOSE FAILED              !!!',
        '[10:02:11] !!! DO NOT close long leg manually - margin spike! !!!',
        '[10:02:12]   BCS #457 JINDALSTEL: Close failed — manual intervention needed.',
    ])
    d = digest.build(DAY)
    joined = ' | '.join(d['flags'])
    assert 'short_leg_close_failed' in joined, joined
    assert 'close_failed' in joined
    assert 'monitor' in joined, 'the failure is not attributed to an engine'
    assert 'short_leg_close_failed' in digest.render(d)


def test_the_engine_that_failed_is_named(logs):
    """"Something failed" is not actionable. The digest says WHO."""
    _mon(logs, ['[10:02:11]   BCS #1 X: spot fetch failed: Too many requests'])
    _zeb(logs, ['2026-08-28 10:03:00,000 [ERROR] zebra.monitor: '
                'LTP fetch failed [rate_limit]: Too many requests'])
    ev = {(e['engine'], e['name']): e['count']
          for e in digest.build(DAY)['engines']['events']}
    assert ev[('monitor', 'spot_fetch_failed')] == 1
    assert ev[('monitor', 'rate_limited')] == 1
    assert ev[('zebra', 'rate_limited')] == 1


# ── 2. silence is never clean ───────────────────────────────────────────────

def test_a_missing_monitor_log_degrades_gracefully_AND_says_so(logs):
    """A day the monitor never ran must still produce a digest — and must not
    read as a clean day. `feedback_watchdog_must_not_all_clear`."""
    _zeb(logs, ['2026-08-28 10:00:00,000 [INFO] zebra.monitor: === CYCLE START x ==='])
    d = digest.build(DAY)
    assert isinstance(digest.render(d), str)
    assert d['engines']['events'] == []
    joined = ' | '.join(d['flags'])
    assert 'ABSENT' in joined, joined
    assert 'no monitor failure is covered' in joined


def test_an_unparseable_monitor_log_fails_loudly_rather_than_counting_zero(logs):
    """The reason the omission survived: the monitor's format is
    `[HH:MM:SS] ...` and zebra's `_TS` cannot match it, so a naive read
    yielded NOTHING rather than an error. If the format changes again, the
    digest must say the counts are UNKNOWN, not print zero."""
    _mon(logs, ['2026-08-28T10:02:11Z level=error close failed',
                '2026-08-28T10:02:12Z level=error close failed again'])
    d = digest.build(DAY)
    probs = d['engines']['problems']
    assert probs, 'a file that parses to nothing reported no problem'
    assert 'UNKNOWN' in probs[0], probs
    assert any('UNKNOWN' in f for f in d['flags'])


def test_an_unnamed_failure_shaped_line_is_surfaced_not_swallowed(logs):
    """Vocabulary drift is what let a take-profit clear the arming gate. A
    line that looks like a failure and matches no named event is REPORTED, so
    the catalogue cannot go quietly out of date."""
    _mon(logs, ['[10:02:11]   WARNING: No bid depth for FOO26FEB100CE, retrying...'])
    d = digest.build(DAY)
    assert d['engines']['uncatalogued_total'] == 1
    assert any('match NO named event' in f for f in d['flags']), d['flags']


def test_a_multi_line_banner_is_not_dropped(logs):
    """`log(f"\\n  *** FLIPPED POSITION ***")` puts the `[HH:MM:SS]` prefix on
    an EMPTY first line and the payload on the next with no timestamp. A
    parser keeping only timestamped lines drops exactly the loudest events in
    the file — this is the Feb-2026 shape."""
    _mon(logs, [
        '[10:02:11] ',
        '  *** FLIPPED POSITION — NO ORDERS WILL BE PLACED ***',
        '[10:02:11]   short=+700 long=+700 (expected short<0, long>0)',
    ])
    d = digest.build(DAY)
    ev = {e['name']: e for e in d['engines']['events']}
    assert 'flipped_position' in ev, ev
    assert ev['flipped_position']['first'] == '10:02:11', \
        'the continuation did not inherit its timestamp'


# ── 3. a count is a NAMED event ─────────────────────────────────────────────

def test_a_named_event_is_counted_not_merely_tallied(logs):
    """"17 ERROR lines" is what the digest had. Two distinct failures with
    the same severity must not collapse into one number."""
    _mon(logs, [
        '[10:00:00]   BCS #1 X: Close failed — manual intervention needed.',
        '[10:05:00]   BCS #2 Y: Close failed — manual intervention needed.',
        '[10:06:00]     FAILED to close FOO26FEB100CE after 3 attempts!',
        '[10:07:00]     ORDER CUTOFF: 15:26:00 > 15:25 — not placing',
    ])
    ev = {e['name']: e for e in digest.build(DAY)['engines']['events']}
    assert ev['close_failed']['count'] == 2
    assert ev['close_retries_exhausted']['count'] == 1
    assert ev['order_cutoff_missed']['count'] == 1
    assert ev['close_failed']['first'] == '10:00:00'
    assert ev['close_failed']['last'] == '10:05:00'


def test_one_line_may_raise_two_events_because_both_are_true(logs):
    """`spot fetch failed: Too many requests` is a rate limit AND a position
    that went un-priced. Each count must mean exactly what its name says, so
    the counts deliberately do not sum to a line count."""
    _mon(logs, ['[10:02:11]   BCS #1 X: spot fetch failed: Too many requests'])
    ev = {e['name']: e['count']
          for e in digest.build(DAY)['engines']['events']}
    assert ev['rate_limited'] == 1 and ev['spot_fetch_failed'] == 1


@pytest.mark.parametrize('line,name', [
    ('!!! CRITICAL: SHORT CALL CLOSE FAILED — naked risk remains !!!',
     'short_leg_close_failed'),
    ('  *** SHORT RESIDUE REMAINS — LONG LEG WILL NOT BE SOLD ***',
     'naked_short_averted'),
    ('!!! Short is closed - naked long remains           !!!',
     'naked_long_remains'),
    ('  *** FLIPPED POSITION — NO ORDERS WILL BE PLACED ***',
     'flipped_position'),
    ('  EXPIRY FORCE CLOSE FAILED after 3 attempt(s).',
     'expiry_force_close_failed'),
    ('  *** CLOSE NOT PRICED — REFUSING TO BOOK ***', 'close_not_priced'),
    ('  Could not set partial_close status: boom',
     'partial_close_freeze_failed'),
    ('  LATE-DAY GUARD: 15:26 > 15:25.', 'late_day_guard'),
    ('    ORDER REJECTED: insufficient margin. Will not retry',
     'order_rejected'),
    ('  *** RECONCILE FAILED: leg qty mismatch ***', 'reconcile_failed'),
    ('  MALFORMED RECORD bcs:9: no long_symbol — NOT MONITORED, skipping.',
     'malformed_record'),
    ('  *** BCS STORE QUARANTINED: bad json ***', 'store_quarantined'),
    ('    REFUSING to recover a fill for FOO BUY: no tagged order',
     'fill_recovery_refused'),
    ('  WARNING: heartbeat write failed (disk full)',
     'heartbeat_write_failed'),
])
def test_every_action_class_the_engine_can_emit_is_named(logs, line, name):
    """One case per failure the M14 note lists. Each is a state a human has
    to resolve, and none of them reached the digest before."""
    _mon(logs, ['[10:02:11] %s' % line])
    ev = {e['name']: e for e in digest.build(DAY)['engines']['events']}
    assert name in ev, (name, list(ev))
    assert ev[name]['severity'] == engine_log.ACTION


def test_the_catalogue_still_matches_what_the_engine_writes():
    """The vocabulary was derived by grepping the engines, not from memory.
    If a wording changes, this fails LOUDLY — the alternative is the count
    silently going to zero, which is exactly the bug being fixed here.

    Reports every drift at once: fixing them one failure at a time is how a
    reviewer stops reading the list.
    """
    src = {}
    missing = []
    for e in engine_log.CATALOGUE:
        if not e.probe:
            continue
        if e.source not in src:
            src[e.source] = (HELPER / e.source).read_text(
                encoding='utf-8', errors='replace')
        if e.probe not in src[e.source]:
            missing.append('%s: %r no longer in %s' % (e.name, e.probe, e.source))
    assert not missing, (
        'the engines no longer write these strings — update CATALOGUE in '
        'zebra/engine_log.py:\n  ' + '\n  '.join(missing))


def test_every_catalogue_pattern_actually_compiles_and_is_unique():
    names = [e.name for e in engine_log.CATALOGUE]
    assert len(names) == len(set(names)), 'duplicate event name'
    for e in engine_log.CATALOGUE:
        re.compile(e.pattern)
        assert e.severity in (engine_log.ACTION, engine_log.DEGRADED)
        assert e.note, '%s has no note — a name without a note is a tally' % e.name


# ── 4. a blip is not a failed close ─────────────────────────────────────────

def test_a_wall_of_rate_limits_does_not_bury_one_failed_close(logs):
    """2026-08-28 had 70 rate limits. If severity were flat, the one line
    that needs a human would be row 71."""
    lines = ['[09:%02d:%02d]   BCS #1 X: Spot=100.00 | TP:100.0(+1.0) | '
             'SL:90.0(+1.0) [QUOTE-FAIL Too many requests]' % (16 + i // 2, (i % 2) * 30)
             for i in range(50)]
    lines.append('[10:02:11]   BCS #1 X: Close failed — manual intervention needed.')
    _mon(logs, lines)
    d = digest.build(DAY)
    action = [e for e in d['engines']['events']
              if e['severity'] == engine_log.ACTION]
    degraded = [e for e in d['engines']['events']
                if e['severity'] == engine_log.DEGRADED]
    assert {e['name'] for e in action} == {'close_failed', 'manual_intervention'}
    assert sum(e['count'] for e in degraded) >= 100
    # every ACTION event gets its own flag; the degraded wall gets ONE
    flags = d['flags']
    assert sum(1 for f in flags if 'close_failed' in f) == 1
    assert sum(1 for f in flags if 'degraded event(s)' in f) == 1


# ── 5. un-priced time, the risk nothing reported ────────────────────────────

def test_unwatched_time_is_reported_per_position(logs):
    """2026-08-28: JINDALSTEL sat at `[QUOTE-FAIL Too many requests]` for
    minutes at a stretch. A stop that reads the option book could not have
    fired in that window, and no per-line count shows it — the DURATION does.
    """
    _mon(logs, [
        _status('09:20:00', 'BCS #457 JINDALSTEL', 1188.0, 18.1),
        _status('09:20:30', 'BCS #457 JINDALSTEL', 1188.0,
                tag=' [QUOTE-FAIL Too many requests]'),
        _status('09:21:00', 'BCS #457 JINDALSTEL', 1188.0,
                tag=' [QUOTE-FAIL Too many requests]'),
        _status('09:23:00', 'BCS #457 JINDALSTEL', 1188.0, 18.2),
        _status('09:23:30', 'BCS #426 PERSISTENT', 5778.0, 117.0),
    ])
    uw = {u['position']: u for u in digest.build(DAY)['engines']['unwatched']}
    assert 'BCS #426 PERSISTENT' not in uw, 'a fully priced position was flagged'
    j = uw['BCS #457 JINDALSTEL']
    assert j['stretches'] == 1
    assert j['longest_sec'] == 150      # 09:20:30 -> 09:23:00
    assert j['total_sec'] == 150
    assert j['why'] == 'quote failed'
    assert not j['open_at_end']
    assert any('JINDALSTEL' in f and 'no book-driven stop' in f
               for f in digest.build(DAY)['flags'])


def test_a_short_blip_is_reported_but_not_flagged(logs):
    """One missed 30s status tick is noise. Two minutes is a window in which a
    stop could not have fired. The row still appears; only the flag is gated,
    so nothing is hidden."""
    _mon(logs, [
        _status('09:20:00', 'BCS #1 X', 100.0, 5.0),
        _status('09:20:30', 'BCS #1 X', 100.0, tag=' [QUOTE-FAIL boom]'),
        _status('09:21:00', 'BCS #1 X', 100.0, 5.0),
    ])
    d = digest.build(DAY)
    assert d['engines']['unwatched'][0]['longest_sec'] == 30
    assert not any('no book-driven stop' in f for f in d['flags'])
    assert 'BCS #1 X' in digest.render(d)


def test_a_stretch_still_open_at_the_last_observation_is_kept_and_marked(logs):
    """It is not silently extended to the close of the session, and it is not
    dropped either — dropping it would erase the worst case, a position that
    was never priced again."""
    _mon(logs, [
        _status('09:20:00', 'BCS #1 X', 100.0, 5.0),
        _status('09:20:30', 'BCS #1 X', 100.0, tag=' [QUOTE-FAIL boom]'),
        _status('09:26:30', 'BCS #1 X', 100.0, tag=' [QUOTE-FAIL boom]'),
    ])
    u = digest.build(DAY)['engines']['unwatched'][0]
    assert u['open_at_end'] and u['longest_sec'] == 360


def test_a_rejected_book_counts_as_unpriced_too(logs):
    """`[SUSPECT ...]` means a book arrived and the reliability gate refused
    it. For risk that is the same state as no book at all: no valuation, so
    debit SL, trail and the intrinsic floor could not fire."""
    _mon(logs, [
        _status('09:20:00', 'BPS #449 WAAREEENER', 2613.0, 38.2),
        _status('09:20:30', 'BPS #449 WAAREEENER', 2613.0,
                tag=' [COOLDOWN] [SUSPECT long wide_book width 1.2 vs mid 3.4]'),
        _status('09:21:30', 'BPS #449 WAAREEENER', 2613.0, 38.0),
    ])
    u = digest.build(DAY)['engines']['unwatched'][0]
    assert u['longest_sec'] == 60 and u['why'] == 'book rejected'


def test_a_stall_is_only_reported_when_a_position_was_due(logs):
    """A day with an empty book is silent for hours because the engine has
    nothing to say. Measuring raw line gaps called 09:39->15:30 a 351-minute
    stall on 2026-07-07, a day with zero open trades. A stall is the absence
    of something DUE."""
    _mon(logs, ['[09:39:21]   Watchlist: LTP fetch failed: read timeout',
                '[15:29:00] Market closed for the day. Cron monitor exiting.'])
    assert digest.build(DAY)['engines']['stalls'] == []


def test_a_gap_in_the_position_status_stream_IS_a_stall(logs):
    """The negative control for the test above."""
    _mon(logs, [_status('09:20:00', 'BCS #1 X', 100.0, 5.0),
                _status('09:35:00', 'BCS #1 X', 100.0, 5.0)])
    d = digest.build(DAY)
    assert d['engines']['stalls'] == [('09:20', '09:35', 15)]
    assert any('order engine silent 15m' in f for f in d['flags'])


def test_the_after_hours_cron_restart_is_not_a_stall(logs):
    """Outside 09:15-15:30 the cron starts every five minutes and exits at
    once. Five identical rows on every quiet day is noise that trains the
    reader to skip the section where a real stall would appear."""
    _mon(logs, [_status('15:35:00', 'BCS #1 X', 100.0, 5.0),
                _status('15:40:00', 'BCS #1 X', 100.0, 5.0),
                _status('15:45:00', 'BCS #1 X', 100.0, 5.0)])
    assert digest.build(DAY)['engines']['stalls'] == []


def test_the_engine_mode_is_reported(logs):
    """A digest full of clean exits means something different when the engine
    that would have placed the orders was in dry run."""
    _mon(logs, ['[09:00:30]   Mode:   DRY RUN',
                '[09:00:30]   Trades: 9 open (BCS: 5, BPS: 4, FH: 0)'])
    assert digest.build(DAY)['engines']['mode'] == 'DRY RUN'


# ── 6. everything the digest already did, unchanged ─────────────────────────

def test_the_new_section_is_purely_additive_to_the_rendered_digest(logs):
    """The arming gate, funnel, vetting and cohort sections must render
    exactly as before. Proven by reconstruction, not by eye: strip the new
    block and the new flags, and what is left must equal the digest rendered
    with no engine data at all."""
    _mon(logs, ['[10:02:11]   BCS #1 X: Close failed — manual intervention needed.'])
    _zeb(logs, ['2026-08-28 10:00:00,000 [INFO] zebra.monitor: === CYCLE START x ===',
                '2026-08-28 10:05:00,000 [INFO] zebra.scanner: Scanner: 51 raw '
                '→ 1 added | skipped: gap_too_wide=28',
                # a zebra-side flag, so the stripped digest is not the
                # "nothing flagged" branch — that would compare two different
                # shapes and prove nothing.
                '2026-08-28 10:06:00,000 [WARNING] zebra.vet: VET STARVED #404 — x'])
    d = digest.build(DAY)
    eng_lines = engine_log.render(d['engines'])
    eng_flags = set('- ' + f for f in engine_log.flags(d['engines']))
    assert eng_flags, 'the fixture produced no engine flag to strip'

    lines = digest.render(d).splitlines()
    i = lines.index('## Engines')
    assert lines[i:i + len(eng_lines)] == eng_lines
    assert lines[i + len(eng_lines)] == '', 'the section must end with a blank'
    rest = [ln for ln in lines[:i] + lines[i + len(eng_lines) + 1:]
            if ln not in eng_flags]

    bare = dict(d)
    bare.pop('engines')
    bare['flags'] = [f for f in d['flags'] if ('- ' + f) not in eng_flags]
    assert '\n'.join(rest) == digest.render(bare)


def test_the_pre_existing_flags_keep_their_text_and_order(logs):
    """`_flags` gained a parameter. Every call site that knows nothing about
    the order engine — and every test written before it existed — must keep
    working, and the flags it produced must be unchanged and still in order.
    """
    args = ({'gaps': [('10:00', '10:35', 35)]},
            {'events': {'starved': 1}, 'blocks_at': [], 'transcripts_tiny': 0,
             'transcripts': 0},
            {'closed': []}, {}, digest._cohort([]), None)
    old = digest._flags(*args)
    eng = {'logs': [], 'events': [{'engine': 'monitor', 'name': 'close_failed',
                                   'severity': engine_log.ACTION, 'count': 1,
                                   'first': '10:00', 'last': '10:00',
                                   'note': 'n'}],
           'uncatalogued': [], 'uncatalogued_total': 0, 'unwatched': [],
           'stalls': [], 'problems': [], 'mode': None}
    new = digest._flags(*(args + (eng,)))
    assert [f for f in new if f in old] == old, 'an existing flag changed'
    assert any('close_failed' in f for f in new)


def test_the_digest_still_writes_nothing_to_any_store():
    """Read-only by construction: both modules run beside a live money system.
    The digest gained a second FILE to read; it must not have gained a write.
    """
    for name in ('digest', 'engine_log'):
        src = (HELPER / 'zebra' / ('%s.py' % name)).read_text(encoding='utf-8')
        for banned in ('_mutate', 'mark_entered', 'mark_exited', 'add_signal',
                       '.cancel(', 'save_trades', 'write_text('):
            if name == 'digest' and banned == 'write_text(':
                continue          # digest.write() persists the digest itself
            assert banned not in src, '%s touches the store via %s' % (name, banned)


# ── 7. the real 2026-08-28 log, if it is still on disk ──────────────────────

def test_the_real_2026_08_28_log_reports_the_rate_limit_wall(monkeypatch):
    """Regression anchor on the day this was found. READ-ONLY: it points the
    log directory at the real one and calls the parser, which never writes.

    The M15 note says 58; the file's full-day count is 70. 58 was true at
    10:02 when the note was written and the session ran on to 10:13. The
    assertion is therefore `>= 58`, and the exactness lives in the digest.
    """
    real = HELPER / 'logs'
    if not (real / 'spread_monitor_cron_20260828.log').exists():
        pytest.skip('the 2026-08-28 monitor log has been archived')
    monkeypatch.setattr(cfg, 'LOG_DIR', real)
    a = engine_log.analyse('2026-08-28')
    ev = {e['name']: e['count'] for e in a['events']}
    assert ev['rate_limited'] >= 58, ev
    assert ev['quote_fail'] >= 50
    assert a['problems'] == []
    jindal = [u for u in a['unwatched'] if 'JINDALSTEL' in u['position']]
    assert jindal and jindal[0]['longest_sec'] >= 120, \
        'the un-priced stretches that nothing reported are missing'
