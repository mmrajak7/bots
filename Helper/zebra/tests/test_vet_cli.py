"""The two CLI verbs the spawned Claude process actually calls.

This is the whole interface between the LLM and the trade store. Claude never
writes JSON — it reads `vet show` and calls `vet decide`, both of which go
through the locked, schema-validated API. These tests guard that contract.

Run:  cd Helper && python -m pytest zebra/tests/test_vet_cli.py -v
"""
import json
import sys
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import __main__ as cli        # noqa: E402
from zebra import config as cfg          # noqa: E402
from zebra import vet as vet_mod         # noqa: E402
from zebra import decisions as dec_mod   # noqa: E402
from zebra import trade_store as ts_mod  # noqa: E402

SIGNAL = {
    'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
    'st_value': 100.0, 'st_direction': 'UP',
    'signal_price': 96.0, 'signal_gap_pct': 4.0,
}
CONTEXT = {'stock': 'TESTCO', 'debit': 14.0, 'debit_to_width_pct': 35.0,
           'long_strike': 96.0, 'short_strike': 100.0}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point both stores at a temp dir and defeat the module singletons."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'DECISIONS_FILE', tmp_path / 'zebra_decisions.json')
    monkeypatch.setattr(cfg, 'DECISIONS_LOCK', tmp_path / 'zebra_decisions.lock')

    store = ts_mod.ZebraStore(config={})
    store._load_local()
    store.add_signal(dict(SIGNAL))
    monkeypatch.setattr(ts_mod, '_store', store)
    monkeypatch.setattr(ts_mod, 'get_store', lambda: store)

    djournal = dec_mod.DecisionStore(path=cfg.DECISIONS_FILE,
                                     lock_path=cfg.DECISIONS_LOCK).initialize()
    monkeypatch.setattr(dec_mod, '_store', djournal)
    monkeypatch.setattr(dec_mod, 'get_store', lambda: djournal)
    return store, djournal


def _decide(**kw):
    base = dict(id=1, verdict='allow', reason=None, red_flag=None,
                confidence=None, notes='')
    base.update(kw)
    return Namespace(**base)


# ── vet show ─────────────────────────────────────────────────────────────
def test_show_emits_parseable_json(wired, capsys):
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert cli.cmd_vet_show(Namespace(id=1)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['trade_id'] == 1 and out['stock'] == 'TESTCO'
    assert out['context'] == CONTEXT
    assert out['vet_state'] == 'pending' and out['expired'] is False


def test_show_serves_the_context_captured_at_trigger(wired, capsys):
    """The vetter must judge the book the BOT saw. If show re-quoted, the
    verdict would describe a trade that no longer exists."""
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    cli.cmd_vet_show(Namespace(id=1))
    assert json.loads(capsys.readouterr().out)['context']['debit'] == 14.0


def test_show_on_missing_trade_is_json_not_a_traceback(wired, capsys):
    """The caller is an LLM parsing stdout — an exception would be unparseable
    and it would have no way to report the failure."""
    assert cli.cmd_vet_show(Namespace(id=999)) == 1
    assert 'error' in json.loads(capsys.readouterr().out)


def test_show_reports_expiry_so_the_agent_can_bail(wired, capsys):
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    with store._mutate():
        store.find(1)['vet']['deadline'] = (
            datetime.now() - timedelta(minutes=1)).isoformat()
    cli.cmd_vet_show(Namespace(id=1))
    assert json.loads(capsys.readouterr().out)['expired'] is True


# ── vet decide ───────────────────────────────────────────────────────────
def test_decide_journals_and_lands_the_verdict(wired, capsys):
    store, journal = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert cli.cmd_vet_decide(_decide(verdict='veto',
                                      reason=['results inside expiry window'],
                                      red_flag=['Q1 print Aug 14'],
                                      confidence=0.8)) == 0
    d = journal.all()[0]
    assert d['verdict'] == 'veto' and d['red_flags'] == ['Q1 print Aug 14']
    assert d['confidence'] == 0.8 and d['model'] == cfg.VET_MODEL
    t = store.find(1)
    assert t['vet']['state'] == vet_mod.VETOED
    assert t['vet']['decision_id'] == d['id']


def test_one_decision_covers_the_bcs_shadow_too(wired):
    """Both A/B arms must share the verdict, or the July structure comparison
    silently becomes structure+judgment vs structure alone."""
    store, journal = wired
    with store._mutate():
        store._trades.append({'id': 2, 'version': 1, 'status': 'entered',
                              'structure': 'bcs', 'shadow_of': 1,
                              'stock': 'TESTCO', 'direction': 'CE'})
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    cli.cmd_vet_decide(_decide(verdict='allow'))
    assert sorted(journal.all()[0]['signal_ref']['trade_ids']) == [1, 2]


def test_decide_on_missing_trade_returns_nonzero(wired):
    assert cli.cmd_vet_decide(_decide(id=999)) == 1


def test_late_verdict_reports_discarded_but_exits_zero(wired, capsys):
    """A settled signal is not an error the agent should retry. Exiting
    non-zero would invite a retry loop hammering a closed case."""
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(minutes=11))
    assert cli.cmd_vet_decide(_decide(verdict='veto')) == 0
    assert 'discarded' in capsys.readouterr().out
    # Since 2026-08-13 the timed-out ENTRY is QUEUED for a fresh agent rather
    # than entered unvetted. The verdict is still discarded — it judged the
    # book of the attempt that died, and the retry re-snapshots.
    assert store.find(1)['vet']['state'] == vet_mod.QUEUED


def test_journal_is_written_even_when_the_verdict_is_discarded(wired):
    """Journal-first ordering: a late verdict still leaves an audit trail of
    what Claude concluded, even though nothing was acted on."""
    store, journal = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(minutes=11))
    cli.cmd_vet_decide(_decide(verdict='veto'))
    assert len(journal.all()) == 1


def test_bad_confidence_is_rejected_before_anything_lands(wired):
    store, journal = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    with pytest.raises(ValueError, match='confidence'):
        cli.cmd_vet_decide(_decide(confidence=42.0))
    assert journal.all() == []
    assert vet_mod.is_pending(store.find(1))     # signal untouched, still vettable


# ── notifications: one signal, one alert ─────────────────────────────────
def test_veto_sends_a_telegram(wired, monkeypatch):
    """A veto is the end of the story for this signal — nothing else will be
    sent, so silence would read as 'nothing fired'."""
    from zebra import monitor
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: sent.append(m))
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    cli.cmd_vet_decide(_decide(verdict='veto', red_flag=['Q1 results Aug 14']))
    assert len(sent) == 1
    assert 'VETOED' in sent[0] and 'TESTCO' in sent[0]
    assert 'Q1 results Aug 14' in sent[0]


def test_allow_sends_no_separate_telegram(wired, monkeypatch):
    """One signal, one alert: an allow rides on the ENTER ticket already going
    out, rather than firing a second notification."""
    from zebra import monitor
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: sent.append(m))
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    cli.cmd_vet_decide(_decide(verdict='allow'))
    assert sent == []


def test_discarded_veto_sends_nothing(wired, monkeypatch):
    """The signal already entered unvetted; a 'VETOED' alert for a live
    position would be actively misleading."""
    from zebra import monitor
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: sent.append(m))
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(minutes=11))
    cli.cmd_vet_decide(_decide(verdict='veto'))
    assert sent == []


def test_telegram_failure_does_not_break_a_landed_verdict(wired, monkeypatch):
    """The decision is already applied; a Telegram outage must not make the
    agent think its verdict failed and retry."""
    from zebra import monitor
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: (_ for _ in ()).throw(RuntimeError('down')))
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    assert cli.cmd_vet_decide(_decide(verdict='veto')) == 0
    assert store.find(1)['vet']['state'] == vet_mod.VETOED


def test_veto_alert_escapes_html(wired, monkeypatch):
    """Reasons are free text from an LLM; a bare '<' would 400 the whole
    message and the veto would vanish silently."""
    from zebra import monitor
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: sent.append(m))
    store, _ = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    cli.cmd_vet_decide(_decide(verdict='veto', reason=['depth < 500 lots']))
    assert '&lt;' in sent[0] and ' < ' not in sent[0]


def test_prompt_template_renders_with_every_placeholder(wired):
    """A missing placeholder would raise KeyError inside _spawn_cli and the
    signal would silently never be vetted."""
    from zebra import config as _cfg
    import sys as _sys
    p = _cfg.VET_PROMPT_TEMPLATE.format(trade_id=1, python=_sys.executable,
                                        vetting_doc=_cfg.VETTING_DOC)
    assert 'vet show 1' in p and 'vet decide 1' in p
    assert _cfg.VETTING_DOC.exists(), "VETTING.md must ship with the package"


# ---------------------------------------------------------------------------
# Per-spawn transcripts (2026-08-14)
# ---------------------------------------------------------------------------
# The daily `vet_cli_YYYYMMDD.log` wrote its banner at SPAWN and the child
# wrote its body minutes later at COMPLETION, so concurrent agents produced
# banner A / banner B / banner C then body A / body B / body C — every body
# filed under the wrong agent. Observed on the real Pi: the 09:15:47
# `postmortem` banner carried the events agent's permission errors and the
# exit vet's verdict. A cleanly misattributed transcript is worse than a
# garbled one, because it reads as true.


# conftest's autouse `_no_real_agents` rail replaces `_spawn_generic` with a
# recorder, which is exactly right for every other test and useless here: the
# behaviour under test IS that function's file handling. Bound at import time,
# before any fixture runs, so this is the genuine function — and Popen is
# stubbed below, so nothing is ever launched. Do not weaken the rail itself.
_REAL_SPAWN_GENERIC = vet_mod._spawn_generic


def _spawn_capture(monkeypatch, tmp_path):
    """Run _spawn_generic with Popen stubbed; return the files it created."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(vet_mod, 'resolve_cli', lambda: 'claude')
    monkeypatch.setattr(vet_mod.subprocess, 'Popen',
                        lambda *a, **k: type('P', (), {'pid': 4242})())
    monkeypatch.setattr(vet_mod, '_spawn_budget_ok', lambda *a, **k: 'ok')
    monkeypatch.setattr(vet_mod, 'claim_slot_pid', lambda *a, **k: None)
    monkeypatch.setattr(vet_mod, '_note_spawn', lambda *a, **k: None)
    monkeypatch.setattr(vet_mod, '_reap_children', lambda *a, **k: None)
    monkeypatch.setattr(vet_mod.shutil, 'which', lambda n: None)
    return tmp_path


def test_each_spawn_gets_its_own_transcript(monkeypatch, tmp_path):
    """Three agents in one cycle must produce three files, not one shared one.

    This is the bug's actual shape: all three appended to a single fd, so the
    reader could not tell which agent said what.
    """
    logs = _spawn_capture(monkeypatch, tmp_path)
    for tag, channel in (('vet #352 tp', 'exit'), ('events', 'events'),
                         ('postmortem', 'postmortem')):
        _REAL_SPAWN_GENERIC('prompt', 'sonnet', tag, channel=channel)
    files = sorted(logs.glob('vet_cli_*.log'))
    assert len(files) == 3, f'one transcript per spawn, got {[f.name for f in files]}'
    # Each file names exactly one agent — no second banner can follow a body.
    for f in files:
        headers = [ln for ln in f.read_text().splitlines()
                   if ln.startswith('=== ')]
        assert len(headers) == 1, f'{f.name} names >1 agent: {headers}'
    # And the three names are distinct, so no transcript overwrote another.
    assert len({f.name for f in files}) == 3


def test_same_second_same_channel_does_not_collide(monkeypatch, tmp_path):
    """Two reviews fired 3s apart today; a same-second pair must not share a
    file, or one agent's reasoning silently overwrites the other's."""
    logs = _spawn_capture(monkeypatch, tmp_path)
    fixed = datetime(2026, 8, 14, 10, 45, 37)
    monkeypatch.setattr(vet_mod, '_now', lambda: fixed)
    _REAL_SPAWN_GENERIC('p', 'opus', 'review #390', channel='review')
    _REAL_SPAWN_GENERIC('p', 'opus', 'review #390', channel='review')
    assert len(list(logs.glob('vet_cli_*.log'))) == 2


def test_transcript_name_keeps_the_trim_cron_and_the_trade_id(monkeypatch,
                                                              tmp_path):
    """`vet_cli_` prefix + `.log` suffix + flat in logs/, because the Pi's
    installed cron trims `-name "vet_cli_*.log"`; a subdirectory or a renamed
    file would silently stop being trimmed on the SD card. The id must survive
    so one transcript is findable among a day's fifty."""
    logs = _spawn_capture(monkeypatch, tmp_path)
    _REAL_SPAWN_GENERIC('p', 'opus', 'vet #352 tp', channel='exit')
    name = next(logs.glob('vet_cli_*.log')).name
    assert name.startswith('vet_cli_') and name.endswith('.log')
    assert '352' in name and 'exit' in name


def test_log_slug_is_path_safe():
    """Tags carry '#' and spaces; a raw tag would create a bad path or a
    nested directory."""
    s = vet_mod._log_slug('exit', 'vet #352 tp')
    assert '/' not in s and '\' not in s and '#' not in s and ' ' not in s
    assert vet_mod._log_slug('', '') == 'agent', 'never an empty filename'
