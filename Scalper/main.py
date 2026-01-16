"""
Kayal - Trading Terminal

High-speed GUI-based trading terminal for Kotak NEO API.
Optimized for intraday options trading with one-click execution.
"""

import sys
import os
import yaml
import logging
from datetime import datetime

from PyQt6.QtWidgets import QApplication, QSplashScreen, QMessageBox
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap, QFont


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

        # Import core modules
        from core.session_manager import SessionManager
        from core.symbol_mapper import SymbolMapper
        from core.order_manager import OrderManager
        from core.kite_spot import KiteSpotFetcher
        from core.sound_alerts import SoundAlertManager
        from core.telegram_notifier import TelegramNotifier
        from core.trade_logger import TradeLogger
        from core.trailing_sl import TrailingSLManager
        from core.oco_monitor import OCOMonitor
        from core.websocket_handler import WebSocketHandler
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

        # Initialize Symbol Mapper
        logger.info("Initializing symbol mapper...")
        mapper = SymbolMapper(session.get_client(), config)
        success, msg = mapper.initialize()
        logger.info(msg)

        if not success:
            logger.warning(f"Symbol mapper initialization warning: {msg}")

        # Build Kite to NEO mapping cache for fast lookups during trading
        if session.is_connected():
            logger.info("Building Kite to NEO mapping cache...")
            cache_success, cache_msg = mapper.build_mapping_cache()
            logger.info(cache_msg)
            if not cache_success:
                logger.warning(f"Mapping cache warning: {cache_msg}")

        # Initialize Order Manager
        logger.info("Initializing order manager...")
        order_mgr = OrderManager(session.get_client(), config, mapper)

        # Initialize Kite Spot Fetcher
        logger.info("Initializing Kite spot fetcher...")
        kite_spot = KiteSpotFetcher(config)
        success, msg = kite_spot.connect()
        logger.info(msg)
        if not success:
            logger.warning("Kite not connected - ATM presets will not work")

        # Initialize auxiliary modules
        logger.info("Initializing auxiliary modules...")
        sound_mgr = SoundAlertManager(config)
        telegram_mgr = TelegramNotifier(config)
        trade_logger = TradeLogger(config)

        # Initialize trailing SL manager
        trail_mgr = TrailingSLManager(
            session.get_client(),
            order_mgr,
            sound_mgr,
            telegram_mgr,
            config
        )

        # Initialize OCO monitor
        oco_monitor = OCOMonitor(
            session.get_client(),
            telegram_mgr,
            sound_mgr
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

        # Start background monitors
        if session.is_connected():
            trail_mgr.start_auto_trail()
            oco_monitor.start()

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
            ws_handler=ws_handler
        )

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
