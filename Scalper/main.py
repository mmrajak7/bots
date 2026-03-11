"""
Kayal - Trading Terminal

High-speed GUI-based trading terminal for Kotak NEO API.
Optimized for intraday options trading with one-click execution.
"""

import sys
import os
import yaml
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QApplication, QSplashScreen, QMessageBox
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QFont


def sync_from_pi():
    """Download latest Kite token files from Pi via Google Drive (async)."""
    # S38: Use relative path from project root instead of hardcoded absolute
    script_path = Path(__file__).resolve().parent.parent / "data" / "sync" / "src" / "gdrive_download.py"
    if not script_path.exists():
        logging.debug(f"[SYNC] Sync script not found: {script_path}")
        return

    process = None
    try:
        # Use Popen for better timeout/kill handling
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate(timeout=30)

        if process.returncode != 0:
            logging.warning(f"[SYNC] Pi sync warning: {stderr.strip() if stderr else 'unknown error'}")
        else:
            logging.info("[SYNC] Pi sync complete - Kite token updated")

    except subprocess.TimeoutExpired:
        logging.warning("[SYNC] Pi sync timed out after 30s - killing process")
        if process:
            process.kill()
            process.wait()  # Clean up zombie
    except Exception as e:
        logging.warning(f"[SYNC] Pi sync failed: {e}")
        if process and process.poll() is None:
            process.kill()
            process.wait()


def setup_logging():
    """Setup application logging."""
    log_dir = 'logs'
    os.makedirs(log_dir, exist_ok=True)

    log_file = os.path.join(log_dir, f"terminal_{datetime.now().strftime('%Y%m%d')}.log")

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()
        ]
    )

    return logging.getLogger(__name__)


def load_config():
    """Load configuration from YAML file."""
    config_path = 'config/settings.yaml'

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)

    return config


def create_credentials_config():
    """Create credentials config file from template if needed."""
    creds_path = 'config/credentials.yaml'

    if not os.path.exists(creds_path):
        template = """# Kayal - Credentials
# DO NOT commit this file to git!

neo_credentials:
  consumer_key: "YOUR_CONSUMER_KEY"
  mobile_number: "+91XXXXXXXXXX"
  ucc: "YOUR_UCC"
  mpin: "XXXXXX"
  totp_secret: "YOUR_TOTP_SECRET"

kite_credentials:
  api_key: "YOUR_KITE_API_KEY"
  access_token: ""  # Will be read from session file

telegram:
  enabled: false
  bot_token: "YOUR_BOT_TOKEN"
  chat_id: "YOUR_CHAT_ID"
"""
        with open(creds_path, 'w') as f:
            f.write(template)

        return None

    with open(creds_path, 'r') as f:
        return yaml.safe_load(f)


def merge_configs(base_config, creds_config):
    """Merge credentials into base config."""
    if creds_config:
        for key in ['neo_credentials', 'kite_credentials', 'telegram']:
            if key in creds_config:
                base_config[key] = creds_config[key]
    return base_config


def main():
    """Main application entry point."""
    # Setup logging first
    logger = setup_logging()
    logger.info("=" * 50)
    logger.info("Kayal v1.0 Starting...")
    logger.info("=" * 50)

    # ASYNC: Sync Kite token from Pi via Google Drive (runs in background)
    sync_thread = threading.Thread(target=sync_from_pi, daemon=True, name="PiSync")
    sync_thread.start()
    logger.info("Background sync started...")

    # Create Qt Application
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    # Set default font
    font = QFont("Segoe UI", 9)
    app.setFont(font)

    try:
        # Load configuration
        logger.info("Loading configuration...")
        config = load_config()

        # Load and merge credentials
        creds = create_credentials_config()
        if creds is None:
            QMessageBox.warning(
                None, "Configuration Required",
                "Please configure your credentials in config/credentials.yaml\n"
                "A template file has been created."
            )
            logger.warning("Credentials not configured")

        config = merge_configs(config, creds)

        # S38: Validate critical config keys exist
        missing_keys = []
        neo_creds = config.get('neo_credentials', {})
        if not neo_creds.get('consumer_key') or neo_creds['consumer_key'] == 'YOUR_CONSUMER_KEY':
            missing_keys.append('neo_credentials.consumer_key')
        if not neo_creds.get('totp_secret') or neo_creds['totp_secret'] == 'YOUR_TOTP_SECRET':
            missing_keys.append('neo_credentials.totp_secret')
        if not neo_creds.get('mobile_number'):
            missing_keys.append('neo_credentials.mobile_number')
        if not neo_creds.get('mpin'):
            missing_keys.append('neo_credentials.mpin')

        if missing_keys:
            logger.warning(f"Missing/placeholder credentials: {', '.join(missing_keys)}")

        # Validate trading config has reasonable defaults
        trading_cfg = config.get('trading', {})
        if not trading_cfg:
            logger.warning("No 'trading' section in config - using defaults")
            config['trading'] = {}

        # Import core modules
        from core.session_manager import SessionManager
        from core.symbol_mapper import SymbolMapper
        from core.order_manager import OrderManager
        from core.kite_spot import KiteSpotFetcher, KiteWebSocket
        from core.sound_alerts import SoundAlertManager
        from core.telegram_notifier import TelegramNotifier
        from core.trade_logger import TradeLogger
        from core.trailing_sl import TrailingSLManager
        from core.oco_monitor import OCOMonitor
        from core.websocket_handler import WebSocketHandler
        from core.partial_fill_monitor import PartialFillMonitor
        from core.realized_pnl_tracker import RealizedPnLTracker
        from core.order_cancel_manager import OrderCancelManager
        from core.rejection_learner import RejectionLearner
        from gui.main_window import MainWindow

        # Initialize Session Manager
        logger.info("Initializing session...")
        session = SessionManager(config)

        # Auto-login
        success, msg = session.auto_login()
        logger.info(msg)

        if not success:
            reply = QMessageBox.question(
                None, "Login Failed",
                f"NEO login failed: {msg}\n\nDo you want to continue without NEO connection?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return 1

        # Initialize Symbol Mapper with mapping cache (fast if cached today)
        logger.info("Initializing symbol mapper...")
        mapper = SymbolMapper(session.get_client(), config)

        if session.is_connected():
            # Build Kite to NEO mapping cache - loads from cache if available (fast)
            # This is the slow part (~50s) when no cache exists
            cache_success, cache_msg = mapper.build_mapping_cache()
            logger.info(cache_msg)
            if not cache_success:
                logger.warning(f"Mapping cache warning: {cache_msg}")

            # Initialize scrip master (needed for order display, expiry lookups)
            # Has its own daily CSV cache - fast if cache exists
            success, msg = mapper.initialize()
            logger.info(msg)
            if not success:
                logger.warning(f"Symbol mapper initialization warning: {msg}")

        # Initialize Realized P&L Tracker
        logger.info("Initializing realized P&L tracker...")
        pnl_tracker = RealizedPnLTracker()

        # CRITICAL: Sync P&L tracker with broker positions on startup
        # This ensures circuit breaker works correctly for positions opened before app start
        if session.is_connected():
            try:
                logger.info("Syncing P&L tracker with broker positions...")
                positions_response = session.get_client().positions()
                broker_positions = positions_response.get('data', []) if positions_response else []
                if broker_positions:
                    pnl_tracker.sync_with_broker_positions(broker_positions)
                    logger.info(f"P&L tracker synced: {len(pnl_tracker.positions)} positions tracked")
                else:
                    logger.info("No broker positions to sync")
            except Exception as e:
                logger.warning(f"Failed to sync P&L tracker with broker: {e}")

        # Initialize Order Cancel Manager (prevents race conditions)
        logger.info("Initializing order cancel manager...")
        cancel_mgr = OrderCancelManager(session.get_client()) if session.is_connected() else None

        # Initialize Rejection Learner (learns from order rejections)
        logger.info("Initializing rejection learner...")
        rejection_learner = RejectionLearner(data_dir="data")

        # Initialize auxiliary modules (before OrderManager - needed for critical alerts)
        logger.info("Initializing auxiliary modules...")
        sound_mgr = SoundAlertManager(config)
        telegram_mgr = TelegramNotifier(config)
        trade_logger = TradeLogger(config)

        # Initialize Order Manager with P&L tracker and telegram for critical alerts
        logger.info("Initializing order manager...")
        order_mgr = OrderManager(session.get_client(), config, mapper, pnl_tracker, telegram_mgr)

        # Initialize Kite Spot Fetcher (pass mapper for expiry date lookups)
        logger.info("Initializing Kite spot fetcher...")
        kite_spot = KiteSpotFetcher(config, symbol_mapper=mapper)
        success, msg = kite_spot.connect()
        logger.info(msg)
        if not success:
            logger.warning("Kite not connected - ATM presets will not work")

        # Initialize Kite WebSocket for reliable LTP streaming (NEO WS is unreliable)
        kite_ws = None
        if kite_spot.is_connected():
            logger.info("Initializing Kite WebSocket for LTP streaming...")
            kite_ws = KiteWebSocket(config)
            if kite_ws.connect():
                logger.info("Kite WebSocket: Connecting...")
            else:
                logger.warning("Kite WebSocket: Connection failed")
                kite_ws = None

        # Initialize trailing SL manager
        trail_mgr = TrailingSLManager(
            session.get_client(),
            order_mgr,
            sound_mgr,
            telegram_mgr,
            config
        )

        # Initialize OCO monitor with P&L tracker and cancel manager
        oco_monitor = OCOMonitor(
            session.get_client(),
            telegram_mgr,
            sound_mgr,
            pnl_tracker,
            cancel_mgr
        )

        # Initialize Partial Fill Monitor
        logger.info("Initializing partial fill monitor...")
        partial_fill_monitor = PartialFillMonitor(
            neo_client=session.get_client(),
            order_manager=order_mgr,
            position_tracker=None,  # Will be set after MainWindow creates it
            oco_monitor=oco_monitor,
            trail_manager=trail_mgr,
            telegram=telegram_mgr,
            sound=sound_mgr
        )

        # Initialize WebSocket handler for real-time order updates
        ws_handler = None
        if session.is_connected():
            logger.info("Initializing WebSocket handler...")
            ws_handler = WebSocketHandler(session.get_client())
            ws_handler.setup_callbacks()
            # Subscribe to order feed for instant updates
            if ws_handler.subscribe_order_feed():
                logger.info("WebSocket: Subscribed to order feed")
            else:
                logger.warning("WebSocket: Order feed subscription failed")

        # NOTE: Background monitors are started AFTER pos_tracker is wired up (see below)
        # This prevents race conditions where monitors access pos_tracker before it's set

        # Create main window
        logger.info("Creating main window...")
        window = MainWindow(
            session_mgr=session,
            order_mgr=order_mgr,
            symbol_mapper=mapper,
            kite_spot=kite_spot,
            config=config,
            sound_mgr=sound_mgr,
            telegram_mgr=telegram_mgr,
            trade_logger=trade_logger,
            trail_mgr=trail_mgr,
            oco_monitor=oco_monitor,
            ws_handler=ws_handler,
            partial_fill_monitor=partial_fill_monitor,
            pnl_tracker=pnl_tracker,
            cancel_mgr=cancel_mgr,
            rejection_learner=rejection_learner,
            kite_ws=kite_ws
        )

        # Connect partial fill monitor to position tracker (created in MainWindow)
        if partial_fill_monitor:
            partial_fill_monitor.pos_tracker = window.pos_tracker

        # Connect trail manager and OCO monitor to position tracker for race condition prevention
        if trail_mgr:
            trail_mgr.pos_tracker = window.pos_tracker
            # S38: Wire oco_monitor so trail updates propagate SL prices
            trail_mgr.oco_monitor = oco_monitor
        if oco_monitor:
            oco_monitor.set_position_tracker(window.pos_tracker)
            # Set LTP getter for target monitoring (avoids margin issues by not placing TARGET limit orders)
            oco_monitor.set_ltp_getter(window.get_ltp_for_symbol)

        # CRITICAL FIX: Start background monitors AFTER pos_tracker is wired up
        # This prevents race conditions where monitors access pos_tracker before it's set
        if session.is_connected():
            trail_mgr.start_auto_trail()
            oco_monitor.start()
            partial_fill_monitor.start()
            logger.info("Background monitors started: trail, OCO, partial fill")

        # CRITICAL: Start periodic position reconciliation and session health check
        # This ensures terminal state matches broker state AND session stays alive
        def periodic_reconciliation():
            """Background thread for periodic position reconciliation and session health."""
            import time as time_module
            reconciliation_interval = 300  # 5 minutes
            session_check_counter = 0

            while True:
                time_module.sleep(reconciliation_interval)
                session_check_counter += 1

                try:
                    # CRITICAL: Check and refresh session every 30 minutes (6 x 5min intervals)
                    # This prevents session expiry during trading
                    if session_check_counter >= 6:  # Every 30 minutes
                        session_check_counter = 0
                        success, msg = session.refresh_if_needed()
                        if success:
                            logger.info(f"[SESSION] Health check: {msg}")
                        else:
                            logger.error(f"[SESSION] Refresh failed: {msg}")
                            if telegram_mgr and telegram_mgr.enabled:
                                telegram_mgr.send(f"⚠️ SESSION WARNING: {msg}")

                    if not session.is_connected():
                        logger.warning("[RECONCILIATION] Session not connected, skipping")
                        continue

                    positions_response = session.get_client().positions()
                    if not positions_response or not isinstance(positions_response, dict):
                        logger.warning("[RECONCILIATION] Broker positions() returned invalid response — skipping cycle")
                        continue
                    broker_positions = positions_response.get('data', []) or []

                    # Sync P&L tracker
                    if pnl_tracker:
                        pnl_tracker.sync_with_broker_positions(broker_positions)

                    # Sync position tracker (removes closed positions, cancels orphan orders)
                    if window.pos_tracker:
                        window.pos_tracker.sync_with_broker_positions(broker_positions)

                    logger.debug(f"[RECONCILIATION] Periodic sync completed: {len(broker_positions)} broker positions")

                except Exception as e:
                    logger.warning(f"[RECONCILIATION] Periodic sync failed: {e}")

        if session.is_connected():
            recon_thread = threading.Thread(target=periodic_reconciliation, daemon=True, name="PositionReconciler")
            recon_thread.start()
            logger.info("Periodic position reconciliation started (every 5 minutes)")

        window.show()
        logger.info("Application ready")

        # Startup notification disabled - too noisy
        # if telegram_mgr.enabled:
        #     telegram_mgr.send("Kayal v1.0 started")

        # Run the application
        return app.exec()

    except FileNotFoundError as e:
        logger.error(f"Configuration error: {e}")
        QMessageBox.critical(None, "Configuration Error", str(e))
        return 1

    except ImportError as e:
        logger.error(f"Import error: {e}")
        QMessageBox.critical(
            None, "Import Error",
            f"Failed to import required module: {e}\n\n"
            "Please install dependencies: pip install -r requirements.txt"
        )
        return 1

    except Exception as e:
        logger.error(f"Startup error: {e}", exc_info=True)
        QMessageBox.critical(None, "Startup Error", f"Failed to start: {str(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
