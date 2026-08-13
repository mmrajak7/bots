"""Test-wide safety rails.

## No test may send a real Telegram

Reported by the owner 2026-08-12, mid-session: "i am getting too many test
messages like this" — a VETOED alert for TESTCO, the fixture symbol. Every
suite run had been messaging his phone, because `cmd_vet_decide` sends the veto
alert with no dry_run and `test_vet_cli.py` drives that function directly.

Exactly the same shape as the spawn incident below, in a different channel: a
test reaching out and touching production. Nothing was lost but the owner's
attention, and an alert channel that cries wolf is worse than no channel — the
one message that matters is the one he will scroll past.

Blocked here rather than in the offending file for the same reason as the
spawn rail: patching it per-file leaves the NEXT test free to do it again.
Sending is impossible by default; a test that wants to observe it passes its
own `send=` or asserts on the recorder.

## No test may spawn a real Claude CLI

Discovered the hard way on 2026-08-11: `monitor._exit_cleared` calls
`vet.exit_gate` with `spawn` defaulting to True, so every test that exercised
the monitor's exit path launched a REAL detached `claude` process. Those agents
inherited the production cwd and config, ran `python -m zebra vet show 1`
against the LIVE store, found a cancelled ABB signal, and wrote ~30 junk rows
into the production decision journal — one more on every suite run, plus the
token cost, plus a scoring record polluted with decisions about a trade that
never existed.

Nothing was lost (the agents correctly refused to act on a signal with no
pending marker, so the trade store was never touched), but a test suite that
reaches out and touches production is a bug regardless of the blast radius.

Patching this at the fixture level in each file would leave the next test — the
one nobody remembers to isolate — free to do it again. So the block lives here,
autouse and package-wide: the default is that spawning is impossible, and a
test that genuinely wants to observe spawn behaviour must opt in via the
`spawns` fixture and assert on the recorded calls.
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg           # noqa: E402
from zebra import monitor as monitor_mod  # noqa: E402
from zebra import vet as vet_mod          # noqa: E402


@pytest.fixture(autouse=True)
def _no_production_paths(tmp_path_factory, monkeypatch):
    """No test may write the production logs, store, or watchdog state.

    Third instance of the same class, after real Telegrams and real spawns.
    `test_review_fixes.py::test_the_spawn_grants_tools_on_argv_not_via_settings`
    does not take the `paths` fixture, and forces `resolve_cli` back to a real
    path so it can inspect argv. Popen is stubbed, so nothing was launched — but
    execution still reached `vet.py:315`, which mkdir'd the production
    `Helper/logs/`, appended a banner to the real `vet_cli_YYYYMMDD.log`, and
    called `_note_spawn(True, 'entry')` -> `health.record_spawn_result` -> the
    real `auth_health.json`.

    Measured: that counter read `spawns_since_landing: 73`, accumulated ~3 per
    suite run over ~24 runs. `SILENT_SPAWN_LIMIT` is 5, so `health.check` would
    have Telegrammed "CLAUDE AGENTS NOT REPORTING BACK" on the first live cycle
    — the one detector built to catch "the layer is dead while the switch reads
    ON", made to cry wolf by the test suite, and clearable only by a real
    landing. The number was also useless as evidence: it measured pytest.

    Railed at the SOURCE and autouse, not per test: `paths` fixes the files one
    test knows about, and the next test nobody remembers to isolate walks
    straight past it. Both modules read `cfg.LOG_DIR` at call time, so this one
    redirect covers every writer. A test wanting real paths must say so.
    """
    tmp = tmp_path_factory.mktemp('zebra_prod_rail')
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp / 'zebra_trades.lock')
    return tmp


@pytest.fixture(autouse=True)
def _pinned_vet_flag(monkeypatch):
    """The suite must not read the MACHINE's live vetting switch.

    Found 2026-08-13 on the Pi: five tests failed there and passed here, on the
    same commit. `cfg.VET_ENABLED` is read from the real `zebra_config.json` at
    import, and the Pi has had vetting ON since 22:45 on the 12th while this
    dev box has no vet key at all. Every price-driven exit goes through
    `_exit_cleared` -> `vet.exit_gate`, which returns 'wait' when the flag is
    set, so on the Pi the exits under test simply never fired.

    Fourth instance of the same class as the rails above, and the subtlest:
    the earlier three had tests reaching OUT to production, this one has
    production reaching IN. A suite whose result depends on the config of the
    box it runs on cannot certify a deploy — which is exactly what it was
    being used for.

    Reproduce either direction with `ZEBRA_VET_ENABLED=1 pytest` (env wins over
    the file, see config.py:561).

    Pinned FALSE because that is the property most tests actually want to
    assert: what the deterministic engine does. The 14 sites that exercise the
    gate set it True explicitly, and their own fixtures run after this one.
    """
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)


@pytest.fixture(autouse=True)
def _value_triggers_awake(monkeypatch):
    """The suite must not depend on WHAT TIME OF DAY it runs.

    The other half of the 2026-08-13 Pi discrepancy, and the reason last
    night's "456 passed" was not reproducible this morning: `_value_triggers_live`
    compares `datetime.now(IST)` against the open, so DEBIT-SL and TRAIL are
    dark before 09:15 + the buffer. Every run last night was after the close
    (True); the 08:40 run today was before the open (False), and the three
    book-driven exit tests silently changed answer. Nothing in the code moved.

    A green suite that is only green after 09:30 cannot gate a deploy, and the
    failure is worse than a plain flake — it looks exactly like a real
    regression in the exit path, on the morning you are least able to afford
    the doubt.

    Delegating rather than blanket-True on purpose: `test_second_source`
    asserts the real boundary by passing an explicit `now`, and that is the
    test that actually pins the buffer's behaviour. Only the implicit
    "whatever o'clock it happens to be" call is forced awake. Tests wanting
    the dark path patch this attribute themselves; a test-body monkeypatch
    runs after fixtures and wins.
    """
    real = monitor_mod._value_triggers_live
    monkeypatch.setattr(
        monitor_mod, '_value_triggers_live',
        lambda now=None: real(now) if now is not None else True)


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch, request):
    """Every Telegram send becomes a recording. Returns the list of messages."""
    sent = []

    def fake_send(msg, dry_run=False):
        sent.append(msg)
        return True

    monkeypatch.setattr(monitor_mod, '_send_telegram', fake_send)
    request.node._telegram = sent
    return sent


@pytest.fixture
def telegrams(_no_real_telegram):
    """Opt-in view for tests that assert on what was sent."""
    return _no_real_telegram


class RealTelegramAttempted(BaseException):
    """Deliberately a BaseException, not an Exception.

    The production senders are wrapped in `except Exception` — correctly, since
    a Telegram failure must never make an agent think its verdict did not land.
    That same handler swallows a plain-Exception rail and logs it as a warning,
    so the suite passes, nobody sees it, and the messages keep arriving. A test
    rail that production error handling can catch is not a rail.
    """


@pytest.fixture(autouse=True)
def _no_telegram_http(monkeypatch):
    """Backstop at the NETWORK call, not just at our wrapper.

    Patching `_send_telegram` covers the paths that go through it. This covers
    the ones that do not: a future sender, a copy-pasted requests.post, a module
    that imported the function by value. One SOURCE beats N call sites —
    counting sources rather than checks is the lesson from the guard audit.

    It raises rather than no-ops: a silent stub would let a real bug ('we never
    send anything') pass as healthy.
    """
    import requests
    real_post = requests.post

    def guarded(url, *a, **k):
        if 'api.telegram.org' in str(url):
            raise RealTelegramAttempted(
                'a test tried to send a REAL Telegram: %s' % str(url)[:60])
        return real_post(url, *a, **k)

    monkeypatch.setattr(requests, 'post', guarded)


@pytest.fixture(autouse=True)
def _no_real_agents(monkeypatch, request):
    """Replace every process-spawning entry point with a recorder.

    Patched at the lowest common point (`_spawn_generic`) AND at `_spawn_cli`,
    because callers reach the CLI through both and a future caller might use
    either. Returns a list of (tag, model) so tests can assert that a spawn was
    ATTEMPTED without one ever happening.
    """
    calls = []

    def fake_generic(prompt, model, tag, channel='entry'):
        calls.append((tag, model))
        return 4242                       # a plausible pid; nothing was started

    def fake_cli(trade_id, exit_kind=None):
        return fake_generic('', vet_mod.cfg.VET_MODEL,
                            'vet #%s%s' % (trade_id,
                                           ' ' + exit_kind if exit_kind else ''),
                            channel='exit' if exit_kind else 'entry')

    monkeypatch.setattr(vet_mod, '_spawn_generic', fake_generic)
    monkeypatch.setattr(vet_mod, '_spawn_cli', fake_cli)
    request.node._spawn_calls = calls
    return calls


@pytest.fixture
def spawns(_no_real_agents):
    """Opt-in view of the spawn recorder for tests that assert on spawning."""
    return _no_real_agents


# The pre-patch function object, captured once at import. A test that needs to
# exercise the REAL spawn logic (argv assembly, CLI resolution, failure
# bookkeeping) cannot reach it through the module attribute — the autouse rail
# above has already replaced it, which is the point.
_REAL_SPAWN_GENERIC = vet_mod._spawn_generic


@pytest.fixture
def real_spawn(monkeypatch):
    """The unpatched `_spawn_generic`, for failure-path tests ONLY.

    Returns it only after forcing `resolve_cli` to None, so the function is
    guaranteed to take the not-found branch and return before it can reach
    Popen. There is deliberately no way to get a version of this that could
    start a process: the rail stays absolute.
    """
    monkeypatch.setattr(vet_mod, 'resolve_cli', lambda refresh=False: None)
    return _REAL_SPAWN_GENERIC
