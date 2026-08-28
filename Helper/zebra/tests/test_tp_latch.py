"""M13 — the FIRST observed take-profit touch arms the exit.

Owner, 2026-08-28: *"touch that doesn't persist — does not matter -> exit -> if
seeing touch once, proven is ok."*

The arming was originally permanent. The same owner bounded it to the trading
day it was armed on later the same day (*"TP latch should be for same day"*);
that bound and its boundary cases live in `test_tp_latch_same_day.py`. Every
test below runs inside ONE session, which is what it was always describing.

What was broken
---------------
Exits are decided on a 5-minute cron tick, and a vetted exit adds ~90 seconds
on top (measured 83-106s on #436 and #450), so the whole round trip from
"trigger observed" to "close booked" is about one cycle. A take-profit
therefore only converted if the touch SURVIVED that window — it was
re-evaluated from scratch, against a later price than the one that fired it.

Measured case, 2026-08-27: COFORGE #436 traded THROUGH its TP (`mfe_spot`
1934.2 against tp 1931.91 at 09:25), the exit vet re-quoted and ALLOWED at
09:27, and by the next actionable poll spot had backed off. Nothing was booked.
The strategy's own target was hit and the position stayed open.

What these tests pin
--------------------
The latch is a decision about WHETHER to exit and never about at what price
(`feedback_trigger_is_not_the_fill`). So each of these has a partner asserting
the thing that must NOT change:

  arms on one touch          / the stops are not latched
  survives a vet defer       / an unreliable book still refuses to book
  survives a restart         / the booking price is the one at the close
  a failed close stays armed / a record with no latch field reads as untouched

Run:  cd Helper && python -m pytest zebra/tests/test_tp_latch.py -v
"""
import inspect
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                       # noqa: E402
from zebra import monitor                             # noqa: E402
from zebra import trade_store as ts                   # noqa: E402
from zebra.trade_store import ZebraStore              # noqa: E402

TP = 100.0
SL = 90.0


@pytest.fixture
def store(tmp_path, monkeypatch):
    """One entered CE position: TP 100, spot SL 90, debit 10 on a 40-wide."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': TP, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    with s._mutate():
        t = s.find(1)
        t['tp_spot'] = TP
        t['sl_spot'] = SL
        t['debit_sl_value'] = 5.0
    return s


def _cycle(store, monkeypatch, spot, mid=30.0, reliable=True, gate=True):
    """One `check_entered` pass. Returns the Telegrams it would have sent.

    `gate=False` is a vet DEFER — `_exit_cleared` returning False is exactly
    what a 'wait' verdict does, and it is the state the trigger used to
    evaporate in.
    """
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': mid, 'reliable': reliable,
                            'reason': None if reliable else 'wide book',
                            'legs': {'long': {'symbol': 'L', 'bid': 40.0,
                                              'ask': 40.2},
                                     'short': {'symbol': 'S', 'bid': 10.0,
                                               'ask': 10.2}},
                            'floored': False})
    monkeypatch.setattr(monitor, '_exit_cleared',
                        lambda *a, **k: gate)
    monitor.check_entered(store, kite=None, dry_run=True)
    return sent


# ── The decision itself, as a function ──────────────────────────────────────

def test_absence_means_not_yet_touched():
    """462 records exist and none of them has this field. Reading one must
    answer "not touched" rather than raising or guessing."""
    assert ts.tp_latched({}) is False
    assert ts.tp_latched({'stock': 'OLD', 'status': 'entered'}) is False
    assert ts.tp_latch({}, False, 100.0)['armed'] is False


def test_the_first_touch_produces_the_patch_and_the_second_does_not():
    first = ts.tp_latch({}, True, 1934.2)
    assert first['armed'] and first['new_touch'] and not first['latched']
    assert first['patch'][ts.TP_TOUCH_SPOT] == 1934.2
    assert first['patch'][ts.TP_TOUCHED_AT]

    already = ts.tp_latch(dict(first['patch']), True, 1935.0)
    assert already['armed'] and already['latched']
    assert already['patch'] == {}, (
        're-touching rewrote the latch — the FIRST touch is the one that '
        'arms, and moving the timestamp would move the latency measurement '
        'with it')


def test_a_latched_record_is_armed_with_the_trigger_gone():
    """The whole rule, in one line: hit_now False, armed True.

    The stamp is 09:25 on the day of the poll, not a fixed date. The owner
    bounded the latch to its own trading day on 2026-08-28 (M13a,
    `test_tp_latch_same_day.py`), so a hardcoded date here would be asserting
    the arming and the expiry at the same time and would answer differently
    depending on when it ran.
    """
    now = datetime(2026, 8, 27, 14, 30, tzinfo=cfg.IST)
    latched = {ts.TP_TOUCHED_AT: datetime(2026, 8, 27, 9, 25,
                                          tzinfo=cfg.IST).isoformat(),
               ts.TP_TOUCH_SPOT: 1934.2}
    assert ts.tp_latch(latched, False, 1900.0, now=now)['armed'] is True


def test_the_latch_carries_no_price_to_book_at():
    """The TRIGGER latches; the PRICE does not. If the touch spot ever became
    a booking price this is the test that fails."""
    patch = ts.tp_latch({}, True, 1934.2)['patch']
    assert set(patch) == {ts.TP_TOUCHED_AT, ts.TP_TOUCH_SPOT}
    assert 'debit' not in patch and 'mid' not in patch and 'value' not in patch


def test_a_spot_that_will_not_float_still_arms_the_exit():
    """Losing the measurement must never cost the arming."""
    out = ts.tp_latch({}, True, None)
    assert out['armed'] and out['patch'][ts.TP_TOUCHED_AT]
    assert out['patch'][ts.TP_TOUCH_SPOT] is None


# ── The engine: one touch, then the retreat ─────────────────────────────────

def test_a_touch_seen_once_and_then_retreating_still_exits(store, monkeypatch):
    """COFORGE #436, replayed. The touch is seen while the vet is deferring;
    by the next actionable poll spot is back below the target.

    PRE-FIX: the second cycle recomputes `spot >= tp_spot`, finds it False,
    and the position stays open forever with its target already hit.
    """
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    assert store.find(1)['status'] == 'entered', 'the vet defer did not hold'
    assert store.find(1)[ts.TP_TOUCHED_AT], (
        'the touch was not persisted — the trigger evaporates the moment the '
        'verdict lands on the next tick')

    _cycle(store, monkeypatch, spot=90.5, gate=True)
    t = store.find(1)
    assert t['status'] == 'exited', (
        'spot backed off before the verdict was consumed and the take-profit '
        'was lost — this is exactly the COFORGE leak M13 exists to close')
    assert t['exit_reason'] == 'paper:tp'


def test_the_latch_survives_a_process_restart(store, monkeypatch, tmp_path):
    """zebra's cron process EXITS between cycles. An in-memory latch would
    look correct in a single-process test and be worthless on the Pi — the
    same reason the corroboration reference and the time-stop state are
    persisted."""
    _cycle(store, monkeypatch, spot=150.0, gate=False)

    fresh = ZebraStore()               # a new process, same file
    fresh.initialize()
    assert fresh.find(1)[ts.TP_TOUCHED_AT], 'the latch did not reach the file'

    _cycle(fresh, monkeypatch, spot=88.0, gate=True)
    assert fresh.find(1)['status'] == 'exited'


def test_the_booked_price_is_the_one_at_the_CLOSE_not_at_the_touch(
        store, monkeypatch):
    """`feedback_trigger_is_not_the_fill`. The touch was worth 40.00 and the
    exit books 28.00, because 28.00 is what the book said when the close
    actually ran. A latch that booked the touch price would be inventing a
    fill nobody could have got."""
    _cycle(store, monkeypatch, spot=150.0, mid=40.0, gate=False)
    _cycle(store, monkeypatch, spot=95.0, mid=28.0, gate=True)
    t = store.find(1)
    assert t['status'] == 'exited'
    assert t['exit_debit'] == pytest.approx(28.0), (
        'the exit booked at the price observed AT THE TOUCH — the trigger '
        'latches, the price never does')
    assert t['exit_spot'] == pytest.approx(95.0)


def test_a_latched_exit_that_cannot_be_booked_stays_armed(store, monkeypatch):
    """Paper never books a price it could not have transacted at. When the
    close is refused for that reason the latch must NOT be spent — otherwise
    the guard that protects the price silently cancels the exit."""
    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False, gate=True)
    t = store.find(1)
    assert t['status'] == 'entered', 'an unreliable book was booked'
    assert t[ts.TP_TOUCHED_AT], 'a refused close cleared the latch'
    assert not t.get('tp_alerted_at'), (
        'the consume-once alert flag was kept on a close that never booked')

    _cycle(store, monkeypatch, spot=93.0, mid=27.0, reliable=True, gate=True)
    assert store.find(1)['status'] == 'exited'
    assert store.find(1)['exit_debit'] == pytest.approx(27.0)


def test_the_latch_survives_many_deferred_cycles(store, monkeypatch):
    """Not one cycle — the trigger has to stay armed for as long as the human
    or the agent takes."""
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    for spot in (140.0, 120.0, 99.0, 91.0):
        _cycle(store, monkeypatch, spot=spot, gate=False)
        assert store.find(1)['status'] == 'entered'
        assert store.find(1)[ts.TP_TOUCHED_AT]
    _cycle(store, monkeypatch, spot=91.0, gate=True)
    assert store.find(1)['status'] == 'exited'


def test_the_first_touch_is_the_one_that_is_recorded(store, monkeypatch):
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    first = dict(store.find(1))
    _cycle(store, monkeypatch, spot=175.0, gate=False)
    again = store.find(1)
    assert again[ts.TP_TOUCHED_AT] == first[ts.TP_TOUCHED_AT]
    assert again[ts.TP_TOUCH_SPOT] == 150.0


# ── The measurement the latency decision will be made on ────────────────────

def test_the_touch_to_fill_gap_is_recorded(store, monkeypatch):
    """This number is what says whether M12 (consuming the verdict inside the
    same cycle) is worth building. Without it the cost of the lag is an
    argument rather than a figure."""
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    _cycle(store, monkeypatch, spot=140.0, gate=True)
    t = store.find(1)
    assert t['tp_touch_spot'] == 150.0
    assert t['tp_touch_spot_move'] == pytest.approx(-10.0)
    assert t['tp_touch_spot_move_pct'] == pytest.approx(-6.6667, abs=1e-3)
    assert t['tp_touch_gave_back'] is True
    assert t['tp_touch_to_exit_sec'] >= 0


def test_a_tp_that_books_on_the_touch_reports_no_give_back(store, monkeypatch):
    """Negative control for the measurement: same fields, no give-back, and
    the gap near zero. Without this the test above passes on a stamp that
    always says "gave back"."""
    _cycle(store, monkeypatch, spot=150.0, gate=True)
    t = store.find(1)
    assert t['status'] == 'exited'
    assert t['tp_touch_spot_move'] == pytest.approx(0.0)
    assert t['tp_touch_gave_back'] is False
    assert t['tp_touch_to_exit_sec'] < 60


def test_the_gap_reads_a_PE_target_the_other_way_round():
    """A bear put spread's TP is a FALL. Spot rising after the touch is the
    give-back there, and a shared measurement that assumed CE would label
    every PE exit backwards."""
    latched = {ts.TP_TOUCHED_AT: datetime.now().isoformat(),
               ts.TP_TOUCH_SPOT: 100.0, 'tp_spot': 105.0}
    assert ts.tp_touch_to_fill(latched, 104.0, rising=False)[
        'tp_touch_gave_back'] is True
    assert ts.tp_touch_to_fill(latched, 96.0, rising=False)[
        'tp_touch_gave_back'] is False


def test_the_gap_never_raises_on_a_mixed_clock():
    """zebra reasons in IST-aware datetimes and this store writes naive local
    time. Subtracting one from the other raises, and a measurement must not be
    able to throw inside an exit path."""
    aware = datetime.now(cfg.IST)
    latched = {ts.TP_TOUCHED_AT: (aware - timedelta(seconds=300)).isoformat(),
               ts.TP_TOUCH_SPOT: 100.0}
    out = ts.tp_touch_to_fill(latched, 99.0, rising=True, now=aware)
    assert out['tp_touch_to_exit_sec'] == pytest.approx(300.0, abs=1)


def test_an_unlatched_record_reports_no_gap():
    """Inventing a zero here would make the distribution unreadable — a TP
    that booked on its own touch is not the same fact as a five-minute lag."""
    assert ts.tp_touch_to_fill({}, 100.0) == {}


# ── What must NOT latch ─────────────────────────────────────────────────────

def test_the_spot_stop_is_not_latched(store, monkeypatch):
    """`CLAUDE.md`, "Spot-based stops — VETO, never TRIGGER": measured over 147
    records, a 3% spot stop cut 31 of 78 winners for a Rs 8.9L giveaway.
    Latching it would promote a veto to a permanently-armed trigger, which is
    that finding inverted."""
    monkeypatch.setattr(cfg, 'SPOT_SL_ENABLED', True)
    with store._mutate():
        store.find(1)['tp_spot'] = 500.0        # keep TP far out of the way

    _cycle(store, monkeypatch, spot=85.0, gate=False)   # through the spot SL
    assert store.find(1)['status'] == 'entered'
    assert not any(k.startswith('sl_touched') or k.startswith('spot_sl_touched')
                   for k in store.find(1)), 'a stop grew a latch'

    _cycle(store, monkeypatch, spot=95.0, gate=True)    # recovered
    assert store.find(1)['status'] == 'entered', (
        'a spot stop that had recovered still fired — the stop must be '
        're-evaluated on every poll, unlike the take-profit')


def test_the_value_stops_are_not_latched(store, monkeypatch):
    """DEBIT-SL and TRAIL are defended by an N-consecutive-reliable-poll
    debounce that exists because ONE garbage print cost Rs 7,297 (NHPC,
    2026-07-24). A latch is a debounce of one, in the loss direction, on the
    source that has twice been wrong."""
    _cycle(store, monkeypatch, spot=95.0, mid=4.0, gate=False)  # under debit_sl
    assert store.find(1)['status'] == 'entered'
    _cycle(store, monkeypatch, spot=95.0, mid=30.0, gate=True)  # recovered
    assert store.find(1)['status'] == 'entered', (
        'a value stop fired on a book that had recovered — only the '
        'take-profit latches')


def test_only_the_take_profit_reads_the_latch_in_the_cascade():
    """Pinned on the SOURCE, because a mutation moving `tp_latch` onto a stop
    would still pass every behavioural test above until that stop happened to
    fire in the same test. One call, in one branch."""
    src = inspect.getsource(monitor.check_entered)
    assert src.count('tp_latch(') == 1, (
        'the latch is called more than once in the exit cascade — it is a '
        'take-profit rule and the stops must stay re-evaluated every poll')


# ── The alert has to tell the truth ─────────────────────────────────────────

def test_the_alert_says_the_TOUCH_fired_it_not_the_current_spot(
        store, monkeypatch):
    """The reader acts on this message. "spot 90.50 hit TP 100.00" is false,
    and a ticket that is visibly wrong about the trigger is one the reader
    learns to distrust about everything else."""
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    sent = _cycle(store, monkeypatch, spot=90.5, gate=True)
    tp_msgs = [m for m in sent if 'TP' in m]
    assert tp_msgs, 'the latched exit fired with no alert at all'
    msg = tp_msgs[0]
    assert 'TOUCHED' in msg and '150.00' in msg
    assert 'spot 90.50 hit TP' not in msg, (
        'the alert claims a touch that is not happening')


def test_the_ordinary_tp_alert_is_unchanged(store, monkeypatch):
    sent = _cycle(store, monkeypatch, spot=150.0, gate=True)
    assert any('spot 150.00 hit TP 100.00' in m for m in sent)


def test_the_poll_line_says_a_position_is_armed(store, monkeypatch, caplog):
    """An armed TP waiting on a verdict looks exactly like a quiet position in
    the log, and in paper the log is the entire forensic record."""
    import logging
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        _cycle(store, monkeypatch, spot=90.5, gate=False)
    assert any('TP-LATCHED' in r.message for r in caplog.records
               if r.message.startswith('POLL')), \
        'the log cannot distinguish an armed position from a quiet one'


def test_nothing_ever_CLEARS_the_latch(store, monkeypatch):
    """A latched exit that fails to book stays armed — for good.

    Asserted on the SOURCE as well as behaviourally, because the dangerous
    version of this bug is a tidy-up: a `pop`, a `del`, or a reset alongside
    the confirm counters would silently return the system to the behaviour
    M13 exists to end, and no ordinary test would notice until a real
    take-profit went missing again.
    """
    from bcs import spread_monitor as sm
    for mod in (monitor, ts, sm):
        src = inspect.getsource(mod)
        for pattern in ("pop('tp_touched_at'", 'pop("tp_touched_at"',
                        "del t['tp_touched_at']", "['tp_touched_at'] = None",
                        "tp_touched_at=None"):
            assert pattern not in src, (
                f'{mod.__name__} clears the take-profit latch ({pattern}) — '
                f'a refused or failed close must leave the exit ARMED')

    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False, gate=True)
    _cycle(store, monkeypatch, spot=95.0, mid=30.0, reliable=False, gate=True)
    _cycle(store, monkeypatch, spot=95.0, mid=30.0, reliable=False, gate=False)
    assert store.find(1)[ts.TP_TOUCHED_AT], (
        'three consecutive failures to book cleared the arming')


def test_a_touch_on_a_DARK_BOOK_cycle_is_still_latched(store, monkeypatch):
    """The touch is a fact about SPOT, and spot is a real trade.

    When the option book quotes nothing the whole cascade defers — correctly,
    since paper must never book a price it could not have transacted at. But
    that defer used to happen ABOVE the take-profit branch, so a touch seen on
    a dark cycle was never recorded at all: the same leak as COFORGE by a
    different route, and one that no vet is involved in.
    """
    _cycle(store, monkeypatch, spot=150.0, mid=None)
    t = store.find(1)
    assert t['status'] == 'entered', 'a position with no book was booked'
    assert t[ts.TP_TOUCHED_AT], (
        'the target was reached on a cycle the option book was dark and the '
        'touch was thrown away')

    _cycle(store, monkeypatch, spot=92.0, mid=26.0)
    assert store.find(1)['status'] == 'exited'
    assert store.find(1)['exit_debit'] == pytest.approx(26.0)


def test_the_latch_is_NOT_written_while_this_engine_is_stood_down(
        store, monkeypatch, caplog):
    """`exits_managed_externally` hands this position's trigger to
    `bcs/spread_monitor.py`, which watches the same spot every five seconds
    and arms its own. Both engines READ the latch; only the one holding the
    trigger writes it — otherwise the stood-down engine is arming an exit it
    is not allowed to take."""
    import logging
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    with store._mutate():
        t = store.find(1)
        t['cohort'] = cfg.COHORT_START
        t['paper'] = False
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        _cycle(store, monkeypatch, spot=150.0, gate=True)
    t = store.find(1)
    assert t['status'] == 'entered'
    assert ts.TP_TOUCHED_AT not in t or not t[ts.TP_TOUCHED_AT]
    assert any('exits are EXTERNAL' in r.message for r in caplog.records), (
        'the stood-down engine declined to record the touch silently — that '
        'is indistinguishable from never having seen it')


def test_a_corporate_action_day_cannot_latch_a_stale_level(store, monkeypatch):
    """A bonus or split re-prices the underlying while `tp_spot` still refers
    to yesterday's scale, so spot "touches" a target for a reason that has
    nothing to do with the market. Every other exit is already suspended for
    the day; a PERMANENT arming off a stale level is the one version of this
    rule that could lose money, so the latch sits below that guard."""
    monkeypatch.setattr(monitor.events_mod, 'adjustment_today',
                        lambda stock: {'type': 'bonus', 'ratio': '1:1'})
    _cycle(store, monkeypatch, spot=150.0, gate=True)
    t = store.find(1)
    assert t['status'] == 'entered'
    assert ts.TP_TOUCHED_AT not in t or not t[ts.TP_TOUCHED_AT], (
        'a take-profit was armed permanently off a spot the exchange had '
        'just re-based')
