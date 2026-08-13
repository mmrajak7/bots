"""LIVE-mode regressions — the branches that only run when paper_mode is off.

Why this file exists: before 2026-08-13 the entire live side of `check_entered`
shipped on inspection alone. Every exit-path test set `PAPER_MODE = True`, so
the mode the owner is about to switch into had ZERO coverage — and the three
defects pinned below were all found by reading, in code that had passed 547
green tests.

Run:  cd Helper && python -m pytest zebra/tests/test_live_mode.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import monitor as mon         # noqa: E402

# Captured at IMPORT — before the autouse production-path rail rebinds them —
# so the rail can be checked against the real values it is supposed to hide.
_REAL_LOG_DIR = cfg.LOG_DIR
_REAL_PATHS = {
    name: value
    for name, value in vars(cfg).items()
    if isinstance(value, Path) and _REAL_LOG_DIR in value.parents
}


def test_prod_rail_covers_every_log_path():
    """Every cfg Path under logs/ must be redirected by the autouse rail.

    The rail used to redirect three of eight. The five it missed were
    module-level constants derived from LOG_DIR at import time, so rebinding
    LOG_DIR moved nothing — and a suite run rewrote the production decision
    journal after a real Drive download.

    This asserts the property rather than the list, so adding a ninth path
    without railing it fails HERE instead of in production.
    """
    assert _REAL_PATHS, "expected cfg to define Paths under logs/"
    unrailed = {
        name: getattr(cfg, name)
        for name in _REAL_PATHS
        if _REAL_LOG_DIR in getattr(cfg, name).parents
    }
    assert not unrailed, (
        "these cfg paths still point at production logs/ during tests: "
        + ", ".join(sorted(unrailed))
    )


def test_drive_is_unreachable_from_the_suite():
    """The Drive rail must raise something `except Exception` cannot eat.

    Identity note: pytest imports conftest as a top-level module, so
    `zebra.tests.conftest.RealDriveAttempted` is a DIFFERENT class object from
    the one the fixture raises. Assert the behaviour (escapes `except
    Exception`, names itself) rather than the class, or the test passes for the
    wrong reason.
    """
    import bcs.drive_store as ds

    raised = None
    try:
        try:
            ds.get_drive_service('whatever')
        except Exception as e:                 # noqa: BLE001 - the point
            pytest.fail(f"Drive rail was swallowed by `except Exception`: {e}")
    except BaseException as e:                 # noqa: BLE001 - the point
        raised = e

    assert raised is not None, "Drive rail did not fire"
    assert type(raised).__name__ == 'RealDriveAttempted'
    assert not isinstance(raised, Exception), \
        "rail must derive from BaseException, not Exception"


class _Store:
    """Minimal store recording which flag API each exit claim used."""

    def __init__(self):
        self.once = []
        self.daily = []

    def set_alert_flag(self, trade_id, kind, persist=True):
        self.once.append((trade_id, kind))
        return True

    def set_alert_flag_daily(self, trade_id, kind, persist=True):
        self.daily.append((trade_id, kind))
        return True


@pytest.mark.parametrize('kind', ['tp', 'trail', 'spot_sl', 'debit_sl'])
def test_live_exit_claims_rearm_daily(monkeypatch, kind):
    """In LIVE the alert IS the exit, so its claim must not be once-ever.

    `_paper_auto_close` returns at its first line when PAPER_MODE is off, so
    nothing books the exit and the position stays `entered`. With a
    one-time-EVER claim, a stop announced once while the owner was away could
    never be announced again — the capped loss quietly becoming the maximum
    loss, which is the shape of both real-money incidents.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    store = _Store()
    assert mon._claim_exit_alert(store, 42, kind) is True
    assert store.daily == [(42, kind)], "live exit claim must re-arm daily"
    assert store.once == [], "live exit claim must not be one-time-ever"


@pytest.mark.parametrize('kind', ['tp', 'trail', 'spot_sl', 'debit_sl'])
def test_paper_exit_claims_stay_once_ever(monkeypatch, kind):
    """PAPER is unchanged: the close books in the same cycle, so a second
    alert would describe a trade that is already shut."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    store = _Store()
    assert mon._claim_exit_alert(store, 42, kind) is True
    assert store.once == [(42, kind)]
    assert store.daily == []


class _BandStore:
    def __init__(self):
        self.cancelled = []
        self.flags = []

    def cancel(self, trade_id, reason):
        self.cancelled.append((trade_id, reason))

    def set_alert_flag_daily(self, trade_id, kind, persist=True):
        self.flags.append((trade_id, kind))
        return True

    def clear_alert_flag(self, trade_id, kind, persist=True):
        pass


def _ticketed_trade():
    return {'id': 7, 'stock': 'M&M', 'direction': 'CE',
            'vet_enter_alerted_at': '2026-08-13T10:00:00'}


def test_live_band_exit_never_cancels_a_ticketed_signal(monkeypatch):
    """A delivered order ticket may already be a funded position.

    The drift/crossed/stale checks run on `triggered` rows too. Cancelling one
    after the ticket went out makes `mark_entered` refuse the row, so the human
    can no longer record the fill — a real position with no record, therefore
    no TP alert, no stop, no expiry nag, in the mode where alerts are the only
    exit mechanism. The likeliest trigger is `crossed`, i.e. the trade WINNING.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    sent = []
    monkeypatch.setattr(mon, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    store = _BandStore()
    mon._band_cancel(store, _ticketed_trade(), 'crossed: gap -0.40% (past ST)')

    assert store.cancelled == [], "a ticketed live signal must not be cancelled"
    assert store.flags == [(7, 'band_exit')]
    assert sent, "the owner must be told the signal left the band"
    assert 'zebra enter 7' in sent[0]


def test_live_band_exit_escapes_html_in_the_symbol(monkeypatch):
    """M&M is a real, currently-open underlying. A bare & in a parse_mode=HTML
    send is what Telegram's own docs forbid, and a rejected send in LIVE means
    the message never arrives."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    sent = []
    monkeypatch.setattr(mon, '_send_telegram',
                        lambda msg, dry_run=False: sent.append(msg) or True)
    mon._band_cancel(_BandStore(), _ticketed_trade(), 'drift')
    assert 'M&amp;M' in sent[0]
    assert 'M&M' not in sent[0]


def test_paper_band_exit_still_cancels(monkeypatch):
    """Paper is unchanged — an unentered signal is worth nothing."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    store = _BandStore()
    mon._band_cancel(store, _ticketed_trade(), 'stale: gap 2.10% < 3.0%')
    assert store.cancelled == [(7, 'stale: gap 2.10% < 3.0%')]
    assert store.flags == []


def test_events_replace_is_denied_to_every_channel_but_events():
    """Deny beats allow in Claude Code, so scoping a globally-denied verb has
    to REMOVE the rule for the one channel whose job it is — otherwise the
    calendar refresh is silently disabled instead of scoped."""
    from zebra import vet as vet_mod

    for channel in ('entry', 'exit', 'review', 'postmortem'):
        denied = vet_mod._denied_tools(channel)
        assert any('events replace' in d for d in denied), \
            f"channel {channel} may replace the shared event calendar"
    assert not any('events replace' in d
                   for d in vet_mod._denied_tools('events')), \
        "the events channel must still be able to install its calendar"
    for channel in ('entry', 'exit', 'review', 'postmortem', 'events'):
        assert any('postmortem run' in d
                   for d in vet_mod._denied_tools(channel)), \
            f"channel {channel} can spawn agents via `postmortem run`"


def test_open_position_cap_binds_in_live_and_not_in_paper(monkeypatch):
    """MAX_OPEN_TRADES was defined, asserted by a test, and read by nothing —
    the only portfolio-level control in the system was decorative, and the
    paper book duly reached 17 open against a stated cap of 8."""
    from zebra.trade_store import ZebraStore

    class _S(ZebraStore):
        def __init__(self, n_open):
            self._trades = [{'id': i, 'status': 'entered'}
                            for i in range(n_open)]

    monkeypatch.setattr(cfg, 'MAX_OPEN_TRADES', 3)

    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    _S(2)._refuse_if_book_full(99)                 # under the cap: fine
    with pytest.raises(ValueError, match='cap is 3'):
        _S(3)._refuse_if_book_full(99)
    with pytest.raises(ValueError):
        _S(9)._refuse_if_book_full(99)

    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    _S(99)._refuse_if_book_full(99)                # paper stays uncapped


# ── cohort boundary ──────────────────────────────────────────────────────

def test_legacy_records_are_out_of_the_cohort_by_absence():
    """The 383 records predating the stamp were priced mid-mid, ran unvetted,
    and came from an engine that no longer exists. Absence IS the answer — a
    reader must never fall back to guessing from `entry_date`."""
    from zebra.trade_store import in_cohort

    assert in_cohort({'cohort': cfg.COHORT_START}) is True
    assert in_cohort({}) is False
    assert in_cohort({'cohort': None}) is False
    assert in_cohort({'entry_date': '2026-09-01'}) is False, \
        "an unstamped record must not be admitted on its entry_date"
    assert in_cohort({'cohort': '2026-01-01'}) is False


def test_cohort_split_puts_the_current_engine_first():
    from zebra.trade_store import cohort_split

    trades = [{'id': 1}, {'id': 2, 'cohort': cfg.COHORT_START},
              {'id': 3, 'cohort': '2020-01-01'}]
    current, legacy = cohort_split(trades)
    assert [t['id'] for t in current] == [2]
    assert [t['id'] for t in legacy] == [1, 3]


def test_cohort_start_is_a_real_date():
    """A typo would silently mis-stamp every future entry and corrupt the only
    measurement the paper run exists to produce."""
    from datetime import datetime
    datetime.strptime(cfg.COHORT_START, '%Y-%m-%d')


def test_a_bad_cohort_start_falls_back_instead_of_stamping_garbage(monkeypatch):
    import importlib
    from zebra import config as c
    monkeypatch.setitem(c._runtime, 'cohort_start', 'not-a-date')
    assert c._cohort_start() == c._DEFAULTS['cohort_start']
    monkeypatch.setitem(c._runtime, 'cohort_start', None)
    assert c._cohort_start() == c._DEFAULTS['cohort_start']


def test_the_cohort_stamp_is_frozen_not_recomputed(monkeypatch):
    """Moving the boundary later must not reclassify positions already open —
    same rule as `pricing_basis`, and for the same reason."""
    from zebra.trade_store import ZebraStore, in_cohort

    t = {}
    monkeypatch.setattr(cfg, 'COHORT_START', '2026-08-14')
    ZebraStore._stamp_cohort(None, t)
    assert t['cohort'] == '2026-08-14'
    # The operator moves the boundary; the open trade keeps its own stamp...
    monkeypatch.setattr(cfg, 'COHORT_START', '2026-10-01')
    assert t['cohort'] == '2026-08-14'
    # ...and is correctly no longer counted in the NEW cohort.
    assert in_cohort(t) is False


# ── cohort-only alerting ─────────────────────────────────────────────────

def _legacy(**o):
    t = {'structure': 'bcs', 'status': 'entered'}
    t.update(o)
    return t


def _current(**o):
    t = {'structure': 'bcs', 'status': 'entered', 'cohort': cfg.COHORT_START}
    t.update(o)
    return t


def test_cohort_only_silences_legacy_and_keeps_current(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', True)
    monkeypatch.setattr(cfg, 'ALERT_STRUCTURES', ['bcs'])
    assert mon._alerts_enabled(_current()) is True
    assert mon._alerts_enabled(_legacy()) is False


def test_cohort_gate_is_off_when_the_switch_is_off(monkeypatch):
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', False)
    monkeypatch.setattr(cfg, 'ALERT_STRUCTURES', ['bcs'])
    assert mon._alerts_enabled(_legacy()) is True


def test_live_mode_never_silences_anything(monkeypatch):
    """A pre-cohort record cannot be proven to carry no real money from the
    record alone, and in LIVE the alert IS the exit instruction. So the live
    override stays absolute — the cohort gate is a paper-mode luxury, exactly
    like alert_structures."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', True)
    monkeypatch.setattr(cfg, 'ALERT_STRUCTURES', [])
    assert mon._alerts_enabled(_legacy()) is True
    assert mon._alerts_enabled(_current()) is True


def test_silencing_an_alert_does_not_silence_the_EXIT(monkeypatch):
    """THE property this whole feature stands on.

    `_alerts_enabled` gates the SEND. `_paper_auto_close` runs regardless, so a
    silenced legacy position still books its exits and still records P&L. If
    this ever inverts, a notification preference has quietly become a trading
    halt on 25 open positions.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', True)
    monkeypatch.setattr(cfg, 'ALERT_STRUCTURES', ['bcs'])
    monkeypatch.setattr(mon, '_send_telegram',
                        lambda *a, **k: pytest.fail("legacy trade Telegrammed"))

    booked = []

    class _S:
        def clear_alert_flag(self, *a, **k):
            pytest.fail("a silenced alert must not release its claim")

    trade = _legacy(id=5, stock='M&M', direction='CE')
    mon._send_exit_alert(_S(), trade, 'tp', 'msg')      # must not send, not raise

    # And the close path is reached independently of the send.
    import inspect
    src = inspect.getsource(mon.check_entered)
    for kind in ('tp', 'spot_sl', 'debit_sl'):
        i_send = src.index(f"_send_exit_alert(store, trade, '{kind}'")
        i_close = src.index(f"_paper_auto_close(store, trade, mid, '{kind}'")
        assert i_close > i_send, f"{kind}: close must follow, not depend on, the send"
    assert booked == []


# ── EOD consolidated-position switch ─────────────────────────────────────

def _report(open_n=2):
    opens = [{'id': i, 'stock': 'M&M', 'direction': 'CE', 'long_strike': 3450,
              'short_strike': 3550, 'structure': 'bcs'} for i in range(open_n)]
    return {'type': 'daily', 'date': '2026-08-14', 'closed': [], 'open': opens,
            'closed_summary': {'count': 0}, 'unrealized': {}}


def test_eod_lists_positions_only_when_switched_on(monkeypatch):
    from zebra import report as rep

    monkeypatch.setattr(cfg, 'EOD_OPEN_POSITIONS', False)
    off = rep.format_telegram(_report())
    monkeypatch.setattr(cfg, 'EOD_OPEN_POSITIONS', True)
    on = rep.format_telegram(_report())

    assert 'Open:</b> 2 pos' in off, "the COUNT must always go out"
    assert 'M&amp;M' not in off, "the per-position listing must be gone"
    assert 'M&amp;M' in on, "and must come back when switched on"
    assert len(off.splitlines()) < len(on.splitlines())


def test_eod_escapes_the_symbol_in_both_blocks(monkeypatch):
    """The EOD report is parse_mode=HTML too, and M&M is in this book."""
    from zebra import report as rep

    monkeypatch.setattr(cfg, 'EOD_OPEN_POSITIONS', True)
    r = _report(1)
    r['closed'] = [{'id': 9, 'stock': 'M&M', 'direction': 'CE', 'pnl': 500,
                    'long_strike': 3450, 'short_strike': 3550,
                    'exit_reason': 'paper:tp', 'structure': 'bcs'}]
    r['closed_summary'] = {'count': 1, 'wins': 1, 'losses': 0, 'net_pnl': 500,
                           'win_rate': 100.0, 'avg_hold_days': 3.0}
    msg = rep.format_telegram(r)
    assert 'M&amp;M' in msg
    assert '>M&M<' not in msg


def test_reports_are_scoped_to_the_cohort(monkeypatch):
    from zebra import report as rep

    trades = [_current(id=1), _legacy(id=2)]
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', True)
    assert [t['id'] for t in rep._reportable(trades)] == [1]
    monkeypatch.setattr(cfg, 'ALERTS_COHORT_ONLY', False)
    assert [t['id'] for t in rep._reportable(trades)] == [1, 2]


@pytest.mark.parametrize('fmt,args', [
    ('_format_tp_alert', {'spot': 3500.0, 'mid': 12.0}),
    ('_format_spot_sl_alert', {'spot': 3200.0, 'mid': 4.0}),
])
def test_exit_alert_formatters_escape_the_symbol(monkeypatch, fmt, args):
    """Every runtime value in a parse_mode=HTML send must be escaped.

    131 of the 132 symbols in the book are plain alphanumerics, which is why
    the one that is not went unnoticed — and M&M is open right now.
    """
    monkeypatch.setattr(cfg, 'PAPER_MODE', False)
    trade = {'stock': 'M&M', 'direction': 'CE', 'tp_spot': 3565.24,
             'sl_spot': 3200.0, 'entry_spot': 3400.0, 'structure': 'bcs',
             'long_symbol': 'M&M26AUG3450CE', 'short_symbol': 'M&M26AUG3550CE',
             'debit': 20.0, 'debit_sl_value': 10.0, 'quantity': 250}
    msg = getattr(mon, fmt)(trade, **args)
    assert 'M&amp;M' in msg
    for bad in ('M&M2', '  M&M '):
        assert bad not in msg
