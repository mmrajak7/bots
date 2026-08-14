"""`zebra quote` — the live re-quote the vetting agents could not run.

Measured 2026-08-14: eleven agents in one morning reported they could not
obtain an independent book, and every one docked its own confidence for it.
The checklist demanded a step the permission set forbade. These tests guard
the three things that make the verb worth having:

  1. it is READ-ONLY (an agent must never be able to move the book it judges),
  2. a refusal is distinguishable from a low price (the NHPC failure was
     acting on a number nobody could trade at), and
  3. it reuses the ENGINE's valuation, so agent and engine cannot disagree.

Run:  cd Helper && python -m pytest zebra/tests/test_quote_verb.py -v
"""
import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import __main__ as cli        # noqa: E402
from zebra import config as cfg          # noqa: E402
from zebra import trade_store as ts_mod  # noqa: E402

POSITION = {
    'id': 7, 'version': 1, 'status': 'entered', 'stock': 'NBCC',
    'direction': 'PE', 'structure': 'bcs', 'pricing_basis': 'fill',
    'long_symbol': 'NBCC26SEP90PE', 'short_symbol': 'NBCC26SEP87.5PE',
    'long_strike': 90.0, 'short_strike': 87.5, 'width': 2.5,
    'debit': 1.00, 'debit_sl_value': 0.50, 'entry_spot': 90.27,
    'tp_spot': 87.06, 'sl_spot': 87.56, 'expiry': '2026-09-29',
}


@pytest.fixture
def book(tmp_path, monkeypatch):
    """One entered position, a stub Kite, and no network anywhere."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    store = ts_mod.ZebraStore(config={})
    store._load_local()
    with store._mutate():
        store._trades.append(dict(POSITION))
    monkeypatch.setattr(ts_mod, '_store', store)
    monkeypatch.setattr(ts_mod, 'get_store', lambda: store)

    from zebra import scanner as sc
    monkeypatch.setattr(sc, '_get_kite', lambda: object())
    monkeypatch.setattr(sc, 'get_ltp', lambda k, syms: {'NBCC': 90.27})
    return store


def _quote(id=7):
    """Run the verb, return (rc, parsed stdout)."""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_quote(Namespace(id=id))
    return rc, json.loads(buf.getvalue())


def _legs(bid_l, ask_l, bid_s, ask_s, reliable=True):
    def leg(sym, b, a):
        return {'symbol': sym, 'bid': b, 'ask': a, 'mid': round((b + a) / 2, 2),
                'oi': 435500, 'last': round((b + a) / 2, 2),
                'spread_pct': 2.6, 'reliable': reliable,
                'unreliable_reason': None}
    return {'long': leg('NBCC26SEP90PE', bid_l, ask_l),
            'short': leg('NBCC26SEP87.5PE', bid_s, ask_s)}


def _stub_structure_quote(monkeypatch, **ret):
    from zebra import monitor
    monkeypatch.setattr(monitor, '_structure_quote', lambda k, t, s: ret)


# -- it answers at all ----------------------------------------------------
def test_quotes_an_open_position_with_both_legs(book, monkeypatch):
    _stub_structure_quote(monkeypatch, mid=1.20, reliable=True, reason=None,
                          legs=_legs(3.80, 3.92, 2.60, 2.72), floored=False)
    rc, out = _quote()
    assert rc == 0 and out['kind'] == 'position'
    assert out['value'] == 1.20 and out['spot'] == 90.27
    assert out['legs']['long']['bid'] == 3.80
    assert out['legs']['short']['ask'] == 2.72
    # The agent is judging an exit; it needs the reference points to judge it
    # AGAINST, or it can only say "the book is what it is".
    assert out['entry_debit'] == 1.00 and out['debit_sl_value'] == 0.50
    assert out['pnl_pct'] == 20.0


def test_it_reports_the_basis_the_position_was_entered_on(book, monkeypatch):
    """A number quoted on a different basis than the position trades on is a
    different number. The agent must be able to see which it got."""
    _stub_structure_quote(monkeypatch, mid=1.20, reliable=True, reason=None,
                          legs=_legs(3.80, 3.92, 2.60, 2.72), floored=False)
    assert _quote()[1]['pricing_basis'] == 'fill'


# -- a refusal must not read as a price -----------------------------------
def test_unusable_book_is_a_refusal_not_a_zero(book, monkeypatch):
    """THE test. NHPC cost Rs 7,297 by acting on a price nobody could trade
    at. A null value that an agent skims as 'fine' would rebuild that."""
    _stub_structure_quote(monkeypatch, mid=None, reliable=False,
                          reason='no_two_way_book', legs=None, floored=False)
    rc, out = _quote()
    assert out['value'] is None
    assert out['reliable'] is False
    assert 'REFUSAL' in out['advice'], 'silence must announce itself'
    assert 'no_two_way_book' in out['advice']
    assert 'pnl_pct' not in out, 'never compute a P&L off a price that is not one'


def test_a_rejected_quote_says_so_explicitly(book, monkeypatch):
    """Below the intrinsic floor is the ABB #242 shape — an estimate is not a
    price, so the engine defers. The agent must be told the same thing."""
    _stub_structure_quote(monkeypatch, mid=None, reliable=False,
                          reason='below_intrinsic_floor',
                          rejected='value 0.40 below intrinsic floor 2.10',
                          legs=_legs(3.80, 3.92, 2.60, 2.72), floored=False)
    out = _quote()[1]
    assert 'intrinsic floor' in out['rejected']
    assert out['value'] is None


def test_unreliable_book_still_surfaces_the_reason(book, monkeypatch):
    _stub_structure_quote(monkeypatch, mid=1.20, reliable=False,
                          reason='short one_sided',
                          legs=_legs(3.80, 3.92, 2.60, 2.72, reliable=False),
                          floored=False)
    out = _quote()[1]
    assert out['reliable'] is False and out['unreliable_reason'] == 'short one_sided'


def test_dead_token_refuses_rather_than_quoting_zero(book, monkeypatch):
    """An expired Kite token must not be indistinguishable from a flat book —
    that is how a silent auth failure becomes an endorsed exit."""
    from zebra import scanner as sc

    def _dead():
        raise RuntimeError('token dead')
    monkeypatch.setattr(sc, '_get_kite', _dead)
    rc, out = _quote()
    assert rc == 1 and 'token dead' in out['error']
    assert 'do not upgrade confidence' in out['advice']
    assert 'value' not in out


def test_missing_trade_is_json_not_a_traceback(book):
    """The caller is an LLM parsing stdout."""
    rc, out = _quote(id=999)
    assert rc == 1 and 'not found' in out['error']


def test_exited_trade_has_nothing_to_quote(book, monkeypatch):
    with book._mutate():
        book.find(7)['status'] = 'exited'
    rc, out = _quote()
    assert rc == 1 and 'nothing to quote' in out['error']


# -- read-only, and reachable ---------------------------------------------
def test_quote_never_writes_the_store(book, monkeypatch):
    """An agent that could move the book it judges is not a judge. The verb
    opens no lock and calls no mutator."""
    _stub_structure_quote(monkeypatch, mid=1.20, reliable=True, reason=None,
                          legs=_legs(3.80, 3.92, 2.60, 2.72), floored=False)
    before = json.dumps(book.find(7), sort_keys=True)

    def _boom(*a, **k):
        pytest.fail('quote must not mutate the store')
    monkeypatch.setattr(book, '_mutate', _boom)
    _quote()
    assert json.dumps(book.find(7), sort_keys=True) == before


def test_the_verb_is_granted_and_not_denied():
    """It must be reachable by a spawned agent under the EXISTING coarse
    grant, and must not accidentally match a deny pattern — an unmatched or
    denied grant is indistinguishable from a broken agent, which is the bug
    that cost four spawns on 2026-08-14."""
    cmd = '%s -m zebra quote 7' % sys.executable
    assert any(p.startswith('Bash(') and '-m zebra' in p
               for p in cfg.VET_ALLOWED_TOOLS)
    for pattern in cfg.VET_DENIED_TOOLS:
        verb = pattern[len('Bash(*'):-len('*)')]
        assert verb not in cmd, '%s would block the read-only quote' % pattern


def test_vetting_doc_names_the_verb_for_both_channels():
    """Agents reach for what the doc spells. Today they reached for Kite MCP
    and inline Python — both unpermitted — because nothing told them this
    existed."""
    doc = cfg.VETTING_DOC.read_text(encoding='utf-8')
    assert doc.count('zebra quote') >= 3, 'entry AND exit sections must say it'
    assert 'unpermitted' in doc, 'and must say what NOT to reach for'


# -- the signal path: the entry channel's only view of the short leg -------
def _stub_builder(monkeypatch, bcs):
    from zebra import strikes as strikes_mod
    monkeypatch.setattr(strikes_mod, 'analyze', lambda *a, **k: {
        'expiry': '2026-09-29', 'dte': 46, 'lot_size': 3000,
        'atm_strike': 90.0, 'atm_quote': {'bid': 3.80, 'ask': 3.92,
                                          'mid': 3.86, 'oi': 435500}})
    monkeypatch.setattr(strikes_mod, 'analyze_bcs', lambda *a, **k: bcs)


def test_a_triggered_signal_rebuilds_the_pair_that_would_open(book,
                                                              monkeypatch):
    """The entry context carries the ATM leg only — the short is picked after
    that snapshot. This is the agent's ONLY view of the short leg's book."""
    with book._mutate():
        t = book.find(7)
        t['status'] = 'triggered'
        t['st_value'] = 87.06
    _stub_builder(monkeypatch, {'k_l': 90.0, 'k_s': 87.5, 'width': 2.5,
                                'debit': 1.00, 'short_bid': 2.60,
                                'short_ask': 2.72, 'short_oi': 279000})
    rc, out = _quote()
    assert rc == 0 and out['kind'] == 'signal' and out['buildable'] is True
    assert out['bcs']['short_bid'] == 2.60, 'the short leg must be visible'
    assert out['dte'] == 46


def test_gates_failing_now_is_a_finding_not_a_tool_error(book, monkeypatch):
    """`buildable: false` means the engine would SUPPRESS this entry at the
    current book. An agent must read that as evidence, not as a broken tool —
    so it carries a reason and still exits 0."""
    with book._mutate():
        book.find(7)['status'] = 'triggered'
    _stub_builder(monkeypatch, {'error': 'debit 48.2% of width > 45%'})
    rc, out = _quote()
    assert rc == 0, 'a suppressed structure is an answer, not a failure'
    assert out['buildable'] is False
    assert '48.2%' in out['reason']


# -- the stubs must not be hiding a signature drift -----------------------
def test_it_really_calls_the_engines_valuation(book, monkeypatch):
    """Every test above stubs `_structure_quote`, so a signature change in the
    real one would pass them all while the verb raised in production. Drive
    the REAL function with only the Kite call stubbed."""
    from zebra import strikes as strikes_mod

    def fake_quote(kite, symbol):
        px = {'NBCC26SEP90PE': (3.80, 3.92),
              'NBCC26SEP87.5PE': (2.60, 2.72)}[symbol]
        return {'bid': px[0], 'ask': px[1], 'mid': round(sum(px) / 2, 2),
                'oi': 435500, 'last': px[0], 'bid_qty': 3000, 'ask_qty': 3000,
                'ltp': px[0], 'ltp_fresh': True, 'reliable': True,
                'unreliable_reason': None}
    monkeypatch.setattr(strikes_mod, '_quote_option', fake_quote)
    rc, out = _quote()
    assert rc == 0
    # fill basis: sell the long at its BID, buy the short back at its ASK.
    assert out['value'] == round(3.80 - 2.72, 2)
    assert out['legs']['short']['symbol'] == 'NBCC26SEP87.5PE'


def test_a_silent_auth_failure_is_not_reported_as_a_thin_book(book,
                                                              monkeypatch):
    """Found by running the verb for real against an expired token.

    `get_ltp` SWALLOWS an auth error and returns {}, so a dead token arrives
    as a missing spot rather than an exception — and the verb happily said
    "no transactable price right now", a claim about the market made on
    evidence that was really a claim about our login. An agent reading that
    would defer for the wrong reason today and, worse, could read a null value
    as a collapsed position tomorrow.
    """
    from zebra import scanner as sc
    monkeypatch.setattr(sc, 'get_ltp', lambda k, syms: {})
    rc, out = _quote()
    assert rc == 1, 'a broken pipe is a failure, not a valuation'
    assert 'token failure' in out['error'] and 'NOT a thin book' in out['error']
    assert 'do not read it as a low valuation' in out['advice']
    assert 'value' not in out and 'pnl_pct' not in out
