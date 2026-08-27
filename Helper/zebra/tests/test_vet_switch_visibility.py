"""The vetting master switch must be visible — in git, and in the log.

`vet_enabled` was absent from `config/zebra_config.defaults.json` and defaulted
to False in code, so the ON state existed ONLY in the Pi's untracked overlay.
Two consequences, both silent:

  * the switch could not be audited or flipped from git — the one control that
    decides whether any Claude judgement gates a trade had no history at all;
  * a routine overlay rebuild disarms it, after which `vet.exit_gate` returns
    'proceed' for every exit (`zebra/vet.py:1541`) and every entry alert goes
    out unvetted. A dark layer and a working one look identical from outside.

The fix is a tracked default plus a log line that says where the switch
resolved, at WARNING when it resolves OFF with cohort money on the table.

Run:  cd Helper && python -m pytest zebra/tests/test_vet_switch_visibility.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import layered_config          # noqa: E402
from zebra import config as cfg            # noqa: E402

TRACKED = HELPER / 'config' / 'zebra_config.defaults.json'


def _config_dir(tmp_path, overlay=None):
    """A config directory holding the REAL tracked defaults, plus an optional
    overlay. The tracked file is copied rather than synthesised: the point of
    these tests is what THIS repo's committed config resolves to."""
    d = tmp_path / 'config'
    d.mkdir(parents=True)
    (d / 'zebra_config.defaults.json').write_text(TRACKED.read_text(),
                                                  encoding='utf-8')
    if overlay is not None:
        (d / 'zebra_config.json').write_text(json.dumps(overlay),
                                             encoding='utf-8')
    return d


# ── resolution through the real two-layer merge ─────────────────────────────

def test_vetting_resolves_ON_from_the_tracked_config_with_no_overlay(
        tmp_path, monkeypatch):
    """A fresh checkout — no secrets file yet — must come up VETTED.

    This is the case the bug produced: no overlay meant no `vet_enabled` key
    anywhere, and the code fallback said False.
    """
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', _config_dir(tmp_path))
    monkeypatch.delenv('ZEBRA_VET_ENABLED', raising=False)
    assert cfg._load_runtime()['vet_enabled'] is True


def test_an_overlay_still_wins_so_the_Pi_is_unchanged(tmp_path, monkeypatch):
    """Overlay-wins is what makes this change safe to land on a live box: the
    Pi's own value keeps deciding, in both directions. Pinned so a later
    'simplification' to defaults-win cannot flip a live switch silently."""
    monkeypatch.setattr(layered_config, 'CONFIG_DIR',
                        _config_dir(tmp_path, {'vet_enabled': False}))
    assert cfg._load_runtime()['vet_enabled'] is False
    monkeypatch.setattr(layered_config, 'CONFIG_DIR',
                        _config_dir(tmp_path / 'b', {'vet_enabled': True}))
    assert cfg._load_runtime()['vet_enabled'] is True


def test_max_dte_resolves_to_45_through_the_same_merge(tmp_path, monkeypatch):
    """Owner decision 2026-08-27. Landed in the tracked file in the same pass,
    so it is verified through the same path rather than only as a literal."""
    monkeypatch.setattr(layered_config, 'CONFIG_DIR', _config_dir(tmp_path))
    assert cfg._load_runtime()['max_dte'] == 45


# ── the startup line ────────────────────────────────────────────────────────

def test_the_resolved_value_is_logged_when_the_layer_is_ON():
    level, msg = cfg.vet_state_line(True, 0)
    assert level == 'info'
    assert 'ENABLED' in msg


def test_disabled_with_open_cohort_positions_is_a_WARNING():
    """The whole point. OFF with live positions is the state that killed the
    layer silently, and INFO in a cron log is not said out loud."""
    level, msg = cfg.vet_state_line(False, 3)
    assert level == 'warning'
    assert 'DISABLED' in msg and '3 open' in msg


def test_disabled_with_an_UNREADABLE_book_warns_like_open_positions():
    """`feedback_watchdog_must_not_all_clear`: 'we could not look' must never
    render as 'we looked and it is fine'."""
    level, msg = cfg.vet_state_line(False, None)
    assert level == 'warning'
    assert 'UNKNOWN' in msg


def test_disabled_with_a_flat_book_still_says_so():
    level, msg = cfg.vet_state_line(False, 0)
    assert 'DISABLED' in msg


def test_the_state_line_names_where_the_value_came_from(monkeypatch):
    """A switch with two sources needs its line to say WHICH one answered —
    the env override exists precisely so one process can disagree with the
    fleet, and that is worth reading in the log."""
    monkeypatch.setattr(cfg, '_vet_env', '1')
    assert 'env' in cfg.vet_state_line(True, 0)[1]
    monkeypatch.setattr(cfg, '_vet_env', '')
    assert 'zebra_config' in cfg.vet_state_line(True, 0)[1]


# ── the open-position count the line leans on ───────────────────────────────

COHORT = cfg.COHORT_START


@pytest.mark.parametrize('status,counts', [
    ('entered', True), ('closing', True), ('partial_close', True),
    ('exited', False), ('watching', False), ('triggered', False),
    ('cancelled', False),
])
def test_which_statuses_count_as_an_open_cohort_position(
        tmp_path, monkeypatch, status, counts):
    """`closing` and `partial_close` count. A position half-way out of the
    market is the last one you want judged by a layer nobody noticed had
    gone — and `partial_close` is the state a failed bridge close freezes in.
    """
    book = tmp_path / 'zebra_trades.json'
    book.write_text(json.dumps([{'id': 1, 'status': status, 'cohort': COHORT}]),
                    encoding='utf-8')
    monkeypatch.setattr(cfg, 'LOCAL_FILE', book)
    assert cfg.open_cohort_positions() == (1 if counts else 0)


def test_non_cohort_positions_do_not_count(tmp_path, monkeypatch):
    """450 records from the dropped back-ratio strategy share this file. They
    are not what the vetting layer gates."""
    book = tmp_path / 'zebra_trades.json'
    book.write_text(json.dumps([
        {'id': 1, 'status': 'entered'},
        {'id': 2, 'status': 'entered', 'cohort': '2020-01-01'},
        {'id': 3, 'status': 'entered', 'cohort': COHORT},
    ]), encoding='utf-8')
    monkeypatch.setattr(cfg, 'LOCAL_FILE', book)
    assert cfg.open_cohort_positions() == 1


@pytest.mark.parametrize('body', ['', 'not json', '{"trades": null}', '[1, 2]'])
def test_a_malformed_book_reports_UNKNOWN_rather_than_raising(
        tmp_path, monkeypatch, body):
    """This decorates a log line at import. A bad book must not stop the
    process that was about to warn about it — but it must not read as zero
    either, which is why None is a distinct answer."""
    book = tmp_path / 'zebra_trades.json'
    book.write_text(body, encoding='utf-8')
    monkeypatch.setattr(cfg, 'LOCAL_FILE', book)
    got = cfg.open_cohort_positions()
    assert got is None or got == 0


def test_a_missing_book_is_UNKNOWN_not_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'nope.json')
    assert cfg.open_cohort_positions() is None
