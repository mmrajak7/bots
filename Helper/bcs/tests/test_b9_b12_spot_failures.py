"""B9 + B12 — the two failure classes of the per-trade spot fetch.

One `try/except` produced both defects, which is why they are one fix:

* **B9** — a dead Kite token. `get_spot` is the FIRST Kite call per trade, and
  its exception was swallowed per-trade, so the outer handler that detects a
  token error and Telegrams "UNMONITORED" was unreachable from the path most
  likely to raise. Token dies at 11:00 with open positions -> log lines until
  15:30, no alert, every stop dark. On expiry day the 15:15 force-close never
  fires, which is a physical delivery obligation.
* **B12** — anything else (a renamed or garbled `spot_symbol`). Same silent
  `continue`, but only that record is affected. SL_SPOT, SL_SPREAD and TP all
  read spot, so a spot-blind record has NO live trigger at all while the
  process reports healthy.

They need OPPOSITE responses — escalate globally vs isolate and escalate
per-record — which is exactly the distinction `feedback_never_asked_is_not_failed`
says must never share a handler.

Run:  cd Helper && python -m pytest bcs/tests/test_b9_b12_spot_failures.py -v
"""
import inspect
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm       # noqa: E402


# ── The shared predicate ─────────────────────────────────────────────────────

@pytest.mark.parametrize('msg', [
    'Incorrect `api_key` or `access_token`.',
    'Token is invalid or has expired.',
    'InvalidToken',
    'SessionExpired',
    'kite.exceptions.TokenException: Invalid session',
])
def test_auth_errors_are_recognised(msg):
    assert sm._is_auth_error(Exception(msg)) is True


@pytest.mark.parametrize('msg', [
    "KeyError: 'NSE:RENAMEDCO'",
    'Connection aborted',
    'Read timed out',
    'no depth for symbol',
    '',
])
def test_non_auth_errors_are_not_mistaken_for_auth(msg):
    """Negative control for the predicate.

    If this over-matched, every transient network blip would be escalated as
    a dead token and return out of monitor_all — turning a 2-second hiccup
    into an unmonitored book, which is worse than the bug being fixed.
    """
    assert sm._is_auth_error(Exception(msg)) is False


def test_the_outer_handlers_use_the_shared_predicate():
    """Both loops must call _is_auth_error, not re-inline the string test.

    Two copies of a predicate drift. The per-trade path could not see the
    failure the outer path existed for precisely because the test lived in
    only one of them.
    """
    for fn in (sm.monitor, sm.monitor_all):
        src = inspect.getsource(fn)
        assert '_is_auth_error' in src, (
            f"{fn.__name__} must classify auth errors via _is_auth_error")
        assert "'sessionexpired' in err_str" not in src, (
            f"{fn.__name__} still has an inlined copy of the auth predicate")


# ── B9: the auth class must escalate, not be swallowed ───────────────────────

def test_an_auth_error_in_the_spot_fetch_is_reraised_not_swallowed():
    """The per-trade handler must let auth errors through to the outer one."""
    src = inspect.getsource(sm.monitor_all)
    m = re.search(r'spot = get_spot\(.*?\n\s*except Exception as e:\n(.*?)\n\s*track_spot_blindness\(\s*\n\s*spot_blind_state\[close_key\], spot_ok=True',
                  src, re.S)
    assert m, "the spot-fetch try/except is not in the expected shape"
    handler = m.group(1)
    assert re.search(r'if _is_auth_error\(e\):\s*\n(\s*#.*\n)*\s*raise', handler), (
        "an auth error in the per-trade spot fetch must `raise` so the outer "
        "handler's UNMONITORED alert is reachable; swallowing it is B9")


# ── B12: everything else isolates and escalates per record ───────────────────

def _spot_except_handler(src: str) -> str:
    """The body of the per-trade spot-fetch `except`, and nothing else."""
    # Anchor on the PER-TRADE call specifically. monitor_all has other
    # get_spot call sites (the expiry-proximity check), and a loose anchor
    # matched one of those instead.
    m = re.search(r"spot = get_spot\(kite, trade\['spot_symbol'\]\)"
                  r".*?\n\s*except Exception as e:\n(.*?)\n\s*continue\n",
                  src, re.S)
    assert m, "the spot-fetch try/except is not in the expected shape"
    return m.group(1)


def test_a_non_auth_spot_failure_is_tracked_per_trade():
    """The tracking call must be in the FAILURE path, not merely in the file.

    First draft of this test asserted `track_spot_blindness(` appeared
    anywhere in monitor_all — which the success-path call satisfies on its
    own, so deleting the failure-path call left the test green. Scope it to
    the except handler.
    """
    src = inspect.getsource(sm.monitor_all)
    assert 'spot_blind_state' in src, (
        "B12 needs per-trade spot-blind state; without it a renamed "
        "spot_symbol is silent forever")
    handler = _spot_except_handler(src)
    assert 'track_spot_blindness(' in handler, (
        "the non-auth branch of the spot-fetch handler must call "
        "track_spot_blindness — a bare log() and continue is B12")
    assert 'spot_ok=False' in handler, (
        "the failure path must report spot_ok=False")


def test_the_success_path_also_reports_so_the_alarm_can_clear():
    """Without this the blind clock never resets and the alert is permanent."""
    src = inspect.getsource(sm.monitor_all)
    assert re.search(r'track_spot_blindness\(\s*\n?\s*spot_blind_state\[close_key\],\s*spot_ok=True',
                     src), "no success-path call; a recovered symbol stays flagged"


def test_spot_blind_escalates_harder_than_spread_blind():
    """A spot-blind trade has NO live trigger; a spread-blind one still has
    SL_SPOT. The clocks must reflect that, or the more severe condition is
    reported more slowly than the less severe one."""
    assert sm.SPOT_BLIND_ALERT_SEC < sm.SPREAD_BLIND_ALERT_SEC
    assert sm.SPOT_BLIND_REPEAT_SEC < sm.SPREAD_BLIND_REPEAT_SEC


def test_the_spot_blind_tracker_alerts_after_the_threshold(monkeypatch):
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    bs = sm.new_blind_state()
    t0 = 1_000_000.0
    monkeypatch.setattr(sm.time, 'time', lambda: t0)
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    assert sent == [], "must not alert on the first failed poll"

    monkeypatch.setattr(sm.time, 'time', lambda: t0 + sm.SPOT_BLIND_ALERT_SEC + 1)
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    assert len(sent) == 1
    assert 'NO live triggers' in sent[0]


def test_the_spot_blind_tracker_is_silent_below_the_threshold(monkeypatch):
    """Negative control for the test above: same calls, clock not advanced."""
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    bs = sm.new_blind_state()
    t0 = 1_000_000.0
    monkeypatch.setattr(sm.time, 'time', lambda: t0)
    for _ in range(50):
        sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    assert sent == [], "alerted without the clock advancing — the timer is inert"


def test_recovery_is_announced_and_only_after_a_streak(monkeypatch):
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    bs = sm.new_blind_state()
    t0 = 1_000_000.0
    monkeypatch.setattr(sm.time, 'time', lambda: t0)
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    monkeypatch.setattr(sm.time, 'time', lambda: t0 + sm.SPOT_BLIND_ALERT_SEC + 1)
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    sent.clear()

    # One good poll inside a flickering book must not clear the alarm.
    for i in range(sm.BLIND_CLEAR_OK_POLLS - 1):
        sm.track_spot_blindness(bs, True, '', 'BCS #1 TESTCO')
        assert sent == [], f"recovered after only {i + 1} ok poll(s)"
    sm.track_spot_blindness(bs, True, '', 'BCS #1 TESTCO')
    assert len(sent) == 1 and 'RECOVERED' in sent[0]
    assert bs['since'] is None


def test_a_transient_blip_is_silent_in_both_directions(monkeypatch):
    """The spam bug: one failed poll set `since`, and three good polls later
    the all-clear fired for an alarm that had never rung.

    This is the real shape of a Kite hiccup — a single 7s read timeout or a
    503 burst — and at a 5s poll it produced a RECOVERED Telegram roughly
    every 15 seconds, multiplied by every open position. Below the alert
    threshold the tracker must say NOTHING, either way.
    """
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    bs = sm.new_blind_state()
    t = [1_000_000.0]
    monkeypatch.setattr(sm.time, 'time', lambda: t[0])

    for _ in range(20):                      # twenty separate blips
        sm.track_spot_blindness(bs, False, 'Read timed out.', 'BCS #1 TESTCO')
        t[0] += 5
        for _ in range(sm.BLIND_CLEAR_OK_POLLS):
            sm.track_spot_blindness(bs, True, '', 'BCS #1 TESTCO')
            t[0] += 5
    assert sent == [], f"transient blips produced {len(sent)} alert(s): {sent}"


def test_the_next_outage_alerts_on_its_own_clock(monkeypatch):
    """Clearing must reset the repeat timer too, or the FIRST alert of the
    next outage is swallowed by the previous spell's SPOT_BLIND_REPEAT_SEC."""
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    bs = sm.new_blind_state()
    t = [1_000_000.0]
    monkeypatch.setattr(sm.time, 'time', lambda: t[0])

    # Spell 1: blind past the threshold -> alert, then recover.
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    t[0] += sm.SPOT_BLIND_ALERT_SEC + 1
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    assert len(sent) == 1 and 'NO live triggers' in sent[0]
    for _ in range(sm.BLIND_CLEAR_OK_POLLS):
        t[0] += 5
        sm.track_spot_blindness(bs, True, '', 'BCS #1 TESTCO')
    assert len(sent) == 2 and 'RECOVERED' in sent[1]

    # Spell 2 starts well inside the old repeat window.
    t[0] += 60
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    t[0] += sm.SPOT_BLIND_ALERT_SEC + 1
    sm.track_spot_blindness(bs, False, 'KeyError', 'BCS #1 TESTCO')
    assert len(sent) == 3 and 'NO live triggers' in sent[2], (
        "the second outage was silenced by the first spell's repeat clock")


# ── The error budget ─────────────────────────────────────────────────────────

def test_the_error_budget_is_reset_at_the_bottom_of_the_loop():
    """`consecutive_errors = 0` at the TOP meant "we reached the loop", not
    "an iteration succeeded" — so any per-iteration raise went 0->1 forever
    and MAX_CONSECUTIVE_ERRORS was unreachable for exactly the failures it
    exists for."""
    src = inspect.getsource(sm.monitor_all)
    trade_loop = src.index('for trade in all_trades:')
    loop_top = src.index('while True:')

    # Trailing comments must NOT defeat this. The line being fixed was
    # literally `consecutive_errors = 0  # Reset on successful iteration`, so
    # a `\s*$` anchor would have missed the original bug entirely.
    at = [m.start() for m in re.finditer(
        r'^[ \t]*consecutive_errors = 0[ \t]*(#.*)?$', src, re.M)]
    assert at, "consecutive_errors is never zeroed"

    # One occurrence before `while True:` is the legitimate initialisation.
    # What must NOT exist is a reset between the top of the poll loop and the
    # per-trade loop — that is the placement that made the counter mean "we
    # reached the loop" instead of "an iteration succeeded".
    in_loop_head = [p for p in at if loop_top < p < trade_loop]
    assert not in_loop_head, (
        "consecutive_errors is reset between the top of the poll loop and the "
        "per-trade loop; that is the B9 placement and it makes "
        "MAX_CONSECUTIVE_ERRORS unreachable")

    assert any(p > trade_loop for p in at), (
        "consecutive_errors must be reset AFTER the per-trade loop completes")


# ── Startup hardening ────────────────────────────────────────────────────────

def test_the_startup_positions_call_is_guarded():
    """An unwrapped raise here exits main(), cron restarts, it dies again —
    all day, with no alert at all."""
    src = inspect.getsource(sm.monitor_all)
    m = re.search(r'try:\s*\n\s*positions = kite\.positions\(\)', src)
    assert m, "the startup kite.positions() call must be inside a try"


def test_a_stale_token_at_startup_telegrams(monkeypatch, tmp_path):
    """A log line is not an alert when the process runs unattended."""
    import json
    from datetime import datetime, timedelta
    tok = tmp_path / 'kite_access_token.json'
    old = (datetime.now() - timedelta(days=3)).isoformat()
    tok.write_text(json.dumps({'api_key': 'k', 'access_token': 'a',
                               'generated_at': old}))
    monkeypatch.setattr(sm, 'TOKEN_FILE', tok)
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sm, 'KiteConnect', lambda api_key: type(
        'K', (), {'set_access_token': lambda self, t: None})())
    sm.load_kite()
    assert len(sent) == 1 and 'STALE KITE TOKEN' in sent[0]


def test_a_fresh_token_at_startup_is_silent(monkeypatch, tmp_path):
    """Negative control: same path, today's token, no alert."""
    import json
    from datetime import datetime
    tok = tmp_path / 'kite_access_token.json'
    tok.write_text(json.dumps({'api_key': 'k', 'access_token': 'a',
                               'generated_at': datetime.now().isoformat()}))
    monkeypatch.setattr(sm, 'TOKEN_FILE', tok)
    sent = []
    monkeypatch.setattr(sm, 'send_telegram', lambda m, *a, **k: sent.append(m))
    monkeypatch.setattr(sm, 'KiteConnect', lambda api_key: type(
        'K', (), {'set_access_token': lambda self, t: None})())
    sm.load_kite()
    assert sent == []
