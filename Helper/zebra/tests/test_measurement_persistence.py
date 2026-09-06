"""The measurements must reach DISK, not just the candidate dict.

THE DEFECT THIS FILE EXISTS FOR (found in review, 2026-09-03, before deploy).
Two "measured, not enforced" instruments shipped in the same change:

  * `analyze_bcs` stamped `proj_value_at_tp` / `proj_gain_at_tp_pct` /
    `tp_value_frac_of_width_k` / `min_gain_at_tp_pct_at_entry` /
    `would_block_on_gain_at_tp` on the candidate, "so the would-block
    population can be scored at ~30 closes";
  * `_swing_target_shadow` stamped `swing_shadow`, "stored so the two
    constructions can be compared at exit on ~10 swing signals".

`ZebraStore._bcs_entry_fields` is an ALLOWLIST. Every one of those keys was
dropped on the store's doorstep. Both features computed, logged, and threw the
result away -- and 230 tests passed, because every test asserted on the
ephemeral return dict. The swing shadow is the worse of the two: unlike the
projection it is NOT recomputable later at any price, because an option book
cannot be reconstructed after the fact (the same reason `exit_legs` is
persisted).

This is the codebase's own recurring shape -- FIFTY's breadth capture that
looked deployed and captured zero sessions, the corp-action calendar whose only
writer was disabled, the digest cron that went uninstalled for 18 days. Nothing
FAILS when evidence is absent; it is simply not there when someone finally
looks. It is also exactly why the exit-bridge write path stayed broken: no test
drove the REAL store.

So these tests drive `ZebraStore.mark_entered_bcs` and read the record back.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_measurement_persistence.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg               # noqa: E402
from zebra.trade_store import ZebraStore      # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    return s


SHADOW = {'target_spot': 103.0, 'same_strike': False, 'short_strike': 120.0,
          'width': 20.0, 'debit': 8.0, 'debit_to_width_pct': 40.0,
          'proj_gain_at_tp_pct': 37.5, 'would_block_on_gain_at_tp': True,
          'tp_value_frac_of_width_k': 0.55,
          'min_gain_at_tp_pct_at_entry': 50.0,
          'short_symbol': 'TESTCO26SEP120CE', 'short_oi': 40000}


def _bcs(**over):
    b = {
        'long_strike': 100.0, 'short_strike': 140.0,
        'long_symbol': 'TESTCO26SEP100CE', 'short_symbol': 'TESTCO26SEP140CE',
        'debit': 10.0, 'width': 40.0, 'lot_size': 100, 'lots': 1,
        'expiry': '2026-09-30', 'structure': 'bcs',
        'entry_spot': 96.0, 'tp_spot': 104.0,
        'long_bid': 11.0, 'long_ask': 11.2, 'long_mid': 11.1,
        'short_bid': 1.1, 'short_ask': 1.3, 'short_mid': 1.2,
        'long_oi': 50000, 'short_oi': 40000,
        # what the change under test adds
        'proj_value_at_tp': 22.0,
        'proj_gain_at_tp_pct': 120.0,
        'tp_value_frac_of_width_k': 0.55,
        'min_gain_at_tp_pct_at_entry': 50.0,
        # The THIRD input to the flag, added 2026-09-06 with the penetration
        # model. Without it a stored projection cannot be re-derived at all:
        # k and the floor travel, but the term that SCALES them did not, so
        # every row would read as pen=1 whether it was or not.
        'tp_penetration': 0.85,
        'would_block_on_gain_at_tp': False,
        'swing_shadow': dict(SHADOW),
    }
    b.update(over)
    return b


MEASUREMENT_KEYS = ('proj_value_at_tp', 'proj_gain_at_tp_pct',
                    'tp_value_frac_of_width_k', 'min_gain_at_tp_pct_at_entry',
                    'tp_penetration',
                    'would_block_on_gain_at_tp', 'swing_shadow')


def test_the_measurements_actually_reach_the_record(store):
    """THE defect, one assertion. Every one of these was dropped by the
    allowlist while every other test in the change passed."""
    store.mark_entered_bcs(1, _bcs())
    t = store.find(1)
    missing = [k for k in MEASUREMENT_KEYS if k not in t]
    assert not missing, (
        'these were stamped on the candidate and dropped by '
        '_bcs_entry_fields -- the feature computes, logs, and throws the '
        'result away: %s' % missing)
    assert t['proj_gain_at_tp_pct'] == 120.0
    assert t['would_block_on_gain_at_tp'] is False
    assert t['tp_penetration'] == 0.85


def test_the_swing_shadow_survives_whole(store):
    """It is a nested dict and it is NOT recomputable later -- an option book
    cannot be reconstructed after the fact. A truncated copy is no better than
    no copy."""
    store.mark_entered_bcs(1, _bcs())
    got = store.find(1)['swing_shadow']
    assert got == SHADOW, 'the shadow was reshaped or truncated on the way in'


def test_the_measurements_survive_a_reload_from_disk(store, tmp_path):
    """The cron is a fresh process every 5 minutes. In-memory is not evidence.

    This is the assertion that separates "the dict has the key" from "the book
    has the key", which is the whole distinction this file is about.

    It reads the raw JSON on purpose: `find()` could satisfy the loop above
    from a cache, and `swing_shadow` is a NESTED dict, which is the shape most
    likely to survive a round trip as a string repr rather than as an object.

    RETIRES WHEN: the store gains a schema/serialisation contract test that
    asserts every persisted field survives a disk round trip with its type
    intact -- at which point this file only needs to assert the keys are in
    the allowlist, and the raw-file read here becomes redundant.
    """
    store.mark_entered_bcs(1, _bcs())
    fresh = ZebraStore(config={'google_drive': {'enabled': False}})
    fresh._load_local()
    t = fresh.find(1)
    for k in MEASUREMENT_KEYS:
        assert k in t, '%s did not survive the round trip to disk' % k
    assert t['swing_shadow']['short_strike'] == 120.0
    # and it is real JSON, not a repr of a dict
    raw = json.loads((tmp_path / 'zebra_trades.json').read_text())
    rec = [x for x in (raw['trades'] if isinstance(raw, dict) else raw)
           if x['id'] == 1][0]
    assert isinstance(rec['swing_shadow'], dict)


def test_k_and_the_floor_BOTH_travel_with_the_flag(store):
    """The point of stamping them. `would_block_on_gain_at_tp` is a comparison
    of two config numbers; a record carrying the flag but not its inputs is
    silently re-labelled the moment either number moves before the ~30-close
    review -- the exact failure the stamp exists to prevent. Storing only `k`
    leaves the same hole open on the threshold side.
    """
    store.mark_entered_bcs(1, _bcs())
    t = store.find(1)
    assert t['tp_value_frac_of_width_k'] == 0.55
    assert t['min_gain_at_tp_pct_at_entry'] == 50.0
    # the flag must be reproducible from what the record itself carries
    recomputed = (t['tp_value_frac_of_width_k'] * t['width'] / t['debit'] - 1) * 100
    assert (recomputed < t['min_gain_at_tp_pct_at_entry']) is \
        t['would_block_on_gain_at_tp']


def test_a_signal_with_no_swing_stores_no_shadow_and_does_not_crash(store):
    """The common case -- most signals have no swing level in the way. Absent
    must read as absent, not as a missing key that breaks a later reader."""
    b = _bcs()
    del b['swing_shadow']
    store.mark_entered_bcs(1, b)
    t = store.find(1)
    assert t['swing_shadow'] is None
    assert t['proj_gain_at_tp_pct'] == 120.0
