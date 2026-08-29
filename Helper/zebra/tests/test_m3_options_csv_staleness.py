"""M3 - a stale option chain must not size a live order.

`nse_stocks_options.csv` is where LOT SIZES come from, and a lot size becomes
an order QUANTITY at the broker. It is rebuilt by its own 09:00 Mon-Fri cron
(`flow/server_setup/README_SERVER.txt`) and nothing checked that the cron had
run. A refresh job that dies quietly is invisible from the inside: the stale
chain still parses, still has strikes, still returns a number, and every entry
after that sizes itself from whatever was true the last time the job worked.

Two properties, and they are not the same property:

  1. **ENTRIES are refused** on a stale or missing chain, at the top of
     `analyze_bcs`, before a single quote is spent.
  2. **EXITS are never gated on it.** A close reads its symbols and quantity
     off the trade record and never opens this file. Refusing to exit because
     a scanner input went stale would abandon a live position over a cron job.

And one that is about being heard rather than being correct: the refusal is
silent per-signal by design (it looks like every other hard gate), so a dead
refresh would suppress EVERY signal, every cycle, indefinitely — stalling the
cohort evidence the arming gate is waiting on. The FAULT therefore alerts
separately from the trades it blocks, once a day.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_m3_options_csv_staleness.py -v
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import config as cfg           # noqa: E402
from zebra import monitor as monitor_mod  # noqa: E402
from zebra import strikes                 # noqa: E402

STOCK, EXPIRY, DIRECTION = 'TESTCO', '2026-08-25', 'CE'
ATM, TGT, LOT = 1000.0, 1040.0, 500


def _aged(monkeypatch, days):
    """Override the conftest rail: pretend the file is `days` old."""
    monkeypatch.setattr(strikes, 'options_csv_age_days',
                        lambda now=None, _d=days: _d)


def _missing(monkeypatch):
    monkeypatch.setattr(strikes, 'options_csv_age_days', lambda now=None: None)


def _chain(monkeypatch):
    monkeypatch.setattr(strikes, '_load_options_csv', lambda: None)
    monkeypatch.setattr(strikes, '_OPTIONS_CACHE', {STOCK: {EXPIRY: {
        ATM: {DIRECTION: {'tradingsymbol': 'TESTCO26AUG1000CE'}},
        TGT: {DIRECTION: {'tradingsymbol': 'TESTCO26AUG1040CE'}},
    }}})
    monkeypatch.setattr(strikes, '_list_strikes', lambda *a, **k: [ATM, TGT])


def _quote(mid, oi=20000):
    return {'mid': mid, 'bid': mid, 'ask': mid, 'oi': oi, 'reliable': True}


def _build(monkeypatch):
    _chain(monkeypatch)
    monkeypatch.setattr(strikes, '_quote_option',
                        lambda kite, sym: _quote(16.0))
    return strikes.analyze_bcs(
        kite=None, stock=STOCK, direction=DIRECTION, spot=1000.0,
        target_spot=1040.0, expiry=EXPIRY, atm_strike=ATM,
        atm_quote=_quote(30.0), lot_size=LOT)


# ── the age reading ─────────────────────────────────────────────────────────

def test_the_age_comes_from_the_files_mtime(tmp_path, monkeypatch):
    """Read from the mtime, because the file carries no build stamp inside it
    and the only thing that rewrites it is the refresh job."""
    monkeypatch.undo()                       # drop the conftest rail
    f = tmp_path / 'nse_stocks_options.csv'
    f.write_text('x')
    import os
    old = (datetime.now() - timedelta(days=9)).timestamp()
    os.utime(f, (old, old))
    monkeypatch.setattr(cfg, 'OPTIONS_CSV', f)

    assert 8.9 < strikes.options_csv_age_days() < 9.1


def test_a_missing_file_has_no_age(tmp_path, monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(cfg, 'OPTIONS_CSV', tmp_path / 'nope.csv')
    assert strikes.options_csv_age_days() is None


# ── the rule ────────────────────────────────────────────────────────────────

def test_a_fresh_chain_is_not_stale(monkeypatch):
    _aged(monkeypatch, 0.5)
    assert strikes.options_csv_stale() == (False, '')


def test_a_chain_at_the_limit_is_not_stale(monkeypatch):
    """The boundary is stated, so a future edit to the default cannot quietly
    move it by one day."""
    _aged(monkeypatch, float(cfg.OPTIONS_CSV_MAX_AGE_DAYS))
    stale, _ = strikes.options_csv_stale()
    assert stale is False


def test_a_chain_past_the_limit_is_stale(monkeypatch):
    _aged(monkeypatch, cfg.OPTIONS_CSV_MAX_AGE_DAYS + 0.1)
    stale, why = strikes.options_csv_stale()
    assert stale is True
    assert 'days old' in why


def test_a_MISSING_chain_fails_CLOSED(monkeypatch):
    """"I could not look" must not be spendable as "it is fine" — the same
    polarity as an unknown OI. And `why` says WHICH of the two it was:
    a missing file and a stale one need different fixes."""
    _missing(monkeypatch)
    stale, why = strikes.options_csv_stale()
    assert stale is True
    assert 'MISSING' in why


def test_the_default_tolerates_a_weekend_plus_a_holiday():
    """Friday's file is correct on Monday, and a holiday makes that three
    calendar days. The gate is trying to catch a refresh that has STOPPED, not
    a circular the day it lands — a 1-day limit would block every Monday."""
    assert cfg.OPTIONS_CSV_MAX_AGE_DAYS >= 3


# ── the entry gate ──────────────────────────────────────────────────────────

def test_a_stale_chain_SUPPRESSES_the_entry(monkeypatch):
    _aged(monkeypatch, 30.0)
    r = _build(monkeypatch)
    assert 'error' in r and 'days old' in r['error']


def test_a_missing_chain_suppresses_the_entry(monkeypatch):
    _missing(monkeypatch)
    assert 'MISSING' in _build(monkeypatch).get('error', '')


def test_a_fresh_chain_still_builds(monkeypatch):
    """The negative control. A gate that blocked everything would pass every
    test above and be worthless."""
    _aged(monkeypatch, 0.1)
    r = _build(monkeypatch)
    assert 'error' not in r, r
    assert r['debit'] == 14.0


def test_the_gate_runs_BEFORE_any_quote_is_spent(monkeypatch):
    """Kite allows one quote-family request per second. A gate that priced the
    legs first and then refused would burn the exit path's rate budget to
    reach a decision it could have made from a file stat."""
    quotes = []
    _chain(monkeypatch)
    monkeypatch.setattr(strikes, '_quote_option',
                        lambda kite, sym: quotes.append(sym) or _quote(16.0))
    _aged(monkeypatch, 30.0)
    strikes.analyze_bcs(
        kite=None, stock=STOCK, direction=DIRECTION, spot=1000.0,
        target_spot=1040.0, expiry=EXPIRY, atm_strike=ATM,
        atm_quote=_quote(30.0), lot_size=LOT)
    assert quotes == []


# ── EXITS ARE NEVER GATED ───────────────────────────────────────────────────

def test_no_exit_path_consults_the_chain_freshness():
    """The property, pinned on the SOURCE rather than on one exit's behaviour.

    A close reads its symbols and quantity off the trade record. If a future
    edit put this check on an exit, a cron job that failed overnight would
    strand every open position with its stops refusing to fire — a strictly
    worse failure than the one the gate exists to prevent.
    """
    src = Path(strikes.__file__).read_text(encoding='utf-8')
    body = src[src.index('def analyze_bcs('):]
    assert 'options_csv_stale()' in body, 'the entry gate is gone'

    monitor_src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    # The ONE permitted caller in the monitor is the operational alert. Any
    # other reference would be a gate on a path that is not an entry.
    assert monitor_src.count('options_csv_stale()') == 1
    fn = monitor_src[monitor_src.index('def _alert_options_csv_stale('):]
    assert 'options_csv_stale()' in fn[:fn.index('\ndef ', 1)]


# ── the operational alert ───────────────────────────────────────────────────

def test_a_stale_chain_alerts_the_owner(monkeypatch, telegrams):
    """The refusal is silent per-signal by design. A dead refresh job would
    therefore stall the cohort in silence — and the cohort's missing evidence
    is the one thing standing between this system and being armed."""
    _aged(monkeypatch, 30.0)
    assert monitor_mod._alert_options_csv_stale() is True
    assert len(telegrams) == 1
    assert 'ENTRIES BLOCKED' in telegrams[0]
    assert 'Open positions are UNAFFECTED' in telegrams[0]


def test_the_alert_is_once_a_day_across_PROCESSES(monkeypatch, telegrams):
    """A marker FILE, not an in-memory set: zebra is a five-minute cron that
    exits between cycles, so an in-process dedup would re-alert 78 times a
    session. (The BCS monitor's nags can use a set — that one is a single
    long-lived process. The difference is the process model, not the policy.)
    """
    _aged(monkeypatch, 30.0)
    for _ in range(12):
        monitor_mod._alert_options_csv_stale()
    assert len(telegrams) == 1


def test_a_fresh_chain_alerts_nothing(monkeypatch, telegrams):
    _aged(monkeypatch, 0.5)
    assert monitor_mod._alert_options_csv_stale() is False
    assert telegrams == []


def test_a_REPEAT_outage_alerts_again(monkeypatch, telegrams):
    """The marker is cleared when the file comes back. Deduping next month's
    outage against last month's stamp would silence the second one entirely."""
    _aged(monkeypatch, 30.0)
    monitor_mod._alert_options_csv_stale()
    _aged(monkeypatch, 0.5)
    monitor_mod._alert_options_csv_stale()          # recovered, marker cleared
    _aged(monkeypatch, 30.0)
    monitor_mod._alert_options_csv_stale()
    assert len(telegrams) == 2


def test_the_cycle_calls_it_and_cannot_be_killed_by_it():
    """Wired, and wrapped. An input-freshness check that could raise into the
    cycle would stop exit monitoring — a worse bug than the one it reports."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('def run_cycle('):]
    body = body[:body.index('\ndef ', 1)]
    assert '_alert_options_csv_stale(dry_run=dry_run)' in body
    call = body.index('_alert_options_csv_stale(dry_run=dry_run)')
    assert 'try:' in body[:call][-400:], 'the call is not inside a try'


def test_the_tracked_default_and_the_code_default_agree():
    """The config split's standing rule: a threshold that lives in only one of
    the two sources drifts the moment somebody rebuilds the overlay."""
    import json
    tracked = json.loads(
        (Path(cfg.__file__).resolve().parents[1]
         / 'config' / 'zebra_config.defaults.json').read_text(encoding='utf-8'))
    assert (tracked['options_csv_max_age_days']
            == cfg._DEFAULTS['options_csv_max_age_days'])
