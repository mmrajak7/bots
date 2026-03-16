"""Position store — CRUD + Drive sync for flat equity positions.

Flat 5L model: one entry per stock, no pyramid levels.
SL: touch month low -> ATR(1.5x) trail, only moves up.

Mirrors zerodha/watchlist.py pattern: local-first, Drive-secondary, atomic writes,
version-based merge, singleton access via get_pyramid_store().
"""

import json
import logging
import os
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()          # playbook/pyramid/
PLAYBOOK_DIR = SCRIPT_DIR.parent                      # playbook/
PROJECT_ROOT = PLAYBOOK_DIR.parent                    # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_FILE = LOG_DIR / 'pyramid_positions.json'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'pyramid_config.json'


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        logger.warning("Config %s not found, using defaults", CONFIG_FILE)
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    env_path = os.environ.get('PYRAMID_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


REQUIRED_FIELDS = [
    'symbol', 'spot_symbol', 'sector', 'thesis',
]


class PyramidStore:
    """Manages flat equity positions with local JSON + Google Drive sync."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._positions: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )

    # ── Initialization ─────────────────────────────────────────────────────

    def initialize(self):
        """Startup: auth Drive, download, fallback to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        active = sum(1 for p in self._positions
                     if p.get('status') == 'active')
        logger.info(
            "PyramidStore initialized: %d positions (%d active), drive=%s",
            len(self._positions), active,
            'enabled' if self._drive_enabled else 'disabled'
        )

    # ── Core Operations ────────────────────────────────────────────────────

    def load_positions(self) -> list:
        """Return all positions from cache (zero network)."""
        return self._positions

    def add_position(self, data: dict) -> dict:
        """Create a new flat position.

        Required in data: symbol, spot_symbol, sector, thesis, price, quantity.
        Optional: account_id (defaults to 'QSK814'), decision_id, touch_month_low.
        """
        # Validate required fields
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        if 'price' not in data or 'quantity' not in data:
            raise ValueError("price and quantity are required")

        if data['price'] <= 0 or data['quantity'] <= 0:
            raise ValueError("price and quantity must be positive")

        # Duplicate symbol check
        symbol = data['symbol']
        for p in self.get_active():
            if p['symbol'] == symbol:
                raise ValueError(
                    f"Active position already exists for {symbol} (#{p['id']})."
                )

        # Sector cap check
        sector = data['sector']
        max_per_sector = self._config.get('max_per_sector', 3)
        sector_count = self.count_sector(sector)
        if sector_count >= max_per_sector:
            raise ValueError(
                f"Sector cap reached: {sector} already has {sector_count} "
                f"active positions (max {max_per_sector})"
            )

        # Max positions check
        max_positions = self._config.get('max_positions', 25)
        active_count = len(self.get_active())
        if active_count >= max_positions:
            raise ValueError(
                f"Max positions reached: {active_count} active "
                f"(max {max_positions})"
            )

        entry_price = data['price']
        entry_qty = data['quantity']
        entry_amount = round(entry_price * entry_qty, 2)
        today = data.get('entry_date', datetime.now().strftime('%Y-%m-%d'))
        entry_month = today[:7]  # YYYY-MM

        position = {
            "id": self._next_id(),
            "version": 1,
            "status": "active",
            "symbol": data['symbol'],
            "spot_symbol": data['spot_symbol'],
            "account_id": data.get('account_id', 'QSK814'),
            "sector": sector,
            "entry_price": entry_price,
            "entry_quantity": entry_qty,
            "entry_amount": entry_amount,
            "current_sl": data.get('touch_month_low'),
            "sl_type": "atr_trail",
            "monthly_lows": {},
            "last_checked_month": None,
            "decision_id": data.get('decision_id'),
            "thesis": data['thesis'],
            "created_at": today,
            "entry_month": entry_month,
            "exit": None,
        }

        self._positions.append(position)
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Added position #%d: %s @%.2f x%d = Rs %.0f, sector=%s",
            position['id'], data['symbol'], entry_price, entry_qty,
            entry_amount, sector
        )
        return position

    def record_monthly_low(self, position_id: int, month: str,
                           low: float, persist: bool = True) -> dict:
        """Record a month's intraday low for data tracking.

        Sets last_checked_month. Does NOT modify current_sl.
        SL is set exclusively by the ATR trail computation in pyramid_checker.
        Pass persist=False during batch operations, then call flush() once.
        """
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")

        pos['monthly_lows'][month] = low
        pos['last_checked_month'] = month
        pos['version'] = pos.get('version', 0) + 1

        if persist:
            self._save_local()
            self._upload_to_drive()

        logger.info(
            "Position #%d %s: month %s low=%.2f recorded",
            position_id, pos['symbol'], month, low
        )
        return pos

    def update_peak(self, position_id: int, peak_price: float,
                    persist: bool = True) -> dict:
        """Update stored peak price (only moves up). Used for ATR trail.
        Pass persist=False during batch operations, then call flush() once.
        """
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")

        old_peak = pos.get('peak_price', 0) or 0
        new_peak = max(old_peak, peak_price)
        if new_peak != old_peak:
            pos['peak_price'] = round(new_peak, 2)
            pos['version'] = pos.get('version', 0) + 1
            if persist:
                self._save_local()
                self._upload_to_drive()
            logger.info(
                "Position #%d %s: peak %.2f -> %.2f",
                position_id, pos['symbol'], old_peak, new_peak
            )
        return pos

    def update_sl(self, position_id: int, new_sl: float,
                  persist: bool = True) -> dict:
        """Set SL (from ATR trail or manual override).
        Pass persist=False during batch operations, then call flush() once.
        """
        if new_sl <= 0:
            raise ValueError(f"SL must be positive, got {new_sl}")
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")

        old_sl = pos['current_sl']
        pos['current_sl'] = round(new_sl, 2)
        pos['version'] = pos.get('version', 0) + 1

        if persist:
            self._save_local()
            self._upload_to_drive()

        logger.info(
            "Position #%d %s: SL set %.2f -> %.2f",
            position_id, pos['symbol'], old_sl or 0, new_sl
        )
        return pos

    def flush(self):
        """Persist current in-memory state to local + Drive.
        Call after a batch of persist=False operations.
        """
        self._save_local()
        self._upload_to_drive()

    def close_position(self, position_id: int, exit_price: float,
                       reason: str) -> dict:
        """Mark position as exited, compute realized P&L."""
        if exit_price <= 0:
            raise ValueError(f"Exit price must be positive, got {exit_price}")
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")
        if pos['status'] == 'exited':
            raise ValueError(f"Position #{position_id} is already exited")

        qty = pos.get('entry_quantity') or pos.get('total_quantity', 0)
        amt = pos.get('entry_amount') or pos.get('total_invested', 0)
        total_exit = round(exit_price * qty, 2)
        realized_pnl = round(total_exit - amt, 2)
        pnl_pct = round(
            (realized_pnl / amt) * 100, 2
        ) if amt > 0 else 0

        pos['status'] = 'exited'
        pos['exit'] = {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "price": exit_price,
            "total_exit_value": total_exit,
            "realized_pnl": realized_pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
        }
        pos['version'] = pos.get('version', 0) + 1

        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Position #%d %s CLOSED: exit=%.2f, P&L=Rs %.0f (%.1f%%), reason=%s",
            position_id, pos['symbol'], exit_price, realized_pnl, pnl_pct, reason
        )
        return pos

    def get_active(self) -> list:
        """Positions still holding."""
        return [p for p in self._positions if p.get('status') == 'active']

    def count_sector(self, sector: str) -> int:
        """Count active positions in a sector."""
        return sum(
            1 for p in self.get_active()
            if p.get('sector', '').lower() == sector.lower()
        )

    def list_positions(self, status_filter: Optional[str] = None):
        """Print formatted position table."""
        positions = self._positions
        if status_filter:
            positions = [p for p in positions if p['status'] == status_filter]

        if not positions:
            print("No positions found.")
            return

        # Header
        print(f"\n{'ID':>3} {'Symbol':<12} {'Status':<8} {'Sector':<10} "
              f"{'Entry':>10} {'Qty':>6} {'Amount':>10} "
              f"{'SL':>10} {'Thesis':<30}")
        print("-" * 110)

        for p in positions:
            sl_str = f"{p['current_sl']:.2f}" if p.get('current_sl') else "-"
            thesis = (p.get('thesis', '')[:28] + '..') if len(p.get('thesis', '')) > 30 else p.get('thesis', '')
            # Handle legacy positions (pre-flat format)
            entry_price = p.get('entry_price') or p.get('avg_cost', 0)
            entry_qty = p.get('entry_quantity') or p.get('total_quantity', 0)
            entry_amt = p.get('entry_amount') or p.get('total_invested', 0)

            print(f"{p['id']:>3} {p['symbol']:<12} {p['status']:<8} "
                  f"{p.get('sector', '-'):<10} {entry_price:>10,.2f} "
                  f"{entry_qty:>6} {entry_amt:>10,.0f} "
                  f"{sl_str:>10} {thesis:<30}")

            if p.get('exit'):
                ex = p['exit']
                print(f"     EXIT: {ex['date']} @ {ex['price']:.2f} "
                      f"P&L: Rs {ex['realized_pnl']:,.0f} ({ex['pnl_pct']:.1f}%) "
                      f"- {ex['reason']}")
        print()

    # ── Sync ───────────────────────────────────────────────────────────────

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale."""
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Private: ID Assignment ─────────────────────────────────────────────

    def _next_id(self) -> int:
        if not self._positions:
            return 1
        return max(p['id'] for p in self._positions) + 1

    def _find(self, position_id: int) -> Optional[dict]:
        for p in self._positions:
            if p['id'] == position_id:
                return p
        return None

    # ── Private: Drive Integration ─────────────────────────────────────────

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found at %s, local-only", creds_path)
            return

        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'pyramid_positions.json')
            self._drive_file_id = find_file(
                self._drive_service, folder_id, file_name
            )
            self._drive_enabled = True
            logger.info("Drive enabled, file_id=%s", self._drive_file_id)
        except Exception as e:
            logger.warning("Drive init failed: %s. Local-only.", e)

    def _sync_from_drive(self):
        try:
            from bcs.drive_store import download_json
            if self._drive_file_id:
                drive_data = download_json(
                    self._drive_service, self._drive_file_id
                )
                base = self._positions if self._positions else self._read_local()
                merged = self._merge(base, drive_data)
                self._positions = merged
                self._save_local()
                self._last_sync_time = time.time()

                # Re-upload if merge diverged
                drive_vers = {p['id']: p.get('version', 0) for p in drive_data}
                merged_vers = {p['id']: p.get('version', 0) for p in merged}
                if drive_vers != merged_vers:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                logger.info("No positions file on Drive yet, loading local")
                self._load_local()
        except Exception as e:
            logger.warning("Drive sync failed: %s. Using local.", e)
            self._load_local()

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'pyramid_positions.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._positions, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local is safe.", e)

    # ── Private: Local File ────────────────────────────────────────────────

    def _read_local(self) -> list:
        if not LOCAL_FILE.exists():
            return []
        try:
            with open(LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = LOCAL_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical("File CORRUPT (%s). Backed up to %s.", e, backup)
            return []

    def _load_local(self):
        self._positions = self._read_local()
        if self._positions:
            logger.info("Loaded %d positions from local", len(self._positions))
        else:
            logger.info("No local positions file, starting empty")

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        """Per-position ID, higher version wins."""
        by_id = {}
        for p in base:
            by_id[p['id']] = p
        for p in incoming:
            pid = p['id']
            if pid not in by_id:
                by_id[pid] = p
            elif p.get('version', 0) > by_id[pid].get('version', 0):
                by_id[pid] = p
        return sorted(by_id.values(), key=lambda p: p['id'])

    def _save_local(self):
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='pyramid_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._positions, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_FILE))
            tmp_path = None
        except Exception:
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise


# ── Singleton ──────────────────────────────────────────────────────────────

_store: Optional[PyramidStore] = None


def get_pyramid_store() -> PyramidStore:
    """Get or create the singleton PyramidStore."""
    global _store
    if _store is None:
        _store = PyramidStore()
        _store.initialize()
    return _store
