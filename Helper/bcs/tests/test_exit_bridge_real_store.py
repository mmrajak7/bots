"""The exit bridge, driven against the REAL `ZebraStore`.

Why this file exists
--------------------
`bcs/tests/test_zebra_bridge.py` is 600 lines and contains ZERO tests of the
`begin_close -> update_trade_exit` cycle — the two calls that constitute the
whole live close. `bcs/tests/replay.py` substitutes `MemoryStore` for the
cohort book, so the real state machine appeared in NO replay, and both calls
sit inside `if not dry_run:` so the dry-run session on the Pi cannot exercise
them either. Three separate blind spots, all pointing at the same two lines.

What they were hiding (both verified against the pre-fix code by these tests):

1. `ZebraStore.mark_exited` required `status == 'entered'`, but `begin_close`
   persists `'closing'` BEFORE any order leaves for the broker. Every bridged
   close raised, the caller froze the record at `partial_close` and Telegrammed
   "manual intervention needed" — after the legs were already flat.
2. `ZebraStoreAdapter.update_trade_exit` read `exit_value` and `reason`. The
   monitor writes `exit_spread` and `exit_reason`; nothing in the repo has ever
   written `exit_value`. Both `.get()`s returned None, so `exit_debit=None` sent
   `_apply_exit` down its max-loss branch: every exit books **-100%**,
   take-profits included, under `exit_reason='unknown'`.

Defect 2 is why defect 1 could not be fixed alone. Fixing the precondition
without the key names replaces a loud freeze with a silent -100% on a winner —
and the arming gate for this cohort is read off exactly those P&L figures.

Nothing here is mocked except Google Drive and the file location. The store,
the adapter, the lock, the merge, the fee stamp and the bound checks are all
production code.
"""
from __future__ import annotations

import ast
import inspect
import json

import pytest

from bcs import spread_monitor as sm
from bcs.zebra_adapter import ZebraStoreAdapter
from zebra import config as zcfg
from zebra import capital
from zebra.trade_store import ZebraStore

COHORT = zcfg.COHORT_START

#: An entered cohort BCS, in ZEBRA's own field names — this is what the file on
#: the Pi actually holds. `debit` 13.55 x 700 shares; a 50-wide vertical.
ENTERED = {
    'id': 419, 'version': 4, 'status': 'entered',
    'cohort': COHORT, 'structure': 'bcs',
    'stock': 'TESTCO', 'timeframe': 'monthly', 'direction': 'CE',
    'st_value': 1400.0, 'st_direction': 'DOWN',
    'signal_price': 1350.0, 'signal_gap_pct': 3.7,
    'long_symbol': 'TESTCO26SEP1340CE', 'short_symbol': 'TESTCO26SEP1390CE',
    'long_strike': 1340, 'short_strike': 1390,
    'debit': 13.55, 'width': 50, 'quantity': 700, 'lot_size': 700, 'lots': 1,
    'long_ask_entry': 21.20, 'short_bid_entry': 7.65,
    'entry_spot': 1360.0, 'entry_date': '2026-08-20',
    'expiry': '2026-09-29', 'tp_spot': 1400.0, 'sl_spot': 1319.0,
    'debit_sl_value': 6.78, 'capital': 9485.0,
    'paper': True,
}


@pytest.fixture
def zstore(tmp_path, monkeypatch):
    """A REAL `ZebraStore` on a tempfile, with Drive off.

    Every path constant is redirected, not just LOG_DIR — `zebra/config.py`
    derives LOCAL_FILE and LOCK_FILE at IMPORT, so rebinding the directory
    afterwards moves nothing (the lesson recorded in `zebra/tests/conftest.py`
    after a suite run rewrote the production decision journal).
    """
    d = tmp_path / 'zebra'
    d.mkdir()
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')

    def _seed(records):
        (d / 'zebra_trades.json').write_text(json.dumps(records))
        s = ZebraStore(config={'google_drive': {'enabled': False}})
        s.initialize()
        assert not s._drive_enabled, 'this test must never reach Drive'
        return s

    return _seed


@pytest.fixture
def bridge(zstore):
    """The adapter over a real store holding one entered cohort position."""
    return ZebraStoreAdapter(zstore([dict(ENTERED)]))


def _monitor_exit_data(exit_net, reason, spot=1401.0,
                       short_fill=10.20, long_fill=50.20):
    """`exit_data` in exactly the shape `_close_spread_inner` builds it.

    Copied from `bcs/spread_monitor.py:2227`. `test_the_adapter_reads_the_keys
    _the_monitor_actually_writes` below re-derives the key set from the live
    source, so this fixture cannot drift away from production unnoticed.
    """
    return {
        'exit_date': '2026-09-21T14:35:00',
        'exit_reason': reason,
        'exit_spot': spot,
        'short_fill': short_fill,
        'long_fill': long_fill,
        'exit_spread': exit_net,
        'pnl_per_share': exit_net - 13.55,
        'total_pnl': (exit_net - 13.55) * 700,
    }


# ── The cycle, end to end ───────────────────────────────────────────────────

def test_a_take_profit_close_books_a_POSITIVE_pnl(bridge):
    """The headline assertion, and the one the arming gate depends on.

    Long fill 50.20, short buy-back 10.20 => exit spread 40.00 against a debit
    of 13.55. That is +26.45/share, +Rs 18,515 on 700 shares, +195%.

    Against the pre-fix code this test does not merely fail, it fails TWICE
    over, and the two failures are in opposite directions:

    * with the old precondition, `begin_close` sets 'closing' and `mark_exited`
      raises `ValueError: #419 status=closing, can't exit`;
    * with the precondition fixed but the key names not, it returns quietly
      having booked pnl_pct = -100.0 on a trade that made three times its money.
    """
    assert bridge.begin_close(419, 'TP') is True
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))

    t = bridge.raw.find(419)
    assert t['status'] == 'exited'
    assert t['exit_debit'] == pytest.approx(40.00)
    assert t['pnl'] == pytest.approx(18515.0), (
        'a take-profit booked a loss — this is the -100%-on-a-winner shape '
        'that would have poisoned the cohort scorecard')
    assert t['pnl'] > 0
    assert t['pnl_pct'] == pytest.approx(195.2, abs=0.1)


def test_the_reason_survives_the_bridge(bridge):
    """`exit_reason='unknown'` is not a cosmetic defect.

    `outcomes.label_for_reason` and the arming gate both classify exits by this
    string. The gate clears only on real STOP exits, so a book full of
    'unknown' cannot clear it and cannot be shown not to have cleared it
    either — the evidence is simply gone.
    """
    bridge.begin_close(419, 'SL_SPREAD')
    bridge.update_trade_exit(419, _monitor_exit_data(6.10, 'SL_SPREAD'))
    assert bridge.raw.find(419)['exit_reason'] == 'sl_spread'


def test_the_exit_book_is_persisted_from_the_fills(bridge):
    """The exit book is the one direction that has twice cost real money and
    the one direction with no evidence. The monitor hands over two fill
    scalars; they must land in `exit_legs`, priced, so `zebra/fees.py` costs
    the round trip off REAL fills instead of a decay estimate."""
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))

    legs = bridge.raw.find(419).get('exit_legs')
    assert legs, 'the exit book was thrown away'
    assert legs['long']['price'] == pytest.approx(50.20)
    assert legs['short']['price'] == pytest.approx(10.20)
    assert legs['long']['source'] == 'fill'
    assert 'symbol' not in legs['long'], (
        'the leg book carries a key exit_data does not contain — that is the '
        'read-a-key-nobody-writes shape, persisted this time')


def test_the_net_pnl_is_costed_from_the_real_fills(bridge):
    """Consequence of the book being there: `round_trip_for_trade` finds a
    price on both legs and does NOT fall back to scaling the entry legs by the
    structure's decay. The gross/net gap is the whole point of the paper run
    (+0.90% gross, -0.79% net on the measured baseline), so an estimated net
    on a trade whose fills are known is a self-inflicted blind spot."""
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))

    t = bridge.raw.find(419)
    assert 'pnl_net' in t and t['pnl_net'] < t['pnl']
    assert t['fees'].get('approx') in (False, None), (
        'the fee stamp fell back to an estimate although both fill prices '
        'were known')


def test_an_unrecovered_fill_is_not_persisted_as_a_price_of_zero(bridge):
    """`_close_spread_inner` initialises both fill variables to 0.0, and the
    ALREADY_FLAT path leaves them there when `_find_last_fill_price` finds
    nothing in the order history. Zero is "no price", not "traded at zero".

    Persisting it under `price` would be worse than persisting nothing:
    `round_trip_for_trade` stops estimating the moment it finds a price on both
    legs, so a fabricated zero on the long leg silently zeroes out the STT —
    the largest single charge in the round trip — and reports the result as a
    FULL costing rather than an approximate one."""
    data = _monitor_exit_data(0.0, 'ALREADY_FLAT_TP',
                              short_fill=0.0, long_fill=0.0)
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, data)

    t = bridge.raw.find(419)
    legs = t.get('exit_legs') or {}
    assert (legs.get('long') or {}).get('price') is None
    assert t['fees']['approx'] is True, (
        'a costing built on fills that were never found reported itself as '
        'exact')


def test_the_already_flat_recovery_path_books_too(bridge):
    """`_close_spread_inner`'s OTHER exit_data dict (`:2024`). It fires when
    both legs are already flat, prefixes the reason `ALREADY_FLAT_`, and is the
    path a crash-recovery close takes — so it must not be the one that stayed
    broken."""
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'ALREADY_FLAT_TP'))
    t = bridge.raw.find(419)
    assert t['exit_reason'] == 'already_flat_tp'
    assert t['pnl'] > 0


def test_booking_twice_is_still_refused(bridge):
    """The precondition was widened, not removed. Idempotence is the reason it
    exists: two processes both reading 'entered', both closing, and only the
    status check stopping the second from double-booking. Widening to accept
    'closing' must not have opened 'exited'."""
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))
    with pytest.raises(ValueError):
        bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))


def test_a_second_process_cannot_take_the_close_lock(bridge):
    """`begin_close` stays consume-once. False, not an exception: "somebody
    else got there first" is the normal answer and the monitor branches on it
    rather than treating it as an error."""
    assert bridge.begin_close(419, 'TP') is True
    assert bridge.begin_close(419, 'TP') is False


def test_a_close_still_books_without_a_lock(bridge):
    """`zebra close` on the CLI, and the paper engine, both book straight from
    'entered' with no lock taken. That path was the only one that ever worked
    and it must keep working."""
    bridge.update_trade_exit(419, _monitor_exit_data(40.00, 'TP'))
    assert bridge.raw.find(419)['status'] == 'exited'


def test_the_close_survives_a_process_boundary(zstore, tmp_path):
    """The lock is PERSISTED, so the two halves of the cycle are allowed to
    happen in different processes — which on the Pi they can, since the close
    lock is taken specifically so a crash between the two is visible. Re-read
    the file between the calls and the exit must still book."""
    a = ZebraStoreAdapter(zstore([dict(ENTERED)]))
    assert a.begin_close(419, 'TP') is True

    reopened = ZebraStore(config={'google_drive': {'enabled': False}})
    reopened.initialize()
    assert reopened.find(419)['status'] == 'closing'
    ZebraStoreAdapter(reopened).update_trade_exit(
        419, _monitor_exit_data(40.00, 'TP'))
    assert reopened.find(419)['pnl'] > 0


def test_a_value_beyond_the_width_is_still_clamped(bridge):
    """The bound checks are not bypassed by the bridge. A vertical cannot be
    worth more than its width; PIIND #50 booked -112.4% on a -100%-capped
    structure because nothing enforced either end."""
    bridge.begin_close(419, 'TP')
    bridge.update_trade_exit(419, _monitor_exit_data(99.0, 'TP'))
    assert bridge.raw.find(419)['exit_debit'] == pytest.approx(50.0)


# ── The key names, pinned to the live source ────────────────────────────────

def _exit_data_keys(fn):
    """Every key of every dict literal assigned to `exit_data` in `fn`."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    keys = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if 'exit_data' not in names or not isinstance(node.value, ast.Dict):
            continue
        keys |= {k.value for k in node.value.keys
                 if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return keys


def test_the_adapter_reads_the_keys_the_monitor_actually_writes():
    """A translation layer that reads a key nobody writes cannot be caught by
    testing either side alone — which is exactly how `exit_value` survived. So
    this test reads BOTH sides from source and joins them.

    It fails against the pre-fix adapter, and it will fail again the day
    somebody renames a key in `_close_spread_inner` without opening this file.
    """
    written = _exit_data_keys(sm._close_spread_inner)
    assert {'exit_spread', 'exit_reason', 'exit_spot',
            'short_fill', 'long_fill'} <= written, (
        f'the monitor no longer writes what the adapter reads: {written}')
    assert 'exit_value' not in written and 'reason' not in written

    from bcs import zebra_adapter as za
    src = inspect.getsource(za.ZebraStoreAdapter.update_trade_exit)
    read = {n.args[0].value
            for n in ast.walk(ast.parse(src.lstrip()))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == 'get'
            and n.args and isinstance(n.args[0], ast.Constant)}
    unwritten = read - written - {'exit_legs'}
    assert not unwritten, (
        f'the adapter reads exit_data keys the monitor never writes: '
        f'{sorted(unwritten)}. Each one silently becomes None on the money '
        f'path — which is how every cohort exit came to book -100%.')


# ── C3: `partial_close` is not free capital, and not invisible ──────────────

def test_a_frozen_position_still_holds_its_money(bridge):
    """`partial_close` means legs are live at the broker and a human is needed.
    Reading its rupees as FREE lets the next signal be sized against money that
    is still committed — to a position whose size nobody currently knows."""
    bridge.set_trade_status(419, 'partial_close', close_failed_leg='short')
    rupees, n, _ = capital.deployed(bridge.raw.load_trades())
    assert n == 1 and rupees == pytest.approx(13.55 * 700)


def test_a_frozen_position_is_surfaced_not_silently_dropped(bridge):
    """It leaves `get_open_trades` (correctly — no automated path may put
    orders on top of it) but it must not leave the reporting surface with it.
    An unmonitored live position nobody is told about is the failure that has
    cost real money here twice."""
    bridge.set_trade_status(419, 'partial_close', close_failed_leg='short')
    assert bridge.get_open_trades() == []
    frozen = bridge.get_frozen_trades()
    assert [t['id'] for t in frozen] == [419]
    assert frozen[0]['net_debit'] == 13.55, 'frozen records are mapped too'


def test_a_frozen_position_is_NOT_offered_to_the_recovery_sweep(bridge):
    """`monitor_all` calls `recover_closing_trade` on everything
    `get_closing_trades` returns and announces "Recovered ... Re-monitoring".
    The store refuses that transition for a frozen record, so putting it in
    that list would produce a daily Telegram claiming a recovery that never
    happened."""
    bridge.set_trade_status(419, 'partial_close')
    assert bridge.get_closing_trades() == []
    assert bridge.recover_closing_trade(419) is False


def test_a_legacy_frozen_record_is_not_claimed_by_the_cohort(zstore):
    """Same boundary as every other read here: 450 back-ratio records share
    this file and the order path must never see one."""
    legacy = dict(ENTERED, id=7, status='partial_close')
    legacy.pop('cohort')
    a = ZebraStoreAdapter(zstore([legacy]))
    assert a.get_frozen_trades() == []
