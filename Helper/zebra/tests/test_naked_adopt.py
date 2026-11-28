"""The adoption sweep for unresolved live naked entries (`zebra/naked_adopt.py`, P3c).

The book is `kotak_neo.fake.FakeNeo` behind the real kotak_neo client, so
"what the book says" is decided by the same `orders.resolve` live uses.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_adopt.py -v
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE.parents[2]))

from test_naked_entry import (_iso, store, parent, Sent, go, naked, twin,   # noqa: E402,F401
                              NOW, QUOTES, _q, LOT)
from kotak_neo import client as C           # noqa: E402
from kotak_neo import session as S          # noqa: E402
from kotak_neo import symbols as Y          # noqa: E402
from kotak_neo.fake import FakeNeo          # noqa: E402
from zebra import config as cfg             # noqa: E402
from zebra import naked_adopt as na         # noqa: E402
from zebra import naked_entry as ne         # noqa: E402
from zebra import naked_neo as nn           # noqa: E402

NSYM, TOK = 'TESTCO26DEC1000CE', '555001'
SESS = S.Session(3, '2026-12-02', 'c', 'https://x', 'T', 'SID', None, 'CK', 'U')


class Refresh:
    def __call__(self, gen, who):
        return S.Result(S.STILL_ALIVE, SESS, 'scripted')


@pytest.fixture
def neo(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    f = FakeNeo()
    f.set_quote(NSYM, 21.0, 21.5)
    return f


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'live')
    monkeypatch.setattr(cfg, 'NAKED_ADOPT_ABSENT_GRACE_SEC', 600.0)


def client(neo):
    return C.NeoClient('zbr', http=neo, current_fn=lambda: SESS, refresh_fn=Refresh())


def smap():
    return Y.ScripMap([Y.ScripRow(NSYM, TOK, 'TESTCO', date(2026, 12, 29), 1000.0, 'CE',
                                  LOT, 0.05, 'OPTSTK')])


def stuck(store, p, state, tag=None, at=NOW, **kw):
    t = {'state': state, 'mode': cfg.NAKED_LIVE, 'at': at.strftime('%Y-%m-%d %H:%M:%S'),
         'qty': LOT, 'limit': 21.5, 'neo_symbol': NSYM, 'neo_token': TOK}
    if tag:
        t['tag'] = tag
    t.update(kw)
    store.update_trade_fields(p['id'], **{ne.TWIN_FIELD: t})


def place(c, price='21.50', side='B'):
    tag = c.new_tag()
    c.place(C.OrderRequest(NSYM, TOK, side, LOT, price, tag))
    return tag


def test_off_mode_does_nothing(store, monkeypatch):
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'off')
    p = parent(store)
    stuck(store, p, 'unclear', tag='zbr-x')
    assert na.sweep(store, now=NOW) == []
    assert twin(store, p['id'])['state'] == 'unclear'


def test_a_filled_unclear_order_is_adopted_once(store, neo, live):
    c = client(neo)
    p = parent(store)
    tag = place(c)                                  # fills at the ask 21.5
    stuck(store, p, 'unclear', tag=tag)
    sent = Sent()
    lines = na.sweep(store, now=NOW, client=c, send=sent)
    [r] = naked(store)
    assert r['parent_id'] == p['id'] and r['entry_long_price'] == 21.5
    assert r['adopted'] is True and r['entry_tag'] == tag and r['neo_token'] == TOK
    assert twin(store, p['id'])['state'] == 'entered'
    assert any('adopted' in m for m in sent) and lines
    assert na.sweep(store, now=NOW, client=c, send=sent) == []     # idempotent
    assert len(naked(store)) == 1


def test_a_rejected_order_resolves_to_nothing(store, neo, live):
    c = client(neo)
    p = parent(store)
    neo.rms_reject('RMS:Margin Exceeds')
    tag = place(c)
    stuck(store, p, 'sending', tag=tag)
    na.sweep(store, now=NOW, client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'resolved_none' and naked(store) == []


def test_an_absent_tag_waits_for_the_grace_then_resolves(store, neo, live):
    c = client(neo)
    p = parent(store)
    stuck(store, p, 'unclear', tag='zbr-1202110000-abc1', at=NOW)
    na.sweep(store, now=NOW + timedelta(seconds=60), client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'unclear'            # may still surface
    na.sweep(store, now=NOW + timedelta(seconds=700), client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'resolved_none'


def test_a_resting_order_is_cancelled_then_resolved(store, neo, live):
    c = client(neo)
    p = parent(store)
    tag = place(c, price='20.00')                   # below the ask: rests
    stuck(store, p, 'unclear', tag=tag)
    na.sweep(store, now=NOW, client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'unclear'            # cancel sent this sweep
    na.sweep(store, now=NOW, client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'resolved_none' and naked(store) == []


def test_an_unrecorded_fill_is_filed_from_its_stamp(store, live):
    p = parent(store)
    stuck(store, p, 'unrecorded', fill=21.6, order_id='261127000500001', tag='zbr-t1')
    sent = Sent()
    na.sweep(store, now=NOW, send=sent)
    [r] = naked(store)
    assert r['entry_long_price'] == 21.6 and r['adopted'] is True
    assert twin(store, p['id'])['state'] == 'entered'


def test_a_stale_stamp_with_an_existing_record_is_cleared(store, neo, live):
    c = client(neo)
    p = parent(store)
    tag = place(c)
    stuck(store, p, 'unclear', tag=tag)
    na.sweep(store, now=NOW, client=c, send=Sent())              # adopts
    stuck(store, p, 'unclear', tag=tag)                          # a late stale stamp
    na.sweep(store, now=NOW, client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'entered' and len(naked(store)) == 1


def test_rehearsal_sending_has_no_book_and_is_cleared(store):
    p = parent(store)                                # fixture mode = rehearsal
    stuck(store, p, 'sending', tag=None)
    na.sweep(store, now=NOW, send=Sent())
    assert twin(store, p['id'])['state'] == 'resolved_none'


def test_unknown_book_keeps_the_block(store, neo, live, monkeypatch):
    c = client(neo)
    p = parent(store)
    stuck(store, p, 'unclear', tag='zbr-x1')
    monkeypatch.setattr(c, 'orders_tagged', lambda tag: (_ for _ in ()).throw(
        C.Unavailable('OMS down')))
    na.sweep(store, now=NOW + timedelta(hours=1), client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'unclear'


def test_a_crash_after_the_send_blocks_entries_until_adopted(store, neo, live):
    """End to end: the broker sends, then the process 'dies' before the result
    is stamped -- the `sending` witness alone must block and then be adopted."""
    c = client(neo)
    b = nn.NeoBroker(client=c, scrip_map=smap(), wait_s=0)
    p = parent(store)

    class Dies(Exception):
        pass

    real_buy = b.buy

    def buy_then_die(symbol, qty, limit, ctx):
        real_buy(symbol, qty, limit, ctx)           # the order reaches NEO
        raise SystemExit('killed mid-attempt')
    ne.begin_cycle(store, None, now=NOW, send=Sent())
    with pytest.raises(SystemExit):
        ne._attempt(store, None, p, NOW, 'live', type('B', (), {'buy': staticmethod(
            buy_then_die)})(), Sent(), False)
    assert twin(store, p['id'])['state'] == 'sending' and twin(store, p['id'])['tag']
    # gate 4: a second signal is refused while the witness stands
    p2 = parent(store, stock='OTHERCO')
    out, _ = go(store, p2, broker=b)
    assert out.state == 'refused' and 'unresolved' in out.reason
    na.sweep(store, now=NOW, client=c, send=Sent())
    assert twin(store, p['id'])['state'] == 'entered' and len(naked(store)) == 1


def test_a_live_stamp_is_settled_from_the_book_even_after_the_switch_moved(store, neo,
                                                                           monkeypatch):
    c = client(neo)
    p = parent(store)
    tag = place(c)                                  # really filled at NEO
    stuck(store, p, 'unclear', tag=tag, mode='live')
    monkeypatch.setattr(cfg, 'NAKED_LIVE', 'rehearsal')   # switch moved since
    na.sweep(store, now=NOW, client=c, send=Sent())
    [r] = naked(store)
    assert r['rehearsal'] is False and twin(store, p['id'])['state'] == 'entered'


def test_an_absent_tag_with_the_lot_still_held_goes_to_a_human(store, neo, live):
    """Overnight: yesterday's order is not in today's book, the lot is."""
    c = client(neo)
    p = parent(store)
    place(c)                                        # the account holds the lot
    stuck(store, p, 'unclear', tag='zbr-1201150000-old1', at=NOW - timedelta(days=1))
    sent = Sent()
    na.sweep(store, now=NOW, client=c, send=sent)
    assert twin(store, p['id'])['state'] == 'unclear'
    assert any('HOLDS' in m for m in sent)
