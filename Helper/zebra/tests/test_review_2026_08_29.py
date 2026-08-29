"""Defects found by the 2026-08-29 adversarial review of that day's own commits.

Four commits shipped, three reviewers read them, and the sharpest findings were
against the work of the same session. Each test below is one of them.

The unifying shape, again: **a rule implemented in one of the two places that
enforce it.** M9 moved a LIVE slot cap and the paper exemption existed only in
the store, not in the pre-gate that decides vetting. M7 moved the clock and two
of twenty-four reads did not come along — and the guard that was supposed to
catch that enumerated the shapes it already knew about.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_review_2026_08_29.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import capital                     # noqa: E402
from zebra import config as cfg               # noqa: E402
from zebra import monitor as monitor_mod      # noqa: E402


# ── #1 · PAPER must not stop vetting when the LIVE slot cap binds ───────────

def test_a_capital_refusal_in_paper_does_not_skip_the_vet():
    """THE regression. `max_open_trades` 8 -> 4 (a LIVE decision) against a
    book holding 6 open PAPER positions refused every new signal in the
    pre-gate — which skips the Claude vet and swaps the order ticket for a
    capital notice — while the store's paper exemption let the record enter
    anyway. A paper trade entered with NO VERDICT, and the vetting pipeline
    THE GOAL exists to validate going dark, from a config change that was
    documented as live-only.

    `ZebraStore._refuse_if_over_budget` already had the rule: "LIVE refuses.
    PAPER evaluates and LOGS what it WOULD have refused." The pre-gate is the
    other place the cap is enforced and did not.
    """
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    body = src[src.index('capital_refused = bool(cap_plan)'):]
    body = body[:body.index('vet_skipped')]
    assert 'cfg.PAPER_MODE' in body, (
        'the capital pre-gate does not distinguish paper from live — a LIVE '
        'slot cap will silently stop the paper book being vetted')
    assert 'capital_refused = False' in body, (
        'paper must SHADOW-LOG the refusal, not act on it')
    assert 'WOULD REFUSE' in body, (
        'the shadow evidence line is gone — that log is how the rupee '
        'numbers get chosen from data instead of guessed')


def test_the_cap_still_refuses_when_it_is_LIVE():
    """The negative control. Paper is exempt; live is not, or M9 bought
    nothing."""
    src = Path(monitor_mod.__file__).read_text(encoding='utf-8')
    assert 'elif capital_refused:' in src
    assert 'no vet spent, no order ticket' in src


def test_the_capital_layer_still_evaluates_in_paper():
    """The exemption is in what we DO with the answer, never in whether we
    ask. An exemption that skipped the check would mean the capital layer has
    never run when it first becomes load-bearing — the exit bridge's mistake
    in a different file."""
    lim = capital.limits([])
    book = [{'status': 'entered', 'stock': 'S%d' % i, 'capital': 1000.0}
            for i in range(cfg.MAX_OPEN_TRADES + 2)]
    ok, why = capital.check(book, {'stock': 'NEW', 'debit': 1.0,
                                   'lot_size': 100}, lim)
    assert ok is False and 'max_open_trades' in why


# ── #10 · a warning that can only arrive after the thing it warns about ────

def test_the_expiry_warning_starts_BEFORE_the_trades_own_time_stop():
    """A flat `EXPIRY_WARN_SESSIONS = 5` became a silent no-op for the cohort
    the moment M10 moved its time stop to 6: the stop fires at 6, the warn
    only starts at 5, so the position is closed before its own "expiry is
    close" nag can fire. Nothing would have said so — it just stops
    appearing."""
    from bcs import spread_monitor as sm

    cohort = {'time_stop_sessions': cfg.TIME_SL_DAYS}
    assert sm.expiry_warn_sessions(cohort) > cfg.TIME_SL_DAYS, (
        'the expiry warning can never fire before the time stop closes the '
        'position it is warning about')


def test_a_default_policy_book_keeps_the_old_warning_window():
    """The three non-cohort books close on expiry day itself, so 5 is right
    for them. Deriving per-record must not move them."""
    from bcs import spread_monitor as sm

    assert sm.expiry_warn_sessions({}) == sm.EXPIRY_WARN_SESSIONS


def test_the_adapter_says_the_time_stop_is_read_LIVE_not_frozen():
    """It is stamped at MAP time from the live config, so a config change
    retimes OPEN positions — which is exactly what 5 -> 6 did to the six open
    cohort records. Safe in that direction (M10: the count is a FLOOR, never
    a ceiling) and dangerous in the other, so the comment must say what the
    code does rather than the reassuring opposite."""
    from bcs import zebra_adapter

    src = Path(zebra_adapter.__file__).read_text(encoding='utf-8')
    block = src[src.index('ZEBRA_EXIT_POLICY'):src.index("'time_stop_sessions'")]
    assert 'carried on the record rather than read' not in block, (
        'the comment still claims the N is frozen at entry; it is not')


# ── #12 · a switch a running process cannot see is not a switch ────────────

def test_the_telegram_switch_is_read_LIVE():
    """M8 fixed the LAYER (it read the overlay alone) and, in the first pass,
    broke the TIMING by freezing the value at import — `zebra loop` is
    long-lived, so an operator muting alerts mid-session would have had to
    restart the process. One defect traded for another."""
    assert callable(cfg.telegram_enabled)
    assert not hasattr(cfg, 'TELEGRAM_ENABLED'), (
        'the import-time constant is back')


def test_only_an_explicit_false_mutes_the_alerts(monkeypatch, tmp_path):
    """`bool(None)` would have let a null key mute a safety channel. Absence
    has always meant SEND."""
    import json

    from common import layered_config

    for value, expected in ((False, False), (True, True),
                            (None, True), ('__absent__', True)):
        block = {} if value == '__absent__' else {'enabled': value}
        monkeypatch.setattr(
            layered_config, 'load',
            lambda *a, _b=block, **k: {'telegram': dict(_b)})
        assert cfg.telegram_enabled() is expected, value


def test_an_unreadable_config_never_mutes(monkeypatch):
    from common import layered_config

    def _boom(*a, **k):
        raise OSError('gone')
    monkeypatch.setattr(layered_config, 'load', _boom)
    assert cfg.telegram_enabled() is True
