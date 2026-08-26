"""The tracked config layer, and the guard that keeps secrets out of it.

`Helper/config/` is excluded wholesale by `.gitignore` because three of its
files carry live secrets and **the GitHub repo is PUBLIC**. That left the
thresholds for twelve subsystems with no history at all. The split
(2026-08-26) tracks a secret-free `<name>.defaults.json` per subsystem and
leaves the secrets in the untracked `<name>.json` overlay.

The dangerous half is not the merge — it is that a tracked file now sits in a
directory whose whole reason for being ignored is that it holds secrets. One
careless `folder_id` copied back and it is published, and
`feedback_public_repo_scan_before_adding` records that a commit-window scan is
not an audit. So the scan runs here, on every test run, over every tracked
defaults file.
"""
import json
import re
import subprocess

import pytest

from common import layered_config as lc

CONFIG_DIR = lc.CONFIG_DIR
DEFAULTS = sorted(CONFIG_DIR.glob('*.defaults.json'))


# -- The merge ---------------------------------------------------------------

def test_overlay_wins_leaf_by_leaf():
    merged, _ = lc._merge({'a': 1, 'b': 2}, {'b': 9})
    assert merged == {'a': 1, 'b': 9}


def test_nested_dicts_merge_rather_than_replace():
    """The property the whole split depends on: an overlay carrying only
    `google_drive.folder_id` must not delete `google_drive.file_name`."""
    merged, _ = lc._merge(
        {'google_drive': {'enabled': True, 'file_name': 'bcs_trades.json'}},
        {'google_drive': {'folder_id': 'xxx'}})
    assert merged['google_drive'] == {
        'enabled': True, 'file_name': 'bcs_trades.json', 'folder_id': 'xxx'}


def test_a_list_is_one_value_not_a_thing_to_merge():
    """Element-wise list merging would make it impossible for an overlay to
    REMOVE a symbol from a universe -- it could only ever add."""
    merged, _ = lc._merge({'universe': ['A', 'B', 'C']}, {'universe': ['A']})
    assert merged['universe'] == ['A']


def test_an_overlay_key_absent_from_defaults_is_added_not_rejected():
    """Secrets live only in the overlay. If unknown keys were dropped the
    split would delete every credential on the box."""
    merged, shadows = lc._merge({'a': 1}, {'secret': 'x'})
    assert merged['secret'] == 'x'
    assert shadows == [], 'adding a key is not shadowing'


# -- The shadow warning ------------------------------------------------------

def test_shadowing_a_different_value_is_reported_with_both_values():
    """Overlay-wins is a footgun: an edit to the tracked file does nothing on a
    box whose overlay still carries the old value. It is accepted only because
    the alternative -- reusing the untracked name -- makes `git pull` refuse on
    a Pi with no working SSH. The warning is what makes it survivable."""
    _, shadows = lc._merge({'sl_pct': 0.5}, {'sl_pct': 0.25})
    assert len(shadows) == 1
    assert '0.5' in shadows[0] and '0.25' in shadows[0]
    assert 'sl_pct' in shadows[0]


def test_an_identical_value_is_not_reported():
    """Negative control. If every equal leaf warned, the warning would be pure
    noise on any box and nobody would read the one that matters."""
    _, shadows = lc._merge({'sl_pct': 0.5}, {'sl_pct': 0.5})
    assert shadows == []


def test_the_warning_reaches_the_caller_hook(tmp_path, monkeypatch):
    """The kill switch reads through here, and its 'unreadable, staying ARMED'
    notice has to land in the monitor's session log -- a logging record in a
    cron job goes nowhere."""
    monkeypatch.setattr(lc, 'CONFIG_DIR', tmp_path)
    (tmp_path / 'x.defaults.json').write_text('{"a": 1}')
    (tmp_path / 'x.json').write_text('{"a": 2}')
    seen = []
    lc.load('x', warn=seen.append)
    assert seen and 'a: defaults 1 -> overlay 2' in seen[0]


# -- Failure modes: neither layer may take the system down -------------------

def test_a_corrupt_overlay_falls_back_to_the_tracked_layer(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(lc, 'CONFIG_DIR', tmp_path)
    (tmp_path / 'x.defaults.json').write_text('{"a": 1}')
    (tmp_path / 'x.json').write_text('{not json')
    assert lc.load('x', warn=lambda m: None) == {'a': 1}


def test_a_corrupt_defaults_layer_still_yields_the_overlay(tmp_path,
                                                           monkeypatch):
    monkeypatch.setattr(lc, 'CONFIG_DIR', tmp_path)
    (tmp_path / 'x.defaults.json').write_text('[]')
    (tmp_path / 'x.json').write_text('{"a": 2}')
    assert lc.load('x', warn=lambda m: None) == {'a': 2}


def test_neither_layer_present_is_an_empty_dict_not_an_error(tmp_path,
                                                             monkeypatch):
    """Every loader this replaced returned {} for a missing file. A subsystem
    that has not been split yet must behave exactly as before."""
    monkeypatch.setattr(lc, 'CONFIG_DIR', tmp_path)
    assert lc.load('nothing_here') == {}


# -- The guard: no secret may reach a tracked file ---------------------------

#: Shapes that must never appear in a tracked file. Patterns, not a value list,
#: because the value list would itself have to name the secrets.
FORBIDDEN = [
    (re.compile(r'\d{6,}:[A-Za-z0-9_-]{30,}'), 'a Telegram bot token'),
    (re.compile(r'[A-Za-z]:\\\\Users\\\\'), 'a Windows home directory'),
    (re.compile(r'/home/[^/"]+/'), 'a Linux home directory'),
    (re.compile(r'\b1[A-Za-z0-9_-]{27,}\b'), 'a Google Drive folder id'),
    # By FIELD NAME, not by shape. A Zerodha client id is 3 letters + 3 digits
    # (or 2 + 4) and so is the ETF ticker MON100 that sits in two of these
    # files — the shapes are identical and a shape scan flags the ETFs
    # forever, which is how a real warning gets trained into noise. What an
    # account id cannot hide is the field that labels it.
    (re.compile(r'"(?:user_id|client_id|account_id|accounts|default_account)"'),
     'a broker account id'),
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY'), 'a private key'),
    (re.compile(r'"(?:api_key|access_token|client_secret|private_key)"'),
     'a credential field'),
]


@pytest.mark.parametrize('path', DEFAULTS, ids=lambda p: p.name)
def test_no_tracked_defaults_file_contains_a_secret(path):
    text = path.read_text(encoding='utf-8')
    overlay = path.name.replace('.defaults', '')
    for pat, what in FORBIDDEN:
        m = pat.search(text)
        assert m is None, (
            '%s looks like it contains %s: %s... The repo is PUBLIC. Move it '
            'to the untracked %s overlay.'
            % (path.name, what, m.group(0)[:12], overlay))


def test_the_guard_actually_catches_a_planted_secret():
    """Negative control. Without this, a scan whose patterns silently stopped
    matching would report CLEAN forever -- which is exactly how a false CLEAN
    got recorded once already."""
    # SYNTHETIC. The first draft of this fixture used the real Drive folder id,
    # the real account id and the real bot token's prefix, because those were
    # what the patterns were written against -- and the staged-content scan
    # caught them here, in the test whose entire job is to stop exactly that.
    # A negative control has to match the SHAPE, never the value.
    planted = {
        'a token': '{"bot_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}',
        'a windows home': r'{"p": "C:\\Users\\example\\c.json"}',
        'a linux home': '{"p": "/home/example/secret.json"}',
        'a folder id': '{"f": "1EXAMPLEexampleEXAMPLEexample012"}',
        'an account': '{"user_id": "AAA000"}',
        'a cred field': '{"api_key": "whatever"}',
    }
    for label, text in planted.items():
        assert any(p.search(text) for p, _ in FORBIDDEN), \
            'the scan missed %s' % label


@pytest.mark.parametrize('path', DEFAULTS, ids=lambda p: p.name)
def test_every_defaults_file_is_actually_tracked(path):
    """Fail-open means losing one is silent: the overlay keeps supplying the
    values, so nothing breaks and the history quietly stops accruing."""
    out = subprocess.run(['git', 'ls-files', '--error-unmatch', path.name],
                         cwd=str(path.parent),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert out.returncode == 0, (
        '%s is NOT tracked. config/ is gitignored wholesale, so it needs '
        '`git add -f` -- and a scan before that, because the repo is PUBLIC.'
        % path.name)


def test_there_is_at_least_one_defaults_file():
    """Guards the two parametrised tests above: an empty glob makes both of
    them vacuously pass, reporting green for a split that got deleted."""
    assert len(DEFAULTS) >= 10, 'only found %d defaults files' % len(DEFAULTS)


# -- The split did not change any value --------------------------------------

@pytest.mark.parametrize('path', DEFAULTS, ids=lambda p: p.name)
def test_defaults_and_overlay_do_not_both_claim_the_same_leaf(path):
    """After the split the overlay holds ONLY secrets, so nothing should
    shadow. A shadow here means the strip left a duplicate behind, and the
    tracked value it duplicates is now unreachable."""
    name = path.name[:-len('.defaults.json')]
    overlay = path.parent / (name + '.json')
    if not overlay.exists():
        pytest.skip('no overlay on this box')
    _, shadows = lc._merge(json.loads(path.read_text(encoding='utf-8')),
                           json.loads(overlay.read_text(encoding='utf-8')))
    assert shadows == [], (
        '%s: overlay shadows tracked value(s) %s. Edits to the tracked file '
        'will not take effect here.' % (name, shadows))
