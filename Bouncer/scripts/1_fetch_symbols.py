#!/usr/bin/env python3
"""
Step 1: Fetch Stock Symbols (8:30 AM)
=====================================
Downloads instrument data from Kite (public API - no token needed).
Saves to BOTS/data/stock_instruments.csv

Run: python scripts/1_fetch_symbols.py
Cron: 30 8 * * 1-5
"""

import csv
import requests
from io import StringIO
from pathlib import Path
from datetime import datetime
from collections import defaultdict

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
DATA_DIR = BOUNCER_DIR.parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Output files
STOCK_INSTRUMENTS_FILE = DATA_DIR / 'stock_instruments.csv'
INDEX_OPTIONS_FILE = DATA_DIR / 'index_options.csv'

INDEX_NAMES = ['NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY']


def fetch_instruments():
    """Fetch all instruments from Kite API (public, no auth needed)."""
    print("Fetching instruments from Kite API...")
    response = requests.get(KITE_INSTRUMENTS_URL, timeout=60)
    response.raise_for_status()
    return response.text


def parse_instruments(csv_data):
    """Parse CSV data."""
    reader = csv.DictReader(StringIO(csv_data))
    return list(reader)


def process_stock_options(instruments):
    """Extract NSE stocks and their F&O options."""
    nse_stocks = {}
    for inst in instruments:
        if inst['exchange'] == 'NSE' and inst['segment'] == 'NSE':
            symbol = inst['tradingsymbol']
            nse_stocks[symbol] = {
                'instrument_token': inst['instrument_token'],
                'name': inst['name'],
                'lot_size': inst.get('lot_size', '1'),
            }

    stock_options = defaultdict(list)
    for inst in instruments:
        if inst['exchange'] == 'NFO' and inst['instrument_type'] in ('CE', 'PE'):
            underlying = inst['name']
            if underlying in nse_stocks:
                stock_options[underlying].append({
                    'instrument_token': inst['instrument_token'],
                    'tradingsymbol': inst['tradingsymbol'],
                    'instrument_type': inst['instrument_type'],
                    'strike': inst['strike'],
                    'expiry': inst['expiry'],
                    'lot_size': inst.get('lot_size', ''),
                })

    return nse_stocks, stock_options


def process_index_options(instruments):
    """Extract index options."""
    index_options = defaultdict(list)
    for inst in instruments:
        segment = inst.get('segment', '')
        name = inst.get('name', '')
        inst_type = inst.get('instrument_type', '')

        if inst_type in ('CE', 'PE') and segment in ('NFO-OPT', 'BFO-OPT') and name in INDEX_NAMES:
            index_options[name].append({
                'instrument_token': inst['instrument_token'],
                'tradingsymbol': inst['tradingsymbol'],
                'instrument_type': inst_type,
                'strike': inst['strike'],
                'expiry': inst['expiry'],
                'lot_size': inst.get('lot_size', ''),
                'exchange': inst.get('exchange', ''),
            })

    return index_options


def write_stock_instruments(nse_stocks, stock_options):
    """Write stock_instruments.csv."""
    print(f"Writing {STOCK_INSTRUMENTS_FILE.name}...")
    today = datetime.now().date()

    with open(STOCK_INSTRUMENTS_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'symbol', 'name', 'stock_token', 'lot_size',
            'option_symbol', 'option_type', 'strike', 'expiry', 'option_token', 'dte'
        ])
        writer.writeheader()

        for symbol in sorted(nse_stocks.keys()):
            stock = nse_stocks[symbol]
            options = stock_options.get(symbol, [])

            if options:
                for opt in sorted(options, key=lambda x: (x['expiry'], float(x['strike']))):
                    try:
                        exp_date = datetime.strptime(opt['expiry'], '%Y-%m-%d').date()
                        dte = (exp_date - today).days
                    except (ValueError, TypeError):
                        dte = 0

                    writer.writerow({
                        'symbol': symbol,
                        'name': stock['name'],
                        'stock_token': stock['instrument_token'],
                        'lot_size': opt['lot_size'] or stock['lot_size'],
                        'option_symbol': opt['tradingsymbol'],
                        'option_type': opt['instrument_type'],
                        'strike': opt['strike'],
                        'expiry': opt['expiry'],
                        'option_token': opt['instrument_token'],
                        'dte': dte
                    })


def write_index_options(index_options):
    """Write index_options.csv."""
    print(f"Writing {INDEX_OPTIONS_FILE.name}...")
    today = datetime.now().date()

    with open(INDEX_OPTIONS_FILE, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=[
            'index', 'option_symbol', 'option_type', 'strike', 'expiry',
            'option_token', 'lot_size', 'exchange', 'dte'
        ])
        writer.writeheader()

        for index_name in sorted(index_options.keys()):
            for opt in sorted(index_options[index_name], key=lambda x: (x['expiry'], float(x['strike']))):
                try:
                    exp_date = datetime.strptime(opt['expiry'], '%Y-%m-%d').date()
                    dte = (exp_date - today).days
                except (ValueError, TypeError):
                    dte = 0

                writer.writerow({
                    'index': index_name,
                    'option_symbol': opt['tradingsymbol'],
                    'option_type': opt['instrument_type'],
                    'strike': opt['strike'],
                    'expiry': opt['expiry'],
                    'option_token': opt['instrument_token'],
                    'lot_size': opt['lot_size'],
                    'exchange': opt['exchange'],
                    'dte': dte
                })


def main():
    print("=" * 60)
    print("STEP 1: FETCH SYMBOLS")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Output: {DATA_DIR}")
    print()

    try:
        # Fetch from Kite (public API)
        csv_data = fetch_instruments()
        instruments = parse_instruments(csv_data)
        print(f"Total instruments: {len(instruments)}")

        # Process stocks
        nse_stocks, stock_options = process_stock_options(instruments)
        stocks_with_fo = sum(1 for s in nse_stocks if s in stock_options)
        print(f"NSE stocks: {len(nse_stocks)}, with F&O: {stocks_with_fo}")

        # Process indices
        index_options = process_index_options(instruments)
        print(f"Index options: {sum(len(v) for v in index_options.values())}")

        # Write files
        write_stock_instruments(nse_stocks, stock_options)
        write_index_options(index_options)

        print()
        print("DONE - Files saved:")
        print(f"  {STOCK_INSTRUMENTS_FILE}")
        print(f"  {INDEX_OPTIONS_FILE}")

    except Exception as e:
        print(f"ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
