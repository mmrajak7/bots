"""Auto-entry wired into the LIVE path (Phase 3).

`bcs/entry_executor.py` has its own tests for how orders are placed. This file
tests the DECISION around them: when the engine may open a position at all,
what it records afterwards, and what it does when the orders and the record
disagree.

The through-line: every refusal falls back to the TICKET, which is what LIVE
did before auto-entry existed. The worst case of a broken auto-entry is
therefore the old behaviour, not a new failure — with one exception that gets
its own tests, the case where orders FILLED and the record did not happen.

Run:  cd Helper && python -m pytest zebra/tests/test_auto_entry.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import entry_executor as ee      # noqa: E402
from zebra import capital                 # noqa: E402
from zebra import config as cfg           # noqa: E402
from zebra import monitor                 # noqa: E402
from zebra.trade_store import ZebraStore  # noqa: E402

LOT = 100
BCS = {'long_symbol': 'TESTCO26SEP1000CE', 'short_symbol': 'TESTCO26SEP1040CE',
       'long_strike': 1000.0, 'short_strike': 1040.0, 'debit': 20.0,
       'lot_size': LOT, 'expiry': '2026-09-29', 'entry_spot': 1000.0,
       'short_extrinsic': 5.0, 'long_ask_qty': 5000, 'short_bid_qty': 5000,
       'width': 40.0, 'debit_to_width_pct': 50.0}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)          # LIVE
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 1040.0, 'st_direction': 'UP',
                  'signal_price': 1000.0, 'signal_gap_pct': 4.0})
    return s


@pytest.fixture
def armed(monkeypatch):
    """Auto-entry ON and the kill switch armed — the only state that trades."""
    from bcs import spread_monitor as sm
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: True)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: out.append(m) or True)
    return out


def fake_exec(monkeypatch, *, lots_filled, long_fills=None, short_fills=None,
              orphan=None, raises=None):
    calls = []

    def _open(kite, **kw):
        calls.append(kw)
        if raises:
            raise raises
        n = lots_filled
        return {'stock': kw['stock'], 'lots_requested': kw['lots'],
                'lots_filled': n,
                'long_fills': long_fills if long_fills is not None
                else [30.0] * n,
                'short_fills': short_fills if short_fills is not None
                else [10.0] * n,
                'orphan': orphan, 'problems': []}
    monkeypatch.setattr(ee, 'open_spread', _open)
    return calls


def enter(store, trade_id=1, kite=None):
    return monitor._auto_enter_bcs(store, kite, store.find(trade_id),
                                   dict(BCS), dry_run=False)


# ── the gate ────────────────────────────────────────────────────────────────

def test_with_auto_entry_off_nothing_is_placed_and_the_ticket_stands(
        store, monkeypatch, sent):
    """The default, and the fall-back for every refusal below. LIVE behaved
    exactly this way before auto-entry existed."""
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', False)
    calls = fake_exec(monkeypatch, lots_filled=1)
    assert enter(store) is None
    assert calls == [], 'the executor ran with auto-entry off'
    assert store.find(1)['status'] == 'watching', (
        'a refused entry moved the signal')


def test_the_kill_switch_also_stops_auto_entry(store, monkeypatch, sent):
    from bcs import spread_monitor as sm
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', True)
    monkeypatch.setattr(sm, 'trading_enabled', lambda: False)
    calls = fake_exec(monkeypatch, lots_filled=1)
    assert enter(store) is None
    assert calls == []


def test_the_refusal_is_logged_not_silent(store, monkeypatch, sent, caplog):
    """A gate returning False in silence is indistinguishable from a signal
    that never arrived."""
    import logging
    monkeypatch.setattr(cfg, 'AUTO_ENTRY', False)
    fake_exec(monkeypatch, lots_filled=1)
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        enter(store)
    assert any('AUTO-ENTRY off' in r.message for r in caplog.records)


# ── capital decides the size ────────────────────────────────────────────────

def test_capital_decides_how_many_lots_are_placed(store, monkeypatch, armed,
                                                  sent):
    monkeypatch.setattr(cfg, 'CAPITAL_RUPEES', 600000.0)   # 3 lots
    monkeypatch.setattr(cfg, 'CAPITAL_PER_LOT', 200000.0)
    calls = fake_exec(monkeypatch, lots_filled=3)
    enter(store)
    assert calls[0]['lots'] == 3


def test_a_thin_book_sizes_the_order_down(store, monkeypatch, armed, sent):
    """The owner's case: the budget allows more than the touch can absorb."""
    monkeypatch.setattr(cfg, 'CAPITAL_RUPEES', 600000.0)
    monkeypatch.setattr(cfg, 'CAPITAL_PER_LOT', 200000.0)
    calls = fake_exec(monkeypatch, lots_filled=2)
    thin = dict(BCS, long_ask_qty=200, short_bid_qty=250)   # 2 lots at 100
    monitor._auto_enter_bcs(store, None, store.find(1), thin, dry_run=False)
    assert calls[0]['lots'] == 2


def test_a_capital_refusal_places_nothing(store, monkeypatch, armed, sent):
    """Zero lots is a refusal, not a smaller trade. One lot of this spread
    costs Rs 2,000; a Rs 8,000 book allows Rs 1,000 per trade."""
    monkeypatch.setattr(cfg, 'CAPITAL_RUPEES', 8000.0)
    calls = fake_exec(monkeypatch, lots_filled=1)
    assert enter(store) is None
    assert calls == [], 'orders went out against a refusing budget'
    assert store.find(1)['status'] == 'watching'


# ── what gets recorded ──────────────────────────────────────────────────────

def test_it_records_the_debit_PAID_not_the_debit_quoted(store, monkeypatch,
                                                        armed, sent):
    """Every stop and the trail derive from the entry debit. Recording the
    quote instead of the fill puts every level under the position."""
    fake_exec(monkeypatch, lots_filled=1, long_fills=[31.0],
              short_fills=[9.0])                 # paid 22.0, quoted 20.0
    fresh = enter(store)
    assert fresh['debit'] == 22.0


def test_it_records_the_lots_FILLED_not_the_lots_asked_for(store, monkeypatch,
                                                           armed, sent):
    monkeypatch.setattr(cfg, 'CAPITAL_RUPEES', 600000.0)
    monkeypatch.setattr(cfg, 'CAPITAL_PER_LOT', 200000.0)
    fake_exec(monkeypatch, lots_filled=2, long_fills=[30.0, 30.0],
              short_fills=[10.0, 10.0])          # asked 3, got 2
    fresh = enter(store)
    assert fresh['lots'] == 2
    assert fresh['quantity'] == 2 * LOT


def test_nothing_filled_records_nothing_and_leaves_the_signal(store,
                                                              monkeypatch,
                                                              armed, sent):
    fake_exec(monkeypatch, lots_filled=0, long_fills=[], short_fills=[])
    assert enter(store) is None
    assert sent == [], (
        'a no-fill raised an alarm. Nothing was established and nothing is '
        'held, so the ticket simply stands — shouting here is a false alarm '
        'and it makes the REAL "filled but unmanaged" alert cheaper')
    assert store.find(1)['status'] == 'watching', (
        'a refused entry moved the signal')


# ── filled but not recorded: the only case worse than not trading ───────────

def test_filled_with_an_uncomputable_debit_is_NOT_recorded_but_IS_shouted(
        store, monkeypatch, armed, sent):
    """A record with no debit is a position with no stop levels at all. Better
    an unmanaged position the owner has been told about than a managed-looking
    one whose levels are fiction."""
    fake_exec(monkeypatch, lots_filled=1, long_fills=[None],
              short_fills=[10.0])
    assert enter(store) is None
    assert store.find(1)['status'] == 'watching', (
        'a refused entry moved the signal')
    assert sent and 'UNMANAGED' in sent[0]
    # The SPECIFIC branch, not just "something went wrong". Without the guard
    # the debit reaches the store as None, the store raises, and the generic
    # handler one branch down sends a message about the STORE refusing --
    # which is a different fault with a different fix, reported as this one.
    assert 'debit could not be computed' in sent[0], (
        'the uncomputable-debit refusal was replaced by a store exception '
        'wearing its clothes: %r' % (sent[0],))


def test_a_store_failure_after_a_fill_is_shouted(store, monkeypatch, armed,
                                                 sent):
    """Orders filled, the record did not happen. Silence here is a live
    position nothing is watching."""
    fake_exec(monkeypatch, lots_filled=1)

    def boom(*a, **k):
        raise RuntimeError('lock timeout')
    monkeypatch.setattr(store, 'mark_entered_bcs', boom)
    assert enter(store) is None
    assert sent and 'UNMANAGED' in sent[0]
    assert 'trade store refused' in sent[0]


def test_an_executor_crash_falls_back_to_the_ticket(store, monkeypatch, armed,
                                                    sent):
    fake_exec(monkeypatch, lots_filled=0, raises=RuntimeError('kite is down'))
    assert enter(store) is None
    assert store.find(1)['status'] == 'watching', (
        'a refused entry moved the signal')


# ── verification against the broker ─────────────────────────────────────────

class _Kite:
    def __init__(self, net):
        self._net = net

    def positions(self):
        return {'net': self._net}


def _net(long_qty, short_qty):
    return [{'tradingsymbol': BCS['long_symbol'], 'quantity': long_qty},
            {'tradingsymbol': BCS['short_symbol'], 'quantity': short_qty}]


def test_a_matching_position_verifies_quietly(store, monkeypatch, armed, sent):
    fake_exec(monkeypatch, lots_filled=1)
    enter(store, kite=_Kite(_net(LOT, -LOT)))
    assert sent == [], 'a clean entry nagged the owner'


def test_a_mismatch_is_telegrammed(store, monkeypatch, armed, sent):
    """The code that placed the orders is exactly the code that cannot be
    trusted to say what it placed."""
    fake_exec(monkeypatch, lots_filled=1)
    enter(store, kite=_Kite(_net(LOT, -LOT // 2)))
    assert sent and 'NOT verified' in sent[0]


def test_an_unreadable_broker_is_reported_not_assumed_fine(store, monkeypatch,
                                                           armed, sent):
    class _Dead:
        def positions(self):
            raise RuntimeError('token expired')
    fake_exec(monkeypatch, lots_filled=1)
    enter(store, kite=_Dead())
    assert sent and 'NOT verified' in sent[0]


def test_verification_never_costs_the_record(store, monkeypatch, armed, sent):
    """The trade IS recorded either way. Verification decides whether the
    owner is told to go and look, never whether the position exists."""
    fake_exec(monkeypatch, lots_filled=1)
    fresh = enter(store, kite=_Kite(_net(LOT, -LOT // 2)))
    assert fresh is not None and store.find(1)['status'] == 'entered'


# ── paper is untouched ──────────────────────────────────────────────────────

def test_paper_mode_never_reaches_the_live_entry_path(store, monkeypatch,
                                                      armed, sent):
    """`_auto_enter_bcs` sits behind `if not cfg.PAPER_MODE`. Paper books its
    own fill and must not place orders however the switches are set."""
    import inspect
    src = inspect.getsource(monitor._enter_as_bcs)
    i = src.index('_auto_enter_bcs')
    assert 'if not cfg.PAPER_MODE:' in src[:i], (
        'the live auto-entry call is no longer guarded by paper mode')
