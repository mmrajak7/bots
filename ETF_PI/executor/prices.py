"""Prices for the executor (spec §8 P1-P3).

P1 Kite WebSocket (KiteTicker, FULL mode = 5-level depth) in its own thread, one
   connection; symbols subscribed on intent and while a position is managed.
P2 a symbol whose last tick is older than `price_stale_s` is refreshed by REST
   quote (every `rest_poll_s` at most) through option_buy's cross-process Kite
   limiter, so the executor never starves the cron bots on the same API key.
P3 tick tape: one JSONL line per received update for managed symbols.

The token is the shared `kite_access_token.json` (made by `python -m kite_login`); on an auth error or a
dead socket the ticker is rebuilt from a FRESH read of that file.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from .rules import Quote

log = logging.getLogger('executor.prices')


def _levels(side: Any) -> list:
    out = []
    for lv in side or []:
        try:
            px, q = float(lv.get('price') or 0), int(lv.get('quantity') or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if px > 0:
            out.append((px, q))
    return out


def quote_from_kite(d: Dict[str, Any], source: str, now: float) -> Quote:
    """A Kite tick (WS full mode) or a kite.quote() record -> Quote."""
    depth = d.get('depth') or {}
    bids, asks = _levels(depth.get('buy')), _levels(depth.get('sell'))
    exch = d.get('exchange_timestamp') or d.get('timestamp')
    exch_ts = None
    if exch is not None and hasattr(exch, 'timestamp'):
        try:
            exch_ts = exch.timestamp()
        except (OverflowError, OSError, ValueError):
            exch_ts = None
    return Quote(ltp=float(d.get('last_price') or 0), bid=bids[0][0] if bids else 0.0,
                 ask=asks[0][0] if asks else 0.0, bids=bids, asks=asks, ts=now,
                 exch_ts=exch_ts, source=source)


class PriceFeed:
    """Thread-safe last quote per Kite symbol. `rest_quote(kite_key)` returns a
    kite.quote()-style record or None; `ticker_factory(api_key, access_token)`
    returns a KiteTicker-like object. Both injectable for tests."""

    def __init__(self, cfg: Dict[str, Any], token_path: str, state_dir: str,
                 rest_quote: Callable[[str], Optional[Dict[str, Any]]],
                 ticker_factory: Optional[Callable] = None, clock: Callable = time.time,
                 async_rest: bool = False):
        self.cfg = cfg
        self.token_path = token_path
        self.state_dir = state_dir
        self._rest = rest_quote
        self._factory = ticker_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._q: Dict[str, Quote] = {}
        self._tok2sym: Dict[int, str] = {}
        self._sym2tok: Dict[str, int] = {}
        self._last_rest: Dict[str, float] = {}
        self._ticker = None
        self._ws_ok = False
        self._ws_err = ''
        self._ws_started = 0.0
        self._ws_down_since = 0.0
        self._async_rest = async_rest
        self._inflight: Dict[str, bool] = {}
        self.on_update: Optional[Callable[[str, Quote], None]] = None

    # -- subscriptions ------------------------------------------------------------
    def watch(self, kite_symbol: str, token: Optional[int]) -> None:
        with self._lock:
            if token:
                token = int(token)
                self._tok2sym[token] = kite_symbol
                self._sym2tok[kite_symbol] = token
            else:
                self._sym2tok.setdefault(kite_symbol, 0)
        t = self._ticker
        if t is not None and token and self._ws_ok:
            try:
                t.subscribe([token])
                t.set_mode(t.MODE_FULL, [token])
            except Exception as e:                      # the reconnect path re-subscribes
                log.warning('subscribe %s failed: %s', kite_symbol, e)

    def unwatch(self, kite_symbol: str) -> None:
        with self._lock:
            tok = self._sym2tok.pop(kite_symbol, 0)
            if tok:
                self._tok2sym.pop(tok, None)
        t = self._ticker
        if t is not None and tok and self._ws_ok:
            try:
                t.unsubscribe([tok])
            except Exception as e:
                log.warning('unsubscribe %s failed: %s', kite_symbol, e)

    def watched(self) -> list:
        with self._lock:
            return list(self._sym2tok)

    # -- the websocket ----------------------------------------------------------------
    def _read_token(self):
        with io.open(self.token_path, encoding='utf-8') as f:
            td = json.load(f)
        return td['api_key'], td['access_token']

    def start(self) -> None:
        """(Re)build the ticker from a FRESH token read. Never raises."""
        if self._factory is None:
            return
        self.stop()
        # review B3 (2026-11-28): every build attempt -- good or failed -- restarts the
        # back-off clock, so a feed down since the 08:00 boot (yesterday's token: 403)
        # is rebuilt once per `down_for_s`, not on every 0.5 s loop.
        self._ws_started = self._clock()
        self._ws_down_since = 0.0
        try:
            api_key, access = self._read_token()
            t = self._factory(api_key, access)
        except Exception as e:
            self._ws_err = 'ticker build failed: %s' % e
            log.error(self._ws_err)
            return
        feed = self

        def mine() -> bool:
            # B3: a REPLACED ticker reporting late must not flip the live one's health
            return feed._ticker is t

        def on_connect(ws, response):
            if not mine():
                return
            feed._ws_ok, feed._ws_err, feed._ws_down_since = True, '', 0.0
            toks = list(feed._tok2sym)
            if toks:
                ws.subscribe(toks)
                ws.set_mode(ws.MODE_FULL, toks)
            log.info('kite ws connected; %d tokens', len(toks))

        def on_ticks(ws, ticks):
            if not mine():
                return
            now = feed._clock()
            for tk in ticks or []:
                sym = feed._tok2sym.get(int(tk.get('instrument_token') or 0))
                if sym:
                    feed._store(sym, quote_from_kite(tk, 'ws', now))

        def on_close(ws, code, reason):
            if not mine():
                return
            feed._ws_ok = False
            feed._ws_down_since = feed._ws_down_since or feed._clock()
            feed._ws_err = 'closed %s %s' % (code, reason)
            log.warning('kite ws %s', feed._ws_err)

        def on_error(ws, code, reason):
            if not mine():
                return
            feed._ws_ok = False
            feed._ws_down_since = feed._ws_down_since or feed._clock()
            feed._ws_err = 'error %s %s' % (code, reason)
            log.warning('kite ws %s', feed._ws_err)

        t.on_connect, t.on_ticks, t.on_close, t.on_error = on_connect, on_ticks, on_close, on_error
        self._ticker = t
        try:
            t.connect(threaded=True)
        except Exception as e:
            self._ws_err = 'connect failed: %s' % e
            log.error(self._ws_err)

    def stop(self) -> None:
        t, self._ticker, self._ws_ok = self._ticker, None, False
        if t is not None:
            try:
                t.close()
            except Exception:
                pass

    def ws_healthy(self) -> bool:
        return self._ws_ok

    def ws_error(self) -> str:
        return self._ws_err

    def maybe_restart(self, down_for_s: float = 60.0) -> bool:
        """Rebuild a ticker that has been down `down_for_s` (fresh token read:
        kite_login may have re-logged in). True when a rebuild was attempted."""
        if self._factory is None or self._ws_ok:
            return False
        # review L2: time DOWN, not built; B3: and never less than since the last build
        since = max(self._ws_down_since, self._ws_started)
        if self._clock() - since < down_for_s:
            return False                                 # let KiteTicker's own reconnect work
        log.warning('kite ws down (%s) -- rebuilding from a fresh token read', self._ws_err)
        self.start()
        return True

    # -- quotes -------------------------------------------------------------------------
    def _store(self, sym: str, q: Quote) -> None:
        with self._lock:
            self._q[sym] = q
        self._tape(sym, q)
        cb = self.on_update
        if cb is not None:
            try:
                cb(sym, q)
            except Exception as e:
                log.error('on_update %s: %s', sym, e)

    def push(self, sym: str, q: Quote) -> None:
        """Tests / replay: inject a quote as if it had arrived."""
        self._store(sym, q)

    def get(self, sym: str) -> Optional[Quote]:
        with self._lock:
            return self._q.get(sym)

    def fresh(self, sym: str, exchange: str = 'NFO') -> Optional[Quote]:
        """The newest quote; a REST refresh first when the last is stale (P2)."""
        now = self._clock()
        q = self.get(sym)
        if q is not None and q.age(now) <= self.cfg['price_stale_s']:
            return q
        if now - self._last_rest.get(sym, 0.0) < self.cfg['rest_poll_s']:
            return q
        self._last_rest[sym] = now
        key = '%s:%s' % (exchange, sym)
        if self._async_rest:                             # review M1: never block the loop
            if not self._inflight.get(sym):
                self._inflight[sym] = True
                threading.Thread(target=self._rest_fetch, args=(sym, key), daemon=True).start()
            return q
        return self._rest_fetch(sym, key) or q

    def _rest_fetch(self, sym: str, key: str) -> Optional[Quote]:
        try:
            rec = self._rest(key)
        except Exception as e:
            log.warning('REST quote %s failed: %s', key, e)
            return None
        finally:
            self._inflight[sym] = False
        if rec:
            q2 = quote_from_kite(rec, 'rest', self._clock())
            if q2.ltp > 0 or q2.bid > 0:
                self._store(sym, q2)
                return q2
        return None

    def _tape(self, sym: str, q: Quote) -> None:
        try:
            day = time.strftime('%Y%m%d', time.localtime(q.ts or time.time()))
            p = os.path.join(self.state_dir, 'tape_%s.jsonl' % day)
            with io.open(p, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'t': round(q.ts, 3), 's': sym, 'l': q.ltp, 'b': q.bid,
                                    'a': q.ask, 'src': q.source}) + '\n')
        except OSError:
            pass
