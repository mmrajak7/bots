"""Gates on the shadow BCS builder (strikes.analyze_bcs).

Both gates were soft warnings until 2026-08-10 and are now hard blocks:
  1. OI >= cfg.MIN_LEG_OI on BOTH legs
  2. debit <= cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT of spread width
plus the removal of the `target_spread>2%` warning, which fired on 68% of
trades and carried no signal.

The rejection path matters as much as the numbers: an unclean signal must
come back as {'error': ...} so the caller suppresses the alert, NOT as a
tradeable dict carrying a ⚠ nobody reads.

Run:  cd Helper && python -m pytest zebra/tests/test_bcs_gates.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import config as cfg          # noqa: E402
from zebra import strikes                # noqa: E402

STOCK, EXPIRY, DIRECTION = 'TESTCO', '2026-08-25', 'CE'
ATM, TGT = 1000.0, 1040.0                # width 40 on a 1000 spot = 4%, realistic
LOT = 500


def _chain(monkeypatch):
    """Minimal in-memory option chain so no CSV or Kite call is needed."""
    monkeypatch.setattr(strikes, '_load_options_csv', lambda: None)
    monkeypatch.setattr(strikes, '_OPTIONS_CACHE', {STOCK: {EXPIRY: {
        ATM: {DIRECTION: {'tradingsymbol': 'TESTCO26AUG1000CE'}},
        TGT: {DIRECTION: {'tradingsymbol': 'TESTCO26AUG1040CE'}},
    }}})
    monkeypatch.setattr(strikes, '_list_strikes', lambda *a, **k: [ATM, TGT])


def _quote(mid, oi, spread_pct=0.0):
    """Zero-width by default so mid == bid == ask, and the FILL debit equals
    the MID debit. That keeps the gate tests below testing gates rather than
    the ask/bid arithmetic, which has its own section at the end of the file."""
    half = mid * spread_pct / 2
    return {'mid': mid, 'bid': round(mid - half, 2), 'ask': round(mid + half, 2),
            'oi': oi, 'reliable': True}


def _run(monkeypatch, *, tgt_mid, tgt_oi, atm_mid=30.0, atm_oi=20000,
         tgt_spread_pct=0.0, atm_quote=None):
    _chain(monkeypatch)
    monkeypatch.setattr(strikes, '_quote_option',
                        lambda kite, sym: _quote(tgt_mid, tgt_oi, tgt_spread_pct))
    return strikes.analyze_bcs(
        kite=None, stock=STOCK, direction=DIRECTION, spot=1000.0,
        target_spot=1040.0, expiry=EXPIRY, atm_strike=ATM,
        atm_quote=atm_quote if atm_quote is not None
        else _quote(atm_mid, atm_oi), lot_size=LOT,
    )


# ── happy path ───────────────────────────────────────────────────────────
def test_clean_spread_is_accepted(monkeypatch):
    """debit 14 on width 40 = 35% — inside both gates."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000)
    assert 'error' not in r, r
    assert r['debit'] == 14.0
    assert r['width'] == 40.0
    assert r['debit_to_width_pct'] == 35.0
    assert r['warnings'] == []


def test_low_debit_ratio_still_accepted(monkeypatch):
    """No FLOOR by design: cheap spreads are the high-payoff tail."""
    r = _run(monkeypatch, tgt_mid=25.0, tgt_oi=20000)   # debit 5 = 12.5%
    assert 'error' not in r, r
    assert r['debit_to_width_pct'] == 12.5


# ── gate 1: liquidity ────────────────────────────────────────────────────
def test_illiquid_target_leg_is_blocked(monkeypatch):
    """The COCHINSHIP case: thin target book -> stop cannot fill at trigger."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=cfg.MIN_LEG_OI - 1)
    assert 'error' in r
    assert 'target leg OI' in r['error']
    assert 'suppressed' in r['error']


def test_illiquid_atm_leg_is_blocked(monkeypatch):
    """The ATM leg comes from zebra's picker, which RELAXES its own gates,
    so it cannot be assumed clean — check it independently."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000, atm_oi=cfg.MIN_LEG_OI - 1)
    assert 'error' in r
    assert 'ATM leg OI' in r['error']


def test_oi_exactly_at_threshold_passes(monkeypatch):
    """Gate is `< MIN_LEG_OI`, so the boundary value itself is allowed."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=cfg.MIN_LEG_OI,
             atm_oi=cfg.MIN_LEG_OI)
    assert 'error' not in r, r


# ── gate 2: debit / width ────────────────────────────────────────────────
def test_debit_over_width_cap_is_blocked(monkeypatch):
    """debit 20 on width 40 = 50% — past the 45% cap, no payoff left."""
    r = _run(monkeypatch, tgt_mid=10.0, tgt_oi=20000)
    assert 'error' in r
    assert '50.0% of' in r['error']
    assert 'suppressed' in r['error']


def test_debit_exactly_at_cap_passes(monkeypatch):
    """Gate is `>` the cap, so a spread landing exactly on it is allowed."""
    cap = cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT           # 45.0 -> debit 18 on width 40
    r = _run(monkeypatch, tgt_mid=round(30.0 - 40.0 * cap / 100, 2), tgt_oi=20000)
    assert 'error' not in r, r
    assert r['debit_to_width_pct'] == pytest.approx(cap)


def test_reported_ratio_matches_the_gate(monkeypatch):
    """The stored d/w must be the same number the gate judged — they were
    computed twice before this change, which is how they could drift."""
    r = _run(monkeypatch, tgt_mid=13.0, tgt_oi=20000)   # debit 17 / 40 = 42.5%
    assert r['debit_to_width_pct'] == pytest.approx(r['debit'] / r['width'] * 100)


# ── the removed warning ──────────────────────────────────────────────────
def test_wide_spread_no_longer_warns_or_blocks(monkeypatch):
    """`target_spread>2%` is gone: it fired on 68% of trades and split 58.8%
    vs 62.5% WR — pure noise. The measurement still ships for the record."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000, tgt_spread_pct=0.08)
    assert 'error' not in r, r
    assert r['warnings'] == []
    assert r['short_spread_pct'] > 2.0          # measured, just not acted on


# ── ordering: liquidity is judged before payoff ──────────────────────────
def test_illiquid_and_overpriced_reports_liquidity_first(monkeypatch):
    """When both gates fail the OI reason must win — it is the one that
    describes an unfillable stop rather than a merely poor trade."""
    r = _run(monkeypatch, tgt_mid=10.0, tgt_oi=cfg.MIN_LEG_OI - 1)
    assert 'error' in r
    assert 'OI' in r['error']


# ── malformed OI must fail CLOSED, never raise ───────────────────────────
# A raise inside analyze_bcs is caught by the monitor's broad except and
# misreported as 'shadow build failed' — the gate reason would be lost.

def test_oi_none_on_target_is_blocked_not_raised(monkeypatch):
    """Kite can ship "oi": null → _quote_option passes None through."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=None)
    assert 'error' in r
    assert 'target leg OI 0' in r['error']


def test_oi_none_on_atm_is_blocked_not_raised(monkeypatch):
    atm = _quote(30.0, 20000)
    atm['oi'] = None
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000, atm_quote=atm)
    assert 'error' in r
    assert 'ATM leg OI 0' in r['error']


def test_missing_oi_key_is_blocked_not_raised(monkeypatch):
    """A caller-built atm_quote without 'oi' = unknown liquidity = blocked."""
    atm = _quote(30.0, 20000)
    del atm['oi']
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000, atm_quote=atm)
    assert 'error' in r
    assert 'ATM leg OI 0' in r['error']


# ── config plumbing: a typo must never disarm the gate ───────────────────
# json.load accepts NaN/Infinity literals; NaN compares False against
# everything, which would silently turn the hard gate into a no-op. Strings
# and null raise TypeError on every comparison. All fall back to the default.

@pytest.mark.parametrize('bad', ['45', None, -5, 0, True, False,
                                 float('nan'), float('inf')])
def test_bad_config_value_falls_back_to_default(bad):
    assert cfg._positive_finite('bcs_max_debit_to_width_pct', bad, 45.0) == 45.0


@pytest.mark.parametrize('good,expect', [(40, 40.0), (45.5, 45.5), (100, 100.0)])
def test_valid_config_value_round_trips(good, expect):
    got = cfg._positive_finite('bcs_max_debit_to_width_pct', good, 45.0)
    assert got == expect and isinstance(got, float)


def test_exported_cap_is_a_positive_float():
    """Whatever the JSON said, the module-level export must be usable in a
    float comparison — this is the invariant the gate depends on."""
    v = cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT
    assert isinstance(v, float) and v > 0


# The same exposure applied to every other numeric key (all 15 of which the
# live zebra_config.json hand-overrides), so they now share the validator.
# Count-valued keys keep int type: they feed range()/timedelta()/sleep().

@pytest.mark.parametrize('bad', ['5', None, -1, 0, True, float('nan'),
                                 float('inf'), 10.5])
def test_bad_int_config_value_falls_back_to_default(bad):
    assert cfg._positive_int('min_leg_oi', bad, 5000) == 5000


def test_non_integral_is_rejected_not_rounded():
    """Silently rounding a lookback window is drift nobody notices until a
    backtest stops reconciling — reject and warn instead."""
    assert cfg._positive_int('st_period', 10.7, 10) == 10
    assert cfg._positive_int('st_period', 10.0, 99) == 10   # integral float is fine


@pytest.mark.parametrize('const,want_type', [
    ('WATCH_GAP_MAX', float), ('TRIGGER_GAP_MAX', float), ('STALE_GAP_MIN', float),
    ('FRESHNESS_DAYS', int), ('MIN_DTE', int), ('MAX_DTE', int),
    ('MIN_LEG_OI', int), ('MAX_LEG_SPREAD_PCT', float),
    ('SPOT_SL_PCT', float), ('DEBIT_SL_PCT', float), ('TIME_SL_DAYS', int),
    ('MAX_OPEN_TRADES', int), ('MAX_WATCHING_SIGNALS', int),
    ('SCAN_INTERVAL_SEC', int), ('MONITOR_INTERVAL_SEC', int),
    ('ST_PERIOD', int), ('ST_MULTIPLIER', float),
    ('BCS_MAX_DEBIT_TO_WIDTH_PCT', float),
])
def test_every_numeric_export_is_validated_and_correctly_typed(const, want_type):
    v = getattr(cfg, const)
    assert isinstance(v, want_type), f"{const} is {type(v).__name__}"
    assert v > 0


def test_validation_did_not_change_the_live_values():
    """Regression guard: adding validation must not silently alter what the
    bot actually trades with. Every key present in the live JSON must survive
    the validator numerically unchanged."""
    import json
    with open(cfg.CONFIG_FILE) as f:
        live = json.load(f)
    for const, key in [
        ('WATCH_GAP_MAX', 'watch_gap_max'), ('TRIGGER_GAP_MAX', 'trigger_gap_max'),
        ('STALE_GAP_MIN', 'stale_gap_min'), ('FRESHNESS_DAYS', 'freshness_days'),
        ('MIN_DTE', 'min_dte'), ('MAX_DTE', 'max_dte'), ('MIN_LEG_OI', 'min_leg_oi'),
        ('MAX_LEG_SPREAD_PCT', 'max_leg_spread_pct'), ('SPOT_SL_PCT', 'spot_sl_pct'),
        ('DEBIT_SL_PCT', 'debit_sl_pct'),
        ('TIME_SL_DAYS', 'time_sl_days_before_expiry'),
        ('MAX_OPEN_TRADES', 'max_open_trades'),
        ('MAX_WATCHING_SIGNALS', 'max_watching_signals'),
        ('SCAN_INTERVAL_SEC', 'scan_interval_sec'),
        ('MONITOR_INTERVAL_SEC', 'monitor_interval_sec'),
    ]:
        if key in live:
            assert float(getattr(cfg, const)) == float(live[key]), key


# ── a suppression is LOG-ONLY, never Telegram ────────────────────────────
# User's call 2026-08-10: one notification per rejected signal is the alert
# fatigue the gates exist to cure. The record still has to be greppable.

def test_suppression_is_logged_with_a_greppable_prefix(monkeypatch, caplog):
    from zebra import monitor
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=cfg.MIN_LEG_OI - 1)
    trade = {'id': 7, 'stock': STOCK, 'direction': DIRECTION}
    with caplog.at_level('WARNING', logger='zebra.monitor'):
        monitor._log_bcs_suppressed(trade, r['error'])
    text = caplog.text
    assert 'BCS SUPPRESSED' in text        # the documented grep handle
    assert STOCK in text and '#7' in text
    assert 'target leg OI' in text         # the real reason, not a summary


def test_suppression_log_has_a_fallback_reason(monkeypatch, caplog):
    from zebra import monitor
    trade = {'id': 3, 'stock': STOCK, 'direction': DIRECTION}
    with caplog.at_level('WARNING', logger='zebra.monitor'):
        monitor._log_bcs_suppressed(trade, None)
    assert 'no viable BCS pair' in caplog.text


def test_no_telegram_formatter_for_suppression_remains():
    """Guard against the notification creeping back in: there must be no
    suppression *alert* builder, only the logger."""
    from zebra import monitor
    assert not hasattr(monitor, '_format_bcs_suppressed_alert')


# The alerts that DO still fire must keep their escaping — gate_fails tags
# ("long_OI<5000") carry a bare '<' and would 400 the whole message.

def test_live_alerts_still_escape_interpolated_tags():
    from zebra import monitor
    import inspect
    for fn in (monitor._format_enter_alert, monitor._format_bcs_enter_alert):
        assert 'html.escape' in inspect.getsource(fn), fn.__name__


# ── FILL basis: ask to buy, bid to sell (2026-08-12) ─────────────────────
# The local BCS rule has always said "For BUYING use ASK. For SELLING use BID
# (never LTP)". The zebra ticket obeyed it; this builder priced both legs at
# mid, and BCS is now the only structure that trades and the only Telegram
# voice — so the ticket quoted a debit nobody could fill at, and that number
# anchors max_gain, the debit-SL and both trail levels.

def test_the_debit_is_priced_at_ask_minus_bid(monkeypatch):
    """atm 30 ±5% -> ask 30.75; tgt 16 ±5% -> bid 15.60. Fill debit 15.15,
    against 14.00 at mid: 8% more expensive before anything moves."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000,
             tgt_spread_pct=0.05, atm_quote=_quote(30.0, 20000, 0.05))
    assert 'error' not in r, r
    assert r['debit'] == pytest.approx(30.75 - 15.60, abs=0.02)
    assert r['debit_mid'] == 14.0
    assert r['pricing_basis'] == 'fill'


def test_the_entry_cost_is_reported_against_max_gain(monkeypatch):
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000,
             tgt_spread_pct=0.05, atm_quote=_quote(30.0, 20000, 0.05))
    assert r['entry_cost'] == pytest.approx(r['debit'] - r['debit_mid'], abs=0.01)
    assert r['entry_cost_pct'] == pytest.approx(
        r['entry_cost'] / (r['width'] - r['debit_mid']) * 100, abs=0.1)


def test_max_profit_is_computed_off_the_fill_not_the_quote(monkeypatch):
    """The whole point: what you can actually win, after paying to get in."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000,
             tgt_spread_pct=0.05, atm_quote=_quote(30.0, 20000, 0.05))
    assert r['max_profit_per_share'] == pytest.approx(r['width'] - r['debit'],
                                                      abs=0.01)
    assert r['max_profit_per_share'] < r['width'] - r['debit_mid']


def test_a_one_sided_book_cannot_be_priced(monkeypatch):
    """No bid on the leg being sold = no fill price = no trade, rather than a
    debit computed off a mid that nothing backs."""
    atm = _quote(30.0, 20000)
    tgt = _quote(16.0, 20000)
    tgt['bid'] = 0
    monkeypatch.setattr(strikes, '_quote_option', lambda kite, sym: tgt)
    _chain(monkeypatch)
    r = strikes.analyze_bcs(kite=None, stock=STOCK, direction=DIRECTION,
                            spot=1000.0, target_spot=1040.0, expiry=EXPIRY,
                            atm_strike=ATM, atm_quote=atm, lot_size=LOT)
    assert 'error' in r and 'two-way book' in r['error']


# ── gate 3: what the book charges to open ────────────────────────────────
def test_a_book_that_eats_the_payoff_is_blocked(monkeypatch):
    """_leg_reliable admits legs up to 25% of mid — a garbage-print detector,
    never a tradeability gate. At that width the entry cost alone takes a
    quarter of the max gain, which is the same 'payoff priced out' problem the
    d/w cap exists for."""
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000,
             tgt_spread_pct=0.25, atm_quote=_quote(30.0, 20000, 0.25))
    assert 'error' in r
    assert 'entry cost' in r['error'] and 'suppressed' in r['error']


def test_the_entry_cost_gate_is_judged_after_the_debit_cap(monkeypatch):
    """A spread that is both overpriced AND badly quoted should report the
    payoff problem — it is the one that survives a better book."""
    r = _run(monkeypatch, tgt_mid=10.0, tgt_oi=20000,
             tgt_spread_pct=0.25, atm_quote=_quote(30.0, 20000, 0.25))
    assert 'error' in r and 'of width' in r['error']


def test_a_tight_book_passes_the_entry_cost_gate(monkeypatch):
    r = _run(monkeypatch, tgt_mid=16.0, tgt_oi=20000,
             tgt_spread_pct=0.01, atm_quote=_quote(30.0, 20000, 0.01))
    assert 'error' not in r, r
    assert r['entry_cost_pct'] < cfg.BCS_MAX_ENTRY_COST_PCT


def test_the_d_w_gate_still_reads_the_mid_basis(monkeypatch):
    """It was calibrated on mid-basis d/w across 42 records and cannot be
    re-derived — none of them persisted an entry book. Moving it onto the fill
    basis would change selectivity by an unmeasurable amount."""
    cap = cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT
    # mid debit lands exactly on the cap; the fill debit is well past it.
    r = _run(monkeypatch, tgt_mid=round(30.0 - 40.0 * cap / 100, 2),
             tgt_oi=20000, tgt_spread_pct=0.02,
             atm_quote=_quote(30.0, 20000, 0.02))
    assert 'error' not in r, r
    assert r['debit_to_width_pct_mid'] == pytest.approx(cap)
    assert r['debit_to_width_pct'] > cap
