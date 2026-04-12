"""
Fetch Option Instruments for all indices
Downloads instruments from Kite API and filters for index options
Run daily before market opens (e.g., 8:30 AM)
"""
import pandas as pd
import os
import json
import platform
from datetime import datetime

# Get the current directory
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))

# Token path for Kite Connect
if platform.system() == 'Windows':
    TOKEN_PATH = r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
else:
    TOKEN_PATH = '/home/trustit/Desktop/BOTS/data/kite_access_token.json'

# All indices to fetch (must match option_buy.py)
ALLOWED_INDICES = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']

# MCX commodities for options
ALLOWED_MCX = ['GOLD', 'SILVER', 'CRUDEOIL']

pd.set_option('display.max_rows', 5000)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


def fetch_instruments_from_api():
    """Fetch instruments using Kite Connect API"""
    try:
        from kiteconnect import KiteConnect

        with open(TOKEN_PATH, 'r') as f:
            token_data = json.load(f)

        api_key = token_data.get('api_key')
        access_token = token_data.get('access_token')

        if not api_key or not access_token:
            print("Invalid token file, falling back to public URL")
            return None

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # Fetch NFO (NSE F&O) and BFO (BSE F&O) instruments
        print("Fetching NFO instruments...")
        nfo_instruments = kite.instruments("NFO")

        print("Fetching BFO instruments...")
        try:
            bfo_instruments = kite.instruments("BFO")
        except:
            bfo_instruments = []
            print("BFO fetch failed, continuing with NFO only")

        print("Fetching MCX instruments...")
        try:
            mcx_instruments = kite.instruments("MCX")
        except:
            mcx_instruments = []
            print("MCX fetch failed, continuing without MCX")

        # Combine into DataFrame
        all_instruments = nfo_instruments + bfo_instruments + mcx_instruments
        df = pd.DataFrame(all_instruments)

        print(f"Fetched {len(df)} instruments via Kite API")
        return df

    except Exception as e:
        print(f"Kite API fetch failed: {e}")
        return None


def fetch_instruments_from_url():
    """Fallback: Fetch instruments from public Kite URL"""
    print("Fetching instruments from public URL...")
    url = 'https://api.kite.trade/instruments'
    df = pd.read_csv(url)
    print(f"Fetched {len(df)} instruments from URL")
    return df


def process_option_instruments():
    """Main function to fetch and process option instruments"""

    # Try Kite API first, fallback to URL
    df = fetch_instruments_from_api()
    if df is None or df.empty:
        df = fetch_instruments_from_url()

    df = df.reset_index(drop=True)

    if len(df) == 0:
        print("ERROR: No instruments fetched!")
        return

    # Filter for options: NFO-OPT (NSE), BFO-OPT (BSE), MCX-OPT (commodities)
    df_nfo_bfo = df[
        (df['segment'].isin(['NFO-OPT', 'BFO-OPT'])) &
        (df['name'].isin(ALLOWED_INDICES))
    ].copy()

    df_mcx_opt = df[
        (df['segment'] == 'MCX-OPT') &
        (df['name'].isin(ALLOWED_MCX))
    ].copy()

    df_options = pd.concat([df_nfo_bfo, df_mcx_opt], ignore_index=True)

    print(f"\nFiltered to {len(df_options)} option instruments")

    # Show counts per index/commodity
    print("\nInstruments per underlying:")
    for idx in ALLOWED_INDICES + ALLOWED_MCX:
        count = len(df_options[df_options['name'] == idx])
        if count > 0:
            print(f"  {idx}: {count}")

    # Convert expiry to datetime
    df_options['expiry'] = pd.to_datetime(df_options['expiry'])

    # Get current date
    current_date = pd.Timestamp.now().floor('D')

    # Filter for future expiries only
    df_options = df_options[df_options['expiry'] >= current_date]

    print(f"\nAfter filtering future expiries: {len(df_options)} instruments")

    # Sort by name, expiry, strike
    df_options = df_options.sort_values(['name', 'expiry', 'strike'])

    # Reset index and save to CSV
    df_options = df_options.reset_index(drop=True)

    output_path = os.path.join(CURRENT_DIR, 'op_instruments.csv')
    df_options.to_csv(output_path, index=False)
    print(f"\nSaved {len(df_options)} instruments to {output_path}")

    # Show upcoming expiries per underlying
    print("\nUpcoming expiries:")
    for idx in ALLOWED_INDICES + ALLOWED_MCX:
        idx_df = df_options[df_options['name'] == idx]
        if not idx_df.empty:
            expiries = sorted(idx_df['expiry'].unique())[:3]
            expiry_strs = [e.strftime('%d-%b-%Y') for e in expiries]
            print(f"  {idx}: {', '.join(expiry_strs)}")


def process_mcx_instruments():
    """Fetch MCX futures for CRUDEOIL, GOLD, SILVER commodity trading"""
    print("\n" + "="*50)
    print("Fetching MCX futures instruments...")

    # Use Kite API first (same source as options), fallback to URL
    df = fetch_instruments_from_api()
    if df is None or df.empty:
        df = fetch_instruments_from_url()

    # Filter for CRUDEOIL, GOLD, SILVER futures
    df_mcx = df[
        (df['segment'] == 'MCX-FUT') &
        (df['name'].isin(['CRUDEOIL', 'GOLD', 'SILVER']))
    ].copy()

    if len(df_mcx) == 0:
        print("No MCX futures found")
        return

    # Sort by name then expiry
    df_mcx = df_mcx.sort_values(by=['name', 'expiry'], ascending=True)
    df_mcx = df_mcx.reset_index(drop=True)

    output_path = os.path.join(CURRENT_DIR, 'mcx_instruments.csv')
    df_mcx.to_csv(output_path, index=False)
    print(f"Saved {len(df_mcx)} MCX futures instruments to {output_path}")

    # Show counts per commodity
    for name in ['CRUDEOIL', 'GOLD', 'SILVER']:
        count = len(df_mcx[df_mcx['name'] == name])
        if count > 0:
            nearest = df_mcx[df_mcx['name'] == name].iloc[0]['tradingsymbol']
            print(f"  {name}: {count} contracts (nearest: {nearest})")


if __name__ == "__main__":
    print("="*50)
    print(f"Instrument Fetcher - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*50)

    process_option_instruments()
    process_mcx_instruments()

    print("\n" + "="*50)
    print("Done!")
    print("="*50)
