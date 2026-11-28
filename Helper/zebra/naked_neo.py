"""Kotak NEO execution for the live naked long (naked live build P3b / P3c / P4).

Design: `docs/NAKED_LIVE_DESIGN.md` (status box 2026-11-27: orders via the shared
`BOTS/kotak_neo` package; quotes, signals and the paper twin stay on Kite).

ONE place in zebra that talks to NEO. Three callers:

  NeoBroker.buy        the entry (`naked_entry`'s broker seam, P3b)
  adopt-side reads     `naked_adopt` resolves unclear entries by TAG (P3c)
  exit chase           `naked_exit` sells to flat via `kotak_neo.orders.chase_exit` (P4)

Everything NEO-specific -- the client, the scrip map, the tag, the reject
classification -- lives here so the rest of zebra names no broker (CLAUDE.md:
"the broker is a NUMBER, never a branch").

WHAT THE ENTRY GUARANTEES (each one is a test in `tests/test_naked_neo.py`):

  * The order's TAG is handed to `ctx['on_tag']` BEFORE the order is sent, so a
    crash between send and reply leaves a parent stamped `sending` with the tag
    -- which blocks every new entry and which the adoption sweep resolves.
  * A reply is never trusted. The outcome is read from the BOOK by tag
    (`orders.resolve`); RMS can reject an order after NEO gave it a number
    (measured 2026-11-27).
  * An order still resting after `naked_entry_wait_sec` is CANCELLED and the
    cancel is read back. Only a terminal book decides `filled` / `unfilled`;
    anything else is `unclear`, never a guess.
  * A refusal raised BEFORE anything was sent (no session, symbol not in the
    scrip master, lot mismatch, contract owned by another bot) is `rejected`
    with retry=False -- nothing to adopt, and nothing that a second identical
    order would fix.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from . import config as cfg

logger = logging.getLogger(__name__)

#: zebra's bot prefix on every NEO order (`kotak_neo`: 2-6 lowercase letters).
BOT = 'zbr'

#: A refusal containing any of these is NOT retried (design Q8): the account,
#: the risk system or the connection refused it, and a re-priced order would be
#: refused the same way. Checked BEFORE the price words, because an RMS text
#: can also mention a price.
STOP_WORDS = ('margin', 'fund', 'insufficient', 'balance', 'rms', 'permission',
              'not allowed', 'not permitted', 'blocked', 'ip', 'session', 'exposure',
              'limit exceeded', 'ban', 'surveillance')
#: A refusal containing one of these (and no stop word) is a stale or
#: out-of-band PRICE: one re-quoted retry is allowed.
PRICE_WORDS = ('price', 'band', 'circuit', 'dpr', 'ltp', 'tick', 'range', 'freeze')


def _ensure_path() -> None:
    """`kotak_neo` lives at the BOTS root, one level above Helper."""
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


def neo():
    """The kotak_neo modules, imported lazily (paper zebra never needs them)."""
    _ensure_path()
    from kotak_neo import client, orders, symbols    # noqa: F401
    return client, orders, symbols


def classify_reject(message: str) -> bool:
    """True = a price/band/stale reject (retry once); False = stop.

    Whole-word matching: 'ban' must not match 'band', nor 'ip' match 'SHIPPING'.
    Stop words are checked first; anything unrecognised stops (design Q8)."""
    m = str(message or '').lower()

    def has(w):
        # A word STARTS at a boundary ('funds' matches 'fund'); a word of three
        # letters or fewer must also END at one ('ban' is not 'band').
        tail = r'\b' if len(w) <= 3 else ''
        return re.search(r'\b' + re.escape(w) + tail, m) is not None
    if any(has(w) for w in STOP_WORDS):
        return False
    return any(has(w) for w in PRICE_WORDS)


def default_client():
    """The shared-session client for zebra's orders (exclusive: one bot per contract).

    `refresh_wait_s` (review K8/A7, 2026-11-28): a read that finds the
    session dead waits at most `naked_neo_refresh_wait_sec` for the refresh,
    not the session lock's 75 s -- the exit engine runs inline in a 5-second
    loop, and a stall there blinds every position and ages the heartbeat the
    live entry gate needs. The read then fails as unreadable; the next poll
    tries again."""
    client, _, _ = neo()
    return client.NeoClient(BOT, refresh_wait_s=float(cfg.NAKED_NEO_REFRESH_WAIT_SEC))


def scrip_row(c, parent: dict, scrip_map=None):
    """The NEO row for the parent's LONG leg, lot size cross-checked.

    Built from the record's own fields (underlying, expiry, strike, side) --
    never from the Kite trading symbol, whose format NEO does not share.
    Raises `symbols.ScripMasterError` / ValueError when it cannot be found
    exactly once; the caller treats that as "nothing sent".
    """
    _, _, symbols = neo()
    smap = scrip_map if scrip_map is not None else symbols.load(
        c, underlyings=[str(parent['stock']).upper()])
    row = smap.find(str(parent['stock']), date.fromisoformat(str(parent['expiry'])[:10]),
                    float(parent['long_strike']), str(parent['direction']))
    lot = int(parent.get('lot_size') or 0)
    if lot and row.lot_size != lot:
        raise ValueError('lot size NEO %d != record %d for %s'
                         % (row.lot_size, lot, row.trading_symbol))
    return row


class NeoBroker:
    """`buy(symbol, qty, limit, ctx) -> BuyResult` on Kotak NEO.

    `ctx` (from `naked_entry._attempt`) must carry `parent` (the paper parent
    record) and may carry `on_tag(tag, neo_symbol)`, called BEFORE the send.
    Clock and sleep are injectable so tests never wait.
    """
    name = 'neo'

    def __init__(self, client=None, scrip_map=None, wait_s: Optional[float] = None,
                 sleep: Optional[Callable] = None, clock: Optional[Callable] = None):
        self._client = client
        self._scrip_map = scrip_map
        self.wait_s = cfg.NAKED_ENTRY_WAIT_SEC if wait_s is None else float(wait_s)
        import time
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time

    @property
    def client(self):
        if self._client is None:
            self._client = default_client()
        return self._client

    def buy(self, symbol: str, qty: int, limit: float, ctx: dict):
        from .naked_entry import BuyResult           # lazy: naked_entry imports us
        client_mod, orders, symbols = neo()
        parent = ctx.get('parent') or {}
        try:
            c = self.client
            row = scrip_row(c, parent, self._scrip_map)
        except Exception as e:
            return BuyResult('rejected', message='not sent: %s: %s' % (type(e).__name__, e),
                             retry=False)
        tag = c.new_tag()
        on_tag = ctx.get('on_tag')
        if on_tag is not None:
            # The witness goes to disk BEFORE the order can exist. If this
            # write fails we do not send: an order nobody recorded is the one
            # outcome this module exists to prevent.
            try:
                on_tag(tag, row.trading_symbol, neo_token=str(row.token),
                       neo_tick=float(getattr(row, 'tick_size', 0) or 0) or None)
            except Exception as e:
                return BuyResult('rejected', message='not sent: tag not persisted (%s)' % e,
                                 retry=False)
        tick = float(getattr(row, 'tick_size', 0) or 0)
        lim = float(limit)
        if tick > 0:
            lim = round(int(lim / tick + 1e-9) * tick, 2)    # on NEO's grid, never above
        px = '%.2f' % lim
        req = client_mod.OrderRequest(row.trading_symbol, row.token, 'B', int(qty), px, tag)
        unclear_reply = ''
        try:
            c.place(req)
        except client_mod.Rejected as e:
            _release(c, row.trading_symbol)
            return BuyResult('rejected', message=str(e), retry=classify_reject(str(e)),
                             tag=tag)
        except (client_mod.NotOwner, client_mod.Auth, client_mod.Unavailable,
                ValueError) as e:
            # Raised before the HTTP send (claim, session, validation).
            return BuyResult('rejected', message='not sent: %s: %s' % (type(e).__name__, e),
                             retry=False, tag=tag)
        except client_mod.Unclear as e:
            unclear_reply = str(e)
        res = orders.resolve(c, tag, wait_s=self.wait_s, expect_qty=int(qty),
                             sleep=self._sleep, clock=self._clock)
        if res.state in (orders.RESTING, orders.PARTIAL) and not res.terminal and res.order_no:
            res = orders.cancel_and_confirm(c, res.order_no, tag, wait_s=self.wait_s,
                                            expect_qty=int(qty), sleep=self._sleep,
                                            clock=self._clock)
        out = self._result(res, orders, tag, row, unclear_reply)
        if out.status in ('rejected', 'unfilled'):
            _release(c, row.trading_symbol)       # review A8/L2: hold nothing, claim nothing
        return out

    @staticmethod
    def _result(res, orders, tag, row, unclear_reply):
        from .naked_entry import BuyResult
        extra = dict(tag=tag, order_id=res.order_no or None, neo_symbol=row.trading_symbol,
                     neo_token=str(row.token),
                     neo_tick=float(getattr(row, 'tick_size', 0) or 0) or None)
        if res.state == orders.FILLED and res.avg_price > 0:
            return BuyResult('filled', fill_price=round(float(res.avg_price), 2),
                             message='filled', **extra)
        if res.state == orders.REJECTED:
            return BuyResult('rejected', message=res.reason, retry=classify_reject(res.reason),
                             **extra)
        if res.state == orders.CANCELLED and res.terminal and res.filled == 0:
            return BuyResult('unfilled', message='not filled at the limit; cancelled', **extra)
        # PARTIAL (terminal, part of the lot), ABSENT, UNKNOWN, a cancel still
        # pending: the book cannot say what we hold. Unclear -- the sweep decides.
        why = '%s: %s' % (res.state, res.reason)
        if unclear_reply:
            why = 'reply unclear (%s); book: %s' % (unclear_reply, why)
        return BuyResult('unclear', message=why, filled_qty=int(res.filled or 0), **extra)


def _release(c, symbol: str) -> None:
    """Give the contract claim back when the account holds nothing in it.
    Best effort: a stale claim only reserves the contract to zebra."""
    try:
        c.release_if_flat(symbol)
    except Exception as e:                          # noqa: BLE001
        logger.warning('release %s: %s', symbol, e)


def quote_bid_fn(kite, kite_symbol: str,
                 floor: Optional[float] = None) -> Callable[[], Optional[dict]]:
    """{bid, ask} of the long from Kite -- the bots' quote source. None on any failure.

    A `floor` of 0 (two sessions from expiry) takes ANY positive bid.

    The SALE after a fired exit takes a reliable bid, or -- because the book is
    often wide right after a stop collapse (review R9) -- an unreliable one at
    or above `floor` (by exit reason: `naked_exit.NakedExitEngine._floor`).
    Below the floor a wide book's bid is treated as garbage, and the chase
    waits (and alerts).
    """
    from . import strikes as strikes_mod

    def q():
        try:
            d = strikes_mod._quote_option(kite, kite_symbol)
        except Exception as e:                      # noqa: BLE001
            logger.warning('naked exit quote %s: %s', kite_symbol, e)
            return None
        if not d or d.get('error'):
            return None
        bid = float(d.get('bid') or 0)
        if not d.get('reliable') and not (floor is not None and bid >= floor and bid > 0):
            return None
        return {'bid': bid, 'ask': float(d.get('ask') or 0)}
    return q
