"""Quick verification script for IRFC weekly SuperTrend signal"""
import sys
sys.path.insert(0, '.')

from kiteconnect import KiteConnect
import pandas as pd
import json
from datetime import datetime, timedelta

# Load token
with open('../data/kite_access_token.json', 'r') as f:
    token_data = json.load(f)

kite = KiteConnect(api_key=token_data['api_key'])
kite.set_access_token(token_data['access_token'])

# Get IRFC instrument token
instruments = kite.instruments("NSE")
irfc_token = None
for inst in instruments:
    if inst['tradingsymbol'] == 'IRFC':
        irfc_token = inst['instrument_token']
        print(f"IRFC instrument token: {irfc_token}")
        break

if not irfc_token:
    print("IRFC not found!")
    sys.exit(1)

# Fetch daily data and resample to weekly
from_date = datetime.now() - timedelta(days=550)  # ~1.5 years
to_date = datetime.now()

print(f"\nFetching daily data from {from_date.date()} to {to_date.date()}...")
daily_data = kite.historical_data(irfc_token, from_date, to_date, "day")

df = pd.DataFrame(daily_data)
df['date'] = pd.to_datetime(df['date'])
df.set_index('date', inplace=True)

# Resample to weekly (Monday start)
weekly = df.resample('W-MON', label='left', closed='left').agg({
    'open': 'first',
    'high': 'max',
    'low': 'min',
    'close': 'last',
    'volume': 'sum'
}).dropna()

# Rename columns to match SuperTrend calculation
weekly.columns = ['Open', 'High', 'Low', 'Close', 'Volume']

print(f"\nWeekly candles: {len(weekly)}")
print("\n=== Last 5 Weekly Candles ===")
print(weekly.tail(5).to_string())

# Calculate SuperTrend
period = 10
multiplier = 3

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()

    # ATR calculation
    df['prev_close'] = df['Close'].shift(1)
    df['tr1'] = df['High'] - df['Low']
    df['tr2'] = abs(df['High'] - df['prev_close'])
    df['tr3'] = abs(df['Low'] - df['prev_close'])
    df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1/period, adjust=False).mean()

    # Basic bands
    hl2 = (df['High'] + df['Low']) / 2
    df['basic_ub'] = hl2 + (multiplier * df['atr'])
    df['basic_lb'] = hl2 - (multiplier * df['atr'])

    # Final bands
    df['final_ub'] = 0.0
    df['final_lb'] = 0.0
    df['supertrend'] = 0.0
    df['trend'] = 0

    for i in range(len(df)):
        if i == 0:
            df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i]['basic_ub']
            df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i]['basic_lb']
        else:
            # Final Upper Band
            if df.iloc[i]['basic_ub'] < df.iloc[i-1]['final_ub'] or df.iloc[i-1]['Close'] > df.iloc[i-1]['final_ub']:
                df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i]['basic_ub']
            else:
                df.iloc[i, df.columns.get_loc('final_ub')] = df.iloc[i-1]['final_ub']

            # Final Lower Band
            if df.iloc[i]['basic_lb'] > df.iloc[i-1]['final_lb'] or df.iloc[i-1]['Close'] < df.iloc[i-1]['final_lb']:
                df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i]['basic_lb']
            else:
                df.iloc[i, df.columns.get_loc('final_lb')] = df.iloc[i-1]['final_lb']

    for i in range(len(df)):
        if i == 0:
            df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_ub']
            df.iloc[i, df.columns.get_loc('trend')] = 1  # DOWN
        else:
            if df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_ub'] and df.iloc[i]['Close'] <= df.iloc[i]['final_ub']:
                df.iloc[i, df.columns.get_loc('trend')] = 1  # DOWN
            elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_ub'] and df.iloc[i]['Close'] > df.iloc[i]['final_ub']:
                df.iloc[i, df.columns.get_loc('trend')] = -1  # UP
            elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_lb'] and df.iloc[i]['Close'] >= df.iloc[i]['final_lb']:
                df.iloc[i, df.columns.get_loc('trend')] = -1  # UP
            elif df.iloc[i-1]['supertrend'] == df.iloc[i-1]['final_lb'] and df.iloc[i]['Close'] < df.iloc[i]['final_lb']:
                df.iloc[i, df.columns.get_loc('trend')] = 1  # DOWN
            else:
                df.iloc[i, df.columns.get_loc('trend')] = df.iloc[i-1]['trend']

            if df.iloc[i]['trend'] == 1:  # DOWN
                df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_ub']
            else:  # UP
                df.iloc[i, df.columns.get_loc('supertrend')] = df.iloc[i]['final_lb']

    return df

df_st = calculate_supertrend(weekly, period, multiplier)

print("\n=== SuperTrend Analysis (Last 15 Candles) ===")
display_cols = ['Open', 'High', 'Low', 'Close', 'supertrend', 'trend']
last_15 = df_st[display_cols].tail(15).copy()
last_15['trend_dir'] = last_15['trend'].apply(lambda x: 'UP' if x == -1 else 'DOWN')
last_15['LOW vs ST'] = last_15['Low'] - last_15['supertrend']
print(last_15.to_string())

# Current week analysis
print("\n" + "="*60)
print("=== IRFC SIGNAL VERIFICATION ===")
print("="*60)

current = df_st.iloc[-1]
prev = df_st.iloc[-2]

print(f"\nCURRENT WEEK (incomplete):")
print(f"  Date: {df_st.index[-1]}")
print(f"  OHLC: O={current['Open']:.2f}, H={current['High']:.2f}, L={current['Low']:.2f}, C={current['Close']:.2f}")
print(f"  SuperTrend: {current['supertrend']:.2f}")
print(f"  Trend: {'UP' if current['trend'] == -1 else 'DOWN'}")

print(f"\nPREVIOUS WEEK (completed - used for signal):")
print(f"  Date: {df_st.index[-2]}")
print(f"  OHLC: O={prev['Open']:.2f}, H={prev['High']:.2f}, L={prev['Low']:.2f}, C={prev['Close']:.2f}")
print(f"  SuperTrend: {prev['supertrend']:.2f}")
print(f"  Trend: {'UP' if prev['trend'] == -1 else 'DOWN'}")

# Fresh touch check (last 10 candles before current)
print("\n=== FRESH TOUCH CHECK (10 candle lookback) ===")
lookback = 10
touch_found = False
for i in range(1, lookback + 1):
    idx = len(df_st) - 1 - i
    if idx < 0:
        break
    candle = df_st.iloc[idx]
    low = candle['Low']
    st = candle['supertrend']
    is_touch = low <= st
    print(f"  Candle {i} back: LOW={low:.2f}, ST={st:.2f}, Touch={'YES' if is_touch else 'NO'}")
    if is_touch:
        touch_found = True
        break

if not touch_found:
    print("  ✅ FRESH TOUCH - No touches in previous 10 candles")
else:
    print("  ❌ NOT FRESH - Previous touch found")

# LTP check
print("\n=== ORDER PLACEMENT ANALYSIS ===")
ltp = kite.ltp(f"NSE:IRFC")['NSE:IRFC']['last_price']
print(f"Current LTP: Rs.{ltp:.2f}")
print(f"SuperTrend (order price): Rs.{prev['supertrend']:.2f}")
print(f"Price gap: {((ltp / prev['supertrend']) - 1) * 100:.1f}%")

# Circuit limits
try:
    quote = kite.quote(f"NSE:IRFC")['NSE:IRFC']
    lower_circuit = quote.get('lower_circuit_limit')
    upper_circuit = quote.get('upper_circuit_limit')
    print(f"\nCircuit Limits:")
    print(f"  Lower: Rs.{lower_circuit}")
    print(f"  Upper: Rs.{upper_circuit}")
    print(f"\nOrder at Rs.{prev['supertrend']:.2f} vs Lower Circuit Rs.{lower_circuit}")
    if prev['supertrend'] < lower_circuit:
        print("  ❌ ORDER REJECTED: Limit price below lower circuit!")
    else:
        print("  ✅ Order would be accepted")
except Exception as e:
    print(f"Could not get circuit limits: {e}")

print("\n" + "="*60)
print("=== CONCLUSION ===")
print("="*60)
ref_trend = prev['trend']
if ref_trend == -1:
    print("✅ Signal QUALIFIED: Trend is UP (bullish)")
else:
    print("❌ Signal did NOT qualify: Trend is DOWN (bearish)")
