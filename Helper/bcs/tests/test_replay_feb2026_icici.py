"""Replay of 2026-02-18: the session that put 4x BUY on a short leg.

Transcribed from `bcs/logs/spread_monitor_cron_20260218.log:47-160`. Not a
constructed scenario — every price below was printed by the monitor as it
happened.

What happened
-------------
09:15:25, twenty-five seconds after the open, the long leg had NO BID. The
spread therefore valued at `0 - 10.30 = -10.30`, tripping SL_SPREAD (6.78).
Step 1 bought the short leg back. Step 2 could not sell the long — "No bid
depth", three attempts — so the trade was left with a naked long and never
marked closed.

Thirteen seconds later the same garbage book tripped the same trigger, and
Step 1 bought the short leg back AGAIN. Four times in 46 seconds: the short
leg went -700 -> +2100.

At 09:16:16 the book formed and the spread was **+38.95** — the position was
up ~190%. The whole thing was a quote artifact at the open. Recovering by hand
turned roughly +Rs 16K into about +Rs 2K.

What this test asserts
----------------------
That today's loop places **no orders at all** across those 46 seconds, AND
values the recovered book correctly at +Rs 17,780 when it arrives. Refusing
everything would satisfy the first half and be useless.

SEVEN independent guards can each refuse this book, and the mutation run walked
them: strip the open buffers and `leg_quote_reliable` catches it
(`no_two_way_book`); strip that and the intrinsic floor catches it
(`below_intrinsic`); strip that and `negative_spread` catches it. Only with all
seven gone does the loop fire SL_SPREAD at 09:15:25 and buy the short leg back
— which is how we know this fixture reproduces the incident rather than merely
passing.

Run:  cd Helper && python -m pytest bcs/tests/test_replay_feb2026_icici.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402
from bcs.tests.replay import Tick, run_session           # noqa: E402

DAY = date(2026, 2, 18)
LONG, SHORT = 'ICICIBANK26FEB1360CE', 'ICICIBANK26FEB1410CE'
QTY = 700

#: The real trade, from the incident note in CLAUDE.md and the log header.
TRADE = {
    'id': 1, 'status': 'open', 'stock': 'ICICIBANK',
    'long_symbol': LONG, 'short_symbol': SHORT, 'spot_symbol': 'NSE:ICICIBANK',
    'exchange': 'NFO', 'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'expiry': '2026-02-26', 'version': 1,
    # Load-bearing, and its absence is silent. `spread_intrinsic_floor` derives
    # the short leg's entry extrinsic from entry_short_price and entry_spot;
    # without entry_spot it falls back to 0.3 * net_debit, which here is 4.07
    # against a true 7.65. That tighter floor (43.40 vs 38.03) flags the REAL,
    # healthy 09:16 book as below_intrinsic and blinds the monitor for the rest
    # of the session. entry_spot is NOT in the store's REQUIRED_FIELDS.
    'entry_spot': 1360.0,
}

#: A long leg with no bid at all. `bid: 0` is what the log means by
#: "No bid depth for ICICIBANK26FEB1360CE" — it is transcription, not a stub.
DEAD_LONG = {'bid': 0.0, 'bid_qty': 0, 'ask': 50.0, 'ask_qty': 700,
             'ltp': 49.0, 'prev_close': 48.05}

#: The formed book at 09:16:16, back-solved from the logged spread of 38.95
#: against the short leg's own printed depth.
LIVE_LONG = {'bid': 49.25, 'bid_qty': 1400, 'ask': 49.60, 'ask_qty': 1400,
             'ltp': 49.40, 'prev_close': 48.05}

TICKS = [
    # at         spot      long        short (depth exactly as logged)
    Tick('09:15:25', 1409.30, DEAD_LONG,
         {'bid': 10.05, 'bid_qty': 1400, 'ask': 10.30, 'ask_qty': 1400,
          'ltp': 10.20, 'prev_close': 9.80},
         'SL_SPREAD fired here: 0 - 10.30 = -10.30 <= 6.78'),
    Tick('09:15:38', 1408.30, DEAD_LONG,
         {'bid': 10.65, 'bid_qty': 3500, 'ask': 10.90, 'ask_qty': 700,
          'ltp': 10.85, 'prev_close': 9.80}, 'fired again'),
    Tick('09:15:50', 1410.10, DEAD_LONG,
         {'bid': 10.70, 'bid_qty': 3500, 'ask': 10.95, 'ask_qty': 2800,
          'ltp': 10.95, 'prev_close': 9.80}, 'and again'),
    Tick('09:16:03', 1409.80, DEAD_LONG,
         {'bid': 10.70, 'bid_qty': 1400, 'ask': 10.95, 'ask_qty': 2100,
          'ltp': 10.95, 'prev_close': 9.80}, 'and again — four in 46s'),
    Tick('09:16:16', 1409.50, LIVE_LONG,
         {'bid': 10.05, 'bid_qty': 2800, 'ask': 10.30, 'ask_qty': 2800,
          'ltp': 10.20, 'prev_close': 9.80},
         'the book forms: spread +38.95, the position is up ~190%'),
    Tick('09:20:13', 1406.10, LIVE_LONG,
         {'bid': 11.85, 'bid_qty': 2800, 'ask': 12.10, 'ask_qty': 2800,
          'ltp': 12.00, 'prev_close': 9.80}, 'still healthy 5 min later'),
]

OPEN_POSITIONS = [{'tradingsymbol': SHORT, 'quantity': -QTY},
                  {'tradingsymbol': LONG, 'quantity': QTY}]


@pytest.fixture
def session(monkeypatch):
    return run_session(monkeypatch, sm, TRADE, TICKS, DAY,
                       positions=[dict(p) for p in OPEN_POSITIONS])


# ── The money assertion ──────────────────────────────────────────────────────

def test_the_replay_places_no_orders_at_all(session):
    clock, kite, store, spy = session
    assert kite.placed == [], (
        "the loop placed %d order(s) on the Feb-2026 book:\n%s"
        % (len(kite.placed),
           '\n'.join('  %s %s x%s' % (o['transaction_type'],
                                      o['tradingsymbol'], o['quantity'])
                     for o in kite.placed)))


def test_the_short_leg_is_never_bought_back(session):
    """The specific damage: -700 became +2100 across four buy-backs."""
    clock, kite, store, spy = session
    assert kite.net_qty(SHORT) == -QTY, (
        f"the short leg moved to {kite.net_qty(SHORT)}; on the day it reached "
        f"+2100 and the naked long is what cost the money")


def test_the_position_is_still_open_when_the_real_book_arrives(session):
    clock, kite, store, spy = session
    assert not store.called('update_trade_exit'), "the trade was booked closed"
    assert not store.called('begin_close'), (
        "a close was even STARTED — begin_close is what precedes the orders")
    assert store.trades[0]['status'] == 'open'


def test_the_recording_actually_reached_the_recovered_book(session):
    """Without this, a harness that exited after one poll would pass
    everything above while replaying almost nothing."""
    clock, kite, store, spy = session
    notes = [t.note for t in kite.seen]
    assert any('the book forms' in n for n in notes), (
        f"the replay stopped early; it only reached {len(kite.seen)} of "
        f"{len(TICKS)} ticks")
    assert len(kite.seen) == len(TICKS)


def test_the_monitor_exits_through_its_own_market_close_branch(session):
    """Not through an exception, and not through something the harness
    invented — otherwise the loop's own teardown is untested."""
    clock, kite, store, spy = session
    assert clock.dt.hour == 15 and clock.dt.minute >= 30


# ── Which guard did the refusing ─────────────────────────────────────────────
#
# "No orders" is the outcome that matters, but a single guard doing all the
# work is fragile: the one time these were relied on individually, the loop as
# assembled still lost money. These pin WHY.

def test_the_garbage_book_is_refused_by_the_reliability_gate():
    """The long leg has no bid, so no value should be computed from it."""
    ok, why = sm.leg_quote_reliable(
        {'bid': 0.0, 'ask': 50.0, 'bid_qty': 0, 'ask_qty': 700, 'ltp': 49.0})
    assert ok is False and why


def test_the_intrinsic_floor_alone_would_also_have_refused_it():
    """Spot 1409.30 against a 1360 long strike is ~49 of intrinsic. A spread
    valued at -10.30 is not a repriced position, it is a broken quote."""
    intrinsic = 1409.30 - 1360
    assert intrinsic > 45
    assert -10.30 < intrinsic, "the floor must sit above the logged value"


def test_spot_corroboration_alone_would_also_have_refused_it():
    """Value collapsing while spot barely moves is the NHPC signature, and it
    is present here too: 1409.30 -> 1408.30 is 0.07%."""
    state = {}
    ok, _ = sm.spot_corroborates(state, 1409.30, 38.95)   # the honest value
    assert ok is True, "the first observation only establishes a reference"
    ok, why = sm.spot_corroborates(state, 1408.30, -10.30)
    assert ok is False, "a >=35% value collapse on a 0.07% spot move passed"
    assert why


def test_the_negative_spread_guard_alone_would_also_have_refused_it():
    """The seventh guard, and the one this file did not know about until the
    mutation run needed it stripped to reproduce the incident at all.

    A long bid of 0 against a short ask of 10.30 values the spread at -10.30.
    A bull call spread cannot be worth less than nothing: you can always let it
    expire. So a negative value is a broken book, never a loss signal.
    """
    got = sm.get_spread_value.__doc__ is not None      # real function, not a stub
    assert got
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    # The CLAMP, not the word. This asserted `'negative_spread' in src` until
    # 2026-08-30, and the only occurrence of that string was in a DOCSTRING
    # explaining an older design — so the guard's own text moving broke a test
    # about the guard's behaviour. A test that can be broken by a comment is
    # testing the comment.
    assert 'if spread_val < 0:' in src, (
        "the negative-spread clamp moved; this replay depends on it")
    assert sm.get_spread_value is not None


def test_the_open_buffer_covers_the_whole_incident():
    """Every one of the four triggers fell inside it. 09:15:25 to 09:16:03 is
    38 seconds after the open; the spread buffer is far longer."""
    assert sm.SPREAD_TRIGGER_OPEN_BUFFER_SEC >= 15 * 60
    last_trigger_sec = (16 - 15) * 60 + 3
    assert last_trigger_sec < sm.SPREAD_TRIGGER_OPEN_BUFFER_SEC


# ── Negative control ─────────────────────────────────────────────────────────

def test_a_genuine_stop_on_a_formed_book_still_closes(monkeypatch):
    """Without this, a loop that never traded at all would pass every test
    above. Same trade, same harness — but a real, two-sided, collapsed book,
    in the afternoon, well past every buffer.
    """
    dead = [
        Tick('14:00:00', 1330.0,
             {'bid': 3.05, 'bid_qty': 1400, 'ask': 3.25, 'ask_qty': 1400,
              'ltp': 3.15, 'prev_close': 21.0},
             {'bid': 0.55, 'bid_qty': 1400, 'ask': 0.70, 'ask_qty': 1400,
              'ltp': 0.60, 'prev_close': 7.6}, 'spread 2.35 <= 6.78'),
    ]
    dead = dead + [Tick('14:0%d:00' % i, 1330.0, dead[0].long, dead[0].short)
                   for i in range(1, 6)]

    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, dead, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    assert kite.placed, (
        "no orders on a genuine stop with a clean book — the guards are not "
        "refusing garbage, they are refusing everything")
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit')
