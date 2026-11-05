"""A live engine wired to the FakeMarket (quotes) and the FakeBroker (orders),
driven by a settable clock. No network, no real state directories."""
import json
from datetime import date, datetime

from conftest import APP, LOT, T, make_options
from nifty_btst.broker import FakeBroker
from nifty_btst.journal import Journal
from nifty_btst.live_config import build_neo
from nifty_btst.live_engine import LiveEngine, LiveStore
from nifty_btst.neo_symbols import ScripMap, ScripRow
from nifty_btst.notify import NullNotifier

MON, TUE, WED = '2026-10-26', '2026-10-27', '2026-10-28'


def neo_raw(**over):
    """The tracked `neo` block, as the live loader reads it (paper's Config
    does not carry it any more)."""
    raw = dict(json.load(open(APP / 'btst_config.defaults.json'))['neo'])
    raw.update(over)
    return raw


def neo_symbol(kite_symbol: str) -> str:
    # Deliberately NOT the Kite spelling: the map must go by facts, not strings.
    return 'NEO-' + kite_symbol[::-1]


def make_scrips(lot=LOT) -> ScripMap:
    rows = []
    for o in make_options():
        rows.append(ScripRow(trading_symbol=neo_symbol(o['tradingsymbol']),
                             token=str(100000 + o['instrument_token']), underlying='NIFTY',
                             expiry=date.fromisoformat(o['expiry']), strike=o['strike'],
                             option_type=o['instrument_type'], lot_size=lot, tick_size=0.05))
    return ScripMap(rows)


SCRIPS = make_scrips()


def armed_reader(until=date(2099, 1, 1)):
    return lambda today: until


class Rig:
    def __init__(self, cfg, tmp_path, market, fill_delay=0.0, mode='live', armed=True,
                 neo_overrides=None, scrips=None, session_expected=True, persist=False):
        self.cfg = cfg
        self.neo = build_neo(neo_raw(**(neo_overrides or {})))
        self.clock = {'now': T(MON, '09:00:00')}
        self.market = market
        self.dir = tmp_path / 'state_live'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.persist = persist
        self.broker = FakeBroker(lambda: self.clock['now'], fill_delay=fill_delay,
                                 path=(self.dir / 'fake_broker.json') if persist else None)
        self.store = LiveStore(self.dir)
        self.journal = Journal(self.dir)
        self.notifier = NullNotifier()
        self.armed = armed
        self.engine = LiveEngine(cfg, self.neo, self.store, market, self.notifier, self.broker,
                                 self.journal, lambda: scrips or SCRIPS,
                                 session_expected=session_expected,
                                 arm_reader=armed_reader() if armed else (lambda d: None),
                                 mode=mode)

    def restart(self, armed=True, scrips=None, fresh_broker=False):
        """A fresh process over the same state dir. The broker is the same
        object (the exchange does not restart) unless `fresh_broker`, which
        models rehearsal's new FakeBroker loaded from disk."""
        if fresh_broker:
            self.broker = FakeBroker(lambda: self.clock['now'], fill_delay=self.broker.fill_delay,
                                     path=(self.dir / 'fake_broker.json') if self.persist else None)
        self.store = LiveStore(self.dir)
        self.journal = Journal(self.dir)
        self.notifier = NullNotifier()
        self.engine = LiveEngine(self.cfg, self.neo, self.store, self.market, self.notifier,
                                 self.broker, self.journal, lambda: scrips or SCRIPS,
                                 arm_reader=armed_reader() if armed else (lambda d: None),
                                 mode='live')
        return self.engine

    def start(self, when: datetime):
        self.clock['now'] = when
        self.market.today = when.date()
        self.engine.start(when)

    def service(self, when: datetime):
        self.clock['now'] = when
        self.market.today = when.date()
        self.engine.service_orders(when)

    def go(self, when: datetime):
        """One CLI tick: NEO service, then the quote step (as the loop does)."""
        self.clock['now'] = when
        self.market.today = when.date()
        self.engine.service_orders(when)
        self.engine.step(when)

    @property
    def sent(self):
        return self.notifier.sent

    def alerts(self, needle: str):
        return [s for s in self.notifier.sent if needle in s]
