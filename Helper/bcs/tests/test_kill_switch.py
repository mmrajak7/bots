"""B0 — the live-order kill switch.

Before this existed there was NO way to disarm the money path short of editing
crontab on the Pi: the cron line is `flock -n` on a */5 schedule, so killing
the process gets it restarted inside five minutes.

The switch FAILS OPEN, which is the opposite of the usual instinct and is the
part most likely to be "corrected" by a future reader. It is deliberate:
`bcs.spread_monitor` only ever CLOSES positions, so disarming it does not
prevent a bad trade — it abandons the stops on a live book. Every test below
that asserts "still enabled" is guarding that decision, not being lax.

Each guard gets a negative control proving the test is sensitive to the guard
itself and not to something incidental.
"""
import json

import pytest

from bcs import spread_monitor as sm


def _write(tmp_path, payload) -> None:
    p = tmp_path / 'bcs_config.json'
    if isinstance(payload, str):
        p.write_text(payload)
    else:
        p.write_text(json.dumps(payload))
    return p


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point the module at a throwaway config."""
    def _set(payload):
        monkeypatch.setattr(sm, 'CONFIG_FILE', _write(tmp_path, payload))
    return _set


# ── The switch itself ────────────────────────────────────────────────────────

def test_explicit_false_disarms(cfg):
    cfg({'trading': {'enabled': False}})
    assert sm.trading_enabled() is False


def test_explicit_true_arms(cfg):
    """Negative control for the test above: same file, one value flipped."""
    cfg({'trading': {'enabled': True}})
    assert sm.trading_enabled() is True


def test_the_shipped_config_is_armed():
    """The real config/bcs_config.json must parse and must be armed.

    A typo here is indistinguishable from a deliberate disarm at runtime, and
    the failure is silent (stops stop firing). Pin it.
    """
    assert sm.trading_enabled() is True


# ── Fail-open: every one of these must stay ARMED ────────────────────────────

@pytest.mark.parametrize('payload,why', [
    ({}, 'no trading node at all'),
    ({'trading': {}}, 'node present, key absent'),
    ({'trading': None}, 'node is null'),
    ({'trading': 'false'}, 'node is a string, not a dict'),
    ({'trading': {'enabled': 'false'}}, 'string "false", not the boolean'),
    ({'trading': {'enabled': 0}}, 'zero is not False'),
    ({'trading': {'enabled': None}}, 'null is not False'),
    ('{not json at all', 'corrupt file'),
])
def test_anything_other_than_boolean_false_stays_armed(cfg, payload, why):
    cfg(payload)
    assert sm.trading_enabled() is True, why


def test_a_missing_config_stays_armed(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, 'CONFIG_FILE', tmp_path / 'does_not_exist.json')
    assert sm.trading_enabled() is True


def test_an_unreadable_config_stays_armed_and_says_so(tmp_path, monkeypatch,
                                                      capsys):
    """A directory where a file should be: open() raises IsADirectoryError.

    Distinct from the corrupt-JSON case — that one is caught by the broad
    handler, this one proves the handler is broad enough.
    """
    d = tmp_path / 'bcs_config.json'
    d.mkdir()
    monkeypatch.setattr(sm, 'CONFIG_FILE', d)
    assert sm.trading_enabled() is True
    assert 'unreadable' in capsys.readouterr().out


def test_the_failopen_default_is_not_an_accident(cfg):
    """Negative control for the whole fail-open family.

    If `trading_enabled` ever gets "fixed" to fail closed, the parametrised
    test above goes red — but so would a version that simply returns True
    unconditionally. This proves the function can still say False, so those
    passes mean fail-open and not always-open.
    """
    cfg({'trading': {'enabled': False}})
    assert sm.trading_enabled() is False
    cfg({'trading': {'enabled': True}})
    assert sm.trading_enabled() is True
