"""
neo_login.py — Standalone daily Neo API login script.

Performs TOTP-based login to Kotak Neo API and caches the session token.
When option_buy.py starts later, SessionManager._restore_session() finds
the cached token and skips re-login.

Cron: 45 8 * * 1-5 python3 /path/to/neo_login.py >> /path/to/logs/neo_login.log 2>&1
"""

import os
import sys
import logging
from datetime import datetime
import pytz

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from neo.config import NeoConfig
from neo.session import SessionManager

IST = pytz.timezone('Asia/Kolkata')

LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(LOG_DIR, 'neo_login.log'), mode='a'),
    ],
)
log = logging.getLogger('neo_login')


def main():
    now = datetime.now(IST)
    log.info(f"=== Neo daily login — {now.strftime('%Y-%m-%d %H:%M:%S')} ===")

    cfg = NeoConfig()

    if not cfg.enabled:
        log.info("trade_enabled=false — login skipped (signal-only mode)")
        return 0

    if cfg.log_only:
        log.info("log_only=true — login skipped (no live orders)")
        return 0

    # Check required credentials
    missing = [f for f in ('consumer_key', 'mobile_number', 'ucc', 'mpin', 'totp_secret')
               if not getattr(cfg, f)]
    if missing:
        log.error(f"Missing credentials: {', '.join(missing)}")
        return 1

    session = SessionManager(cfg, cfg.data_dir)
    ok, msg = session.auto_login()

    if ok:
        log.info(f"LOGIN OK: {msg}")
        # Verify the session is actually usable
        if session.is_connected():
            log.info("Session verified — cached for option_buy.py")
            return 0
        else:
            log.error("Login reported success but session not connected")
            return 1
    else:
        log.error(f"LOGIN FAILED: {msg}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
