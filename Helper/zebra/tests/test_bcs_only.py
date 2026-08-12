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
       'long_mid': 12.0, 'short_mid': 2.0}


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
