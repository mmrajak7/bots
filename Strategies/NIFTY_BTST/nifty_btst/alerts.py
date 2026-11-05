"""Telegram from a worker thread, so an alert can never delay an order (R3-06).

`Notifier.send` posts synchronously with a 10 s timeout. On the live path
that is the only thread, and a blackholed Telegram would hold a due SELL
for the length of the alert. `AsyncNotifier` keeps `Notifier`'s contract
(`sent` is appended at once, the text is logged at once) but hands the HTTP
post to a daemon worker through a BOUNDED queue: when the queue is full the
oldest message is dropped and the drop is counted and logged, so a dead
Telegram costs messages, never time. Paper keeps the synchronous `Notifier`
untouched.

Review 4 (R4-01/R4-19): `flush` waits for the post IN FLIGHT as well as the
queue, bounded by its timeout, so the last alert before a process exit is
delivered; a failed post is retried a few times, so a one-shot alert (a fill,
a kill) does not vanish on a blip; a worker that has died is noticed in
`send`, which then posts synchronously with a short timeout.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, Optional

from .notify import Notifier

logger = logging.getLogger(__name__)

QUEUE_MAX = 200
POST_RETRIES = 3            # attempts per message, RETRY_WAIT_SEC apart
RETRY_WAIT_SEC = 1.0
SYNC_TIMEOUT_SEC = 3.0      # the dead-worker fallback: short, and after any order


class AsyncNotifier(Notifier):
    def __init__(self, config_path: Path, enabled: bool, prefix: str):
        super().__init__(config_path, enabled, prefix)
        self._q: Deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._inflight = False
        self.dropped = 0
        self.posted = 0
        self.failed = 0
        self.sync_posts = 0
        self._worker: Optional[threading.Thread] = None
        if self.enabled and self._cfg is not None:
            self._worker = threading.Thread(target=self._run, name='telegram', daemon=True)
            self._worker.start()

    def send(self, text: str) -> bool:
        for secret in self.redactions:
            if secret:
                text = text.replace(secret, '<redacted>')
        msg = '%s\n%s' % (self.prefix, text)
        self.sent.append(msg)
        logger.info('ALERT %s', text.replace('\n', ' | '))
        if not self.enabled or self._cfg is None:
            return False
        if self._worker is not None and not self._worker.is_alive():
            # the worker died (R4-19): say so once per message in the log and
            # post on the caller's thread with a SHORT timeout; the caller has
            # already sent its order (R3-06)
            self.sync_posts += 1
            logger.error('telegram worker is dead: posting synchronously (%d so far)',
                         self.sync_posts)
            return self._post(self._cfg, msg, timeout=SYNC_TIMEOUT_SEC, retries=1)
        with self._lock:
            if len(self._q) >= QUEUE_MAX:
                self._q.popleft()
                self.dropped += 1
                logger.error('telegram queue full: dropped a message (%d dropped so far)',
                             self.dropped)
            self._q.append(msg)
        self._wake.set()
        return True                    # queued; delivery is the worker's business

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._q) + (1 if self._inflight else 0)

    def _post(self, cfg: Dict[str, Any], msg: str, timeout: float, retries: int) -> bool:
        import requests                   # type: ignore[import-untyped]
        for attempt in range(1, retries + 1):
            try:
                r = requests.post(
                    'https://api.telegram.org/bot%s/sendMessage' % cfg['bot_token'],
                    data={'chat_id': cfg['chat_id'], 'text': msg[:4000]}, timeout=timeout)
                if r.status_code == 200:
                    self.posted += 1
                    return True
                logger.error('Telegram %s (attempt %d/%d): %s | message: %s', r.status_code,
                             attempt, retries, r.text[:300], msg[:300])
            except Exception as exc:                        # noqa: BLE001
                logger.error('Telegram send failed (attempt %d/%d): %s | message: %s',
                             attempt, retries, type(exc).__name__, msg[:300])
            if attempt < retries:
                time.sleep(RETRY_WAIT_SEC)
        self.failed += 1
        return False

    def _run(self) -> None:
        cfg = self._cfg
        if cfg is None:
            return
        while True:
            self._wake.wait()
            with self._lock:
                if not self._q:
                    self._wake.clear()
                    continue
                msg = self._q.popleft()
                self._inflight = True           # counted in `pending` until posted (R4-01)
            try:
                self._post(cfg, msg, timeout=10, retries=POST_RETRIES)
            finally:
                with self._lock:
                    self._inflight = False

    def flush(self, timeout: float = 5.0) -> None:
        """Wait (bounded) for the queue AND the post in flight to finish: used
        before every process exit, so the last alert is not lost (R4-01)."""
        t0 = time.monotonic()
        while self.pending and time.monotonic() - t0 < timeout:
            if self._worker is None or not self._worker.is_alive():
                return
            time.sleep(0.02)
