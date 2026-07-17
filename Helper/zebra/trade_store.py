"""Zebra trade store — CRUD + Drive sync.

Lifecycle: watching → triggered → entered → exited.
A signal can also be 'cancelled' from watching/triggered.

Mirrors pyramid/bcs store pattern: local-first JSON, Drive-secondary,
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
    env_path = os.environ.get('ZEBRA_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)
    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')
    return Path(path_str) if path_str else None


class ZebraStore:
    """Zebra trades with local JSON + Drive sync."""

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
        drive_cfg = self._config.get('google_drive', {})
        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)
        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        watching = sum(1 for t in self._trades if t.get('status') == 'watching')
        triggered = sum(1 for t in self._trades if t.get('status') == 'triggered')
        entered = sum(1 for t in self._trades if t.get('status') == 'entered')
        logger.info(
            "ZebraStore: %d trades (%d watching, %d triggered, %d entered), drive=%s",
            len(self._trades), watching, triggered, entered,
            'enabled' if self._drive_enabled else 'disabled'
        )

    # ── Reads ─────────────────────────────────────────────────────────────
    def load_trades(self) -> list:
        return self._trades

    def get_by_status(self, status: str) -> list:
        return [t for t in self._trades if t.get('status') == status]

    def get_watching(self) -> list:
        return self.get_by_status('watching')

    def get_triggered(self) -> list:
        return self.get_by_status('triggered')

    def get_entered(self) -> list:
        return self.get_by_status('entered')

    def find(self, trade_id: int) -> Optional[dict]:
        for t in self._trades:
            if t.get('id') == trade_id:
                return t
        return None

    # ── Writes ────────────────────────────────────────────────────────────
    def add_signal(self, data: dict) -> dict:
        """Add a fresh signal at WATCH band entry (gap <= watch_gap_max).

        Required: stock, timeframe, direction, st_value, st_direction,
                  signal_price, signal_gap_pct.
        """
        required = ['stock', 'timeframe', 'direction', 'st_value',
                    'st_direction', 'signal_price', 'signal_gap_pct']
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        stock = data['stock']
        timeframe = data['timeframe']
        direction = data['direction']

        # Dedup: no two open signals for same (stock, timeframe, direction).
        # BCS shadows are excluded — they are passive A/B mirrors and must
        # never block a fresh zebra signal.
        for t in self._trades:
            if (t.get('stock') == stock
                    and t.get('timeframe') == timeframe
                    and t.get('direction') == direction
                    and t.get('structure', 'zebra') != 'bcs'
                    and t.get('status') in ('watching', 'triggered', 'entered')):
                raise ValueError(
                    f"{stock} {timeframe} {direction} already open as #{t['id']}"
                )

        now = datetime.now()
        trade = {
            'id': self._next_id(),
            'version': 1,
            'status': 'watching',
            'stock': stock,
            'timeframe': timeframe,
            'direction': direction,            # CE or PE
            'st_value': data['st_value'],
            'st_direction': data['st_direction'],
            # trend_aligned is NOT stored — it is derived on demand from
            # direction + st_direction via cfg.is_trend_aligned (single source
            # of truth), so it can never drift from or be dropped by the schema.
            'signal_price': data['signal_price'],
            'signal_gap_pct': data['signal_gap_pct'],
            'signal_date': now.strftime('%Y-%m-%d'),
            'signal_time': now.strftime('%H:%M:%S'),
            'paper': data.get('paper', True),
            'notes': data.get('notes', ''),
        }

        self._trades.append(trade)
        self._save_local()
        self._upload_to_drive()
        logger.info(
            "WATCHING #%d %s %s %s spot=%.2f ST=%.2f gap=%.2f%%",
            trade['id'], stock, timeframe, direction,
            trade['signal_price'], trade['st_value'], trade['signal_gap_pct']
        )
        return trade

    def mark_triggered(self, trade_id: int, trigger_spot: float,
                       trigger_gap_pct: float,
                       alert_strikes: list) -> dict:
        """Promote watching → triggered. alert_strikes = candidate pairs from analyzer."""
        t = self._must_find(trade_id)
        if t['status'] != 'watching':
            raise ValueError(f"#{trade_id} status={t['status']}, can't trigger")
        now = datetime.now()
        t['status'] = 'triggered'
        t['triggered_at'] = now.isoformat()
        t['trigger_spot'] = trigger_spot
        t['trigger_gap_pct'] = trigger_gap_pct
        t['alert_strikes'] = alert_strikes
        t['version'] = t.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()
        logger.info("TRIGGERED #%d %s gap=%.2f%% (%d candidate pairs)",
                    trade_id, t['stock'], trigger_gap_pct, len(alert_strikes))
        return t

    def mark_entered(self, trade_id: int, entry_data: dict) -> dict:
        """Promote triggered → entered. User has placed the trade.

        Required in entry_data: long_strike, short_strike, long_symbol,
        short_symbol, debit, lot_size, lots, expiry.
        Computed: quantity, capital, tp_spot, sl_spot, debit_sl_value, dte.
        """
        t = self._must_find(trade_id)
        if t['status'] not in ('watching', 'triggered'):
            raise ValueError(f"#{trade_id} status={t['status']}, can't enter")

        required = ['long_strike', 'short_strike', 'long_symbol',
                    'short_symbol', 'debit', 'lot_size', 'lots', 'expiry']
        missing = [f for f in required if f not in entry_data]
        if missing:
            raise ValueError(f"mark_entered missing: {missing}")

        lot_size = int(entry_data['lot_size'])
        lots = int(entry_data['lots'])
        quantity = lot_size * lots
        debit = float(entry_data['debit'])
        capital = round(debit * quantity, 2)
        entry_spot = float(entry_data.get('entry_spot', t.get('trigger_spot',
                                          t.get('signal_price'))))

        direction = t['direction']
        # Spot SL: adverse direction from entry_spot
        spot_sl_pct = float(entry_data.get('spot_sl_pct', cfg.SPOT_SL_PCT))
        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - spot_sl_pct), 2)
        else:
            sl_spot = round(entry_spot * (1 + spot_sl_pct), 2)

        # TP: ST line (default) or short strike
        tp_target = cfg.TP_TARGET
        if tp_target == 'short_strike':
            tp_spot = float(entry_data['short_strike'])
        else:
            tp_spot = float(t['st_value'])

        debit_sl_value = round(debit * cfg.DEBIT_SL_PCT, 2)

        # DTE
        try:
            exp_date = datetime.strptime(entry_data['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        t['status'] = 'entered'
        t['entry_date'] = now.strftime('%Y-%m-%d')
        t['entry_time'] = now.strftime('%H:%M:%S')
        t['entry_spot'] = entry_spot
        t['long_strike'] = float(entry_data['long_strike'])
        t['short_strike'] = float(entry_data['short_strike'])
        t['long_symbol'] = entry_data['long_symbol']
        t['short_symbol'] = entry_data['short_symbol']
        t['debit'] = debit
        t['lot_size'] = lot_size
        t['lots'] = lots
        t['quantity'] = quantity
        t['capital'] = capital
        t['expiry'] = entry_data['expiry']
        t['dte_at_entry'] = dte
        t['tp_spot'] = tp_spot
        t['sl_spot'] = sl_spot
        t['spot_sl_pct'] = spot_sl_pct
        t['debit_sl_value'] = debit_sl_value
        t['debit_sl_pct'] = cfg.DEBIT_SL_PCT
        # Entry-time extrinsic of the short leg — feeds the intrinsic-floor
        # quote-sanity guard in the monitor (bad-quote false-SL protection).
        if 'short_extrinsic_entry' in entry_data:
            t['short_extrinsic_entry'] = float(entry_data['short_extrinsic_entry'])
        t['version'] = t.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()
        logger.info(
            "ENTERED #%d %s %s/%s debit=%.2f qty=%d cap=Rs%.0f TP=%.2f SL=%.2f",
            trade_id, t['stock'], int(t['long_strike']), int(t['short_strike']),
            debit, quantity, capital, tp_spot, sl_spot
        )
        return t

    def add_bcs_shadow(self, zebra_trade: dict, bcs: dict) -> dict:
        """Create a paper BCS trade shadowing a just-entered zebra trade.

        Born directly as 'entered' (it has no watching/triggered life of its
        own — the zebra signal already did that). `bcs` is the dict returned
        by strikes.analyze_bcs plus 'expiry' and 'entry_spot' set by caller.
        Tagged structure='bcs' + shadow_of=<zebra id> so reports can pair the
        A/B legs; excluded from all scanner dedup.
        """
        lot_size = int(bcs['lot_size'])
        lots = 1
        quantity = lot_size * lots
        debit = float(bcs['debit'])
        entry_spot = float(bcs['entry_spot'])
        direction = zebra_trade['direction']

        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - cfg.SPOT_SL_PCT), 2)
        else:
            sl_spot = round(entry_spot * (1 + cfg.SPOT_SL_PCT), 2)

        try:
            exp_date = datetime.strptime(bcs['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        trade = {
            'id': self._next_id(),
            'version': 1,
            'status': 'entered',
            'structure': 'bcs',
            'shadow_of': zebra_trade['id'],
            'stock': zebra_trade['stock'],
            'timeframe': zebra_trade['timeframe'],
            'direction': direction,
            'st_value': zebra_trade['st_value'],
            'st_direction': zebra_trade['st_direction'],
            'signal_price': zebra_trade['signal_price'],
            'signal_gap_pct': zebra_trade['signal_gap_pct'],
            'signal_date': zebra_trade.get('signal_date'),
            'signal_time': zebra_trade.get('signal_time'),
            'paper': True,
            'notes': f"BCS shadow of zebra #{zebra_trade['id']}",
            'entry_date': now.strftime('%Y-%m-%d'),
            'entry_time': now.strftime('%H:%M:%S'),
            'entry_spot': entry_spot,
            'long_strike': float(bcs['long_strike']),
            'short_strike': float(bcs['short_strike']),
            'long_symbol': bcs['long_symbol'],
            'short_symbol': bcs['short_symbol'],
            'debit': debit,
            'lot_size': lot_size,
            'lots': lots,
            'quantity': quantity,
            'capital': round(debit * quantity, 2),
            'expiry': bcs['expiry'],
            'dte_at_entry': dte,
            'tp_spot': float(zebra_trade.get('tp_spot', zebra_trade['st_value'])),
            'sl_spot': sl_spot,
            'spot_sl_pct': cfg.SPOT_SL_PCT,
            'debit_sl_value': round(debit * cfg.DEBIT_SL_PCT, 2),
            'debit_sl_pct': cfg.DEBIT_SL_PCT,
            'width': float(bcs['width']),
            'debit_to_width_pct': bcs.get('debit_to_width_pct'),
            'short_extrinsic_entry': float(bcs.get('short_extrinsic', 0)
                                           or bcs.get('short_extrinsic_entry', 0)),
            'entry_warnings': bcs.get('warnings', []),
        }
        self._trades.append(trade)
        self._save_local()
        self._upload_to_drive()
        logger.info(
            "BCS SHADOW #%d (of #%d) %s %s %g/%g debit=%.2f qty=%d d/w=%s%%",
            trade['id'], zebra_trade['id'], trade['stock'], direction,
            trade['long_strike'], trade['short_strike'], debit, quantity,
            trade.get('debit_to_width_pct')
        )
        return trade

    def mark_exited(self, trade_id: int, exit_spot: float,
                    exit_debit: Optional[float],
                    reason: str) -> dict:
        """Close an entered trade. exit_debit = closing net debit per share
        (positive if still costs money to close, negative if closes for credit)."""
        t = self._must_find(trade_id)
        if t['status'] != 'entered':
            raise ValueError(f"#{trade_id} status={t['status']}, can't exit")

        debit = float(t['debit'])
        qty = int(t['quantity'])
        if exit_debit is not None:
            # P&L per share = current value of structure - entry debit
            # If user closed at exit_debit (i.e., paid that much to unwind),
            # then they recovered (debit - exit_debit). But Zebra is a debit
            # trade: the structure has POSITIVE value when in profit, negative
            # of debit when worst case. exit_debit here = current mark of the
            # structure (long_value*2 - short_value), so P&L = exit_debit - debit.
            # We document this convention in monitor.
            pnl_per_share = float(exit_debit) - debit
        else:
            # Worst case: structure went to -debit (max loss)
            pnl_per_share = -debit
        pnl = round(pnl_per_share * qty, 2)
        pnl_pct = round((pnl_per_share / debit) * 100, 2) if debit > 0 else 0

        now = datetime.now()
        t['status'] = 'exited'
        t['exit_date'] = now.strftime('%Y-%m-%d')
        t['exit_time'] = now.strftime('%H:%M:%S')
        t['exit_spot'] = exit_spot
        t['exit_debit'] = exit_debit
        t['pnl'] = pnl
        t['pnl_pct'] = pnl_pct
        t['exit_reason'] = reason
        t['version'] = t.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()
        logger.info(
            "EXITED #%d %s reason=%s spot=%.2f P&L=Rs%.0f (%.1f%%)",
            trade_id, t['stock'], reason, exit_spot, pnl, pnl_pct
        )
        return t

    def cancel(self, trade_id: int, reason: str) -> dict:
        """Cancel a watching/triggered signal."""
        t = self._must_find(trade_id)
        if t['status'] not in ('watching', 'triggered'):
            raise ValueError(f"#{trade_id} status={t['status']}, can't cancel")
        t['status'] = 'cancelled'
        t['cancelled_at'] = datetime.now().isoformat()
        t['cancel_reason'] = reason
        t['version'] = t.get('version', 0) + 1
        self._save_local()
        self._upload_to_drive()
        logger.info("CANCELLED #%d %s: %s", trade_id, t['stock'], reason)
        return t

    def update_gap(self, trade_id: int, current_gap_pct: float) -> dict:
        """Cheap update of last seen gap on a watching signal — no Drive write."""
        t = self._must_find(trade_id)
        t['last_gap_pct'] = current_gap_pct
        t['last_gap_at'] = datetime.now().isoformat()
        # No version bump — purely advisory
        return t

    def set_alert_flag(self, trade_id: int, kind: str,
                       persist: bool = True) -> bool:
        """Idempotent: set <kind>_alerted_at on the trade if not already set.

        Returns True if the flag was newly set (caller should fire the alert),
        False if it was already set (alert already fired in a previous cycle).
        This is the persistent replacement for in-memory dedup that survives
        cron restarts.
        """
        t = self.find(trade_id)
        if not t:
            return False
        key = f"{kind}_alerted_at"
        if t.get(key):
            return False
        t[key] = datetime.now().isoformat()
        t['version'] = t.get('version', 0) + 1
        if persist:
            self._save_local()
            self._upload_to_drive()
        return True

    def set_alert_flag_daily(self, trade_id: int, kind: str,
                             persist: bool = True) -> bool:
        """Like set_alert_flag, but fires at most once per calendar day.

        Used for recurring reminders (e.g. expiry T-3..T-1 daily nag) so the
        user keeps getting nudged each day until they close the position.
        Returns True if the flag was set/refreshed today (fire the alert);
        False if it already fired today.
        """
        t = self.find(trade_id)
        if not t:
            return False
        key = f"{kind}_alerted_at"
        today_str = datetime.now().strftime('%Y-%m-%d')
        last = t.get(key, '')
        if last.startswith(today_str):
            return False
        t[key] = datetime.now().isoformat()
        t['version'] = t.get('version', 0) + 1
        if persist:
            self._save_local()
            self._upload_to_drive()
        return True

    # ── Listing ───────────────────────────────────────────────────────────
    def list_trades(self, status_filter: Optional[str] = None):
        trades = self._trades
        if status_filter:
            trades = [t for t in trades if t.get('status') == status_filter]
        if not trades:
            print("No trades found.")
            return
        print(f"\n{'ID':>3} {'Status':<10} {'Stock':<12} {'Dir':<4} "
              f"{'TF':<8} {'Strikes':<14} {'Debit':>7} {'TP':>9} {'SL':>9}  Notes")
        print("-" * 110)
        for t in trades:
            strikes = '-'
            if t.get('long_strike') and t.get('short_strike'):
                strikes = f"{int(t['long_strike'])}/{int(t['short_strike'])}"
            debit = f"{t.get('debit', 0):.2f}" if t.get('debit') else '-'
            tp = f"{t.get('tp_spot', 0):.2f}" if t.get('tp_spot') else '-'
            sl = f"{t.get('sl_spot', 0):.2f}" if t.get('sl_spot') else '-'
            notes = (t.get('notes') or t.get('exit_reason') or '')[:30]
            if t.get('structure') == 'bcs':
                notes = f"[BCS] {notes}"[:36]
            print(f"{t['id']:>3} {t.get('status', '?'):<10} {t.get('stock', '?'):<12} "
                  f"{t.get('direction', '?'):<4} {t.get('timeframe', '?'):<8} "
                  f"{strikes:<14} {debit:>7} {tp:>9} {sl:>9}  {notes}")
        print()

    # ── Sync ──────────────────────────────────────────────────────────────
    def maybe_sync(self, force: bool = False):
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Private ───────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        if not self._trades:
            return 1
        return max(t.get('id', 0) for t in self._trades) + 1

    def _must_find(self, trade_id: int) -> dict:
        t = self.find(trade_id)
        if not t:
            raise ValueError(f"Trade #{trade_id} not found")
        return t

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found at %s, local-only", creds_path)
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
            self._drive_file_id = find_file(self._drive_service, folder_id, file_name)
            self._drive_enabled = True
            logger.info("Drive enabled, file_id=%s", self._drive_file_id)
        except Exception as e:
            logger.warning("Drive init failed: %s. Local-only.", e)

    def _sync_from_drive(self):
        try:
            from bcs.drive_store import download_json
            if self._drive_file_id:
                drive_data = download_json(self._drive_service, self._drive_file_id)
                base = self._trades if self._trades else self._read_local()
                merged = self._merge(base, drive_data)
                self._trades = merged
                self._save_local()
                self._last_sync_time = time.time()
                drive_vers = {t['id']: t.get('version', 0) for t in drive_data}
                merged_vers = {t['id']: t.get('version', 0) for t in merged}
                if drive_vers != merged_vers:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                logger.info("No zebra file on Drive yet, loading local")
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
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
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
        if self._trades:
            logger.info("Loaded %d trades from local", len(self._trades))
        else:
            logger.info("No local zebra file, starting empty")

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        by_id = {t['id']: t for t in base}
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
                dir=str(cfg.LOG_DIR), suffix='.tmp', prefix='zebra_'
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


# ── Singleton ────────────────────────────────────────────────────────────
_store: Optional[ZebraStore] = None


def get_store() -> ZebraStore:
    global _store
    if _store is None:
        _store = ZebraStore()
        _store.initialize()
    return _store
