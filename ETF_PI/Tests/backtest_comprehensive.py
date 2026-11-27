"""
Comprehensive Backtest - ST Signals + Support Zone Signals
- All Indexes: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX
- December 2025 (range-bound) + January 2026 (bearish)
- Timeframes: 3M, 5M, 10M, 15M
- Collects: Regime, Underlying ST directions, Candle patterns, etc.

Two signal types:
1. SuperTrend Signal: Price near ST level, ST direction UP
2. Support Zone Signal: Two candle lows forming support + Regime filter
"""

import pandas as pd
import datetime
import pytz
import json
import time
import platform
import os
import indicators
from kiteconnect import KiteConnect

pd.set_option('display.max_columns', None)
pd.set_option('display.width', None)
pd.set_option('display.max_rows', None)

# Token path
if platform.system() == 'Windows':
    TOKEN_PATH = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
else:
    TOKEN_PATH = '/home/trustit/Desktop/BOTS/data/kite_access_token.json'

# Constants
SUPPORT_ZONE_LOW_DIFF_PCT = 0.03
SUPPORT_ZONE_ENTRY_PCT = 0.01
SL_PERCENT = 0.05       # 5% below entry for support zone
TARGET_PERCENT = 0.25   # 25% above entry
ST_SL_PERCENT = 0.065   # 6.5% SL for ST signals
ST_TARGET_PERCENT = 0.25  # 25% target for ST signals
COOLDOWN_CANDLES = 10   # Minimum candles between signals

# Signal proximity thresholds for ST (price/st_price ratio)
SIGNAL_MIN_RATIO = 1.01
SIGNAL_MAX_RATIO = 1.025

# All indexes with their tokens
INDEXES = [
    {'name': 'NIFTY', 'token': 256265, 'strike_interval': 50},
    {'name': 'BANKNIFTY', 'token': 260105, 'strike_interval': 100},
    {'name': 'FINNIFTY', 'token': 257801, 'strike_interval': 50},
    {'name': 'MIDCPNIFTY', 'token': 288009, 'strike_interval': 25},
    {'name': 'SENSEX', 'token': 265, 'strike_interval': 100},
]

# Timeframes
TIMEFRAMES = ['3minute', '5minute', '10minute', '15minute']
TIMEFRAME_DISPLAY = {'3minute': '3M', '5minute': '5M', '10minute': '10M', '15minute': '15M'}

# Kite client
_KITE_CLIENT = None
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))


def get_kite_client():
    global _KITE_CLIENT
    if _KITE_CLIENT is None:
        with open(TOKEN_PATH, 'r') as f:
            token_data = json.load(f)
        _KITE_CLIENT = KiteConnect(api_key=token_data['api_key'])
        _KITE_CLIENT.set_access_token(token_data['access_token'])
    return _KITE_CLIENT


def get_historical_data(token, start_date, end_date, interval):
    """Fetch historical data from Kite"""
    kite = get_kite_client()
    time.sleep(0.4)  # Rate limiting
    try:
        data = kite.historical_data(
            instrument_token=int(token),
            from_date=start_date,
            to_date=end_date,
            interval=interval
        )
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        df = df.rename(columns={
            'date': 'Date', 'open': 'Open', 'high': 'High',
            'low': 'Low', 'close': 'Close', 'volume': 'Volume'
        })
        return df
    except Exception as e:
        print(f"API Error: {e}")
        return pd.DataFrame()


def calculate_supertrend(data, period=10, multiplier=3):
    """Calculate SuperTrend indicator"""
    if len(data) < period + 1:
        return None
    try:
        st = indicators.SuperTrend(data, period, multiplier)
        st_column = 'ST' if 'ST' in st.columns else 'STX'
        return st[st_column]
    except Exception as e:
        print(f"ST calculation error: {e}")
        return None


def get_atm_strike(price, strike_interval):
    """Calculate ATM strike"""
    return round(price / strike_interval) * strike_interval


def get_option_token(df_instruments, index_name, strike, expiry, opt_type='CE'):
    """Get option token for given parameters"""
    day = expiry.strftime('%d')
    month_single_digit = str(int(expiry.strftime('%m')))
    year_short = expiry.strftime('%y')
    month_abbr = expiry.strftime('%b').upper()

    formats = [
        f"{index_name}{year_short}{month_single_digit}{day}{int(strike)}{opt_type}",
        f"{index_name}{year_short}{month_abbr}{int(strike)}{opt_type}",
        f"{index_name}{day}{expiry.strftime('%m')}{year_short}{int(strike)}{opt_type}",
        f"{index_name}{day}{month_abbr}{year_short}{int(strike)}{opt_type}",
    ]

    for fmt in formats:
        match = df_instruments[df_instruments['tradingsymbol'] == fmt]
        if not match.empty:
            return match['instrument_token'].iloc[0], fmt

    return None, None


def get_expiry_for_date(df_instruments, index_name, check_date, use_monthly=True):
    """Get relevant expiry for a date"""
    expiries = sorted(df_instruments[df_instruments['name'] == index_name]['expiry'].dt.date.unique())
    future_expiries = [e for e in expiries if e >= check_date]

    if not future_expiries:
        return None

    if use_monthly:
        check_month = check_date.month
        check_year = check_date.year
        same_month_expiries = [e for e in future_expiries if e.month == check_month and e.year == check_year]
        if same_month_expiries:
            return max(same_month_expiries)
        next_month = check_month + 1 if check_month < 12 else 1
        next_year = check_year if check_month < 12 else check_year + 1
        next_month_expiries = [e for e in future_expiries if e.month == next_month and e.year == next_year]
        if next_month_expiries:
            return max(next_month_expiries)
        return future_expiries[0] if future_expiries else None
    else:
        return future_expiries[0]


def get_candle_pattern(candle1, candle2):
    """Get two-candle pattern"""
    c1_green = candle1['Close'] >= candle1['Open']
    c2_green = candle2['Close'] >= candle2['Open']

    if c1_green and c2_green:
        return 'GG'
    elif not c1_green and not c2_green:
        return 'RR'
    elif c1_green and not c2_green:
        return 'GR'
    else:
        return 'RG'


def detect_regime_at_index(daily_data, idx):
    """
    Detect market regime at a specific index in daily data
    Returns: 'BEARISH', 'BULLISH', or 'RANGE'
    """
    if idx < 2:
        return 'RANGE'

    today = daily_data.iloc[idx]
    yesterday = daily_data.iloc[idx - 1]

    # Gap detection
    gap_down = today['Open'] < yesterday['Close'] * 0.999
    gap_up = today['Open'] > yesterday['Close'] * 1.001

    # Intraday direction
    intraday_bullish = today['Close'] > today['Open']

    # Recent 3-day direction
    start_idx = max(0, idx - 2)
    recent_3 = daily_data.iloc[start_idx:idx + 1]
    bullish_days = (recent_3['Close'] > recent_3['Open']).sum()
    bearish_days = len(recent_3) - bullish_days

    if gap_down or (bearish_days >= 2 and not intraday_bullish):
        return 'BEARISH'
    elif gap_up or (bullish_days >= 2 and intraday_bullish):
        return 'BULLISH'
    else:
        return 'RANGE'


def get_underlying_st_directions(spot_data_by_tf, candle_time):
    """
    Get ST directions for underlying across all timeframes at a given time
    Returns dict: {'3M': 'UP/DOWN', '5M': 'UP/DOWN', ...}
    """
    directions = {}
    for tf, (data, st_values) in spot_data_by_tf.items():
        if st_values is None:
            directions[TIMEFRAME_DISPLAY.get(tf, tf)] = 'NA'
            continue

        # Find candle at or before the given time
        mask = data['Date'] <= candle_time
        if not mask.any():
            directions[TIMEFRAME_DISPLAY.get(tf, tf)] = 'NA'
            continue

        idx = mask.sum() - 1
        if idx >= len(st_values) or pd.isna(st_values.iloc[idx]):
            directions[TIMEFRAME_DISPLAY.get(tf, tf)] = 'NA'
            continue

        st_val = st_values.iloc[idx]
        close_val = data.iloc[idx]['Close']
        directions[TIMEFRAME_DISPLAY.get(tf, tf)] = 'UP' if close_val > st_val else 'DOWN'

    return directions


def check_st_signal_at_index(data, st_values, idx):
    """
    Check if there's an ST signal at given index
    Signal: ST is UP (green) and price is near ST level
    """
    if st_values is None or idx >= len(st_values) or pd.isna(st_values.iloc[idx]):
        return None

    current = data.iloc[idx]
    st_value = st_values.iloc[idx]

    # ST must be UP (price above ST)
    if current['Close'] <= st_value:
        return None

    # Price should be close to ST (within ratio range)
    ratio = current['Close'] / st_value
    if not (SIGNAL_MIN_RATIO <= ratio <= SIGNAL_MAX_RATIO):
        return None

    return {
        'signal_time': current['Date'],
        'close': current['Close'],
        'st_value': st_value,
        'ratio': ratio
    }


def check_support_zone_at_index(data, st_values, idx):
    """
    Check for support zone pattern at given index
    """
    if idx < 3 or st_values is None or idx >= len(st_values):
        return None

    candle1 = data.iloc[idx - 2]
    candle2 = data.iloc[idx - 1]
    current = data.iloc[idx]

    low1, low2 = candle1['Low'], candle2['Low']
    high1, high2 = candle1['High'], candle2['High']
    current_price = current['Close']

    # ST UP filter
    st_value = st_values.iloc[idx]
    if pd.isna(st_value) or current_price <= st_value:
        return None

    # Validate prices
    if low1 <= 0 or low2 <= 0 or high1 <= 0 or high2 <= 0:
        return None

    # Check lows within 3%
    min_low = min(low1, low2)
    low_diff_pct = abs(low1 - low2) / min_low
    if low_diff_pct > SUPPORT_ZONE_LOW_DIFF_PCT:
        return None

    highest_low = max(low1, low2)

    # Check current price within 1% of highest low
    price_diff_pct = abs(current_price - highest_low) / highest_low
    if price_diff_pct > SUPPORT_ZONE_ENTRY_PCT:
        return None

    # Price should be above support
    if current_price < min_low * 0.99:
        return None

    entry = highest_low
    sl = min_low * (1 - SL_PERCENT)
    target = entry * (1 + TARGET_PERCENT)

    if entry - sl <= 0 or target - entry <= 0:
        return None

    candle_pattern = get_candle_pattern(candle1, candle2)

    return {
        'signal_time': current['Date'],
        'current_price': current_price,
        'entry': entry,
        'sl': sl,
        'target': target,
        'st_value': st_value,
        'rr_ratio': (target - entry) / (entry - sl),
        'candle_pattern': candle_pattern,
        'c1_low': low1,
        'c2_low': low2,
        'c1_high': high1,
        'c2_high': high2
    }


def analyze_outcome(data, signal_idx, entry, sl, target):
    """Determine trade outcome"""
    if signal_idx >= len(data) - 1:
        return 'OPEN', None, None, None

    after_data = data.iloc[signal_idx + 1:]
    if after_data.empty:
        return 'OPEN', None, None, None

    high_after = after_data['High'].max()
    low_after = after_data['Low'].min()

    for _, candle in after_data.iterrows():
        if candle['Low'] <= sl:
            return 'SL_HIT', high_after, low_after, sl
        if candle['High'] >= target:
            return 'TARGET_HIT', high_after, low_after, target

    last_price = after_data.iloc[-1]['Close']
    return 'OPEN', high_after, low_after, last_price


def main():
    ist = pytz.timezone('Asia/Kolkata')

    print("=" * 120)
    print("COMPREHENSIVE BACKTEST - ST Signals + Support Zone Signals")
    print("=" * 120)
    print(f"Run Time: {datetime.datetime.now(ist).strftime('%Y-%m-%d %H:%M:%S')}")

    # Load instruments
    df_instruments = pd.read_csv(os.path.join(CURRENT_DIR, 'op_instruments.csv'))
    df_instruments['expiry'] = pd.to_datetime(df_instruments['expiry'])

    # Date range: Dec 1, 2025 to Jan 23, 2026
    # Need extra days before Dec 1 for ST calculation
    data_start = datetime.date(2025, 11, 15)  # Extra days for ST warmup
    backtest_start = datetime.date(2025, 12, 1)
    backtest_end = datetime.date(2026, 1, 23)

    print(f"\nData fetch period: {data_start} to {backtest_end}")
    print(f"Backtest period: {backtest_start} to {backtest_end}")
    print(f"Indexes: {[idx['name'] for idx in INDEXES]}")
    print(f"Timeframes: {TIMEFRAMES}")

    # ============================
    # PHASE 1: Download all data
    # ============================
    print("\n" + "=" * 80)
    print("PHASE 1: Downloading Historical Data")
    print("=" * 80)

    # Store all data
    spot_data_cache = {}  # {index_name: {tf: (data, st_values)}}
    daily_data_cache = {}  # {index_name: daily_data}
    option_data_cache = {}  # {(index, date, strike, opt_type, tf): (data, st_values)}

    for idx_info in INDEXES:
        idx_name = idx_info['name']
        print(f"\n[{idx_name}] Downloading spot data...")

        # Daily data for regime detection
        print(f"  Fetching daily data...")
        daily_data = get_historical_data(
            idx_info['token'],
            data_start.strftime('%Y-%m-%d'),
            backtest_end.strftime('%Y-%m-%d'),
            'day'
        )
        if not daily_data.empty:
            daily_data_cache[idx_name] = daily_data
            print(f"    Daily: {len(daily_data)} candles")

        # Spot data for each timeframe
        spot_data_cache[idx_name] = {}
        for tf in TIMEFRAMES:
            print(f"  Fetching {tf} data...")
            data = get_historical_data(
                idx_info['token'],
                data_start.strftime('%Y-%m-%d'),
                backtest_end.strftime('%Y-%m-%d'),
                tf
            )
            if not data.empty:
                st_values = calculate_supertrend(data)
                spot_data_cache[idx_name][tf] = (data, st_values)
                print(f"    {tf}: {len(data)} candles, ST calculated: {st_values is not None}")
            time.sleep(0.3)

    # ============================
    # PHASE 2: Run Signal Simulations
    # ============================
    print("\n" + "=" * 80)
    print("PHASE 2: Running Signal Simulations")
    print("=" * 80)

    all_st_signals = []
    all_sz_signals = []  # Support Zone signals

    for idx_info in INDEXES:
        idx_name = idx_info['name']
        strike_interval = idx_info['strike_interval']

        print(f"\n[{idx_name}] Processing signals...")

        if idx_name not in spot_data_cache or idx_name not in daily_data_cache:
            print(f"  Skipping - no data")
            continue

        daily_data = daily_data_cache[idx_name]
        spot_by_tf = spot_data_cache[idx_name]

        # Get unique trading dates in backtest period
        trading_dates = []
        for tf in TIMEFRAMES:
            if tf in spot_by_tf:
                data, _ = spot_by_tf[tf]
                dates = data[data['Date'].dt.date >= backtest_start]['Date'].dt.date.unique()
                trading_dates.extend(dates)
        trading_dates = sorted(set([d for d in trading_dates if d <= backtest_end]))

        print(f"  Trading days: {len(trading_dates)}")

        # Process each trading day
        for trade_date in trading_dates:
            # Get regime for this day
            daily_mask = daily_data['Date'].dt.date <= trade_date
            if not daily_mask.any():
                continue
            daily_idx = daily_mask.sum() - 1
            regime = detect_regime_at_index(daily_data, daily_idx)

            # Get spot price for ATM calculation
            spot_price = daily_data.iloc[daily_idx]['Close']
            atm_strike = get_atm_strike(spot_price, strike_interval)

            # Get expiry for this date
            expiry = get_expiry_for_date(df_instruments, idx_name, trade_date, use_monthly=True)
            if expiry is None:
                continue

            # Process each timeframe
            for tf in TIMEFRAMES:
                if tf not in spot_by_tf:
                    continue

                spot_data, spot_st = spot_by_tf[tf]
                tf_display = TIMEFRAME_DISPLAY.get(tf, tf)

                # Get candles for this day
                day_mask = spot_data['Date'].dt.date == trade_date
                day_indices = spot_data[day_mask].index.tolist()

                if not day_indices:
                    continue

                # Process CE and PE options
                for opt_type in ['CE', 'PE']:
                    # Get option token
                    opt_token, opt_symbol = get_option_token(df_instruments, idx_name, atm_strike, expiry, opt_type)
                    if opt_token is None:
                        continue

                    # Check if we have option data cached, if not fetch it
                    cache_key = (idx_name, trade_date, atm_strike, opt_type, tf)
                    if cache_key not in option_data_cache:
                        opt_data = get_historical_data(
                            opt_token,
                            (trade_date - datetime.timedelta(days=5)).strftime('%Y-%m-%d'),
                            (trade_date + datetime.timedelta(days=5)).strftime('%Y-%m-%d'),
                            tf
                        )
                        if opt_data.empty:
                            option_data_cache[cache_key] = (None, None)
                        else:
                            opt_st = calculate_supertrend(opt_data)
                            option_data_cache[cache_key] = (opt_data, opt_st)
                        time.sleep(0.35)

                    opt_data, opt_st = option_data_cache[cache_key]
                    if opt_data is None:
                        continue

                    # Get underlying ST directions at various TFs
                    sample_time = spot_data.iloc[day_indices[0]]['Date'] if day_indices else None

                    # Track last signal index for cooldown
                    last_st_signal_idx = -COOLDOWN_CANDLES - 1
                    last_sz_signal_idx = -COOLDOWN_CANDLES - 1

                    # Scan through the day's candles
                    for i, spot_idx in enumerate(day_indices):
                        # Get option data index
                        candle_time = spot_data.iloc[spot_idx]['Date']
                        opt_mask = opt_data['Date'] == candle_time
                        if not opt_mask.any():
                            continue
                        opt_idx = opt_data[opt_mask].index[0]

                        # Get underlying ST directions
                        underlying_st_dirs = get_underlying_st_directions(spot_by_tf, candle_time)
                        up_count = sum(1 for v in underlying_st_dirs.values() if v == 'UP')
                        down_count = sum(1 for v in underlying_st_dirs.values() if v == 'DOWN')

                        # ================================
                        # CHECK ST SIGNAL
                        # ================================
                        if i - last_st_signal_idx > COOLDOWN_CANDLES:
                            st_signal = check_st_signal_at_index(opt_data, opt_st, opt_idx)
                            if st_signal:
                                entry = st_signal['close']
                                sl = entry * (1 - ST_SL_PERCENT)
                                target = entry * (1 + ST_TARGET_PERCENT)

                                outcome, high_after, low_after, exit_price = analyze_outcome(
                                    opt_data, opt_idx, entry, sl, target
                                )

                                pnl_pct = ((exit_price - entry) / entry * 100) if exit_price else 0

                                all_st_signals.append({
                                    'date': trade_date,
                                    'day': trade_date.strftime('%a'),
                                    'index': idx_name,
                                    'symbol': opt_symbol,
                                    'opt_type': opt_type,
                                    'strike': atm_strike,
                                    'tf': tf_display,
                                    'signal_type': 'ST',
                                    'signal_time': st_signal['signal_time'],
                                    'spot_price': spot_data.iloc[spot_idx]['Close'],
                                    'entry': entry,
                                    'sl': sl,
                                    'target': target,
                                    'st_value': st_signal['st_value'],
                                    'ratio': st_signal['ratio'],
                                    'regime': regime,
                                    'underlying_st_3M': underlying_st_dirs.get('3M', 'NA'),
                                    'underlying_st_5M': underlying_st_dirs.get('5M', 'NA'),
                                    'underlying_st_10M': underlying_st_dirs.get('10M', 'NA'),
                                    'underlying_st_15M': underlying_st_dirs.get('15M', 'NA'),
                                    'underlying_up_count': up_count,
                                    'underlying_down_count': down_count,
                                    'high_after': high_after,
                                    'low_after': low_after,
                                    'outcome': outcome,
                                    'exit_price': exit_price,
                                    'pnl_pct': pnl_pct
                                })
                                last_st_signal_idx = i

                        # ================================
                        # CHECK SUPPORT ZONE SIGNAL
                        # ================================
                        if i - last_sz_signal_idx > COOLDOWN_CANDLES:
                            sz_signal = check_support_zone_at_index(opt_data, opt_st, opt_idx)
                            if sz_signal:
                                outcome, high_after, low_after, exit_price = analyze_outcome(
                                    opt_data, opt_idx, sz_signal['entry'], sz_signal['sl'], sz_signal['target']
                                )

                                pnl_pct = ((exit_price - sz_signal['entry']) / sz_signal['entry'] * 100) if exit_price else 0

                                all_sz_signals.append({
                                    'date': trade_date,
                                    'day': trade_date.strftime('%a'),
                                    'index': idx_name,
                                    'symbol': opt_symbol,
                                    'opt_type': opt_type,
                                    'strike': atm_strike,
                                    'tf': tf_display,
                                    'signal_type': 'SUPPORT_ZONE',
                                    'signal_time': sz_signal['signal_time'],
                                    'spot_price': spot_data.iloc[spot_idx]['Close'],
                                    'entry': sz_signal['entry'],
                                    'sl': sz_signal['sl'],
                                    'target': sz_signal['target'],
                                    'rr_ratio': sz_signal['rr_ratio'],
                                    'candle_pattern': sz_signal['candle_pattern'],
                                    'regime': regime,
                                    'underlying_st_3M': underlying_st_dirs.get('3M', 'NA'),
                                    'underlying_st_5M': underlying_st_dirs.get('5M', 'NA'),
                                    'underlying_st_10M': underlying_st_dirs.get('10M', 'NA'),
                                    'underlying_st_15M': underlying_st_dirs.get('15M', 'NA'),
                                    'underlying_up_count': up_count,
                                    'underlying_down_count': down_count,
                                    'high_after': high_after,
                                    'low_after': low_after,
                                    'outcome': outcome,
                                    'exit_price': exit_price,
                                    'pnl_pct': pnl_pct
                                })
                                last_sz_signal_idx = i

        print(f"  ST signals: {len([s for s in all_st_signals if s['index'] == idx_name])}")
        print(f"  SZ signals: {len([s for s in all_sz_signals if s['index'] == idx_name])}")

    # ============================
    # PHASE 3: Save Results
    # ============================
    print("\n" + "=" * 80)
    print("PHASE 3: Saving Results")
    print("=" * 80)

    st_df = pd.DataFrame(all_st_signals)
    sz_df = pd.DataFrame(all_sz_signals)

    st_csv = os.path.join(CURRENT_DIR, f"backtest_st_signals_{backtest_start.strftime('%Y%m%d')}_{backtest_end.strftime('%Y%m%d')}.csv")
    sz_csv = os.path.join(CURRENT_DIR, f"backtest_sz_signals_{backtest_start.strftime('%Y%m%d')}_{backtest_end.strftime('%Y%m%d')}.csv")

    if not st_df.empty:
        st_df.to_csv(st_csv, index=False)
        print(f"ST signals saved: {st_csv} ({len(st_df)} signals)")

    if not sz_df.empty:
        sz_df.to_csv(sz_csv, index=False)
        print(f"SZ signals saved: {sz_csv} ({len(sz_df)} signals)")

    # ============================
    # PHASE 4: Analytics
    # ============================
    print("\n" + "=" * 120)
    print("PHASE 4: ANALYTICS")
    print("=" * 120)

    for signal_type, df in [('ST SIGNALS', st_df), ('SUPPORT ZONE SIGNALS', sz_df)]:
        if df.empty:
            print(f"\n{signal_type}: No signals")
            continue

        print(f"\n{'=' * 100}")
        print(f"{signal_type} ANALYSIS")
        print(f"{'=' * 100}")

        print(f"\nTotal signals: {len(df)}")

        # Split by month
        df['month'] = pd.to_datetime(df['date']).dt.to_period('M')

        print("\n" + "-" * 80)
        print("BY MONTH")
        print("-" * 80)
        print(f"{'Month':<12} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
        print("-" * 60)

        for month in sorted(df['month'].unique()):
            m_df = df[df['month'] == month]
            total = len(m_df)
            targets = len(m_df[m_df['outcome'] == 'TARGET_HIT'])
            sls = len(m_df[m_df['outcome'] == 'SL_HIT'])
            closed = m_df[m_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            print(f"{str(month):<12} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # By Regime
        print("\n" + "-" * 80)
        print("BY REGIME")
        print("-" * 80)
        print(f"{'Regime':<12} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
        print("-" * 60)

        for regime in ['BEARISH', 'BULLISH', 'RANGE']:
            r_df = df[df['regime'] == regime]
            if r_df.empty:
                continue
            total = len(r_df)
            targets = len(r_df[r_df['outcome'] == 'TARGET_HIT'])
            sls = len(r_df[r_df['outcome'] == 'SL_HIT'])
            closed = r_df[r_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            print(f"{regime:<12} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # By Regime + CE/PE
        print("\n" + "-" * 80)
        print("BY REGIME + CE/PE")
        print("-" * 80)
        print(f"{'Regime':<12} {'Type':<5} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
        print("-" * 70)

        for regime in ['BEARISH', 'BULLISH', 'RANGE']:
            for opt in ['CE', 'PE']:
                r_df = df[(df['regime'] == regime) & (df['opt_type'] == opt)]
                if r_df.empty:
                    continue
                total = len(r_df)
                targets = len(r_df[r_df['outcome'] == 'TARGET_HIT'])
                sls = len(r_df[r_df['outcome'] == 'SL_HIT'])
                closed = r_df[r_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
                win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
                total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
                print(f"{regime:<12} {opt:<5} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # By Index
        print("\n" + "-" * 80)
        print("BY INDEX")
        print("-" * 80)
        print(f"{'Index':<12} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
        print("-" * 60)

        for idx in df['index'].unique():
            i_df = df[df['index'] == idx]
            total = len(i_df)
            targets = len(i_df[i_df['outcome'] == 'TARGET_HIT'])
            sls = len(i_df[i_df['outcome'] == 'SL_HIT'])
            closed = i_df[i_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            print(f"{idx:<12} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # By Timeframe
        print("\n" + "-" * 80)
        print("BY TIMEFRAME")
        print("-" * 80)
        print(f"{'TF':<8} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
        print("-" * 55)

        for tf in ['3M', '5M', '10M', '15M']:
            t_df = df[df['tf'] == tf]
            if t_df.empty:
                continue
            total = len(t_df)
            targets = len(t_df[t_df['outcome'] == 'TARGET_HIT'])
            sls = len(t_df[t_df['outcome'] == 'SL_HIT'])
            closed = t_df[t_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            print(f"{tf:<8} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # By Underlying ST alignment (for ST signals)
        if signal_type == 'ST SIGNALS' and 'underlying_up_count' in df.columns:
            print("\n" + "-" * 80)
            print("BY UNDERLYING ST ALIGNMENT (Higher TFs supporting)")
            print("-" * 80)
            print(f"{'UP Count':<12} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
            print("-" * 60)

            for up_cnt in sorted(df['underlying_up_count'].unique()):
                u_df = df[df['underlying_up_count'] == up_cnt]
                total = len(u_df)
                targets = len(u_df[u_df['outcome'] == 'TARGET_HIT'])
                sls = len(u_df[u_df['outcome'] == 'SL_HIT'])
                closed = u_df[u_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
                win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
                total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
                print(f"{int(up_cnt):<12} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

        # Support Zone specific: By candle pattern
        if signal_type == 'SUPPORT ZONE SIGNALS' and 'candle_pattern' in df.columns:
            print("\n" + "-" * 80)
            print("BY CANDLE PATTERN")
            print("-" * 80)
            print(f"{'Pattern':<10} {'Signals':>8} {'Targets':>8} {'SL':>8} {'WinRate':>10} {'P&L':>12}")
            print("-" * 55)

            for pattern in ['GG', 'RR', 'GR', 'RG']:
                p_df = df[df['candle_pattern'] == pattern]
                if p_df.empty:
                    continue
                total = len(p_df)
                targets = len(p_df[p_df['outcome'] == 'TARGET_HIT'])
                sls = len(p_df[p_df['outcome'] == 'SL_HIT'])
                closed = p_df[p_df['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
                win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
                total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
                print(f"{pattern:<10} {total:>8} {targets:>8} {sls:>8} {win_rate:>9.1f}% {total_pnl:>11.2f}%")

    # ============================
    # PHASE 5: Filter Comparison
    # ============================
    print("\n" + "=" * 120)
    print("PHASE 5: FILTER COMPARISON - SUPPORT ZONE")
    print("=" * 120)

    if not sz_df.empty:
        filters = [
            ('No Filter', sz_df),
            ('PE Only', sz_df[sz_df['opt_type'] == 'PE']),
            ('BEARISH + PE', sz_df[(sz_df['regime'] == 'BEARISH') & (sz_df['opt_type'] == 'PE')]),
            ('BULLISH + CE', sz_df[(sz_df['regime'] == 'BULLISH') & (sz_df['opt_type'] == 'CE')]),
            ('Aligned (BEAR+PE / BULL+CE)', sz_df[
                ((sz_df['regime'] == 'BEARISH') & (sz_df['opt_type'] == 'PE')) |
                ((sz_df['regime'] == 'BULLISH') & (sz_df['opt_type'] == 'CE'))
            ]),
        ]

        print(f"\n{'Filter':<35} {'Signals':>10} {'WinRate':>10} {'P&L':>12} {'P&L/Trade':>12}")
        print("-" * 85)

        for name, fdf in filters:
            if fdf.empty:
                continue
            total = len(fdf)
            closed = fdf[fdf['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            targets = len(fdf[fdf['outcome'] == 'TARGET_HIT'])
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            pnl_per_trade = total_pnl / len(closed) if len(closed) > 0 else 0
            print(f"{name:<35} {total:>10} {win_rate:>9.1f}% {total_pnl:>11.2f}% {pnl_per_trade:>11.2f}%")

    # ============================
    # PHASE 6: ST Signal Filters
    # ============================
    print("\n" + "=" * 120)
    print("PHASE 6: FILTER COMPARISON - ST SIGNALS")
    print("=" * 120)

    if not st_df.empty:
        filters = [
            ('No Filter', st_df),
            ('CE Only', st_df[st_df['opt_type'] == 'CE']),
            ('PE Only', st_df[st_df['opt_type'] == 'PE']),
            ('BEARISH regime', st_df[st_df['regime'] == 'BEARISH']),
            ('BULLISH regime', st_df[st_df['regime'] == 'BULLISH']),
            ('BEARISH + PE', st_df[(st_df['regime'] == 'BEARISH') & (st_df['opt_type'] == 'PE')]),
            ('BULLISH + CE', st_df[(st_df['regime'] == 'BULLISH') & (st_df['opt_type'] == 'CE')]),
            ('3+ underlying UP', st_df[st_df['underlying_up_count'] >= 3]),
            ('3+ underlying DOWN', st_df[st_df['underlying_down_count'] >= 3]),
            ('All underlying UP (4)', st_df[st_df['underlying_up_count'] == 4]),
        ]

        print(f"\n{'Filter':<35} {'Signals':>10} {'WinRate':>10} {'P&L':>12} {'P&L/Trade':>12}")
        print("-" * 85)

        for name, fdf in filters:
            if fdf.empty:
                print(f"{name:<35} {'0':>10} {'-':>10} {'-':>12} {'-':>12}")
                continue
            total = len(fdf)
            closed = fdf[fdf['outcome'].isin(['TARGET_HIT', 'SL_HIT'])]
            targets = len(fdf[fdf['outcome'] == 'TARGET_HIT'])
            win_rate = (targets / len(closed) * 100) if len(closed) > 0 else 0
            total_pnl = closed['pnl_pct'].sum() if len(closed) > 0 else 0
            pnl_per_trade = total_pnl / len(closed) if len(closed) > 0 else 0
            print(f"{name:<35} {total:>10} {win_rate:>9.1f}% {total_pnl:>11.2f}% {pnl_per_trade:>11.2f}%")

    print("\n" + "=" * 120)
    print("BACKTEST COMPLETE")
    print("=" * 120)


if __name__ == "__main__":
    main()
