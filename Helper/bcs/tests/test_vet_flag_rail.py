# -*- coding: utf-8 -*-
"""N4 — the vetting-switch rail existed on one side of the suite only.

`zebra/tests/conftest.py` has pinned `cfg.VET_ENABLED` False since 2026-08-13,
when five tests failed on the Pi and passed on the dev box on the same commit:
the flag is resolved from config at import, the Pi had vetting ON, and the
gated exits under test simply never fired there. A suite whose answer depends
on the config of the box it runs on cannot certify a deploy.

`bcs/tests/` had no equivalent — and it reaches the same flag:

    bcs/exit_vet.py::exit_cleared
      -> zebra.monitor._exit_cleared
        -> zebra.vet.exit_gate      # first line: if not cfg.VET_ENABLED: proceed

so every `_close_spread_inner` test on a `_store_type: 'zebra'` trade has been
taking whichever branch the machine happened to be configured for. With the
flag ON that call is not read-only: it writes vet markers into the store under
test and can spawn a Claude agent.

It went latent-to-live on 2026-08-27, when H3 moved the switch into tracked
config and flipped the CODE default False -> True so it would be auditable
from git. The resolved value on a box with no overlay key changed that day,
silently, and only for these tests. Both states are green today. A rail on one
side of a copied pair and not the other is the shape that has produced six
separate bugs in this repo, so the fix is the rail, not the audit.

These tests are deliberately BOX-INDEPENDENT. A bare `assert cfg.VET_ENABLED
is False` proves the rail only on a box whose resolved value is True — which
this one happens to be today, and the Pi is, and a box with an overlay saying
otherwise is not. So a module-scoped fixture below stands in for "the machine
says vetting is ON", and the function-scoped rail in conftest has to overrule
it. Higher-scoped fixtures are set up first, so the ordering is the real one.
"""

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import exit_vet                      # noqa: E402
from zebra import config as cfg               # noqa: E402
from zebra import vet as vet_mod              # noqa: E402


@pytest.fixture(scope='module', autouse=True)
def _box_says_vetting_is_on():
    """Stand in for a machine whose resolved config has vetting ON.

    Module-scoped on purpose: pytest sets higher-scoped fixtures up first, so
    this lands BEFORE conftest's function-scoped `_pinned_vet_flag` and the
    rail has something real to overrule. Without it these tests would assert
    the rail on a box that already agreed with it — which is not a test.

    Its own MonkeyPatch because `monkeypatch` is function-scoped.
    """
    mp = pytest.MonkeyPatch()
    mp.setattr(cfg, 'VET_ENABLED', True)
    yield
    mp.undo()


def test_the_rail_pins_the_flag_whatever_the_box_says():
    """THE test. Fails without conftest's `_pinned_vet_flag`."""
    assert cfg.VET_ENABLED is False, \
        'bcs/tests/ is reading the machine\'s live vetting switch — pin it ' \
        'in conftest the way zebra/tests/conftest.py does'


def test_the_rail_holds_for_a_second_test_in_the_same_module():
    """The module-scoped stand-in persists between tests; the rail is
    function-scoped, so it has to re-apply. A rail that only covers the first
    test in a file is worse than none, because it looks like it works."""
    assert cfg.VET_ENABLED is False


def test_a_test_that_needs_vetting_can_still_turn_it_on(monkeypatch):
    """The rail pins a DEFAULT, it does not make a vet path untestable.

    This is why it uses the shared `monkeypatch` rather than a private
    MonkeyPatch like `_journal_to_tmp` and `_monitor_logs_to_tmp`: those two
    rail against a mistake and must be unswitchable, this one sets a starting
    position. Fixtures and test bodies both run after it and win — which is
    how zebra's 14 vet-path sites set it True.
    """
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    assert cfg.VET_ENABLED is True


def test_the_override_does_not_leak_into_the_next_test():
    """Companion to the one above: `monkeypatch` unwinds to the value the
    rail installed, not to the box's."""
    assert cfg.VET_ENABLED is False


# ── what the flag actually gates, from the BCS side ─────────────────────────

class _Store:
    """`exit_cleared` hands `store.raw` to the gate."""

    def __init__(self):
        self.raw = object()


_TRADE = {'id': 1, 'stock': 'TESTCO', '_store_type': 'zebra',
          'long_symbol': 'TESTCO26SEP1340CE',
          'short_symbol': 'TESTCO26SEP1390CE'}


@pytest.fixture
def marker_spy(monkeypatch):
    """Records whether `exit_gate` got PAST its `VET_ENABLED` check.

    `_exit_marker` is the first thing it touches afterwards, and asserting on
    it is what makes this a test of the flag rather than of the return value:
    `exit_cleared` fails OPEN on any exception, so a gate that ran, blew up on
    the sentinel store and was swallowed returns True exactly like a gate that
    never ran. The spy tells them apart.
    """
    seen = []

    def spy(trade, kind):
        seen.append((trade.get('id'), kind))
        return {}

    monkeypatch.setattr(vet_mod, '_exit_marker', spy)
    return seen


def test_the_bcs_exit_path_never_reaches_the_vet_under_the_rail(marker_spy):
    """End to end: bcs/exit_vet -> zebra.monitor -> zebra.vet -> the flag.

    `SL_SPREAD` is in `VET_KIND`, `_store_type` is zebra and `dry_run` is
    False, so this is exactly the call that would consult Claude in
    production. Under the rail it must short-circuit at the flag — no marker
    read, no marker written, no agent.
    """
    said = []
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'SL_SPREAD', None, 100.0,
                                 dry_run=False, log=said.append) is True
    assert marker_spy == [], \
        'the vetting layer ran in a test — it writes markers into the store ' \
        'under test and can spawn a real Claude agent'
    assert not any('EXIT VET ERROR' in m for m in said), \
        'the gate ran and threw; fail-open hid it behind the same True'


def test_the_gate_itself_short_circuits_on_the_flag(marker_spy):
    """One level down, so a change in `bcs/exit_vet.py`'s early-outs cannot
    make the test above pass for the wrong reason."""
    assert vet_mod.exit_gate(object(), dict(_TRADE), 'debit_sl', {},
                             100.0) == 'proceed'
    assert marker_spy == []


# ── N7 · the spawn rail, proven rather than assumed ─────────────────────────
#
# A rail with no test that fires it is decoration. These are the companions to
# `_no_real_agents` in conftest, and they exist because the flag rail above is
# deliberately overridable: the moment a test here sets `VET_ENABLED` True, the
# route from `_exit_cleared` -> `exit_gate` -> `_spawn_cli` reopens, and that
# is exactly the route that wrote ~30 junk rows into the production decision
# journal from zebra's suite in August.

def test_the_named_spawn_doors_refuse():
    from bcs.tests.conftest import RealAgentSpawnAttempted
    from zebra import vet as vet_mod

    for door in (vet_mod._spawn_generic, vet_mod._spawn_cli):
        with pytest.raises(RealAgentSpawnAttempted):
            door('prompt', 'model', 'tag')


def test_the_POPEN_backstop_refuses_a_claude_command():
    """The second layer. One SOURCE beats N call sites — it catches a future
    spawn path that goes through neither named door, the same way the Telegram
    rail guards `requests.post` and not only our own sender."""
    import subprocess

    from bcs.tests.conftest import RealAgentSpawnAttempted

    with pytest.raises(RealAgentSpawnAttempted):
        subprocess.Popen(['claude', '-p', 'hello'])
    with pytest.raises(RealAgentSpawnAttempted):
        subprocess.Popen('/usr/local/bin/claude --help')


def test_the_backstop_is_a_guard_and_not_a_sandbox():
    """Non-claude subprocesses still work. A rail that blocked every Popen
    would be switched off by the first test that legitimately needed one,
    which is how rails die."""
    import subprocess
    import sys

    out = subprocess.check_output([sys.executable, '-c', 'print(1)'])
    assert out.strip() == b'1'


def test_the_spawn_rail_survives_monkeypatch_undo(monkeypatch):
    """It rails against a MISTAKE, so it owns a private MonkeyPatch. The first
    version of `_no_production_writes` took the shared one and its own proof
    failed: `undo()` reverts every patch on that instance, so the rail was
    removed by exactly the call it exists to catch.
    """
    from bcs.tests.conftest import RealAgentSpawnAttempted
    from zebra import vet as vet_mod

    monkeypatch.undo()
    with pytest.raises(RealAgentSpawnAttempted):
        vet_mod._spawn_cli(1)
