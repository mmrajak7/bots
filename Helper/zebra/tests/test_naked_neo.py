"""The NEO entry broker (`zebra/naked_neo.py`, naked live build P3b).

Driven against `kotak_neo.fake.FakeNeo`, so the REAL kotak_neo client code --
envelopes, error classes, tags, resolve-by-tag -- is what gets exercised. No
network, no session file, no sleeping.

The properties that protect money:
  * the tag reaches `on_tag` BEFORE the order exists, and a failed tag write
    sends nothing;
  * the outcome is read from the BOOK, never from the reply;
  * a resting order is cancelled and only a terminal book decides;
  * anything the book cannot settle is `unclear`, never `filled`/`unfilled`;
  * refusals that happen before the send are `rejected`, retry False.

Run:  cd Helper && python -m pytest zebra/tests/test_naked_neo.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))
sys.path.insert(0, str(HELPER.parent))

from kotak_neo import client as C           # noqa: E402
from kotak_neo import session as S          # noqa: E402
from kotak_neo import symbols as Y          # noqa: E402
from kotak_neo.fake import FakeNeo          # noqa: E402
from zebra import naked_neo as nn           # noqa: E402

SYM, TOK, LOT = 'RELIANCE26DEC1540CE', '144284', 500
SESS = S.Session(3, '2026-12-02', 'c', 'https://cis.kotaksecurities.com', 'T', 'SID', None,
                 'CK', 'U')
PARENT = {'id': 7, 'stock': 'RELIANCE', 'expiry': '2026-12-29', 'long_strike': 1540.0,
          'direction': 'CE', 'lot_size': LOT}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 0.5)


class Refresh:
    def __call__(self, gen, who):
        return S.Result(S.STILL_ALIVE, SESS, 'scripted')


@pytest.fixture
def neo(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    f = FakeNeo()
    f.set_quote(SYM, 10.0, 10.2)
    return f


def smap(lot=LOT):
    return Y.ScripMap([Y.ScripRow(SYM, TOK, 'RELIANCE', date(2026, 12, 29), 1540.0, 'CE',
                                  lot, 0.05, 'OPTSTK')])


def broker(neo, lot=LOT, wait=10.0):
    c = C.NeoClient('zbr', http=neo, current_fn=lambda: SESS, refresh_fn=Refresh())
    clk = Clock()
    return nn.NeoBroker(client=c, scrip_map=smap(lot), wait_s=wait, sleep=clk.sleep, clock=clk)


def ctx(log=None, fail=False):
    seen = log if log is not None else []

    def on_tag(tag, neo_symbol):
        if fail:
            raise OSError('disk full')
        seen.append((tag, neo_symbol))
    return {'parent': PARENT, 'on_tag': on_tag, 'trade_id': 7}


def test_fill_at_the_ask_reads_the_book_and_carries_the_tag(neo):
    seen = []
    placed_at_tag_time = []
    c = ctx(seen)
    inner = c['on_tag']

    def on_tag(tag, s):
        placed_at_tag_time.append(len(neo.placed()))
        inner(tag, s)
    c['on_tag'] = on_tag
    r = broker(neo).buy('RELIANCE26DEC1540CE', LOT, 10.2, c)
    assert r.status == 'filled' and r.fill_price == 10.2
    assert r.tag and r.tag.startswith('zbr-') and r.neo_symbol == SYM and r.neo_token == TOK
    assert seen == [(r.tag, SYM)]
    assert placed_at_tag_time == [0]            # the witness existed before the order
    assert neo.net(SYM) == LOT


def test_tag_write_failure_sends_nothing(neo):
    r = broker(neo).buy(SYM, LOT, 10.2, ctx(fail=True))
    assert r.status == 'rejected' and not r.retry and 'tag not persisted' in r.message
    assert neo.placed() == []


def test_definite_refusal_is_rejected_and_not_retried(neo):
    neo.reject(1004, 'Insufficient balance.')
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and not r.retry


def test_rms_reject_after_an_order_number_is_read_from_the_book(neo):
    neo.rms_reject('RMS:Margin Exceeds')
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and not r.retry and r.order_id


def test_a_price_band_rms_reject_may_retry(neo):
    neo.rms_reject('Order price is outside the price band')
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and r.retry


def test_resting_order_is_cancelled_and_reads_unfilled(neo):
    r = broker(neo).buy(SYM, LOT, 9.5, ctx())          # below the ask: rests
    assert r.status == 'unfilled'
    assert neo.net(SYM) == 0


def test_unclear_reply_that_did_fill_is_a_fill(neo):
    neo.unclear(placed=True)
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'filled' and r.fill_price == 10.2


def test_unclear_reply_never_in_the_book_stays_unclear(neo):
    neo.unclear(placed=False)
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'unclear' and 'absent' in r.message and r.tag


def test_timeout_that_placed_resolves_from_the_book(neo):
    neo.timeout(placed=True)
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'filled'


def test_partial_fill_is_unclear_never_a_fill(neo):
    neo.partial(200)
    r = broker(neo, wait=1.0).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'unclear' and r.filled_qty == 200


def test_symbol_not_in_the_scrip_master_sends_nothing(neo):
    seen = []
    p = dict(PARENT, long_strike=9999.0)
    r = broker(neo).buy(SYM, LOT, 10.2, dict(ctx(seen), parent=p))
    assert r.status == 'rejected' and not r.retry and 'not sent' in r.message
    assert seen == [] and neo.placed() == []


def test_lot_size_mismatch_sends_nothing(neo):
    r = broker(neo, lot=250).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and 'lot size' in r.message and neo.placed() == []


def test_contract_owned_by_another_bot_sends_nothing(neo):
    C.assign(SYM, 'btst')
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and not r.retry and 'NotOwner' in r.message
    assert neo.placed() == []


@pytest.mark.parametrize('msg,retry', [
    ('Order price is outside the price band', True),
    ('LTP protection: limit price too far', True),
    ('RMS:Margin Exceeds for price 10.2', False),     # stop word wins over a price word
    ('Insufficient funds', False),
    ('session ip doesnt match', False),
    ('Static IP not registered', False),
    ('Scrip in ban period', False),
    ('Scrip in F&O ban: order price outside band', False),   # review R8: ban beats price
    ('something nobody has seen', False),             # unknown -> stop (design Q8)
    ('', False),
])
def test_reject_classification(msg, retry):
    assert nn.classify_reject(msg) is retry


def test_ip_is_matched_as_a_word_not_a_substring():
    assert nn.classify_reject('price outside band for SHIPPING') is True


# -- review fixes P7e (2026-11-28) -----------------------------------------------

def test_a_fill_carries_the_contract_tick_from_the_scrip_row(neo):
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'filled' and r.neo_tick == 0.05


def test_an_unfilled_order_gives_the_contract_claim_back(neo):
    r = broker(neo).buy(SYM, LOT, 9.5, ctx())          # rests, cancelled
    assert r.status == 'unfilled' and C.owner_of(SYM) is None


def test_a_book_rejected_order_gives_the_contract_claim_back(neo):
    neo.rms_reject('RMS:Margin Exceeds')
    r = broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert r.status == 'rejected' and C.owner_of(SYM) is None


def test_a_fill_keeps_the_claim(neo):
    broker(neo).buy(SYM, LOT, 10.2, ctx())
    assert C.owner_of(SYM) == 'zbr'
