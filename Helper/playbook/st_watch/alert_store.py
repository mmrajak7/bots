"""ST Watch alert history — append-only log with Google Drive sync.

Every alert sent via Telegram is recorded here. Provides a queryable
history of all ST touch alerts for Core & Tactical baskets.

Reuses bcs.drive_store for Drive operations (no duplication).
"""

import json
import logging
import os
import platform
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytz

from . import config as cfg

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')


def _resolve_credentials_path(drive_cfg: dict) -> Optional[Path]:
    """Resolve Google credentials path (env var > platform-specific config)."""
    env_path = os.environ.get('ST_WATCH_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class AlertStore:
    """Append-only alert history log with Drive sync."""

    def __init__(self):
        self._alerts: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._drive_cfg: dict = {}
        self._initialized = False

    def initialize(self):
        """Load from local, init Drive sync."""
        if self._initialized:
            return

        # Load config
        full_cfg = cfg.load_config()
        self._drive_cfg = full_cfg.get('google_drive', {}) if isinstance(full_cfg, dict) else {}

        # Load local first
        self._load_local()

        # Init Drive
        if self._drive_cfg.get('enabled', False):
            self._init_drive()
            if self._drive_enabled:
                self._sync_from_drive()

        self._initialized = True
        logger.info("AlertStore: %d alerts loaded, drive=%s",
                     len(self._alerts),
                     'enabled' if self._drive_enabled else 'disabled')

    def _init_drive(self):
        """Authenticate with Google Drive."""
        try:
            from bcs.drive_store import get_drive_service, find_file
            creds_path = _resolve_credentials_path(self._drive_cfg)
            if not creds_path or not creds_path.exists():
                logger.warning("Drive credentials not found, running local-only")
                return

            self._drive_service = get_drive_service(creds_path)
            folder_id = self._drive_cfg.get('folder_id', '')
            file_name = self._drive_cfg.get('file_name', 'st_watch_alerts.json')
            self._drive_file_id = find_file(self._drive_service, folder_id, file_name)
            self._drive_enabled = True
            logger.info("Drive connected (file_id=%s)", self._drive_file_id or 'new')
        except Exception as e:
            logger.warning("Drive init failed, running local-only: %s", e)

    def _load_local(self):
        """Load alerts from local JSON file."""
        if not cfg.ALERT_LOG_FILE.exists():
            self._alerts = []
            return
        try:
            with open(cfg.ALERT_LOG_FILE) as f:
                self._alerts = json.load(f)
        except Exception as e:
            logger.warning("Failed to load local alerts: %s", e)
            self._alerts = []

    def _save_local(self):
        """Save alerts to local JSON (atomic write)."""
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cfg.ALERT_LOG_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(self._alerts, f, indent=2, default=str)
        os.replace(str(tmp), str(cfg.ALERT_LOG_FILE))

    def _sync_from_drive(self):
        """Download from Drive and merge with local (Drive wins on conflict)."""
        if not self._drive_enabled or not self._drive_file_id:
            return
        try:
            from bcs.drive_store import download_json
            remote = download_json(self._drive_service, self._drive_file_id)
            if remote:
                # Merge: use higher ID set as source of truth
                local_ids = {a['id'] for a in self._alerts}
                remote_ids = {a['id'] for a in remote}

                # Add any remote alerts not in local
                for alert in remote:
                    if alert['id'] not in local_ids:
                        self._alerts.append(alert)

                # Sort by ID
                self._alerts.sort(key=lambda a: a['id'])
                self._save_local()
                logger.info("Synced from Drive: %d local + %d remote merged",
                             len(local_ids), len(remote_ids - local_ids))
        except Exception as e:
            logger.warning("Drive sync failed: %s", e)

    def _upload_to_drive(self):
        """Upload local alerts to Drive. Best-effort."""
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            folder_id = self._drive_cfg.get('folder_id', '')
            file_name = self._drive_cfg.get('file_name', 'st_watch_alerts.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._alerts, file_id=self._drive_file_id
            )
        except Exception as e:
            logger.warning("Drive upload failed: %s", e)

    def _next_id(self) -> int:
        if not self._alerts:
            return 1
        return max((a.get('id', 0) for a in self._alerts), default=0) + 1

    # ── Public API ────────────────────────────────────────────────────────

    def log_alert(self, symbol: str, basket: str, timeframe: str,
                  ltp: float, st_val: float, gap_pct: float,
                  direction: str, threshold: int,
                  prev_gap: float = None) -> dict:
        """Record an alert that was sent. Returns the alert entry."""
        alert = {
            'id': self._next_id(),
            'timestamp': datetime.now(IST).isoformat(),
            'symbol': symbol,
            'basket': basket,
            'timeframe': timeframe,
            'ltp': round(ltp, 2),
            'st': round(st_val, 2),
            'gap_pct': round(gap_pct, 2),
            'direction': direction,
            'threshold': threshold,
            'prev_gap': round(prev_gap, 2) if prev_gap is not None else None,
        }
        self._alerts.append(alert)
        self._save_local()
        self._upload_to_drive()
        return alert

    def get_alerts(self, symbol: str = None, days: int = None,
                   timeframe: str = None) -> list:
        """Query alert history with optional filters."""
        results = self._alerts

        if symbol:
            results = [a for a in results if a['symbol'].upper() == symbol.upper()]

        if timeframe:
            results = [a for a in results if a['timeframe'] == timeframe]

        if days:
            cutoff = (datetime.now(IST) - timedelta(days=days)).isoformat()
            results = [a for a in results if a['timestamp'] >= cutoff]

        return results

    def list_alerts(self, days: int = 7, symbol: str = None):
        """Print alert history table."""
        alerts = self.get_alerts(symbol=symbol, days=days)

        if not alerts:
            print(f"No alerts in the last {days} days"
                  + (f" for {symbol}" if symbol else ""))
            return

        print(f"\n{'='*85}")
        print(f"  Support Level Alert History (last {days} days"
              + (f", {symbol}" if symbol else "") + ")")
        print(f"{'='*85}")
        print(f"  {'ID':>4} {'Timestamp':<20} {'Symbol':<14} {'TF':<8} "
              f"{'LTP':>9} {'Support':>9} {'Gap%':>7} {'Thr':>4} {'Dir':>5}")
        print(f"  {'-'*81}")

        for a in alerts:
            ts = a['timestamp'][:16].replace('T', ' ')
            print(f"  {a['id']:>4} {ts:<20} {a['symbol']:<14} {a['timeframe']:<8} "
                  f"{a['ltp']:>9,.2f} {a['st']:>9,.2f} {a['gap_pct']:>+6.1f}% "
                  f"{a['threshold']:>3}% {a['direction']:>5}")

        print(f"\n  Total: {len(alerts)} alerts")
        print()


# ── Singleton ────────────────────────────────────────────────────────────

_store: Optional[AlertStore] = None


def get_alert_store() -> AlertStore:
    """Get singleton AlertStore instance (initializes on first call)."""
    global _store
    if _store is None:
        _store = AlertStore()
        _store.initialize()
    return _store
