"""The INVERSE direction: does a guard ever block an exit that should happen?

`feedback_live_automation_bar` line 4: "Guards must protect BOTH directions —
against false firing AND against blocking/delaying a real exit. A guard
reviewed only against the incident direction creates the inverse bug."

Every guard on this path was written after a FALSE trigger cost money, and
every one of them was reviewed against that direction. None was reviewed
against the other. That asymmetry is the point: a monitor that refuses a
genuine stop is not obviously broken — the position simply keeps running, the
logs look healthy, and the loss shows up as a bad trade rather than as a bug.
It is the same silent-unmonitoring failure that cost money twice, wearing the
opposite sign.

The unit tests next door already ask this of individual functions
(`test_the_floor_is_generous_enough_to_let_a_real_stop_through`,
`test_a_collapse_the_underlying_explains_is_allowed_through`). This file asks
it of the ASSEMBLED loop, because composition is where the guards stack: the
open buffer, then the reliability gate, then corroboration, then the debounce,
then the re-verify, each one able to withhold an exit the previous one allowed.
A stop that every guard individually permits can still never fire.

Where the answer is "yes, blocked" or "yes, delayed", the test PINS the
behaviour with the reason rather than asserting it away. A known, measured
hole is a decision; an unmeasured one is the bug.

Run:  cd Helper && python -m pytest bcs/tests/test_guards_do_not_block_a_real_exit.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402
from bcs.tests.replay import Tick, run_session           # noqa: E402

DAY = date(2026, 9, 15)
LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

#: net_debit 13.55, so sl_spread 6.78 is the documented 50% guard.
TRADE = {
    'id': 1, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'long_symbol': LONG, 'short_symbol': SHORT, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
}

POSITIONS = [{'tradingsymbol': SHORT, 'quantity': -QTY},
             {'tradingsymbol': LONG, 'quantity': QTY}]


def book(bid, ask, qty=1400, ltp=None, prev=None):
    """A tidy two-way book: normal width, real size, LTP inside the spread."""
    return {'bid': bid, 'bid_qty': qty, 'ask': ask, 'ask_qty': qty,
            'ltp': ltp if ltp is not None else round((bid + ask) / 2, 2),
            'prev_close': prev if prev is not None else bid}


#: Healthy mid-session: spread worth 40.00 - 10.30 = 29.70, well above the stop.
FINE_LONG, FINE_SHORT = book(40.00, 40.20), book(10.05, 10.30)

#: A GENUINE stop. Every leg is tidy — normal widths, real depth, LTP inside
#: the book — and the value is 20.00 - 13.50 = 6.50, under the 6.78 guard.
#: Nothing here is a garbage print; the trade simply went wrong.
STOP_LONG, STOP_SHORT = book(20.00, 20.30), book(13.30, 13.50)


def ticks(times, spot, long, short, note=''):
    return [Tick(t, spot, long, short, note) for t in times]


def _every(start_h, start_m, n, step_min=1):
    out = []
    for i in range(n):
        total = start_h * 60 + start_m + i * step_min
        out.append('%02d:%02d:00' % (total // 60, total % 60))
    return out


def genuine_stop_session(monkeypatch, from_time='11:00:00', calm=3, breach=10,
                         spot_calm=1400.0, spot_stop=1340.0, **kw):
    """Calm, then a real deterioration that never recovers.

    Spot falls 4.3% alongside the structure, so the collapse is one the
    underlying explains — and it stays above sl_spot (1319), which isolates
    SL_SPREAD from SL_SPOT. Otherwise a passing test would prove only that
    SOME exit fired.
    """
    h, m = (int(x) for x in from_time.split(':')[:2])
    t = ticks(_every(h, m, calm), spot_calm, FINE_LONG, FINE_SHORT, 'calm')
    t += ticks(_every(h, m + calm, breach), spot_stop, STOP_LONG, STOP_SHORT,
               'genuine stop: 6.50 <= 6.78, tidy book, spot down 4.3%')
    return run_session(monkeypatch, sm, TRADE, t, DAY,
                       positions=[dict(p) for p in POSITIONS], **kw)


# ── The headline: a real stop, mid-session, must actually close ─────────────

@pytest.fixture
def genuine(monkeypatch):
    return genuine_stop_session(monkeypatch)


def test_a_genuine_stop_is_not_blocked_by_the_stack(genuine):
    """If this ever goes red, the monitor has stopped protecting the book —
    and it will look perfectly healthy while doing it."""
    clock, kite, store, spy = genuine
    assert kite.placed, (
        'a tidy book, a corroborating spot fall and a value under the stop, '
        'and NOTHING was placed. Some guard is refusing a real exit.')
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit')


def test_the_genuine_stop_closes_the_short_leg_first(genuine):
    """Selling the long first leaves a naked short and a margin spike."""
    clock, kite, store, spy = genuine
    order = [o['tradingsymbol'] for o in kite.placed]
    assert order.index(SHORT) < order.index(LONG), order


def test_the_genuine_stop_is_reported(genuine):
    clock, kite, store, spy = genuine
    assert spy.sent, 'the book was closed and the owner was not told'


def test_a_value_just_above_the_stop_is_left_alone(monkeypatch):
    """Negative control for every test above. 6.80 against a 6.78 guard —
    two paise of daylight, same tidy book, same spot fall. A suite that only
    ever showed the stop firing could not tell 'the guards allow it' from
    'the monitor closes everything'.
    """
    t = ticks(_every(11, 0, 3), 1400.0, FINE_LONG, FINE_SHORT)
    t += ticks(_every(11, 3, 10), 1340.0, book(20.00, 20.30),
               book(13.00, 13.20), 'value 6.80 > 6.78')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == [], 'closed a position that was above its stop'


# ── Each guard, asked whether IT is the one that would block ────────────────

def test_the_debounce_delays_the_stop_but_does_not_cancel_it(monkeypatch):
    """SL_CONFIRM_POLLS=3 exists so one bad print cannot trigger. The inverse
    risk is a stop that needs a persistence the market never grants.

    Pinned as a NUMBER: three consecutive polls at POLL_INTERVAL_SEC=5 is a
    ~15-second delay on a genuine stop. That is the price of the guard, and it
    is only acceptable while it stays this small.
    """
    assert sm.SL_CONFIRM_POLLS == 3
    assert sm.POLL_INTERVAL_SEC == 5
    assert sm.SL_CONFIRM_POLLS * sm.POLL_INTERVAL_SEC <= 30, (
        'the debounce now delays a real stop by more than 30s')

    clock, kite, store, spy = genuine_stop_session(monkeypatch, breach=10)
    assert kite.placed, 'the debounce swallowed a persistent genuine stop'


def test_a_stop_that_does_not_persist_is_correctly_ignored(monkeypatch):
    """The other side of the same guard: a breach that does NOT persist must
    not trade. Without this, the test above could pass on a monitor with no
    debounce at all.

    Note the SECONDS. A tick stays in force until the next one, and the loop
    polls every 5s, so a breach tick a minute wide is twelve consecutive
    breaching polls — four times the debounce. The first draft of this test
    called that "one bad print" and failed, correctly.
    """
    t = ticks(_every(11, 0, 2), 1400.0, FINE_LONG, FINE_SHORT)
    t += [Tick('11:02:00', 1340.0, STOP_LONG, STOP_SHORT, 'one bad print'),
          Tick('11:02:06', 1400.0, FINE_LONG, FINE_SHORT, 'gone 6s later')]
    t += ticks(_every(11, 3, 6), 1400.0, FINE_LONG, FINE_SHORT, 'recovered')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == [], 'a sub-debounce dip was traded on'


def test_corroboration_does_not_veto_a_stop_the_spot_explains(monkeypatch):
    """`spot_corroborates` is the guard with the most power to block, because
    it can refuse an exit the whole rest of the stack allowed. It is veto-only
    for exactly this reason — but veto-only is not the same as harmless.
    """
    clock, kite, store, spy = genuine_stop_session(
        monkeypatch, spot_calm=1400.0, spot_stop=1340.0)
    assert kite.placed, 'a 4.3% spot fall failed to corroborate a real stop'


def test_corroboration_does_veto_the_same_collapse_with_a_flat_spot(monkeypatch):
    """Negative control, and the NHPC shape: identical books, spot unchanged."""
    t = ticks(_every(11, 0, 3), 1400.0, FINE_LONG, FINE_SHORT)
    t += ticks(_every(11, 3, 10), 1400.4, STOP_LONG, STOP_SHORT,
               'value collapses, spot moves 0.03%')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == [], 'the NHPC shape was traded on'


#: Ordinary books that must NOT be called unreliable, and WHICH constant
#: saves each one. Split deliberately: a mutation run showed that testing only
#: the tick-sized books left QUOTE_MAX_WIDTH_PCT completely uncovered -- both
#: were rescued by QUOTE_MAX_WIDTH_ABS, so tightening the percentage cap from
#: 25% to 5% changed nothing and the suite stayed green.
ORDINARY_BOOKS = [
    (book(2.40, 2.60, ltp=2.50), 'ABS', '8% of mid, a few ticks wide'),
    (book(0.40, 0.60, ltp=0.50), 'ABS', 'a near-worthless leg 4 ticks wide — 40% of mid, which only QUOTE_MAX_WIDTH_ABS forgives'),
    (book(8.10, 9.00, ltp=8.60), 'PCT', '10.5% of mid — the top of the normal '
                                        'band for an illiquid stock option'),
    (book(48.00, 49.60, ltp=48.80), 'PCT', '3% of mid on a deep-ITM leg, but '
                                           'Rs 1.60 in absolute terms'),
]


@pytest.mark.parametrize('leg,saved_by,what', ORDINARY_BOOKS)
def test_the_reliability_gate_does_not_reject_an_ordinary_book(leg, saved_by,
                                                               what):
    """The gate blinds the monitor on any leg it rejects, so a cap set too
    tight is an outage on exactly the illiquid strikes most needing watching.
    """
    ok, why = sm.leg_quote_reliable(leg)
    assert ok, f'an ordinary book ({what}) was called unreliable: {why}'


def test_the_reliability_gate_still_rejects_the_incident_book():
    """Negative control: the NHPC print, 133% of mid."""
    ok, why = sm.leg_quote_reliable(book(0.28, 1.40, ltp=0.30))
    assert not ok and 'wide_book' in why, why


#: The two books actually recorded on 2026-02-18, transcribed in
#: `test_replay_feb2026_icici.py` from cron_20260218.log.
#:
#: Its OWN strikes, not TESTCO's. The floor is computed from the strikes parsed
#: out of the tradingsymbols, so reusing the fixture trade above would have
#: valued a 1360/1410 book against 1340/1390 -- which is how the first draft of
#: this test produced a failure that looked like a finding.
REAL_ICICI = {
    'long_symbol': 'ICICIBANK26FEB1360CE',
    'short_symbol': 'ICICIBANK26FEB1410CE',
    'entry_long_price': 21.20, 'entry_short_price': 7.65,
    'entry_spot': 1360.0, 'net_debit': 13.55, 'spread_width': 50,
}

#: (spot, long bid, short ask, what it was)
REAL_ICICI_BOOKS = [
    (1409.50, 49.25, 10.30, 'the formed 09:16:16 book, position up ~190%'),
    (1406.10, 49.25, 12.10, 'still healthy at 09:20:13'),
]


@pytest.mark.parametrize('spot,long_bid,short_ask,what', REAL_ICICI_BOOKS)
def test_the_floor_accepts_the_real_recorded_books(spot, long_bid, short_ask,
                                                   what):
    """The floor refuses any value beneath it, so a floor set above a genuine
    price strands the position. Tested against REAL books rather than invented
    ones, because whether the calibration is generous is a question about the
    market, not about arithmetic.
    """
    floor = sm.spread_intrinsic_floor(REAL_ICICI, spot)
    value = long_bid - short_ask
    assert value >= floor, (
        f'the floor {floor:.2f} refuses {value:.2f} — {what}. A real, healthy '
        f'book rejected by an anti-garbage guard.')


def test_the_floors_margin_on_real_data_is_measured_not_assumed():
    """MEASURED FINDING, pinned so a recalibration cannot move it silently.

    The docstring on `spread_intrinsic_floor` calls it "deliberately generous
    — it only ever fires on the impossible, never the merely unfavourable."
    On the only real near-ATM book in evidence the margin is **Rs 0.93** on a
    value of 38.95, i.e. 2.4%.

    A short ask of 11.23 instead of 10.30 refuses it. That is a 9% move in one
    leg with spot held — and that same short leg went 10.30 -> 12.10 inside
    five minutes that morning. It passed at 09:20 only because spot fell 3.4
    alongside, dropping the floor with it. Extrinsic widening with spot FLAT
    is what refuses, and an IV spike is exactly that shape.

    Consequence when it fires is degradation, not abandonment: the valuation
    is refused, so the record goes blind, SL_TRAIL cannot arm or update, and
    the blind alert follows within 15 minutes. TP and SL_SPOT read spot and
    are unaffected. The exposure is a trailing stop that stops trailing on a
    WINNER during a vol spike.

    Deliberately NOT recalibrated here. Loosening the multiplier weakens the
    ABB #242 protection this guard exists for, in the direction that has
    actually cost money — the classic both-directions tension, and the owner's
    call, not a test's.
    """
    spot, long_bid, short_ask, _ = REAL_ICICI_BOOKS[0]
    margin = (long_bid - short_ask) - sm.spread_intrinsic_floor(REAL_ICICI, spot)
    assert 0.5 < margin < 1.5, (
        f'the floor margin on the real 09:16 book is now {margin:.2f}, not '
        f'the measured 0.93. Someone recalibrated the floor or the allowance; '
        f'read this docstring before deciding that is fine.')


def test_the_intrinsic_floor_is_inert_below_the_long_strike_by_construction():
    """Below the long strike the floor cannot reject anything, and that is fine.

    This test earned its keep on 2026-08-30. The two engines' floors were being
    merged into `common/spread_valuation`, and the merge was justified partly
    by "zebra clamps the floor at zero and the money path does not, so the
    guard is inert here in the loss region". This test said no: the VALUE is
    clamped to >= 0 upstream, so a floor at or below zero rejects nothing
    either way, and the clamp changes no outcome on this path.

    The clamp is in the shared module because CLAUDE.md documents it and it
    costs nothing. The claim that it FIXED something here was wrong, and this
    is where that was caught.

    (The docstring it replaced said the negative value was REFUSED upstream.
    That was itself stale — the August bounds change made it a clamp to 0.
    Same conclusion, different reason.)
    """
    assert sm.spread_intrinsic_floor(TRADE, 1300.0) <= 0
    assert sm.spread_intrinsic_floor(TRADE, 1420.0) > 0


def test_a_worthless_spread_still_exits_via_spot(monkeypatch):
    """The case a reader will flag as a blocked exit, and the reason it is not.

    Long bid 0.05 / short ask 0.20 values the spread at -0.15, which
    `negative_spread` refuses, so SL_SPREAD cannot fire. For a LIVE order path
    that is correct: both legs are far OTM, expiry costs nothing, and closing
    means paying the short ask — the exit is worth LESS than the non-exit.
    zebra clamps the same book to 0 instead, because zebra needs a NUMBER for
    its P&L while this needs a DECISION about an order.

    The position is not abandoned: spot owns this case, and it fires.
    """
    t = ticks(_every(11, 0, 3), 1400.0, FINE_LONG, FINE_SHORT)
    t += ticks(_every(11, 3, 10), 1250.0, book(0.05, 0.15, ltp=0.10),
               book(0.10, 0.20, ltp=0.15), 'worthless, both legs far OTM')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed, 'spot far below sl_spot and nothing fired'


# ── The open buffer is the one guard that blocks by DESIGN ──────────────────

def test_sl_spread_is_dark_for_the_first_15_minutes(monkeypatch):
    """A real stop present at 09:16 is NOT acted on. Deliberate — both
    real-money losses were on the opening prints — but it is a hole, so it is
    measured rather than assumed.
    """
    assert sm.SPREAD_TRIGGER_OPEN_BUFFER_SEC == 900
    t = ticks(_every(9, 16, 8), 1340.0, STOP_LONG, STOP_SHORT,
              'genuine stop, but inside the opening buffer')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == [], (
        'SL_SPREAD fired inside the opening buffer — the buffer is the fix '
        'for both real-money losses')


def test_the_same_stop_fires_once_the_buffer_expires(monkeypatch):
    """The buffer must DELAY, never cancel. If state accumulated during the
    dark window suppressed the trigger afterwards, the hole would be the whole
    morning instead of fifteen minutes.
    """
    t = ticks(_every(9, 16, 14), 1340.0, STOP_LONG, STOP_SHORT, 'dark window')
    t += ticks(_every(9, 31, 10), 1340.0, STOP_LONG, STOP_SHORT, 'buffer over')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed, (
        'a stop that was live at 09:16 and still live at 09:31 never fired — '
        'the opening buffer cancelled it instead of delaying it')


def test_spot_triggers_are_deliberately_not_dark_at_the_open(monkeypatch):
    """SL_SPOT and TP read the underlying, not the option book, so the
    unformed-book argument does not apply to them. Gating them too would have
    made the buffer a blanket 15-minute outage.
    """
    t = ticks(_every(9, 16, 8), 1300.0, FINE_LONG, FINE_SHORT,
              'spot 1300 < sl_spot 1319, at 09:16')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, t, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed, 'SL_SPOT was suppressed by the spread buffer'


# ── The kill switch must withhold the ORDER, never the WARNING ──────────────

def test_a_disarmed_monitor_still_alerts_on_a_genuine_stop(monkeypatch,
                                                           tmp_path):
    """The worst possible reading of a kill switch: it stops the orders AND
    the alerts, so the owner believes he is closing by hand while being told
    nothing. Disarmed means "you close it", which only works if he is told.
    """
    off = tmp_path / 'trading_switch.json'
    off.write_text('{"trading": {"enabled": false}}')
    monkeypatch.setattr(sm, 'SWITCH_FILE', off)

    clock, kite, store, spy = genuine_stop_session(monkeypatch)
    assert kite.placed == [], 'a disarmed monitor placed an order'
    assert spy.sent, 'a disarmed monitor went silent on a genuine stop'
    assert spy.any('DISARMED'), (
        f'the owner was not told the monitor was disarmed: {spy.sent}')
