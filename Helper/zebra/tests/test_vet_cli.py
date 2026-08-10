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
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(hours=2))
    assert cli.cmd_vet_decide(_decide(verdict='veto')) == 0
    assert 'discarded' in capsys.readouterr().out
    assert store.find(1)['vet']['state'] == vet_mod.UNAVAILABLE


def test_journal_is_written_even_when_the_verdict_is_discarded(wired):
    """Journal-first ordering: a late verdict still leaves an audit trail of
    what Claude concluded, even though nothing was acted on."""
    store, journal = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    vet_mod.expire_stale(store, now=datetime.now() + timedelta(hours=2))
    cli.cmd_vet_decide(_decide(verdict='veto'))
    assert len(journal.all()) == 1


def test_bad_confidence_is_rejected_before_anything_lands(wired):
    store, journal = wired
    vet_mod.request_entry_vet(store, 1, CONTEXT, spawn=False)
    with pytest.raises(ValueError, match='confidence'):
        cli.cmd_vet_decide(_decide(confidence=42.0))
    assert journal.all() == []
    assert vet_mod.is_pending(store.find(1))     # signal untouched, still vettable
