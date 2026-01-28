#!/usr/bin/env python3
"""
Cleanup stale signals from signal_queue

Removes signals in NOTIFIED/HOLD/AWAITING_PRICE status that have old/wrong prices.
Fresh signals will be picked up on next run.

Usage:
    python cleanup_stale_signals.py --dry-run  # Preview what will be cleaned
    python cleanup_stale_signals.py            # Actually clean up
"""

import argparse
import os
import sys
from pathlib import Path

# Set working directory to bot root
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from src.models.database import get_session, SignalQueue, SignalStatus, init_database
from src.utils.timezone_helper import ist_now_naive


def cleanup_stale_signals(dry_run: bool = True):
    """Clean up stale signals awaiting response"""
    init_database()
    session = get_session()

    try:
        # Find all signals in awaiting states
        awaiting_statuses = [
            SignalStatus.NOTIFIED,
            SignalStatus.HOLD,
            SignalStatus.AWAITING_PRICE
        ]

        stale_signals = session.query(SignalQueue).filter(
            SignalQueue.status.in_(awaiting_statuses)
        ).all()

        if not stale_signals:
            print("No stale signals found.")
            return

        print(f"\nFound {len(stale_signals)} stale signals:\n")
        print(f"{'Script':<15} {'Level':>10} {'Status':<15} {'Date':<12}")
        print("-" * 55)

        for sig in stale_signals:
            print(f"{sig.script:<15} {sig.signal_level:>10,.2f} {sig.status.value:<15} {sig.signal_date}")

        if dry_run:
            print("\n[DRY RUN] No changes made. Run without --dry-run to clean up.")
            return

        # Set all to EXPIRED so they can be re-detected fresh
        for sig in stale_signals:
            sig.status = SignalStatus.EXPIRED
            sig.notes = f"Cleaned up: stale awaiting signal - {ist_now_naive()}"

        session.commit()
        print(f"\n✅ Cleaned up {len(stale_signals)} stale signals (marked as EXPIRED)")
        print("Fresh signals will be detected on next processing run.")

    except Exception as e:
        logger.error(f"Error: {e}")
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Cleanup stale signals')
    parser.add_argument('--dry-run', action='store_true', help='Preview only, no changes')
    args = parser.parse_args()

    cleanup_stale_signals(dry_run=args.dry_run)
