"""
backtest_time_to_tp.py — Time-to-TP Backtest for NIFTY Option Magnet

Measures how long it takes for ATM option premium to hit its SuperTrend
level (TP) after an entry signal, across 3 timeframes (15M, 30M, 1H).

Results determine the optimal time-based SL per TF.

Strategy (from option_buy.py):
  - ST(10,3) on ATM CE/PE option candles
  - Entry: STX=DOWN, premium within 3% below ST
  - TP: premium touches ST from below (close >= ST)
  - SL: premium drops 35% from entry

Usage:
  python backtest_time_to_tp.py          # Run full backtest
  python backtest_time_to_tp.py --auth   # Just generate Kite token
"""

import os
import sys
import json
import time
import datetime
import argparse
import pandas as pd
import numpy as np
import pytz
from kiteconnect import KiteConnect

# Add local and SNAIL paths
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, CURRENT_DIR)
sys.path.insert(0, os.path.join(r'C:\Users\mail2\Documents\Projects\BOTS\SNAIL', 'src', 'api'))

import indicators
from kite_auth import KiteAuthenticator
import etf_creds  # secrets live in the untracked credentials.json

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

IST = pytz.timezone('Asia/Kolkata')
CACHE_DIR = os.path.join(CURRENT_DIR, 'backtest_data')
TOKEN_PATH = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'

# Credentials for the account WITH historical data subscription (<personal account>)
KITE_CREDS = {
    'api_key': etf_creds.get('backtest_kite')['api_key'],
    'api_secret': etf_creds.get('backtest_kite')['api_secret'],
    'user_id': etf_creds.get('backtest_kite')['user_id'],
    'password': etf_creds.get('backtest_kite')['password'],
    'totp_secret': etf_creds.get('backtest_kite')['totp_secret'],
    'token_file': TOKEN_PATH,
}

# Strategy params (must match option_buy.py)
ALL_TIMEFRAMES = ['15minute', '30minute', '60minute']
TF_DISPLAY = {'15minute': '15M', '30minute': '30M', '60minute': '1H'}
TF_MINUTES = {'15minute': 15, '30minute': 30, '60minute': 60}

# Kite historical data limits (days)
TF_MAX_DAYS = {'15minute': 200, '30minute': 200, '60minute': 400}

ENTRY_GAP_PCT = 5.0        # entry: premium within 5% below ST
MIN_REWARD_PCT = 3.0        # minimum upside filter: skip if gap < 3%
PREMIUM_SL_PCT = 35         # hard SL: -35% from entry
MIN_ST_CANDLES = 50         # min candles for ST computation
STRIKE_INTERVAL = 50        # NIFTY strike gap
NIFTY_SPOT_TOKEN = 256265   # Kite token for NIFTY 50 index

# How many strikes around ATM to scan (±ATM_RANGE in strike points)
# NIFTY can move ~300-500 pts in a month, so ±500 covers most ATM shifts
ATM_RANGE = 500

# Rate limit between Kite API calls (seconds)
API_SLEEP = 0.4


# ---------------------------------------------------------------------------
# Kite Auth & Client
# ---------------------------------------------------------------------------

def get_kite():
    """Authenticate and return KiteConnect client."""
    auth = KiteAuthenticator(KITE_CREDS)
    access_token = auth.ensure_valid_token()
    kite = KiteConnect(api_key=KITE_CREDS['api_key'])
    kite.set_access_token(access_token)
    return kite


# ---------------------------------------------------------------------------
# Data Download (with caching)
# ---------------------------------------------------------------------------

def download_candles(kite, token, from_date, to_date, interval, cache_key):
    """Download OHLCV candles from Kite. Cached to disk."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe_key = cache_key.replace('/', '_').replace('\\', '_')
    cache_file = os.path.join(CACHE_DIR, f'{safe_key}_{interval}.csv')

    if os.path.exists(cache_file):
        df = pd.read_csv(cache_file, parse_dates=['Date'])
        if len(df) >= 10:
            return df

    time.sleep(API_SLEEP)
    try:
        data = kite.historical_data(int(token), from_date, to_date, interval,
                                    continuous=False)
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data).rename(columns={
            'date': 'Date', 'open': 'Open', 'high': 'High',
            'low': 'Low', 'close': 'Close', 'volume': 'Volume'
        })
        # Normalize timezone — store as IST-naive for simpler matching
        df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)
        df.to_csv(cache_file, index=False)
        return df
    except Exception as e:
        print(f"    [WARN] Download failed ({cache_key}, {interval}): {e}")
        return pd.DataFrame()


def get_nifty_instruments():
    """Load NIFTY options from local op_instruments.csv (already downloaded)."""
    instruments_path = os.path.join(CURRENT_DIR, 'op_instruments.csv')
    df = pd.read_csv(instruments_path, parse_dates=['expiry'])
    nifty = df[
        (df['name'] == 'NIFTY') &
        (df['instrument_type'].isin(['CE', 'PE']))
    ].copy()
    return nifty


# ---------------------------------------------------------------------------
# ATM Strike Identification
# ---------------------------------------------------------------------------

def identify_atm_per_candle(spot_df):
    """For each spot candle, compute ATM strike. Returns Series."""
    return (spot_df['Close'] / STRIKE_INTERVAL).round() * STRIKE_INTERVAL


def unique_atm_strikes(spot_df):
    """Get the set of unique ATM strikes across all spot candles."""
    atm = identify_atm_per_candle(spot_df)
    return sorted(atm.unique().astype(int))


# ---------------------------------------------------------------------------
# SuperTrend Computation
# ---------------------------------------------------------------------------

def compute_st(df):
    """Compute ST(10,3) on option candle DataFrame. Returns df with ST, STX cols or None."""
    if df.empty or len(df) < MIN_ST_CANDLES:
        return None
    result = indicators.SuperTrend(df.copy(), 10, 3)
    if 'ST' not in result.columns:
        return None
    return result


# ---------------------------------------------------------------------------
# Signal Detection & Tracking
# ---------------------------------------------------------------------------

def build_spot_lookup(spot_df):
    """Build a fast lookup: datetime -> spot Close price."""
    lookup = {}
    for _, row in spot_df.iterrows():
        dt = pd.Timestamp(row['Date'])
        if dt.tzinfo is not None:
            dt = dt.tz_localize(None)
        lookup[dt] = row['Close']
    return lookup


def simulate_tf(spot_df, instruments_df, kite, tf):
    """Run backtest for one timeframe. Returns list of trade dicts."""
    print(f"\n{'='*50}")
    print(f"  TIMEFRAME: {TF_DISPLAY[tf]}")
    print(f"{'='*50}")

    now = datetime.datetime.now(IST)
    max_days = TF_MAX_DAYS[tf]
    from_date = (now - datetime.timedelta(days=max_days)).strftime('%Y-%m-%d')
    to_date = now.strftime('%Y-%m-%d')

    # 1. Download spot candles for this TF
    print(f"  Downloading NIFTY spot {TF_DISPLAY[tf]} candles...")
    spot_df = download_candles(kite, NIFTY_SPOT_TOKEN, from_date, to_date,
                               tf, 'NIFTY_SPOT')
    if spot_df.empty:
        print("  [ERROR] No spot data")
        return []

    spot_df = spot_df.sort_values('Date').reset_index(drop=True)
    print(f"  Spot candles: {len(spot_df)} "
          f"({spot_df['Date'].iloc[0].strftime('%Y-%m-%d')} to "
          f"{spot_df['Date'].iloc[-1].strftime('%Y-%m-%d')})")

    # 2. Find unique ATM strikes
    atm_strikes = unique_atm_strikes(spot_df)
    print(f"  Unique ATM strikes: {len(atm_strikes)} "
          f"(range: {atm_strikes[0]}-{atm_strikes[-1]})")

    # 3. Build spot lookup for ATM verification
    spot_lookup = build_spot_lookup(spot_df)

    # 4. Find which instrument tokens match our ATM strikes
    # Filter to near-term expiries (within 90 days) — LEAPS have no intraday data
    now_ts = pd.Timestamp(datetime.datetime.now(IST).strftime('%Y-%m-%d'))
    cutoff = now_ts + pd.Timedelta(days=90)
    all_expiries = sorted(instruments_df['expiry'].unique())
    monthly_expiries = [e for e in all_expiries if pd.Timestamp(e) <= cutoff]
    print(f"  Near-term expiries: {len(monthly_expiries)} "
          f"(of {len(all_expiries)} total)")

    results = []
    options_processed = 0
    options_with_signals = 0

    # Count total options to download for progress tracking
    total_to_download = 0
    for expiry in monthly_expiries:
        exp_df = instruments_df[instruments_df['expiry'] == expiry]
        for opt_type in ['CE', 'PE']:
            type_df = exp_df[exp_df['instrument_type'] == opt_type]
            for strike in atm_strikes:
                if not type_df[type_df['strike'] == float(strike)].empty:
                    total_to_download += 1
    print(f"  Options to download: {total_to_download}")

    download_count = 0
    for expiry in monthly_expiries:
        exp_df = instruments_df[instruments_df['expiry'] == expiry]
        exp_label = pd.Timestamp(expiry).strftime('%d-%b-%Y')

        for opt_type in ['CE', 'PE']:
            type_df = exp_df[exp_df['instrument_type'] == opt_type]

            for strike in atm_strikes:
                match = type_df[type_df['strike'] == float(strike)]
                if match.empty:
                    continue

                row = match.iloc[0]
                token = row['instrument_token']
                sym = row['tradingsymbol']
                download_count += 1

                if download_count % 10 == 0 or download_count == 1:
                    print(f"    [{download_count}/{total_to_download}] "
                          f"{sym}...")

                # Download option candles
                opt_df = download_candles(kite, token, from_date, to_date,
                                          tf, sym)
                if opt_df.empty or len(opt_df) < MIN_ST_CANDLES:
                    continue

                opt_df = opt_df.sort_values('Date').reset_index(drop=True)
                options_processed += 1

                # Compute ST
                st_df = compute_st(opt_df)
                if st_df is None:
                    continue

                # Find signals and track
                trades = find_and_track(st_df, spot_lookup, strike,
                                        opt_type, tf, expiry)
                if trades:
                    options_with_signals += 1
                    results.extend(trades)
                    print(f"    {sym}: {len(trades)} signal(s)")

    print(f"\n  {TF_DISPLAY[tf]} summary: "
          f"{options_processed} options scanned, "
          f"{options_with_signals} had signals, "
          f"{len(results)} total trades")
    return results


def find_and_track(st_df, spot_lookup, strike, opt_type, tf, expiry):
    """Find entry signals in ST data and track each to exit. Returns trade list."""
    trades = []
    n = len(st_df)

    # Track which dates we've already entered for (1 entry per type per TF per day)
    entered_dates = set()

    for i in range(MIN_ST_CANDLES, n):
        row = st_df.iloc[i]
        st_val = float(row['ST'])
        close = float(row['Close'])
        stx = row['STX']

        # Must be ST DOWN with valid ST
        if stx != 'DOWN' or st_val <= 0 or close <= 0:
            continue

        # Gap: how far below ST (positive = below)
        gap = ((st_val - close) / st_val) * 100
        if gap < MIN_REWARD_PCT or gap > ENTRY_GAP_PCT:
            continue

        entry_dt = pd.Timestamp(row['Date'])
        if entry_dt.tzinfo is not None:
            entry_dt = entry_dt.tz_localize(None)

        # Only trade during market hours
        t = entry_dt.time()
        if t < datetime.time(9, 15) or t >= datetime.time(15, 25):
            continue

        # One entry per opt_type per TF per day
        day_key = entry_dt.date()
        if day_key in entered_dates:
            continue

        # Verify this strike was ATM at entry time
        spot = spot_lookup.get(entry_dt, 0)
        if spot <= 0:
            # Try closest spot candle within ±1 TF period
            spot = _nearest_spot(spot_lookup, entry_dt, TF_MINUTES[tf])
        if spot <= 0:
            continue

        atm = round(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL
        if int(atm) != int(strike):
            continue  # not ATM at this time — skip

        # Entry signal confirmed
        entry_premium = close
        prem_sl = entry_premium * (1 - PREMIUM_SL_PCT / 100)

        # Track forward to exit
        exit_type = None
        exit_premium = 0.0
        exit_dt = None

        for j in range(i + 1, n):
            fwd = st_df.iloc[j]
            fwd_dt = pd.Timestamp(fwd['Date'])
            if fwd_dt.tzinfo is not None:
                fwd_dt = fwd_dt.tz_localize(None)
            fwd_close = float(fwd['Close'])

            # New trading day -> EOD exit at previous candle
            if fwd_dt.date() != entry_dt.date():
                exit_type = 'EOD'
                prev = st_df.iloc[j - 1]
                exit_premium = float(prev['Close'])
                exit_dt = pd.Timestamp(prev['Date'])
                if exit_dt.tzinfo is not None:
                    exit_dt = exit_dt.tz_localize(None)
                break

            # EOD at 15:25
            if fwd_dt.time() >= datetime.time(15, 25):
                exit_type = 'EOD'
                exit_premium = fwd_close
                exit_dt = fwd_dt
                break

            # TP: premium touches ST from below
            if fwd_close >= st_val:
                exit_type = 'TP'
                exit_premium = fwd_close
                exit_dt = fwd_dt
                break

            # Premium SL: -35%
            if fwd_close <= prem_sl:
                exit_type = 'PREM_SL'
                exit_premium = fwd_close
                exit_dt = fwd_dt
                break

        if exit_type is None:
            continue  # reached end of data

        entered_dates.add(day_key)
        elapsed_mins = (exit_dt - entry_dt).total_seconds() / 60
        pnl_pct = ((exit_premium - entry_premium) / entry_premium) * 100

        trades.append({
            'date': entry_dt.strftime('%Y-%m-%d'),
            'entry_time': entry_dt.strftime('%H:%M'),
            'exit_time': exit_dt.strftime('%H:%M'),
            'opt_type': opt_type,
            'tf': TF_DISPLAY[tf],
            'strike': int(strike),
            'expiry': pd.Timestamp(expiry).strftime('%Y-%m-%d'),
            'entry_premium': round(entry_premium, 2),
            'st_value': round(st_val, 2),
            'gap_pct': round(gap, 2),
            'exit_type': exit_type,
            'exit_premium': round(exit_premium, 2),
            'pnl_pct': round(pnl_pct, 2),
            'time_to_exit_mins': round(elapsed_mins, 1),
            'spot': round(spot, 2),
        })

    return trades


def _nearest_spot(spot_lookup, target_dt, window_mins):
    """Find closest spot price within window_mins of target_dt."""
    best_spot = 0
    best_delta = float('inf')
    for dt, spot in spot_lookup.items():
        delta = abs((dt - target_dt).total_seconds()) / 60
        if delta <= window_mins and delta < best_delta:
            best_delta = delta
            best_spot = spot
    return best_spot


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_results(df):
    """Print time-to-TP analysis with per-TF breakdown."""
    print("\n" + "=" * 70)
    print("  BACKTEST RESULTS — NIFTY OPTION MAGNET TIME-TO-TP")
    print("=" * 70)

    total = len(df)
    wins = len(df[df['exit_type'] == 'TP'])
    losses = len(df[df['exit_type'] == 'PREM_SL'])
    eod = len(df[df['exit_type'] == 'EOD'])

    print(f"\n  Total signals: {total}")
    print(f"  TP (win):  {wins:>4} ({wins/total*100:5.1f}%)")
    print(f"  PREM_SL:   {losses:>4} ({losses/total*100:5.1f}%)")
    print(f"  EOD:       {eod:>4} ({eod/total*100:5.1f}%)")
    print(f"  Avg P&L:   {df['pnl_pct'].mean():+.2f}%")

    # Date range
    dates = pd.to_datetime(df['date'])
    print(f"\n  Data range: {dates.min().strftime('%Y-%m-%d')} to "
          f"{dates.max().strftime('%Y-%m-%d')} "
          f"({(dates.max() - dates.min()).days} days)")

    # Per-TF breakdown
    time_sl_recommendations = {}

    for tf_label in ['15M', '30M', '1H']:
        tf_df = df[df['tf'] == tf_label]
        if tf_df.empty:
            print(f"\n  --- {tf_label}: No data ---")
            continue

        tp_df = tf_df[tf_df['exit_type'] == 'TP']
        sl_df = tf_df[tf_df['exit_type'] == 'PREM_SL']
        eod_df = tf_df[tf_df['exit_type'] == 'EOD']

        print(f"\n  {'-'*55}")
        print(f"  {tf_label} - {len(tf_df)} signals")
        print(f"  {'-'*55}")
        print(f"  Win (TP):    {len(tp_df):>4} ({len(tp_df)/len(tf_df)*100:5.1f}%)")
        print(f"  PREM_SL:     {len(sl_df):>4} ({len(sl_df)/len(tf_df)*100:5.1f}%)")
        print(f"  EOD:         {len(eod_df):>4} ({len(eod_df)/len(tf_df)*100:5.1f}%)")
        print(f"  Avg P&L:     {tf_df['pnl_pct'].mean():+.2f}%")

        if not tp_df.empty:
            times = tp_df['time_to_exit_mins']
            p90 = times.quantile(0.90)
            print(f"\n  Time to TP (winners only):")
            print(f"    Min:   {times.min():>6.0f} min")
            print(f"    P25:   {times.quantile(0.25):>6.0f} min")
            print(f"    P50:   {times.quantile(0.50):>6.0f} min (median)")
            print(f"    P75:   {times.quantile(0.75):>6.0f} min")
            print(f"    P90:   {p90:>6.0f} min")
            print(f"    P95:   {times.quantile(0.95):>6.0f} min")
            print(f"    Max:   {times.max():>6.0f} min")

            # Map TF label back to key
            tf_key_map = {'15M': '15minute', '30M': '30minute', '1H': '60minute'}
            time_sl_recommendations[tf_key_map[tf_label]] = int(p90)
            print(f"\n  >>> RECOMMENDED TIME_SL for {tf_label}: "
                  f"{int(p90)} min (P90 of TP times)")

        # Losers detail
        if not sl_df.empty:
            print(f"\n  PREM_SL losers:")
            print(f"    Avg time held: {sl_df['time_to_exit_mins'].mean():.0f} min")
            print(f"    Avg loss:      {sl_df['pnl_pct'].mean():.1f}%")

        if not eod_df.empty:
            print(f"\n  EOD exits:")
            print(f"    Avg time held: {eod_df['time_to_exit_mins'].mean():.0f} min")
            print(f"    Avg P&L:       {eod_df['pnl_pct'].mean():+.1f}%")

    # Final recommendation
    if time_sl_recommendations:
        print(f"\n{'='*70}")
        print("  FINAL TF_TIME_SL RECOMMENDATION (P90)")
        print(f"{'='*70}")
        print("  TF_TIME_SL = {")
        for tf_key in ['15minute', '30minute', '60minute']:
            if tf_key in time_sl_recommendations:
                val = time_sl_recommendations[tf_key]
                label = TF_DISPLAY[tf_key]
                print(f'      "{tf_key}": {val},  '
                      f'# {label}: max hold {val} min')
        print("  }")

    return time_sl_recommendations


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='NIFTY Option Magnet Time-to-TP Backtest')
    parser.add_argument('--auth', action='store_true', help='Just generate Kite token')
    parser.add_argument('--analyze', action='store_true',
                        help='Re-analyze existing results (skip download)')
    args = parser.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)

    # Auth only mode
    if args.auth:
        print("Generating Kite token...")
        kite = get_kite()
        print("Token generated and saved. Ready for backtest.")
        return

    # Re-analyze existing results
    results_file = os.path.join(CACHE_DIR, 'backtest_results.csv')
    if args.analyze:
        if not os.path.exists(results_file):
            print(f"No results file found at {results_file}")
            return
        df = pd.read_csv(results_file)
        analyze_results(df)
        return

    # Full backtest
    print("=" * 70)
    print("  NIFTY OPTION MAGNET — TIME-TO-TP BACKTEST")
    print("=" * 70)

    # 1. Auth
    print("\n[1/4] Authenticating with Kite...")
    kite = get_kite()
    print("  Connected")

    # 2. Get instruments
    print("\n[2/4] Loading NIFTY option instruments...")
    instruments = get_nifty_instruments()
    expiries = sorted(instruments['expiry'].unique())
    print(f"  {len(instruments)} NIFTY options across "
          f"{len(expiries)} expiries")
    for exp in expiries[:6]:
        exp_str = pd.Timestamp(exp).strftime('%d-%b-%Y')
        count = len(instruments[instruments['expiry'] == exp])
        print(f"    {exp_str}: {count} options")

    # 3. Run backtest per TF
    print("\n[3/4] Running backtest...")
    all_results = []

    for tf in ALL_TIMEFRAMES:
        tf_results = simulate_tf(None, instruments, kite, tf)
        all_results.extend(tf_results)

    if not all_results:
        print("\n  NO SIGNALS FOUND. Possible causes:")
        print("  - Insufficient historical data (options too recently listed)")
        print("  - ATM strikes not matching available instruments")
        print("  - No ST DOWN signals in available data")
        return

    # Save results
    results_df = pd.DataFrame(all_results)
    results_df.to_csv(results_file, index=False)
    print(f"\n  Results saved to: {results_file}")
    print(f"  Total trades: {len(results_df)}")

    # 4. Analyze
    print("\n[4/4] Analyzing results...")
    recommendations = analyze_results(results_df)

    # Save recommendations
    if recommendations:
        rec_file = os.path.join(CACHE_DIR, 'time_sl_recommendations.json')
        with open(rec_file, 'w') as f:
            json.dump(recommendations, f, indent=2)
        print(f"\n  Recommendations saved to: {rec_file}")

    print("\n  Done. Use --analyze to re-analyze without re-downloading.")


if __name__ == '__main__':
    main()
