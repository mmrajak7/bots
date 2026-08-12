"""Regressions for the third adversarial review (2026-08-12).

Every test here asserts the CORRECT behaviour and fails if its fix is reverted
— that is the point, and it was checked by reverting each one in turn. The
findings clustered into three families:

1. **The scoring pipeline counted verdicts the store had refused.** A journalled
   decision looked identical whether it governed the signal or was thrown away
   for arriving late, so the layer could be credited with a trade that entered
   unvetted.
2. **The watchdog reported healthy while the layer was dead.** Three probes in
   an if/elif chain behind a credential check that always succeeds.
3. **A lost compare-and-set was read as consent.** The one place the exit gate
   could fire on a book Claude had just refused to verify.

Run:  cd Helper && python -m pytest zebra/tests/test_review_fixes.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg              # noqa: E402
from zebra import health                     # noqa: E402
from zebra import outcomes                   # noqa: E402
from zebra import vet as vet_mod             # noqa: E402
from zebra.decisions import DecisionStore    # noqa: E402
from zebra.trade_store import ZebraStore     # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
ENTRY = {'long_strike': 96.0, 'short_strike': 100.0,
         'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
         'debit': 10.0, 'lot_size': 100, 'lots': 1, 'expiry': '2026-09-24'}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    return tmp_path


@pytest.fixture
def store(paths):
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    return s


@pytest.fixture
def decisions(tmp_path):
    return DecisionStore(path=tmp_path / 'd.json',
                         lock_path=tmp_path / 'd.lock').initialize()


# ── 1. only an APPLIED verdict is evidence ───────────────────────────────
def test_a_discarded_allow_is_never_scored_as_the_layers_own(store, decisions):
    """The Critical. An agent that overruns its 10-minute deadline is an
    engineered-for case, not misbehaviour: the signal fails open and enters
    UNVETTED, and the late `allow` is discarded. But the journal row was
    written BEFORE the store refused it, so the join used to attribute that
    unvetted trade's P&L to the layer — flattering the very score that decides
    whether it ever gets live authority."""
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    # Deadline blows; the sweep fails it open and it enters unvetted.
    with store._mutate():
        t = store._must_find(1)
        t['vet']['deadline'] = (datetime.now() - timedelta(minutes=1)).isoformat()
    vet_mod.expire_stale(store)
    assert vet_mod.vet_state(store.find(1)) == vet_mod.UNAVAILABLE

    d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    outcome = vet_mod.record_verdict(store, 1, vet_mod.ALLOWED,
                                     decision_id=d['id'])
    assert outcome.startswith('discarded')
    decisions.mark_discarded(d['id'], outcome)

    store.mark_entered(1, dict(ENTRY))
    store.mark_exited(1, 110.0, 20.0, 'paper:tp')
    assert outcomes.join(store, decisions) == 0, \
        "a refused verdict was joined to a trade it never authorised"
    assert decisions.score()['allow']['n'] == 0


def test_a_retried_agent_cannot_double_count_one_trade(store, decisions):
    """The CLI is explicitly allowed to retry (`vet decide` exits 0 on a
    discard so a retry loop does not hammer). Both calls journal, only the
    first is applied — so n must be 1, not 2."""
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    for i in range(2):
        d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                             stock='TESTCO', direction='CE')
        out = vet_mod.record_verdict(store, 1, vet_mod.ALLOWED,
                                     decision_id=d['id'])
        (decisions.mark_acted if out == 'applied'
         else lambda i: decisions.mark_discarded(i, out))(d['id'])
    store.mark_entered(1, dict(ENTRY))
    store.mark_exited(1, 110.0, 20.0, 'paper:tp')
    outcomes.join(store, decisions)
    assert decisions.score()['allow']['n'] == 1, "one trade scored twice"


def test_score_itself_refuses_an_unacted_row(decisions):
    """Belt and braces, and separately tested because the pending_outcome gate
    alone would hide a hand-edited or legacy row that already carries an
    outcome. Both scores must independently refuse to count a verdict the store
    never applied."""
    d = decisions.record(kind='entry', verdict='allow', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    decisions.mark_discarded(d['id'], 'discarded (deadline passed)')
    decisions.set_outcome(d['id'], {'basis': 'realized', 'pnl': 5000.0,
                                    'label': 'hit'})
    assert decisions.score()['allow']['n'] == 0
    assert decisions.score()['signal_quality']['allow']['n'] == 0


def test_a_discarded_row_does_not_pend_forever(store, decisions):
    """A veto landing after the fail-open flip has no shadow and no position,
    so it can never settle — it used to be re-scanned every five minutes for
    the life of the journal."""
    d = decisions.record(kind='entry', verdict='veto', trade_ids=[1],
                         stock='TESTCO', direction='CE')
    decisions.mark_discarded(d['id'], 'discarded (already unavailable)')
    assert decisions.pending_outcome(kind='entry') == []
    # The reasoning is KEPT — it is audit history, just not evidence.
    assert decisions.find(d['id'])['discarded_reason'].startswith('discarded')


def test_a_verdict_past_its_deadline_is_void_as_the_doc_promises(store):
    """VETTING.md tells the agent 'past it your verdict is void'. That was true
    only by the accident of sweep timing — inside the window between the
    deadline and the next cycle's sweep, a late verdict was applied."""
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    with store._mutate():
        t = store._must_find(1)
        t['vet']['deadline'] = (datetime.now() - timedelta(seconds=1)).isoformat()
    assert vet_mod.record_verdict(store, 1, vet_mod.VETOED) == \
        'discarded (deadline passed)'
    assert vet_mod.vet_state(store.find(1)) == vet_mod.PENDING


# ── the journal must survive the Pi ──────────────────────────────────────
class _FakeDrive:
    """Stands in for the Drive round trip: one remote blob, union-merged."""
    def __init__(self):
        self.remote = None
        self.uploads = 0


@pytest.fixture
def fake_drive(monkeypatch, tmp_path):
    import zebra.decisions as dec
    fd = _FakeDrive()

    def _init(self, drive_cfg):
        self._drive_service, self._drive_enabled = 'svc', True
        self._drive_file_id = 'fid' if fd.remote is not None else None
    monkeypatch.setattr(dec.DecisionStore, '_init_drive', _init)
    monkeypatch.setattr(dec.DecisionStore, '_drive_name', lambda self: 'x.json')

    import bcs.drive_store as ds
    monkeypatch.setattr(ds, 'download_json', lambda svc, fid: fd.remote or [])
    monkeypatch.setattr(ds, 'find_file',
                        lambda svc, folder, name: 'fid' if fd.remote is not None
                        else None)

    def _upload(svc, folder, name, rows, fid):
        fd.remote = [dict(r) for r in rows]
        fd.uploads += 1
        return 'fid'
    monkeypatch.setattr(ds, 'upload_json', _upload)
    return fd


def _dstore(tmp_path, name, fake=True):
    from zebra.decisions import DecisionStore
    s = DecisionStore(path=tmp_path / (name + '.json'),
                      lock_path=tmp_path / (name + '.lock'),
                      config={'google_drive': {'enabled': True,
                                               'folder_id': 'f',
                                               'sync_interval_sec': 0}})
    s._drive_wanted = fake
    return s.initialize()


def test_the_journal_reaches_drive_and_comes_back(tmp_path, fake_drive):
    """The decision journal is the ONLY evidence that decides whether this
    layer earns live authority, and it lived on the Pi's SD card alone —
    unreadable from the machine the user actually runs reports on, and gone if
    that card dies."""
    a = _dstore(tmp_path, 'a')
    d = a.record(kind='entry', verdict='veto', trade_ids=[1], stock='TESTCO',
                 direction='CE', reasons=['results inside the window'])
    a.mark_acted(d['id'])
    assert fake_drive.uploads >= 1

    b = _dstore(tmp_path, 'b')          # a DIFFERENT machine, empty locally
    rows = b.all()
    assert len(rows) == 1 and rows[0]['reasons'] == ['results inside the window']
    assert rows[0]['acted'] is True


def test_two_machines_do_not_mint_the_same_decision_id(tmp_path, fake_drive):
    """Ids are max(id)+1, so two stale writers both mint #1 and the union-merge
    — which keys on id — silently drops one agent's reasoning. record() pulls
    from Drive before allocating."""
    # BOTH stores initialise while Drive is empty — the case that matters. If
    # `b` only synced at startup it would still believe the journal is empty
    # when it writes, and would mint #1 a second time.
    a = _dstore(tmp_path, 'a')
    b = _dstore(tmp_path, 'b')
    a.record(kind='entry', verdict='allow', trade_ids=[1], stock='AAA',
             direction='CE')
    b.record(kind='entry', verdict='veto', trade_ids=[2], stock='BBB',
             direction='PE')
    assert sorted(r['id'] for r in fake_drive.remote) == [1, 2]
    assert {r['signal_ref']['stock'] for r in fake_drive.remote} == {'AAA', 'BBB'}


def test_a_drive_outage_never_blocks_a_verdict(tmp_path, fake_drive,
                                               monkeypatch):
    """Local is operational truth; Drive is durability. An agent must never be
    told its verdict failed to land because a network call did."""
    import bcs.drive_store as ds

    def boom(*a, **k):
        raise RuntimeError('network down')
    monkeypatch.setattr(ds, 'upload_json', boom)
    monkeypatch.setattr(ds, 'download_json', boom)
    a = _dstore(tmp_path, 'a')
    d = a.record(kind='entry', verdict='allow', trade_ids=[1], stock='TESTCO',
                 direction='CE')
    a.mark_acted(d['id'])
    assert a.find(d['id'])['acted'] is True          # landed locally regardless


def test_a_test_store_never_touches_the_real_drive(tmp_path):
    """Every test in this package constructs a store with an explicit path.
    Those must be Drive-blind by construction — the 2026-08-11 lesson was that
    a rail nobody has to remember is the only rail that holds."""
    from zebra.decisions import DecisionStore
    s = DecisionStore(path=tmp_path / 'z.json', lock_path=tmp_path / 'z.lock')
    assert s._drive_wanted is False
    s.initialize()
    assert s._drive_enabled is False


# ── 2. the watchdog cannot report a false all-clear ──────────────────────
def _cred(tmp_path, days):
    import json
    p = tmp_path / 'creds.json'
    when = (datetime.now() + timedelta(days=days)).timestamp() * 1000
    p.write_text(json.dumps({'claudeAiOauth': {'refreshTokenExpiresAt': when}}))
    return p


def test_a_healthy_credential_does_not_mask_dying_agents(paths):
    """The Critical. The probes sat in `elif` branches under the credential
    check — and on the Pi that check always succeeds, so a missing binary, a
    denied tool permission or a crashing agent reported perfectly healthy for
    the ~28 days until the credential itself aged out."""
    cred = _cred(paths, days=20)                 # nowhere near expiry
    for _ in range(health.SILENT_SPAWN_LIMIT):
        health.record_spawn_result(True, 'entry')
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True, paths=[cred])
    assert len(sent) == 1, "a healthy credential silenced the behavioural probe"
    assert 'NOT REPORTING BACK' in sent[0]


def test_a_healthy_credential_does_not_mask_a_binary_that_will_not_start(paths):
    """The other half of the same Critical: `spawn_failures` was also chained
    behind the credential check, so a CLI that cannot start at all — the single
    most likely first-deploy failure — reported healthy too."""
    cred = _cred(paths, days=20)
    for _ in range(3):
        health.record_spawn_result(False, 'entry')
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True, paths=[cred])
    assert sent and 'NOT STARTING' in sent[0]


def test_one_healthy_channel_does_not_clear_another_channels_alarm(paths):
    """A single global counter is reset by whichever agent happens to succeed.
    The Sonnet calendar agent runs every two hours and is the most likely to
    work, so it would have kept resetting the alarm while every Fable vetting
    agent died — the threshold then being effectively unreachable."""
    for _ in range(health.SILENT_SPAWN_LIMIT):
        health.record_spawn_result(True, 'entry')
    health.record_agent_landed('events')         # the easy channel is fine
    assert health.silent_channels() == ['entry']
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'none.json'])
    assert sent and 'entry' in sent[0]


# ── 3. a lost CAS is not consent ─────────────────────────────────────────
def test_a_defer_landing_at_the_deadline_is_not_overridden(paths, monkeypatch):
    """The exit gate's timeout path CAS'd the marker to `unavailable` and then
    returned 'proceed' WITHOUT checking whether the write landed. In `zebra
    loop` the cached read can be a full cycle old, so a `defer` that arrives in
    that window was silently overridden and the exit fired on the very book
    Claude had just said it could not verify — the NHPC direction exactly."""
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    s.mark_triggered(1, 96.0, 4.0, [])
    s.mark_entered(1, dict(ENTRY))
    quote = {'reliable': False, 'reason': 'one-sided book', 'mid': 3.0}

    assert vet_mod.exit_gate(s, s.find(1), 'debit_sl', quote, 96.0,
                             spawn=False) == 'wait'
    stale = s.find(1)                       # the caller's cached copy
    with s._mutate():                       # deadline passes...
        m = s._must_find(1)['exit_vet']['debit_sl']
        m['deadline'] = (datetime.now() - timedelta(seconds=1)).isoformat()
    stale['exit_vet']['debit_sl']['deadline'] = \
        s.find(1)['exit_vet']['debit_sl']['deadline']
    # ...and the agent's `defer` lands before we act on the timeout.
    vet_mod.record_exit_verdict(s, 1, 'debit_sl', 'defer')

    gate = vet_mod.exit_gate(s, stale, 'debit_sl', quote, 96.0, spawn=False)
    assert gate != 'proceed', "fired an exit on a book Claude refused to verify"
    assert vet_mod.exit_state(s.find(1), 'debit_sl') == vet_mod.DEFER


def test_an_escalated_hold_stops_respawning_agents(paths):
    """A persistently untradeable book used to restart the whole episode —
    agent included — every 15 minutes, ~30 Fable runs a day for one position,
    all to re-reach an escalation the user had already been told about."""
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    s.mark_triggered(1, 96.0, 4.0, [])
    s.mark_entered(1, dict(ENTRY))
    quote = {'reliable': False, 'reason': 'no depth', 'mid': 3.0}
    for _ in range(cfg.EXIT_MAX_DEFERS):
        vet_mod.exit_gate(s, s.find(1), 'debit_sl', quote, 96.0, spawn=False)
        vet_mod.record_exit_verdict(s, 1, 'debit_sl', 'defer')
    assert vet_mod.exit_gate(s, s.find(1), 'debit_sl', quote, 96.0,
                             spawn=False) == 'hold'
    # Well past the ordinary verdict TTL, the hold still holds — and, crucially,
    # has not re-requested a vet.
    later = datetime.now() + timedelta(seconds=cfg.EXIT_VET_TTL_SEC + 60)
    marker = vet_mod._exit_marker(s.find(1), 'debit_sl')
    assert vet_mod._marker_fresh(marker, now=later), \
        "the escalated hold went stale and would respawn the whole episode"


# ── the side channels must actually run from the real cycle ──────────────
def test_run_cycle_actually_drives_every_side_channel(paths, monkeypatch):
    """Four "wired but never executes" bugs in this fleet, three of them an
    early `continue` above the new call. The one wiring assertion that existed
    covered `expire_stale` only — every side channel was wired by reading, not
    by test. This drives the REAL `run_cycle` and names each channel that ran.
    """
    from zebra import monitor
    ran = []
    s = ZebraStore(config={})
    s._load_local()
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, syms: {})
    monkeypatch.setattr(monitor.outcomes_mod, 'track_shadows',
                        lambda *a, **k: ran.append('shadows') or [])
    monkeypatch.setattr(monitor.outcomes_mod, 'join',
                        lambda *a, **k: ran.append('join') or 0)
    monkeypatch.setattr(monitor.review_mod, 'run',
                        lambda *a, **k: ran.append('review') or [])
    monkeypatch.setattr(monitor.events_mod, 'is_stale',
                        lambda *a, **k: ran.append('events') or False)
    monkeypatch.setattr(monitor.health_mod, 'check',
                        lambda *a, **k: ran.append('health'))
    monkeypatch.setattr(monitor.vet_mod, 'expire_stale',
                        lambda *a, **k: ran.append('expire') or [])
    monitor.run_cycle(s, kite=None, dry_run=True, do_scan=False)
    assert ran == ['expire', 'shadows', 'join', 'review', 'events', 'health']


def test_a_side_channel_failure_cannot_stop_the_others(paths, monkeypatch):
    """Observation must never be able to break trading, or each other."""
    from zebra import monitor
    ran = []
    s = ZebraStore(config={})
    s._load_local()
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, syms: {})

    def boom(*a, **k):
        raise RuntimeError('drive down')
    monkeypatch.setattr(monitor.outcomes_mod, 'track_shadows', boom)
    monkeypatch.setattr(monitor.outcomes_mod, 'join', boom)
    monkeypatch.setattr(monitor.review_mod, 'run',
                        lambda *a, **k: ran.append('review') or [])
    monkeypatch.setattr(monitor.health_mod, 'check',
                        lambda *a, **k: ran.append('health'))
    monitor.run_cycle(s, kite=None, dry_run=True, do_scan=False)
    assert ran == ['review', 'health']


# ── the CLI must be findable where cron actually runs ────────────────────
def test_the_cli_is_resolved_to_an_absolute_path(monkeypatch):
    """`claude` was invoked by bare name. Debian cron's PATH is /usr/bin:/bin
    and the CLI installs to ~/.local/bin, so every spawn on the Pi would have
    raised FileNotFoundError — the layer inert from its first cycle."""
    import os
    resolved = vet_mod.resolve_cli(refresh=True)
    if resolved is None:
        pytest.skip('no claude CLI installed on this machine')
    assert os.sep in resolved and os.path.isfile(resolved)


def test_a_missing_cli_is_recorded_as_a_spawn_failure(paths, real_spawn):
    """It must fail OPEN (return None, never raise) and must be VISIBLE.

    Before the watchdog fix these two were the same silence: the spawn failed,
    the counter incremented, and nothing ever read the counter."""
    assert real_spawn('prompt', 'fable', 'test', channel='entry') is None
    assert health._read_state().get('spawn_failures', 0) >= 1
    for _ in range(2):
        real_spawn('prompt', 'fable', 'test', channel='entry')
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'none.json'])
    assert sent and 'NOT STARTING' in sent[0]
