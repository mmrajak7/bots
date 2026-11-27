"""Deliver kotak_neo alerts to Telegram (review F8: a latch nobody hears is silent).

`session._alert` QUEUES to `alerts.jsonl` (under the refresh lock, no network).
`flush()` SENDS what is new, once each, and is called after every refresh /
login / measurement -- outside the lock -- and by `python -m kotak_neo alerts`.

  * at-least-once, never silently dropped: the delivered position is advanced
    only after Telegram answers 200; a failure leaves the rest for next time;
  * never blocks a trading loop for long: short timeout, a few per call, and a
    non-blocking lock so two processes never send the same alert twice;
  * the bot token is read from the shared `BOTS/data/telegram_config.json`
    (the file every bot uses) and never logged. Alert texts are already
    redacted by `session.redact`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import session as S

logger = logging.getLogger(__name__)

MAX_PER_FLUSH = 5
PREFIX = 'NEO SESSION'


def _tg_config() -> Optional[Dict[str, Any]]:
    p = Path(S.config()['credentials_file']).parent / 'telegram_config.json'
    try:
        with open(str(p), encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get('bot_token') and d.get('chat_id'):
            return d
    except (OSError, ValueError):
        pass
    return None


def _send_default(text: str) -> bool:
    import requests
    cfg = _tg_config()
    if cfg is None:
        logger.error('kotak_neo alerts: no usable telegram_config.json -- alert kept queued')
        return False
    try:
        r = requests.post('https://api.telegram.org/bot%s/sendMessage' % cfg['bot_token'],
                          json={'chat_id': cfg['chat_id'], 'text': text}, timeout=5)
    except requests.RequestException as e:
        logger.warning('kotak_neo alerts: telegram %s', type(e).__name__)
        return False
    if r.status_code != 200:
        logger.warning('kotak_neo alerts: telegram HTTP %s', r.status_code)
        return False
    return True


def _format(row: Dict[str, Any]) -> str:
    return '%s [%s] %s\n%s' % (PREFIX, row.get('kind', '?'), row.get('at', ''),
                              str(row.get('text', ''))[:900])


def flush(send: Optional[Callable[[str], bool]] = None) -> int:
    """Send undelivered alerts. Returns how many were delivered. Never raises."""
    send = send or _send_default
    try:
        with S.file_lock('alerts.lock', 0.0) as got:
            if not got:
                return 0                              # another process is flushing
            src = S._p('alerts.jsonl')
            if not src.exists():
                return 0
            pos_file = S._p('alerts.sent')
            try:
                pos = int(pos_file.read_text(encoding='utf-8').strip() or 0)
            except (OSError, ValueError):
                pos = 0
            with open(str(src), 'rb') as f:
                f.seek(0, 2)
                if pos > f.tell():                    # file was replaced: start over
                    pos = 0
                f.seek(pos)
                sent = 0
                while sent < MAX_PER_FLUSH:
                    line = f.readline()
                    if not line or not line.endswith(b'\n'):
                        break                         # nothing new / a half-written line
                    try:
                        row = json.loads(line.decode('utf-8'))
                    except ValueError:
                        row = {'kind': 'corrupt', 'text': line[:200].decode('utf-8', 'replace')}
                    if not send(_format(row)):
                        break                         # keep it for the next flush
                    pos = f.tell()
                    sent += 1
                    _write_pos(pos_file, pos)
            return sent
    except Exception as e:                            # never into a trading loop
        logger.error('kotak_neo alerts flush failed: %s', e)
        return 0


def _write_pos(path: Path, pos: int) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(str(pos), encoding='utf-8')
    tmp.replace(path)
