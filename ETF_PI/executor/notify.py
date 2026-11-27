"""Telegram + dead-man pings for the executor (spec H1, H3, O8).

`Notifier.send(text, kind)`: kind 'I' info channel, 'E' error channel, through
ETF_PI's `common.sendMsgTelegram` (its tokens come from credentials.json). Never
raises: a dead Telegram must not stop an exit. Every message is also logged.
`throttled(key, every_s, ...)` sends at most once per `every_s` per key.
`DeadMan.ping()` -- healthchecks.io URL from credentials.json section
`healthchecks.url`; missing = one warning, then silent (owner sets it up).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger('executor.notify')


class Notifier:
    def __init__(self, sender: Optional[Callable[[str, str], Any]] = None,
                 clock: Callable[[], float] = time.time, prefix: str = 'ETF LIVE'):
        self._send = sender
        self._clock = clock
        self._last: Dict[str, float] = {}
        self.prefix = prefix
        self.sent = []                       # (kind, text) -- tests and status

    def _sender(self):
        if self._send is None:
            import common                    # ETF_PI/common.py (Telegram, creds via etf_creds)
            self._send = common.sendMsgTelegram
        return self._send

    def send(self, text: str, kind: str = 'I') -> None:
        msg = '<b>%s</b> %s' % (self.prefix, text)
        (log.error if kind == 'E' else log.info)('TELEGRAM[%s] %s', kind, text)
        self.sent.append((kind, text))
        del self.sent[:-200]
        try:
            self._sender()(msg, kind)
        except Exception as e:               # never let Telegram stop trading logic
            log.error('telegram failed: %s', e)

    def throttled(self, key: str, every_s: float, text: str, kind: str = 'E') -> bool:
        now = self._clock()
        if now - self._last.get(key, -1e18) < every_s:
            return False
        self._last[key] = now
        self.send(text, kind)
        return True


class DeadMan:
    """Pings a healthchecks.io check from a background thread (never blocks the loop)."""

    def __init__(self, url: Optional[str], every_s: float, clock: Callable[[], float] = time.time):
        self.url = url
        self.every_s = every_s
        self._clock = clock
        self._last = 0.0
        self._warned = False

    @staticmethod
    def url_from_creds() -> Optional[str]:
        try:
            import etf_creds
            return str(etf_creds.get('healthchecks')['url']) or None
        except Exception:
            return None

    def ping(self, status: str = '') -> None:
        if not self.url:
            if not self._warned:
                log.warning('dead-man: no healthchecks.url in credentials.json -- pings OFF')
                self._warned = True
            return
        now = self._clock()
        if now - self._last < self.every_s:
            return
        self._last = now
        url = self.url + ('/fail' if status == 'fail' else '')

        def go():
            try:
                import requests
                requests.get(url, timeout=5)
            except Exception as e:
                log.warning('dead-man ping failed: %s', e)
        threading.Thread(target=go, daemon=True).start()
