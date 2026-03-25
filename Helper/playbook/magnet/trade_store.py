"""Magnet trade store — CRUD + Google Drive sync for ST-magnet option trades.

Trade lifecycle: watching → entered → exited
Paper mode: trades are tracked but no real orders placed.

Follows pyramid_store.py pattern: local-first JSON, Drive-secondary,
atomic writes, version-based merge, singleton access.
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
    env_path = os.environ.get('MAGNET_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class MagnetStore:
    """Manages magnet trades with local JSON + Google Drive sync."""

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

    # ── Initialization ────────────────────────────────────────────────────

    def initialize(self):
        """Startup: auth Drive, download, fallback to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        watching = sum(1 for t in self._trades if t.get('status') == 'watching')
        entered = sum(1 for t in self._trades if t.get('status') == 'entered')
        logger.info(
            "MagnetStore: %d trades (%d watching, %d entered), drive=%s",
            len(self._trades), watching, entered,
            'enabled' if self._drive_enabled else 'disabled'
        )

    # ── Core Operations ───────────────────────────────────────────────────

    def load_trades(self) -> list:
        """Return all trades from cache."""
        return self._trades

    def add_signal(self, data: dict) -> dict:
        """Add a new signal to watchlist (status='watching').

        Required: stock, timeframe, st_value, st_direction, signal_price.
        """
        required = ['stock', 'timeframe', 'st_value', 'st_direction', 'signal_price']
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        stock = data['stock']
        timeframe = data['timeframe']

        # Dedup: already watching or entered for same stock (ANY timeframe)?
        # Prevents double exposure from weekly + monthly signals on same stock
        for t in self._trades:
            if (t['stock'] == stock
                    and t['status'] in ('watching', 'entered')):
                logger.info("Duplicate signal: %s already active as #%d (%s), "
                            "skipping %s signal",
                            stock, t['id'], t['timeframe'], timeframe)
                return t

        st_val = data['st_value']
        price = data['signal_price']
        gap_pct = abs(price - st_val) / st_val
        side = 'above' if price > st_val else 'below'

        trade = {
            "id": self._next_id(),
            "version": 1,
            "status": "watching",
            "stock": stock,
            "timeframe": timeframe,
            "direction": cfg.OPTION_TYPE_MAP[side],  # CE or PE
            "side": side,
            "st_value": round(st_val, 2),
            "st_direction": data['st_direction'],
            "signal_price": round(price, 2),
            "signal_gap_pct": round(gap_pct * 100, 2),
            "signal_date": data.get('signal_date',
                                    datetime.now().strftime('%Y-%m-%d')),
            "signal_time": datetime.now().strftime('%H:%M:%S'),
            # Entry fields (populated when gap hits 2%)
            "entry_spot": None,
            "entry_date": None,
            "entry_time": None,
            "option_strike": None,
            "option_symbol": None,
            "option_premium": None,
            "option_type": cfg.OPTION_TYPE_MAP[side],
            "option_expiry": None,
            "product": cfg.PRODUCT,
            "quantity": None,
            "lot_size": None,
            # Target / SL
            "target_spot": round(st_val, 2),
            "sl_spot": None,  # set at entry: price at 5% gap from ST
            "sl_time_deadline": None,  # set at entry: 5 trading days
            "days_held": 0,
            "peak_premium": None,       # highest observed long leg premium (trail SL)
            "entry_retries": 0,         # failed option lookup count (survives restarts)
            # Cost SL (activated when gap < 1%)
            "cost_sl_active": False,
            "cost_sl_level": None,     # entry_premium + 0.10
            "cost_sl_activated_at": None,
            # Hedge (short leg beyond ST, added on reversal at 3% gap)
            "hedged": False,
            "hedge_strike": None,
            "hedge_symbol": None,
            "hedge_premium": None,     # credit received (BID - slippage)
            "hedge_date": None,
            "hedge_net_debit": None,   # entry_premium - hedge_credit
            "hedge_spread_width": None,
            # Exit
            "exit_spot": None,
            "exit_date": None,
            "exit_time": None,
            "exit_premium": None,      # long leg exit
            "exit_hedge_premium": None, # short leg buyback (None if not hedged)
            "exit_reason": None,
            "pnl": None,
            "pnl_pct": None,
            # Meta
            "paper": True,
            "notes": data.get('notes', ''),
        }

        self._trades.append(trade)
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "Signal #%d: %s (%s) gap=%.1f%% side=%s -> %s, watching",
            trade['id'], stock, timeframe, gap_pct * 100, side,
            trade['direction']
        )
        return trade

    def enter_trade(self, trade_id: int, entry_data: dict) -> dict:
        """Transition watching → entered (paper trade entry).

        entry_data: entry_spot, option_strike, option_symbol, option_premium,
                    lot_size, quantity, sl_spot
        """
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'watching':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not watching")

        now = datetime.now()
        trade['status'] = 'entered'
        trade['entry_spot'] = entry_data.get('entry_spot')
        trade['entry_date'] = now.strftime('%Y-%m-%d')
        trade['entry_time'] = now.strftime('%H:%M:%S')
        trade['option_strike'] = entry_data.get('option_strike')
        trade['option_symbol'] = entry_data.get('option_symbol')
        trade['option_premium'] = entry_data.get('option_premium')
        trade['peak_premium'] = entry_data.get('option_premium')  # initialize peak = entry
        trade['option_expiry'] = entry_data.get('option_expiry')
        trade['lot_size'] = entry_data.get('lot_size')
        trade['quantity'] = entry_data.get('quantity')
        trade['sl_spot'] = entry_data.get('sl_spot')
        if entry_data.get('delegated_to_confidence'):
            trade['delegated_to_confidence'] = True
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()

        logger.info(
            "ENTRY #%d: %s %s @%.2f, premium=%.2f, target=%.2f, sl=%.2f",
            trade_id, trade['stock'], trade['direction'],
            trade['entry_spot'] or 0, trade['option_premium'] or 0,
            trade['target_spot'], trade['sl_spot'] or 0
        )
        return trade

    def revert_to_watching(self, trade_id: int, reason: str = '') -> dict:
        """Revert an entered trade back to watching (e.g., delegation failure).

        Only allowed for trades with delegated_to_confidence=True that were
        never actually entered by the user.
        """
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'entered':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not entered")

        trade['status'] = 'watching'
        # Clear entry fields that were set during delegation
        for key in ('entry_spot', 'entry_date', 'entry_time', 'option_strike',
                    'option_symbol', 'option_premium', 'peak_premium',
                    'option_expiry', 'lot_size', 'quantity', 'sl_spot',
                    'delegated_to_confidence'):
            trade.pop(key, None)
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()
        logger.info("REVERT #%d: %s back to watching (%s)",
                     trade_id, trade['stock'], reason or 'delegation failed')
        return trade

    def add_hedge(self, trade_id: int, hedge_data: dict) -> dict:
        """Add short leg beyond ST to cap losses on reversal.

        hedge_data: hedge_strike, hedge_symbol, hedge_premium (credit),
                    hedge_spread_width
        """
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'entered':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not entered")
        if trade.get('hedged'):
            return trade  # already hedged

        entry_prem = trade.get('option_premium', 0) or 0
        credit = hedge_data.get('hedge_premium', 0) or 0

        trade['hedged'] = True
        trade['hedge_strike'] = hedge_data.get('hedge_strike')
        trade['hedge_symbol'] = hedge_data.get('hedge_symbol')
        trade['hedge_premium'] = credit
        trade['hedge_date'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        trade['hedge_net_debit'] = round(entry_prem - credit, 2)
        trade['hedge_spread_width'] = hedge_data.get('hedge_spread_width')
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()

        logger.info(
            "HEDGE #%d: %s sell %s @%.2f, net debit %.2f (was %.2f)",
            trade_id, trade['stock'], trade['hedge_symbol'],
            credit, trade['hedge_net_debit'], entry_prem
        )
        return trade

    def activate_cost_sl(self, trade_id: int) -> dict:
        """Activate cost SL when gap shrinks to 1%. SL = entry premium + 0.10."""
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade.get('cost_sl_active'):
            return trade  # already active

        entry_prem = trade.get('option_premium', 0) or 0
        trade['cost_sl_active'] = True
        trade['cost_sl_level'] = round(entry_prem + 0.10, 2)
        trade['cost_sl_activated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()

        logger.info(
            "COST SL #%d: %s activated, SL floor = Rs %.2f (cost+0.10)",
            trade_id, trade['stock'], trade['cost_sl_level']
        )
        return trade

    def update_peak_premium(self, trade_id: int, peak: float) -> None:
        """Update peak premium for trailing SL. Local save only (Drive via maybe_sync).

        Called every 30s monitor cycle when peak changes. Bumps version so
        Drive merge picks up the latest peak on sync.
        """
        trade = self._find(trade_id)
        if not trade:
            return
        old_peak = trade.get('peak_premium') or 0
        if peak > old_peak:
            trade['peak_premium'] = round(peak, 2)
            trade['version'] += 1
            self._save_local()

    def update_entry_retries(self, trade_id: int, retries: int) -> None:
        """Persist entry retry count. Local save only, no version bump."""
        trade = self._find(trade_id)
        if not trade:
            return
        trade['entry_retries'] = retries
        self._save_local()

    def exit_trade(self, trade_id: int, exit_spot: float,
                   exit_premium: float, reason: str,
                   exit_hedge_premium: float = None) -> dict:
        """Transition entered → exited. Handles naked and hedged P&L.

        exit_premium: long leg sell price (BID - slippage)
        exit_hedge_premium: short leg buyback (ASK + slippage), None if not hedged
        """
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
        trade['exit_hedge_premium'] = (round(exit_hedge_premium, 2)
                                       if exit_hedge_premium is not None else None)
        trade['exit_reason'] = reason

        entry_prem = trade.get('option_premium', 0) or 0
        qty = trade.get('quantity', 0) or 0
        if entry_prem > 0 and qty > 0:
            if trade.get('hedged') and exit_hedge_premium is not None:
                # Spread P&L: (long_exit - short_buyback) - (long_entry - short_credit)
                hedge_credit = trade.get('hedge_premium', 0) or 0
                pnl_per_share = (exit_premium - exit_hedge_premium) - (entry_prem - hedge_credit)
            else:
                pnl_per_share = exit_premium - entry_prem
            trade['pnl'] = round(pnl_per_share * qty, 2)
            trade['pnl_pct'] = round((pnl_per_share / entry_prem) * 100, 2)

        if trade.get('entry_date'):
            entry_dt = datetime.strptime(trade['entry_date'], '%Y-%m-%d')
            trade['days_held'] = (now - entry_dt).days

        trade['version'] += 1
        self._save_local()
        self._upload_to_drive()

        logger.info(
            "EXIT #%d: %s reason=%s, P&L=Rs %.0f (%.1f%%)",
            trade_id, trade['stock'], reason,
            trade.get('pnl', 0), trade.get('pnl_pct', 0)
        )
        return trade

    def cancel_signal(self, trade_id: int, reason: str) -> dict:
        """Cancel a watching signal (e.g., stale, already moved past entry)."""
        trade = self._find(trade_id)
        if not trade:
            raise ValueError(f"Trade #{trade_id} not found")
        if trade['status'] != 'watching':
            raise ValueError(f"Trade #{trade_id} is {trade['status']}, not watching")

        trade['status'] = 'cancelled'
        trade['exit_reason'] = reason
        trade['exit_date'] = datetime.now().strftime('%Y-%m-%d')
        trade['version'] += 1

        self._save_local()
        self._upload_to_drive()
        logger.info("CANCEL #%d: %s, reason=%s", trade_id, trade['stock'], reason)
        return trade

    def get_watching(self) -> list:
        return [t for t in self._trades if t['status'] == 'watching']

    def get_entered(self) -> list:
        return [t for t in self._trades if t['status'] == 'entered']

    def get_open(self) -> list:
        """Watching + entered."""
        return [t for t in self._trades if t['status'] in ('watching', 'entered')]

    def list_trades(self, status_filter: Optional[str] = None):
        """Print formatted trade table."""
        trades = self._trades
        if status_filter:
            trades = [t for t in trades if t['status'] == status_filter]

        if not trades:
            print("No magnet trades found.")
            return

        print(f"\n{'ID':>3} {'Stock':<12} {'TF':<8} {'Dir':<4} {'Status':<10} "
              f"{'Gap%':>6} {'ST':>10} {'Entry':>10} {'Target':>10} "
              f"{'SL':>10} {'P&L':>10}")
        print("-" * 105)

        for t in trades:
            sl_str = f"{t['sl_spot']:.2f}" if t.get('sl_spot') else "-"
            entry_str = f"{t['entry_spot']:.2f}" if t.get('entry_spot') else "-"
            pnl_str = f"{t['pnl']:,.0f}" if t.get('pnl') is not None else "-"

            print(f"{t['id']:>3} {t['stock']:<12} {t['timeframe']:<8} "
                  f"{t['direction']:<4} {t['status']:<10} "
                  f"{t['signal_gap_pct']:>5.1f}% {t['st_value']:>10,.2f} "
                  f"{entry_str:>10} {t['target_spot']:>10,.2f} "
                  f"{sl_str:>10} {pnl_str:>10}")

            if t.get('hedged'):
                print(f"     HEDGE: short {t.get('hedge_symbol', '?')} "
                      f"@{t.get('hedge_premium', 0):.2f} "
                      f"| net debit={t.get('hedge_net_debit', 0):.2f} "
                      f"| width={t.get('hedge_spread_width', 0)}")
            if t.get('cost_sl_active'):
                print(f"     COST SL: Rs {t.get('cost_sl_level', 0):.2f} "
                      f"(activated {t.get('cost_sl_activated_at', '?')})")

            if t.get('exit_reason'):
                hedge_str = ""
                if t.get('exit_hedge_premium') is not None:
                    hedge_str = f" short_buyback={t['exit_hedge_premium']:.2f}"
                print(f"     EXIT: {t.get('exit_date', '')} "
                      f"reason={t['exit_reason']} "
                      f"days={t.get('days_held', 0)}{hedge_str}")
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

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found, local-only")
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'magnet_trades.json')
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

                drive_vers = {t['id']: t.get('version', 0) for t in drive_data}
                merged_vers = {t['id']: t.get('version', 0) for t in merged}
                if drive_vers != merged_vers:
                    self._upload_to_drive()
            else:
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
            file_name = drive_cfg.get('file_name', 'magnet_trades.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local is safe.", e)

    def _read_local(self) -> list:
        if not cfg.LOCAL_FILE.exists():
            return []
        try:
            with open(cfg.LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = cfg.LOCAL_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                cfg.LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical("File CORRUPT (%s). Backed up to %s.", e, backup)
            return []

    def _load_local(self):
        self._trades = self._read_local()

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        by_id = {}
        for t in base:
            by_id[t['id']] = t
        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                by_id[tid] = t
            elif t.get('version', 0) > by_id[tid].get('version', 0):
                by_id[tid] = t
        return sorted(by_id.values(), key=lambda t: t['id'])

    def _save_local(self):
        cfg.LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(cfg.LOG_DIR), suffix='.tmp', prefix='magnet_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(cfg.LOCAL_FILE))
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


# ── Singleton ─────────────────────────────────────────────────────────────

_store: Optional[MagnetStore] = None


def get_store() -> MagnetStore:
    """Get or create the singleton MagnetStore."""
    global _store
    if _store is None:
        _store = MagnetStore()
        _store.initialize()
    return _store
