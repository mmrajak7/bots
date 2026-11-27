"""`python -m kotak_neo <command>` -- the operator's handle on the shared session.

  login      the morning cron: NOOP if today's session verifies, else ONE login
             (cron at 08:40 and 08:55 on the Pi only; refused elsewhere)
  status     generation, day, logins today, latch, cooldown. Never a secret.
  refresh    a manual refresh request: --reason is required and logged
  unblock    clear a latch (credentials/MPIN) and the cooldown -- owner only

Exit codes: 0 ok / nothing to do, 1 the action did not succeed, 2 usage.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys

from . import session as S

_GOOD = {S.LOGGED_IN, S.NOOP, S.ADOPTED, S.STILL_ALIVE}


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    ap = argparse.ArgumentParser(prog='python -m kotak_neo')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('login', help='scheduled login (Pi cron)')
    sub.add_parser('status', help='safe summary')
    r = sub.add_parser('refresh', help='manual refresh request')
    r.add_argument('--reason', required=True)
    sub.add_parser('unblock', help='clear a latch (owner)')
    a = ap.parse_args(argv)

    if a.cmd == 'status':
        print(json.dumps(S.status(), indent=1, default=str))
        return 0
    if a.cmd == 'unblock':
        out = S.unblock('cli:%s' % getpass.getuser())
        print('unblocked (was: %s)' % out['was'])
        return 0
    if a.cmd == 'login':
        res = S.scheduled_login()
    else:
        cur = S._current_or_none()
        res = S.refresh(cur.generation if cur else 0, 'manual:%s %s'
                        % (getpass.getuser(), a.reason[:80]))
    print('%s %s%s' % (res.outcome, res.detail,
                       ' (generation %d)' % res.session.generation if res.session else ''))
    return 0 if res.outcome in _GOOD else 1


if __name__ == '__main__':
    sys.exit(main())
