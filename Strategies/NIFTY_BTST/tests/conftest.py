import json
from dataclasses import replace
import sys
from datetime import date, datetime, time as dtime
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from nifty_btst import config as config_mod  # noqa: E402
from nifty_btst.kite_io import Market, QuoteError  # noqa: E402
from nifty_btst.notify import NullNotifier  # noqa: E402
from nifty_btst.pricing import Book  # noqa: E402
from nifty_btst.sessions import IST  # noqa: E402
from nifty_btst.store import Store  # noqa: E402

EXPIRIES = ['2026-10-27', '2026-11-03', '2026-11-10']
LOT = 65


def T(d: str, hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(':'))
    return datetime.combine(date.fromisoformat(d), dtime(h, m, s)).replace(tzinfo=IST)


def sym(exp: str, strike: int, side: str) -> str:
    return 'NIFTY%s%d%s' % (exp.replace('-', ''), strike, side)


def make_options():
    out, tok = [], 1000
    for e in EXPIRIES:
        for k in range(22000, 23550, 50):
            for side in ('CE', 'PE'):
                tok += 1
                out.append({'tradingsymbol': sym(e, k, side), 'instrument_token': tok,
                            'strike': float(k), 'expiry': e, 'instrument_type': side,
                            'lot_size': LOT})
    return out


class FakeMarket(Market):
    """Quotes come from `self.books` (symbol -> Book), set by the test."""

    def __init__(self):
        self.opts = make_options()
        self.books = {}
        self.spot = 22800.0
        self.fail = False
        self.candles = {}
        self.closes = {}
        self.quote_calls = 0
        #: the simulated trading day: quotes say their last trade was on it,
        #: except symbols in `stale`, which last traded the session before.
        self.today = None
        self.stale = set()

    def options(self):
        return self.opts

    def set(self, symbol, ltp, bid=None, ask=None, qty=1000):
        bid = ltp - 0.1 if bid is None else bid
        ask = ltp + 0.1 if ask is None else ask
        self.books[symbol] = Book(ltp, bid, ask, qty, qty, '')

    def quote(self, keys):
        self.quote_calls += 1
        if self.fail:
            raise QuoteError('simulated outage')
        out = {}
        for k in keys:
            if k == 'NSE:NIFTY 50':
                out[k] = Book(self.spot, 0, 0, 0, 0, '')
                continue
            s = k.split(':', 1)[1]
            if s in self.books:
                day = self.today if s not in self.stale else date(2000, 1, 1)
                out[k] = replace(self.books[s], ltt='%sT10:00:00' % day)
        return out

    def minute_candles(self, token, start, end):
        return [c for c in self.candles.get(token, []) if start <= c['t'] <= end]

    def day_close(self, token, d):
        return self.closes.get(d)


def go(engine, when):
    """One poll at `when`, with the fake market trading on that day."""
    engine.market.today = when.date()
    engine.step(when)


def default_premiums(m: FakeMarket, exp: str):
    """A smooth chain: CE 22850 / PE 22700 sit nearest Rs 100."""
    for k in range(22000, 23550, 50):
        m.set(sym(exp, k, 'CE'), max(1.0, 102 + (22850 - k) * 0.9))
        m.set(sym(exp, k, 'PE'), max(1.0, 98 + (k - 22700) * 0.9))


@pytest.fixture
def cfg():
    raw = json.load(open(APP / 'btst_config.defaults.json'))
    raw['telegram_enabled'] = False
    return config_mod.build(raw)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def market():
    return FakeMarket()


@pytest.fixture
def notifier():
    return NullNotifier()
