#!/usr/bin/env python3
"""
Bouncer Scanner Runner - For cron job scheduling.
Runs during market hours (9:15 AM - 3:30 PM IST) every 5 minutes.

Usage:
  python run_scanner.py          # Run once (for cron)
  python run_scanner.py --loop   # Run continuously (for manual)
  python run_scanner.py --test   # Test mode (no alerts)

Cron setup (every 5 mins during market hours):
  */5 9-15 * * 1-5 cd /path/to/BOTS/Bouncer && python run_scanner.py >> logs/cron.log 2>&1
"""

import sys
import time
from datetime import datetime
import pytz

def is_market_hours() -> bool:
    """Check if current time is within market hours."""
    ist = pytz.timezone('Asia/Kolkata')
    now = datetime.now(ist)

    # Market hours: 9:15 AM - 3:30 PM IST, Mon-Fri
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    market_start = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)

    return market_start <= now <= market_end


def run_once(send_alerts: bool = True):
    """Run scanner once."""
    from scanner import run_scanner, print_results

    print(f"\n{'='*60}")
    print(f"BOUNCER RUN: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    if not is_market_hours():
        print("Outside market hours. Skipping scan.")
        return

    setups = run_scanner(send_alerts=send_alerts)
    print_results(setups)


def run_loop(interval_mins: int = 5):
    """Run scanner continuously in a loop."""
    from scanner import run_scanner, print_results, CONFIG

    print("Starting Bouncer in loop mode...")
    print(f"Interval: {interval_mins} minutes")
    print("Press Ctrl+C to stop")
    print()

    while True:
        try:
            if is_market_hours():
                setups = run_scanner(send_alerts=True)
                print_results(setups)
            else:
                print(f"{datetime.now().strftime('%H:%M')} - Outside market hours, sleeping...")

            # Sleep until next interval
            time.sleep(interval_mins * 60)

        except KeyboardInterrupt:
            print("\nStopping Bouncer...")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)  # Wait 1 min on error


if __name__ == '__main__':
    if '--loop' in sys.argv:
        run_loop(interval_mins=5)
    elif '--test' in sys.argv:
        run_once(send_alerts=False)
    else:
        run_once(send_alerts=True)
