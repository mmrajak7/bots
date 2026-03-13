"""Pyramid position store — CRUD + Drive sync for 3-level scaling positions.

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
    """Manages pyramid positions with local JSON + Google Drive sync."""

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
                     if p.get('status') in ('active', 'completed'))
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
        """Create a new pyramid position with Level 1 filled.

        Required in data: symbol, spot_symbol, sector, thesis, and L1 price + quantity.
        Also requires: price (L1 fill price), quantity (L1 shares bought).
        Optional: target_amount (defaults to config max_per_position = Rs 10L),
                  account_id (defaults to 'QSK814'), decision_id.
        """
        # Validate required fields
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        if 'price' not in data or 'quantity' not in data:
            raise ValueError("L1 price and quantity are required")

        if data['price'] <= 0 or data['quantity'] <= 0:
            raise ValueError("price and quantity must be positive")

        # Duplicate symbol check — can't pyramid same stock twice
        symbol = data['symbol']
        for p in self.get_active():
            if p['symbol'] == symbol:
                raise ValueError(
                    f"Active pyramid already exists for {symbol} (#{p['id']}). "
                    f"Use 'fill' to add to existing position, not 'add'."
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
        max_positions = self._config.get('max_positions', 20)
        active_count = len(self.get_active())
        if active_count >= max_positions:
            raise ValueError(
                f"Max positions reached: {active_count} active "
                f"(max {max_positions})"
            )

        # Read level config
        level_config = self._config.get('levels', [
            {"pct": 30, "trigger_pct": 0},
            {"pct": 30, "trigger_pct": 8},
            {"pct": 40, "trigger_pct": 8},
        ])

        target_amount = data.get('target_amount',
                                 self._config.get('max_per_position', 1000000))
        l1_price = data['price']
        l1_qty = data['quantity']
        l1_amount = round(l1_price * l1_qty, 2)
        today = data.get('entry_date', datetime.now().strftime('%Y-%m-%d'))
        entry_month = today[:7]  # YYYY-MM

        # Build levels
        levels = []
        for i, lc in enumerate(level_config):
            level_num = i + 1
            pct = lc['pct']
            amount = round(target_amount * pct / 100, 2)
            trigger_pct = lc['trigger_pct']

            if level_num == 1:
                # L1 is filled at entry
                levels.append({
                    "level": 1,
                    "pct": pct,
                    "status": "filled",
                    "date": today,
                    "price": l1_price,
                    "amount": l1_amount,
                    "quantity": l1_qty,
                    "trigger_pct": trigger_pct,
                    "trigger_price": None,
                })
            elif level_num == 2:
                # L2 trigger = L1 price + trigger_pct%
                trigger_price = round(l1_price * (1 + trigger_pct / 100), 2)
                levels.append({
                    "level": level_num,
                    "pct": pct,
                    "status": "pending",
                    "date": None,
                    "price": None,
                    "amount": amount,
                    "quantity": None,
                    "trigger_pct": trigger_pct,
                    "trigger_price": trigger_price,
                })
            else:
                # L3+ trigger computed after L2 fills
                levels.append({
                    "level": level_num,
                    "pct": pct,
                    "status": "pending",
                    "date": None,
                    "price": None,
                    "amount": amount,
                    "quantity": None,
                    "trigger_pct": trigger_pct,
                    "trigger_price": None,
                })

        position = {
            "id": self._next_id(),
            "version": 1,
            "status": "active",
            "symbol": data['symbol'],
            "spot_symbol": data['spot_symbol'],
            "account_id": data.get('account_id', 'QSK814'),
            "sector": sector,
            "target_amount": target_amount,
            "levels": levels,
            "total_invested": l1_amount,
            "total_quantity": l1_qty,
            "avg_cost": l1_price,
            "current_sl": None,
            "sl_type": "monthly_low",
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
            "Added pyramid #%d: %s L1@%.2f x%d = Rs %.0f, sector=%s",
            position['id'], data['symbol'], l1_price, l1_qty,
            l1_amount, sector
        )
        return position

    def fill_level(self, position_id: int, level: int,
                   price: float, quantity: int,
                   date: Optional[str] = None) -> dict:
        """Record a level fill. Recomputes avg_cost, total_invested,
        total_quantity. Applies SL precedence (cost-cost floor after L2).
        Auto-computes next level's trigger_price.
        """
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")
        if pos['status'] == 'exited':
            raise ValueError(f"Position #{position_id} is already exited")

        if level < 1 or level > len(pos['levels']):
            raise ValueError(f"Invalid level {level}, position has {len(pos['levels'])} levels")

        lvl = pos['levels'][level - 1]
        if lvl['status'] == 'filled':
            raise ValueError(f"Level {level} is already filled")

        # Check levels are filled in order
        for i in range(level - 1):
            if pos['levels'][i]['status'] != 'filled':
                raise ValueError(
                    f"Cannot fill level {level}: level {i + 1} is not filled yet"
                )

        if price <= 0 or quantity <= 0:
            raise ValueError("price and quantity must be positive")

        fill_date = date or datetime.now().strftime('%Y-%m-%d')
        fill_amount = round(price * quantity, 2)

        # Update level
        lvl['status'] = 'filled'
        lvl['date'] = fill_date
        lvl['price'] = price
        lvl['quantity'] = quantity
        lvl['amount'] = fill_amount

        # Recompute aggregates
        total_invested = 0
        total_qty = 0
        for l in pos['levels']:
            if l['status'] == 'filled':
                total_invested += l['amount']
                total_qty += l['quantity']

        pos['total_invested'] = round(total_invested, 2)
        pos['total_quantity'] = total_qty
        pos['avg_cost'] = round(total_invested / total_qty, 2) if total_qty > 0 else 0

        # Auto-compute next level trigger_price
        next_level_idx = level  # 0-indexed for next level
        if next_level_idx < len(pos['levels']):
            next_lvl = pos['levels'][next_level_idx]
            if next_lvl['status'] == 'pending' and next_lvl['trigger_pct'] > 0:
                next_lvl['trigger_price'] = round(
                    price * (1 + next_lvl['trigger_pct'] / 100), 2
                )

        # Check if all levels filled -> status = completed
        all_filled = all(l['status'] == 'filled' for l in pos['levels'])
        if all_filled:
            pos['status'] = 'completed'

        # Apply SL precedence: after L2, avg_cost becomes SL floor
        # BUT: skip SL setting during entry month — let the position breathe.
        # Cost-cost floor activates when entry month has closed (monthly_lows has entry_month).
        filled_count = sum(1 for l in pos['levels'] if l['status'] == 'filled')
        entry_month = pos.get('entry_month', '')
        entry_month_closed = entry_month in pos.get('monthly_lows', {})

        if filled_count >= 2 and entry_month_closed:
            # Cost-cost floor: SL can never go below avg_cost after L2
            candidate = pos['avg_cost']
            current_sl = pos['current_sl']
            if current_sl is None:
                pos['current_sl'] = candidate
            else:
                pos['current_sl'] = max(current_sl, candidate)
        elif filled_count >= 2 and not entry_month_closed:
            logger.info(
                "Pyramid #%d: L2 filled in entry month %s — SL deferred "
                "until month-end close. Cost-cost floor (%.2f) will activate then.",
                position_id, entry_month, pos['avg_cost']
            )

        pos['version'] = pos.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Filled pyramid #%d L%d: %.2f x%d = Rs %.0f, avg=%.2f, SL=%s",
            position_id, level, price, quantity, fill_amount,
            pos['avg_cost'], pos['current_sl']
        )
        return pos

    def update_monthly_low(self, position_id: int, month: str,
                           low: float) -> dict:
        """Record a month's intraday low. Apply SL precedence rules.
        Sets last_checked_month.

        SL precedence:
        1. Entry month (Month 1): current_sl = null
        2. After Month 1: current_sl = entry_month_low
        3. Each subsequent month: max(current_sl, that_month_low)
        4. After Level 2+: max(current_sl, avg_cost) — cost-cost floor
        """
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")

        pos['monthly_lows'][month] = low
        pos['last_checked_month'] = month

        entry_month = pos['entry_month']

        if month == entry_month:
            # Entry month: no SL yet, just record the low
            pos['version'] = pos.get('version', 0) + 1
            self._save_local()
            self._upload_to_drive()
            logger.info(
                "Pyramid #%d: entry month %s low=%.2f recorded (no SL yet)",
                position_id, month, low
            )
            return pos

        # Month 2+: compute SL
        # Gather all completed month lows (excluding entry month for initial SL calc)
        sorted_months = sorted(pos['monthly_lows'].keys())

        # Start with entry month low as base SL
        base_sl = pos['monthly_lows'].get(entry_month, 0)

        # Trail upward through each subsequent month
        new_sl = base_sl
        for m in sorted_months:
            if m == entry_month:
                continue
            candidate = pos['monthly_lows'][m]
            new_sl = max(new_sl, candidate)

        # Cost-cost floor after Level 2
        filled_count = sum(1 for l in pos['levels'] if l['status'] == 'filled')
        if filled_count >= 2:
            new_sl = max(new_sl, pos['avg_cost'])

        # SL only moves up
        if pos['current_sl'] is not None:
            new_sl = max(new_sl, pos['current_sl'])

        old_sl = pos['current_sl']
        pos['current_sl'] = round(new_sl, 2)
        pos['version'] = pos.get('version', 0) + 1

        self._save_local()
        self._upload_to_drive()

        if old_sl != pos['current_sl']:
            logger.info(
                "Pyramid #%d %s: SL updated %.2f -> %.2f (month %s low=%.2f)",
                position_id, pos['symbol'], old_sl or 0,
                pos['current_sl'], month, low
            )
        else:
            logger.info(
                "Pyramid #%d %s: SL unchanged at %.2f (month %s low=%.2f)",
                position_id, pos['symbol'], pos['current_sl'], month, low
            )
        return pos

    def update_sl(self, position_id: int, new_sl: float) -> dict:
        """Manual SL override."""
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")

        old_sl = pos['current_sl']
        pos['current_sl'] = round(new_sl, 2)
        pos['version'] = pos.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Pyramid #%d %s: SL manually set %.2f -> %.2f",
            position_id, pos['symbol'], old_sl or 0, new_sl
        )
        return pos

    def close_position(self, position_id: int, exit_price: float,
                       reason: str) -> dict:
        """Mark position as exited, compute realized P&L."""
        pos = self._find(position_id)
        if not pos:
            raise ValueError(f"Position #{position_id} not found")
        if pos['status'] == 'exited':
            raise ValueError(f"Position #{position_id} is already exited")

        total_exit = round(exit_price * pos['total_quantity'], 2)
        realized_pnl = round(total_exit - pos['total_invested'], 2)
        pnl_pct = round(
            (realized_pnl / pos['total_invested']) * 100, 2
        ) if pos['total_invested'] > 0 else 0

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
            "Pyramid #%d %s CLOSED: exit=%.2f, P&L=Rs %.0f (%.1f%%), reason=%s",
            position_id, pos['symbol'], exit_price, realized_pnl, pnl_pct, reason
        )
        return pos

    def get_active(self) -> list:
        """Positions still holding (active or completed = all levels filled)."""
        return [p for p in self._positions
                if p.get('status') in ('active', 'completed')]

    def get_pending_adds(self) -> list:
        """Active positions with at least one pending level."""
        result = []
        for p in self.get_active():
            has_pending = any(l['status'] == 'pending' for l in p['levels'])
            if has_pending:
                result.append(p)
        return result

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
            print("No pyramid positions found.")
            return

        # Header
        print(f"\n{'ID':>3} {'Symbol':<12} {'Status':<10} {'Sector':<10} "
              f"{'Levels':<8} {'Invested':>10} {'Avg Cost':>10} "
              f"{'SL':>10} {'Thesis':<30}")
        print("-" * 115)

        for p in positions:
            filled = sum(1 for l in p['levels'] if l['status'] == 'filled')
            total = len(p['levels'])
            sl_str = f"{p['current_sl']:.2f}" if p['current_sl'] else "-"
            thesis = (p.get('thesis', '')[:28] + '..') if len(p.get('thesis', '')) > 30 else p.get('thesis', '')

            print(f"{p['id']:>3} {p['symbol']:<12} {p['status']:<10} "
                  f"{p['sector']:<10} {filled}/{total:<5} "
                  f"{p['total_invested']:>10,.0f} {p['avg_cost']:>10,.2f} "
                  f"{sl_str:>10} {thesis:<30}")

            # Show level details
            for l in p['levels']:
                if l['status'] == 'filled':
                    print(f"     L{l['level']}: FILLED {l['date']} "
                          f"@ {l['price']:.2f} x{l['quantity']} "
                          f"= Rs {l['amount']:,.0f}")
                else:
                    trigger = (f"trigger={l['trigger_price']:.2f}"
                               if l['trigger_price'] else
                               f"+{l['trigger_pct']}% from prev fill")
                    print(f"     L{l['level']}: PENDING "
                          f"({trigger}, budget Rs {l['amount']:,.0f})")

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
                logger.info("No pyramid file on Drive yet, loading local")
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
            logger.info("No local pyramid file, starting empty")

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
