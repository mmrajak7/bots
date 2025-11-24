"""
Simple kite_trade implementation for getting enctoken
This module handles Zerodha login and extracts the session token (enctoken)
"""

import re
import time
import requests
from urllib.parse import parse_qs, urlparse
from loguru import logger


def get_enctoken(userid, password, twofa):
    """
    Login to Zerodha and get enctoken

    Args:
        userid: Zerodha user ID
        password: Zerodha password
        twofa: TOTP code (6 digits)

    Returns:
        str: Enctoken for session authentication
    """
    try:
        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        # Step 1: Get login page to retrieve request_id
        logger.debug("Getting login page...")
        login_url = "https://kite.zerodha.com/api/login"
        r = session.get(login_url)

        # Step 2: Post credentials
        logger.debug("Posting credentials...")
        payload = {
            'user_id': userid,
            'password': password
        }
        r = session.post(login_url, data=payload)

        if r.status_code != 200:
            raise Exception(f"Login failed with status {r.status_code}")

        response_data = r.json()

        # Check if TOTP is required
        if response_data.get('status') != 'success':
            raise Exception(f"Login failed: {response_data.get('message', 'Unknown error')}")

        request_id = response_data['data']['request_id']

        # Step 3: Submit TOTP
        logger.debug("Submitting TOTP...")
        twofa_url = "https://kite.zerodha.com/api/twofa"
        payload = {
            'user_id': userid,
            'request_id': request_id,
            'twofa_value': twofa,
            'twofa_type': 'totp',
            'skip_session': ''
        }
        r = session.post(twofa_url, data=payload)

        if r.status_code != 200:
            raise Exception(f"TOTP submission failed with status {r.status_code}")

        # Step 4: Extract enctoken from cookies
        enctoken = None
        for cookie in session.cookies:
            if cookie.name == 'enctoken':
                enctoken = cookie.value
                break

        if not enctoken:
            raise Exception("Failed to retrieve enctoken from session cookies")

        logger.info(f"Successfully retrieved enctoken for user {userid}")
        return enctoken

    except Exception as e:
        logger.error(f"Error getting enctoken: {e}")
        raise


if __name__ == "__main__":
    # Test mode - requires credentials from environment
    import os
    from dotenv import load_dotenv
    import pyotp

    load_dotenv('config/cred.env')

    userid = os.getenv('ZERODHA_USER_ID')
    password = os.getenv('ZERODHA_PASSWORD')
    totp_key = os.getenv('ZERODHA_TOTP_KEY')

    if not all([userid, password, totp_key]):
        print("Error: Credentials not found in config/cred.env")
        exit(1)

    # Generate TOTP code
    totp = pyotp.TOTP(totp_key)
    twofa_code = totp.now()

    print(f"Testing login for user: {userid}")
    print(f"Generated TOTP: {twofa_code}")

    try:
        token = get_enctoken(userid, password, twofa_code)
        print(f"\nSuccess! Enctoken retrieved:")
        print(f"Token length: {len(token)} characters")
        print(f"Token preview: {token[:30]}...")
    except Exception as e:
        print(f"\nFailed: {e}")
        exit(1)
