"""`python -m kotak_neo probe` -- REAL orders, 1 lot, through the package's own code.

The end-to-end proof that client / orders / symbols work against the live
exchange (BTST's probe proved NEO; this proves THIS package). Steps:

  1. BUY 1 lot LIMIT at the ask                   -> resolve(tag): filled?
  2. CANCEL the filled buy                        -> the reply to cancelling a
                                                     FILLED order (unmeasured)
  3. SL sell, trigger far below the bid           -> `trigger pending`?
  4. cancel_and_confirm the SL                    -> cancelled, filled 0
  5. chase_exit to flat                           -> sold, account flat
  6. positions + funds before/after               -> what it cost

Refuses unless: today's date is in ~/.kotak_neo/PROBE_ARM (never arms a bot),
--confirm-real-order, market hours (09:20-15:10 IST), 1 lot, ask x lot under
the ceiling, the account already flat in the contract, and the lot size the
same on NEO and Kite. Whatever happens, it ends by reading what is HELD.
The bot tag is `prb`.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import orders as O
from . import session as S
from . import symbols as Y
from .client import NeoClient, OrderRequest, Rejected, Unavailable, Unclear

logger = logging.getLogger(__name__)

CEILING_RS = 2000.0          # code constant; the CLI may only LOWER it
WINDOW = (dtime(9, 20), dtime(15, 10))


class ProbeRefused(RuntimeError):
    pass


def kite_quote_fn(kite_symbol: str, token_file: Path) -> Callable[[], Optional[Dict[str, float]]]:
    """Bid/ask from Kite (the bots' quote source), read-only."""
    from kiteconnect import KiteConnect
    with open(str(token_file), encoding='utf-8') as f:
        t = json.load(f)
    k = KiteConnect(api_key=t['api_key'])
    k.set_access_token(t['access_token'])
    key = 'NFO:' + kite_symbol

    def q() -> Optional[Dict[str, float]]:
        try:
            d = k.quote([key])[key]['depth']
            return {'bid': float(d['buy'][0]['price']), 'ask': float(d['sell'][0]['price'])}
        except Exception as e:                          # noqa: BLE001
            logger.warning('kite quote %s: %s', key, e)
            return None
    return q


def armed(today: str) -> bool:
    try:
        return (S.HOME / 'PROBE_ARM').read_text(encoding='utf-8').strip() == today
    except OSError:
        return False


def run(c: NeoClient, row: Y.ScripRow, kite_lot: int, quote: Callable, *,
        ceiling: float = CEILING_RS, confirm: bool = False, now: Optional[datetime] = None,
        sleep: Callable = time.sleep, clock: Callable = time.time,
        out: Callable[[str], None] = print) -> int:
    now = now or S._now()
    if not confirm:
        raise ProbeRefused('pass --confirm-real-order (this places REAL orders)')
    if not armed(now.strftime('%Y-%m-%d')):
        raise ProbeRefused('write today\'s date to %s' % (S.HOME / 'PROBE_ARM'))
    if not (WINDOW[0] <= now.time() <= WINDOW[1]):
        raise ProbeRefused('outside %s-%s IST' % WINDOW)
    if ceiling > CEILING_RS:
        raise ProbeRefused('ceiling above the code limit Rs %.0f' % CEILING_RS)
    if row.lot_size != kite_lot:
        raise ProbeRefused('lot size NEO %d != Kite %d' % (row.lot_size, kite_lot))
    q = quote() or {}
    ask, bid = float(q.get('ask') or 0), float(q.get('bid') or 0)
    if ask <= 0 or bid <= 0:
        raise ProbeRefused('no two-way quote')
    if ask * row.lot_size > ceiling:
        raise ProbeRefused('premium Rs %.0f over the ceiling Rs %.0f' % (ask * row.lot_size, ceiling))
    if c.net_held(row.trading_symbol) != 0:
        raise ProbeRefused('the account already holds %s' % row.trading_symbol)
    funds0 = c.funds()
    out('PROBE %s token %s lot %d tick %.2f | bid %.2f ask %.2f | funds %.2f'
        % (row.trading_symbol, row.token, row.lot_size, row.tick_size, bid, ask, funds0))
    lot = row.lot_size
    st: Optional[O.ChaseState] = None
    try:
        # 1. buy
        tag = c.new_tag()
        t0 = clock()
        try:
            no = c.place(OrderRequest(row.trading_symbol, row.token, 'B', lot, '%.2f' % ask, tag))
        except Rejected as e:
            out('1) BUY rejected: %s' % e)
            return 1
        except Unclear as e:
            out('1) BUY unclear: %s -- resolving by tag' % e)
            no = ''
        r = O.resolve(c, tag, wait_s=20, poll_s=0.5, expect_qty=lot, sleep=sleep, clock=clock)
        out('1) BUY %s @ %.2f -> %s filled %d avg %.2f (%.1fs) %s'
            % (no or '?', ask, r.state, r.filled, r.avg_price, clock() - t0, r.reason))
        if not r.terminal:
            r = O.cancel_and_confirm(c, r.order_no or no, tag, wait_s=10, expect_qty=lot,
                                     sleep=sleep, clock=clock)
            out('   not filled in 20s -> cancel -> %s filled %d' % (r.state, r.filled))
        held = c.net_held(row.trading_symbol)
        if held <= 0:
            out('   nothing held; stopping')
            return 1
        # 2. cancel a FILLED order: the reply shape is the measurement
        try:
            c.cancel(r.order_no)
            out('2) CANCEL of the filled buy: accepted (!) -- book says %s'
                % O.resolve(c, tag, wait_s=0).state)
        except Rejected as e:
            out('2) CANCEL of the filled buy: rejected stCode %s: %s' % (e.code, e.msg))
        except Unclear as e:
            out('2) CANCEL of the filled buy: unclear: %s' % e)
        # 3. SL sell far below
        q = quote() or {}
        b = float(q.get('bid') or bid)
        trig = max(O._round_down(b * 0.5, row.tick_size), 2 * row.tick_size)
        lim = max(O._round_down(trig - 2 * row.tick_size, row.tick_size), row.tick_size)
        sl_tag = c.new_tag()
        try:
            sl_no = c.place(OrderRequest(row.trading_symbol, row.token, 'S', held, '%.2f' % lim,
                                         sl_tag, 'SL', '%.2f' % trig), reduce_only=True)
            r3 = O.resolve(c, sl_tag, wait_s=3, until_terminal=False, expect_qty=held,
                           sleep=sleep, clock=clock)
            out('3) SL sell trigger %.2f limit %.2f -> %s (%s)' % (trig, lim, r3.state, r3.reason))
            r4 = O.cancel_and_confirm(c, sl_no, sl_tag, wait_s=10, expect_qty=held,
                                      sleep=sleep, clock=clock)
            out('4) cancel SL -> %s filled %d' % (r4.state, r4.filled))
        except (Rejected, Unclear) as e:
            out('3) SL sell: %s' % e)
        # 5. chase to flat
        st = O.ChaseState(bot=c.bot, symbol=row.trading_symbol, token=row.token,
                          tick=row.tick_size, remaining=c.net_held(row.trading_symbol))
        pol = O.ChasePolicy(reprice_after_s=10, confirm_wait_s=0)
        deadline = clock() + 180
        while st.status == 'active' and clock() < deadline:
            O.chase_exit(c, st, quote, pol, sleep=sleep, clock=clock)
            if st.status == 'active':
                sleep(2)
        out('5) chase -> %s sold %d avg %.2f reprices %d %s'
            % (st.status, st.sold, st.avg_exit, st.reprices, st.note))
        return 0 if st.status == 'done' else 1
    finally:
        try:
            held = c.net_held(row.trading_symbol)
            live = c.live_orders(row.trading_symbol)
            funds1 = c.funds()
            out('END: held %d, live orders %d, funds %.2f (change %.2f) -> %s'
                % (held, len(live), funds1, funds1 - funds0,
                   'FLAT' if held == 0 and not live else 'CHECK THE APP'))
            for f in c.trades():
                if f.symbol == row.trading_symbol and f.bot() == c.bot:
                    out('   fill %s %d @ %.2f %s' % (f.side, f.qty, f.price, f.filled_at))
        except Unavailable as e:
            out('END: could not read the account (%s) -- CHECK THE APP' % e)
