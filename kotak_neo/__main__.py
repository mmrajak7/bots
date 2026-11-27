"""`python -m kotak_neo <command>` -- the operator's handle on the shared session.

  login      the morning cron: NOOP if today's session verifies, else ONE login
             (cron at 08:40 and 08:55 on the Pi only; refused elsewhere)
  status     generation, day, logins today, latch, cooldown. Never a secret.
  refresh    a manual refresh request: --reason is required and logged
  unblock    clear a latch (credentials/MPIN) and the cooldown -- owner only
  owners     list which bot holds which contract; --assign SYM BOT / --clear SYM
  measure-second-login   after hours, no bot trading: does a 2nd login kill the 1st?
  probe      REAL 1-lot order probe through this package (armed by PROBE_ARM):
             probe --underlying RELIANCE --expiry 2026-12-29 --strike 1540 --type CE
                   --kite-symbol RELIANCE26DEC1540CE --kite-lot 500 --confirm-real-order

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
    o = sub.add_parser('owners', help='contract ownership')
    o.add_argument('--assign', nargs=2, metavar=('SYMBOL', 'BOT'))
    o.add_argument('--clear', metavar='SYMBOL')
    sub.add_parser('measure-second-login', help='after hours only (DESIGN section 10)')
    pr = sub.add_parser('probe', help='REAL 1-lot order probe (armed by PROBE_ARM)')
    pr.add_argument('--underlying', required=True)
    pr.add_argument('--expiry', required=True)
    pr.add_argument('--strike', type=float, required=True)
    pr.add_argument('--type', required=True, choices=['CE', 'PE'])
    pr.add_argument('--kite-symbol', required=True)
    pr.add_argument('--kite-lot', type=int, required=True)
    pr.add_argument('--ceiling', type=float, default=2000.0)
    pr.add_argument('--confirm-real-order', action='store_true')
    a = ap.parse_args(argv)

    if a.cmd == 'status':
        print(json.dumps(S.status(), indent=1, default=str))
        return 0
    if a.cmd == 'owners':
        from . import client as C
        if a.assign:
            C.assign(a.assign[0], a.assign[1])
        elif a.clear:
            C.assign(a.clear, None)
        print(json.dumps(C.owners(), indent=1, sort_keys=True))
        return 0
    if a.cmd == 'measure-second-login':
        out = S.measure_second_login()
        print(json.dumps(out, indent=1))
        return 0 if not str(out.get('verdict', '')).startswith('REFUSED') else 1
    if a.cmd == 'probe':
        from datetime import date
        from pathlib import Path
        from . import probe as P, symbols as Y
        from .client import NeoClient
        c = NeoClient('prb')
        smap = Y.load(c, underlyings=[a.underlying])
        row = smap.find(a.underlying, date.fromisoformat(a.expiry), a.strike, a.type)
        tok = Path(S.DEFAULTS['credentials_file']).parent / 'kite_access_token.json'
        try:
            return P.run(c, row, a.kite_lot, P.kite_quote_fn(a.kite_symbol, tok),
                         ceiling=a.ceiling, confirm=a.confirm_real_order)
        except P.ProbeRefused as e:
            print('REFUSED: %s' % e)
            return 2
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
