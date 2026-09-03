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


def test_debit_sl_needs_a_vet_when_spot_does_not_corroborate(store, monkeypatch):
    """The value trigger is priced off the option book — the one that can be
    faked. A spot trigger is corroborated by real trades.

    RENAMED 2026-09-03: this was `test_debit_sl_always_needs_a_vet`, and
    "always" stopped being true when the spot-corroborated fast path landed.
    The assertion still holds for the reason it always did — no `spot` is
    passed, so there is nothing to clear it — but the NAME was making a claim
    the code no longer honours, and a stale name becomes a stale belief."""
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
    intrinsic floor are unchanged and still stand on their own.

    Twelve minutes: past the 10-minute deadline, inside the window in which the
    timeout still describes THIS episode. It used to jump two HOURS, which
    quietly made this a test of the fossil path below instead of a test of an
    outage — and asserted that the fossil should fire the exit.
    """
    _gate(store)
    _clock(monkeypatch, datetime.now() + timedelta(minutes=12))
    assert _gate(store) == 'proceed'
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.UNAVAILABLE


def test_an_ancient_pending_is_rerequested_not_cashed_in_as_a_timeout(
        store, monkeypatch):
    """The V1 bypass: `exit_gate` runs ONLY when a trigger fires, and nothing
    sweeps `exit_vet`. A request whose agent died, on a trigger that then
    stopped firing, sat on disk — and days later the timeout branch flipped it
    to UNAVAILABLE and returned 'proceed'. The exit fired on a timeout from last
    week, against a book no agent had ever seen, logging the same line a real
    ten-minute outage logs. One silent bypass per (trade, kind), on exactly the
    shape this channel exists to catch.
    """
    _gate(store)
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.PENDING
    _clock(monkeypatch, datetime.now() + timedelta(days=3))
    # Discarded and re-requested against the CURRENT book, not cashed in.
    assert _gate(store) == 'wait'
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.PENDING
    assert vet.exit_defers(store.find(1), 'debit_sl') == 0


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
    _clock(monkeypatch, datetime.now() + timedelta(minutes=12))
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
    # Returns a PID, as the real `_spawn_cli` does. `list.append`
    # returns None, which production reads as SPAWN FAILED -- and
    # since 2026-08-31 a failed exit spawn flips the marker to
    # UNAVAILABLE rather than waiting out the deadline, so a double
    # that fakes a failure no longer tests what this name says.
    def _spawned(tid, exit_kind=None):
        spawns.append(exit_kind)
        return 4242
    monkeypatch.setattr(vet, '_spawn_cli', _spawned)
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
    # A PID: this fixture is about a HELD exit on an unreliable book,
    # not about a spawn that failed. Returning None would now short-
    # circuit the wait to UNAVAILABLE and the gate would proceed.
    monkeypatch.setattr(vet, '_spawn_cli', lambda tid, exit_kind=None: 4242)
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


# ── a usage-limit block: fail to the guards NOW, not in fifteen minutes ──
# ASHOKLEY #390, 2026-08-14. debit_sl fired at 13:25 with the structure at 1.01
# against a 1.00 stop. The vet had died on quota two seconds after spawning;
# the gate waited three cycles for it and the exit booked at 0.68 — roughly
# -50% turned into -75%. The verdict never changes, only how long it costs.

def _block(logdir, monkeypatch, when=MIDDAY):
    """Plant the real refusal transcript and make the CLI read as blocked.

    Both the transcript's mtime and its reset time sit on the SAME pinned clock
    the gate reads. Leaving either at real wall-clock time makes the reset land
    hours in the past, which `_sane_reset` then rightly refuses — the fixture
    would register no block at all and the test would pass on nothing.
    """
    import os
    monkeypatch.setattr(cfg, 'LOG_DIR', logdir)
    reset = when + timedelta(minutes=45)
    p = logdir / 'vet_cli_20260814_132531_exit-vet-390-debit-sl.log'
    p.write_text(
        '=' * 78 + '\n=== 2026-08-14 13:25:31  vet #390 debit_sl  model=opus  '
        'channel=exit\n' + '=' * 78 + '\n'
        "You've hit your session limit · resets %d:%02d%s (Asia/Kolkata)\n"
        % (reset.hour % 12 or 12, reset.minute,
           'am' if reset.hour < 12 else 'pm'),
        encoding='utf-8')
    os.utime(p, (when.timestamp(), when.timestamp()))
    vet.refresh_cli_block(when)
    assert vet.cli_blocked_until(when) is not None, \
        "fixture did not register a block"


def test_a_blocked_exit_vet_is_never_requested(store, tmp_path, monkeypatch):
    """Raising a request nobody can answer only buys a 10-minute wait before
    the identical fail-open."""
    _clock(monkeypatch, MIDDAY)
    _block(tmp_path, monkeypatch)
    assert _gate(store) == 'proceed'
    assert vet.exit_state(store.find(1), 'debit_sl') is None, \
        "a PENDING marker was left behind for an agent that cannot run"


def test_a_pending_exit_vet_stops_waiting_once_the_cli_is_known_blocked(
        store, tmp_path, monkeypatch):
    """The three cycles ASHOKLEY spent waiting."""
    _clock(monkeypatch, MIDDAY)
    assert _gate(store) == 'wait'                     # request goes out
    _block(tmp_path, monkeypatch)
    assert _gate(store) == 'proceed', "still waiting out a dead agent"
    assert vet.exit_state(store.find(1), 'debit_sl') == vet.UNAVAILABLE


def test_a_block_does_not_convert_an_explicit_defer_into_consent(
        store, tmp_path, monkeypatch):
    """Claude LOOKED and said it could not verify the quote. "The agent is
    offline" is no more consent than a timeout is — the escalation must run its
    normal course to the human, not fall through to the trigger."""
    _clock(monkeypatch, MIDDAY)
    assert _gate(store) == 'wait'
    vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)
    assert _gate(store) == 'wait'                     # re-asked
    _block(tmp_path, monkeypatch)
    assert _gate(store) != 'proceed', \
        "a usage limit was read as permission to fire a refused exit"


# ── the spot-corroborated fast path (2026-09-03) ─────────────────────────
#
# WHAT IT IS FOR. The exit vet exists for ONE shape: the NHPC signature, value
# collapsing while spot stands still. A stop whose loss the underlying has
# already walked 3% to confirm is the opposite shape, and making it wait for an
# agent is pure cost. On COALINDIA #440 that cost was real: a 900s hold that
# produced no verdict at all (the CLI grant was broken) while the position
# drifted 2.90 -> 2.40, Rs 540 of a Rs 4,703 loss.
#
# It may only ever REMOVE the 'value-based' reason. Every test below that
# asserts `needed is True` is guarding that boundary, and matters more than the
# one that asserts it works.

def _sl_spot(store, value, direction='CE'):
    t = store.find(1)
    t['sl_spot'] = value
    t['direction'] = direction
    return t


def test_spot_confirming_the_loss_skips_the_vet_on_a_clean_midday_book(store, monkeypatch):
    """CE hurt by spot FALLING. 93.0 is through the 93.5 stop, so the option
    book is not the only witness and there is nothing for an agent to add."""
    _clock(monkeypatch, MIDDAY)
    t = _sl_spot(store, 93.5, 'CE')
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=93.0)
    assert needed is False, why


def test_spot_short_of_the_level_still_needs_the_vet(store, monkeypatch):
    """One paisa the right side of the level is not corroboration."""
    _clock(monkeypatch, MIDDAY)
    t = _sl_spot(store, 93.5, 'CE')
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=93.6)
    assert needed and 'value-based' in why


def test_the_direction_is_not_inverted_for_a_put_spread(store, monkeypatch):
    """THE EASY BUG. A PE position is hurt by spot RISING, so its `sl_spot`
    sits ABOVE entry. Getting this backwards would skip the vet on exactly the
    positions where spot is moving favourably — i.e. where a collapsing value
    IS the NHPC signature and most deserves a look."""
    _clock(monkeypatch, MIDDAY)
    t = _sl_spot(store, 103.0, 'PE')
    needed, _ = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=104.0)
    assert needed is False                      # spot rose through it: confirmed
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=102.0)
    assert needed and 'value-based' in why      # spot below it: NOT confirmed


def test_the_fast_path_never_suppresses_an_unreliable_book(store, monkeypatch):
    """Spot agreeing with a lying quote is not evidence the quote is honest.
    The garbage-book reason must survive corroboration."""
    _clock(monkeypatch, MIDDAY)
    t = _sl_spot(store, 93.5, 'CE')
    needed, why = vet.needs_exit_vet(t, 'debit_sl', BAD, spot=90.0)
    assert needed and 'unreliable book' in why


def test_the_fast_path_never_suppresses_the_opening_fifteen_minutes(store, monkeypatch):
    """Both money-losing incidents were opening prints."""
    _clock(monkeypatch, OPEN_)
    t = _sl_spot(store, 93.5, 'CE')
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=90.0)
    assert needed and 'first 15 minutes' in why


def test_the_fast_path_never_suppresses_blind_cycles(store, monkeypatch):
    _clock(monkeypatch, MIDDAY)
    store.bump_blind(1)
    t = _sl_spot(store, 93.5, 'CE')
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=90.0)
    assert needed and 'blind' in why


@pytest.mark.parametrize('sl,spot,direction', [
    (None, 90.0, 'CE'),       # no stored level
    (0, 90.0, 'CE'),          # falsy level
    (93.5, None, 'CE'),       # no spot this cycle
    ('bad', 90.0, 'CE'),      # unparseable level
    # DIRECTION belongs in this list too. The name said "unknown
    # corroboration" but only sl and spot were covered, while an unknown
    # direction silently took the PE comparison — the one case that failed
    # OPEN rather than closed.
    (93.5, 90.0, None),
    (93.5, 90.0, ''),
    (93.5, 90.0, 'ce'),
])
def test_unknown_corroboration_fails_towards_vetting(store, monkeypatch, sl,
                                                     spot, direction):
    """It may only ever remove a reason to look, so the answer when we cannot
    tell must be 'not corroborated'."""
    _clock(monkeypatch, MIDDAY)
    t = _sl_spot(store, sl, direction)
    needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=spot)
    assert needed and 'value-based' in why


def test_the_gate_actually_threads_spot_into_the_prefilter(store, monkeypatch):
    """WIRING, not logic. `exit_gate` takes spot as its own argument; if it is
    not passed down, the fast path is dead code that every unit test above
    still passes."""
    _clock(monkeypatch, MIDDAY)
    _sl_spot(store, 93.5, 'CE')
    # spot 93.0 is through the level -> corroborated -> no agent, no wait
    assert vet.exit_gate(store, store.find(1), 'debit_sl', GOOD, 93.0,
                         spawn=False) == 'proceed'
    assert store.find(1).get('exit_vet') in (None, {}), (
        'a corroborated stop opened a vet episode anyway')


def test_a_corroborated_spot_does_not_convert_an_explicit_defer_into_consent(
        store, monkeypatch):
    """THE H1 REGRESSION (found in review, 2026-09-03, before deploy).

    Every marker state EXCEPT a fresh under-cap DEFER returns before the
    `needs_exit_vet` call, so that one state falls through to the pre-filter.
    Offering it the spot-corroboration shortcut discards the refusal it is
    carrying: Claude LOOKED at 12:00 and said it could not verify this book;
    at 12:05 the trigger re-fires with spot through `sl_spot`, and the exit
    books at the very mid the agent declined to certify. The defer count is
    dropped, so the at-cap escalation to a human never arrives.

    Spot agreeing about the LOSS is not evidence about the PRICE, and the price
    is what was refused. Same rule as the usage-limit branch and as
    `test_a_block_does_not_convert_an_explicit_defer_into_consent`, one input
    over.
    """
    _clock(monkeypatch, MIDDAY)
    _sl_spot(store, 93.5, 'CE')
    # Cycle 1: the value stop fires while spot is still ABOVE its level (94.0),
    # so there is no corroboration and a vet is properly requested.
    assert vet.exit_gate(store, store.find(1), 'debit_sl', GOOD, 94.0,
                         spawn=False) == 'wait'
    vet.record_exit_verdict(store, 1, 'debit_sl', 'defer', decision_id=1)
    # Cycle 2: spot has since crossed 93.5. The loss is now corroborated — but
    # the agent's refusal was about the PRICE, and that has not been answered.
    _sl_spot(store, 93.5, 'CE')
    assert vet.exit_gate(store, store.find(1), 'debit_sl', GOOD, 90.0,
                         spawn=False) != 'proceed', (
        'a corroborated spot fired an exit an agent had explicitly refused to '
        'verify — the defer was silently discarded')


def test_a_malformed_direction_never_reads_as_corroborated(store, monkeypatch):
    """M1. The comparison used to be `... if direction == 'CE' else spot >= sl`,
    so anything not exactly 'CE' took the PE branch. A CE-shaped record with a
    FAVOURABLE spot then read as corroborated — skipping the vet on exactly the
    value-collapse-while-spot-is-fine shape it exists for."""
    _clock(monkeypatch, MIDDAY)
    for bad in (None, '', 'ce', 'CALL', 0):
        t = _sl_spot(store, 93.5, bad)
        assert vet._spot_confirms_loss(t, 96.0) is False, (
            'direction %r read as corroborated on a favourable spot' % bad)
        needed, why = vet.needs_exit_vet(t, 'debit_sl', GOOD, spot=96.0)
        assert needed and 'value-based' in why
