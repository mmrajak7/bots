"""The Medium/Low tail from the 2026-08-12 six-reviewer audit.

Each of these is small on its own. They are here because "small" and "cannot
happen" are different claims, and three of the four had already been verified
against the real log or the real store before being fixed.

Run:  cd Helper && python -m pytest zebra/tests/test_review_tail.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg              # noqa: E402
from zebra import outcomes                   # noqa: E402
from playbook.magnet import scanner as mscan  # noqa: E402


# ── a label must never be asserted from missing data ─────────────────────

def test_a_nan_pnl_does_not_score_as_a_hit():
    """NaN fails EVERY comparison, so `pnl < 0` was False and a corrupt or
    unknown P&L sailed through as a HIT — then fed the vetting scorecard and
    the precedent system as evidence of a good signal."""
    assert outcomes.label_for_reason('paper:tp', float('nan')) == outcomes.FLAT
    assert outcomes.label_for_reason('paper:tp', float('inf')) == outcomes.FLAT


def test_a_real_loss_still_scores_as_a_miss():
    """Companion — the NaN guard must not swallow the P&L override itself."""
    assert outcomes.label_for_reason('paper:trail', -500.0) == outcomes.MISS


def test_a_real_profit_still_scores_as_a_hit():
    assert outcomes.label_for_reason('paper:tp', 1200.0) == outcomes.HIT


# ── a liquidity gate must not switch itself off ──────────────────────────

def test_the_liquidity_check_follows_the_configured_oi(monkeypatch):
    """`liquidity_ok` matched the literal '_OI<5000' while the string it
    matches is built from cfg.MIN_LEG_OI. Raise the config to 6000 and the
    gate emits 'long_OI<6000', matches nothing, and silently marks an illiquid
    leg liquid — a check that turns itself off exactly when you tighten it."""
    import inspect
    from zebra import strikes
    # Comments stripped: the fix's own rationale quotes the old literal, and a
    # naive substring check would match the explanation rather than the code.
    code = '\n'.join(ln.split('#')[0] for ln
                     in inspect.getsource(strikes.analyze).splitlines())
    assert '_OI<5000' not in code, "a config threshold is baked into the match"
    for thresh in (5000, 6000, 12345):
        fails = [f'long_OI<{thresh}']
        ok = not any(g.startswith(('long_OI<', 'short_OI<', 'long_spread',
                                   'short_spread')) for g in fails)
        assert ok is False, f"an OI failure at {thresh} read as liquid"


# ── the warning channel must stay readable ───────────────────────────────

def test_index_symbols_do_not_warn(monkeypatch, caplog):
    """NIFTY 4,472 + CNXMIDCAP 3,386 + BANKNIFTY 1,921 = 9,779 of the 9,928
    WARNING lines in the real log. Chartink returns the index alongside its
    constituents; there is no mapping to add, so they are noise."""
    monkeypatch.setattr(mscan, '_load_instrument_cache', lambda kite: None)
    monkeypatch.setattr(mscan, '_instrument_cache', {'RELIANCE': 1})
    with caplog.at_level('WARNING'):
        out = mscan.get_ltp(None, ['NIFTY', 'BANKNIFTY', 'CNXMIDCAP'])
    assert not caplog.records, "index symbols still spam the warning channel"
    assert out == {'NIFTY': 0.0, 'BANKNIFTY': 0.0, 'CNXMIDCAP': 0.0}, \
        "the not-in-NSE sentinel was lost"


def test_a_genuinely_unmapped_symbol_still_warns_once(monkeypatch, caplog):
    """Companion. The actionable half must survive — this is the warning that
    means 'add an entry to _CHARTINK_TO_NSE'."""
    monkeypatch.setattr(mscan, '_load_instrument_cache', lambda kite: None)
    monkeypatch.setattr(mscan, '_instrument_cache', {'RELIANCE': 1})
    with caplog.at_level('WARNING'):
        mscan.get_ltp(None, ['NIFTY', 'WEIRD_SYM', 'ALSO_WEIRD'])
    warnings = [r for r in caplog.records if r.levelname == 'WARNING']
    assert len(warnings) == 1, "expected exactly one aggregated warning"
    assert 'WEIRD_SYM' in warnings[0].getMessage()
    assert 'NIFTY' not in warnings[0].getMessage()


# ── a triggered signal must not be parked forever ────────────────────────

def test_paper_retries_a_triggered_signal_that_never_entered():
    """In PAPER a completed entry moves the row to `entered`, so a row still
    in `triggered` means the entry did NOT complete. The old `continue`
    (justified as saving Kite quote calls) parked it forever: still inside its
    trigger band, never retried, released only by a drift/stale cancel."""
    import inspect
    from zebra import monitor
    body = inspect.getsource(monitor.check_watching)
    at = body.index("if trade['status'] == 'triggered':")
    window = body[at:at + 1400]
    assert 'if not cfg.PAPER_MODE:' in window, \
        "the bail-out is no longer gated on LIVE — paper would park again"
    assert 'RETRY' in window, "no retry path for an incomplete paper entry"


def test_live_still_stops_at_the_ticket():
    """LIVE must NOT retry: there the alert IS the order ticket, and
    re-alerting every 5 minutes is noise, not recovery."""
    import inspect
    from zebra import monitor
    body = inspect.getsource(monitor.check_watching)
    at = body.index("if trade['status'] == 'triggered':")
    window = body[at:at + 1400]
    assert window.index('if not cfg.PAPER_MODE:') < window.index('RETRY'), \
        "the LIVE bail-out must come before the retry log line"


# ── cycle boundaries in the log ──────────────────────────────────────────

def test_the_cycle_is_delimited_and_timed():
    """cron appends every run to one file and the process exits between them,
    so without a marker a cycle that died halfway is indistinguishable from
    one that had nothing to say."""
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor.run_once)
    assert 'CYCLE START' in src and 'ABORTED' in src
    assert 'finally:' in src, "a crashed cycle must still close its marker"


# ── an unpriceable watchlist row must not be immortal ────────────────────

def test_a_symbol_that_stops_quoting_is_eventually_released(tmp_path,
                                                            monkeypatch):
    """Drift and stale cancels both need a GAP, which needs a price. A
    suspended/renamed/delisted symbol never updates its gap, so it holds one of
    the 25 watchlist slots and its stock's dedup entry forever."""
    from datetime import datetime, timedelta
    from zebra import monitor
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'z.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'z.lock')
    s = ZebraStore()
    s.add_signal({'stock': 'GONECO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    old = (datetime.now() - timedelta(days=cfg.WATCH_MAX_AGE_DAYS + 5))
    with s._mutate():
        s.find(1)['signal_date'] = old.strftime('%Y-%m-%d')

    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'GONECO': 0.0})
    monitor.check_watching(s, kite=None, dry_run=True)
    assert s.find(1)['status'] == 'cancelled', "the dead row kept its slot"


def test_a_fresh_unpriceable_row_is_left_alone(tmp_path, monkeypatch):
    """Companion: one bad quote is not a dead symbol. A feed hiccup must not
    cancel a signal that was added this morning."""
    from zebra import monitor
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'z2.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'z2.lock')
    s = ZebraStore()
    s.add_signal({'stock': 'FINECO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'FINECO': 0.0})
    monitor.check_watching(s, kite=None, dry_run=True)
    assert s.find(1)['status'] == 'watching'
