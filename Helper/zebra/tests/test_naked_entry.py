"""The live naked ENTRY hook (P3): gates in order, the broker seam, outcomes.

`zebra/naked_entry.py`; design G1-G4, F4, Q8. No broker is real here: the
rehearsal broker and scripted fakes stand in for Kotak NEO.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_entry.py -v
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import order_journal               # noqa: E402
from zebra import config as cfg             # noqa: E402
from zebra import monitor                   # noqa: E402
from zebra import naked_entry as ne         # noqa: E402
from zebra import naked_halt as nh          # noqa: E402
from zebra import strikes as strikes_mod    # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

LOT = 100
NOW = datetime(2026, 12, 2, 11, 0, 0)        # Wednesday 11:00
_n = iter(range(1, 10000))


def _bcs(stock):
    return {'long_symbol': '%s26DEC1000CE' % stock,
            'short_symbol': '%s26DEC1040CE' % stock,
            'long_strike': 1000.0, 'short_strike': 1040.0, 'debit': 20.0,
            'lot_size': LOT, 'expiry': '2026-12-29', 'entry_spot': 1000.0,
            'short_extrinsic': 5.0, 'long_ask_qty': 5000, 'short_bid_qty': 5000,
            'width': 40.0, 'debit_to_width_pct': 50.0, 'tp_spot': 1040.0}


QUOTES = {}


def _q(ask=21.5, bid=21.0, reliable=True, ask_qty=900, error=None):
    q = {'ask': ask, 'bid': bid, 'mid': (ask + bid) / 2, 'ask_qty': ask_qty,
         'bid_qty': 900, 'oi': 12000, 'reliable': reliable,
         'unreliable_reason': None if reliable else 'wide_book'}
    if error:
        q['error'] = error
    return q


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'rehearsal')
    monkeypatch.setattr(cfg, 'NAKED_CAPITAL', 500000.0)
    monkeypatch.setattr(cfg, 'NAKED_MAX_OPEN', 8)
    monkeypatch.setattr(cfg, 'NAKED_MAX_PREMIUM', 25000.0)
    monkeypatch.setattr(cfg, 'NAKED_ENTRY_CUTOFF', (15, 20))
    monkeypatch.setattr(cfg, 'NAKED_MAX_ORDERS_PER_SIGNAL', 2)
    monkeypatch.setattr(cfg, 'NAKED_OVERRIDE', False)
    monkeypatch.setattr(cfg, 'NAKED_LOSS_BUDGET', 0.0)
    monkeypatch.setattr(order_journal, 'LOG_DIR', tmp_path)
    monkeypatch.setattr('common.nse_holidays.is_holiday', lambda d: False)
    monkeypatch.setattr(monitor, '_entry_deadline', None)
    QUOTES.clear()

    def quote(kite, sym):
        q = QUOTES.get(sym, _q())
        if isinstance(q, list):                   # a sequence: one per call
            return q.pop(0) if len(q) > 1 else q[0]
        return q
    monkeypatch.setattr(strikes_mod, '_quote_option', quote)
    monkeypatch.setattr(strikes_mod, 'prefetch_quotes', lambda kite, syms: 0)
    ne.end_cycle()
    yield
    ne.end_cycle()


@pytest.fixture
def store():
    return ZebraStore()


def parent(store, stock='TESTCO', timeframe='weekly'):
    sig = store.add_signal({'stock': stock, 'timeframe': timeframe,
                            'direction': 'CE', 'st_value': 1040.0,
                            'st_direction': 'UP', 'signal_price': 1000.0,
                            'signal_gap_pct': 4.0})
    return store.mark_entered_bcs(sig['id'], _bcs(stock))


class Sent(list):
    def __call__(self, msg, dry_run=False):
        self.append(msg)
        return True


class Scripted:
    """A broker that answers from a list, one result per order."""
    def __init__(self, *results):
        self.results = list(results)
        self.orders = []

    def buy(self, symbol, qty, limit, ctx):
        self.orders.append((symbol, qty, limit))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def go(store, p, broker=None, now=NOW, cycle=True, sent=None):
    sent = sent if sent is not None else Sent()
    if cycle:
        ne.begin_cycle(store, None, now=now, send=sent)
    return ne.after_paper_entry(store, None, p, now=now, broker=broker,
                                send=sent), sent


def naked(store):
    return [t for t in store.load_trades() if t.get('structure') == 'naked_long']


def twin(store, pid):
    return store.find(pid).get(ne.TWIN_FIELD)


# ── happy path ─────────────────────────────────────────────────────────────

def test_rehearsal_enters_one_lot_at_the_ask_and_stamps_the_parent(store):
    p = parent(store)
    out, sent = go(store, p)
    assert out.state == 'entered' and out.orders == 1
    [r] = naked(store)
    assert r['parent_id'] == p['id'] and r['rehearsal'] is True
    assert r['entry_long_price'] == 21.5 and r['long_ask_entry'] == 21.5
    assert r['quantity'] == LOT and r['entry_order_ids'][0].startswith('REHEARSAL-')
    assert not any(k.startswith('short_') for k in r)
    t = twin(store, p['id'])
    assert t['state'] == 'entered' and t['id'] == r['id'] and t['mode'] == 'rehearsal'
    assert any('BOUGHT' in m for m in sent)


def test_rehearsal_journals_intent_and_result_as_dry_run(store):
    p = parent(store)
    go(store, p)
    recs = order_journal.read_day(NOW.strftime('%Y%m%d')) or order_journal.read_day()
    kinds = [r['kind'] for r in recs]
    assert kinds == ['intent', 'result'] and all(r['dry_run'] for r in recs)
    assert recs[0]['context']['leg'] == 'naked_long'


def test_off_does_nothing_at_all(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'off')
    p = parent(store)
    out, sent = go(store, p)
    assert out.state == 'off' and not naked(store) and not sent
    assert twin(store, p['id']) is None


def test_zebra_dry_run_places_and_writes_nothing(store):
    p = parent(store)
    ne.begin_cycle(store, None, now=NOW, send=Sent())
    out = ne.after_paper_entry(store, None, p, now=NOW, send=Sent(), dry_run=True)
    assert out.state == 'skipped' and not naked(store)


# ── the gates, in order ───────────────────────────────────────────────────

def test_no_verdict_this_cycle_refuses(store):
    p = parent(store)
    out, _ = go(store, p, cycle=False)
    assert out.state == 'refused' and 'not evaluated' in out.reason
    assert not naked(store)


def test_a_killed_halt_refuses(store):
    nh.halt('rehearsal', 'owner test', NOW, 'test')
    p = parent(store)
    out, sent = go(store, p)
    assert out.state == 'refused' and 'KILLED' in out.reason and not naked(store)


def test_an_unreadable_halt_file_refuses(store):
    nh.state_path('rehearsal').write_text('{not json', encoding='utf-8')
    p = parent(store)
    out, _ = go(store, p)
    assert out.state == 'refused' and 'fails closed' in out.reason


def test_spent_entry_budget_skips(store, monkeypatch):
    monkeypatch.setattr(monitor, '_entry_deadline', 0.0)
    out, _ = go(store, parent(store))
    assert out.state == 'skipped' and 'budget' in out.reason


def test_an_unresolved_twin_anywhere_blocks_every_entry(store):
    a = parent(store, 'AAA')
    store.update_trade_fields(a['id'], **{ne.TWIN_FIELD: {'state': 'unclear'}})
    out, _ = go(store, parent(store, 'BBB'))
    assert out.state == 'refused' and 'unresolved' in out.reason


@pytest.mark.parametrize('n_open,state', [(7, 'entered'), (8, 'skipped')])
def test_cap_is_exactly_max_open(store, n_open, state):
    for i in range(n_open):
        go(store, parent(store, 'S%02d' % i))
    assert len(naked(store)) == n_open
    out, _ = go(store, parent(store, 'NEW'))
    assert out.state == state
    if state == 'skipped':
        assert 'cap' in out.reason


def test_a_closed_twin_does_not_count_against_the_cap(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_MAX_OPEN', 1)
    go(store, parent(store, 'AAA'))
    [r] = naked(store)
    store.set_trade_status(r['id'], 'exited')
    out, _ = go(store, parent(store, 'BBB'))
    assert out.state == 'entered'


def test_one_per_stock(store):
    go(store, parent(store, 'AAA'))
    out, _ = go(store, parent(store, 'AAA', timeframe='monthly'))   # 2nd signal, same stock
    assert out.state == 'skipped' and 'already holding AAA' in out.reason


@pytest.mark.parametrize('hm,state', [((15, 19), 'entered'), ((15, 20), 'skipped')])
def test_cutoff_boundary(store, hm, state):
    now = NOW.replace(hour=hm[0], minute=hm[1])
    out, _ = go(store, parent(store), now=now)
    assert out.state == state
    if state == 'skipped':
        assert out.reason.startswith('late')


@pytest.mark.parametrize('ask,state', [(250.0, 'entered'), (250.01, 'skipped')])
def test_premium_cap_boundary_skips_never_resizes(store, ask, state):
    QUOTES['TESTCO26DEC1000CE'] = _q(ask=ask, bid=ask - 0.5)
    out, _ = go(store, parent(store))
    assert out.state == state
    if state == 'skipped':
        assert 'never resize' in out.reason and not naked(store)
    else:
        assert naked(store)[0]['quantity'] == LOT


@pytest.mark.parametrize('q,needle', [
    (_q(reliable=False), 'unreliable'),
    (_q(ask_qty=0), 'no ask'),
    (_q(error='429'), 'quote error'),
])
def test_a_bad_quote_skips(store, q, needle):
    QUOTES['TESTCO26DEC1000CE'] = q
    out, _ = go(store, parent(store))
    assert out.state == 'skipped' and needle in out.reason and not naked(store)


def test_live_without_a_broker_fails_closed_and_alerts(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    out, sent = go(store, parent(store))
    assert out.state == 'refused' and 'NEO' in out.reason and not naked(store)
    assert any('not entered' in m for m in sent)


def test_validation_runs_before_any_order(store):
    p = parent(store)
    p = dict(p, expiry='2020-01-01')                # would not validate
    b = Scripted()
    out, _ = go(store, p, broker=b)
    assert out.state == 'refused' and 'validate' in out.reason and b.orders == []


# ── broker outcomes ────────────────────────────────────────────────────────

def test_price_reject_requotes_and_retries_once(store):
    QUOTES['TESTCO26DEC1000CE'] = [_q(ask=21.5), _q(ask=22.0)]
    b = Scripted(ne.BuyResult('rejected', message='price band', retry=True),
                 ne.BuyResult('filled', fill_price=22.0, order_id='X2'))
    out, _ = go(store, parent(store), broker=b)
    assert out.state == 'entered' and out.orders == 2
    assert [o[2] for o in b.orders] == [21.5, 22.0]
    assert naked(store)[0]['long_ask_entry'] == 22.0     # the touch of the order that filled


def test_never_more_than_two_orders(store):
    b = Scripted(ne.BuyResult('rejected', message='band', retry=True),
                 ne.BuyResult('rejected', message='band', retry=True),
                 ne.BuyResult('filled', fill_price=21.5))
    out, sent = go(store, parent(store), broker=b)
    assert out.state == 'refused' and len(b.orders) == 2 and not naked(store)


def test_a_margin_reject_stops_without_retry(store):
    b = Scripted(ne.BuyResult('rejected', message='insufficient funds', retry=False))
    out, sent = go(store, parent(store), broker=b)
    assert out.state == 'refused' and len(b.orders) == 1
    assert any('REJECTED' in m and 'not a price reject' in m for m in sent)


def test_retry_requote_over_the_cap_skips(store):
    QUOTES['TESTCO26DEC1000CE'] = [_q(ask=21.5), _q(ask=300.0, bid=299.0)]
    b = Scripted(ne.BuyResult('rejected', message='band', retry=True))
    out, _ = go(store, parent(store), broker=b)
    assert out.state == 'skipped' and 'retry' in out.reason and len(b.orders) == 1


def test_unfilled_skips_quietly(store):
    b = Scripted(ne.BuyResult('unfilled', message='cancelled, nothing filled'))
    out, _ = go(store, parent(store), broker=b)
    assert out.state == 'skipped' and not naked(store)


@pytest.mark.parametrize('res', [ne.BuyResult('unclear', message='timeout'),
                                 RuntimeError('socket closed')])
def test_unclear_leaves_the_intent_open_and_blocks_later_entries(store, res):
    p = parent(store, 'AAA')
    out, sent = go(store, p, broker=Scripted(res))
    assert out.state == 'unclear' and not naked(store)
    assert twin(store, p['id'])['state'] == 'unclear'
    recs = order_journal.read_day()
    assert [r['kind'] for r in recs] == ['intent']        # no result: the witness
    assert any('STATE UNKNOWN' in m for m in sent)
    out2, _ = go(store, parent(store, 'BBB'), broker=Scripted())
    assert out2.state == 'refused' and 'unresolved' in out2.reason


def test_a_fill_the_store_refuses_is_stamped_unrecorded(store, monkeypatch):
    def boom(rec):
        raise ValueError('disk full')
    monkeypatch.setattr(store, 'add_naked_live', boom)
    p = parent(store)
    out, sent = go(store, p, broker=Scripted(ne.BuyResult('filled', fill_price=21.5,
                                                          order_id='O1')))
    assert out.state == 'unrecorded'
    assert twin(store, p['id'])['state'] == 'unrecorded'
    assert any('NOT WRITTEN' in m for m in sent)


def test_a_fill_over_the_cap_is_recorded_and_alerted_red(store):
    b = Scripted(ne.BuyResult('filled', fill_price=260.0, order_id='O1'))
    QUOTES['TESTCO26DEC1000CE'] = _q(ask=249.0, bid=248.5)
    out, sent = go(store, parent(store), broker=b)
    assert out.state == 'entered' and out.extra.get('over_cap')
    assert any('OVER THE' in m for m in sent)


def test_filled_without_a_price_is_not_a_fill():
    with pytest.raises(ValueError):
        ne.BuyResult('filled', fill_price=None)
    with pytest.raises(ValueError):
        ne.BuyResult('maybe')


# ── the cycle ─────────────────────────────────────────────────────────────

def test_begin_cycle_alerts_a_kill_and_end_cycle_drops_the_verdict(store, monkeypatch):
    go(store, parent(store))
    [r] = naked(store)
    # mark the long at 30% of entry on a 1-lot book: -15 x 100 = -1,500; force
    # the kill with a tiny capital so the drawdown crosses 25%
    monkeypatch.setattr(cfg, 'NAKED_CAPITAL', 4000.0)
    QUOTES[r['long_symbol']] = _q(ask=6.6, bid=6.45)
    sent = Sent()
    v = ne.begin_cycle(store, object(), now=NOW, send=sent)
    assert v['allowed'] is False
    assert any('KILLED' in m and 'zebra resume' in m for m in sent)
    ne.end_cycle()
    out = ne.after_paper_entry(store, None, parent(store, 'NEW'), now=NOW, send=Sent())
    assert out.state == 'refused' and 'not evaluated' in out.reason


def test_an_unreliable_mark_is_not_used(store):
    go(store, parent(store))
    [r] = naked(store)
    QUOTES[r['long_symbol']] = _q(ask=1.0, bid=0.5, reliable=False)
    assert ne._marks(object(), naked(store)) == {}


def test_the_hook_runs_after_the_paper_entry_and_never_breaks_it(store, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(a[2]['id'])
        raise RuntimeError('naked path exploded')
    monkeypatch.setattr(ne, 'after_paper_entry', boom)
    sig = store.add_signal({'stock': 'HOOK', 'timeframe': 'weekly',
                            'direction': 'CE', 'st_value': 1040.0,
                            'st_direction': 'UP', 'signal_price': 1000.0,
                            'signal_gap_pct': 4.0})
    store.mark_triggered(sig['id'], 1000.0, 4.0, {})
    built = monitor._enter_as_bcs(store, None, store.find(sig['id']), {}, 1000.0,
                                  bcs=_bcs('HOOK'))
    assert built is not None and built[1]['status'] == 'entered'
    assert calls == [sig['id']]
