"""
Generate Kite API Token - Standalone script for CROCODILE

Uses HTTP-based automated login to generate access token.
Based on SNAIL's kite_auth module but simplified for standalone use.
"""

import os
import sys
import json
import hashlib
import requests
import pyotp
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Configuration - using values from SNAIL/CROCODILE
CONFIG = {
    "api_key": "REDACTED_API_KEY",
    "api_secret": "REDACTED_API_SECRET",
    "user_id": "YL6478",
    "password": "REDACTED_PASSWORD",
    "totp_secret": "REDACTED_TOTP_SECRET"
}

# Token output path (shared BOTS/data folder)
# Using parent.parent goes from CROCODILE -> BOTS -> data
TOKEN_FILE = Path(__file__).parent.parent / "data" / "kite_access_token.json"

# Also save to local CROCODILE/data for consistency
LOCAL_TOKEN_FILE = Path(__file__).parent / "data" / "kite_access_token.json"

KITE_BASE_URL = "https://kite.zerodha.com"
KITE_API_URL = "https://api.kite.trade"


def generate_totp():
    """Generate TOTP code from secret."""
    totp = pyotp.TOTP(CONFIG["totp_secret"])
    return totp.now()


def auto_login():
    """Complete automated login flow and return access_token."""
    print(f"\n{'='*60}")
    print("Kite API Token Generator")
    print(f"{'='*60}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"User: {CONFIG['user_id']}")
    print(f"{'='*60}\n")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    })

    try:
        # Step 1: Get login page
        print("[1/5] Loading login page...")
        login_url = f"{KITE_BASE_URL}/connect/login?v=3&api_key={CONFIG['api_key']}"
        response = session.get(login_url, allow_redirects=True, timeout=30)

        if response.status_code != 200:
            raise Exception(f"Failed to load login page: HTTP {response.status_code}")
        print("      Login page loaded")

        # Step 2: Submit credentials
        print("[2/5] Submitting credentials...")
        login_endpoint = f"{KITE_BASE_URL}/api/login"
        payload = {
            "user_id": CONFIG["user_id"],
            "password": CONFIG["password"]
        }
        response = session.post(login_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise Exception(f"Credential submission failed: HTTP {response.status_code}")

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            raise Exception(f"Login failed: {error_msg}")

        request_id = result.get('data', {}).get('request_id')
        if not request_id:
            raise Exception("No request_id received from login")
        print("      Credentials accepted")

        # Step 3: Submit TOTP
        print("[3/5] Submitting TOTP...")
        totp_code = generate_totp()
        totp_endpoint = f"{KITE_BASE_URL}/api/twofa"
        payload = {
            "user_id": CONFIG["user_id"],
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp"
        }
        response = session.post(totp_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise Exception(f"TOTP submission failed: HTTP {response.status_code}")

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            raise Exception(f"TOTP verification failed: {error_msg}")
        print("      TOTP verified")

        # Step 4: Get request_token via redirect
        print("[4/5] Getting request_token...")
        response = session.get(login_url, allow_redirects=False, timeout=30)

        request_token = None
        max_redirects = 10
        current_url = login_url

        for i in range(max_redirects):
            response = session.get(current_url, allow_redirects=False, timeout=30)

            if response.status_code not in [301, 302, 303, 307, 308]:
                break

            location = response.headers.get('Location', '')
            if not location:
                break

            if 'request_token=' in location:
                parsed = urlparse(location)
                params = parse_qs(parsed.query)
                request_token = params.get('request_token', [None])[0]
                if request_token:
                    break

            # Don't follow localhost redirects
            if '127.0.0.1' in location or 'localhost' in location:
                break

            current_url = location

        if not request_token:
            raise Exception("Could not obtain request_token from redirect chain")
        print(f"      Got request_token: {request_token[:15]}...")

        # Step 5: Generate access token
        print("[5/5] Generating access token...")
        checksum_string = f"{CONFIG['api_key']}{request_token}{CONFIG['api_secret']}"
        checksum = hashlib.sha256(checksum_string.encode()).hexdigest()

        token_endpoint = f"{KITE_API_URL}/session/token"
        payload = {
            "api_key": CONFIG["api_key"],
            "request_token": request_token,
            "checksum": checksum
        }
        response = session.post(token_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise Exception(f"Token generation failed: HTTP {response.status_code}")

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            raise Exception(f"Token generation failed: {error_msg}")

        access_token = result.get('data', {}).get('access_token')
        if not access_token:
            raise Exception("No access_token in response")

        print(f"      Access token generated: {access_token[:15]}...")

        return access_token

    except Exception as e:
        print(f"\nERROR: {e}")
        raise


def save_token(access_token):
    """Save token to JSON file (both shared and local locations)."""
    token_data = {
        "access_token": access_token,
        "api_key": CONFIG["api_key"],
        "user_id": CONFIG["user_id"],
        "generated_at": datetime.now().isoformat(),
        "valid_until": "6:00 AM IST next day"
    }

    # Save to shared BOTS/data folder
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TOKEN_FILE, 'w') as f:
        json.dump(token_data, f, indent=2)

    # Also save to local CROCODILE/data folder
    LOCAL_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCAL_TOKEN_FILE, 'w') as f:
        json.dump(token_data, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Token saved to:")
    print(f"  - {TOKEN_FILE}")
    print(f"  - {LOCAL_TOKEN_FILE}")
    print(f"{'='*60}")


def validate_token(access_token):
    """Validate token by making a test API call."""
    print("\nValidating token...")

    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=CONFIG["api_key"])
        kite.set_access_token(access_token)

        # Test with profile call
        profile = kite.profile()
        print(f"   User: {profile.get('user_name')} ({profile.get('user_id')})")
        print(f"   Broker: {profile.get('broker')}")
        print("   Token is VALID")
        return True

    except ImportError:
        print("   kiteconnect not installed - skipping validation")
        return True
    except Exception as e:
        print(f"   Token validation failed: {e}")
        return False


def main():
    """Main entry point."""
    try:
        access_token = auto_login()
        save_token(access_token)
        validate_token(access_token)

        print(f"\n{'='*60}")
        print("SUCCESS: Token generated and saved")
        print(f"{'='*60}\n")
        return 0

    except Exception as e:
        print(f"\nFAILED: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
