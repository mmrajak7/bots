"""Exit vetting — the dangerous direction.

Both real-money losses in this fleet were automated EXITS on bad data at market
open. Every structure here is hedged (max loss = debit, known at entry), so
HOLDING is bounded and EXITING BADLY is not. The tests below encode that
asymmetry, and above all the one property that must never break:

    a gated exit must never lose its ability to fire.

`set_alert_flag` is consume-once. If the gate ran after it, a deferred exit
would burn the flag and the position could never be closed by that trigger
again — a capped loss quietly becoming a maximum loss.

Run:  cd Helper && python -m pytest zebra/tests/test_exit_vet.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import monitor                # noqa: E402
from zebra import vet                    # noqa: E402
from zebra.trade_store import ZebraStore  # noqa: E402

GOOD = {'mid': 5.0, 'reliable': True, 'reason': ''}
BAD = {'mid': 0.18, 'reliable': False, 'reason': 'wide_book width 3.2 vs mid 0.18'}
MIDDAY = datetime(2026, 8, 11, 12, 0)
OPEN_ = datetime(2026, 8, 11, 9, 20)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {
        'long_strike': 96.0, 'short_strike': 100.0,
        'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
        'debit': 10.0, 'lot_size': 100, 'lots': 1, 'expiry': '2026-09-24',
    })
    return s


def _gate(store, kind='debit_sl', quote=BAD, spawn=False):
    return vet.exit_gate(store, store.find(1), kind, quote, 96.0, spawn=spawn)


def _clock(monkeypatch, when):
    """Freeze BOTH clocks.

    vet keeps them separate on purpose — `_now` is the naive clock used for
    durations, `_now_ist` is the exchange wall clock used for "where are we in
    the session". A test that pins only the first would let the real IST time
    decide whether the opening-15-minutes rule fires, i.e. pass or fail
    depending on when it runs.
    """
    monkeypatch.setattr(vet, '_now', lambda: when)
    monkeypatch.setattr(vet, '_now_ist', lambda: when)


# ── pre-filter: don't burn tokens on uninteresting exits ─────────────────
def test_clean_quote_midday_needs_no_vet(store, monkeypatch):
    _clock(monkeypatch, MIDDAY)
    needed, _ = vet.needs_exit_vet(store.find(1), 'tp', GOOD)
    assert needed is False
    assert _gate(store, 'tp', GOOD) == 'proceed'


def test_unreliable_book_needs_a_vet(store, monkeypatch):
    _clock(monkeypatch, MIDDAY)
    needed, why = vet.needs_exit_vet(store.find(1), 'tp', BAD)
    assert needed and 'unreliable book' in why


def test_market_open_needs_a_vet_even_on_a_clean_book(store, monkeypatch):
    """Both real-money incidents happened in the opening minutes."""
    _clock(monkeypatch, OPEN_)
    needed, why = vet.needs_exit_vet(store.find(1), 'tp', GOOD)
    assert needed and 'first 15 minutes' in why


def test_debit_sl_always_needs_a_vet(store, monkeypatch):
    """The value trigger is priced off the option book — the one that can be
    faked. A spot trigger is corroborated by real trades."""
    _clock(monkeypatch, MIDDAY)
    needed, why = vet.needs_exit_vet(store.find(1), 'debit_sl', GOOD)
    assert needed and 'value-based' in why


def test_blind_cycles_need_a_vet(store, monkeypatch):
    _clock(monkeypatch, MIDDAY)
    store.bump_blind(1)
    needed, why = vet.needs_exit_vet(store.find(1), 'tp', GOOD)
    assert needed and 'blind' in why


# ── the gate ─────────────────────────────────────────────────────────────
def test_layer_off_always_proceeds(store, monkeypatch):
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    assert _gate(store) == 'proceed'


def test_first_flagged_exit_waits_and_requests(store):
    assert _gate(store) == 'wait'
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.PENDING


def test_pending_keeps_waiting(store):
    _gate(store)
    assert _gate(store) == 'wait'


def test_allow_lets_the_exit_fire(store):
    _gate(store)
    vet.record_exit_verdict(store, 1, 'debit_sl', 'allow', decision_id=1)
    assert _gate(store) == 'proceed'


def test_defer_reasks_then_escalates_at_the_cap(store):
    """Two defers, then HOLD — the human decides. It deliberately does NOT
    fall through to the deterministic trigger: the loss is already capped, and
    firing on a book we could not verify is the unbounded direction."""
    for i in range(cfg.EXIT_MAX_DEFERS):
        assert _gate(store) == 'wait'
        vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=i)
    assert vet.exit_defers(store.find(1), 'debit_sl') == cfg.EXIT_MAX_DEFERS
    assert _gate(store) == 'hold'


def test_timeout_proceeds_on_the_deterministic_guards(store, monkeypatch):
    """Claude down must NOT stop exits — the debounce, reliability check and
    intrinsic floor are unchanged and still stand on their own."""
    _gate(store)
    _clock(monkeypatch, datetime.now() + timedelta(hours=2))
    assert _gate(store) == 'proceed'
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.UNAVAILABLE


def test_request_failure_proceeds(store, monkeypatch):
    """A broken vet layer must never block an exit."""
    monkeypatch.setattr(vet, '_request_exit_vet',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    assert _gate(store) == 'proceed'


def test_corrupt_marker_does_not_raise(store):
    with store._mutate():
        store.find(1)['exit_vet'] = 'garbage-not-a-dict'
    assert _gate(store, 'tp', GOOD) in ('proceed', 'wait')


def test_kinds_are_independent(store):
    """A deferred debit_sl must not block a legitimate TP."""
    _gate(store, 'debit_sl', BAD)
    assert vet.exit_state(store.find(1), 'tp') is None


# ── THE invariant: a gated exit never loses its ability to fire ──────────
def test_gate_runs_before_the_consume_once_flag(store, monkeypatch):
    """If the gate ran after set_alert_flag, a deferred exit would burn the
    one-shot flag and could never fire again."""
    _clock(monkeypatch, MIDDAY)
    assert monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD, 96.0,
                                 dry_run=True) is False
    # flag untouched -> the exit can still fire once vetted
    assert store.find(1).get('debit_sl_alerted_at') is None
    vet.record_exit_verdict(store, 1, 'debit_sl', 'allow', decision_id=1)
    assert monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD, 96.0,
                                 dry_run=True) is True
    assert store.set_alert_flag(1, 'debit_sl') is True      # still available


def test_escalation_telegram_fires_once(store, monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    for i in range(cfg.EXIT_MAX_DEFERS):
        monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD, 96.0)
        vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=i)
    for _ in range(3):
        assert monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD,
                                     96.0) is False
    assert len(sent) == 1, "escalation repeated"
    assert 'EXIT NEEDS YOU' in sent[0] and 'TESTCO' in sent[0]
    assert 'HOLDING' in sent[0]


def test_escalation_escapes_html(store, monkeypatch):
    """The quote reason contains '<' ('wide_book width 3.2 vs mid') and other
    free text; a bare '<' would 400 the whole message and the escalation --
    the one asking a human to intervene -- would vanish."""
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    q = dict(BAD, reason='depth < 100 & spread > 5%')
    for i in range(cfg.EXIT_MAX_DEFERS):
        monitor._exit_cleared(store, store.find(1), 'debit_sl', q, 96.0)
        vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=i)
    monitor._exit_cleared(store, store.find(1), 'debit_sl', q, 96.0)
    assert '&lt;' in sent[0] and '&amp;' in sent[0]


# ── verdict handling ─────────────────────────────────────────────────────
def test_rejects_a_veto_verdict(store):
    """There is deliberately no hard veto for exits: it would let the model
    disarm a stop-loss outright."""
    _gate(store)
    with pytest.raises(ValueError, match='allow'):
        vet.record_exit_verdict(store, 1, 'debit_sl', 'veto')


def test_late_verdict_is_discarded(store, monkeypatch):
    _gate(store)
    _clock(monkeypatch, datetime.now() + timedelta(hours=2))
    _gate(store)                                     # flips to unavailable
    assert 'discarded' in vet.record_exit_verdict(store, 1, 'debit_sl', 'allow')


def test_verdict_on_a_closed_trade_is_discarded(store):
    """The agent was still thinking when the position closed."""
    _gate(store)
    store.mark_exited(1, 100.0, 6.0, 'tp')
    assert 'discarded' in vet.record_exit_verdict(store, 1, 'debit_sl', 'allow')


# ── a verdict authorises an EPISODE, not the trade forever ───────────────
# Without a TTL, one `allow` on a clean book waves through every future exit on
# that trade — including one priced off a book that has since rotted. That is
# the NHPC shape the whole layer exists to catch, so it gets its own tests.
def test_a_stale_allow_does_not_authorise_a_later_exit(store, monkeypatch):
    _clock(monkeypatch, MIDDAY)
    _gate(store)
    vet.record_exit_verdict(store, 1, 'debit_sl', 'allow', decision_id=1)
    assert _gate(store) == 'proceed'                 # same episode: fine
    _clock(monkeypatch, MIDDAY + timedelta(seconds=cfg.EXIT_VET_TTL_SEC + 1))
    assert _gate(store) == 'wait'                    # days later: re-vetted
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.PENDING


def test_a_stale_outage_does_not_disarm_the_gate_forever(store, monkeypatch):
    """One ten-minute Claude outage must not turn this (trade, kind) into
    unvetted-forever while the switch still reads ON."""
    _clock(monkeypatch, MIDDAY)
    _gate(store)
    _clock(monkeypatch, MIDDAY + timedelta(seconds=cfg.VET_TIMEOUT_SEC + 1))
    assert _gate(store) == 'proceed'
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.UNAVAILABLE
    _clock(monkeypatch, MIDDAY + timedelta(days=4))
    assert _gate(store) == 'wait'


def test_a_stale_hold_is_re_judged_not_frozen(store, monkeypatch):
    """A cap reached days ago must not silently hold a fresh, healthy exit."""
    # Same KIND throughout — markers are per-kind, so re-checking a different
    # trigger would prove nothing about staleness.
    _clock(monkeypatch, MIDDAY)
    for i in range(cfg.EXIT_MAX_DEFERS):
        assert _gate(store, 'tp', BAD) == 'wait'
        vet.record_exit_verdict(store, 1, 'tp', 'defer', decision_id=i)
    assert _gate(store, 'tp', BAD) == 'hold'
    _clock(monkeypatch, MIDDAY + timedelta(days=2))
    assert _gate(store, 'tp', GOOD) == 'proceed'     # clean book, fresh look


def test_a_timeout_after_a_defer_does_not_become_consent(store, monkeypatch):
    """Fail-open is the contract for 'Claude never looked'. Here Claude looked
    and said it could not verify the quote; a later timeout is still 'we don't
    know', which must not resolve to 'fire it'."""
    _clock(monkeypatch, MIDDAY)
    _gate(store)
    vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)
    assert _gate(store) == 'wait'                    # re-requested
    _clock(monkeypatch, MIDDAY + timedelta(seconds=cfg.VET_TIMEOUT_SEC + 1))
    assert _gate(store) == 'hold'
    assert vet.exit_defers(store.find(1), 'debit_sl') == 2


# ── two agents, one request ──────────────────────────────────────────────
def test_two_agents_cannot_both_land_a_verdict(store):
    """The state check must be INSIDE the lock. Outside it, a `defer` and an
    `allow` race and the loser is silently the one saying 'I cannot verify
    this' — which is the whole point of the gate."""
    _gate(store)
    other = ZebraStore(config={})
    other._load_local()                              # cache still shows PENDING
    assert vet.record_exit_verdict(store, 1, 'debit_sl', 'defer',
                                   decision_id=1) == 'applied'
    assert 'discarded' in vet.record_exit_verdict(other, 1, 'debit_sl', 'allow',
                                                  decision_id=2)
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.DEFER
    assert vet.exit_defers(store.find(1), 'debit_sl') == 1


def test_overlapping_cycles_spawn_one_cli(store, monkeypatch):
    """`zebra loop` and a manual `zebra run` are not serialised against each
    other — flock covers the STORE, not the cycle. Two CLIs answering one
    request would drive the defer counter to the cap in a single re-check."""
    spawns = []
    monkeypatch.setattr(vet, '_spawn_cli',
                        lambda tid, exit_kind=None: spawns.append(exit_kind))
    other = ZebraStore(config={})
    other._load_local()
    assert vet.exit_gate(store, store.find(1), 'debit_sl', BAD, 96.0,
                         spawn=True) == 'wait'
    assert vet.exit_gate(other, other.find(1), 'debit_sl', BAD, 96.0,
                         spawn=True) == 'wait'
    assert len(spawns) == 1


def test_a_failed_escalation_is_retried_not_lost(store, monkeypatch):
    """The flag is claimed BEFORE the send so two processes can't both nag.
    But if the send then fails, the day's only human-in-the-loop message would
    vanish — on a position we are holding precisely because nothing automated
    can verify it."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: False)
    for i in range(cfg.EXIT_MAX_DEFERS):
        monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD, 96.0)
        vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=i)
    assert monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD,
                                 96.0) is False
    assert store.find(1).get('exit_escalate_debit_sl_alerted_at') is None
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monitor._exit_cleared(store, store.find(1), 'debit_sl', BAD, 96.0)
    assert len(sent) == 1 and 'EXIT NEEDS YOU' in sent[0]


# ── END-TO-END through check_entered ─────────────────────────────────────
# Every test above calls the gate directly. Twice in this fleet a subsystem
# passed its unit tests while executing zero times in the live path, so the
# properties that matter get asserted through the function the cron actually
# calls.
@pytest.fixture
def wired(store, monkeypatch):
    """A trade whose TP is hit, on a book too unreliable to trade."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 101.0})
    monkeypatch.setattr(monitor, '_structure_quote', lambda *a, **k: dict(BAD))
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    monkeypatch.setattr(vet, '_spawn_cli', lambda tid, exit_kind=None: None)
    return store


def test_check_entered_gates_the_tp_and_keeps_the_flag_unburnt(wired):
    """The live path must consult the gate, and a held TP must leave its
    consume-once flag available so the exit can still fire once vetted."""
    monitor.check_entered(wired, kite=None, dry_run=True)
    t = wired.find(1)
    assert t['status'] == 'entered', "TP fired on an unverifiable book"
    assert t.get('tp_alerted_at') is None, "consume-once flag burnt on a held exit"
    assert vet.exit_state(t, 'tp') == vet.PENDING, "gate not wired into check_entered"


def test_a_held_tp_does_not_suppress_the_time_exit(wired, monkeypatch):
    """A blocked trigger must skip only its OWN branch. When it `continue`d the
    whole trade, a TP pinned on an untradeable book silenced the T-3 expiry nag
    and the position rode into settlement week unnoticed."""
    with wired._mutate():
        wired.find(1)['expiry'] = (datetime.now() + timedelta(days=1)
                                   ).strftime('%Y-%m-%d')
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monitor.check_entered(wired, kite=None, dry_run=True)
    assert wired.find(1).get('time_alerted_at'), "TIME exit lost behind a held TP"
