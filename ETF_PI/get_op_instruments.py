"""
Fetch option (and MCX futures) instruments for the option magnet.
Downloads the Kite instrument dump ONCE and writes:
  op_instruments.csv   - index options (NFO/BFO) + MCX options + MCX futures (FUT rows are the
                         ATM reference for MCX: an MCX option settles into a futures contract)
  mcx_instruments.csv  - MCX futures only (kept for backward compatibility)
Run daily before market opens (Pi cron 08:52).

2026-10-06 (spec SPEC_multi_instrument_2026-10-06 step 3):
  * one download instead of two (the MCX futures pass used to re-download the whole dump)
  * MCX names extended to the mini contracts the bot signals on (CRUDEOILM, NATGASMINI, GOLDM,
    SILVERM) and NATURALGAS, which the old filter silently dropped
  * a partial download can no longer overwrite a good file: NIFTY missing -> keep yesterday's
    file; MCX fetch failed -> carry yesterday's still-valid MCX rows forward. Either case exits
    non-zero so the cron log and preflight see it instead of every MCX process failing quietly.
"""
import pandas as pd
import os
import sys
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

# All indices to fetch (other scripts still read FINNIFTY/MIDCPNIFTY/SENSEX rows)
ALLOWED_INDICES = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']

# MCX commodities. Minis are the signal chains; CRUDEOIL/NATURALGAS full chains are quoted by the
# liquidity probe; GOLD/SILVER kept for other readers.
ALLOWED_MCX = ['CRUDEOILM', 'NATGASMINI', 'GOLDM', 'SILVERM',
               'CRUDEOIL', 'NATURALGAS', 'GOLD', 'SILVER']

# Names the bot cannot run without. Missing NFO -> the file is not written at all.
REQUIRED_NFO = ['NIFTY', 'BANKNIFTY']
REQUIRED_MCX = ['CRUDEOILM', 'NATGASMINI', 'GOLDM', 'SILVERM']

OP_PATH = os.path.join(CURRENT_DIR, 'op_instruments.csv')
MCX_FUT_PATH = os.path.join(CURRENT_DIR, 'mcx_instruments.csv')

pd.set_option('display.max_rows', 5000)
pd.set_option('display.max_columns', 500)
pd.set_option('display.width', 1000)


def fetch_instruments_from_api():
    """Fetch instruments using Kite Connect API. Returns (df, mcx_ok) or (None, False)."""
    try:
        from kiteconnect import KiteConnect

        with open(TOKEN_PATH, 'r') as f:
            token_data = json.load(f)

        api_key = token_data.get('api_key')
        access_token = token_data.get('access_token')

        if not api_key or not access_token:
            print("Invalid token file, falling back to public URL")
            return None, False

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        print("Fetching NFO instruments...")
        nfo_instruments = kite.instruments("NFO")

        print("Fetching BFO instruments...")
        try:
            bfo_instruments = kite.instruments("BFO")
        except Exception as e:
            bfo_instruments = []
            print(f"BFO fetch failed ({e}), continuing with NFO only")

        print("Fetching MCX instruments...")
        mcx_ok = True
        try:
            mcx_instruments = kite.instruments("MCX")
        except Exception as e:
            mcx_instruments = []
            mcx_ok = False
            print(f"ERROR: MCX fetch failed ({e})")

        df = pd.DataFrame(nfo_instruments + bfo_instruments + mcx_instruments)
        print(f"Fetched {len(df)} instruments via Kite API")
        return df, mcx_ok

    except Exception as e:
        print(f"Kite API fetch failed: {e}")
        return None, False


def fetch_instruments_from_url():
    """Fallback: Fetch instruments from public Kite URL (all exchanges in one dump)."""
    print("Fetching instruments from public URL...")
    url = 'https://api.kite.trade/instruments'
    df = pd.read_csv(url)
    print(f"Fetched {len(df)} instruments from URL")
    return df, True


def select_rows(df, today):
    """Option rows for the allowed names + MCX futures rows, unexpired, sorted."""
    nfo_bfo = df[(df['segment'].isin(['NFO-OPT', 'BFO-OPT'])) & (df['name'].isin(ALLOWED_INDICES))]
    mcx_opt = df[(df['segment'] == 'MCX-OPT') & (df['name'].isin(ALLOWED_MCX))]
    mcx_fut = df[(df['segment'] == 'MCX-FUT') & (df['name'].isin(ALLOWED_MCX))]
    out = pd.concat([nfo_bfo, mcx_opt, mcx_fut], ignore_index=True).copy()
    out['expiry'] = pd.to_datetime(out['expiry'])
    out = out[out['expiry'] >= today]
    return out.sort_values(['name', 'expiry', 'strike']).reset_index(drop=True)


def carry_forward_mcx(today):
    """Yesterday's MCX rows that have not expired, used when today's MCX fetch failed."""
    if not os.path.exists(OP_PATH):
        return pd.DataFrame()
    old = pd.read_csv(OP_PATH)
    old['expiry'] = pd.to_datetime(old['expiry'])
    return old[(old['exchange'] == 'MCX') & (old['expiry'] >= today)]


def sanity(df):
    """Per-name counts. Returns (missing_nfo, missing_mcx)."""
    print("\nInstruments per underlying (options / futures):")
    for name in ALLOWED_INDICES + ALLOWED_MCX:
        sub = df[df['name'] == name]
        if sub.empty:
            continue
        n_opt = int(sub['instrument_type'].isin(['CE', 'PE']).sum())
        n_fut = int((sub['instrument_type'] == 'FUT').sum())
        exps = sorted(sub[sub['instrument_type'].isin(['CE', 'PE'])]['expiry'].unique())[:3]
        print(f"  {name:11s} {n_opt:5d} / {n_fut:2d}   next expiries: "
              f"{', '.join(pd.Timestamp(e).strftime('%d-%b-%Y') for e in exps)}")
    opts = df[df['instrument_type'].isin(['CE', 'PE'])]
    futs = df[df['instrument_type'] == 'FUT']
    missing_nfo = [n for n in REQUIRED_NFO if opts[opts['name'] == n].empty]
    missing_mcx = [n for n in REQUIRED_MCX if opts[opts['name'] == n].empty or futs[futs['name'] == n].empty]
    return missing_nfo, missing_mcx


def write_csv(df, path):
    tmp = path + '.tmp'
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def main():
    print("=" * 50)
    print(f"Instrument Fetcher - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 50)

    df, mcx_ok = fetch_instruments_from_api()
    if df is None or df.empty:
        df, mcx_ok = fetch_instruments_from_url()
    if df is None or df.empty:
        print("ERROR: No instruments fetched! Keeping the existing files.")
        return 2

    today = pd.Timestamp.now().floor('D')
    out = select_rows(df.reset_index(drop=True), today)

    if not mcx_ok:
        carried = carry_forward_mcx(today)
        print(f"ERROR: MCX not fetched today - carrying forward {len(carried)} unexpired MCX rows")
        out = pd.concat([out[out['exchange'] != 'MCX'], carried], ignore_index=True)

    missing_nfo, missing_mcx = sanity(out)
    if missing_nfo:
        print(f"ERROR: required NFO names missing {missing_nfo} - NOT overwriting {OP_PATH}")
        return 2

    write_csv(out, OP_PATH)
    print(f"\nSaved {len(out)} instruments to {OP_PATH}")

    futs = out[(out['exchange'] == 'MCX') & (out['instrument_type'] == 'FUT')]
    if not futs.empty:
        write_csv(futs.sort_values(['name', 'expiry']).reset_index(drop=True), MCX_FUT_PATH)
        print(f"Saved {len(futs)} MCX futures to {MCX_FUT_PATH}")

    if missing_mcx or not mcx_ok:
        print(f"ERROR: MCX incomplete (missing {missing_mcx}, fetched today={mcx_ok})")
        return 3
    print("\n" + "=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
