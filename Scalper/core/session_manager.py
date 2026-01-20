"""
Kayal - Session Manager

Auto-login with TOTP generation on terminal startup.
Maintains session tokens and handles reconnection.
"""

import pyotp
from neo_api_client import NeoAPI
from datetime import datetime, timedelta
import json
import os
import logging
from typing import Tuple, Optional, Dict, Any

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages NEO API session with auto-TOTP login."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.client: Optional[NeoAPI] = None
        self.session_file = "data/session.json"
        self.session_valid_until: Optional[datetime] = None
        self._connected = False

    def auto_login(self) -> Tuple[bool, str]:
        """
        Automatic login flow:
        1. Check if existing session is valid
        2. If not, generate TOTP and login fresh

        Returns:
            Tuple of (success: bool, message: str)
        """
        # Ensure data directory exists
        os.makedirs('data', exist_ok=True)

        # Try to restore session first
        if self._restore_session():
            self._connected = True
            return True, "Session restored from cache"

        # Fresh login with TOTP
        try:
            neo_creds = self.config.get('neo_credentials', {})

            if not neo_creds.get('consumer_key'):
                return False, "NEO credentials not configured in settings.yaml"

            self.client = NeoAPI(
                environment='prod',
                access_token=None,
                neo_fin_key=None,
                consumer_key=neo_creds['consumer_key']
            )

            # Generate TOTP automatically
            totp_secret = neo_creds.get('totp_secret', '')
            if not totp_secret:
                return False, "TOTP secret not configured"

            totp = pyotp.TOTP(totp_secret)
            current_totp = totp.now()

            logger.info(f"Generated TOTP: {current_totp[:2]}****")

            # Step 1: TOTP Login
            mobile = neo_creds.get('mobile_number', '')
            ucc = neo_creds.get('ucc', '')

            if not mobile or not ucc:
                return False, "Mobile number or UCC not configured"

            login_response = self.client.totp_login(
                mobile_number=mobile,
                ucc=ucc,
                totp=current_totp
            )

            logger.info(f"TOTP login response: {login_response}")

            # Step 2: MPIN Validation
            mpin = neo_creds.get('mpin', '')
            if not mpin:
                return False, "MPIN not configured"

            validate_response = self.client.totp_validate(mpin=mpin)
            logger.info(f"MPIN validation response: {validate_response}")

            # Check if session establishment required
            if hasattr(self.client, 'session_2fa'):
                session_response = self.client.session_2fa()
                logger.info(f"Session 2FA response: {session_response}")

            # Save session for reuse
            self._save_session()
            self._connected = True

            return True, f"Login successful at {datetime.now().strftime('%H:%M:%S')}"

        except Exception as e:
            logger.error(f"Login failed: {str(e)}", exc_info=True)
            return False, f"Login failed: {str(e)}"

    def _save_session(self):
        """Save session tokens for quick restoration."""
        try:
            session_data = {
                'timestamp': datetime.now().isoformat(),
                'valid_until': (datetime.now() + timedelta(hours=8)).isoformat(),
                'date': datetime.now().strftime('%Y-%m-%d'),
            }

            # Save access token if available
            if self.client and hasattr(self.client, '_access_token'):
                session_data['access_token'] = self.client._access_token

            os.makedirs('data', exist_ok=True)
            with open(self.session_file, 'w') as f:
                json.dump(session_data, f, indent=2)

            logger.info("Session saved to cache")
        except Exception as e:
            logger.warning(f"Failed to save session: {e}")

    def _restore_session(self) -> bool:
        """
        Attempt to restore previous session.

        Returns:
            True if session restored successfully, False otherwise
        """
        if not os.path.exists(self.session_file):
            return False

        try:
            with open(self.session_file, 'r') as f:
                session_data = json.load(f)

            # Check if session is from today
            session_date = session_data.get('date', '')
            today = datetime.now().strftime('%Y-%m-%d')

            if session_date != today:
                logger.info("Session from previous day, needs fresh login")
                return False

            # Check if session is still valid
            valid_until_str = session_data.get('valid_until', '')
            if valid_until_str:
                valid_until = datetime.fromisoformat(valid_until_str)
                if datetime.now() >= valid_until:
                    logger.info("Session expired, needs fresh login")
                    return False

            # Restore client with saved token
            access_token = session_data.get('access_token')
            if access_token:
                neo_creds = self.config.get('neo_credentials', {})
                self.client = NeoAPI(
                    environment='prod',
                    access_token=access_token,
                    neo_fin_key=None,
                    consumer_key=neo_creds.get('consumer_key', '')
                )

                # Verify session is actually working
                if self._verify_session():
                    self.session_valid_until = valid_until
                    logger.info("Session restored successfully")
                    return True

            return False

        except Exception as e:
            logger.warning(f"Failed to restore session: {e}")
            return False

    def _verify_session(self) -> bool:
        """
        Verify if current session is active by making a test API call.

        Returns:
            True if session is active, False otherwise
        """
        try:
            # Quick API call to verify session
            limits_response = self.client.limits()
            if limits_response and not limits_response.get('error'):
                return True
            return False
        except Exception as e:
            logger.warning(f"Session verification failed: {e}")
            return False

    def get_client(self) -> Optional[NeoAPI]:
        """Get the NEO API client instance."""
        return self.client

    def is_connected(self) -> bool:
        """Check if session is connected."""
        return self._connected and self.client is not None

    def is_session_active(self) -> bool:
        """Check if current session is active."""
        if not self._connected or not self.client:
            return False
        return self._verify_session()

    def logout(self):
        """Logout and clear session."""
        try:
            if self.client:
                # NEO API doesn't have explicit logout, just clear local session
                pass

            self._connected = False
            self.client = None

            # Remove session file
            if os.path.exists(self.session_file):
                os.remove(self.session_file)

            logger.info("Logged out successfully")
        except Exception as e:
            logger.warning(f"Logout error: {e}")

    def get_session_data(self) -> Dict[str, Any]:
        """Get current session data for sharing with other bots."""
        session_data = {
            'timestamp': datetime.now().isoformat(),
            'valid_until': (datetime.now() + timedelta(hours=8)).isoformat(),
            'date': datetime.now().strftime('%Y-%m-%d'),
        }

        if self.client and hasattr(self.client, '_access_token'):
            session_data['access_token'] = self.client._access_token

        return session_data

    def restore_session(self, session_data: Dict[str, Any]) -> bool:
        """
        Restore session from provided data (for sharing between bots).

        Args:
            session_data: Session data dict with access_token, date, valid_until

        Returns:
            True if session restored successfully, False otherwise
        """
        try:
            # Check if session is from today
            session_date = session_data.get('date', '')
            today = datetime.now().strftime('%Y-%m-%d')

            if session_date != today:
                logger.info("Session from previous day, needs fresh login")
                return False

            # Check if session is still valid
            valid_until_str = session_data.get('valid_until', '')
            if valid_until_str:
                valid_until = datetime.fromisoformat(valid_until_str)
                if datetime.now() >= valid_until:
                    logger.info("Session expired, needs fresh login")
                    return False

            # Restore client with saved token
            access_token = session_data.get('access_token')
            if access_token:
                neo_creds = self.config.get('neo_credentials', self.config)
                self.client = NeoAPI(
                    environment='prod',
                    access_token=access_token,
                    neo_fin_key=None,
                    consumer_key=neo_creds.get('consumer_key', '')
                )

                # Verify session is actually working
                if self._verify_session():
                    self.session_valid_until = valid_until
                    self._connected = True
                    logger.info("Session restored from shared data")
                    return True

            return False

        except Exception as e:
            logger.warning(f"Failed to restore session from shared data: {e}")
            return False

    def login(self) -> bool:
        """Convenience wrapper for auto_login."""
        success, message = self.auto_login()
        if success:
            logger.info(message)
        else:
            logger.error(message)
        return success

    def get_limits(self) -> Optional[Dict[str, Any]]:
        """Get trading limits/margins."""
        if not self.client:
            return None
        try:
            return self.client.limits()
        except Exception as e:
            logger.error(f"Failed to get limits: {e}")
            return None

    def get_positions(self) -> Optional[Dict[str, Any]]:
        """Get current positions."""
        if not self.client:
            return None
        try:
            return self.client.positions()
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return None

    def get_order_report(self) -> Optional[Dict[str, Any]]:
        """Get order report for today."""
        if not self.client:
            return None
        try:
            return self.client.order_report()
        except Exception as e:
            logger.error(f"Failed to get order report: {e}")
            return None
