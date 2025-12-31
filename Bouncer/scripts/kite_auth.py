#!/usr/bin/env python3
"""
Kite Authentication Module for Bouncer
=======================================
Uses SNAIL's credentials (creds.env) for token generation.
Saves token to shared BOTS/data/kite_access_token.json

This allows Bouncer to run independently on weekends.

Run: python scripts/kite_auth.py
"""

import os
import sys
import json
import hashlib
import requests
import pyotp
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any
from dotenv import load_dotenv

# =============================================================================
# PATHS
# =============================================================================

SCRIPT_DIR = Path(__file__).parent.resolve()
BOUNCER_DIR = SCRIPT_DIR.parent
BOTS_DIR = BOUNCER_DIR.parent
DATA_DIR = BOTS_DIR / 'data'

# SNAIL credentials file
SNAIL_CREDS = BOTS_DIR / 'SNAIL' / 'config' / 'creds.env'

# Shared token file
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'


# =============================================================================
# KITE AUTHENTICATOR
# =============================================================================

class KiteAuthenticationError(Exception):
    """Exception raised for Kite authentication failures."""
    pass


class KiteAuthenticator:
    """
    HTTP-based Kite authentication using TOTP.
    Loads credentials from SNAIL's creds.env file.
    """

    KITE_BASE_URL = "https://kite.zerodha.com"
    KITE_API_URL = "https://api.kite.trade"

    def __init__(self):
        """Initialize authenticator with credentials from SNAIL config."""
        # Load credentials from SNAIL's creds.env
        if SNAIL_CREDS.exists():
            load_dotenv(SNAIL_CREDS)
            print(f"Loaded credentials from: {SNAIL_CREDS}")
        else:
            # Fallback to environment variables
            print("SNAIL creds.env not found, using environment variables")

        self.api_key = os.getenv('ZERODHA_API_KEY')
        self.api_secret = os.getenv('ZERODHA_API_SECRET')
        self.user_id = os.getenv('ZERODHA_USER_ID')
        self.password = os.getenv('ZERODHA_PASSWORD')
        self.totp_secret = os.getenv('ZERODHA_TOTP_SECRET')

        # Clean TOTP secret (remove spaces)
        if self.totp_secret:
            self.totp_secret = self.totp_secret.replace(" ", "").strip()

        # HTTP session with browser-like headers
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
        })

        self._validate_config()

    def _validate_config(self) -> None:
        """Validate that required credentials are present."""
        missing = []
        if not self.api_key:
            missing.append('ZERODHA_API_KEY')
        if not self.api_secret:
            missing.append('ZERODHA_API_SECRET')
        if not self.user_id:
            missing.append('ZERODHA_USER_ID')
        if not self.password:
            missing.append('ZERODHA_PASSWORD')
        if not self.totp_secret:
            missing.append('ZERODHA_TOTP_SECRET')

        if missing:
            raise ValueError(
                f"Missing credentials: {', '.join(missing)}\n"
                f"Please ensure SNAIL's creds.env is configured: {SNAIL_CREDS}"
            )

    def generate_totp(self) -> str:
        """Generate TOTP code from secret."""
        if not self.totp_secret:
            raise ValueError("TOTP secret not configured")
        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def auto_login(self) -> str:
        """
        Complete automated login flow.
        Returns access_token string.
        """
        print(f"\n{'='*60}")
        print("Kite Connect - Automated Login")
        print(f"{'='*60}")
        print(f"User: {self.user_id}")
        print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        try:
            # Step 1: Get login page
            print("\n[1/5] Getting login page...")
            request_token = self._step1_get_login_page()

            if not request_token:
                # Step 2: Submit credentials
                print("[2/5] Submitting credentials...")
                request_id = self._step2_submit_credentials()

                # Step 3: Submit TOTP
                print("[3/5] Submitting TOTP...")
                self._step3_submit_totp(request_id)

                # Step 4: Authorize
                print("[4/5] Authorizing...")
                request_token = self._step4_authorize()

            # Step 5: Generate access token
            print("[5/5] Generating access token...")
            access_token = self._step5_generate_token(request_token)

            # Save token
            self._save_token(access_token)

            print(f"\n{'='*60}")
            print("SUCCESS! Token generated and saved.")
            print(f"{'='*60}\n")

            return access_token

        except KiteAuthenticationError:
            raise
        except Exception as e:
            raise KiteAuthenticationError(f"Login failed: {str(e)}")

    def _step1_get_login_page(self) -> Optional[str]:
        """Get login page, return request_token if already authenticated."""
        login_url = f"{self.KITE_BASE_URL}/connect/login?v=3&api_key={self.api_key}"
        response = self.session.get(login_url, allow_redirects=False, timeout=30)

        if response.status_code in [302, 303, 307]:
            location = response.headers.get('Location', '')
            if 'request_token=' in location:
                parsed = urlparse(location)
                params = parse_qs(parsed.query)
                request_token = params.get('request_token', [None])[0]
                if request_token:
                    print("  Already authenticated!")
                    return request_token
            response = self.session.get(location, allow_redirects=True, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(f"Failed to get login page: HTTP {response.status_code}")

        print("  Login page loaded")
        return None

    def _step2_submit_credentials(self) -> str:
        """Submit user_id and password, return request_id."""
        login_endpoint = f"{self.KITE_BASE_URL}/api/login"
        payload = {"user_id": self.user_id, "password": self.password}

        response = self.session.post(login_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(f"Credential submission failed: HTTP {response.status_code}")

        result = response.json()
        if result.get('status') != 'success':
            raise KiteAuthenticationError(f"Login failed: {result.get('message', 'Unknown error')}")

        request_id = result.get('data', {}).get('request_id')
        if not request_id:
            raise KiteAuthenticationError("No request_id received")

        print("  Credentials accepted")
        return request_id

    def _step3_submit_totp(self, request_id: str) -> None:
        """Submit TOTP code."""
        totp_endpoint = f"{self.KITE_BASE_URL}/api/twofa"
        totp_code = self.generate_totp()

        payload = {
            "user_id": self.user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp"
        }

        response = self.session.post(totp_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(f"TOTP submission failed: HTTP {response.status_code}")

        result = response.json()
        if result.get('status') != 'success':
            raise KiteAuthenticationError(f"TOTP failed: {result.get('message', 'Unknown error')}")

        print(f"  TOTP verified ({totp_code[:2]}****)")

    def _step4_authorize(self) -> str:
        """Get request token via redirect chain."""
        login_url = f"{self.KITE_BASE_URL}/connect/login?v=3&api_key={self.api_key}"
        current_url = login_url

        for i in range(10):  # Max 10 redirects
            response = self.session.get(current_url, allow_redirects=False, timeout=30)

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
                    print(f"  Got request_token: {request_token[:15]}...")
                    return request_token

            if '127.0.0.1' in location or 'localhost' in location:
                break

            current_url = location

        raise KiteAuthenticationError("Could not obtain request_token")

    def _step5_generate_token(self, request_token: str) -> str:
        """Generate access token from request token."""
        checksum_string = f"{self.api_key}{request_token}{self.api_secret}"
        checksum = hashlib.sha256(checksum_string.encode()).hexdigest()

        token_endpoint = f"{self.KITE_API_URL}/session/token"
        payload = {
            "api_key": self.api_key,
            "request_token": request_token,
            "checksum": checksum
        }

        response = requests.post(token_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(f"Token generation failed: HTTP {response.status_code}")

        result = response.json()
        access_token = result.get('data', {}).get('access_token')
        if not access_token:
            raise KiteAuthenticationError("No access_token in response")

        print(f"  Access token: {access_token[:15]}...")
        return access_token

    def _save_token(self, access_token: str) -> None:
        """Save access token to shared file."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        token_data = {
            "access_token": access_token,
            "api_key": self.api_key,
            "user_id": self.user_id,
            "generated_at": datetime.now().isoformat(),
            "generated_by": "Bouncer",
            "valid_until": "6:00 AM IST next day"
        }

        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_data, f, indent=2)

        print(f"  Token saved to: {TOKEN_FILE}")

    def load_existing_token(self) -> Optional[str]:
        """Load existing token if still valid."""
        if not TOKEN_FILE.exists():
            return None

        try:
            with open(TOKEN_FILE) as f:
                data = json.load(f)

            generated = datetime.fromisoformat(data['generated_at'])
            now = datetime.now()

            # Token valid if: generated today, or yesterday before 6 AM
            if generated.date() == now.date():
                return data['access_token']
            elif (now.date() - generated.date()).days == 1 and now.hour < 6:
                return data['access_token']

            return None
        except Exception:
            return None

    def validate_token(self, access_token: str) -> bool:
        """Validate token by making test API call."""
        try:
            from kiteconnect import KiteConnect
            kite = KiteConnect(api_key=self.api_key)
            kite.set_access_token(access_token)
            kite.profile()
            return True
        except Exception:
            return False

    def ensure_valid_token(self) -> str:
        """
        Ensure we have a valid token.
        Returns existing token if valid, otherwise generates new one.
        """
        # Try loading existing token
        existing = self.load_existing_token()
        if existing:
            print("Found existing token, validating...")
            if self.validate_token(existing):
                print("Existing token is valid!")
                return existing
            print("Existing token is invalid, regenerating...")

        # Generate new token
        return self.auto_login()


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    """Main entry point."""
    print("\n" + "=" * 60)
    print("BOUNCER - Kite Token Generator")
    print("=" * 60)
    print(f"SNAIL config: {SNAIL_CREDS}")
    print(f"Token file: {TOKEN_FILE}")
    print("=" * 60)

    try:
        auth = KiteAuthenticator()

        if '--force' in sys.argv:
            print("\nForce regenerating token...")
            token = auth.auto_login()
        else:
            token = auth.ensure_valid_token()

        print(f"\nToken ready: {token[:20]}...")
        print("Bouncer can now run independently.")
        return 0

    except Exception as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
