"""The startup banners must land in a log that exists.

`python -m zebra ...` imports `zebra.config` through the package `__init__`
BEFORE `main()` configures logging, and an INFO record with no handler is
dropped. The vetting banner had never once appeared in the Pi's cron log
(checked 2026-09-04), and the daily-sweep banner inherited the hole on its
first day -- the exact "looks armed, is dark" state both lines exist to refuse.
"""
import logging
import re
from pathlib import Path

import pytest

from zebra import config as cfg


@pytest.fixture
def pending_banners(monkeypatch):
    monkeypatch.setattr(cfg, '_BANNERS_PENDING', True)


def test_no_sink_means_the_banners_wait(pending_banners, monkeypatch):
    monkeypatch.setattr(cfg, '_log_sink_exists', lambda: False)
    assert cfg.emit_state_banners() is False
    assert cfg._BANNERS_PENDING is True


def test_with_a_sink_both_banners_go_out_exactly_once(pending_banners, caplog):
    caplog.set_level(logging.INFO, logger='zebra.config')
    assert cfg.emit_state_banners() is True
    texts = [r.getMessage() for r in caplog.records]
    assert any('Claude vetting layer' in t for t in texts)
    assert any('Daily EOD position sweep' in t for t in texts)
    caplog.clear()
    assert cfg.emit_state_banners() is False
    assert caplog.records == []


def test_force_re_emits_for_a_long_lived_process(pending_banners, caplog):
    caplog.set_level(logging.INFO, logger='zebra.config')
    cfg.emit_state_banners()
    caplog.clear()
    assert cfg.emit_state_banners(force=True) is True
    assert any('Daily EOD position sweep' in r.getMessage()
               for r in caplog.records)


def test_main_emits_the_banners_after_it_configures_logging():
    """Source guard: the fix is only real if `main()` calls it AFTER
    `setup_logging`. RETIRES WHEN: `zebra/__init__.py` stops importing the
    trade store (and therefore config) at package import, so the import-time
    emission sees a configured handler in every entrypoint.
    """
    src = (Path(__file__).resolve().parents[1] / '__main__.py').read_text(
        encoding='utf-8')
    m = re.search(r'setup_logging\(args\.verbose\)(.*?)emit_state_banners\(\)',
                  src, re.S)
    assert m, 'main() must call cfg.emit_state_banners() after setup_logging'
    assert 'def ' not in m.group(1), 'the call must be in main() itself'
