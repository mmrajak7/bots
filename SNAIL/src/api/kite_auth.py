"""
Kite Connect Authenticator

HTTP-based automated login for Kite Connect API.
No browser needed - perfect for headless Raspberry Pi deployment.

@file        kite_auth.py
@description Automated Kite authentication with TOTP support
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 2.1
"""

import os
import json
import hashlib
import requests
import pyotp
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any
from loguru import logger


class KiteAuthenticationError(Exception):
    """Exception raised for Kite authentication failures."""
    pass


class KiteAuthenticator:
    """
    HTTP-based Kite authentication (no browser needed).

    Handles complete OAuth flow:
    1. POST /api/login with user_id + password
    2. Generate TOTP using pyotp
    3. POST /api/twofa with request_id + totp
    4. POST /connect/finish to get request_token
    5. POST /session/token with checksum to get access_token

    Attributes:
        api_key: Kite API key
        api_secret: Kite API secret
        user_id: Zerodha user ID
        password: Zerodha password
        totp_secret: TOTP secret for 2FA
        token_file: Path to token storage file
    """

    KITE_BASE_URL = "https://kite.zerodha.com"
    KITE_API_URL = "https://api.kite.trade"

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize authenticator with configuration.

        Args:
            config: Dictionary containing Kite credentials

        Raises:
            ValueError: If required credentials are missing
        """
        self.api_key = config.get('api_key') or os.getenv('ZERODHA_API_KEY')
        self.api_secret = config.get('api_secret') or os.getenv('ZERODHA_API_SECRET')
        self.user_id = config.get('user_id') or os.getenv('ZERODHA_USER_ID')
        self.password = config.get('password') or os.getenv('ZERODHA_PASSWORD')
        self.totp_secret = config.get('totp_secret') or os.getenv('ZERODHA_TOTP_SECRET')

        # Clean TOTP secret (remove spaces)
        if self.totp_secret:
            self.totp_secret = self.totp_secret.replace(" ", "").strip()

        # Token file path - default to shared BOTS/data folder
        token_file = config.get('token_file', '../data/kite_access_token.json')
        self.token_file = Path(token_file)

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
        """
        Validate that required credentials are present.

        Raises:
            ValueError: If any required credential is missing
        """
        missing = []
        if not self.api_key:
            missing.append('api_key')
        if not self.api_secret:
            missing.append('api_secret')
        if not self.user_id:
            missing.append('user_id')
        if not self.password:
            missing.append('password')
        if not self.totp_secret:
            missing.append('totp_secret')

        if missing:
            raise ValueError(f"Missing required Kite credentials: {', '.join(missing)}")

    def generate_totp(self) -> str:
        """
        Generate TOTP code from secret.

        Returns:
            6-digit TOTP code

        Raises:
            ValueError: If TOTP secret is not configured
        """
        if not self.totp_secret:
            raise ValueError("TOTP secret not configured")

        totp = pyotp.TOTP(self.totp_secret)
        code = totp.now()
        logger.debug(f"Generated TOTP code: {code[:2]}****")
        return code

    def auto_login(self) -> str:
        """
        Complete automated login flow.

        Returns:
            access_token string

        Raises:
            KiteAuthenticationError: If login fails at any step
        """
        logger.info(f"Starting auto-login for user: {self.user_id}")

        try:
            # Step 1: Get login page (establish session)
            self._step1_get_login_page()

            # Step 2: Submit credentials
            request_id = self._step2_submit_credentials()

            # Step 3: Submit TOTP
            self._step3_submit_totp(request_id)

            # Step 4: Authorize and get request token
            request_token = self._step4_authorize()

            # Step 5: Generate access token
            access_token = self._step5_generate_token(request_token)

            # Save token
            self._save_token(access_token)

            logger.info("Auto-login completed successfully")
            return access_token

        except KiteAuthenticationError:
            raise
        except Exception as e:
            logger.error(f"Auto-login failed: {e}")
            raise KiteAuthenticationError(f"Login failed: {str(e)}")

    def _step1_get_login_page(self) -> None:
        """Step 1: Get login page to establish session cookies."""
        login_url = f"{self.KITE_BASE_URL}/connect/login?v=3&api_key={self.api_key}"

        logger.debug("[1/5] Getting login page...")
        # Note: allow_redirects=True here is intentional - Kite may redirect
        # during initial page load. The localhost callback issue occurs later.
        response = self.session.get(login_url, allow_redirects=True, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(
                f"Failed to get login page: HTTP {response.status_code}"
            )

        logger.debug("Login page loaded successfully")

    def _step2_submit_credentials(self) -> str:
        """
        Step 2: Submit user_id and password.

        Returns:
            request_id for TOTP step
        """
        login_endpoint = f"{self.KITE_BASE_URL}/api/login"
        payload = {
            "user_id": self.user_id,
            "password": self.password
        }

        logger.debug("[2/5] Submitting credentials...")
        response = self.session.post(login_endpoint, data=payload, timeout=30, allow_redirects=False)

        if response.status_code != 200:
            raise KiteAuthenticationError(
                f"Credential submission failed: HTTP {response.status_code}"
            )

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            raise KiteAuthenticationError(f"Login failed: {error_msg}")

        request_id = result.get('data', {}).get('request_id')
        if not request_id:
            raise KiteAuthenticationError("No request_id received from login")

        logger.debug("Credentials accepted")
        return request_id

    def _step3_submit_totp(self, request_id: str) -> None:
        """
        Step 3: Submit TOTP code.

        Args:
            request_id: Request ID from credential submission
        """
        totp_endpoint = f"{self.KITE_BASE_URL}/api/twofa"
        totp_code = self.generate_totp()

        payload = {
            "user_id": self.user_id,
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp"
        }

        logger.debug("[3/5] Submitting TOTP...")
        response = self.session.post(totp_endpoint, data=payload, timeout=30, allow_redirects=False)

        if response.status_code != 200:
            raise KiteAuthenticationError(
                f"TOTP submission failed: HTTP {response.status_code}"
            )

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            raise KiteAuthenticationError(f"TOTP verification failed: {error_msg}")

        logger.debug("TOTP verified successfully")

    def _step4_authorize(self) -> str:
        """
        Step 4: Authorize app and get request token.

        Returns:
            request_token from redirect URL
        """
        authorize_endpoint = f"{self.KITE_BASE_URL}/connect/finish"
        payload = {
            "api_key": self.api_key,
            "skip_session": "true"
        }

        logger.debug("[4/5] Authorizing app...")
        response = self.session.post(
            authorize_endpoint,
            data=payload,
            allow_redirects=False,
            timeout=30
        )

        if response.status_code not in [302, 303, 307]:
            raise KiteAuthenticationError(
                f"Authorization failed: Expected redirect, got HTTP {response.status_code}"
            )

        location = response.headers.get('Location', '')
        if not location:
            raise KiteAuthenticationError("No redirect location in authorization response")

        # Parse request_token from redirect URL
        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        request_token = params.get('request_token', [None])[0]

        if not request_token:
            # Check for error
            error = params.get('status', [''])[0]
            if error:
                raise KiteAuthenticationError(f"Authorization error: {error}")
            raise KiteAuthenticationError(
                f"No request_token in redirect URL: {location[:100]}"
            )

        logger.debug(f"Got request_token: {request_token[:15]}...")
        return request_token

    def _step5_generate_token(self, request_token: str) -> str:
        """
        Step 5: Generate access token from request token.

        Args:
            request_token: Request token from authorization

        Returns:
            access_token string
        """
        # Generate checksum
        checksum_string = f"{self.api_key}{request_token}{self.api_secret}"
        checksum = hashlib.sha256(checksum_string.encode()).hexdigest()

        token_endpoint = f"{self.KITE_API_URL}/session/token"
        payload = {
            "api_key": self.api_key,
            "request_token": request_token,
            "checksum": checksum
        }

        logger.debug("[5/5] Generating access token...")
        response = requests.post(token_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            raise KiteAuthenticationError(
                f"Token generation failed: HTTP {response.status_code} - {response.text[:100]}"
            )

        result = response.json()
        if result.get('status') == 'error':
            error_msg = result.get('message', 'Unknown error')
            raise KiteAuthenticationError(f"Token generation failed: {error_msg}")

        access_token = result.get('data', {}).get('access_token')
        if not access_token:
            raise KiteAuthenticationError("No access_token in response")

        logger.debug(f"Access token generated: {access_token[:15]}...")
        return access_token

    def _save_token(self, access_token: str) -> None:
        """
        Save access token to file.

        Args:
            access_token: The access token to save
        """
        self.token_file.parent.mkdir(parents=True, exist_ok=True)

        token_data = {
            "access_token": access_token,
            "api_key": self.api_key,
            "user_id": self.user_id,
            "generated_at": datetime.now().isoformat(),
            "valid_until": "6:00 AM IST next day"
        }

        with open(self.token_file, 'w') as f:
            json.dump(token_data, f, indent=2)

        logger.info(f"Token saved to: {self.token_file}")

    def load_token(self) -> Optional[str]:
        """
        Load existing token if valid.

        Returns:
            access_token if valid, None otherwise
        """
        if not self.token_file.exists():
            logger.debug("No token file found")
            return None

        try:
            with open(self.token_file) as f:
                data = json.load(f)

            # Check if token is from today
            generated = datetime.fromisoformat(data['generated_at'])
            now = datetime.now()

            # Token is valid if:
            # - Generated today, or
            # - Generated yesterday and current time is before 6 AM
            if generated.date() == now.date():
                logger.debug("Token is from today, should be valid")
                return data['access_token']
            elif (now.date() - generated.date()).days == 1 and now.hour < 6:
                logger.debug("Token from yesterday, still valid (before 6 AM)")
                return data['access_token']

            logger.info("Token expired, need to re-authenticate")
            return None

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"Error loading token: {e}")
            return None

    def validate_token(self, access_token: str) -> bool:
        """
        Validate a token by making a test API call.

        Args:
            access_token: Token to validate

        Returns:
            True if token is valid, False otherwise
        """
        try:
            from kiteconnect import KiteConnect

            kite = KiteConnect(api_key=self.api_key)
            kite.set_access_token(access_token)

            # Try to get profile - this will fail if token is invalid
            kite.profile()
            logger.debug("Token validation successful")
            return True

        except Exception as e:
            logger.debug(f"Token validation failed: {e}")
            return False

    def ensure_valid_token(self) -> str:
        """
        Ensure we have a valid access token, generating one if needed.

        Returns:
            Valid access_token

        Raises:
            KiteAuthenticationError: If unable to obtain valid token
        """
        # Try loading existing token
        token = self.load_token()

        if token:
            # Validate it
            if self.validate_token(token):
                logger.info("Using existing valid token")
                return token
            logger.info("Existing token is invalid, regenerating")

        # Generate new token
        return self.auto_login()


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    # Load environment variables
    load_dotenv()

    # Configure logging
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    print("\n" + "=" * 60)
    print("Kite Connect - Automated Login Test")
    print("=" * 60)

    try:
        config = {
            'api_key': os.getenv('ZERODHA_API_KEY'),
            'api_secret': os.getenv('ZERODHA_API_SECRET'),
            'user_id': os.getenv('ZERODHA_USER_ID'),
            'password': os.getenv('ZERODHA_PASSWORD'),
            'totp_secret': os.getenv('ZERODHA_TOTP_SECRET'),
            'token_file': '../data/kite_access_token.json'  # Shared BOTS/data folder
        }

        auth = KiteAuthenticator(config)
        token = auth.ensure_valid_token()

        print("\n" + "=" * 60)
        print(f"SUCCESS! Access token: {token[:20]}...")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
