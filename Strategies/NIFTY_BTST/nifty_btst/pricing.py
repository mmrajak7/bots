"""Quote books, reliability, and transaction charges. Pure functions.

PRICING RULE (same as every engine in this repo): a paper fill happens at the
side actually traded against — BUY at the ASK, SELL at the BID — and only on a
book we would believe. No quote, a one-sided book, or a book wider than the
configured limit DEFERS the fill to the next poll; it is never estimated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class Book:
    ltp: float
    bid: float
    ask: float
    bid_qty: int
    ask_qty: int
    ts: str
    #: exchange time of the LAST TRADE ('' if none). The quote's own
    #: timestamp is no use as a liveness signal: Kite keeps refreshing it, and
    #: keeps a two-sided book, long after the close and on holidays.
    ltt: str = ''

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def book_from_quote(q: Optional[Dict[str, Any]]) -> Optional[Book]:
    """Kite `quote()` entry -> Book. None when the entry is missing."""
    if not q:
        return None
    depth = q.get('depth') or {}
    buy = (depth.get('buy') or [{}])[0] or {}
    sell = (depth.get('sell') or [{}])[0] or {}
    ts = q.get('timestamp') or ''
    ltt = q.get('last_trade_time') or ''
    return Book(
        ltp=float(q.get('last_price') or 0.0),
        bid=float(buy.get('price') or 0.0),
        ask=float(sell.get('price') or 0.0),
        bid_qty=int(buy.get('quantity') or 0),
        ask_qty=int(sell.get('quantity') or 0),
        ts=str(ts),
        ltt=str(ltt),
    )


def traded_on(book: Optional[Book], day: date) -> bool:
    """True when the option's last trade happened on `day`.

    The guard against booking yesterday's prices: on a holiday, or in the
    first seconds after the open, an option's book still shows the previous
    session's LTP and quotes, and they are not prices anyone can trade at.
    """
    return book is not None and book.ltt[:10] == day.isoformat()


def reliable(book: Optional[Book], max_abs: float,
             max_pct: float) -> Tuple[bool, str]:
    if book is None:
        return False, 'no quote'
    if book.bid <= 0 or book.ask <= 0:
        return False, 'one-sided book (bid %.2f / ask %.2f)' % (book.bid, book.ask)
    if book.ask < book.bid:
        return False, 'crossed book (bid %.2f > ask %.2f)' % (book.bid, book.ask)
    width = book.ask - book.bid
    if width > max_abs and width > book.mid * max_pct / 100.0:
        return False, 'wide book %.2f/%.2f' % (book.bid, book.ask)
    return True, ''


def charges(buy_value: float, sell_value: float,
            rates: Dict[str, float], orders: int = 2,
            include_brokerage: bool = True) -> Dict[str, float]:
    """Round-trip charges for one long option trade, in rupees.

    `orders` is the number of executed orders (2 for buy + sell; 1 when the
    contract expired and there was no sell order). GST applies to brokerage +
    exchange + SEBI only, as on a Zerodha contract note.
    """
    brokerage = rates['brokerage_per_order'] * orders if include_brokerage else 0.0
    stt = sell_value * rates['stt_sell_pct'] / 100.0
    exchange = (buy_value + sell_value) * rates['exchange_pct'] / 100.0
    sebi = (buy_value + sell_value) * rates['sebi_pct'] / 100.0
    stamp = buy_value * rates['stamp_buy_pct'] / 100.0
    gst = (brokerage + exchange + sebi) * rates['gst_pct'] / 100.0
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {k: round(v, 2) for k, v in dict(
        brokerage=brokerage, stt=stt, exchange=exchange, sebi=sebi,
        stamp=stamp, gst=gst, total=total).items()}
