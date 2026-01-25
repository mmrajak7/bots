#!/usr/bin/env python3
"""
Generate Token - Standalone Kite API Access Token Generator for FIFTY

Uses HTTP-based automated login (no browser needed).
Can be run manually or called by orchestrator at startup.

Usage:
    python generate_token.py          # Generate token
    python generate_token.py --check  # Check if token is valid
    python generate_token.py --test   # Validate token with API call

Requirements:
    Create data/kite_credentials.json with:
    {
        "api_key": "your_api_key",
        "api_secret": "your_api_secret",
        "user_id": "your_user_id",
        "password": "your_password",
        "totp_secret": "your_totp_secret"
    }
"""

import os
import sys
import argparse
from pathlib import Path
from datetime import datetime

# Set working directory to script location
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger


def setup_logging():
    """Configure logging for standalone run"""
    logger.remove()
    logger.add(
        sys.stderr,
        format="{time:HH:mm:ss} | {level:<8} | {message}",
        level="INFO"
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='FIFTY Token Generator')
    parser.add_argument('--check', action='store_true', help='Check if token is valid')
    parser.add_argument('--test', action='store_true', help='Validate token with API call')
    parser.add_argument('--force', action='store_true', help='Force regenerate even if valid')

    args = parser.parse_args()

    setup_logging()

    print(f"\n{'='*60}")
    print("FIFTY Bot - Kite Token Generator")
    print(f"{'='*60}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    try:
        from src.utils.token_generator import (
            is_token_valid,
            ensure_valid_token,
            validate_token_with_api,
            load_credentials,
            generate_access_token,
            save_token
        )

        # Check mode
        if args.check:
            is_valid, msg = is_token_valid()
            status = "VALID" if is_valid else "INVALID/EXPIRED"
            print(f"Token Status: {status}")
            print(f"Details: {msg}")
            return 0 if is_valid else 1

        # Test mode
        if args.test:
            is_valid, msg = validate_token_with_api()
            status = "VALID" if is_valid else "INVALID"
            print(f"API Validation: {status}")
            print(f"Details: {msg}")
            return 0 if is_valid else 1

        # Force regenerate
        if args.force:
            print("Force regeneration requested...\n")
            creds = load_credentials()
            if not creds:
                print("ERROR: Failed to load credentials")
                return 1

            success, msg, token = generate_access_token(creds)
            if not success:
                print(f"FAILED: {msg}")
                return 1

            if not save_token(creds, token):
                print("FAILED: Could not save token")
                return 1

            print(f"\nSUCCESS: New token generated for {creds['user_id']}")
            return 0

        # Normal mode - ensure valid token
        success, msg = ensure_valid_token()

        if success:
            print(f"\n{'='*60}")
            print(f"SUCCESS: {msg}")
            print(f"{'='*60}\n")
            return 0
        else:
            print(f"\n{'='*60}")
            print(f"FAILED: {msg}")
            print(f"{'='*60}\n")
            return 1

    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
