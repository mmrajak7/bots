"""
Token Generator - Automated Kite API Access Token Generator for FIFTY

Handles daily token generation using TOTP + password authentication.
Called by orchestrator during morning startup.
"""

import os
import json
import hashlib
import requests
import pyotp
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from typing import Optional, Tuple, Dict, Any

from loguru import logger

from src.utils.config_manager import config
from src.utils.timezone_helper import now_ist, IST


# Kite URLs
KITE_BASE_URL = "https://kite.zerodha.com"
KITE_API_URL = "https://api.kite.trade"


def load_credentials() -> Optional[Dict[str, str]]:
    """
    Load credentials from JSON file specified in config.

    Returns:
        Dict with api_key, api_secret, user_id, password, totp_secret
        None if file not found or invalid
    """
    credentials_file = config.get('kite_trade.credentials_file', 'data/kite_credentials.json')

    # Resolve relative path from FIFTY root
    if not os.path.isabs(credentials_file):
        project_root = Path(__file__).parent.parent.parent
        credentials_file = project_root / credentials_file

    if not os.path.exists(credentials_file):
        logger.error(f"Credentials file not found: {credentials_file}")
        return None

    try:
        with open(credentials_file, 'r') as f:
            creds = json.load(f)

        required = ['api_key', 'api_secret', 'user_id', 'password', 'totp_secret']
        missing = [k for k in required if not creds.get(k)]

        if missing:
            logger.error(f"Missing credentials: {missing}")
            return None

        return creds
    except Exception as e:
        logger.error(f"Error loading credentials: {e}")
        return None


def is_token_valid() -> Tuple[bool, str]:
    """
    Check if current token is valid (exists and not expired).

    Returns:
        Tuple of (is_valid, message)
    """
    token_file = config.get('kite_trade.token_file', 'data/kite_access_token.json')

    # Resolve relative path
    if not os.path.isabs(token_file):
        project_root = Path(__file__).parent.parent.parent
        token_file = project_root / token_file

    if not os.path.exists(token_file):
        return False, "Token file not found"

    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        access_token = token_data.get('access_token')
        generated_at = token_data.get('generated_at', '')

        if not access_token:
            return False, "Token file is empty or invalid"

        # Check token age
        if generated_at:
            try:
                token_time = datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
                if token_time.tzinfo is None:
                    token_time = token_time.replace(tzinfo=IST)

                current_time = now_ist()
                today_6am = current_time.replace(hour=6, minute=0, second=0, microsecond=0)

                # Tokens are valid from 6 AM to 6 AM next day
                if current_time < today_6am:
                    valid_from = today_6am - timedelta(days=1)
                else:
                    valid_from = today_6am

                if token_time < valid_from:
                    hours_old = (current_time - token_time).total_seconds() / 3600
                    return False, f"Token expired ({hours_old:.1f} hours old)"

                hours_old = (current_time - token_time).total_seconds() / 3600
                return True, f"Token valid ({hours_old:.1f} hours old)"

            except (ValueError, TypeError) as e:
                # Can't validate date but token exists
                return True, f"Token exists (date validation failed: {e})"

        return True, "Token exists"

    except Exception as e:
        return False, f"Error reading token file: {e}"


def generate_totp(secret: str) -> str:
    """Generate TOTP code from secret."""
    totp = pyotp.TOTP(secret)
    return totp.now()


def generate_access_token(creds: Dict[str, str]) -> Tuple[bool, str, Optional[str]]:
    """
    Generate new access token via automated HTTP login.

    Args:
        creds: Dict with api_key, api_secret, user_id, password, totp_secret

    Returns:
        Tuple of (success, message, access_token or None)
    """
    logger.info(f"Generating access token for user {creds['user_id']}")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    })

    try:
        # Step 1: Get login page
        logger.debug("Step 1: Loading login page...")
        login_url = f"{KITE_BASE_URL}/connect/login?v=3&api_key={creds['api_key']}"
        response = session.get(login_url, allow_redirects=True, timeout=30)

        if response.status_code != 200:
            return False, f"Failed to load login page: HTTP {response.status_code}", None

        # Step 2: Submit credentials
        logger.debug("Step 2: Submitting credentials...")
        login_endpoint = f"{KITE_BASE_URL}/api/login"
        payload = {
            "user_id": creds["user_id"],
            "password": creds["password"]
        }
        response = session.post(login_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            return False, f"Credential submission failed: HTTP {response.status_code}", None

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            return False, f"Login failed: {error_msg}", None

        request_id = result.get('data', {}).get('request_id')
        if not request_id:
            return False, "No request_id received from login", None

        # Step 3: Submit TOTP
        logger.debug("Step 3: Submitting TOTP...")
        totp_code = generate_totp(creds["totp_secret"])
        totp_endpoint = f"{KITE_BASE_URL}/api/twofa"
        payload = {
            "user_id": creds["user_id"],
            "request_id": request_id,
            "twofa_value": totp_code,
            "twofa_type": "totp"
        }
        response = session.post(totp_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            return False, f"TOTP submission failed: HTTP {response.status_code}", None

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            return False, f"TOTP verification failed: {error_msg}", None

        # Step 4: Get request_token via redirect
        logger.debug("Step 4: Getting request_token...")
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

            # Check for request_token in redirect URL (including localhost callback)
            if 'request_token=' in location:
                parsed = urlparse(location)
                params = parse_qs(parsed.query)
                request_token = params.get('request_token', [None])[0]
                if request_token:
                    logger.debug(f"Got request_token from redirect: {request_token[:15]}...")
                    break

            # Don't follow localhost/127.0.0.1 redirects (but we already captured token above)
            if '127.0.0.1' in location or 'localhost' in location:
                # Try to extract request_token even if not in query string format
                if 'request_token' in location:
                    parsed = urlparse(location)
                    params = parse_qs(parsed.query)
                    request_token = params.get('request_token', [None])[0]
                break

            current_url = location

        if not request_token:
            return False, "Could not obtain request_token from redirect chain", None

        # Step 5: Generate access token
        logger.debug("Step 5: Generating access token...")
        checksum_string = f"{creds['api_key']}{request_token}{creds['api_secret']}"
        checksum = hashlib.sha256(checksum_string.encode()).hexdigest()

        token_endpoint = f"{KITE_API_URL}/session/token"
        payload = {
            "api_key": creds["api_key"],
            "request_token": request_token,
            "checksum": checksum
        }
        response = session.post(token_endpoint, data=payload, timeout=30)

        if response.status_code != 200:
            return False, f"Token generation failed: HTTP {response.status_code}", None

        result = response.json()
        if result.get('status') != 'success':
            error_msg = result.get('message', 'Unknown error')
            return False, f"Token generation failed: {error_msg}", None

        access_token = result.get('data', {}).get('access_token')
        if not access_token:
            return False, "No access_token in response", None

        logger.info(f"Access token generated successfully for {creds['user_id']}")
        return True, "Token generated successfully", access_token

    except requests.Timeout:
        return False, "Request timed out", None
    except requests.RequestException as e:
        return False, f"Network error: {e}", None
    except Exception as e:
        return False, f"Unexpected error: {e}", None


def save_token(creds: Dict[str, str], access_token: str) -> bool:
    """
    Save access token to JSON file.

    Args:
        creds: Credentials dict (for api_key, user_id)
        access_token: The access token to save

    Returns:
        True if saved successfully
    """
    token_file = config.get('kite_trade.token_file', 'data/kite_access_token.json')

    # Resolve relative path
    if not os.path.isabs(token_file):
        project_root = Path(__file__).parent.parent.parent
        token_file = project_root / token_file

    try:
        token_data = {
            "access_token": access_token,
            "api_key": creds["api_key"],
            "user_id": creds["user_id"],
            "generated_at": now_ist().isoformat(),
            "valid_until": "6:00 AM IST next day"
        }

        # Ensure directory exists
        Path(token_file).parent.mkdir(parents=True, exist_ok=True)

        with open(token_file, 'w') as f:
            json.dump(token_data, f, indent=2)

        logger.info(f"Token saved to: {token_file}")
        return True

    except Exception as e:
        logger.error(f"Failed to save token: {e}")
        return False


def ensure_valid_token() -> Tuple[bool, str]:
    """
    Ensure we have a valid access token.

    If current token is expired or missing, generates a new one.
    This is the main function called by orchestrator during morning startup.

    Returns:
        Tuple of (success, message)
    """
    # Check if current token is valid
    is_valid, msg = is_token_valid()

    if is_valid:
        logger.info(f"Token check: {msg}")
        return True, msg

    logger.info(f"Token invalid: {msg}. Generating new token...")

    # Load credentials
    creds = load_credentials()
    if not creds:
        return False, "Failed to load credentials"

    # Generate new token
    success, gen_msg, access_token = generate_access_token(creds)

    if not success:
        return False, f"Token generation failed: {gen_msg}"

    # Save token
    if not save_token(creds, access_token):
        return False, "Failed to save token"

    return True, f"New token generated for {creds['user_id']}"


def validate_token_with_api() -> Tuple[bool, str]:
    """
    Validate token by making a test API call.

    Returns:
        Tuple of (is_valid, message)
    """
    try:
        from kiteconnect import KiteConnect

        token_file = config.get('kite_trade.token_file', 'data/kite_access_token.json')

        # Resolve relative path
        if not os.path.isabs(token_file):
            project_root = Path(__file__).parent.parent.parent
            token_file = project_root / token_file

        with open(token_file, 'r') as f:
            token_data = json.load(f)

        kite = KiteConnect(api_key=token_data["api_key"])
        kite.set_access_token(token_data["access_token"])

        profile = kite.profile()
        user_name = profile.get('user_name', 'Unknown')
        user_id = profile.get('user_id', 'Unknown')

        return True, f"Token validated: {user_name} ({user_id})"

    except ImportError:
        return True, "kiteconnect not installed - skipping API validation"
    except Exception as e:
        return False, f"Token validation failed: {e}"
