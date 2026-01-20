#!/usr/bin/env python3
"""
Script to fetch NSE stocks and their corresponding options instruments from Kite API.
Also fetches index options (NIFTY, SENSEX) to separate CSV files.

Outputs:
  - nse_stocks_options.csv: Detailed stock options (one row per option)
  - nse_stocks_options_summary.csv: Summary (one row per stock)
  - index_options.csv: Index options (NIFTY from NFO, SENSEX from BFO)
"""

import csv
import requests
from io import StringIO
from collections import defaultdict
from datetime import datetime


KITE_INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Index names to fetch options for
INDEX_NAMES = ['NIFTY', 'SENSEX']

# Index to spot instrument mapping for LTP lookup
INDEX_SPOT_SYMBOLS = {
    'NIFTY': 'NSE:NIFTY 50',
    'SENSEX': 'BSE:SENSEX',
}


def fetch_instruments():
    """Fetch all instruments from Kite API."""
    print("Fetching instruments from Kite API...")
    response = requests.get(KITE_INSTRUMENTS_URL)
    response.raise_for_status()
    return response.text


def parse_instruments(csv_data):
    """Parse the CSV data from Kite API."""
    reader = csv.DictReader(StringIO(csv_data))
    return list(reader)


def filter_nse_stocks_and_options(instruments):
    """
    Filter NSE stocks (EQ segment) and their corresponding options (NFO segment).
    Returns a dictionary mapping stock symbols to their options.
    """
    # Get NSE equity stocks
    nse_stocks = {}
    for inst in instruments:
        if inst['exchange'] == 'NSE' and inst['segment'] == 'NSE':
            symbol = inst['tradingsymbol']
            nse_stocks[symbol] = {
                'instrument_token': inst['instrument_token'],
                'name': inst['name'],
                'tradingsymbol': symbol,
                'exchange': inst['exchange'],
                'lot_size': inst.get('lot_size', '1'),
                'tick_size': inst.get('tick_size', ''),
            }

    # Get NFO options for these stocks
    stock_options = defaultdict(list)
    for inst in instruments:
        if inst['exchange'] == 'NFO' and inst['instrument_type'] in ('CE', 'PE'):
            # The underlying symbol is in the 'name' field
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


def filter_index_options(instruments, index_names=None):
    """
    Filter index options from NFO-OPT (NIFTY) and BFO-OPT (SENSEX) segments.

    Args:
        instruments: List of all instruments from Kite API
        index_names: List of index names to filter (default: NIFTY, SENSEX)

    Returns:
        Dictionary mapping index names to list of options
    """
    if index_names is None:
        index_names = INDEX_NAMES

    index_options = defaultdict(list)

    for inst in instruments:
        # NFO-OPT for NIFTY, BFO-OPT for SENSEX
        segment = inst.get('segment', '')
        name = inst.get('name', '')
        inst_type = inst.get('instrument_type', '')

        # Check if it's an option (CE or PE) in the right segment
        if inst_type not in ('CE', 'PE'):
            continue

        # NIFTY options are in NFO-OPT segment
        # SENSEX options are in BFO-OPT segment
        if segment in ('NFO-OPT', 'BFO-OPT') and name in index_names:
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


def write_index_options_csv(index_options, filename='index_options.csv'):
    """
    Write index options to a CSV file.

    Args:
        index_options: Dictionary mapping index names to list of options
        filename: Output filename
    """
    print(f"Writing index options to {filename}...")

    with open(filename, 'w', newline='') as csvfile:
        fieldnames = [
            'index_name',
            'tradingsymbol',
            'option_type',
            'strike',
            'expiry',
            'instrument_token',
            'lot_size',
            'tick_size',
            'segment',
            'exchange',
            'dte'
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        today = datetime.now().date()

        for index_name in sorted(index_options.keys()):
            options = index_options[index_name]

            for opt in sorted(options, key=lambda x: (x['expiry'], float(x['strike']), x['instrument_type'])):
                # Calculate DTE (Days To Expiry)
                try:
                    expiry_date = datetime.strptime(opt['expiry'], '%Y-%m-%d').date()
                    dte = (expiry_date - today).days
                except (ValueError, TypeError):
                    dte = 0

                writer.writerow({
                    'index_name': index_name,
                    'tradingsymbol': opt['tradingsymbol'],
                    'option_type': opt['instrument_type'],
                    'strike': opt['strike'],
                    'expiry': opt['expiry'],
                    'instrument_token': opt['instrument_token'],
                    'lot_size': opt['lot_size'],
                    'tick_size': opt['tick_size'],
                    'segment': opt['segment'],
                    'exchange': opt['exchange'],
                    'dte': dte
                })

    # Print summary
    for index_name in sorted(index_options.keys()):
        options = index_options[index_name]
        ce_count = sum(1 for o in options if o['instrument_type'] == 'CE')
        pe_count = sum(1 for o in options if o['instrument_type'] == 'PE')
        expiries = set(o['expiry'] for o in options)
        print(f"  {index_name}: {len(options)} options ({ce_count} CE, {pe_count} PE), {len(expiries)} expiries")

    print(f"Successfully written to {filename}")


def write_to_csv(nse_stocks, stock_options, filename='nse_stocks_options.csv'):
    """Write the stock and options data to a CSV file."""
    print(f"Writing data to {filename}...")

    with open(filename, 'w', newline='') as csvfile:
        fieldnames = [
            'stock_symbol',
            'stock_name',
            'stock_instrument_token',
            'stock_lot_size',
            'has_options',
            'options_count',
            'option_tradingsymbol',
            'option_type',
            'option_strike',
            'option_expiry',
            'option_instrument_token',
            'option_lot_size'
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for symbol, stock_info in sorted(nse_stocks.items()):
            options = stock_options.get(symbol, [])

            if options:
                # Write one row per option
                for opt in sorted(options, key=lambda x: (x['expiry'], x['strike'], x['instrument_type'])):
                    writer.writerow({
                        'stock_symbol': symbol,
                        'stock_name': stock_info['name'],
                        'stock_instrument_token': stock_info['instrument_token'],
                        'stock_lot_size': stock_info['lot_size'],
                        'has_options': 'Yes',
                        'options_count': len(options),
                        'option_tradingsymbol': opt['tradingsymbol'],
                        'option_type': opt['instrument_type'],
                        'option_strike': opt['strike'],
                        'option_expiry': opt['expiry'],
                        'option_instrument_token': opt['instrument_token'],
                        'option_lot_size': opt['lot_size']
                    })
            else:
                # Write stock without options
                writer.writerow({
                    'stock_symbol': symbol,
                    'stock_name': stock_info['name'],
                    'stock_instrument_token': stock_info['instrument_token'],
                    'stock_lot_size': stock_info['lot_size'],
                    'has_options': 'No',
                    'options_count': 0,
                    'option_tradingsymbol': '',
                    'option_type': '',
                    'option_strike': '',
                    'option_expiry': '',
                    'option_instrument_token': '',
                    'option_lot_size': ''
                })

    print(f"Successfully written to {filename}")


def write_summary_csv(nse_stocks, stock_options, filename='nse_stocks_options_summary.csv'):
    """Write a summary CSV with one row per stock."""
    print(f"Writing summary to {filename}...")

    with open(filename, 'w', newline='') as csvfile:
        fieldnames = [
            'stock_symbol',
            'stock_name',
            'stock_instrument_token',
            'stock_lot_size',
            'has_options',
            'total_options_count',
            'ce_count',
            'pe_count',
            'earliest_expiry',
            'latest_expiry'
        ]

        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for symbol, stock_info in sorted(nse_stocks.items()):
            options = stock_options.get(symbol, [])

            ce_count = sum(1 for o in options if o['instrument_type'] == 'CE')
            pe_count = sum(1 for o in options if o['instrument_type'] == 'PE')

            expiries = [o['expiry'] for o in options]
            earliest = min(expiries) if expiries else ''
            latest = max(expiries) if expiries else ''

            writer.writerow({
                'stock_symbol': symbol,
                'stock_name': stock_info['name'],
                'stock_instrument_token': stock_info['instrument_token'],
                'stock_lot_size': stock_info['lot_size'],
                'has_options': 'Yes' if options else 'No',
                'total_options_count': len(options),
                'ce_count': ce_count,
                'pe_count': pe_count,
                'earliest_expiry': earliest,
                'latest_expiry': latest
            })

    print(f"Successfully written to {filename}")


def main():
    try:
        # Fetch instruments
        csv_data = fetch_instruments()

        # Parse the data
        instruments = parse_instruments(csv_data)
        print(f"Total instruments fetched: {len(instruments)}")

        # =========================================================================
        # STOCK OPTIONS
        # =========================================================================
        print("\n" + "=" * 60)
        print("STOCK OPTIONS")
        print("=" * 60)

        # Filter NSE stocks and their options
        nse_stocks, stock_options = filter_nse_stocks_and_options(instruments)

        stocks_with_options = sum(1 for s in nse_stocks if s in stock_options)
        total_options = sum(len(opts) for opts in stock_options.values())

        print(f"NSE Stocks found: {len(nse_stocks)}")
        print(f"Stocks with options: {stocks_with_options}")
        print(f"Total stock options: {total_options}")

        # Write detailed CSV (one row per option)
        write_to_csv(nse_stocks, stock_options)

        # Write summary CSV (one row per stock)
        write_summary_csv(nse_stocks, stock_options)

        # =========================================================================
        # INDEX OPTIONS (NIFTY, SENSEX)
        # =========================================================================
        print("\n" + "=" * 60)
        print("INDEX OPTIONS")
        print("=" * 60)

        # Filter index options
        index_options = filter_index_options(instruments)

        total_index_options = sum(len(opts) for opts in index_options.values())
        print(f"Index options found: {total_index_options}")

        # Write index options CSV
        write_index_options_csv(index_options)

        # =========================================================================
        # SUMMARY
        # =========================================================================
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Stock options: {total_options} (from {stocks_with_options} stocks)")
        print(f"Index options: {total_index_options}")
        print(f"Total: {total_options + total_index_options}")
        print("\nOutput files:")
        print("  - nse_stocks_options.csv (detailed stock options)")
        print("  - nse_stocks_options_summary.csv (stock summary)")
        print("  - index_options.csv (NIFTY/SENSEX options)")
        print("\nDone!")

    except requests.RequestException as e:
        print(f"Error fetching data from Kite API: {e}")
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise


if __name__ == "__main__":
    main()
