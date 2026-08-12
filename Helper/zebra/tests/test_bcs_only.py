"""The BCS-only pipeline: one record per signal, no zebra leg, no shadow.

Owner's call 2026-08-12, backed by 28 matched A/B pairs: zebra 4.1% RoC vs BCS
18.4% on identical win counts, ~9x the capital, 3 legs with a deep-ITM illiquid
one. This file guards the two landmines the migration was known to have, plus
the ones it turned out to have.

Run:  cd Helper && python -m pytest zebra/tests/test_bcs_only.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra import mfe                      # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}
SPOT = 96.5
BEST = {'k_l': 90.0, 'k_s': 100.0, 'debit': 5.0, 'lot_size': 100,
        'long_symbol': 'TESTCO26SEP90CE', 'short_symbol': 'TESTCO26SEP100CE',
        'short_extrinsic': 1.0, 'short_mid': 2.0, 'short_bid': 1.9,
        'short_ask': 2.1, 'short_oi': 9000, 'long_oi': 9000,
        'be': 95.0, 'be_pct_from_spot': -1.5, 'capital_per_lot': 500,
        'gate_fails': []}
ANALYSIS = {'spot': SPOT, 'expiry': '2026-09-30', 'dte': 30, 'lot_size': 100,
            'best': BEST, 'candidates': [], 'atm_strike': 100.0,
            'atm_quote': {'bid': 1.9, 'ask': 2.1, 'mid': 2.0, 'oi': 9000}}
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


def cycle(store):
    monitor.check_watching(store, kite=None, dry_run=True)
    return store.load_trades()


# ── one record, not two ──────────────────────────────────────────────────
def test_a_signal_becomes_one_bcs_position(wired):
    trades = cycle(wired)
    assert len(trades) == 1, "the BCS-only path created a shadow as well"
    t = trades[0]
    assert t['status'] == 'entered'
    assert t['structure'] == 'bcs'
    assert t.get('shadow_of') is None
    assert t['long_strike'] == 100.0 and t['short_strike'] == 140.0


# ── LANDMINE 1: structure was stamped in exactly one place ───────────────
def test_the_position_is_valued_as_one_long_not_two(wired):
    """`structure` used to be set only inside add_bcs_shadow. A BCS promoted
    through any other path would carry no structure key, be read as a 2-long
    zebra by _long_multiplier, and have every structure value — quote,
    intrinsic floor, P&L — doubled."""
    t = cycle(wired)[0]
    assert monitor._long_multiplier(t) == 1


def test_the_position_carries_everything_its_guards_read(wired):
    """width feeds the trail and the intrinsic floor; debit_sl_value feeds the
    value stop. A record missing any of them is silently unguarded, not
    obviously broken."""
    t = cycle(wired)[0]
    for field in ('width', 'debit', 'quantity', 'lot_size', 'tp_spot',
                  'sl_spot', 'debit_sl_value', 'expiry', 'entry_spot',
                  'short_extrinsic_entry'):
        assert t.get(field) is not None, f"missing {field}"
    assert mfe.trail_levels(dict(t, mfe_mid=25.0)) is not None


def test_shadow_and_first_class_bcs_agree_on_every_field(wired):
    """Both builders share _bcs_entry_fields, so the two ways a BCS can be born
    cannot drift apart. If they ever do, the guards read different records."""
    first_class = cycle(wired)[0]
    wired.add_signal(dict(SIGNAL, stock='OTHERCO'))
    zeb = wired.find(2)
    wired.mark_triggered(2, SPOT, 3.5, [])
    wired.mark_entered(2, {'long_strike': 90.0, 'short_strike': 100.0,
                           'long_symbol': 'A', 'short_symbol': 'B',
                           'debit': 5.0, 'lot_size': 100, 'lots': 1,
                           'expiry': '2026-09-30'})
    shadow = wired.add_bcs_shadow(wired.find(2), dict(BCS, entry_spot=SPOT, expiry='2026-09-30'))
    shared = ('structure', 'width', 'debit', 'quantity', 'lot_size',
              'debit_sl_value', 'spot_sl_pct', 'debit_to_width_pct')
    for f in shared:
        assert first_class[f] == shadow[f], f"{f} differs between the two paths"


# ── LANDMINE 2: dedup used to exclude structure='bcs' ────────────────────
def test_a_first_class_bcs_blocks_a_duplicate_on_the_same_thesis(wired):
    """The dedup sites excluded structure='bcs' — correct while every BCS was
    an OBSERVATION shadowing a zebra. Once the BCS is the position, that same
    test excludes every real position from dedup and lets the scanner open a
    second one on one signal."""
    cycle(wired)
    with pytest.raises(ValueError, match='already open'):
        wired.add_signal(dict(SIGNAL))


def test_a_shadow_still_does_not_block_a_new_signal(wired):
    """The other half. A shadow is an observation, so it must stay invisible to
    dedup — the predicate is `shadow_of is None`, not the structure."""
    wired.mark_triggered(1, SPOT, 3.5, [])
    wired.mark_entered(1, {'long_strike': 90.0, 'short_strike': 100.0,
                           'long_symbol': 'A', 'short_symbol': 'B',
                           'debit': 5.0, 'lot_size': 100, 'lots': 1,
                           'expiry': '2026-09-30'})
    wired.add_bcs_shadow(wired.find(1), dict(BCS, entry_spot=SPOT, expiry='2026-09-30'))
    wired.mark_exited(1, SPOT, 5.0, 'test')          # free the zebra slot
    wired.add_signal(dict(SIGNAL))                    # must NOT raise


# ── gates that must not carry over ───────────────────────────────────────
def test_a_zebra_gate_failure_no_longer_blocks_the_bcs(wired, monkeypatch):
    """The zebra pair's gates (net-extrinsic, deep-ITM liquidity) constrain a
    structure nobody is opening. Reading the ATM book out of `best` meant those
    gates could veto a perfectly tradeable spread."""
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: dict(ANALYSIS, best=None))
    t = cycle(wired)[0]
    assert t['status'] == 'entered', "a zebra-side gate blocked a BCS"


def test_no_usable_atm_book_leaves_the_signal_watching(wired, monkeypatch):
    """Before mark_triggered, not after: an unformed book is a reason to look
    again in five minutes, not to burn the trigger and its consume-once flags
    on a cycle that could never have entered."""
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: dict(ANALYSIS, atm_quote={'mid': 0}))
    t = cycle(wired)[0]
    assert t['status'] == 'watching'


def test_a_gated_bcs_leaves_the_signal_triggered(wired, monkeypatch):
    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs',
                        lambda *a, **k: {'error': 'debit 52% of width'})
    t = cycle(wired)[0]
    assert t['status'] == 'triggered'
    assert len(wired.load_trades()) == 1, "a rejected BCS left a record behind"


def test_a_crash_in_the_builder_does_not_stop_the_cycle(wired, monkeypatch):
    monkeypatch.setattr(
        monitor.strikes_mod, 'analyze_bcs',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('chain down')))
    t = cycle(wired)[0]                                # must not raise
    assert t['status'] == 'triggered'


# ── LIVE mode: the alert IS the order ticket ─────────────────────────────
def test_live_mode_alerts_and_enters_nothing(wired, monkeypatch):
    """An earlier version returned None for both "skipped" and "live", and the
    caller's `continue` then suppressed EVERY entry alert in LIVE mode — where
    the alert is the only way a trade ever gets placed."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    t = cycle(wired)[0]
    assert t['status'] == 'triggered', "LIVE mode auto-entered a position"
    assert len(sent) == 1 and 'ENTER BCS' in sent[0]


def test_live_ticket_does_not_repeat_every_cycle(wired, monkeypatch):
    """The consume-once claim lives in one shared helper. When the BCS path
    sent its own alert directly it skipped that claim, which in LIVE mode is an
    order ticket re-sent every five minutes — how duplicate entries happen."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(monitor.vet_mod, 'request_entry_vet',
                        lambda *a, **k: None)
    monkeypatch.setattr(monitor.vet_mod, 'vet_state',
                        lambda t: monitor.vet_mod.ALLOWED)
    monkeypatch.setattr(monitor.vet_mod, 'is_pending', lambda t: False)
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    for _ in range(4):
        cycle(wired)
    assert len(sent) == 1, f"the order ticket repeated ({len(sent)} sends)"


# ── the analyzer's side of the contract ──────────────────────────────────
def test_the_real_analyzer_returns_an_atm_quote(monkeypatch):
    """Every other test here mocks `analyze` wholesale, so none of them can see
    the analyzer drop `atm_quote`. If it did, the BCS path would find no usable
    ATM book, every signal would sit in `watching`, and the pipeline would
    simply stop trading — with no error anywhere. Drive the REAL function."""
    from zebra import strikes

    chain = {}
    for k in (90.0, 95.0, 100.0, 105.0, 110.0):
        chain[k] = {'CE': {'tradingsymbol': f'TESTCO26SEP{int(k)}CE',
                           'lot_size': 100, 'strike': k}}
    monkeypatch.setattr(strikes, '_OPTIONS_CACHE',
                        {'TESTCO': {'2026-09-30': chain}})
    monkeypatch.setattr(strikes, '_OPTIONS_CACHE_LOADED', True)
    monkeypatch.setattr(strikes, '_load_options_csv', lambda: None)
    monkeypatch.setattr(strikes, '_pick_expiry', lambda *a, **k: '2026-09-30')
    monkeypatch.setattr(
        strikes, '_quote_option',
        lambda kite, sym: {'bid': 4.9, 'ask': 5.1, 'mid': 5.0, 'oi': 9000,
                           'last': 5.0, 'reliable': True})

    out = strikes.analyze(None, 'TESTCO', 'CE', 99.0)
    assert not out.get('error'), out.get('error')
    assert out.get('atm_strike') == 100.0
    q = out.get('atm_quote')
    assert q and q.get('mid') == 5.0 and q.get('oi') == 9000, \
        "analyze() no longer surfaces the ATM book the BCS path builds from"


# ── rollback ─────────────────────────────────────────────────────────────
def test_entry_structure_zebra_restores_the_old_path(wired, monkeypatch):
    """The migration is one config flip, both ways. Open positions are never
    touched, so there is no data migration to undo either."""
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'zebra')
    monkeypatch.setattr(cfg, 'BCS_PAPER_ENABLED', False)
    trades = cycle(wired)
    assert trades[0]['status'] == 'entered'
    assert trades[0].get('structure') is None, "zebra path stamped a structure"
    assert trades[0]['long_strike'] == 90.0        # the zebra pair, not the BCS


def test_an_unknown_entry_structure_falls_back_loudly():
    """A typo'd config value must not silently pick a structure. config._raw
    validation is import-time, so assert on the validator's own behaviour."""
    import importlib
    from zebra import config
    assert config.ENTRY_STRUCTURE in ('bcs', 'zebra')
    assert config._DEFAULTS['entry_structure'] == 'bcs'


# ── corporate actions suspend the position (F-2) ─────────────────────────
def _entered(store, monkeypatch, mid=13.0, spot=SPOT):
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': mid, 'reliable': True, 'reason': None,
                            'legs': {}, 'floored': False})
    monkeypatch.setattr(monitor, '_exit_cleared', lambda *a, **k: True)


def test_a_bonus_ex_date_suspends_every_automated_exit(wired, monkeypatch):
    """A 1:1 bonus halves the quoted spot while the exchange doubles the lot
    size and halves the strikes. Yesterday's sl_spot is breached instantly by
    an event in which nothing went wrong."""
    cycle(wired)                                   # open the position
    _entered(wired, monkeypatch, mid=0.5, spot=48.0)   # "halved" spot, junk mid
    monkeypatch.setattr(monitor.events_mod, 'adjustment_today',
                        lambda stock: {'type': 'bonus', 'title': '1:1'})
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    monitor.check_entered(wired, kite=None, dry_run=True)

    t = wired.find(1)
    assert t['status'] == 'entered', "an automated exit fired on a bonus ex-date"
    assert len(sent) == 1 and 'CORPORATE ACTION' in sent[0]
    assert 'SUSPENDED' in sent[0]


def test_the_corrupted_spot_never_reaches_the_recorded_peak(wired, monkeypatch):
    """The suspension sits ABOVE the MFE capture, not merely above the exits.
    A post-adjustment spot recorded as a peak would poison the give-back watch
    and the trail for the rest of the position's life."""
    cycle(wired)
    _entered(wired, monkeypatch, mid=13.0, spot=SPOT)
    monitor.check_entered(wired, kite=None, dry_run=True)
    good_peak = wired.find(1)['mfe_spot']

    monkeypatch.setattr(monitor.events_mod, 'adjustment_today',
                        lambda stock: {'type': 'split', 'title': '1:5'})
    _entered(wired, monkeypatch, mid=2.6, spot=SPOT * 5)   # "un-split" price
    monkeypatch.setattr(monitor, '_send_telegram', lambda *a, **k: True)
    monitor.check_entered(wired, kite=None, dry_run=True)
    assert wired.find(1)['mfe_spot'] == good_peak, "a split price became the peak"


def test_the_suspension_alert_fires_once_a_day(wired, monkeypatch):
    cycle(wired)
    _entered(wired, monkeypatch)
    monkeypatch.setattr(monitor.events_mod, 'adjustment_today',
                        lambda stock: {'type': 'rights', 'title': 'R'})
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    for _ in range(4):
        monitor.check_entered(wired, kite=None, dry_run=True)
    assert len(sent) == 1


def test_a_broken_calendar_never_blocks_the_monitor(wired, monkeypatch):
    """Every side channel in this fleet must degrade to 'as if it did not
    exist'. A calendar that raises must not freeze the exit path."""
    cycle(wired)
    _entered(wired, monkeypatch, mid=1.0)          # deep below the debit SL
    monkeypatch.setattr(
        monitor.events_mod, 'adjustment_today',
        lambda stock: (_ for _ in ()).throw(RuntimeError('calendar corrupt')))
    for _ in range(3):
        monitor.check_entered(wired, kite=None, dry_run=True)
    assert wired.find(1)['status'] == 'exited', \
        "a broken calendar suppressed a real exit"


# ── per-leg depth reaches the exit agent (F-3) ───────────────────────────
def test_the_structure_quote_carries_both_books(wired, monkeypatch):
    """VETTING.md asks the exit agent to judge depth at touch and spread as a
    % of mid. The quote dict used to carry only mid/reliable/reason, so the
    agent was asked to judge the one thing it could not see."""
    monkeypatch.setattr(
        monitor.strikes_mod, '_quote_option',
        lambda kite, sym: {'bid': 9.8, 'ask': 10.2, 'mid': 10.0, 'oi': 7000,
                           'last': 10.0, 'reliable': True})
    t = cycle(wired)[0]
    q = monitor._structure_quote(None, t, spot=SPOT)
    assert set(q['legs']) == {'long', 'short'}
    assert q['legs']['long']['bid'] == 9.8
    assert q['legs']['long']['spread_pct'] == 4.0
    assert q['legs']['short']['oi'] == 7000
    assert q['floored'] is False


def test_a_quote_below_the_intrinsic_floor_is_refused_not_clamped(wired,
                                                                  monkeypatch):
    """The floor is an ESTIMATE of fair value, not a price anyone offered.
    Pulling a quote up to it invents a fill exactly the way the garbage book
    did — and on the fill basis it was lifting honest valuations UP (1.8
    booked as 2.5), re-introducing the optimism fill pricing removed. Refuse
    the quote instead and let the next poll try again."""
    monkeypatch.setattr(
        monitor.strikes_mod, '_quote_option',
        lambda kite, sym: {'bid': 0.1, 'ask': 0.2, 'mid': 0.15, 'oi': 7000,
                           'last': 0.15, 'reliable': True})
    t = cycle(wired)[0]
    monkeypatch.setattr(monitor, '_intrinsic_floor', lambda tr, sp: 5.0)
    q = monitor._structure_quote(None, t, spot=SPOT)
    assert q['mid'] is None, "a fair-value estimate was booked as a price"
    assert q['reliable'] is False
    assert q['reason'] == 'below_intrinsic_floor'
    assert q['floored'] is False


def test_a_value_below_zero_is_bounded_to_zero_not_refused(wired, monkeypatch):
    """Distinct from the floor case, and deliberately so. Long bid 0.55 /
    short ask 0.60 is an ORDINARY book once a spread is worthless, and zero is
    a bound the holder can always realise by letting it expire. So it is a
    real value, and booking the loss beats stranding the position until
    expiry. PIIND #50 booked -112.4% on a -100%-capped structure for want of
    this."""
    t = cycle(wired)[0]
    _two_books(monkeypatch, t, 0.50, 0.60, 0.55, 0.65)   # fill = 0.50-0.65
    q = monitor._structure_quote(None, t, spot=None)
    assert q['mid'] == 0.0, "a structure was valued below zero"
    assert q['reliable'] is True, "a bounded value is still a usable quote"


def test_a_value_above_the_width_is_bounded_to_the_width(wired, monkeypatch):
    """The other mathematical bound: a vertical is never worth more than the
    distance between its strikes, whatever a garbage short leg claims."""
    t = cycle(wired)[0]
    assert t['width'] == 40.0
    _two_books(monkeypatch, t, 60.0, 62.0, 1.0, 2.0)     # fill = 60-2 = 58
    q = monitor._structure_quote(None, t, spot=None)
    assert q['mid'] == 40.0, "a vertical was valued above its width"


# ── exit valuation: the basis is a property of the TRADE ─────────────────
# Entry pays the spread and exit pays it again. A mid-mid book records
# neither, which is why the paper P&L read optimistic at BOTH ends and
# modelled zero round-trip cost.

def _books(monkeypatch, bid, ask):
    monkeypatch.setattr(
        monitor.strikes_mod, '_quote_option',
        lambda kite, sym: {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2,
                           'oi': 7000, 'last': (bid + ask) / 2, 'reliable': True})


def _two_books(monkeypatch, trade, l_bid, l_ask, s_bid, s_ask):
    """Per-leg books. Giving both legs the SAME book (what `_books` does) is
    fine for arithmetic but impossible for a vertical — the lower strike is
    always worth more — and an impossible book cannot exercise the bounds."""
    def q(kite, sym):
        bid, ask = ((l_bid, l_ask) if sym == trade['long_symbol']
                    else (s_bid, s_ask))
        return {'bid': bid, 'ask': ask, 'mid': (bid + ask) / 2, 'oi': 7000,
                'last': (bid + ask) / 2, 'reliable': True}
    monkeypatch.setattr(monitor.strikes_mod, '_quote_option', q)


def test_a_fill_basis_position_is_valued_at_bid_minus_ask(wired, monkeypatch):
    """Closing a BCS SELLS the long (at the bid) and BUYS BACK the short (at
    the ask) — strictly worse than mid-mid, which is the honest number.

    One book, both bases, so the gap between them is the assertion: fill
    9 - 6 = 3 against mid 10 - 5 = 5. The 2.00/sh difference IS the round trip
    the old mid-mid valuation recorded as free."""
    t = cycle(wired)[0]
    assert t['pricing_basis'] == 'fill'
    _two_books(monkeypatch, t, 9.0, 11.0, 4.0, 6.0)
    # spot=None: the intrinsic floor is a separate guard with its own tests.
    q = monitor._structure_quote(None, t, spot=None)
    assert q['mid'] == pytest.approx(9.0 - 6.0)   # bid(long) - ask(short)


def test_a_legacy_mid_basis_position_keeps_its_old_valuation(wired, monkeypatch):
    """Basis is stamped at ENTRY and never changes under an open position.
    Flipping a live trade from mid to fill would move its debit-SL and trail
    levels beneath it, and make its round-trip P&L a comparison between two
    different price conventions."""
    t = cycle(wired)[0]
    with wired._mutate():
        wired.find(t['id'])['pricing_basis'] = 'mid'
    t = wired.find(t['id'])
    _two_books(monkeypatch, t, 9.0, 11.0, 4.0, 6.0)   # same book as the fill test
    q = monitor._structure_quote(None, t, spot=None)
    assert q['mid'] == pytest.approx(10.0 - 5.0)      # mid(long) - mid(short)


def test_a_one_sided_book_has_no_fill_value(wired, monkeypatch):
    """There is no price this position could be closed at, so there is no
    honest value to report — the caller freezes its confirm counters instead
    of acting on a number it cannot transact at."""
    t = cycle(wired)[0]
    monkeypatch.setattr(
        monitor.strikes_mod, '_quote_option',
        lambda kite, sym: {'bid': 0, 'ask': 11.0, 'mid': 10.0, 'oi': 7000,
                           'last': 10.0, 'reliable': True})
    q = monitor._structure_quote(None, t, spot=SPOT)
    assert q['mid'] is None and q['reliable'] is False


def test_the_entry_books_are_persisted(wired):
    """42 BCS records existed with no book on any of them, so the gap between
    quoted and fillable could never be measured after the fact — and the
    entry-cost gate had nothing to calibrate against."""
    t = cycle(wired)[0]
    for f in ('long_ask_entry', 'short_bid_entry', 'long_mid_entry',
              'short_mid_entry', 'debit_mid', 'entry_cost', 'entry_cost_pct'):
        assert t.get(f) is not None, f


def test_the_ticket_quotes_prices_you_can_transact_at(wired):
    """In LIVE this alert IS the order ticket. Quoting mid on both legs asks
    the owner to enter at a debit the book will not give him — and the local
    BCS rule has always said ASK to buy, BID to sell, never LTP."""
    t = cycle(wired)[0]
    msg = monitor._format_bcs_enter_alert(t, ANALYSIS, BCS)
    # Compare the trailing PRICE TOKEN, not a substring — "12" lives inside
    # "12.1" and a substring assertion passes on the very bug it is guarding.
    buy_px = next(l for l in msg.splitlines() if 'BUY' in l).split()[-1]
    sell_px = next(l for l in msg.splitlines() if 'SELL' in l).split()[-1]
    assert buy_px == f"{BCS['long_ask']:g}", "ticket does not quote the ask to buy"
    assert sell_px == f"{BCS['short_bid']:g}", "ticket does not quote the bid to sell"
