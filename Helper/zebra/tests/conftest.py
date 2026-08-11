"""Test-wide safety rails.

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

from zebra import vet as vet_mod          # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_agents(monkeypatch, request):
    """Replace every process-spawning entry point with a recorder.

    Patched at the lowest common point (`_spawn_generic`) AND at `_spawn_cli`,
    because callers reach the CLI through both and a future caller might use
    either. Returns a list of (tag, model) so tests can assert that a spawn was
    ATTEMPTED without one ever happening.
    """
    calls = []

    def fake_generic(prompt, model, tag):
        calls.append((tag, model))
        return 4242                       # a plausible pid; nothing was started

    def fake_cli(trade_id, exit_kind=None):
        return fake_generic('', vet_mod.cfg.VET_MODEL,
                            'vet #%s%s' % (trade_id,
                                           ' ' + exit_kind if exit_kind else ''))

    monkeypatch.setattr(vet_mod, '_spawn_generic', fake_generic)
    monkeypatch.setattr(vet_mod, '_spawn_cli', fake_cli)
    request.node._spawn_calls = calls
    return calls


@pytest.fixture
def spawns(_no_real_agents):
    """Opt-in view of the spawn recorder for tests that assert on spawning."""
    return _no_real_agents
