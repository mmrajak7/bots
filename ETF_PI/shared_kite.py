"""ETF_PI reads Kite market data through the ONE shared token (owner, 2026-11-27).

`python -m kite_login` (BOTS root, Pi cron 08:40/08:46) makes the personal
market-data token every weekday morning. ETF_PI NEVER logs in on its own: until
2026-11-27 tracker.py / extract_data.py each did a Kite WEB login (enctoken) to
a third account from credentials in code; `kite_login/tests/test_login_policy.py`
now fails the commit if a login endpoint reappears here.

Absolute path on purpose: ETF_PI's cron runs from a second clone
(~/Desktop/Scripts/bots), so a repo-relative path would miss the shared file.
"""
import json
import os
import platform
import time

import requests

TOKEN_PATH = os.environ.get('KITE_TOKEN_FILE') or (
    r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
    if platform.system() == 'Windows'
    else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')

API = 'https://api.kite.trade'
TIMEOUT_S = 20
PACE_S = 0.35            # Kite Connect historical limit is 3 requests/second


class TokenMissing(RuntimeError):
    """The shared token file is absent/unreadable -- kite_login has not run."""


def load_token(path=TOKEN_PATH):
    """(api_key, access_token) from the shared file, or TokenMissing naming the path."""
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
        return d['api_key'], d['access_token']
    except (OSError, ValueError, KeyError) as e:
        raise TokenMissing('shared Kite token unreadable at %s (%s) -- run '
                           '`python -m kite_login morning` on the Pi' % (path, type(e).__name__))


def headers(path=TOKEN_PATH):
    api_key, access_token = load_token(path)
    return {'X-Kite-Version': '3',
            'Authorization': 'token %s:%s' % (api_key, access_token)}


def candles(instrument_token, interval, from_date, to_date, path=TOKEN_PATH):
    """Kite Connect historical candles WITHOUT the oi column:
    [[ts, open, high, low, close, volume], ...] -- the shape the old web-endpoint
    reader returned. [] on any non-200 (callers treat empty as 'no data')."""
    time.sleep(PACE_S)
    r = requests.get('%s/instruments/historical/%s/%s' % (API, instrument_token, interval),
                     params={'from': from_date, 'to': to_date, 'oi': 1},
                     headers=headers(path), timeout=TIMEOUT_S)
    if r.status_code != 200:
        print('shared_kite: historical %s HTTP %s' % (instrument_token, r.status_code))
        return []
    rows = (r.json().get('data') or {}).get('candles') or []
    return [row[:6] for row in rows]
