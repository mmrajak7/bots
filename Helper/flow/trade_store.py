"""Flow trade store — CRUD + Google Drive sync.

Trade lifecycle: alerted → entered → exited/cancelled
Follows MagnetStore pattern: local-first JSON, Drive-secondary, atomic writes.
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

from . import config as cfg

logger = logging.getLogger(__name__)


def _load_config() -> dict:
    if not cfg.CONFIG_FILE.exists():
        logger.warning("Config %s not found, using defaults", cfg.CONFIG_FILE)
        return {}
    with open(cfg.CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    env_path = os.environ.get('FLOW_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)
    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')
    return Path(path_str) if path_str else None


class FlowStore:
    """Manages flow trades with local JSON + Google Drive sync."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._trades: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )

    def initialize(self):
        """Startup: auth Drive, download, fallback to local."""
        drive_cfg = self._config.get('google_drive', {})
        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)
        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        entered = sum(1 for t in self._trades if t.get('status') == 'entered')
        alerted = sum(1 for t in self._trades if t.get('status') == 'alerted')
        logger.info("FlowStore: %d trades (%d alerted, %d entered), drive=%s",
                     len(self._trades), alerted, entered,
                     'enabled' if self._drive_enabled else 'disabled')

    # ── Core Operations ───────────────────────────────────────────────────

    def load_trades(self) -> list:
        return self._trades

    def add_alert(self, data: dict) -> dict:
        """Record a new entry alert (status='alerted').

        Required: stock, side (UP/DOWN), direction (CE/PE),
                  spot, st_value, gap_pct, option_symbol, option_ask,
                  option_bid, spread_pct, lot_size, lots, cost
        """
        stock = data['stock']

        # Dedup: already alerted/entered for same stock + same side today
        today = datetime.now().strftime('%Y-%m-%d')
        for t in self._trades:
            if (t['stock'] == stock
                    and t.get('side') == data.get('side')
                    and t.get('alert_date') == today
                    and t['status'] in ('alerted', 'entered')):
                logger.info("Duplicate alert: %s (%s) already active today (#%d)",
                            stock, data.get('side'), t['id'])
                return t

        trade = {
            "id": self._next_id(),
            "version": 1,
            "status": "alerted",
            "stock": stock,
            "side": data['side'],           # UP or DOWN
            "direction": data['direction'], # CE or PE
            # Alert info
            "alert_date": today,
            "alert_time": datetime.now().strftime('%H:%M:%S'),
            "alert_spot": round(data['spot'], 2),
            "alert_st_value": round(data['st_value'], 2),
            "alert_gap_pct": round(data['gap_pct'], 4),
            # Option info
            "option_symbol": data['option_symbol'],
            "option_strike": data.get('option_strike'),
            "option_expiry": data.get('option_expiry'),
            "option_ask": data.get('option_ask'),
            "option_bid": data.get('option_bid'),
            "spread_pct": round(data.get('spread_pct', 0), 4),
            "lot_size": data.get('lot_size'),
            "lots": data.get('lots', 1),
            "cost": data.get('cost'),
            # Entry (populated when user confirms entry)
            "entry_spot": None,
            "entry_date": None,
            "entry_time": None,
            "entry_premium": None,
            "quantity": None,
            # Monitoring
            "current_st": round(data['st_value'], 2),
            "peak_spot": None,
            # Exit
            "exit_spot": None,
            "exit_date": None,
            "exit_time": None,
            "exit_premium": None,
            "exit_reason": None,
            "pnl": None,
            "pnl_pct": None,
            "days_held": 0,
            # Meta
            "notes": data.get('notes', ''),
        }

        self._trades.append(trade)
        self._save_local()
        self._upload_to_drive()

        logger.info("ALERT #%d: %s (%s) gap=%.2f%% option=%s",
                     trade['id'], stock, trade['direction'],
                     data['gap_pct'] * 100, trade['option_symbol'])
        return trade

    def enter_trade(self, trade_id: int, entry_data: dict) -> dict:
        """Transition alerted → entered."""
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'alerted':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not alerted")

        now = datetime.now()
        trade['status'] = 'entered'
        trade['entry_spot'] = entry_data.get('entry_spot')
        trade['entry_date'] = now.strftime('%Y-%m-%d')
        trade['entry_time'] = now.strftime('%H:%M:%S')
        trade['entry_premium'] = entry_data.get('entry_premium')
        trade['quantity'] = entry_data.get('quantity')
        trade['peak_spot'] = entry_data.get('entry_spot')
        if entry_data.get('option_symbol'):
            trade['option_symbol'] = entry_data['option_symbol']
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()

        logger.info("ENTRY #%d: %s %s @%.2f premium=%.2f qty=%s",
                     trade_id, trade['stock'], trade['direction'],
                     trade['entry_spot'] or 0, trade['entry_premium'] or 0,
                     trade['quantity'])
        return trade

    def update_monitoring(self, trade_id: int, current_st: float,
                          peak_spot: float = None) -> None:
        """Update current ST level and peak spot. Local save only."""
        trade = self._find(trade_id)
        if not trade:
            return
        changed = False
        if current_st != trade.get('current_st'):
            trade['current_st'] = round(current_st, 2)
            changed = True
        if peak_spot and (not trade.get('peak_spot') or peak_spot > trade['peak_spot']):
            trade['peak_spot'] = round(peak_spot, 2)
            changed = True
        if changed:
            trade['version'] += 1
            self._save_local()

    def exit_trade(self, trade_id: int, exit_spot: float,
                   exit_premium: float, reason: str) -> dict:
        """Transition entered → exited."""
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'entered':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not entered")

        now = datetime.now()
        trade['status'] = 'exited'
        trade['exit_spot'] = round(exit_spot, 2)
        trade['exit_date'] = now.strftime('%Y-%m-%d')
        trade['exit_time'] = now.strftime('%H:%M:%S')
        trade['exit_premium'] = round(exit_premium, 2)
        trade['exit_reason'] = reason

        entry_prem = trade.get('entry_premium', 0) or 0
        qty = trade.get('quantity', 0) or 0
        if entry_prem > 0 and qty > 0:
            pnl_per_share = exit_premium - entry_prem
            trade['pnl'] = round(pnl_per_share * qty, 2)
            trade['pnl_pct'] = round((pnl_per_share / entry_prem) * 100, 2)

        if trade.get('entry_date'):
            entry_dt = datetime.strptime(trade['entry_date'], '%Y-%m-%d')
            trade['days_held'] = (now - entry_dt).days

        trade['version'] += 1
        self._save_local()
        self._upload_to_drive()

        logger.info("EXIT #%d: %s reason=%s P&L=Rs %.0f (%.1f%%)",
                     trade_id, trade['stock'], reason,
                     trade.get('pnl', 0), trade.get('pnl_pct', 0))
        return trade

    def cancel_alert(self, trade_id: int, reason: str) -> dict:
        """Cancel an alerted signal."""
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'alerted':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not alerted")

        trade['status'] = 'cancelled'
        trade['exit_reason'] = reason
        trade['exit_date'] = datetime.now().strftime('%Y-%m-%d')
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()
        logger.info("CANCEL #%d: %s, reason=%s", trade_id, trade['stock'], reason)
        return trade

    def get_alerted(self) -> list:
        return [t for t in self._trades if t['status'] == 'alerted']

    def get_entered(self) -> list:
        return [t for t in self._trades if t['status'] == 'entered']

    def get_open(self) -> list:
        return [t for t in self._trades if t['status'] in ('alerted', 'entered')]

    def get_today_stocks(self) -> set:
        """Stocks that had alerts or entries today (for no-reentry rule)."""
        today = datetime.now().strftime('%Y-%m-%d')
        stocks = set()
        for t in self._trades:
            if t.get('alert_date') == today:
                stocks.add(t['stock'])
            if t.get('entry_date') == today:
                stocks.add(t['stock'])
        return stocks

    def list_trades(self, status_filter: Optional[str] = None):
        """Print formatted trade table."""
        trades = self._trades
        if status_filter:
            trades = [t for t in trades if t['status'] == status_filter]
        if not trades:
            print("No flow trades found.")
            return

        print(f"\n{'ID':>3} {'Stock':<12} {'Dir':<4} {'Side':<5} {'Status':<10} "
              f"{'Gap%':>6} {'ST':>10} {'Entry':>10} "
              f"{'P&L':>10} {'Reason':<12}")
        print("-" * 95)

        for t in trades:
            entry_str = f"{t['entry_spot']:.2f}" if t.get('entry_spot') else "-"
            pnl_str = f"{t['pnl']:,.0f}" if t.get('pnl') is not None else "-"
            reason_str = t.get('exit_reason', '') or ''

            print(f"{t['id']:>3} {t['stock']:<12} {t['direction']:<4} "
                  f"{t['side']:<5} {t['status']:<10} "
                  f"{t['alert_gap_pct']*100:>5.1f}% "
                  f"{t['alert_st_value']:>10,.2f} "
                  f"{entry_str:>10} {pnl_str:>10} {reason_str:<12}")

            if t.get('option_symbol'):
                print(f"     Option: {t['option_symbol']} "
                      f"ask={t.get('option_ask', '?')} "
                      f"bid={t.get('option_bid', '?')} "
                      f"spread={t.get('spread_pct', 0)*100:.1f}%")
        print()

    # ── Sync ──────────────────────────────────────────────────────────────

    def maybe_sync(self, force: bool = False):
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    def flush(self):
        self._save_local()
        self._upload_to_drive()

    # ── Private ───────────────────────────────────────────────────────────

    def _next_id(self) -> int:
        if not self._trades:
            return 1
        return max(t['id'] for t in self._trades) + 1

    def _find(self, trade_id: int) -> Optional[dict]:
        for t in self._trades:
            if t['id'] == trade_id:
                return t
        return None

    def _load_local(self):
        if cfg.LOCAL_FILE.exists():
            try:
                with open(cfg.LOCAL_FILE) as f:
                    self._trades = json.load(f)
            except Exception as e:
                logger.warning("Failed to load local: %s", e)
                self._trades = []
        else:
            self._trades = []

    def _read_local(self) -> list:
        if cfg.LOCAL_FILE.exists():
            try:
                with open(cfg.LOCAL_FILE) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_local(self):
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = cfg.LOCAL_FILE.with_suffix('.tmp')
        try:
            with open(tmp_path, 'w') as f:
                json.dump(self._trades, f, indent=2)
            tmp_path.replace(cfg.LOCAL_FILE)
        except Exception as e:
            logger.error("Failed to save local: %s", e)
            if tmp_path.exists():
                tmp_path.unlink()

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found, local-only")
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'flow_trades.json')
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
                base = self._trades if self._trades else self._read_local()
                merged = self._merge(base, drive_data)
                self._trades = merged
                self._save_local()
            self._last_sync_time = time.time()
        except Exception as e:
            logger.warning("Drive sync failed: %s", e)
            if not self._trades:
                self._load_local()

    def _upload_to_drive(self):
        if not self._drive_enabled or not self._drive_service:
            return
        try:
            from bcs.drive_store import upload_json
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'flow_trades.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
        except Exception as e:
            logger.debug("Drive upload failed: %s", e)

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        """Merge by ID, keep higher version."""
        by_id = {}
        for t in base:
            by_id[t['id']] = t
        for t in incoming:
            tid = t['id']
            if tid not in by_id or t.get('version', 0) > by_id[tid].get('version', 0):
                by_id[tid] = t
        return sorted(by_id.values(), key=lambda x: x['id'])


# ── Singleton ────────────────────────────────────────────────────────────

_store: Optional[FlowStore] = None


def get_store() -> FlowStore:
    global _store
    if _store is None:
        _store = FlowStore()
        _store.initialize()
    return _store
