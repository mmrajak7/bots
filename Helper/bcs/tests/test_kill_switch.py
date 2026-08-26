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
from common import layered_config


def _write(tmp_path, payload, name='bcs_config.json'):
    p = tmp_path / name
    if isinstance(payload, str):
        p.write_text(payload)
    else:
        p.write_text(json.dumps(payload))
    return p


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point BOTH switch files at throwaways, the second one armed.

    The first version patched only CONFIG_FILE. Once the switch became two
    ANDed files that quietly left the REAL config/trading_switch.json in the
    loop, so every case below was reading a live file off the box -- passing
    for a reason the test did not state, and going red the moment the owner
    disarmed the monitor for real.
    """
    monkeypatch.setattr(sm, 'SWITCH_FILE',
                        _write(tmp_path, {'trading': {'enabled': True}},
                               'trading_switch.json'))
    # The bcs arm reads through common.layered_config since the config split,
    # so pointing sm.CONFIG_FILE alone would leave the REAL config/ in the
    # loop -- the same hole the two-file note above describes, reopened.
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)

    def _set(payload):
        monkeypatch.setattr(sm, 'CONFIG_FILE', _write(tmp_path, payload))
    return _set


@pytest.fixture
def both(tmp_path, monkeypatch):
    """Set the tracked switch and the overlay independently."""
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)

    def _set(switch, overlay):
        monkeypatch.setattr(sm, 'SWITCH_FILE',
                            _write(tmp_path, switch, 'trading_switch.json'))
        monkeypatch.setattr(sm, 'CONFIG_FILE', _write(tmp_path, overlay))
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
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'CONFIG_FILE', tmp_path / 'does_not_exist.json')
    monkeypatch.setattr(sm, 'SWITCH_FILE', tmp_path / 'also_missing.json')
    assert sm.trading_enabled() is True


def test_an_unreadable_config_stays_armed_and_says_so(tmp_path, monkeypatch,
                                                      capsys):
    """A directory where a file should be: open() raises IsADirectoryError.

    Distinct from the corrupt-JSON case — that one is caught by the broad
    handler, this one proves the handler is broad enough.
    """
    d = tmp_path / 'bcs_config.json'
    d.mkdir()
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'CONFIG_FILE', d)
    monkeypatch.setattr(sm, 'SWITCH_FILE', tmp_path / 'absent.json')
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


# ── Two files, ANDed ─────────────────────────────────────────────────────────
#
# `config/trading_switch.json` is TRACKED. `config/bcs_config.json` could not
# be, because it carried the Drive folder id and a credentials path and its
# neighbours in config/ carry live Telegram bot tokens into a PUBLIC repo -- so
# the switch got a second home rather than moving house.
#
# Since the 2026-08-26 config split BOTH arms are tracked: the flag now lives in
# `config/bcs_config.defaults.json` with the secrets left behind in the
# untracked overlay. The overlay can still carry a `trading` block and still
# wins if it does, so the two-source property is unchanged -- what changed is
# that neither arm can now vanish from a fresh checkout.

@pytest.mark.parametrize('switch,overlay,expect,why', [
    (True,  True,  True,  'both armed'),
    (False, True,  False, 'the TRACKED file alone can disarm'),
    (True,  False, False, 'the overlay alone can still disarm'),
    (False, False, False, 'both disarmed'),
])
def test_either_file_can_stop_the_money_path(both, switch, overlay, expect,
                                             why):
    """ANDed, not ranked. A precedence rule means a reader who edits the wrong
    file is silently overruled -- the one outcome a stop button may never
    have. Both are stop buttons; neither is an arm button.
    """
    both({'trading': {'enabled': switch}}, {'trading': {'enabled': overlay}})
    assert sm.trading_enabled() is expect, why


def test_the_tracked_defaults_layer_can_disarm_on_its_own(tmp_path,
                                                          monkeypatch):
    """The case that matters on a real box, and the one the fixtures above do
    NOT cover: since the config split the flag lives in the TRACKED
    `bcs_config.defaults.json`, and the untracked overlay carries only secrets
    and has no `trading` block at all. Every other test here writes the
    overlay, so all of them would still pass if the defaults layer were never
    read.
    """
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'SWITCH_FILE',
                        _write(tmp_path, {'trading': {'enabled': True}},
                               'trading_switch.json'))
    monkeypatch.setattr(sm, 'CONFIG_FILE', tmp_path / 'bcs_config.json')
    _write(tmp_path, {'google_drive': {'folder_id': 'x'}}, 'bcs_config.json')

    _write(tmp_path, {'trading': {'enabled': False}},
           'bcs_config.defaults.json')
    assert sm.trading_enabled() is False, 'the tracked layer cannot disarm'

    _write(tmp_path, {'trading': {'enabled': True}},
           'bcs_config.defaults.json')
    assert sm.trading_enabled() is True, 'negative control'


def test_a_secrets_only_overlay_does_not_re_arm_a_disarmed_tracked_flag(
        tmp_path, monkeypatch):
    """The split's own footgun, pointed at the money path: an overlay that has
    no opinion must not count as an opinion. Absence is not `true`."""
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'SWITCH_FILE',
                        _write(tmp_path, {'trading': {'enabled': True}},
                               'trading_switch.json'))
    monkeypatch.setattr(sm, 'CONFIG_FILE', tmp_path / 'bcs_config.json')
    _write(tmp_path, {'google_drive': {'folder_id': 'x'}}, 'bcs_config.json')
    _write(tmp_path, {'trading': {'enabled': False}},
           'bcs_config.defaults.json')
    assert sm.trading_enabled() is False


def test_an_overlay_trading_block_still_wins_over_the_tracked_one(
        tmp_path, monkeypatch):
    """Overlay-wins is the documented direction, and a stop button has to work
    from the file the person in front of the box actually edits."""
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'SWITCH_FILE',
                        _write(tmp_path, {'trading': {'enabled': True}},
                               'trading_switch.json'))
    monkeypatch.setattr(sm, 'CONFIG_FILE', tmp_path / 'bcs_config.json')
    _write(tmp_path, {'trading': {'enabled': True}},
           'bcs_config.defaults.json')
    _write(tmp_path, {'trading': {'enabled': False}}, 'bcs_config.json')
    assert sm.trading_enabled() is False


def test_an_armed_overlay_cannot_re_arm_a_disarmed_tracked_switch(both):
    """The failure this ordering exists to prevent: someone re-arms the file
    they happen to know about and believes the monitor is stopped."""
    both({'trading': {'enabled': False}}, {'trading': {'enabled': True}})
    assert sm.trading_enabled() is False


def test_a_corrupt_tracked_switch_does_not_disarm(both):
    """Fail-open applies per file. A truncated switch must not abandon the
    stops on a live book -- see the module docstring."""
    both('{ truncated', {'trading': {'enabled': True}})
    assert sm.trading_enabled() is True


def test_the_shipped_tracked_switch_is_armed_and_secret_free():
    """It is force-added into a PUBLIC repo, so the scan is part of the test.

    Checked against the file's real bytes rather than its parsed values: a
    secret pasted into a comment key, a new node, or a stray trailing line is
    still published.
    """
    import re
    raw = sm.SWITCH_FILE.read_text(encoding='utf-8')
    assert sm._switch_says(sm.SWITCH_FILE) is True

    forbidden = {
        'telegram bot token': r'\d{8,10}:[A-Za-z0-9_-]{30,}',
        'a real home directory': r'[Cc]:\\Users\\|/home/[a-z]',
        'a google drive id': r'1[A-Za-z0-9_-]{27,}',
        'an api key or token value': r'(?i)"(api_key|access_token|secret)"\s*:',
        'a kite account id': r'[A-Z]{2}\d{4}',
    }
    for what, pat in forbidden.items():
        assert not re.search(pat, raw), (
            f'config/trading_switch.json contains {what}. It is TRACKED in a '
            f'PUBLIC repo -- secrets belong in the untracked overlay.')


def test_the_tracked_switch_is_actually_tracked():
    """Force-added past `.gitignore: Helper/config/`. If a later `git rm
    --cached` or a clean checkout drops it, the file reverts to being just
    another untracked config and the whole point is lost -- silently, because
    fail-open means a missing file still reads as ARMED.
    """
    import subprocess
    out = subprocess.run(
        ['git', 'ls-files', '--error-unmatch', str(sm.SWITCH_FILE)],
        cwd=str(sm.PROJECT_ROOT), capture_output=True, text=True)
    assert out.returncode == 0, (
        'config/trading_switch.json is NOT tracked by git. Re-add it with '
        '`git add -f config/trading_switch.json`.')
