"""Telegram, plain text only.

Plain text (no parse_mode) on purpose: an HTML-escape mistake turns a
message into a silent 400, and this book's alerts are the only sign it is
alive. A non-200 is logged with Telegram's own reason and the message.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config_path: Path, enabled: bool, prefix: str):
        self.enabled = enabled
        self.prefix = prefix
        self.sent: List[str] = []          # kept for tests and the EOD log
        self._cfg = None
        if enabled:
            try:
                with open(config_path, encoding='utf-8') as f:
                    self._cfg = json.load(f)
                if not self._cfg.get('bot_token') or not self._cfg.get('chat_id'):
                    raise ValueError('bot_token/chat_id missing')
            except (OSError, ValueError) as exc:
                logger.error('Telegram config unusable at %s (%s): alerts '
                             'will be LOG-ONLY', config_path, exc)
                self._cfg = None

    def send(self, text: str) -> bool:
        msg = '%s\n%s' % (self.prefix, text)
        self.sent.append(msg)
        logger.info('ALERT %s', text.replace('\n', ' | '))
        if not self.enabled or self._cfg is None:
            return False
        try:
            import requests
            r = requests.post(
                'https://api.telegram.org/bot%s/sendMessage' % self._cfg['bot_token'],
                data={'chat_id': self._cfg['chat_id'], 'text': msg[:4000]},
                timeout=10)
            if r.status_code != 200:
                logger.error('Telegram %s: %s | message: %s',
                             r.status_code, r.text[:300], msg[:300])
                return False
            return True
        except Exception as exc:
            logger.error('Telegram send failed: %s | message: %s', exc, msg[:300])
            return False


class NullNotifier(Notifier):
    def __init__(self) -> None:
        self.enabled = False
        self.prefix = ''
        self.sent = []
        self._cfg = None
