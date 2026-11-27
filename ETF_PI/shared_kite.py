"""ETF_PI reads Kite market data through the ONE shared token (owner, 2026-11-27).

`python -m kite_login` (BOTS root, Pi cron 08:30/08:36) makes the personal
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
import tempfile
import time

import requests

TOKEN_PATH = os.environ.get('KITE_TOKEN_FILE') or (
    r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
    if platform.system() == 'Windows'
    else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')

API = 'https://api.kite.trade'
TIMEOUT_S = 20

# These calls now spend the SHARED personal API key's historical budget (Kite ~3/s),
# the same key option_buy, BTST, zebra and the executor use. So every call joins
# option_buy's cross-process historical queue: SAME file, SAME gap, SAME slot
# protocol (reserve under flock, sleep outside it). Must stay in step with
# option_buy.py `_rate_wait` / HIST_MIN_GAP_S / `_rate_file('hist')`.
HIST_MIN_GAP_S = 0.34
RATE_MAX_WAIT_S = 15
RATE_FILE = os.path.join(tempfile.gettempdir(), 'ob_kite_rate_hist.json')
RETRY_BACKOFF_S = (1.0, 2.0)     # on HTTP 429 / network errors only


def _rate_wait(path=RATE_FILE):
    """Reserve the next historical slot across processes; fail-open (never blocks a run)."""
    slot = None
    try:
        fh = open(path, 'a+')
    except OSError:
        return
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass                                 # Windows dev box: single process
        fh.seek(0)
        try:
            last = float(json.loads(fh.read() or '{}').get('last', 0))
        except (ValueError, AttributeError):
            last = 0.0
        now = time.time()
        if last > now + 60:                      # future stamp = clock stepped back
            last = now
        slot = min(max(now, last + HIST_MIN_GAP_S), now + RATE_MAX_WAIT_S)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({'last': slot}))
        fh.flush()
    except OSError:
        pass
    finally:
        fh.close()                               # release the lock BEFORE sleeping
    if slot is not None:
        wait = slot - time.time()
        if wait > 0:
            time.sleep(wait)


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
    hdr = headers(path)
    r = None
    for attempt in range(len(RETRY_BACKOFF_S) + 1):
        _rate_wait()
        try:
            r = requests.get('%s/instruments/historical/%s/%s' % (API, instrument_token, interval),
                             params={'from': from_date, 'to': to_date, 'oi': 1},
                             headers=hdr, timeout=TIMEOUT_S)
        except requests.RequestException as e:
            if attempt == len(RETRY_BACKOFF_S):
                print('shared_kite: historical %s %s' % (instrument_token, type(e).__name__))
                return []
            time.sleep(RETRY_BACKOFF_S[attempt])
            continue
        if r.status_code == 429 and attempt < len(RETRY_BACKOFF_S):
            time.sleep(RETRY_BACKOFF_S[attempt])
            continue
        break
    if r.status_code != 200:
        print('shared_kite: historical %s HTTP %s' % (instrument_token, r.status_code))
        return []
    rows = (r.json().get('data') or {}).get('candles') or []
    return [row[:6] for row in rows]


MAX_DAILY_SPAN_DAYS = 1900       # Kite caps a daily-candle request at 2000 days


def daily(instrument_token, from_date, to_date, path=TOKEN_PATH):
    """Daily candles for [from_date, to_date] in as few calls as Kite allows.
    The scripts used to ask one calendar year per call (6 calls for 5 years);
    same span, same rows, ~3x fewer calls on the shared key. A failed chunk is
    skipped (as a failed year was), the rest still returned."""
    from datetime import timedelta
    out, start = [], from_date
    while start <= to_date:
        end = min(to_date, start + timedelta(days=MAX_DAILY_SPAN_DAYS - 1))
        out.extend(candles(instrument_token, 'day', str(start), str(end), path))
        start = end + timedelta(days=1)
    return out
