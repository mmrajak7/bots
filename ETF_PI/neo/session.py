"""
neo.session — Kotak Neo API session manager with TOTP auto-login.

Handles: fresh login, session caching, restore-on-restart, proactive refresh.
Uses Neo API v2: consumer_key + totp_login + totp_validate + session_2fa.

Adapted from Scalper core/session_manager.py.
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Tuple, Optional

# Force IPv4 (same fix as Kite bots, 2026-07-23): home machines are dual-stack
# with rotating IPv6; if the broker ever enforces a static-IP whitelist it will
# hold the shared home IPv4, and IPv6 requests would be rejected at the order
# moment. Neo has no known IP gate today — this is defensive consistency.
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4_only_getaddrinfo

import pyotp
import pytz

log = logging.getLogger('neo.session')
IST = pytz.timezone('Asia/Kolkata')


class SessionManager:
    """Manages Neo API session with auto-TOTP login."""

    def __init__(self, config, data_dir):
        """
        Args:
            config: NeoConfig instance (has consumer_key, mobile, ucc, mpin, totp_secret).
            data_dir: Path to neo/data/ for session cache.
        """
        self.config = config
        self.client = None
        self._session_file = os.path.join(data_dir, 'session.json')
        self._valid_until = None
        self._connected = False

        os.makedirs(data_dir, exist_ok=True)

    def auto_login(self):
        """Auto-login: restore cached session or fresh TOTP login.

        Returns:
            Tuple of (success: bool, message: str).
        """
        # Try cached session first
        if self._restore_session():
            self._connected = True
            return True, "Session restored from cache"

        # Fresh login
        try:
            from neo_api_client import NeoAPI

            if not self.config.consumer_key:
                return False, "consumer_key not configured"

            self.client = NeoAPI(
                environment='prod',
                access_token=None,
                neo_fin_key=None,
                consumer_key=self.config.consumer_key,
            )

            # Generate TOTP
            if not self.config.totp_secret:
                return False, "totp_secret not configured"
            totp = pyotp.TOTP(self.config.totp_secret)
            current_totp = totp.now()
            log.info(f"TOTP generated: {current_totp[:2]}****")

            # Step 1: TOTP login
            if not self.config.mobile_number or not self.config.ucc:
                return False, "mobile_number or ucc not configured"

            self.client.totp_login(
                mobile_number=self.config.mobile_number,
                ucc=self.config.ucc,
                totp=current_totp,
            )
            log.info("TOTP login sent")

            # Step 2: MPIN validation
            if not self.config.mpin:
                return False, "mpin not configured"
            self.client.totp_validate(mpin=self.config.mpin)
            log.info("MPIN validated")

            # Step 3: Session 2FA (if available)
            if hasattr(self.client, 'session_2fa'):
                self.client.session_2fa()
                log.info("Session 2FA complete")

            self._valid_until = datetime.now(IST) + timedelta(hours=8)
            self._save_session()
            self._connected = True
            return True, f"Login successful at {datetime.now(IST).strftime('%H:%M:%S')}"

        except Exception as e:
            log.error(f"Login failed: {e}", exc_info=True)
            return False, f"Login failed: {e}"

    def refresh_if_needed(self):
        """Check session health, re-login if needed.

        Returns:
            Tuple of (success: bool, message: str).
        """
        if not self._connected:
            return self.auto_login()

        # Re-login if expiring within 1 hour
        if self._valid_until:
            remaining = (self._valid_until - datetime.now(IST)).total_seconds()
            if remaining < 3600:
                log.info("Session expiring soon, refreshing...")
                return self.auto_login()

        # Verify session is actually working
        if not self._verify():
            log.warning("Session verification failed, re-logging...")
            return self.auto_login()

        return True, "Session healthy"

    def get_client(self):
        """Return the NeoAPI client instance (or None)."""
        return self.client

    def is_connected(self):
        return self._connected and self.client is not None

    # ---- Internal ----

    def _save_session(self):
        try:
            data = {
                'date': datetime.now(IST).strftime('%Y-%m-%d'),
                'valid_until': self._valid_until.isoformat() if self._valid_until else '',
                'timestamp': datetime.now(IST).isoformat(),
            }
            if self.client and hasattr(self.client, '_access_token'):
                data['access_token'] = self.client._access_token
            with open(self._session_file, 'w') as f:
                json.dump(data, f, indent=2)
            log.info("Session cached")
        except Exception as e:
            log.warning(f"Session save failed: {e}")

    def _restore_session(self):
        if not os.path.exists(self._session_file):
            return False
        try:
            with open(self._session_file) as f:
                data = json.load(f)

            # Must be from today
            if data.get('date', '') != datetime.now(IST).strftime('%Y-%m-%d'):
                log.info("Session from previous day, needs fresh login")
                return False

            # Must not be expired
            valid_str = data.get('valid_until', '')
            if valid_str:
                valid_until = datetime.fromisoformat(valid_str)
                if datetime.now(IST) >= valid_until:
                    log.info("Session expired")
                    return False
            else:
                return False

            token = data.get('access_token')
            if not token:
                return False

            from neo_api_client import NeoAPI
            self.client = NeoAPI(
                environment='prod',
                access_token=token,
                neo_fin_key=None,
                consumer_key=self.config.consumer_key,
            )

            if self._verify():
                self._valid_until = valid_until
                log.info("Session restored from cache")
                return True

            return False
        except Exception as e:
            log.warning(f"Session restore failed: {e}")
            return False

    def _verify(self):
        """Quick API call to verify session is active."""
        try:
            resp = self.client.limits()
            if not resp:
                return False
            if (resp.get('error') or resp.get('Error') or
                    str(resp.get('stat', '')).lower() in ('not_ok', 'error')):
                return False
            return True
        except Exception as e:
            log.warning(f"Session verify failed: {e}")
            return False
