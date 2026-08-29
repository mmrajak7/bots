"""One signal, one position, ONE price for it.

WAAREEENER #449, 2026-08-27, from the Pi. A single Telegram said both of
these about the same trade:

    lot 175 | Capital (1 lot) = 6,361
    Capital says DO NOT ENTER: one position at Rs 121700 exceeds the
    per-trade cap Rs 25000 (12.5% of Rs 200000 capital)

Both numbers were computed correctly, from two different structures. The
ticket priced the BCS that entered (debit 36.35 x 175 = Rs 6,361). The sizing
line priced `analysis['best']` — the ZEBRA BACK RATIO, retired 2026-08-12 and
never opened since: 2 x mid(3000 PE) - mid(2600 PE) = 695.43 x 175 =
Rs 121,700. The log recorded a THIRD, different refusal ("8 positions already
open") because `capital.check` ran twice on two different candidate dicts.

Two defects, both pinned here:

  1. the capital gate must price the structure that is alerted and entered;
  2. a signal the capital gate refuses must not render as an order ticket —
     #449 shipped click-copy symbols, a debit and "Vetted by Claude", then
     said DO NOT ENTER underneath, and it had spent a Claude entry vet
     (decision #92) on a book that a store read shows was full.

Run:  cd Helper && python -m pytest zebra/tests/test_capital_alert_coherence.py -v
"""
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra import vet as vet_mod           # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
SPOT = 96.5

#: The zebra back ratio. Rs 500/lot -- deliberately CHEAPER than the BCS, so
#: pricing the wrong one is a permissive error, which is the direction that
#: costs money and the one a test must be able to see.
BEST = {'k_l': 90.0, 'k_s': 100.0, 'debit': 5.0, 'lot_size': 100,
        'long_symbol': 'TESTCO26SEP90CE', 'short_symbol': 'TESTCO26SEP100CE',
        'short_extrinsic': 1.0, 'short_mid': 2.0, 'short_bid': 1.9,
        'short_ask': 2.1, 'short_oi': 9000, 'long_oi': 9000,
        'be': 95.0, 'be_pct_from_spot': -1.5, 'capital_per_lot': 500,
        'gate_fails': []}
ANALYSIS = {'spot': SPOT, 'expiry': '2026-09-30', 'dte': 30, 'lot_size': 100,
            'best': BEST, 'candidates': [], 'atm_strike': 100.0,
            'atm_quote': {'bid': 1.9, 'ask': 2.1, 'mid': 2.0, 'oi': 9000}}

#: The vertical that actually opens. Rs 1,000/lot -- 2x the back ratio.
BCS = {'long_strike': 100.0, 'short_strike': 140.0, 'width': 40.0,
       'long_symbol': 'TESTCO26SEP100CE', 'short_symbol': 'TESTCO26SEP140CE',
       'debit': 10.0, 'lot_size': 100, 'debit_to_width_pct': 25.0,
       'short_extrinsic': 1.0, 'max_profit_per_share': 30.0, 'warnings': [],
       'long_mid': 12.0, 'long_bid': 11.9, 'long_ask': 12.1,
       'short_mid': 2.0, 'short_bid': 1.9, 'short_ask': 2.1,
       'debit_mid': 10.0, 'entry_cost': 0.2, 'entry_cost_pct': 0.7,
       'debit_to_width_pct_mid': 25.0, 'pricing_basis': 'fill',
       'long_ask_qty': 5000, 'short_bid_qty': 5000}

BCS_RUPEES = BCS['debit'] * BCS['lot_size']         # 1,000
BEST_RUPEES = BEST['debit'] * BEST['lot_size']      # 500


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': SPOT})
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: dict(ANALYSIS))
    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs',
                        lambda *a, **k: dict(BCS))
    store = ZebraStore(config={})
    store._load_local()
    store.add_signal(dict(SIGNAL))
    return store


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: out.append(msg) or True)
    return out


def cycle(store):
    monitor.check_watching(store, kite=None, dry_run=True)
    return store.load_trades()


def rupees(text, label):
    """The rupee figure following `label` in a Telegram message."""
    m = re.search(re.escape(label) + r'[^0-9]*([0-9][0-9,]*)', text)
    assert m, f"{label!r} not found in:\n{text}"
    return float(m.group(1).replace(',', ''))


# ── DEFECT 1: the gate must price the structure that trades ──────────────

def test_capital_prices_the_bcs_not_the_retired_back_ratio(wired):
    """The whole of #449 in one assertion.

    `analysis['best']` is the back ratio. Nothing has opened one since
    2026-08-12, so its debit must not reach `capital` at all -- and here it
    would be the CHEAPER of the two, i.e. an error that lets positions
    through.
    """
    trade = wired.find(1)
    ctx = monitor._capital_context(wired, trade, dict(ANALYSIS), dict(BCS))
    assert ctx['priced']['debit'] == BCS['debit'], \
        "the capital layer priced a structure that is never opened"
    assert ctx['plan']['capital'] == BCS_RUPEES
    assert ctx['plan']['capital'] != BEST_RUPEES


def test_the_gate_refuses_the_bcs_a_back_ratio_price_would_have_allowed(wired,
                                                                       monkeypatch):
    """Rs 750 per-trade cap: the back ratio (Rs 500) fits, the BCS (Rs 1,000)
    does not. Pricing the wrong structure is not a cosmetic mislabel -- it
    decides entries, in the permissive direction."""
    monkeypatch.setattr(cfg, 'CAPITAL_RUPEES', 6000.0)   # 12.5% = Rs 750
    trade = wired.find(1)
    pl = monitor._capital_context(wired, trade, dict(ANALYSIS), dict(BCS))['plan']
    assert pl['lots'] == 0, "the BCS was sized off the back ratio's debit"
    assert 'per-trade cap' in pl['reason']


def test_the_ticket_and_the_sizing_line_quote_one_number(wired, sent):
    """The operator-visible defect: one message, two prices for one trade."""
    cycle(wired)
    assert len(sent) == 1, "one signal, one alert"
    msg = sent[0]
    assert rupees(msg, 'Capital (1 lot) =') == BCS_RUPEES
    assert rupees(msg, 'lot(s)</b> = Rs') == BCS_RUPEES
    # PAPER books the position before the alert renders, so a size line that
    # re-ran `capital.plan` here counted the signal's own position against
    # `max_open_per_stock: 1` and refused every paper ticket it printed.
    assert 'DO NOT ENTER' not in msg


def test_the_vet_is_handed_the_pair_it_is_asked_to_judge(wired):
    """The agent used to get the ATM book and 'the short leg is chosen
    later', so it re-quoted the spread itself to argue with a capital figure
    that belonged to a different structure (decision #92)."""
    trade = wired.find(1)
    ctx = monitor._vet_context(wired, trade, dict(ANALYSIS), 4.0, None,
                               dict(BCS))
    assert ctx['bcs']['short_strike'] == BCS['short_strike']
    assert ctx['bcs']['debit'] == BCS['debit']
    assert ctx['capital']['plan']['capital'] == BCS_RUPEES
    # The back-ratio block is GONE (decommissioned 2026-08-27), not merely
    # captioned. #92 quoted its `liquidity_ok: false` and `gate_fails` as facts
    # about the trade being vetted and had to work out unaided that they
    # described a different structure. A caption saying 'ignore this' is not
    # as good as not sending it.
    assert 'zebra' not in ctx, \
        'the retired back-ratio pair is back in the vet handoff'
    assert 'gates_all_passed' not in ctx, \
        'an all-clear derived from an empty back-ratio pick is back'


# ── DEFECT 2: a refused signal is not an order ticket ────────────────────

def _fill_the_book(store, monkeypatch, n=1):
    """Occupy every slot, the way #449 found the book."""
    monkeypatch.setattr(cfg, 'MAX_OPEN_TRADES', n)
    for i in range(n):
        store.add_signal(dict(SIGNAL, stock=f'FULL{i}'))
        tid = store.load_trades()[-1]['id']
        store.mark_triggered(tid, 100.0, 3.5, [])
        store.mark_entered(tid, {
            'long_strike': 90.0, 'short_strike': 100.0,
            'long_symbol': 'A', 'short_symbol': 'B', 'debit': 5.0,
            'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30'})


def test_a_refused_signal_renders_no_order_ticket(wired, sent, monkeypatch):
    """#449's message carried BUY/SELL lines with click-copy symbols and a
    fillable debit, and then said DO NOT ENTER. Whichever half the reader
    acted on, the message was wrong.

    **LIVE**, since 2026-08-29. The rule is about an ORDER TICKET, and in
    paper there is no order — the position is already open by the time the
    alert renders, so a DO-NOT-ENTER line there is #449's contradiction
    arrived at from the other side. See the paper twin below.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    assert len(sent) == 1, "one signal, one alert"
    msg = sent[0]
    assert 'ENTER BCS' not in msg
    assert 'BUY' not in msg and 'SELL' not in msg
    assert BCS['long_symbol'] not in msg, "click-copy symbols on a refusal"
    assert 'NO ENTRY' in msg
    assert 'cap is 1 (max_open_trades)' in msg


def test_paper_still_enters_a_refused_signal(wired, sent, monkeypatch):
    """The refusal is a SHADOW in paper and must stay one: capping paper
    entries biases which trades the validation record contains."""
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    mine = [t for t in wired.load_trades() if t['stock'] == 'TESTCO']
    assert len(mine) == 1 and mine[0]['status'] == 'entered', \
        "the capital shadow became a block"
    assert mine[0]['capital'] == BCS_RUPEES


def test_a_refused_signal_spends_no_vet(wired, sent, monkeypatch):
    """A store read decides what a Claude spawn was being asked to decide.
    #449 burned entry decision #92 on a book that was already full.

    **LIVE**, since 2026-08-29: there the refusal binds, so no entry follows
    and the vet would decide nothing. In PAPER the entry happens regardless,
    and an unvetted entry is a hole in the evidence the paper run exists to
    produce — see the twin below.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    asked = []
    monkeypatch.setattr(vet_mod, 'request_entry_vet',
                        lambda *a, **k: asked.append(a) or None)
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    assert asked == [], "a Claude vet was spent on an unfundable signal"


def test_PAPER_still_vets_a_signal_the_live_cap_would_refuse(wired, sent,
                                                             monkeypatch):
    """The twin, and the regression that made it necessary.

    `max_open_trades` 8 -> 4 (M9, a LIVE decision) against a book holding 6
    open PAPER positions refused every new signal in the pre-gate — which
    skipped the vet — while the store's paper exemption entered the record
    anyway. Paper trades with NO VERDICT, and the vetting pipeline THE GOAL
    exists to validate going dark, from a change documented as live-only.
    """
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    asked = []
    monkeypatch.setattr(vet_mod, 'request_entry_vet',
                        lambda *a, **k: asked.append(a) or None)
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    assert asked, ("paper entered without asking for a verdict — the "
                   "validation record now has a hole in it")


def test_the_PAPER_ticket_says_a_live_book_would_have_refused(wired, sent,
                                                              monkeypatch):
    """Vetting it is not the same as pretending the cap did not speak. The
    alert must not render `_size_line`'s DO-NOT-ENTER over a position that is
    already open, and must not silently omit the refusal either."""
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    assert len(sent) == 1, "one signal, one alert"
    msg = sent[0]
    assert 'PAPER entry' in msg
    assert 'would have REFUSED' in msg
    assert 'cap is 1 (max_open_trades)' in msg
    assert 'DO NOT ENTER' not in msg, (
        "a refusal rendered over an entry that already happened — #449's "
        "contradiction from the other side")


def test_a_pending_vet_is_still_honoured_when_capital_refuses(wired, sent,
                                                              monkeypatch):
    """The pre-gate skips the vet only when none is in flight. Entering behind
    a PENDING verdict would let a VETO land on a position already open."""
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    _fill_the_book(wired, monkeypatch)
    wired.mark_triggered(1, SPOT, 3.5, [])
    wired.find(1)['vet'] = {'state': vet_mod.PENDING,
                            'deadline': '2099-01-01T00:00:00'}
    cycle(wired)
    assert wired.find(1)['status'] == 'triggered', \
        "entered behind a verdict that was still being decided"
    assert sent == []


def test_a_fundable_signal_still_gets_its_vet(wired, monkeypatch):
    """The negative control: the capital pre-gate must not become a silent
    vetting kill switch."""
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    asked = []
    monkeypatch.setattr(vet_mod, 'request_entry_vet',
                        lambda *a, **k: asked.append(a) or None)
    cycle(wired)
    assert len(asked) == 1


def test_the_refusal_does_not_eat_the_live_order_ticket(wired, sent,
                                                        monkeypatch):
    """LIVE dedups the ticket with a consume-once `vet_enter` claim. If the
    refusal notice spent that claim, the real ticket an hour later -- once a
    position closed and a slot freed -- would be dropped as 'already sent'."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    _fill_the_book(wired, monkeypatch, n=1)
    cycle(wired)
    assert 'NO ENTRY' in sent[0]
    assert not wired.find(1).get('vet_enter_alerted_at'), \
        "the refusal consumed the order ticket's claim"

    # A slot frees. The ticket must now go out.
    monkeypatch.setattr(cfg, 'MAX_OPEN_TRADES', 9)
    cycle(wired)
    assert len(sent) == 2 and 'ENTER BCS' in sent[1]


def test_the_refusal_is_announced_once(wired, sent, monkeypatch):
    """Paper books the position in the same cycle, so a second notice would
    describe a trade that is already open."""
    _fill_the_book(wired, monkeypatch)
    cycle(wired)
    cycle(wired)
    assert len(sent) == 1
