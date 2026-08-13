"""Regressions for the 2026-08-13 four-reviewer pass.

Each test below corresponds to a defect that was live in production when the
engine was deployed on 2026-08-12. They are grouped by the property they
protect rather than by the file they touch, because every one of these bugs was
a property that no single file was responsible for.

Run:  cd Helper && python -m pytest zebra/tests/test_review_2026_08_13.py -v
"""
import json
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


@pytest.mark.parametrize('verb', POSITION_VERBS + CYCLE_VERBS)
def test_the_deny_list_covers_the_callers_not_just_the_callees(verb):
    assert any(verb in pattern for pattern in cfg.VET_DENIED_TOOLS), \
        f"`zebra {verb}` is not denied to spawned agents"


@pytest.mark.parametrize('verb', POSITION_VERBS + CYCLE_VERBS)
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
    allowed = [vet_mod._spawn_budget_ok(f'agent-{i}') for i in range(6)]
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
    assert vet_mod._spawn_budget_ok('x') is True


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
            'debit_to_width_pct': 40.0, 'max_profit_per_share': 4.8,
            'warnings': [], 'long_symbol': 'BCSLONG', 'short_symbol': 'BCSSHORT',
            'long_ask': 5.0, 'short_bid': 1.8}


def test_the_ticket_names_the_bcs_and_never_the_retired_zebra_pair(monkeypatch):
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_enter_alert(trade, _analysis(), _bcs())

    assert 'BCSLONG' in msg and 'BCSSHORT' in msg, 'the tradeable legs are missing'
    assert 'ZEBLONG' not in msg and 'ZEBSHORT' not in msg, \
        'the ticket still quotes the retired zebra legs'
    assert '2×' not in msg and '2x' not in msg, \
        'a 2x leg is the zebra back-ratio — never entered under BCS-only'
    assert 'shadow' not in msg.lower(), \
        'the structure that actually trades must not be captioned a shadow'


def test_a_missing_bcs_pair_says_so_instead_of_falling_back_to_zebra(monkeypatch):
    """No spread, no ticket. Silently printing the zebra pair here would be the
    same bug wearing a fallback."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_enter_alert(trade, _analysis(), None)
    assert 'NO SPREAD' in msg
    assert 'ZEBLONG' not in msg


def test_the_zebra_ticket_survives_for_the_zebra_pipeline(monkeypatch):
    """The BCS-only branch must not delete the other pipeline's ticket — the
    config knob still selects it, and 15 open positions are that structure."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'zebra')
    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE', 'st_value': 100.0,
             'st_direction': 'DOWN', 'timeframe': 'weekly'}
    msg = monitor._format_enter_alert(trade, _analysis(), _bcs())
    assert 'ZEBLONG' in msg and 'BCS shadow' in msg
