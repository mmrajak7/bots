#!/usr/bin/env python3
"""
Fetch NSE/NFO instruments and save to BOTS/data folder.
This script should run daily at market open (before 9:15 AM).
All bots (Bouncer, SNAIL, etc.) read from BOTS/data.
"""

import csv
import requests
from io import StringIO
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Output directory - BOTS/data (shared across all bots)
SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR.parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Output files
STOCK_INSTRUMENTS_FILE = DATA_DIR / 'stock_instruments.csv'
INDEX_OPTIONS_FILE = DATA_DIR / 'index_options.csv'
STOCK_OPTIONS_SUMMARY_FILE = DATA_DIR / 'stock_options_summary.csv'

INDEX_NAMES = ['NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY']


def fetch_instruments():
    """Fetch all instruments from Kite API."""
    print("Fetching instruments from Kite API...")
    response = requests.get(KITE_INSTRUMENTS_URL, timeout=60)
    response.raise_for_status()
    return response.text


def parse_instruments(csv_data):
    """Parse the CSV data from Kite API."""
    reader = csv.DictReader(StringIO(csv_data))
    return list(reader)


def filter_nse_stocks_and_options(instruments):
    """Filter NSE stocks and their NFO options."""
    nse_stocks = {}
    for inst in instruments:
        if inst['exchange'] == 'NSE' and inst['segment'] == 'NSE':
            symbol = inst['tradingsymbol']
            nse_stocks[symbol] = {
                'instrument_token': inst['instrument_token'],
                'name': inst['name'],
                'tradingsymbol': symbol,
                'lot_size': inst.get('lot_size', '1'),
                'tick_size': inst.get('tick_size', ''),
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


def filter_index_options(instruments):
    """Filter index options (NIFTY, SENSEX, BANKNIFTY, FINNIFTY)."""
    index_options = defaultdict(list)

    for inst in instruments:
        segment = inst.get('segment', '')
        name = inst.get('name', '')
        inst_type = inst.get('instrument_type', '')

        if inst_type not in ('CE', 'PE'):
            continue

        if segment in ('NFO-OPT', 'BFO-OPT') and name in INDEX_NAMES:
            index_options[name].append({
                'instrument_token': inst['instrument_token'],
                'tradingsymbol': inst['tradingsymbol'],
                'instrument_type': inst_type,
                'strike': inst['strike'],
                'expiry': inst['expiry'],
                'lot_size': inst.get('lot_size', ''),
                'tick_size': inst.get('tick_size', ''),
                'segment': segment,
                'exchange': inst.get('exchange', ''),
            })

    return index_options


def write_instruments_csv(nse_stocks, stock_options):
    """Write stock_instruments.csv (one row per option)."""
    print(f"Writing to {STOCK_INSTRUMENTS_FILE}...")

    today = datetime.now().date()

    with open(STOCK_INSTRUMENTS_FILE, 'w', newline='') as csvfile:
        fieldnames = [
            'stock_symbol', 'stock_name', 'stock_instrument_token', 'lot_size',
            'has_options', 'tradingsymbol', 'option_type', 'strike', 'expiry',
            'option_instrument_token', 'dte'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for symbol, stock_info in sorted(nse_stocks.items()):
            options = stock_options.get(symbol, [])

            if options:
                for opt in sorted(options, key=lambda x: (x['expiry'], float(x['strike']), x['instrument_type'])):
                    try:
                        expiry_date = datetime.strptime(opt['expiry'], '%Y-%m-%d').date()
                        dte = (expiry_date - today).days
                    except:
                        dte = 0

                    writer.writerow({
                        'stock_symbol': symbol,
                        'stock_name': stock_info['name'],
                        'stock_instrument_token': stock_info['instrument_token'],
                        'lot_size': opt['lot_size'] or stock_info['lot_size'],
                        'has_options': 'Yes',
                        'tradingsymbol': opt['tradingsymbol'],
                        'option_type': opt['instrument_type'],
                        'strike': opt['strike'],
                        'expiry': opt['expiry'],
                        'option_instrument_token': opt['instrument_token'],
                        'dte': dte
                    })

    print(f"  Written {INSTRUMENTS_FILE.name}")


def write_index_options_csv(index_options):
    """Write index_options.csv."""
    print(f"Writing to {INDEX_OPTIONS_FILE}...")

    today = datetime.now().date()

    with open(INDEX_OPTIONS_FILE, 'w', newline='') as csvfile:
        fieldnames = [
            'index_name', 'tradingsymbol', 'option_type', 'strike', 'expiry',
            'instrument_token', 'lot_size', 'segment', 'exchange', 'dte'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for index_name in sorted(index_options.keys()):
            options = index_options[index_name]

            for opt in sorted(options, key=lambda x: (x['expiry'], float(x['strike']), x['instrument_type'])):
                try:
                    expiry_date = datetime.strptime(opt['expiry'], '%Y-%m-%d').date()
                    dte = (expiry_date - today).days
                except:
                    dte = 0

                writer.writerow({
                    'index_name': index_name,
                    'tradingsymbol': opt['tradingsymbol'],
                    'option_type': opt['instrument_type'],
                    'strike': opt['strike'],
                    'expiry': opt['expiry'],
                    'instrument_token': opt['instrument_token'],
                    'lot_size': opt['lot_size'],
                    'segment': opt['segment'],
                    'exchange': opt['exchange'],
                    'dte': dte
                })

    print(f"  Written {INDEX_OPTIONS_FILE.name}")


def write_summary_csv(nse_stocks, stock_options):
    """Write stock_options_summary.csv (one row per stock)."""
    print(f"Writing to {STOCK_OPTIONS_SUMMARY_FILE}...")

    with open(STOCK_OPTIONS_SUMMARY_FILE, 'w', newline='') as csvfile:
        fieldnames = [
            'stock_symbol', 'stock_name', 'instrument_token', 'lot_size',
            'has_options', 'options_count', 'ce_count', 'pe_count',
            'earliest_expiry', 'latest_expiry'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for symbol, stock_info in sorted(nse_stocks.items()):
            options = stock_options.get(symbol, [])
            ce_count = sum(1 for o in options if o['instrument_type'] == 'CE')
            pe_count = sum(1 for o in options if o['instrument_type'] == 'PE')
            expiries = [o['expiry'] for o in options]

            writer.writerow({
                'stock_symbol': symbol,
                'stock_name': stock_info['name'],
                'instrument_token': stock_info['instrument_token'],
                'lot_size': options[0]['lot_size'] if options else stock_info['lot_size'],
                'has_options': 'Yes' if options else 'No',
                'options_count': len(options),
                'ce_count': ce_count,
                'pe_count': pe_count,
                'earliest_expiry': min(expiries) if expiries else '',
                'latest_expiry': max(expiries) if expiries else ''
            })

    print(f"  Written {STOCK_OPTIONS_SUMMARY_FILE.name}")


def main():
    print("="*60)
    print("INSTRUMENT DATA FETCHER")
    print("="*60)
    print(f"Output directory: {DATA_DIR}")
    print()

    try:
        csv_data = fetch_instruments()
        instruments = parse_instruments(csv_data)
        print(f"Total instruments: {len(instruments)}")
        print()

        # Stock options
        print("Processing stock options...")
        nse_stocks, stock_options = filter_nse_stocks_and_options(instruments)
        stocks_with_options = sum(1 for s in nse_stocks if s in stock_options)
        total_options = sum(len(opts) for opts in stock_options.values())
        print(f"  NSE stocks: {len(nse_stocks)}")
        print(f"  Stocks with F&O: {stocks_with_options}")
        print(f"  Total options: {total_options}")
        print()

        # Index options
        print("Processing index options...")
        index_options = filter_index_options(instruments)
        for idx in sorted(index_options.keys()):
            print(f"  {idx}: {len(index_options[idx])} options")
        print()

        # Write files
        print("Writing output files...")
        write_instruments_csv(nse_stocks, stock_options)
        write_index_options_csv(index_options)
        write_summary_csv(nse_stocks, stock_options)

        print()
        print("="*60)
        print("DONE")
        print("="*60)
        print(f"Files saved to: {DATA_DIR}")
        print(f"  - instruments.csv")
        print(f"  - index_options.csv")
        print(f"  - stock_options_summary.csv")

    except Exception as e:
        print(f"ERROR: {e}")
        raise


if __name__ == "__main__":
    main()
