"""The executor's ONLY door to orders (spec §1, §10, §13 F1-F11).

`Broker` wraps the shared `BOTS/kotak_neo` package:
  live  -> NeoClient('etf', exclusive=False, timeout=3) on the published session;
  paper -> the SAME NeoClient code over `kotak_neo.fake.FakeNeo`, fed with the live
           Kite quotes (`on_quote`), so paper exercises the real client, envelopes,
           tags, resolve and chase (design §8 item 7, "SimBroker on the same path").

Every method either returns a value or raises `kotak_neo.client.NeoError` (or a
subclass); the engine treats ANY NeoError as "unreadable this step" (F9).
"""
from __future__ import annotations

import datetime as _dt
import importlib
import logging
import sys
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger('executor.broker')


def import_kotak_neo(bots_root: str):
    """Path-import the ONE copy of kotak_neo (spec §1)."""
    if bots_root not in sys.path:
        sys.path.insert(0, bots_root)
    C = importlib.import_module('kotak_neo.client')
    O = importlib.import_module('kotak_neo.orders')
    S = importlib.import_module('kotak_neo.session')
    Y = importlib.import_module('kotak_neo.symbols')
    return C, O, S, Y


class Broker:
    def __init__(self, cfg: Dict[str, Any], paper: bool, client: Any = None, fake: Any = None,
                 modules: Any = None):
        self.cfg = cfg
        self.paper = paper
        self.C, self.O, self.S, self.Y = modules or import_kotak_neo(cfg['bots_root'])
        self.fake = fake
        if client is not None:
            self.c = client
        elif paper:
            F = importlib.import_module('kotak_neo.fake')
            self.fake = self.fake or F.FakeNeo()
            self.fake.funds = 10_000_000.0
            sess = self.S.Session(0, '', '', 'https://paper.kotaksecurities.com', 'PAPER', 'PAPER',
                                  None, 'PAPER', 'PAPER')
            self.c = self.C.NeoClient(cfg['neo_bot'], exclusive=False, http=self.fake,
                                      product=cfg['product'],
                                      timeout=cfg['neo_timeout_s'], current_fn=lambda: sess,
                                      refresh_fn=lambda gen, who, **kw: self.S.Result(
                                          self.S.STILL_ALIVE, sess, 'paper'))
        else:
            # K8: a dead session found by a read waits at most neo_refresh_wait_s
            # for another bot's refresh, then this step is "unreadable" (F9)
            self.c = self.C.NeoClient(cfg['neo_bot'], exclusive=False, product=cfg['product'],
                                      timeout=cfg['neo_timeout_s'],
                                      refresh_wait_s=cfg['neo_refresh_wait_s'])
        if self.c.exclusive:
            raise ValueError('ETF must use a NON-exclusive client (spec O6)')
        self._map = None
        self._map_day: Optional[_dt.date] = None

    # -- symbols (F18) -----------------------------------------------------------
    def load_map(self, today: _dt.date) -> None:
        if self.paper:
            self._map_day = today
            return
        self._map = self.Y.load(self.c, today=today, underlyings=['NIFTY'])
        self._map_day = today

    def map_ready(self, today: _dt.date) -> bool:
        return self._map_day == today

    def map_intent(self, intent: Dict[str, Any]) -> Dict[str, Any]:
        """Kite option -> NEO {symbol, token, lot_size, tick}. Raises on any doubt."""
        if self.paper:
            return {'symbol': intent['kite_symbol'], 'token': str(intent.get('kite_token') or '0'),
                    'lot_size': int(intent['lot_size']), 'tick': float(intent['tick_size'])}
        if self._map is None:
            raise self.Y.ScripMasterError('scrip map not loaded')
        row = self._map.from_kite({'name': intent['underlying'], 'expiry': intent['expiry'],
                                   'strike': intent['strike'],
                                   'instrument_type': intent['opt_type'],
                                   'lot_size': intent['lot_size']})
        return {'symbol': row.trading_symbol, 'token': str(row.token),
                'lot_size': int(row.lot_size), 'tick': float(row.tick_size or intent['tick_size'])}

    # -- quotes into the paper exchange -------------------------------------------
    def on_quote(self, neo_symbol: str, bid: float, ask: float) -> None:
        if self.paper and self.fake is not None and bid > 0 and ask > 0:
            self.fake.set_quote(neo_symbol, bid, ask)

    # -- orders ----------------------------------------------------------------------
    def new_tag(self) -> str:
        return self.c.new_tag()

    def place(self, symbol: str, token: str, side: str, qty: int, price: float, tag: str,
              reduce_only: bool = False) -> str:
        req = self.C.OrderRequest(symbol, token, side, int(qty), '%.2f' % price, tag)
        return self.c.place(req, reduce_only=reduce_only)

    def resolve(self, tag: str, expect_qty: Optional[int] = None):
        return self.O.resolve(self.c, tag, wait_s=0, until_terminal=False, expect_qty=expect_qty)

    def cancel_confirm(self, order_no: str, tag: str, expect_qty: Optional[int] = None):
        return self.O.cancel_and_confirm(self.c, order_no, tag, wait_s=0, expect_qty=expect_qty)

    def own_net(self, symbol: str) -> int:
        """F5: order-book filled qty of own tagged orders, B - S (today)."""
        return self.c.own_order_net(symbol)

    def outside_sold(self, symbol: str) -> int:
        """Review B1: filled SELL shares in today's order book on `symbol` that
        are NOT etf- tagged (an app exit, the broker's square-off)."""
        return self.c.foreign_filled(symbol, 'S')

    def own_orders(self) -> List[Any]:
        return self.c.own_orders()

    def account_net(self, symbol: str) -> Optional[int]:
        """The account's net in `symbol` for ETF's OWN product row only (NEO
        reports positions per product): a BTST NRML lot never props up an ETF
        MIS cap and vice versa. Rows with no product are counted (unknown =
        could be ours). None = NO row for the symbol at all: UNKNOWN, never 0
        (a carried lot's row is unmeasured, Monday M10) -- the caller then
        trusts its own journal + order book."""
        prod = str(self.cfg['product']).strip().upper()
        known = getattr(self.C, 'KNOWN_PRODUCTS', ('NRML', 'MIS', 'CNC', 'MTF', 'CO', 'BO'))
        rows = []
        # Only THIS symbol's rows are parsed (BTST review X-K5): a malformed row
        # of another contract must not make ETF's own position unreadable.
        for p in self.c.positions_for(symbol):
            got = str(p.product or '').strip().upper()
            if not got or got == prod:
                rows.append(p)
            elif got not in known:
                # review R7 (2026-11-28): a label nobody documented may BE our MIS
                # row -- unreadable (fail closed: no foreign-check entry, no carry
                # sale, no adoption; a same-day exit falls back to its own net)
                raise self.C.Unavailable('positions: %s has product %r -- unknown label'
                                         % (symbol, p.product))
        if not rows:
            return None
        return sum(p.net_qty for p in rows)

    def funds(self) -> float:
        return self.c.funds()

    def chase_step(self, st: Any, quote: Callable, held_fn: Callable, price_fn: Callable,
                   persist: Callable, clock: Callable = None) -> Any:
        p = self.O.ChasePolicy(reprice_after_s=self.cfg['exit_step_s'], step_ticks=1,
                               max_reprices=None, confirm_wait_s=0.0,
                               unclear_grace_s=self.cfg['unclear_grace_sell_s'],
                               max_troubles=None,
                               reject_backoff_s=self.cfg['reject_backoff_s'])
        kw = {'clock': clock} if clock is not None else {}
        return self.O.chase_exit(self.c, st, quote, p, persist=persist, held_fn=held_fn,
                                 price_fn=price_fn, **kw)

    def chase_state(self, symbol: str, token: str, tick: float, qty: int) -> Any:
        return self.O.ChaseState(bot=self.c.bot, symbol=symbol, token=token, tick=tick,
                                 remaining=int(qty))

    def chase_from(self, d: Dict[str, Any]) -> Any:
        return self.O.ChaseState.from_dict(d)
