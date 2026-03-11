#!/usr/bin/env python3
"""
Backward compatibility wrapper.

All BCS logic has moved to the bcs/ package.
This wrapper ensures existing CLI usage still works:
    python helper/spread_monitor.py ICICIBANK --dry-run
    python helper/spread_monitor.py --list
    python helper/spread_monitor.py --cron
"""
import sys
from pathlib import Path

# Add project root to path so bcs package is importable
sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))

from bcs.spread_monitor import main

if __name__ == '__main__':
    main()
