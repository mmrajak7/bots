"""Everything that talks to Kite. Read-only: this module places no orders.

The engine depends on the four methods of `Market`; `KiteMarket` implements
them on the shared Kite token, tests implement them on a script.

Rate limits: Kite's quote endpoint allows ~1 req/s and historical ~3 req/s
per API key, and the key is SHARED with zebra and the BCS monitor on the Pi.
One poll here is one `quote()` call; a 429 or network error is retried with
backoff, and a run of failures is surfaced by the engine, not swallowed.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .pricing import Book, book_from_quote
from .sessions import IST

logger = logging.getLogger(__name__)


class TokenError(RuntimeError):
    pass


class QuoteError(RuntimeError):
    pass


def load_token(path: Path, today: date) -> Dict[str, Any]:
    """The shared token, refused unless it was generated today (IST).

    It is generated at 08:45 IST by the scheduled auth job and expires at
    06:00 the next day, so "generated today" is the whole validity test.
    """
    if not path.exists():
        raise TokenError('token file not found: %s (run the daily Kite auth)' % path)
    with open(path, encoding='utf-8') as f:
        tok = json.load(f)
    for k in ('access_token', 'api_key', 'generated_at'):
        if not tok.get(k):
            raise TokenError('token file has no %s' % k)
    gen = str(tok['generated_at'])[:10]
    if gen != today.isoformat():
        raise TokenError('token generated %s, not today %s (stale; re-run '
                         'the daily Kite auth)' % (gen, today))
    return tok


class Market:
    """Interface the engine uses. Keys of `quote()` are 'EXCH:SYMBOL'."""

    def options(self) -> List[dict]:
        raise NotImplementedError

    def quote(self, keys: List[str]) -> Dict[str, Book]:
        raise NotImplementedError

    def minute_candles(self, token: int, start: datetime,
                       end: datetime) -> List[dict]:
        raise NotImplementedError

    def day_close(self, token: int, d: date) -> Optional[float]:
        raise NotImplementedError


class KiteMarket(Market):
    def __init__(self, kite: Any, underlying: str, exchange: str,
                 cache_dir: Path, today: date, retries: int = 3):
        self.kite = kite
        self.underlying = underlying
        self.exchange = exchange
        self.cache_dir = Path(cache_dir)
        self.today = today
        self.retries = retries
        self._options: Optional[List[dict]] = None
        self._last_hist = 0.0

    @classmethod
    def from_token(cls, tok: Dict[str, Any], **kw: Any) -> 'KiteMarket':
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=tok['api_key'])
        kite.set_access_token(tok['access_token'])
        return cls(kite, **kw)

    # -- retry wrapper -------------------------------------------------
    def _call(self, what: str, fn, *a, **kw):
        from kiteconnect import exceptions as kex
        delay = 1.0
        for attempt in range(1, self.retries + 1):
            try:
                return fn(*a, **kw)
            except kex.TokenException as exc:
                raise TokenError('%s: token rejected by Kite (%s)' % (what, exc))
            except (kex.NetworkException, kex.DataException,
                    kex.GeneralException, OSError) as exc:
                if attempt == self.retries:
                    raise QuoteError('%s failed after %d tries: %s'
                                     % (what, attempt, exc))
                logger.warning('%s attempt %d failed (%s); retrying in %.0fs',
                               what, attempt, exc, delay)
                time.sleep(delay)
                delay *= 2

    # -- instruments ---------------------------------------------------
    def options(self) -> List[dict]:
        if self._options is not None:
            return self._options
        cache = self.cache_dir / ('instruments_%s_%s.json'
                                  % (self.underlying, self.today.strftime('%Y%m%d')))
        if cache.exists():
            with open(cache, encoding='utf-8') as f:
                self._options = json.load(f)
            return self._options
        rows = self._call('instruments', self.kite.instruments, self.exchange)
        out = []
        for r in rows:
            if r.get('name') != self.underlying or r.get('segment') != '%s-OPT' % self.exchange:
                continue
            exp = r['expiry']
            out.append({
                'tradingsymbol': r['tradingsymbol'],
                'instrument_token': int(r['instrument_token']),
                'strike': float(r['strike']),
                'expiry': exp.isoformat() if hasattr(exp, 'isoformat') else str(exp)[:10],
                'instrument_type': r['instrument_type'],
                'lot_size': int(r['lot_size']),
            })
        if not out:
            raise QuoteError('instrument dump has no %s options' % self.underlying)
        tmp = cache.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(out, f)
        tmp.replace(cache)
        for old in self.cache_dir.glob('instruments_%s_*.json' % self.underlying):
            if old != cache:
                old.unlink()
        self._options = out
        return out

    # -- quotes --------------------------------------------------------
    def quote(self, keys: List[str]) -> Dict[str, Book]:
        out: Dict[str, Book] = {}
        keys = list(dict.fromkeys(keys))
        for i in range(0, len(keys), 450):   # Kite caps quote() at 500 keys
            chunk = keys[i:i + 450]
            raw = self._call('quote', self.kite.quote, chunk) or {}
            for k in chunk:
                b = book_from_quote(raw.get(k))
                if b is not None:
                    out[k] = b
        return out

    # -- historical ----------------------------------------------------
    def _hist(self, token: int, start: datetime, end: datetime, interval: str):
        gap = time.time() - self._last_hist
        if gap < 0.35:                       # stay under 3 req/s
            time.sleep(0.35 - gap)
        try:
            return self._call('historical', self.kite.historical_data, token,
                              start.replace(tzinfo=None), end.replace(tzinfo=None),
                              interval) or []
        finally:
            self._last_hist = time.time()

    def minute_candles(self, token: int, start: datetime,
                       end: datetime) -> List[dict]:
        rows = self._hist(token, start, end, 'minute')
        out = []
        for r in rows:
            ts = r['date']
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=IST)
            out.append({'t': ts.astimezone(IST), 'open': float(r['open']),
                        'high': float(r['high']), 'low': float(r['low']),
                        'close': float(r['close'])})
        return out

    def day_close(self, token: int, d: date) -> Optional[float]:
        start = datetime(d.year, d.month, d.day, tzinfo=IST)
        rows = self._hist(token, start, start + timedelta(hours=23), 'day')
        for r in rows:
            if r['date'].date() == d:
                return float(r['close'])
        return None
