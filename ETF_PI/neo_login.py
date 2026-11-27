"""
neo_login.py -- RETIRED 2026-11-27.

ETF_PI no longer logs in to Kotak NEO. The ONE NEO login for every bot is the
shared `BOTS/kotak_neo` package (Pi cron 08:40 / 08:55: `python -m kotak_neo login`).
A bot logging in on its own is forbidden (kotak_neo/tests/test_login_policy.py).
This stub exits non-zero so a forgotten cron line is noticed, never silently re-enabled.
"""
import sys

if __name__ == '__main__':
    sys.stderr.write('neo_login.py is retired (2026-11-27): the NEO login is '
                     '`python -m kotak_neo login` in ~/Desktop/BOTS\n')
    sys.exit(2)
