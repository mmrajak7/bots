"""
get_instruments.py — Download full Kite instruments file daily.

Cron: 50 8 * * 1-5 python3 .../get_instruments.py >> .../logs/cron.log 2>&1
"""
import pandas as pd
import os
import sys
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
path = os.path.join(SCRIPT_DIR, 'instruments.csv')
url = 'https://api.kite.trade/instruments'

try:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] get_instruments: downloading...")
    df = pd.read_csv(url)
    df = df.reset_index(drop=True)

    if len(df) > 0:
        df.to_csv(path, index=False)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] get_instruments: saved {len(df)} instruments")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] get_instruments: ERROR — empty response")
        sys.exit(1)
except Exception as e:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] get_instruments: FAILED — {e}")
    sys.exit(1)
