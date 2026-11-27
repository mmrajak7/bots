"""`python -m kite_login <command>` -- the morning Kite tokens + holiday calendar.

  morning [--final]   what the Pi cron runs (08:40, and 08:46 with --final):
                      every account's token (NOOP if today's works) + holiday refresh.
                      --final sends ONE Telegram alert if anything is still not right.
  login [--account personal|company] [--final]   tokens only
  holidays [--force]  refresh data/holiday_calendar.json (once a day unless --force)
  status              offline summary: which tokens are from this session. Never a secret.
  import-legacy       ONE-TIME cutover: data/kite_credentials.json from SNAIL + FIFTY files.

Exit codes: 0 all good, 1 something did not succeed, 2 usage.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional

from . import core as C
from . import holidays as H

logger = logging.getLogger('kite_login')

ALERT_PREFIX = 'KITE LOGIN'


def send_telegram(text: str, cfg: dict) -> bool:
    """One message via the shared bot (BOTS/data/telegram_config.json). Never raises."""
    try:
        import requests
        with open(str(C.resolve(cfg['telegram_file'])), encoding='utf-8') as f:
            tg = json.load(f)
        r = requests.post('https://api.telegram.org/bot%s/sendMessage' % tg['bot_token'],
                          json={'chat_id': tg['chat_id'], 'text': text}, timeout=10)
        if r.status_code == 200:
            return True
        logger.error('telegram alert: HTTP %s', r.status_code)
    except Exception as e:
        logger.error('telegram alert failed: %s', type(e).__name__)
    return False


def _alert_text(problems: List[str]) -> str:
    return ('%s: the morning ended with problems\n%s\n\nEvery bot reads these files; check '
            '`python -m kite_login status` on the Pi, fix, then `python -m kite_login morning`.'
            % (ALERT_PREFIX, '\n'.join('- ' + p for p in problems)))


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    ap = argparse.ArgumentParser(prog='python -m kite_login')
    sub = ap.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('morning', help='tokens + holidays (Pi cron)')
    m.add_argument('--final', action='store_true', help='last slot: alert on any problem')
    lg = sub.add_parser('login', help='tokens only')
    lg.add_argument('--account', action='append', help='alias; repeat for several (default all)')
    lg.add_argument('--final', action='store_true')
    h = sub.add_parser('holidays', help='refresh the NSE holiday calendar')
    h.add_argument('--force', action='store_true')
    sub.add_parser('status', help='offline summary')
    sub.add_parser('import-legacy', help='ONE-TIME: build the credentials file from SNAIL + FIFTY')
    a = ap.parse_args(argv)

    cfg = C.load_config()
    if a.cmd == 'status':
        print(json.dumps(C.status(cfg), indent=1))
        return 0
    if a.cmd == 'import-legacy':
        from . import migrate
        ok, msg = migrate.import_legacy(cfg)
        print(msg)
        return 0 if ok else 1

    problems: List[str] = []
    if a.cmd in ('morning', 'login'):
        try:
            results = C.run(getattr(a, 'account', None), cfg)
        except ValueError as e:
            print(e)
            return 2
        for r in results:
            print('%-9s %-12s %s' % (r.account, r.outcome, r.detail))
            if not r.ok:
                problems.append('%s token: %s %s' % (r.account, r.outcome, r.detail))
    if a.cmd in ('morning', 'holidays'):
        ok, msg = H.refresh(C.resolve(cfg['holiday_file']), force=getattr(a, 'force', False))
        print('%-9s %-12s %s' % ('holidays', 'OK' if ok else 'FAILED', msg))
        if not ok:
            problems.append('holiday calendar: %s' % msg)

    if problems and getattr(a, 'final', False):
        send_telegram(_alert_text(problems), cfg)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
