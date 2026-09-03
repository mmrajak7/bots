"""The evidence must outlive the logs it came from.

WHY (2026-09-03). Four live decisions rest on a replay of POLL observations:
the trail engage level, the rejection of EOD harvest, the reading of COALINDIA
#440 as an overnight gap, and the first cohort-native MAE table. Every one of
those observations lived only in `logs/cron_zebra_*.log`, which
`common.log_cleanup` gzips at 7 days and DELETES at 90 -- so the August
sessions age out around early December, plausibly before the cohort reaches the
~30 closes at which those decisions are due to be re-derived.

`test_the_trail_engage_cliff` instructs the next person to "re-run the replay
before changing this". Without capture, that instruction expires silently, and
nothing fails when it does -- the same shape as the digest cron that went
uninstalled for 18 days, and FIFTY's breadth capture that looked deployed and
recorded nothing.

So the tests that matter here are not "does it parse". They are:
  * does a captured day still say the same thing after the log is gone,
  * is re-running it safe (the deploy_server.sh lesson),
  * does it tell the truth about what is NOT yet captured.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_value_paths.py -v
"""
import gzip
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg               # noqa: E402
from zebra import value_paths as vp           # noqa: E402

# Two real POLL lines, copied verbatim from logs/cron_zebra_20260902.log --
# one fully quoted, one blind. A synthetic line would test the regex against
# itself.
LINE_OK = ('2026-09-02 09:20:14,015 [INFO] zebra.monitor: POLL #440 COALINDIA '
           'PE spot=418.95 tp=390.96 sl=418.75 | value=3.20 (ok) '
           'debit_sl=2.90 | long 4.95/5.1 short 1.65/1.75')
LINE_BLIND = ('2026-09-02 09:15:16,829 [INFO] zebra.monitor: POLL #440 '
              'COALINDIA PE spot=410.45 tp=390.96 sl=418.75 | '
              'value=NA (no_two_way_book) debit_sl=2.90 | '
              'long None/None short None/None')
LINE_LATCHED = ('2026-09-02 09:25:09,721 [INFO] zebra.monitor: POLL #449 '
                'WAAREEENER PE spot=2555.50 tp=2561.00 [TP-LATCHED] '
                'sl=2698.81 | value=45.15 (ok) debit_sl=18.18 | '
                'long 91.65/93.15 short 45.7/46.5')
NOISE = '2026-09-02 09:20:19,012 [INFO] bcs.drive_store: Updated 488 trades'


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    (tmp_path / 'eod').mkdir()
    return tmp_path


def _session(logs, day='20260902', lines=(LINE_OK, LINE_BLIND, NOISE),
             gz=False):
    name = 'cron_zebra_%s.log' % day
    body = '\n'.join(lines) + '\n'
    if gz:
        with gzip.open(str(logs / (name + '.gz')), 'wt') as fh:
            fh.write(body)
        return logs / (name + '.gz')
    (logs / name).write_text(body, encoding='utf-8')
    return logs / name


# ── parsing: the fields a replay actually needs ────────────────────────────

def test_the_fill_basis_value_and_the_quote_QUALITY_both_survive(logs):
    """The pair is the point. Dropping `q` is what turned the overnight-gap
    measurement from +1.3% of debit into -12.1%: the first prints after the
    open are VALUE BOUND clamps, not prices, and only `q` distinguishes them.
    """
    got = vp.parse_session(_session(logs))
    obs = got[440]['obs']
    assert [o['val'] for o in obs] == [None, 3.20]
    assert [o['q'] for o in obs] == ['no_two_way_book', 'ok']
    assert obs[1]['long_bid'] == 4.95 and obs[1]['short_ask'] == 1.75


def test_a_blind_poll_is_recorded_as_ABSENT_not_as_zero(logs):
    """`value=NA` means the engine could not price the structure. Storing 0.0
    would read later as a spread worth nothing -- a -100% observation invented
    out of a missing quote."""
    obs = vp.parse_session(_session(logs))[440]['obs'][0]
    assert obs['val'] is None
    assert obs['long_bid'] is None and obs['long_ask'] is None
    assert obs['spot'] == 410.45, 'spot is still real on a debit-blind poll'


def test_a_tp_latched_line_still_parses(logs):
    """The monitor inserts `[TP-LATCHED]` mid-line once an exit is armed. A
    regex that missed it would silently drop every observation AFTER the most
    interesting moment in the position's life."""
    got = vp.parse_session(_session(logs, lines=(LINE_LATCHED,)))
    assert got[449]['obs'][0]['val'] == 45.15
    assert got[449]['stock'] == 'WAAREEENER'


def test_non_poll_lines_are_ignored(logs):
    got = vp.parse_session(_session(logs))
    assert set(got) == {440}


def test_a_gzipped_session_reads_the_same(logs):
    """Sessions older than 7 days are gzipped in place, and those are exactly
    the ones at risk -- if capture could not read `.gz` it would only ever
    save days that were never in danger."""
    plain = vp.parse_session(_session(logs, day='20260901'))
    gz = vp.parse_session(_session(logs, day='20260828', gz=True))
    assert [o['val'] for o in gz[440]['obs']] == \
           [o['val'] for o in plain[440]['obs']]


# ── capture: idempotence and honesty ───────────────────────────────────────

def test_capture_writes_a_day_and_says_what_it_saw(logs):
    """RETIRES WHEN: the capture writes through a store class with its own
    contract test, rather than straight to a JSON file this test must open by
    hand to check the payload."""
    _session(logs)
    r = vp.capture()
    assert r['written'] == ['2026-09-02']
    d = json.loads(vp.out_path('2026-09-02').read_text(encoding='utf-8'))
    assert d['observations'] == 2
    assert d['basis'] == 'fill', 'a replay that assumes mid would be wrong'
    assert d['source'] == 'cron_zebra_20260902.log'


def test_re_running_capture_changes_nothing(logs):
    """SAFE-TO-RERUN in the same sense the shell-script policy means it. The
    `deploy_server.sh` incident is what a "one-time" job costs when a second
    run is destructive -- here a second run must not even rewrite the file.
    """
    _session(logs)
    vp.capture()
    before = vp.out_path('2026-09-02').read_bytes()
    r = vp.capture()
    assert r['written'] == [] and r['already_had'] == ['2026-09-02']
    assert vp.out_path('2026-09-02').read_bytes() == before


def test_force_re_extracts(logs):
    _session(logs)
    vp.capture()
    assert vp.write_day('2026-09-02', force=True) is not None


def test_the_capture_survives_the_log_being_deleted(logs):
    """THE WHOLE POINT, in one test. The cleaner deletes the log at 90 days;
    the captured evidence must still answer the question afterwards.

    RETIRES WHEN: the capture writes through a store class with its own
    contract test, rather than straight to a JSON file this test must open by
    hand to read the payload back.
    """
    src = _session(logs)
    vp.capture()
    src.unlink()
    d = json.loads(vp.out_path('2026-09-02').read_text(encoding='utf-8'))
    assert d['trades']['440']['obs'][1]['val'] == 3.20
    assert vp.coverage()['observations'] == 2


def test_a_missed_day_is_still_picked_up_later(logs):
    """Capture runs over EVERY session on disk, not just today's, so a cron
    that did not run on Tuesday is not a Tuesday lost -- it has until the log
    ages out."""
    _session(logs, day='20260901')
    vp.capture()
    _session(logs, day='20260902')
    assert vp.capture()['written'] == ['2026-09-02']
    assert len(vp.coverage()['captured']) == 2


def test_an_unparseable_log_does_not_stop_the_others(logs):
    """Losing one day is small. A capture that dies on one bad file and stops
    saving the rest is the failure this guards."""
    (logs / 'cron_zebra_20260901.log').write_bytes(b'\xff\xfe not a log at all')
    _session(logs, day='20260902')
    r = vp.capture()
    assert '2026-09-02' in r['written']


# ── coverage: it must be able to say it is NOT working ─────────────────────

def test_coverage_names_the_sessions_still_only_in_a_log(logs):
    """An all-clear that cannot go red is decorative. `at_risk` is the field
    that makes this module observable -- it names days whose numbers exist
    only in a file scheduled for deletion."""
    _session(logs, day='20260901')
    _session(logs, day='20260902')
    assert vp.coverage()['at_risk'] == ['2026-09-01', '2026-09-02']
    vp.capture()
    cov = vp.coverage()
    assert cov['at_risk'] == []
    assert cov['captured'] == ['2026-09-01', '2026-09-02']


def test_a_plain_log_wins_over_its_own_gz_twin(logs):
    """During compression both can exist for a moment. The plain file is the
    one still being appended to, so it is the fuller of the two.

    RETIRES WHEN: the capture writes through a store class with its own
    contract test, rather than straight to a JSON file this test must open by
    hand to check which source won."""
    _session(logs, day='20260902', gz=True, lines=(LINE_OK,))
    _session(logs, day='20260902', lines=(LINE_OK, LINE_BLIND))
    vp.capture()
    d = json.loads(vp.out_path('2026-09-02').read_text(encoding='utf-8'))
    assert d['observations'] == 2, 'the truncated gz copy won'
    assert not d['source'].endswith('.gz')
