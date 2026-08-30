"""The daily digest — the paper run's only durable record of what happened.

It is deterministic and offline ON PURPOSE: the one outage this system has had
was a Claude usage limit, and an EOD agent would compete for the same budget as
the next morning's entry vets.

What these tests protect is honesty of counting. A digest that inflates a
number, or silently omits a section, is worse than none — it gets believed.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import digest                 # noqa: E402

DAY = '2026-08-14'


def _log(tmp_path, lines):
    p = tmp_path / f"cron_zebra_{DAY.replace('-', '')}.log"
    p.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return p


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    (tmp_path / 'zebra_trades.json').write_text('[]', encoding='utf-8')
    return tmp_path


def test_the_scan_count_is_not_summed_across_cycles(logdir):
    """62 cycles over the same ~51 Chartink symbols reported "3204 raw", which
    reads as throughput and is really double-counting. `added` IS cumulative —
    each one is a distinct new signal — so the two are counted differently."""
    # The REAL arrow the scanner writes is a unicode one; '->' appears when a
    # locale mangles it. Both must parse, or the digest silently counts zero.
    line = ('2026-08-14 %02d:00:00,000 [INFO] zebra.scanner: Scanner: 51 raw '
            '%s 1 added | skipped: gap_too_wide=28, already_open=5')
    _log(logdir, [line % (h, '→' if h % 2 else '->')
                  for h in range(10, 16)])
    f = digest._funnel(digest._read_log(DAY))
    assert f['raw_per_cycle'] == 51, "raw was totalled instead of reported"
    assert f['scans'] == 6
    assert f['added'] == 6, "new signals must accumulate"
    assert f['skipped_per_cycle']['gap_too_wide'] == 28


def test_a_missed_cycle_is_detected(logdir):
    """cron fires every 5 minutes. A long gap is unmonitored time on open
    positions and appears in NO per-event count."""
    _log(logdir, [
        '2026-08-14 10:00:00,000 [INFO] zebra.monitor: === CYCLE START x ===',
        '2026-08-14 10:05:00,000 [INFO] zebra.monitor: === CYCLE START x ===',
        '2026-08-14 10:40:00,000 [INFO] zebra.monitor: === CYCLE START x ===',
    ])
    c = digest._cycles(digest._read_log(DAY))
    assert c['cycles'] == 3
    assert len(c['gaps']) == 1
    assert c['gaps'][0][2] == 35


def test_the_2026_08_14_incident_is_flagged_from_the_log_alone(logdir):
    """The day this was built. A starved entry, an exit on the guards alone,
    and a wall of near-empty transcripts — all of which took a human reading
    364KB of log to notice."""
    _log(logdir, [
        '2026-08-14 14:05:09,864 [WARNING] zebra.vet: VET STARVED #404 — no agent slot. NOT entered.',
        '2026-08-14 13:40:31,294 [WARNING] zebra.vet: EXIT VET TIMED OUT #390 debit_sl — proceeding',
        '2026-08-14 13:05:00,000 [WARNING] zebra.vet: CLI BLOCKED — usage limit',
    ])
    for i in range(3):
        (logdir / f'vet_cli_20260814_1300{i}0_entry-vet-{i}.log').write_text(
            'x' * 100, encoding='utf-8')
    d = digest.build(DAY)
    joined = ' | '.join(d['flags'])
    assert 'STARVED' in joined
    assert 'deterministic guards' in joined
    assert 'quota' in joined
    assert 'near-empty' in joined or 'almost no output' in joined


def test_flags_state_facts_not_diagnoses(logdir):
    """"Vet latency p90 8m" earns a look. "The layer is degraded" is a
    conclusion a script has not earned, and a wrong one trains the reader to
    ignore the right ones."""
    _log(logdir, ['2026-08-14 14:05:09,864 [WARNING] zebra.vet: VET STARVED #404 — x'])
    for f in digest.build(DAY)['flags']:
        low = f.lower()
        for banned in ('degraded', 'broken', 'is failing', 'unhealthy'):
            assert banned not in low, f"flag draws a conclusion: {f}"


def test_an_empty_section_is_STATED_not_omitted(logdir):
    """An absent section reads as an oversight. "No entries today" is a fact
    the paper run needs — a day that vetted signals and entered none is the
    thing to notice, not to hide."""
    _log(logdir, ['2026-08-14 10:00:00,000 [INFO] zebra.monitor: === CYCLE START x ==='])
    text = digest.render(digest.build(DAY))
    assert '## Opened' in text and 'none' in text
    assert '## Closed' in text


def test_the_running_list_is_idempotent_and_keeps_human_ticks(logdir):
    """Guaranteed to be re-run during the manual first week. A plain append
    duplicated the whole day each time — the list that exists to stop
    observations dropping would fill with copies of them instead."""
    p = logdir / 'FLAGS.md'
    flags = ['alpha happened', 'beta happened']
    for _ in range(3):
        digest._merge_flags(p, DAY, flags)
    assert p.read_text(encoding='utf-8').count(f'## {DAY}') == 1
    # a human triages one, and the digest runs again
    p.write_text(p.read_text(encoding='utf-8').replace('- [ ] alpha', '- [x] alpha'),
                 encoding='utf-8')
    digest._merge_flags(p, DAY, flags)
    body = p.read_text(encoding='utf-8')
    assert '- [x] alpha happened' in body, "a re-run un-triaged a handled item"
    assert '- [ ] beta happened' in body


def test_a_missing_log_produces_a_digest_rather_than_an_exception(logdir):
    """A digest is a convenience. It must never be able to matter more than
    that — least of all by raising inside a cron that also runs the monitor."""
    d = digest.build('2019-01-01')
    assert d['cycles']['cycles'] == 0
    assert isinstance(digest.render(d), str)


def test_the_digest_never_writes_to_the_trade_store(logdir):
    """Read-only by construction: it runs beside a live money system."""
    src = (HELPER / 'zebra' / 'digest.py').read_text(encoding='utf-8')
    for banned in ('_mutate', 'mark_entered', 'mark_exited', 'add_signal',
                   '.cancel(', 'save_trades'):
        assert banned not in src, f"digest touches the store via {banned}"


def test_it_is_wired_as_a_cli_verb():
    src = (HELPER / 'zebra' / '__main__.py').read_text(encoding='utf-8')
    assert "add_parser(\n        'digest'" in src or "add_parser('digest'" in src
    assert 'cmd_digest' in src


# ── the Opened table reads a rate that can now legitimately be None ───────
# `in_progress` (2026-08-18) means a symbol whose only departure is still
# running has NO completed episodes and therefore no rate. Before that,
# `touch_rate_pct` was never None on `overall` and the table's `.get` default
# was never exercised.

def _opened_with_rate(logdir, rate, episodes):
    # STAMPED, like every real entered record. Without it this is a LEGACY
    # position by definition (`in_cohort`: absence is the answer), and since
    # 2026-08-31 the digest scopes its rows to this engine — so an unstamped
    # fixture silently produces an empty digest and the sibling
    # `'None%' not in ...` assertion passes for the wrong reason.
    row = {'id': 1, 'stock': 'X', 'direction': 'CE', 'status': 'entered',
           'entry_date': DAY, 'dte_at_entry': 30, 'debit': 10.0,
           'capital': 500000, 'cohort': cfg.COHORT_START,
           'vet': {'context': {'st_attraction': {
               'overall': {'episodes': episodes, 'touch_rate_pct': rate},
               'median_days_to_touch': None}}}}
    (logdir / 'zebra_trades.json').write_text(json.dumps([row]),
                                              encoding='utf-8')
    return digest.render(digest.build(DAY))


def test_a_symbol_with_no_completed_episodes_does_not_print_None_pct(logdir):
    """`.get(key, default)` does NOT fall back when the key exists holding
    None, so the table printed the literal text `None%` — in the one column an
    entry decision is argued from."""
    assert 'None%' not in _opened_with_rate(logdir, None, 0)


def test_a_genuine_zero_rate_is_still_shown(logdir):
    """The other half. `rate or '-'` is the obvious one-liner and it erases a
    real 0.0% — the single most veto-worthy reading this column can hold."""
    assert '0.0%' in _opened_with_rate(logdir, 0.0, 5)


# ── The arming gate ─────────────────────────────────────────────────────────
#
# Owner's decision 2026-08-26: the exit path is not armed until the cohort has
# produced real STOP exits in paper. Every cohort close so far is `paper:tp`,
# which is a SPOT trigger -- so the machinery automation actually risks (debit
# SL, trail, spot veto, intrinsic floor, reliability gate, time SL) has zero
# cohort evidence, and that is the path both real-money losses came from.
#
# The gate is reported on every digest so it can only be MET, never forgotten.

def _exit(i, reason, pnl=100.0, cohort='2026-08-14'):
    return {'id': i, 'stock': 'X%d' % i, 'status': 'exited', 'cohort': cohort,
            'exit_reason': reason, 'pnl': pnl, 'pnl_net': pnl - 5,
            'pnl_net_pct': 10.0, 'fees': {'basis': 'modelled'}}


# ── The running total is AS OF the digest's own date ────────────────────────
#
# `_cohort` read the whole store regardless of `day`. Correct for the same-day
# run and a LOOK-AHEAD for any other: backfilling an older session's digest
# (which is exactly what recovering a missing log enables — M18) would print a
# cohort total containing trades that closed days LATER than the file claims to
# describe. The digest is the paper run's dated record; a dated record must not
# quietly contain the future.


def _dated(i, reason, day, pnl=100.0):
    t = _exit(i, reason, pnl)
    t['exit_date'] = day
    return t


def test_the_running_total_excludes_trades_that_close_after_the_digest_date():
    rows = [_dated(1, 'paper:tp', '2026-08-24'),
            _dated(2, 'paper:tp', '2026-08-26'),
            _dated(3, 'paper:tp', '2026-08-28')]
    assert digest._cohort(rows, '2026-08-24')['closed'] == 1
    assert digest._cohort(rows, '2026-08-26')['closed'] == 2
    assert digest._cohort(rows, '2026-08-28')['closed'] == 3


def test_a_day_before_the_first_close_reports_an_empty_cohort():
    """Not an error, and not the whole book either."""
    rows = [_dated(1, 'paper:tp', '2026-08-24')]
    coh = digest._cohort(rows, '2026-08-19')
    assert coh['closed'] == 0 and coh['net'] == 0


def test_the_gate_is_judged_as_of_the_date_too():
    """A stop that has not happened yet must not clear an earlier day's gate."""
    rows = [_dated(1, 'paper:tp', '2026-08-24'),
            _dated(2, 'paper:debit_sl', '2026-08-28', -50)]
    assert digest._cohort(rows, '2026-08-24')['stop_exits'] == 0
    assert digest._cohort(rows, '2026-08-28')['stop_exits'] == 1


def test_an_undated_exit_is_counted_not_silently_folded_in():
    """It cannot be placed in time, so it is excluded and REPORTED."""
    rows = [_dated(1, 'paper:tp', '2026-08-24'), _exit(2, 'paper:tp')]
    coh = digest._cohort(rows, '2026-08-28')
    assert coh['closed'] == 1
    assert coh['undated'] == 1


def test_no_date_still_means_the_whole_book():
    """The unbounded call is still available and still unbounded."""
    rows = [_dated(1, 'paper:tp', '2026-08-24'), _exit(2, 'paper:tp')]
    assert digest._cohort(rows)['closed'] == 2


def test_the_real_store_as_of_08_26_matches_what_that_day_reported():
    """Cross-check against history: FLAGS.md for 2026-08-26 says "all 4 cohort
    exit(s) are TP". If the bounding is right, as-of that date gives 4."""
    import json
    p = HELPER / 'logs' / 'zebra_trades.json'
    if not p.exists():                       # pragma: no cover - CI without logs
        pytest.skip('no local trade store')
    rows = json.loads(p.read_text(encoding='utf-8'))
    coh = digest._cohort(rows, '2026-08-26')
    assert coh['closed'] == 4, coh
    assert coh['stop_exits'] == 0


def test_a_cohort_of_pure_tps_reports_the_gate_unmet():
    coh = digest._cohort([_exit(1, 'paper:tp'), _exit(2, 'paper:tp')])
    assert coh['stop_exits'] == 0
    flags = digest._flags({'gaps': []},
                          {'events': {}, 'blocks_at': [], 'transcripts_tiny': 0,
                           'transcripts': 0},
                          {'closed': []}, {}, coh, None)
    assert any('ARMING GATE UNMET' in f for f in flags), flags


def test_one_real_stop_exit_flips_the_gate():
    """Negative control. Without this the test above would also pass against a
    digest that printed the warning unconditionally."""
    coh = digest._cohort([_exit(1, 'paper:tp'), _exit(2, 'paper:debit_sl', -50)])
    assert coh['stop_exits'] == 1
    flags = digest._flags({'gaps': []},
                          {'events': {}, 'blocks_at': [], 'transcripts_tiny': 0,
                           'transcripts': 0},
                          {'closed': []}, {}, coh, None)
    assert not any('UNMET' in f for f in flags), flags
    assert any('1 cohort stop exit' in f and 'debit_sl' in f for f in flags), flags


@pytest.mark.parametrize('reason', ['debit_sl', 'spot_sl', 'trail', 'time'])
def test_every_non_tp_reason_counts_as_stop_evidence(reason):
    """TP is the ONLY spot-triggered exit. Everything else reads the option
    book, which is the thing with no cohort evidence."""
    coh = digest._cohort([_exit(1, 'paper:' + reason, -50)])
    assert coh['stop_exits'] == 1, reason


def test_the_paper_prefix_is_stripped():
    """Every exit in paper mode is stamped `paper:<reason>`. Without stripping
    it, `!= 'tp'` is true for a plain TP too and the gate reads as MET on a
    cohort of pure take-profits -- arming on the strength of no evidence."""
    coh = digest._cohort([_exit(1, 'paper:tp'), _exit(2, 'paper:tp')])
    assert coh['exit_reasons'] == {'tp': 2}
    assert coh['stop_exits'] == 0


def test_an_empty_cohort_does_not_claim_the_gate_either_way():
    """No closes is not evidence of anything. `feedback_watchdog_must_not_all_clear`
    -- and equally it must not raise an alarm about a book that does not exist."""
    coh = digest._cohort([])
    assert coh['closed'] == 0 and coh['stop_exits'] == 0
    flags = digest._flags({'gaps': []},
                          {'events': {}, 'blocks_at': [], 'transcripts_tiny': 0,
                           'transcripts': 0},
                          {'closed': []}, {}, coh, None)
    assert not any('ARMING GATE' in f for f in flags), flags


def test_a_legacy_exit_cannot_satisfy_the_gate():
    """The back ratio is dropped and its 70 debit_sl exits are out of scope.
    Only `cohort == '2026-08-14'` rows count, or the gate is met by history
    that has nothing to do with what is going live."""
    coh = digest._cohort([_exit(1, 'paper:tp'),
                          _exit(2, 'paper:debit_sl', -50, cohort=None)])
    assert coh['closed'] == 1
    assert coh['stop_exits'] == 0
