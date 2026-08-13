"""END-TO-END wiring of the vet gate through check_watching.

test_vet.py proves the state machine; these prove the MONITOR honors it. The
distinction is the whole game: the original gate passed every unit test while
an ALLOWED signal could never enter — the early `status == 'triggered'`
continue sat above the gate, so the verdict tick was unreachable. Twice before
in this fleet a subsystem looked deployed and executed zero times; these tests
exist so the vet layer cannot be the third.

Run:  cd Helper && python -m pytest zebra/tests/test_vet_gate.py -v
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

SIGNAL = {
    'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
    'st_value': 100.0, 'st_direction': 'UP',
    'signal_price': 96.0, 'signal_gap_pct': 4.0,
}
# spot 96.5 vs ST 100 -> gap 3.5%: inside the 3-4% trigger zone every cycle.
SPOT = 96.5
BEST = {'k_l': 90.0, 'k_s': 100.0, 'debit': 5.0, 'lot_size': 100,
        'long_symbol': 'TESTCO26SEP90CE', 'short_symbol': 'TESTCO26SEP100CE',
        'short_extrinsic': 1.0, 'short_mid': 2.0, 'short_bid': 1.9,
        'short_ask': 2.1, 'short_oi': 9000, 'long_oi': 9000,
        'be': 95.0, 'be_pct_from_spot': -1.5, 'capital_per_lot': 500,
        'gate_fails': []}
ANALYSIS = {'spot': SPOT, 'expiry': '2026-09-30', 'dte': 30, 'lot_size': 100,
            'best': BEST, 'candidates': [],
            'atm_strike': 100.0,
            'atm_quote': {'bid': 1.9, 'ask': 2.1, 'mid': 2.0, 'oi': 9000}}
# The pipeline entered by default is BCS-only, so the gate is exercised
# against the structure that actually opens. These tests are about the VET
# GATE, not the structure — pinning them to the retired zebra path would leave
# the live entry path ungated by any of them.
BCS = {'long_strike': 100.0, 'short_strike': 140.0, 'width': 40.0,
       'long_symbol': 'TESTCO26SEP100CE', 'short_symbol': 'TESTCO26SEP140CE',
       'debit': 10.0, 'lot_size': 100, 'debit_to_width_pct': 25.0,
       'short_extrinsic': 1.0, 'max_profit_per_share': 30.0, 'warnings': [],
       # Books, not just mids: the ticket quotes ASK to buy and BID to sell,
       # and `debit` is the fill (12.1 - 1.9 = 10.2 here, mid-mid would be 10).
       'long_mid': 12.0, 'long_bid': 11.9, 'long_ask': 12.1,
       'short_mid': 2.0, 'short_bid': 1.9, 'short_ask': 2.1,
       'debit_mid': 10.0, 'entry_cost': 0.2, 'entry_cost_pct': 0.7,
       'debit_to_width_pct_mid': 25.0, 'pricing_basis': 'fill'}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'BCS_PAPER_ENABLED', False)

    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': SPOT})
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: dict(ANALYSIS))
    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs',
                        lambda *a, **k: dict(BCS))
    spawned = []
    # Must return a truthy pid: a None return now MEANS "the spawn was refused"
    # (budget full or CLI missing) and stamps the signal failed-open. `append`
    # returns None, so the old stub silently claimed every spawn was refused.
    monkeypatch.setattr(vet, '_spawn_cli',
                        lambda tid: (spawned.append(tid), 4242)[1])

    store = ZebraStore(config={})
    store._load_local()
    store.add_signal(dict(SIGNAL))
    return store, spawned


def cycle(store):
    monitor.check_watching(store, kite=None, dry_run=True)
    return store.find(1)


# ── the bug this file exists for ─────────────────────────────────────────
def test_allowed_signal_actually_enters_on_a_later_tick(wired):
    """THE wiring test. Trigger tick parks it pending; the verdict lands
    between crons; the next tick MUST enter it. The original gate never could:
    allow behaved exactly like veto — a silent per-signal trading halt."""
    store, spawned = wired
    t = cycle(store)                                     # trigger tick
    assert t['status'] == 'triggered' and vet.is_pending(t)
    assert spawned == [1]                                # CLI fired once

    vet.record_verdict(store, 1, vet.ALLOWED, decision_id=1)
    t = cycle(store)                                     # verdict tick
    assert t['status'] == 'entered', "ALLOWED verdict was never honored"
    assert t['vet']['state'] == vet.ALLOWED


def test_pending_signal_waits_and_does_not_reenter_or_respawn(wired):
    store, spawned = wired
    cycle(store)
    for _ in range(3):
        t = cycle(store)
        assert t['status'] == 'triggered' and vet.is_pending(t)
    assert spawned == [1], "a waiting signal respawned the CLI"


def test_vetoed_signal_never_enters(wired):
    store, _ = wired
    cycle(store)
    vet.record_verdict(store, 1, vet.VETOED)
    for _ in range(3):
        t = cycle(store)
        assert t['status'] == 'triggered', "a VETOED signal entered"


def test_a_timeout_queues_and_the_signal_does_NOT_enter(wired):
    """Inverted 2026-08-13: an entry with no verdict WAITS. It must not be a
    label only — the position must genuinely not open, or the queue is
    decorative and the capital it exists to protect is already committed."""
    store, _ = wired
    cycle(store)
    assert vet.expire_stale(store, now=datetime.now() + timedelta(hours=2)) == [1]
    t = cycle(store)
    assert t['status'] != 'entered', 'a queued signal entered anyway'
    # ...and the retry re-requested it against a freshly quoted book.
    assert t['vet']['state'] in (vet.QUEUED, vet.PENDING)


def test_layer_off_is_the_old_single_tick_behaviour(wired, monkeypatch):
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    store, spawned = wired
    t = cycle(store)
    assert t['status'] == 'entered' and t.get('vet') is None
    assert spawned == []                                 # no CLI, no marker


def test_request_infra_failure_enters_unvetted_this_cycle(wired, monkeypatch):
    """A broken vet layer degrades to today's behaviour immediately — it must
    never turn a tradeable signal into a parked one."""
    store, _ = wired
    monkeypatch.setattr(monitor.vet_mod, 'request_entry_vet',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('io')))
    t = cycle(store)
    assert t['status'] == 'entered', "an infra failure blocked the entry"


def test_request_already_vetted_race_does_not_enter_blind(wired, monkeypatch):
    """If the locked re-check finds the vet already settled (stale cache), the
    gate must NOT fail open into an entry — the settled state could be a veto.
    It defers one cycle and then honors the real state."""
    store, _ = wired
    monkeypatch.setattr(
        monitor.vet_mod, 'request_entry_vet',
        lambda *a, **k: (_ for _ in ()).throw(ValueError('#1 already vetted (vetoed)')))
    t = cycle(store)
    assert t['status'] == 'triggered', "entered past a possibly-vetoed state"


def test_corrupt_marker_does_not_kill_the_watching_loop(wired):
    """One hand-mangled record must not halt entries for the whole book. A
    corrupt marker reads as 'never requested' and gets overwritten."""
    store, _ = wired
    cycle(store)
    with store._mutate():
        store.find(1)['vet'] = "corrupt-not-a-dict"
    t = cycle(store)                     # must not raise, must self-heal
    assert vet.is_pending(t), "corrupt marker was not replaced by a fresh vet"


def test_concurrent_request_does_not_reset_deadline_or_respawn(wired):
    """Two overlapping crons both requesting: the second must keep the first's
    marker (deadline unmoved) and must not spawn a second CLI."""
    store, spawned = wired
    cycle(store)
    first_deadline = store.find(1)['vet']['deadline']
    vet.request_entry_vet(store, 1, {'stock': 'TESTCO'}, spawn=True)
    assert store.find(1)['vet']['deadline'] == first_deadline
    assert spawned == [1]


def test_live_mode_sends_the_order_ticket_exactly_once(wired, monkeypatch):
    """LIVE: entry is manual, so the signal stays 'triggered' after the alert.
    The deferred ENTER alert must fire once on the verdict tick and never
    again — a repeating order ticket is how duplicate manual entries happen."""
    store, _ = wired
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    sent = []
    # Stub BOTH formatters: which one runs depends on entry_structure, and a
    # test that stubs only the retired one silently stops testing the live path.
    monkeypatch.setattr(monitor, '_format_enter_alert',
                        lambda *a, **k: 'TICKET')
    monkeypatch.setattr(monitor, '_format_bcs_enter_alert',
                        lambda *a, **k: 'TICKET')
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    cycle(store)                                         # trigger tick: no alert
    assert sent == []
    vet.record_verdict(store, 1, vet.ALLOWED)
    cycle(store)                                         # verdict tick: alert
    assert len(sent) == 1 and sent[0].startswith('TICKET')
    # One signal, one alert: the ALLOW rides on this ticket rather than firing
    # a second notification, so the verdict must be visible ON it.
    assert 'Vetted by Claude' in sent[0]
    for _ in range(3):
        cycle(store)                                     # later ticks: silent
    assert len(sent) == 1, "the order ticket repeated"


def test_live_order_ticket_survives_one_telegram_failure(wired, monkeypatch):
    """The mirror of the test above, and the more dangerous half.

    The consume-once flag is claimed BEFORE the send, and the `triggered`
    fast-path skips the signal once it is set — so a single Telegram hiccup on
    the verdict tick used to lose the ticket permanently. The user would never
    be told to enter a signal Claude had ALLOWED, and it would ride silently to
    drift-cancel. Same claim-then-release discipline as the exit escalation.
    """
    store, _ = wired
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    monkeypatch.setattr(monitor, '_format_enter_alert', lambda *a, **k: 'TICKET')
    monkeypatch.setattr(monitor, '_format_bcs_enter_alert', lambda *a, **k: 'TICKET')
    sent, working = [], [False]
    monkeypatch.setattr(
        monitor, '_send_telegram',
        lambda msg, dry_run=False: (sent.append(msg) or True) if working[0]
        else False)
    cycle(store)
    vet.record_verdict(store, 1, vet.ALLOWED)
    cycle(store)                                   # verdict tick: send FAILS
    assert sent == []
    working[0] = True
    cycle(store)                                   # Telegram is back
    assert len(sent) == 1, "an allowed entry was lost to one Telegram failure"
    for _ in range(2):
        cycle(store)
    assert len(sent) == 1, "the released flag let the ticket repeat"
