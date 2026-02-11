"""
State Manager for Expiry Trading System

Handles persistent state across script invocations.

@file        state_manager.py
@description JSON-based state persistence for expiry day trading
"""

import json
import os
import tempfile
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict
from enum import Enum
from loguru import logger

# NIFTY lot size — keep in sync with SNAIL/src/utils/calculations.py:NIFTY_LOT_SIZE
NIFTY_LOT_SIZE = 65


class TradingStatus(str, Enum):
    """Trading session status."""
    IDLE = "IDLE"
    CHECKING_EXPIRY = "CHECKING_EXPIRY"
    WAITING_ENTRY = "WAITING_ENTRY"
    ENTERING = "ENTERING"
    POSITION_OPEN = "POSITION_OPEN"
    EXITING = "EXITING"
    EXIT_COMPLETE = "EXIT_COMPLETE"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass
class LegPosition:
    """Single leg of iron condor."""
    symbol: str
    instrument_token: int
    strike: float
    option_type: str  # CE or PE
    transaction_type: str  # BUY or SELL
    quantity: int
    entry_price: float
    order_id: str = ""
    exit_price: float = 0.0
    exit_order_id: str = ""


@dataclass
class Position:
    """Iron condor position."""
    entry_time: str
    spot_at_entry: float
    vix_at_entry: float
    wing_distance: int
    legs: List[LegPosition] = field(default_factory=list)
    total_credit: float = 0.0
    target_pnl: float = 0.0
    stop_loss_pnl: float = 0.0
    lot_size: int = NIFTY_LOT_SIZE
    num_lots: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "entry_time": self.entry_time,
            "spot_at_entry": self.spot_at_entry,
            "vix_at_entry": self.vix_at_entry,
            "wing_distance": self.wing_distance,
            "legs": [asdict(leg) for leg in self.legs],
            "total_credit": self.total_credit,
            "target_pnl": self.target_pnl,
            "stop_loss_pnl": self.stop_loss_pnl,
            "lot_size": self.lot_size,
            "num_lots": self.num_lots
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Position":
        """Create from dictionary."""
        legs = [LegPosition(**leg) for leg in data.get("legs", [])]
        return cls(
            entry_time=data["entry_time"],
            spot_at_entry=data["spot_at_entry"],
            vix_at_entry=data["vix_at_entry"],
            wing_distance=data["wing_distance"],
            legs=legs,
            total_credit=data.get("total_credit", 0.0),
            target_pnl=data.get("target_pnl", 0.0),
            stop_loss_pnl=data.get("stop_loss_pnl", 0.0),
            lot_size=data.get("lot_size", NIFTY_LOT_SIZE),
            num_lots=data.get("num_lots", 1)
        )


@dataclass
class TradingState:
    """Complete trading state for a day."""
    date: str
    is_expiry: bool = False
    status: TradingStatus = TradingStatus.IDLE
    position: Optional[Position] = None
    last_pnl_update: str = ""
    last_chart_sent: str = ""
    last_vix: float = 0.0
    current_pnl: float = 0.0
    exit_reason: str = ""
    error_message: str = ""
    # Alert cooldown tracking
    last_wing_alert: str = ""
    last_vix_alert: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "date": self.date,
            "is_expiry": self.is_expiry,
            "status": self.status.value if isinstance(self.status, TradingStatus) else self.status,
            "position": self.position.to_dict() if self.position else None,
            "last_pnl_update": self.last_pnl_update,
            "last_chart_sent": self.last_chart_sent,
            "last_vix": self.last_vix,
            "current_pnl": self.current_pnl,
            "exit_reason": self.exit_reason,
            "error_message": self.error_message,
            "last_wing_alert": self.last_wing_alert,
            "last_vix_alert": self.last_vix_alert
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradingState":
        """Create from dictionary."""
        position = None
        if data.get("position"):
            position = Position.from_dict(data["position"])

        status_val = data.get("status", "IDLE")
        try:
            status = TradingStatus(status_val)
        except ValueError:
            status = TradingStatus.IDLE

        return cls(
            date=data["date"],
            is_expiry=data.get("is_expiry", False),
            status=status,
            position=position,
            last_pnl_update=data.get("last_pnl_update", ""),
            last_chart_sent=data.get("last_chart_sent", ""),
            last_vix=data.get("last_vix", 0.0),
            current_pnl=data.get("current_pnl", 0.0),
            exit_reason=data.get("exit_reason", ""),
            error_message=data.get("error_message", ""),
            last_wing_alert=data.get("last_wing_alert", ""),
            last_vix_alert=data.get("last_vix_alert", "")
        )


class StateManager:
    """
    Manages persistent state for expiry trading.

    State is stored as JSON and survives script restarts.
    Each day starts with fresh state.
    """

    def __init__(self, state_path: Path):
        """
        Initialize state manager.

        Args:
            state_path: Path to state.json file
        """
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state: Optional[TradingState] = None

    def load(self) -> TradingState:
        """
        Load state from file.

        Returns fresh state if file doesn't exist or date changed.
        """
        today = date.today().isoformat()

        if self.state_path.exists():
            try:
                with open(self.state_path, 'r') as f:
                    data = json.load(f)

                # Check if state is from today
                if data.get("date") == today:
                    self._state = TradingState.from_dict(data)
                    logger.debug(f"Loaded state: {self._state.status.value}")
                    return self._state
                else:
                    logger.info(f"State from {data.get('date')}, creating fresh state for {today}")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Failed to load state: {e}")

        # Create fresh state for today
        self._state = TradingState(date=today)
        self.save()
        logger.info(f"Created fresh state for {today}")
        return self._state

    def save(self) -> None:
        """
        Save current state to file atomically.

        Uses write-to-temp-then-rename pattern to prevent corruption
        if process is killed mid-write. On Windows, uses backup-then-rename
        for crash safety.
        """
        if self._state is None:
            return

        try:
            # Write to temporary file in same directory
            temp_fd, temp_path = tempfile.mkstemp(
                dir=self.state_path.parent,
                prefix='.state_',
                suffix='.tmp'
            )
            try:
                with os.fdopen(temp_fd, 'w') as f:
                    json.dump(self._state.to_dict(), f, indent=2)

                # Atomic rename (on POSIX) or backup-then-rename (on Windows)
                if os.name == 'nt':
                    # Windows: backup first to avoid data loss on crash
                    backup_path = self.state_path.with_suffix('.bak')
                    if self.state_path.exists():
                        # Remove old backup if exists
                        if backup_path.exists():
                            os.remove(backup_path)
                        # Rename current to backup (if crash here, backup has data)
                        os.rename(self.state_path, backup_path)
                    # Now rename temp to target (if crash here, backup still has data)
                    os.rename(temp_path, self.state_path)
                    # Clean up backup after successful write
                    if backup_path.exists():
                        os.remove(backup_path)
                else:
                    # POSIX: atomic rename
                    os.rename(temp_path, self.state_path)

                logger.debug(f"State saved: {self._state.status.value}")
            except Exception:
                # Clean up temp file on error
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    @property
    def state(self) -> TradingState:
        """Get current state, loading if needed."""
        if self._state is None:
            self._state = self.load()
        return self._state

    def update_status(self, status: TradingStatus) -> None:
        """Update trading status."""
        self.state.status = status
        self.save()
        logger.info(f"Status updated: {status.value}")

    def set_expiry_day(self, is_expiry: bool) -> None:
        """Set whether today is expiry day."""
        self.state.is_expiry = is_expiry
        self.save()

    def set_position(self, position: Position) -> None:
        """Set current position."""
        self.state.position = position
        self.state.status = TradingStatus.POSITION_OPEN
        self.save()
        logger.info("Position set and saved")

    def update_pnl(self, pnl: float, vix: float) -> None:
        """Update current P&L."""
        self.state.current_pnl = pnl
        self.state.last_vix = vix
        self.state.last_pnl_update = datetime.now().strftime("%H:%M:%S")
        self.save()

    def mark_chart_sent(self) -> None:
        """Mark that P&L chart was sent."""
        self.state.last_chart_sent = datetime.now().strftime("%H:%M:%S")
        self.save()

    def set_exit(self, reason: str, final_pnl: float) -> None:
        """Set exit details."""
        self.state.exit_reason = reason
        self.state.current_pnl = final_pnl
        self.state.status = TradingStatus.EXIT_COMPLETE
        self.save()
        logger.info(f"Exit recorded: {reason}, P&L: {final_pnl}")

    def set_error(self, message: str) -> None:
        """Set error state."""
        self.state.error_message = message
        self.state.status = TradingStatus.ERROR
        self.save()
        logger.error(f"Error state: {message}")

    def should_send_chart(self, interval_mins: int) -> bool:
        """Check if it's time to send P&L chart."""
        if not self.state.last_chart_sent:
            return True

        try:
            last_sent = datetime.strptime(self.state.last_chart_sent, "%H:%M:%S")
            now = datetime.now()
            last_sent = last_sent.replace(
                year=now.year, month=now.month, day=now.day
            )
            diff_mins = (now - last_sent).total_seconds() / 60
            return diff_mins >= interval_mins
        except ValueError:
            return True

    def is_today_processed(self) -> bool:
        """Check if today has already been processed."""
        return self.state.status in [
            TradingStatus.EXIT_COMPLETE,
            TradingStatus.SKIPPED,
            TradingStatus.ERROR
        ]

    def has_position(self) -> bool:
        """Check if there's an open position."""
        return (
            self.state.position is not None and
            self.state.status == TradingStatus.POSITION_OPEN
        )

    def should_send_wing_alert(self, cooldown_mins: int = 15) -> bool:
        """Check if wing approach alert should be sent (with cooldown)."""
        if not self.state.last_wing_alert:
            return True

        try:
            last_sent = datetime.strptime(self.state.last_wing_alert, "%H:%M:%S")
            now = datetime.now()
            last_sent = last_sent.replace(
                year=now.year, month=now.month, day=now.day
            )
            diff_mins = (now - last_sent).total_seconds() / 60
            return diff_mins >= cooldown_mins
        except ValueError:
            return True

    def mark_wing_alert_sent(self) -> None:
        """Mark that wing approach alert was sent."""
        self.state.last_wing_alert = datetime.now().strftime("%H:%M:%S")
        self.save()

    def should_send_vix_alert(self, cooldown_mins: int = 15) -> bool:
        """Check if VIX spike alert should be sent (with cooldown)."""
        if not self.state.last_vix_alert:
            return True

        try:
            last_sent = datetime.strptime(self.state.last_vix_alert, "%H:%M:%S")
            now = datetime.now()
            last_sent = last_sent.replace(
                year=now.year, month=now.month, day=now.day
            )
            diff_mins = (now - last_sent).total_seconds() / 60
            return diff_mins >= cooldown_mins
        except ValueError:
            return True

    def mark_vix_alert_sent(self) -> None:
        """Mark that VIX spike alert was sent."""
        self.state.last_vix_alert = datetime.now().strftime("%H:%M:%S")
        self.save()


def save_trade_record(
    trades_dir: Path,
    state: TradingState,
    final_pnl: float,
    gross_pnl: float,
    charges: float
) -> Path:
    """
    Save completed trade to trades directory atomically.

    Uses write-to-temp-then-rename pattern to prevent corruption.

    Args:
        trades_dir: Directory for trade records
        state: Trading state with position details
        final_pnl: Net P&L after charges
        gross_pnl: P&L before charges
        charges: Total transaction charges

    Returns:
        Path to saved trade file
    """
    trades_dir = Path(trades_dir)
    trades_dir.mkdir(parents=True, exist_ok=True)

    trade_record = {
        "date": state.date,
        "entry_time": state.position.entry_time if state.position else "",
        "exit_time": datetime.now().strftime("%H:%M:%S"),
        "exit_reason": state.exit_reason,
        "spot_at_entry": state.position.spot_at_entry if state.position else 0,
        "vix_at_entry": state.position.vix_at_entry if state.position else 0,
        "wing_distance": state.position.wing_distance if state.position else 0,
        "total_credit": state.position.total_credit if state.position else 0,
        "lot_size": state.position.lot_size if state.position else NIFTY_LOT_SIZE,
        "num_lots": state.position.num_lots if state.position else 1,
        "legs": [asdict(leg) for leg in state.position.legs] if state.position else [],
        "gross_pnl": gross_pnl,
        "charges": charges,
        "net_pnl": final_pnl,
        "target_pnl": state.position.target_pnl if state.position else 0,
        "stop_loss_pnl": state.position.stop_loss_pnl if state.position else 0
    }

    filename = f"trade_{state.date}.json"
    filepath = trades_dir / filename

    # Atomic write: temp file then rename
    temp_fd, temp_path = tempfile.mkstemp(
        dir=trades_dir,
        prefix='.trade_',
        suffix='.tmp'
    )
    try:
        with os.fdopen(temp_fd, 'w') as f:
            json.dump(trade_record, f, indent=2)

        # Atomic rename (Windows needs target removed first)
        if os.name == 'nt' and filepath.exists():
            os.remove(filepath)
        os.rename(temp_path, filepath)

        logger.info(f"Trade record saved: {filepath}")
        return filepath
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise
