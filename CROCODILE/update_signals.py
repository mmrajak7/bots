#!/usr/bin/env python3
"""
Signal Generator for Crocodile Bot

Fetches daily and weekly SuperTrend signals from Chartink screener
and updates signals_crocodile.csv for the trading bot to process.

Usage:
    cd BOTS/CROCODILE
    python update_signals.py

Schedule (Raspberry Pi):
    Run every 2 minutes during market hours (9:15 AM - 3:30 PM IST)
    Cron: */2 9-15 * * 1-5 cd /home/pi/trading/BOTS/CROCODILE && python3 update_signals.py
"""

import os
import sys
import pandas as pd
import time
import requests
from datetime import datetime
from bs4 import BeautifulSoup
import pytz
import calendar
from loguru import logger

# ============================================================================
# CONFIGURATION
# ============================================================================

# Chartink Screener Payloads
DAILY_PAYLOAD = (
    "( {33489} ( ( {33489} ( ( {33489} ( "
    "daily close <= daily supertrend( 10 , 3 ) * 1.0095 and "
    "1 day ago close >= 1 day ago supertrend( 10 , 3 ) * 1 and "
    "1 week ago close >= 1 week ago supertrend( 10 , 3 ) * 1 and "
    "daily close >= daily supertrend( 10 , 3 ) * 0.9905 and "
    "daily close < 9999 and "
    "daily close > 50 "
    ") ) ) ) ) )"
)

WEEKLY_PAYLOAD = (
    "( {33489} ( ( {33489} ( ( {33489} ( "
    "weekly close <= weekly supertrend( 10 , 3 ) * 1.0095 and "
    "1 week ago close >= 1 week ago supertrend( 10 , 3 ) * 1 and "
    "weekly close >= weekly supertrend( 10 , 3 ) * 0.9905 and "
    "weekly close < 9999 and "
    "weekly close > 50 "
    ") ) ) ) ) )"
)

# Paths (Updated for new BOTS structure)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # BOTS/CROCODILE/
DATA_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), 'data')  # BOTS/data/
SIGNALS_FILE = os.path.join(DATA_DIR, 'signals_crocodile.csv')  # Updated filename
IGNORE_FILE = os.path.join(DATA_DIR, 'ignore_list_crocodile.csv')  # Updated filename

# Market hours (IST)
MARKET_START_HOUR = 9
MARKET_START_MIN = 15
MARKET_END_HOUR = 15
MARKET_END_MIN = 30

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_current_datetime():
    """Get current IST date and time"""
    now = datetime.now(pytz.timezone('Asia/Kolkata'))
    weekday = calendar.day_name[now.weekday()]
    curr_date = now.date()
    curr_time = now.time()
    return weekday, curr_date, curr_time, now


def check_weekend(weekday):
    """Exit if weekend"""
    if weekday in ['Saturday', 'Sunday']:
        logger.info(f"Weekend ({weekday}) - Exiting")
        sys.exit(0)


def check_market_hours(now):
    """Check if current time is within market hours"""
    curr_time = now.replace(second=0, microsecond=0)
    start_time = now.replace(hour=MARKET_START_HOUR, minute=MARKET_START_MIN, second=0, microsecond=0)
    end_time = now.replace(hour=MARKET_END_HOUR, minute=MARKET_END_MIN, second=0, microsecond=0)

    if curr_time < start_time or curr_time > end_time:
        logger.info(f"Outside market hours ({curr_time.strftime('%H:%M')}) - Exiting")
        return False
    return True


def load_ignore_list():
    """Load stocks to ignore from ignore_list.csv"""
    ignore_list = []
    if os.path.exists(IGNORE_FILE):
        try:
            df = pd.read_csv(IGNORE_FILE)
            if 'Script' in df.columns:
                ignore_list = df['Script'].tolist()
                logger.info(f"Loaded {len(ignore_list)} stocks from ignore list")
        except Exception as e:
            logger.warning(f"Could not load ignore list: {e}")
    return ignore_list


def check_duplicate(script, date, timeframe):
    """Check if signal already exists for today"""
    if not os.path.exists(SIGNALS_FILE):
        return False

    try:
        df = pd.read_csv(SIGNALS_FILE)
        if len(df) > 0:
            # Filter by date, script, and timeframe
            duplicate = df[
                (df['Date'] == str(date)) &
                (df['Script'] == script) &
                (df['TF'] == timeframe)
            ]
            return len(duplicate) > 0
    except Exception as e:
        logger.warning(f"Error checking duplicates: {e}")
        return False

    return False


def fetch_from_chartink(payload):
    """
    Fetch stocks from Chartink screener

    Args:
        payload: Screener condition string

    Returns:
        List of stock symbols (NSE codes)
    """
    try:
        chartink_link = "https://chartink.com/screener/"
        chartink_url = "https://chartink.com/screener/process"

        with requests.Session() as session:
            # Get CSRF token
            response = session.get(chartink_link, timeout=10)
            soup = BeautifulSoup(response.text, "html.parser")
            csrf_token = soup.select_one("[name='csrf-token']")['content']

            # Set headers
            session.headers['x-csrf-token'] = csrf_token

            # Post screener query
            payload_data = {'scan_clause': payload}
            response = session.post(chartink_url, data=payload_data, timeout=30)

            # Parse results
            stocks = []
            if response.status_code == 200:
                data = response.json()
                if 'data' in data:
                    for item in data['data']:
                        if 'nsecode' in item:
                            stocks.append(item['nsecode'])

            return stocks

    except Exception as e:
        logger.error(f"Error fetching from Chartink: {e}")
        return []


def initialize_signals_file():
    """Create signals_crocodile.csv if it doesn't exist, or migrate if columns missing"""
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
        logger.info(f"Created data directory: {DATA_DIR}")

    if not os.path.exists(SIGNALS_FILE):
        # Create new file with all columns including Time
        df = pd.DataFrame(columns=['Date', 'Time', 'Script', 'TF', 'Status'])
        df.to_csv(SIGNALS_FILE, index=False)
        logger.info(f"Created signals file: signals_crocodile.csv")
    else:
        # Migrate existing file if columns missing
        try:
            df = pd.read_csv(SIGNALS_FILE)
            migrated = False

            if 'Status' not in df.columns:
                df['Status'] = ''  # Add empty Status for existing rows
                migrated = True
                logger.info(f"Migrated signals file: added Status column")

            if 'Time' not in df.columns:
                df['Time'] = '00:00:00'  # Add default time for existing rows
                migrated = True
                logger.info(f"Migrated signals file: added Time column (default 00:00:00)")

            if migrated:
                # Reorder columns to: Date, Time, Script, TF, Status
                cols = ['Date', 'Time', 'Script', 'TF', 'Status']
                df = df[cols]
                df.to_csv(SIGNALS_FILE, index=False)

        except Exception as e:
            logger.warning(f"Could not check/migrate signals file: {e}")


def update_signals_file(new_signals):
    """
    Append new signals to signals_crocodile.csv with empty Status

    Args:
        new_signals: List of [date, time, script, timeframe] lists
    """
    if not new_signals:
        logger.info("No new signals to add")
        return

    try:
        # Read existing signals
        if os.path.exists(SIGNALS_FILE):
            df_existing = pd.read_csv(SIGNALS_FILE)
            # Ensure required columns exist
            if 'Status' not in df_existing.columns:
                df_existing['Status'] = ''
            if 'Time' not in df_existing.columns:
                df_existing['Time'] = '00:00:00'
        else:
            df_existing = pd.DataFrame(columns=['Date', 'Time', 'Script', 'TF', 'Status'])

        # Create dataframe from new signals with empty Status
        df_new = pd.DataFrame(new_signals, columns=['Date', 'Time', 'Script', 'TF'])
        df_new['Status'] = ''  # New signals have empty status (pending processing)

        # Concatenate
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)

        # Ensure column order is consistent
        df_combined = df_combined[['Date', 'Time', 'Script', 'TF', 'Status']]

        # Save
        df_combined.to_csv(SIGNALS_FILE, index=False)

        logger.success(f"Added {len(new_signals)} new signals to signals_crocodile.csv")

        # Display new signals
        for signal in new_signals:
            logger.info(f"   [{signal[3]}] {signal[2]} - {signal[0]} {signal[1]}")

    except Exception as e:
        logger.error(f"Error updating signals file: {e}")


# ============================================================================
# MAIN EXECUTION
# ============================================================================

def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  SIGNAL UPDATE WORKFLOW - Fetch Chartink Screener Signals   *
***************************************************************"""
    logger.info(banner)


def main():
    """Main execution function"""
    print_workflow_banner()
    logger.info("Signal Update workflow started")

    # Get current date/time
    weekday, curr_date, curr_time, now = get_current_datetime()
    logger.info(f"Date: {curr_date} ({weekday}) | Time: {curr_time.strftime('%H:%M:%S')} IST")

    # Check if weekend
    check_weekend(weekday)

    # Check market hours
    if not check_market_hours(now):
        sys.exit(0)

    # Initialize signals file
    initialize_signals_file()

    # Load ignore list
    ignore_list = load_ignore_list()

    # Define screeners to run
    screeners = [
        ['D', DAILY_PAYLOAD, 'Daily SuperTrend Touch'],
        ['W', WEEKLY_PAYLOAD, 'Weekly SuperTrend Touch']
    ]

    # Process each screener
    new_signals = []

    for timeframe, payload, description in screeners:
        logger.info(f"Fetching {description} ({timeframe})...")

        try:
            # Fetch stocks from Chartink
            stocks = fetch_from_chartink(payload)
            logger.info(f"   Found {len(stocks)} stocks from screener")

            # Process each stock
            added = 0
            for stock in stocks:
                # Skip if in ignore list
                if stock in ignore_list:
                    logger.debug(f"   Skipped {stock} (in ignore list)")
                    continue

                # Skip NIFTY indices
                if 'NIFTY' in stock:
                    logger.debug(f"   Skipped {stock} (index)")
                    continue

                # Check if duplicate
                if check_duplicate(stock, curr_date, timeframe):
                    logger.debug(f"   Skipped {stock} (duplicate)")
                    continue

                # Add to new signals (with current time)
                new_signals.append([str(curr_date), curr_time.strftime('%H:%M:%S'), stock, timeframe])
                added += 1

            logger.info(f"   Added {added} new signals for {timeframe}")

            # Rate limiting (avoid hitting Chartink too fast)
            time.sleep(1)

        except Exception as e:
            logger.error(f"   Error processing {timeframe}: {e}")

    # Update signals file
    if new_signals:
        update_signals_file(new_signals)
    else:
        logger.info("No new signals found")

    logger.info(f"Signal Update workflow complete: {len(new_signals)} new signals added")


if __name__ == "__main__":
    # Setup logging - same log file as other workflows
    LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(LOG_DIR, exist_ok=True)
    logger.add(
        os.path.join(LOG_DIR, "crocodile_{time:YYYY-MM-DD}.log"),
        rotation="1 day",
        retention="45 days",
        level="INFO"
    )

    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)
