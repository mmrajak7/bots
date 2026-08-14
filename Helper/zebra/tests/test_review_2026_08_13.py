"""Regressions for the 2026-08-13 four-reviewer pass.

Each test below corresponds to a defect that was live in production when the
engine was deployed on 2026-08-12. They are grouped by the property they
protect rather than by the file they touch, because every one of these bugs was
a property that no single file was responsible for.

Run:  cd Helper && python -m pytest zebra/tests/test_review_2026_08_13.py -v
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg           # noqa: E402
from zebra import monitor                 # noqa: E402
from zebra import vet as vet_mod          # noqa: E402
from zebra.trade_store import ZebraStore   # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore(config={})
    s._load_local()
    return s


def _signal(s, stock='TESTCO'):
    return s.add_signal({'stock': stock, 'timeframe': 'weekly', 'direction': 'CE',
                         'st_value': 100.0, 'st_direction': 'UP',
                         'signal_price': 96.0, 'signal_gap_pct': 4.0})


def _enter(s, tid, expiry_days=30):
    """Entered, and stamped `bcs` — `_alerts_enabled` gates Telegram to the
    alerting structure, so a zebra-structured fixture is silent by design and
    every assertion about an alert would vacuously fail."""
    t = s.mark_entered(tid, {
        'long_strike': 96.0, 'short_strike': 100.0,
        'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
        'debit': 10.0, 'lot_size': 100, 'lots': 1,
        'expiry': (datetime.now() + timedelta(days=expiry_days)
                   ).strftime('%Y-%m-%d'),
    })
    with s._mutate():
        s.find(tid)['structure'] = 'bcs'
        s.find(tid)['width'] = 4.0
    return t


# ── the agent may not run the trading cycle ──────────────────────────────
# The allow rule is deliberately coarse (`-m zebra:*`) and the invariant is
# carried by the deny list. That list named the five position VERBS and not the
# four that CALL them, so `zebra run` — which opens positions, closes them and
# recursively spawns more agents — was granted and denied by nothing.
CYCLE_VERBS = ['run', 'loop', 'scan', 'report']
POSITION_VERBS = ['close', 'enter', 'cancel', 'reset', 'trigger']
# Added 2026-08-13 by re-applying the same test to the verbs written since.
# `postmortem run` calls spawn_batch -> _spawn_generic (a SPAWNER, the exact
# class this list exists to catch); `events replace` overwrites the shared
# event calendar that the corporate-action interlock reads, and --allow-empty
# can blank it.
SPAWNER_VERBS = ['postmortem run']
# Denied on ARGV to every channel EXCEPT the one whose job it is. Deliberately
# NOT in settings.json: that file is all-or-nothing and applies to every spawn,
# so a per-channel rule placed there disables the channel that needs it — which
# is exactly what happened on 2026-08-14, taking the calendar refresher down.
PER_CHANNEL_VERBS = ['events replace']


@pytest.mark.parametrize(
    'verb', POSITION_VERBS + CYCLE_VERBS + SPAWNER_VERBS + PER_CHANNEL_VERBS)
def test_the_deny_list_covers_the_callers_not_just_the_callees(verb):
    assert any(verb in pattern for pattern in cfg.VET_DENIED_TOOLS), \
        f"`zebra {verb}` is not denied to spawned agents"


@pytest.mark.parametrize('verb', PER_CHANNEL_VERBS)
def test_a_per_channel_deny_is_NOT_in_the_settings_backstop(verb):
    """settings.json is all-or-nothing and applies to every spawned agent.

    A rule that must vary by channel cannot live there: the per-channel
    carve-out in `vet._denied_tools` only edits the argv --disallowedTools
    list, and nothing on argv can grant back what this file denies. Putting
    `events replace` here on 2026-08-14 therefore denied it to the events
    channel — whose entire job is running it — and took down the calendar
    refresher that the corporate-action interlock reads. From the outside the
    failure is indistinguishable from an auth error.
    """
    data = json.loads((HELPER / '.claude' / 'settings.json').read_text())
    deny = data['permissions']['deny']
    assert not any(verb in pattern for pattern in deny), (
        f"`{verb}` is per-channel and must NOT be in settings.json — it would "
        f"disable the channel that needs it, and argv cannot grant it back")


@pytest.mark.parametrize('verb', POSITION_VERBS + CYCLE_VERBS + SPAWNER_VERBS)
def test_the_settings_backstop_matches_the_spawn_deny_list(verb):
    """settings.json is the layer that still applies when a HUMAN runs claude
    interactively on the Pi, where no --disallowedTools flag is in play. It
    drifting behind config.py is the same hole in a place nobody looks."""
    data = json.loads((HELPER / '.claude' / 'settings.json').read_text())
    deny = data['permissions']['deny']
    assert any(verb in pattern for pattern in deny), \
        f"`zebra {verb}` missing from the settings.json backstop"


# ── a paper layer may not brown out a box running live money ─────────────
def test_the_spawn_budget_refuses_beyond_the_cap(store, monkeypatch, tmp_path):
    """One market-wide event row makes `needs_review` true for EVERY open
    position in the same cycle — 24 detached node processes on a Pi that also
    runs the live-money monitor. The per-position daily caps bound how often one
    trade is looked at and say nothing about how many start together."""
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 3)
    # Grants return a slot TOKEN (str); a refusal returns ''.
    allowed = [bool(vet_mod._spawn_budget_ok(f'agent-{i}')) for i in range(6)]
    assert allowed[:3] == [True, True, True]
    assert allowed[3:] == [False, False, False], \
        'the budget did not bound the fan-out'


def test_the_budget_forgets_agents_older_than_their_own_kill_deadline(
        store, monkeypatch, tmp_path):
    """It bounds CONCURRENCY, not lifetime spawns — otherwise the layer stops
    vetting forever after the first busy cycle."""
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 2)
    assert vet_mod._spawn_budget_ok('a') and vet_mod._spawn_budget_ok('b')
    assert not vet_mod._spawn_budget_ok('c')
    stale = [0.0, 0.0]                       # long past CHILD_KILL_SEC
    (tmp_path / 'zebra_spawn_budget.json').write_text(json.dumps(stale))
    assert vet_mod._spawn_budget_ok('d'), 'dead agents still held the budget'


def test_a_broken_budget_file_fails_open(store, monkeypatch, tmp_path):
    """A bookkeeping failure must not become a silent vetting halt."""
    (tmp_path / 'zebra_spawn_budget.json').write_text('{not json')
    assert vet_mod._spawn_budget_ok('x'), 'a broken budget file halted vetting'


# ── ids are never reissued ───────────────────────────────────────────────
def test_a_quarantined_store_does_not_reissue_ids(store, monkeypatch):
    """`max(id)+1` is an allocator only while the list is COMPLETE. After
    `_read_local` quarantines a corrupt file it returns [], so this handed out
    1, 2, 3 again — and `_merge` is keyed on id with the higher version winning,
    so a recycled id silently REPLACES a real trade once Drive returns."""
    for n in range(3):
        _signal(store, 'STOCK%d' % n)
    assert [t['id'] for t in store.load_trades()] == [1, 2, 3]

    # Simulate the quarantine: the file is unparseable, so the store restarts
    # empty while Drive is unavailable.
    cfg.LOCAL_FILE.write_text('{ truncated')
    fresh = ZebraStore(config={})
    fresh._load_local()
    assert fresh.load_trades() == [], 'precondition: store should start empty'

    t = _signal(fresh)
    assert t['id'] == 4, f"id {t['id']} was reissued over a real trade"


def test_the_quarantine_leaves_a_marker_for_the_monitor_to_alert(store):
    """Quarantine empties the book, and an empty book makes check_entered
    return at its first line — so the one event that stops ALL exit monitoring
    is the one `_alert_monitoring_blind` structurally cannot report, because
    that alert requires a non-empty book."""
    cfg.LOCAL_FILE.write_text('{ truncated')
    fresh = ZebraStore(config={})
    fresh._load_local()
    marker = cfg.LOG_DIR / 'zebra_store_corrupt.json'
    assert marker.exists(), 'corruption left no marker'
    assert 'backup' in json.loads(marker.read_text())

    sent = []
    monitor._send_telegram = lambda m, **k: sent.append(m) or True
    assert monitor._alert_store_corruption() is True
    assert len(sent) == 1 and 'STORE CORRUPT' in sent[0]
    # ...and exactly once for the same event.
    assert monitor._alert_store_corruption() is False
    assert len(sent) == 1


# ── TIME does not book at the opening auction ────────────────────────────
def test_the_time_close_is_held_until_the_open_buffer_has_passed(
        store, monkeypatch):
    """All 34 `paper:time` exits in the real book priced between 09:15:35 and
    09:18:05 — not one later in the day, while every other reason spreads
    across the session. The daily flag was claimed by the 09:15 cycle and the
    close ran straight off it, inside the very window
    VALUE_TRIGGER_OPEN_BUFFER_SEC exists to sit out."""
    _signal(store)
    _enter(store, 1, expiry_days=1)
    closed, sent = [], []
    monkeypatch.setattr(monitor, '_paper_auto_close',
                        lambda *a, **k: closed.append(a[3]))
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, '_value_triggers_live', lambda *a, **k: False)

    monitor._time_nag(store, store.find(1), datetime.now().date(),
                      12.0, 101.0, {}, dry_run=True)
    assert closed == [], 'TIME booked inside the opening buffer'
    assert len(sent) == 1, 'the calendar NAG must still go out on time'


def test_the_time_close_retries_every_cycle_not_once_a_day(store, monkeypatch):
    """The close used to hang off the same daily flag as the nag, so a defer on
    a momentarily unusable book cost the position a whole session rather than
    one poll."""
    _signal(store)
    _enter(store, 1, expiry_days=1)
    closed = []
    monkeypatch.setattr(monitor, '_paper_auto_close',
                        lambda *a, **k: closed.append(a[3]))
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    monkeypatch.setattr(monitor, '_value_triggers_live', lambda *a, **k: True)

    today = datetime.now().date()
    for _ in range(3):
        monitor._time_nag(store, store.find(1), today, 12.0, 101.0, {},
                          dry_run=True)
    assert closed == ['time', 'time', 'time'], \
        'the close is still gated on the once-a-day nag flag'


# ── one bad record may not halt the book ─────────────────────────────────
def test_a_malformed_position_does_not_stop_the_ones_after_it(
        store, monkeypatch):
    """The per-trade body indexes directly and calls a store that can raise
    LockTimeout, and run_cycle catches only at the PHASE level — so one
    hand-edited or half-merged row aborted every position sorted after it,
    silently, and took the cycle's peak and corroboration patches with it."""
    _signal(store, 'BADCO')
    _enter(store, 1)
    _signal(store, 'GOODCO')
    _enter(store, 2)
    with store._mutate():
        del store.find(1)['tp_spot']          # the malformed row, checked first

    polled = []
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'BADCO': 101.0, 'GOODCO': 101.0})

    def quote(kite, trade, spot=None):
        polled.append(trade['stock'])
        return {'mid': 12.0, 'reliable': True, 'reason': '', 'legs': {},
                'rejected': None}

    monkeypatch.setattr(monitor, '_structure_quote', quote)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)

    monitor.check_entered(store, kite=None, dry_run=True)
    assert 'GOODCO' in polled, \
        'a malformed row still halts exit monitoring for the rest of the book'


# ── a dead underlying is neither immortal nor invisible ──────────────────
def test_a_position_whose_underlying_stops_quoting_is_reported(
        store, monkeypatch, caplog):
    """`spot <= 0` was a bare `continue`: no POLL line, no counter, no alert,
    while the scanner's dedup went on banning the stock forever.
    `_alert_monitoring_blind` cannot cover it — that fires only when EVERY
    position is unpriceable, so one dead symbol among healthy ones is invisible
    by construction."""
    _signal(store, 'DEADCO')
    _enter(store, 1)
    _signal(store, 'LIVECO')
    _enter(store, 2)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'DEADCO': 0.0, 'LIVECO': 101.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda *a, **k: {'mid': 12.0, 'reliable': True,
                                         'reason': '', 'legs': {},
                                         'rejected': None})
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)

    with caplog.at_level('WARNING'):
        monitor.check_entered(store, kite=None, dry_run=True)

    assert any('NO SPOT' in r.message for r in caplog.records), \
        'a dead underlying still leaves no trace in the log'
    assert any('NO SPOT' in m for m in sent), 'and told nobody'
    # The healthy position was still evaluated: its TP (spot 101 >= 100) fired,
    # which it could only do by the loop continuing past the dead symbol.
    assert store.find(2)['status'] == 'exited', \
        'the dead symbol swallowed the rest of the book'


def test_a_dead_underlying_past_expiry_still_settles(store, monkeypatch):
    """Otherwise the row stays `entered` forever, outliving its own contract
    and holding its stock out of the scanner permanently. `_expire_if_ancient`
    already rescues WATCHING rows from exactly this; entered rows had nothing.

    A live position rides along deliberately: with the dead one alone, the
    cycle takes the ALL-blind early return (a total feed outage, correctly
    alerted and handled elsewhere) and never reaches the per-trade loop. The
    bug being pinned here is one dead symbol among healthy ones.
    """
    _signal(store, 'DEADCO')
    _enter(store, 1, expiry_days=-5)              # expired last week
    _signal(store, 'LIVECO')
    _enter(store, 2)
    with store._mutate():
        store.find(1)['entry_spot'] = 120.0       # deep ITM: worth full width
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'DEADCO': 0.0, 'LIVECO': 101.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda *a, **k: {'mid': 12.0, 'reliable': True,
                                         'reason': '', 'legs': {},
                                         'rejected': None})
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)

    monitor.check_entered(store, kite=None, dry_run=True)
    assert store.find(1)['status'] == 'exited', \
        'an expired position on a dead symbol is still immortal'


# ── The ENTER alert is the order ticket (2026-08-13) ──────────────────────
# Reported by the owner on day one: "we are not working on zebra anymore -
# only BCS". The ticket still led with the retired zebra pair (BUY 2x / SELL
# 1x) and captioned the spread that actually trades as a "BCS shadow
# (paper A/B)" — i.e. it instructed the reader to place the one order the
# engine does not take, and labelled the real one as a side experiment.

def _analysis():
    return {
        'spot': 96.0, 'current_gap_pct': 4.0, 'expiry': '2026-09-29',
        'dte': 30, 'lot_size': 500,
        'best': {'k_l': 90.0, 'k_s': 96.0, 'debit': 4.0, 'be': 98.0,
                 'be_pct_from_spot': 2.08, 'gate_fails': [],
                 'capital_per_lot': 2000.0,
                 'long_symbol': 'ZEBLONG', 'short_symbol': 'ZEBSHORT',
                 'long_ask': 7.0, 'short_bid': 3.0},
    }


def _bcs():
    return {'long_strike': 96.0, 'short_strike': 104.0, 'debit': 3.2,
            'lot_size': 500, 'width': 8.0, 'short_extrinsic': 0.5,
            'debit_to_width_pct': 40.0, 'max_profit_per_share': 4.8,
            'warnings': [], 'long_symbol': 'BCSLONG', 'short_symbol': 'BCSSHORT',
            'long_ask': 5.0, 'short_bid': 1.8}


def test_the_live_bcs_ticket_names_only_the_spread(monkeypatch):
    """REWRITTEN 2026-08-13 after review. The first version of this test drove
    `_format_enter_alert`, which `check_watching` NEVER CALLS under the BCS
    pipeline — it builds the ticket with `_format_bcs_enter_alert` and
    `continue`s first. So the test passed against dead code while the live
    ticket went unasserted. Drive the function that actually sends."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_bcs_enter_alert(trade, _analysis(), _bcs())

    assert 'BCSLONG' in msg and 'BCSSHORT' in msg, 'the tradeable legs are missing'
    assert 'ZEBLONG' not in msg and 'ZEBSHORT' not in msg,         'the live ticket quotes the retired zebra legs'
    assert '2×' not in msg and '2x' not in msg,         'a 2x leg is the zebra back-ratio — never entered under BCS-only'
    assert 'shadow' not in msg.lower(),         'the structure that actually trades must not be captioned a shadow'


def test_format_enter_alert_has_no_dead_bcs_branch(monkeypatch):
    """A BCS branch was added here and was unreachable from the trading path,
    while its `not bcs` fallback WAS reachable from `cmd_trigger` (which passes
    bcs=None) and Telegrammed "NO SPREAD" about signals that had a good spread.
    Dead code that only runs on the wrong path is worse than none."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_enter_alert(trade, _analysis(), None)
    assert 'NO SPREAD' not in msg,         'cmd_trigger would Telegram a false NO SPREAD for a viable signal'


def test_the_zebra_ticket_survives_for_the_zebra_pipeline(monkeypatch):
    """The BCS-only branch must not delete the other pipeline's ticket — the
    config knob still selects it, and 15 open positions are that structure."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'zebra')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_enter_alert(trade, _analysis(), _bcs())
    assert 'ZEBLONG' in msg and 'BCS shadow' in msg


# ── Agent budget: batch channels must not starve the decision ones ────────
# Day one: three agents (review/events sweep every open position) exhausted a
# shared cap of 3, the entry vet behind them was refused, and ASHOKLEY entered
# UNVETTED. The throttle switched off the layer on exactly the decisions it
# exists to make.

def test_a_batch_channel_cannot_consume_the_last_agent_slot(monkeypatch):
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 3)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 1)
    # Two batch agents already running: review may not take the third.
    assert vet_mod._spawn_budget_ok('a', channel='review')
    assert vet_mod._spawn_budget_ok('b', channel='review')
    assert not vet_mod._spawn_budget_ok('c', channel='review'), \
        'a batch channel took the slot reserved for a trading decision'
    # ...but the entry vet still gets through.
    assert vet_mod._spawn_budget_ok('entry', channel='entry')


def test_the_decision_channels_still_share_the_total_cap(monkeypatch):
    """The reserve gives priority, not immunity — the box must stay bounded."""
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 3)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 1)
    assert [bool(vet_mod._spawn_budget_ok(str(i), channel='entry'))
            for i in range(4)] == [True, True, True, False]


def test_a_refused_spawn_says_never_asked_not_did_not_answer(monkeypatch):
    """Two different failures needing two different fixes. The alert said
    'did not answer in time' for both, which points the reader at the CLI when
    the real cause is a full budget."""
    trade = {'id': 1, 'stock': 'TESTCO',
             'vet': {'state': vet_mod.UNAVAILABLE,
                     'failed_open_because': 'no agent slot free (spawn budget)'}}
    line = monitor._vet_line(trade)
    assert 'never asked' in line
    assert 'did not answer' not in line

    trade['vet']['failed_open_because'] = None
    assert 'did not answer in time' in monitor._vet_line(trade)


# ── Funds check: live mode only (2026-08-13) ──────────────────────────────
# In live mode the ENTER alert IS the order ticket. A ticket the account
# cannot fund invites a rejected order at the one moment attention is scarce.
# In paper there is no account and no order, so it must cost nothing at all.

class _Kite:
    def __init__(self, avail, margin=None, boom=False):
        self.avail, self.margin, self.boom = avail, margin, boom
        self.calls = []

    def margins(self, seg):
        self.calls.append('margins')
        if self.boom:
            raise RuntimeError('kite down')
        return {'available': {'live_balance': self.avail}}

    def basket_order_margins(self, basket):
        self.calls.append('basket')
        if self.margin is None:
            raise RuntimeError('no basket api')
        return {'final': {'total': self.margin}}


_BCS = {'long_symbol': 'L', 'short_symbol': 'S', 'debit': 2.0,
        'long_ask': 5.5, 'short_bid': 3.5}


def test_paper_mode_never_touches_the_broker_for_funds(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    k = _Kite(avail=1_000_000, margin=10_000)
    assert monitor._funds_line(k, _BCS, 5000) == ''
    assert k.calls == [], 'paper mode called the margin API'


def test_live_mode_shouts_when_the_account_is_short(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    k = _Kite(avail=4_000, margin=10_000)
    line = monitor._funds_line(k, _BCS, 5000)
    assert 'INSUFFICIENT FUNDS' in line
    assert '6,000' in line, 'the shortfall itself must be on the ticket'


def test_live_mode_confirms_when_funded(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    line = monitor._funds_line(_Kite(avail=50_000, margin=10_000), _BCS, 5000)
    assert 'Funds OK' in line and 'INSUFFICIENT' not in line


def test_it_prefers_the_exchange_margin_over_the_debit_estimate(monkeypatch):
    """A BCS is hedged and the exchange prices the pair as one position, so a
    leg-by-leg guess is meaningfully wrong."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    # debit fallback would be 2.0 * 5000 = 10,000; exchange says 30,000.
    line = monitor._funds_line(_Kite(avail=20_000, margin=30_000), _BCS, 5000)
    assert 'INSUFFICIENT' in line and 'exchange margin' in line


def test_it_falls_back_to_the_net_debit_when_basket_margin_is_unavailable(
        monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    line = monitor._funds_line(_Kite(avail=5_000, margin=None), _BCS, 5000)
    assert 'net debit' in line and 'INSUFFICIENT' in line   # need 10,000


def test_an_unverifiable_balance_warns_but_does_not_shout(monkeypatch):
    """A definite shortfall blocks; an inability to check only warns. Losing a
    real signal to a margin-endpoint hiccup costs an opportunity to guard
    against a maybe, and a human still places the order in live mode."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    line = monitor._funds_line(_Kite(avail=0, margin=10_000, boom=True),
                               _BCS, 5000)
    assert 'Could not verify funds' in line
    assert 'INSUFFICIENT' not in line


# ── the budget counts LIVE agents, not recent starts (2026-08-13) ─────────
# Found by a Fable design review. The filter dropped an entry only once it aged
# past CHILD_KILL_SEC, so an agent finishing in 90s still held its slot for the
# full 15 minutes: the cap was really "N starts per window" (~12/hour box-wide),
# which is how a cap of 3 starved a channel that wanted one agent.

def test_a_finished_agent_releases_its_slot_immediately(store, monkeypatch,
                                                        tmp_path):
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 2)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 0)
    alive = {101: True, 102: True}
    monkeypatch.setattr(vet_mod, '_pid_alive', lambda pid: alive.get(pid, False))

    a = vet_mod._spawn_budget_ok('a'); vet_mod.claim_slot_pid(a, 101)
    b = vet_mod._spawn_budget_ok('b'); vet_mod.claim_slot_pid(b, 102)
    assert not vet_mod._spawn_budget_ok('c'), 'precondition: cap reached'

    alive[101] = False                      # that agent exited seconds later
    assert vet_mod._spawn_budget_ok('c'), \
        'a finished agent still held its slot for the whole kill window'


def test_a_spawn_that_never_reports_a_pid_frees_its_slot(store, monkeypatch,
                                                         tmp_path):
    """The slot is reserved BEFORE Popen (no pid exists yet), so a crash in
    between would leak it until CHILD_KILL_SEC."""
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 1)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 0)
    assert vet_mod._spawn_budget_ok('a')     # reserved, pid never claimed
    assert not vet_mod._spawn_budget_ok('b'), 'an in-flight spawn must hold it'

    import time as _t
    path = tmp_path / 'zebra_spawn_budget.json'
    entries = json.loads(path.read_text())
    entries[0]['t'] = _t.time() - vet_mod._UNCLAIMED_SLOT_SEC - 1
    path.write_text(json.dumps(entries))
    assert vet_mod._spawn_budget_ok('b'), 'a dead spawn leaked its slot'


def test_the_old_bare_timestamp_budget_file_does_not_break_the_layer(
        store, monkeypatch, tmp_path):
    """A Pi mid-upgrade has the pre-2026-08-13 format on disk. Crashing on it
    would take every spawn down with it."""
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 2)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 0)
    import time as _t
    (tmp_path / 'zebra_spawn_budget.json').write_text(json.dumps([_t.time()]))
    assert vet_mod._spawn_budget_ok('a'), 'the legacy format was not tolerated'


def test_pid_liveness_never_probes_off_posix(monkeypatch):
    """os.kill(pid, 0) on Windows TERMINATES the pid — it does not probe. The
    dev box runs this suite, so an unguarded call would shoot local processes."""
    import os as _os
    monkeypatch.setattr(vet_mod.os, 'name', 'nt')
    killed = []
    monkeypatch.setattr(vet_mod.os, 'kill',
                        lambda *a: killed.append(a))
    assert vet_mod._pid_alive(4242) is True
    assert killed == [], 'os.kill was reached on a non-posix host'


# ── the entry queue: fail CLOSED, but never silently (2026-08-13) ─────────
# "there is no rush to enter. opportunity always exists in market - only
# qualified entry long time saves capital" — the owner. A missed entry costs
# nothing; an unqualified one costs capital.

def _queued(s, tid=1):
    return (s.find(tid).get('vet') or {})


def test_a_refused_spawn_queues_instead_of_entering_unvetted(store, monkeypatch):
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    monkeypatch.setattr(vet_mod, '_spawn_cli', lambda tid: None)   # refused
    vet_mod.request_entry_vet(store, 1, context={})
    assert vet_mod.vet_state(store.find(1)) == vet_mod.QUEUED
    assert store.find(1)['status'] == 'triggered', 'a refused vet entered'


def test_the_drop_deadline_is_anchored_and_cannot_be_walked_forward(store):
    """Repeated requeues must not extend the wait forever — the classic way an
    'awaiting approval' state becomes a permanent halt."""
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.queue_entry_vet(store, 1, 'first')
    first = _queued(store)['drop_after']
    for _ in range(3):
        vet_mod.queue_entry_vet(store, 1, 'again')
    assert _queued(store)['drop_after'] == first, 'the drop deadline moved'


def test_the_queue_gives_up_after_max_attempts_and_never_enters(store,
                                                                monkeypatch):
    monkeypatch.setattr(cfg, 'ENTRY_VET_MAX_ATTEMPTS', 2)
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    late = datetime.now() + timedelta(minutes=11)   # past the
    # 600s verdict deadline, inside the 1h queue window, so the
    # ATTEMPTS bound is what ends it rather than the clock.
    vet_mod.expire_stale(store, now=late)                 # attempt 1 -> queued
    assert vet_mod.vet_state(store.find(1)) == vet_mod.QUEUED
    vet_mod.promote_queued(store, 1, context={}, spawn=False)
    vet_mod.expire_stale(store, now=late)                 # attempt 2 -> starved
    assert vet_mod.vet_state(store.find(1)) == vet_mod.STARVED
    assert store.find(1)['status'] != 'entered'


def test_a_dropped_entry_is_cancelled_AND_telegrammed(store, monkeypatch):
    """The guardrail that makes fail-closed safe. A broken CLI must not be able
    to stop trading quietly behind a switch that still reads ON."""
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.starve(store, 1, 'no verdict after 2 attempts')
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    assert monitor._reap_starved_vets(store) == [1]
    assert store.find(1)['status'] == 'cancelled'
    assert len(sent) == 1 and 'ENTRY DROPPED' in sent[0]


def test_a_starved_signal_can_never_reach_the_entry_path(store, monkeypatch):
    """Entering was the DEFAULT for any vet state without an explicit branch,
    so adding a state silently meant 'enter unvetted'."""
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.starve(store, 1, 'gave up')
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 96.5})
    monitor.check_watching(store, kite=None, dry_run=True)
    assert store.find(1)['status'] != 'entered'


def test_promote_is_a_cas_so_overlapping_drainers_spawn_once(store, monkeypatch):
    """cron overlap, `zebra loop` and a hand-run `zebra run` all drain the same
    queue; two agents on one signal would race two verdicts."""
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.queue_entry_vet(store, 1, 'waiting')
    spawned = []
    monkeypatch.setattr(vet_mod, '_spawn_cli',
                        lambda tid: (spawned.append(tid), 99)[1])
    assert vet_mod.promote_queued(store, 1, context={}) is True
    assert vet_mod.promote_queued(store, 1, context={}) is False, \
        'a second drainer promoted an already-pending signal'
    assert spawned == [1]


def test_the_event_write_grant_matches_both_path_forms(monkeypatch):
    """A permission pattern that matches nothing is indistinguishable from a
    broken agent: the CLI exits 0 with the work undone.

    Three cuts at this one grant, each failing the same silent way:
    1. no path scope at all (the agent could write anything);
    2. a bare absolute path — Claude Code matches file-tool patterns relative
       to the project dir and needs `//` for absolutes;
    3. `Write(path)` — REJECTED OUTRIGHT. Claude Code's own words in
       `vet_cli_20260814.log`: *"Write(...) is not matched by file permission
       checks — only Edit(path) rules are. Use Edit(...) instead (Edit rules
       cover all file-editing tools)."* `Edit` is the permission FAMILY for
       every file-editing tool, not the name of one tool.

    This test previously asserted `Write(` and so PINNED cut 3 in place — it
    passed on every run for a day while the calendar refresher was dead and the
    watchdog cried "AGENTS NOT REPORTING BACK". A test that encodes the wrong
    contract is worse than no test: it makes the bug look deliberate.
    """
    grants = cfg.EVENT_EXTRA_TOOLS
    assert len(grants) == 2, 'both path forms must be granted'
    assert all(g.startswith('Edit(') for g in grants), (
        'path-scoped grants must use the Edit(path) family; Write(path) is '
        'silently unmatched by Claude Code. See vet_cli_20260814.log.')
    assert not any(g.startswith('Write(') for g in grants), grants
    # cwd-relative form, forward slashes even on Windows
    assert any(g == 'Edit(logs/event_calendar.candidate.json)' for g in grants), \
        grants
    # absolute form, // prefixed
    assert any(g.startswith('Edit(//') for g in grants), grants
    # ...and neither widens the scope beyond the one candidate file.
    assert all('event_calendar.candidate.json' in g for g in grants)
    assert not any('**' in g or '*' in g for g in grants), \
        'the grant must not become a wildcard'


def _silent_check(monkeypatch, channels):
    """Run health.check() with the given channels silent. Returns the message."""
    import zebra.health as health
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    state = {'channels': {c: {'spawns_since_landing': n}
                          for c, n in channels.items()},
             'spawn_failures': 0}
    monkeypatch.setattr(health, '_read_state', lambda *a, **k: state)
    monkeypatch.setattr(health, 'credential_expiry', lambda *a, **k: None)
    monkeypatch.setattr(health, '_locked_state',
                        lambda *a, **k: __import__('contextlib').nullcontext({}))
    return health.check(send=lambda m, dry_run=False: True, dry_run=True)


def test_the_watchdog_names_what_a_dead_channel_COSTS(monkeypatch):
    """"events: 27" only means something to a reader holding the architecture
    in their head. The consequence that mattered — the corporate-action
    interlock reading nothing — was invisible in the message and took a code
    review to surface, on a morning the owner was watching wave one go live."""
    msg = _silent_check(monkeypatch, {'events': 27})
    assert 'events: 27' in msg
    assert 'corporate-action interlock' in msg, \
        'the alert must say what a dead events channel costs'


def test_the_watchdog_does_not_state_consequences_for_LIVE_channels(monkeypatch):
    """The regression this replaces: the trailer asserted "Entries are QUEUED,
    not entered unvetted; exits fall back to the deterministic guards"
    UNCONDITIONALLY. On the morning only `events` was dead it therefore
    announced that entries were queueing while entry vetting was working
    perfectly — a false statement about the subsystem the reader most needs to
    trust, inside the alert whose entire job is being trustworthy."""
    msg = _silent_check(monkeypatch, {'events': 27})
    assert 'QUEUE' not in msg.upper(), \
        'entry consequence stated while the entry channel is healthy'
    assert 'deterministic guards' not in msg, \
        'exit consequence stated while the exit channel is healthy'


def test_each_silent_channel_gets_its_own_line(monkeypatch):
    msg = _silent_check(monkeypatch, {'entry': 9, 'exit': 6})
    assert 'entry: 9' in msg and 'exit: 6' in msg
    assert 'DROPPED after 2 attempts' in msg
    assert 'deterministic guards' in msg
    assert 'corporate-action' not in msg, 'events is healthy here'


def test_every_channel_that_can_spawn_has_a_stated_impact():
    """A channel with no entry in the table degrades silently to a bare count —
    which is the exact failure this table was added to fix."""
    import zebra.health as health
    from zebra import vet as vet_mod
    for ch in ('entry', 'exit', 'events', 'review', 'postmortem'):
        assert ch in health.CHANNEL_IMPACT, \
            f'channel {ch} can go silent with no stated consequence'
        assert health.CHANNEL_IMPACT[ch].strip(), ch
    # And every impact string must be a consequence, not a restatement.
    for ch, txt in health.CHANNEL_IMPACT.items():
        assert len(txt) > 40, f'{ch}: impact reads like a label, not a cost'


def test_the_watchdog_does_not_assert_a_cause_it_cannot_know(monkeypatch):
    """It told the owner "expired login" while the login was fine and the real
    cause was an unmatched tool grant — sending him to re-login for nothing."""
    import zebra.health as health
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)   # check() no-ops when off
    # Drive it through the real counter rather than stubbing internals, so the
    # test breaks if the alert stops being reachable at all.
    for _ in range(health.SILENT_SPAWN_LIMIT + 1):
        health.record_spawn_result(True, 'events')
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True, dry_run=True)
    assert sent, 'the watchdog said nothing'
    msg = sent[0]
    assert 'vet_cli_' in msg, 'it must point at the agent log, which has the answer'
    assert 'usually an expired login' not in msg


# ── review of the review: gaps the three reviewers found (2026-08-13) ─────

def test_the_funds_check_is_wired_into_the_path_that_actually_trades(
        store, monkeypatch):
    """It was called 93 lines BELOW the `continue` that ends the BCS branch, so
    under the production pipeline it ran exactly never — while six unit tests
    calling `_funds_line` directly all passed. The `wire_into_live_path`
    failure shape, again."""
    import inspect
    src = inspect.getsource(monitor.check_watching)
    bcs_branch = src.index("if cfg.ENTRY_STRUCTURE == 'bcs':")
    tail = src.index('if cfg.PAPER_MODE:', bcs_branch)
    assert '_funds_line' in src[bcs_branch:tail], \
        'the funds check is not reachable from the BCS entry path'


def test_a_queued_signal_is_starved_even_though_it_is_never_pending(
        store, monkeypatch):
    """THE hole in the first cut of the queue. `is_expired` answers only about
    PENDING markers, so `expire_stale` never visited a QUEUED record — a signal
    whose spawn was refused every cycle ping-ponged forever: attempts never
    rose, drop_after was never read, no drop, no Telegram. The module's own
    guarantee ('a broken CLI announces itself within the hour') was false."""
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.queue_entry_vet(store, 1, 'no agent slot free')
    assert vet_mod.vet_state(store.find(1)) == vet_mod.QUEUED

    # An hour later, still queued, never once PENDING-and-expired.
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(seconds=4000))
    assert vet_mod.vet_state(store.find(1)) == vet_mod.STARVED, \
        'a permanently refused signal was never dropped'


def test_one_timeout_burns_one_attempt_not_two(store, monkeypatch):
    """The increment and the requeue/starve decision were in SEPARATE locks, so
    a second drainer passed the same guard between them and incremented again —
    starving a signal after one real agent run."""
    monkeypatch.setattr(cfg, 'ENTRY_VET_MAX_ATTEMPTS', 2)
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    late = datetime.now() + timedelta(minutes=11)
    # Two overlapping drainers, same expired marker.
    vet_mod.expire_stale(store, now=late)
    vet_mod.expire_stale(store, now=late)
    assert (store.find(1)['vet'] or {}).get('attempts') == 1, \
        'one agent run burned two attempts'
    assert vet_mod.vet_state(store.find(1)) == vet_mod.QUEUED


def test_a_killed_agent_cannot_outlive_its_own_verdict_deadline():
    """CHILD_KILL_SEC was 720 against a 600s deadline, so attempt 1's agent was
    still alive when attempt 2 was PENDING — and `record_verdict` checks only
    'PENDING and not expired', so the stale agent's ALLOW would be applied
    against a book it never saw."""
    assert cfg.CHILD_KILL_SEC < cfg.VET_TIMEOUT_SEC, (
        'a spawned agent outlives its marker: %d kill vs %d deadline'
        % (cfg.CHILD_KILL_SEC, cfg.VET_TIMEOUT_SEC))


def test_a_budget_refusal_does_not_claim_the_cli_is_broken(store, monkeypatch,
                                                           tmp_path):
    """`_note_spawn(False)` feeds `spawn_failures`, which drives 'CLAUDE CLI NOT
    STARTING — the binary is missing'. A 24-position sweep against a cap of 3
    produces 21 refusals: seven times the threshold, blaming a healthy CLI.
    Same never-asked/asked-and-failed conflation, one commit later."""
    import zebra.health as health
    # The counters, directly: a refusal must never touch `spawn_failures`.
    for _ in range(5):
        health.record_spawn_refused('review')
    state = health._read_state()
    assert int(state.get('spawn_failures') or 0) == 0, \
        'refusals inflated the missing-binary counter'
    assert int(health._channels(state)['review'].get('refusals') or 0) == 5
    # ...and the refusal branch must actually use it. Read the FILE, not the
    # attribute: the no-real-agents rail replaces `_spawn_generic` autouse, so
    # `inspect.getsource` would return the stub and assert nothing.
    src = (Path(vet_mod.__file__)).read_text(encoding='utf-8')
    body = src[src.index('if not slot:'):][:700]
    assert 'record_spawn_refused' in body, 'the refusal path lost its counter'
    assert '_note_spawn(False' not in body, \
        'a refusal still feeds the missing-binary counter'


def test_the_order_ticket_escapes_symbols_with_ampersands(monkeypatch):
    """M&M is open in the live book right now; M&MFIN, J&KBANK, ARE&M, GVT&D
    and S&SPOWER are all in the F&O universe. Every alert is parse_mode=HTML
    and the house rule is escape EVERY runtime value."""
    trade = {'id': 1, 'stock': 'M&M', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    bcs = dict(_bcs(), long_symbol='M&M26AUG3450CE',
               short_symbol='M&M26AUG3550CE')
    msg = monitor._format_bcs_enter_alert(trade, _analysis(), bcs)
    assert 'M&amp;M' in msg
    assert 'M&M' not in msg.replace('M&amp;M', ''), \
        'a bare & survived into an HTML message'


# ── remaining review findings, fixed 2026-08-13 ──────────────────────────

def test_a_refused_review_gives_its_daily_slot_back(store, monkeypatch):
    """`attempted_at` is stamped BEFORE the spawn so a CRASHING agent cannot
    re-trigger every cycle. That conflates "crashed" with "never started": with
    review capped at 3, a 24-position sweep refused 21 and locked each of them
    out for 24 hours — and tomorrow the same arithmetic repeats, so most
    positions were never reviewed while the log claimed 24 requests a day."""
    from zebra import review as review_mod
    _signal(store)
    _enter(store, 1)
    monkeypatch.setattr(review_mod.vet_mod, '_spawn_generic',
                        lambda *a, **k: None)          # refused
    assert review_mod.request(store, 1, 'test', context={}) is False
    assert not (store.find(1).get('review') or {}).get('attempted_at'), \
        'a refused review still burned the daily slot'


def test_a_refused_postmortem_batch_gives_the_day_back(monkeypatch, tmp_path):
    from zebra import postmortem as pm
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    pm.mark_run()
    assert pm._marker_path().exists()
    pm.clear_run()
    assert not pm._marker_path().exists(), "the day's slot was not released"


def test_accounting_failure_still_bounds_the_batch_channels(store, monkeypatch,
                                                            tmp_path):
    """The fallback used to hand every caller a token, which switched the cap
    OFF: no slot was created, nothing was counted, and on a PERSISTENT failure
    (a full SD card is persistent) a 24-position sweep would start 24 node
    processes on the box running live money — through the budget's own error
    handler. Degrade by priority instead: decisions pass, batches do not."""
    def boom(*a, **k):
        raise OSError('read-only file system')
    # Patch the LOCK, not _load_budget: a corrupt file is caught by an inner
    # handler on purpose (it must not halt vetting). This is the outer path —
    # we cannot account at all.
    monkeypatch.setattr(vet_mod, 'exclusive', boom)
    assert vet_mod._spawn_budget_ok('entry', channel='entry'), \
        'a trading decision was blocked by a bookkeeping failure'
    assert not vet_mod._spawn_budget_ok('sweep', channel='review'), \
        'a batch channel could fan out unbounded with no accounting'


def test_the_unbudgeted_token_never_touches_the_budget_file(store, tmp_path):
    """'unbudgeted' is truthy, so it sailed past the `if not token` guard, took
    the lock, matched nothing and rewrote the file — while its comment claimed
    the slot would self-release. There is no slot."""
    path = tmp_path / 'zebra_spawn_budget.json'
    path.write_text(json.dumps([{'t': 0.0, 'pid': None, 'id': 'real'}]))
    before = path.read_text()
    vet_mod.claim_slot_pid('unbudgeted', 4242)
    assert path.read_text() == before


def test_the_budget_file_is_written_atomically(store, tmp_path):
    """A bare write_text truncates first; an unclean power-down on a Pi then
    leaves a half file, and the next read resets the cap to zero while agents
    are still alive."""
    import inspect
    src = inspect.getsource(vet_mod._write_budget)
    assert 'os.replace' in src, 'the budget write is not atomic'
    vet_mod._spawn_budget_ok('a')
    assert (tmp_path / 'zebra_spawn_budget.json').exists()
    assert not list(tmp_path.glob('*.tmp')), 'a temp file was left behind'


def test_paper_mode_cannot_be_armed_by_a_typo(monkeypatch):
    """Every numeric threshold is validated; the ONE key that decides whether
    real orders are placed was taken raw. `"paper_mode": 0` silently means
    LIVE, and the only visible difference is that the bot starts trading."""
    for bad in (0, 1, 'false', 'true', '', None, []):
        monkeypatch.setitem(cfg._runtime, 'paper_mode', bad)
        assert cfg._strict_bool('paper_mode') is True, (
            'paper_mode=%r was not rejected' % (bad,))
    monkeypatch.setitem(cfg._runtime, 'paper_mode', False)
    assert cfg._strict_bool('paper_mode') is False, 'a real False was ignored'


def test_the_reserve_can_never_exceed_the_total_cap():
    """ZEBRA_AGENT_RESERVE bypasses the config validator; a negative value
    inverted the reserve (cap 5, reserve -3 -> batch cap 8)."""
    assert 0 <= cfg.AGENT_RESERVE < cfg.MAX_CONCURRENT_AGENTS


def test_a_dropped_entry_is_not_cancelled_until_the_alert_lands(store,
                                                                monkeypatch):
    """Cancel-then-send made silence PERMANENT: the reap's status filter
    excludes cancelled rows, so a Telegram lost to a network blip could never
    be retried — and 'the halt cannot be silent' is the entire justification
    for parking entries instead of taking them."""
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.starve(store, 1, 'gave up')
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: False)
    assert monitor._reap_starved_vets(store) == []
    assert store.find(1)['status'] == 'triggered', \
        'cancelled while the alert never landed — silent forever'
    # ...and the next cycle, once Telegram is back, completes it.
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    assert monitor._reap_starved_vets(store) == [1]
    assert store.find(1)['status'] == 'cancelled' and len(sent) == 1


def test_the_queue_is_visible_on_the_one_dashboard(store, monkeypatch, capsys):
    """A queue nobody can see is a trading pause nobody can see."""
    from zebra import __main__ as cli
    import zebra.trade_store as ts_mod
    _signal(store)
    store.mark_triggered(1, 96.0, 4.0, [])
    vet_mod.request_entry_vet(store, 1, context={}, spawn=False)
    vet_mod.queue_entry_vet(store, 1, 'no agent slot free')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(ts_mod, 'get_store', lambda: store)
    cli.cmd_status(type('A', (), {'json': False})())
    out = capsys.readouterr().out
    assert 'QUEUED' in out and 'NOT entering' in out


def test_a_zero_quantity_never_reports_funds_ok(monkeypatch):
    """need = debit * 0 = 0, and 'available >= 0' is always true — the ticket
    would read 'Funds OK — need Rs 0' for an unfundable order."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    line = monitor._funds_line(_Kite(avail=0, margin=10_000), _BCS, 0)
    assert 'Funds OK' not in line and 'lot size unknown' in line


def test_a_zero_exchange_margin_is_not_mistaken_for_an_outage(monkeypatch):
    """`if total:` read a legitimate 0.0 (fully hedged) as 'API unavailable'
    and silently relabelled the basis."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    line = monitor._funds_line(_Kite(avail=50_000, margin=0.0), _BCS, 5000)
    assert 'exchange margin' in line and 'net debit' not in line


@pytest.mark.skipif(os.name != 'posix', reason='POSIX liveness probe')
def test_pid_liveness_is_real_on_posix():
    """The branch that ACTUALLY runs in production had no test: the suite runs
    on Windows, where `_pid_alive` returns True unconditionally, so a green run
    said nothing about live-process semantics on the Pi."""
    import subprocess
    p = subprocess.Popen(['true'])
    p.wait()
    assert vet_mod._pid_alive(os.getpid()) is True
    assert vet_mod._pid_alive(p.pid) is False


def test_a_future_timestamp_cannot_wedge_a_slot_forever(store, monkeypatch,
                                                        tmp_path):
    """A Pi has no RTC and takes its clock from NTP at boot, so it can step
    BACKWARDS. A negative age made every age test trivially true, so neither
    backstop could ever free the slot."""
    import time as _t
    monkeypatch.setattr(cfg, 'MAX_CONCURRENT_AGENTS', 1)
    monkeypatch.setattr(cfg, 'AGENT_RESERVE', 0)
    (tmp_path / 'zebra_spawn_budget.json').write_text(
        json.dumps([{'t': _t.time() + 99999, 'pid': None, 'id': 'future'}]))
    assert vet_mod._spawn_budget_ok('x'), 'a future timestamp wedged the budget'
