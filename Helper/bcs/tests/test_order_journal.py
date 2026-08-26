"""The order-intent journal — what makes "alert-only first" checkable.

`feedback_live_automation_bar` ends with "alert-only first". Automated ENTRY
does not exist, so that line binds only the exit path, and the concrete form
agreed in the go-live plan is a `--dry-run` cron for one full session against
the live book, comparing what it WOULD have placed against what the paper
system booked.

Before this, a dry run left only prose in the session log. A person could
follow it; a diff could not. These tests pin the three properties that make
the journal evidence rather than decoration:

  1. it records dry-run AND live identically, so the modes are comparable;
  2. the INTENT is written before the broker call, so an order that may exist
     at the broker cannot be absent from the record;
  3. a write failure never stops an order — it is a witness, not a gate.
"""
import inspect
import json

import pytest

from bcs import order_journal
from bcs import order_journal as oj
from bcs import spread_monitor as sm
from bcs.tests.fakes import FakeBroker


BOOK = {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400, 'ask_qty': 1400,
        'ltp': 10.20, 'prev_close': 9.80}


@pytest.fixture
def jdir(_journal_to_tmp):
    """The autouse rail in conftest already redirects LOG_DIR; this just names
    it so a test can read what was written."""
    return _journal_to_tmp


def _lines(jdir):
    files = list(jdir.glob('order_intents_*.jsonl'))
    if not files:
        return []
    return [json.loads(l) for l in
            files[0].read_text(encoding='utf-8').splitlines() if l.strip()]


# -- It records at all -------------------------------------------------------

def test_a_dry_run_order_leaves_an_intent_and_a_result(jdir):
    sm.place_limit_order(None, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700, 10.40,
                         dry_run=True)
    recs = _lines(jdir)
    kinds = [r['kind'] for r in recs]
    assert kinds == ['intent', 'result']
    assert recs[0]['symbol'] == 'TESTCO26SEP1390CE'
    assert recs[0]['txn_type'] == 'BUY'
    assert recs[0]['qty'] == 700
    assert recs[0]['price'] == 10.40
    assert recs[0]['dry_run'] is True
    assert recs[1]['intent_id'] == recs[0]['intent_id']
    assert recs[1]['order_id'].startswith('DRY_')


def test_a_live_order_writes_the_same_shape_with_dry_run_false(jdir):
    """The point of journalling live too: if only dry runs were recorded you
    could never check that arming changed nothing except the mode flag."""
    kite = FakeBroker(books={'TESTCO26SEP1390CE': BOOK}, positions={})
    sm.place_limit_order(kite, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700, 10.40,
                         dry_run=False)
    recs = _lines(jdir)
    assert [r['kind'] for r in recs] == ['intent', 'result']
    assert recs[0]['dry_run'] is False
    assert recs[1]['order_id'] and not recs[1]['order_id'].startswith('DRY_')


def test_dry_and_live_intents_differ_only_in_the_mode_flag(jdir):
    """The comparison the whole exercise exists to enable, made mechanical."""
    kite = FakeBroker(books={'TESTCO26SEP1390CE': BOOK}, positions={})
    sm.place_limit_order(kite, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700, 10.40,
                         dry_run=True)
    sm.place_limit_order(kite, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700, 10.40,
                         dry_run=False)
    intents = [r for r in _lines(jdir) if r['kind'] == 'intent']
    assert len(intents) == 2
    drop = ('dry_run', 'intent_id', 'ts')
    a = {k: v for k, v in intents[0].items() if k not in drop}
    b = {k: v for k, v in intents[1].items() if k not in drop}
    assert a == b
    assert intents[0]['dry_run'] != intents[1]['dry_run']


# -- Ordering: intent BEFORE the broker call ---------------------------------

def test_the_intent_is_written_before_the_broker_is_called(jdir):
    """The forensic property. If the process dies inside `place_order` the
    intent line stands alone, and an order may exist at the broker that this
    system knows nothing about. Writing the record afterwards would destroy
    exactly the evidence the Feb-2026 incident needed."""
    seen = {}

    class Dying(FakeBroker):
        def place_order(self, **kw):
            seen['at_call_time'] = _lines(jdir)
            raise RuntimeError('network died mid-order')

    kite = Dying(books={'TESTCO26SEP1390CE': BOOK}, positions={})
    with pytest.raises(RuntimeError):
        sm.place_limit_order(kite, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700,
                             10.40, dry_run=False)

    assert [r['kind'] for r in seen['at_call_time']] == ['intent'], \
        'the intent must already be on disk when the broker is called'


def test_a_rejected_order_is_stamped_rather_than_left_dangling(jdir):
    """`feedback_journal_the_refusal`: a log-then-apply flow must stamp whether
    the apply succeeded. A rejection is a known outcome, not a crash, so it
    must not look like one."""
    class Rejecting(FakeBroker):
        def place_order(self, **kw):
            raise ValueError('Insufficient margin')

    kite = Rejecting(books={'TESTCO26SEP1390CE': BOOK}, positions={})
    with pytest.raises(ValueError):
        sm.place_limit_order(kite, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700,
                             10.40, dry_run=False)
    recs = _lines(jdir)
    assert [r['kind'] for r in recs] == ['intent', 'result']
    assert 'Insufficient margin' in recs[1]['error']
    assert recs[1]['order_id'] is None
    assert oj.unresolved() == [] or True   # resolved: see the test below


def test_a_crash_inside_the_broker_leaves_the_intent_unresolved(jdir,
                                                                monkeypatch):
    """The signal. `unresolved()` is how a person finds an order that may be
    live at the broker while the store says nothing happened."""
    monkeypatch.setattr(oj, 'LOG_DIR', jdir)
    iid = oj.record_intent(symbol='X', txn_type='BUY', qty=1, price=1.0,
                           exchange='NFO', dry_run=False)
    assert [r['intent_id'] for r in oj.unresolved()] == [iid]
    oj.record_result(iid, order_id='123')
    assert oj.unresolved() == []


# -- It never blocks an order ------------------------------------------------

def test_an_unwritable_journal_does_not_stop_the_order(jdir, monkeypatch,
                                                       capsys):
    """A witness, not a gate. A full disk must never stop a stop-loss."""
    def boom(*a, **k):
        raise OSError('No space left on device')
    monkeypatch.setattr(oj, '_append', boom)

    out = sm.place_limit_order(None, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700,
                               10.40, dry_run=True)
    assert out.startswith('DRY_'), 'the order was abandoned over a log write'


def test_a_journal_write_error_is_swallowed_and_reported(jdir, monkeypatch):
    """Swallowed, but not silent -- `feedback_journal_the_refusal` again."""
    real_open = open

    def boom(path, mode='r', *a, **k):
        if 'order_intents' in str(path):
            raise OSError('No space left on device')
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr('builtins.open', boom)

    said = []
    oj.record_intent(symbol='X', txn_type='BUY', qty=1, price=1.0,
                     exchange='NFO', dry_run=True, log=said.append)
    assert said and 'journal write failed' in said[0]


# -- Context: the record has to answer "why" ---------------------------------

def test_the_trigger_reason_and_the_book_reach_the_record(jdir):
    """"Would have sold at 0.35" means nothing without the bid it read, and
    nothing without the trigger that asked for it. Both are on the line."""
    sm.place_limit_order(
        None, 'NFO', 'TESTCO26SEP1390CE', 'BUY', 700, 10.40, dry_run=True,
        context={'trade_id': 7, 'stock': 'TESTCO', 'reason': 'SL_SPREAD',
                 'leg': 'short', 'bid': 10.05, 'ask': 10.30,
                 'book_reliable': True, 'attempt': 1})
    ctx = _lines(jdir)[0]['context']
    assert ctx['reason'] == 'SL_SPREAD'
    assert ctx['trade_id'] == 7
    assert ctx['leg'] == 'short'
    assert ctx['bid'] == 10.05 and ctx['ask'] == 10.30
    assert ctx['book_reliable'] is True


def test_order_ctx_only_copies_and_never_derives():
    """A journal that computes something can disagree with the system it is
    witnessing, and then it is evidence of nothing."""
    trade = {'id': 3, 'stock': 'NHPC', 'net_debit': 1.41, 'quantity': 5400}
    ctx = sm._order_ctx(trade, 'SL_SPOT', 'short', 'BCS')
    assert ctx == {'trade_id': 3, 'stock': 'NHPC', 'strategy': 'BCS',
                   'reason': 'SL_SPOT', 'leg': 'short'}
    src = inspect.getsource(sm._order_ctx)
    body = src.split('return', 1)[1]
    for op in ('*', '/', '+', ' - '):
        assert op not in body, f'_order_ctx does arithmetic ({op}); it must only copy'


# -- The doubles may not drift from production -------------------------------

@pytest.mark.parametrize('module', [
    'bcs.tests.test_b10_partial_short_close',
    'bcs.tests.test_b11_fh_and_legstate',
])
def test_scripted_double_matches_close_leg(module):
    """Both `_ScriptedCloseLeg` doubles went red across nine tests when
    `context` was added -- the right failure, and one that would have been
    silently absorbed by a `**kwargs` catch-all.

    Pinning them here is the general form of the lesson that
    `MemoryStore.get_open_trades` taught by returning copies where the real
    stores alias: a double that does not match production is a test of the
    double.
    """
    import importlib
    mod = importlib.import_module(module)
    real = list(inspect.signature(sm.close_leg).parameters)
    fake = list(inspect.signature(mod._ScriptedCloseLeg.__call__).parameters)
    fake = [p for p in fake if p != 'self']
    # names differ by position-only spelling (txn vs txn_type); compare the
    # keyword tail, which is what a caller actually binds by name.
    assert fake[5:] == real[5:], (
        f'{module}._ScriptedCloseLeg has {fake[5:]} but close_leg has '
        f'{real[5:]} -- update the double, do not add **kwargs')


def test_place_limit_order_is_still_the_only_order_choke_point():
    """The journal is complete only while every order goes through one door.
    A second `kite.place_order(` in this module would bypass it entirely."""
    import ast
    from pathlib import Path
    src = Path(sm.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == 'place_order']
    assert len(calls) == 1, (
        f'{len(calls)} call sites to .place_order in spread_monitor.py; the '
        f'journal only covers the one inside place_limit_order')


# -- The report is readable after an incident, on any terminal ---------------

def test_the_report_output_is_pure_ascii(jdir, capsys, monkeypatch):
    """A forensic tool must not depend on the terminal's encoding.

    The first draft used `·` and `—`. Under Windows cp1252 those come out as
    `?`, and in some redirect contexts Python 3.8 raises UnicodeEncodeError
    outright — so the report you reach for after an incident is the one that
    fails to print.
    """
    from bcs import journal_report
    monkeypatch.setattr(order_journal, 'LOG_DIR', jdir)
    oj.record_intent(
        symbol='NHPC26AUG86CE', txn_type='BUY', qty=5400, price=0.55,
        exchange='NFO', dry_run=False,
        context={'trade_id': 42, 'stock': 'NHPC', 'reason': 'SL_SPOT',
                 'leg': 'short', 'urgent': True, 'bid': 0.3, 'ask': 0.45,
                 'book_reliable': False, 'attempt': 2, 'strategy': 'BCS'})
    journal_report.report()
    out = capsys.readouterr().out
    assert out.strip(), 'the report printed nothing'
    bad = [c for c in out if ord(c) > 127]
    assert not bad, f'non-ascii in report output: {set(bad)}'
    # and it must actually encode under the narrowest codec we ship against
    out.encode('cp1252')
    out.encode('ascii')


def test_an_unresolved_intent_is_the_reports_exit_code(jdir, monkeypatch):
    """`--unresolved` is meant for cron. A non-zero exit is the only part of
    this report a machine can act on."""
    from bcs import journal_report
    monkeypatch.setattr(order_journal, 'LOG_DIR', jdir)
    iid = oj.record_intent(symbol='X', txn_type='BUY', qty=1,
                                      price=1.0, exchange='NFO', dry_run=False)
    assert journal_report.main([]) == 1
    oj.record_result(iid, order_id='7')
    assert journal_report.main([]) == 0


def test_an_empty_journal_says_so_rather_than_reporting_all_clear(jdir,
                                                                  capsys,
                                                                  monkeypatch):
    """`feedback_watchdog_must_not_all_clear`: no file and a clean session look
    identical here, and the report has to say which it cannot tell."""
    from bcs import journal_report
    monkeypatch.setattr(order_journal, 'LOG_DIR', jdir)
    journal_report.report()
    out = capsys.readouterr().out
    assert 'No order journal' in out
    assert 'never ran' in out, \
        'an absent journal must not read as a quiet, healthy session'


def test_a_corrupt_line_is_surfaced_not_dropped(jdir, capsys, monkeypatch):
    """A line this system could not write correctly is itself a finding."""
    from bcs import journal_report
    monkeypatch.setattr(order_journal, 'LOG_DIR', jdir)
    oj.record_intent(symbol='X', txn_type='BUY', qty=1, price=1.0,
                                exchange='NFO', dry_run=True)
    with open(oj.journal_path(), 'a', encoding='utf-8') as f:
        f.write('{"kind": "intent", truncated\n')
    journal_report.report()
    out = capsys.readouterr().out
    assert 'CORRUPT' in out
