"""
SNAIL Database Utilities

Database initialization, connection management, and common queries.

@file        db.py
@description SQLite database utilities with WAL mode support
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN.md Section 4, TECHNICAL_DESIGN_REFERENCE.md Section 1
"""

import sqlite3
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass
from contextlib import contextmanager
from loguru import logger


# =============================================================================
# PATH CONFIGURATION
# =============================================================================

DATABASE_PATH = Path(__file__).parent.parent.parent / "data" / "snail.db"
SCHEMA_PATH = Path(__file__).parent.parent.parent / "data" / "schema.sql"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Position:
    """Position record data class."""
    id: int
    status: str
    entry_time: datetime
    exit_time: Optional[datetime]
    expiry_date: date
    atm_strike: int
    wing_distance: int
    lot_size: int
    entry_premium: float
    straddle_credit: float
    wing_debit: float
    max_profit: float
    max_loss: float
    margin_deployed: float
    entry_charges: float
    exit_premium: Optional[float] = None
    exit_charges: Optional[float] = None
    net_pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    exit_reason: Optional[str] = None
    verified: bool = True
    # Trailing profit fields
    peak_pnl_pct: Optional[float] = None          # Highest P&L % recorded
    peak_pnl_amount: Optional[float] = None       # Highest P&L amount recorded
    peak_pnl_time: Optional[datetime] = None      # When peak was hit
    trailing_active: bool = False                  # Whether trailing is active
    trailing_floor_pct: Optional[float] = None    # Current exit threshold %
    breakeven_locked: bool = False                 # Whether breakeven is locked
    # Exit in progress guard (prevents duplicate exit triggers)
    exit_in_progress: bool = False                 # True when exit is being executed


@dataclass
class PositionLeg:
    """Position leg record data class."""
    id: int
    position_id: int
    leg_type: str
    option_type: str
    strike: int
    tradingsymbol: str
    entry_price: float
    exit_price: Optional[float]
    quantity: int
    instrument_token: str


@dataclass
class Order:
    """Order record data class."""
    id: int
    position_id: Optional[int]
    kite_order_id: str
    order_type: str
    transaction_type: str
    tradingsymbol: str
    strike: int
    option_type: str
    price: float
    quantity: int
    status: str
    fill_price: Optional[float] = None
    slippage: Optional[float] = None


@dataclass
class PnLSnapshot:
    """P&L snapshot data class."""
    id: int
    position_id: int
    current_pnl: float
    pnl_percent: float
    nifty_spot: float
    vix: float
    ce_bid: float
    ce_ask: float
    pe_bid: float
    pe_ask: float
    wing_ce_bid: Optional[float] = None
    wing_ce_ask: Optional[float] = None
    wing_pe_bid: Optional[float] = None
    wing_pe_ask: Optional[float] = None
    timestamp: Optional[datetime] = None  # Maps to created_at in DB


@dataclass
class MarketData:
    """Market data record data class."""
    id: int
    date: date
    atr_14: float
    previous_close: float
    day_open: Optional[float] = None
    day_open_captured_at: Optional[datetime] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None


@dataclass
class SystemStatus:
    """System status data class."""
    id: int
    status: str
    reason: Optional[str]
    set_at: datetime
    set_by: str
    cleared_at: Optional[datetime] = None
    cleared_by: Optional[str] = None


# =============================================================================
# DATABASE CONNECTION MANAGEMENT
# =============================================================================

def init_database(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Initialize SQLite database with WAL mode and schema.

    Creates database file if it doesn't exist and applies schema.

    Args:
        db_path: Optional path to database file. Defaults to data/snail.db

    Returns:
        Database connection

    Raises:
        FileNotFoundError: If schema file is missing
        sqlite3.Error: If database initialization fails
    """
    db_path = db_path or DATABASE_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initializing database at {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Apply schema
    if SCHEMA_PATH.exists():
        schema_sql = SCHEMA_PATH.read_text()
        conn.executescript(schema_sql)
        logger.info("Database schema applied successfully")
    else:
        logger.warning(f"Schema file not found at {SCHEMA_PATH}")

    # Run migrations for existing databases
    _run_migrations(conn)

    conn.commit()
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """
    Run database migrations to update schema for existing databases.

    Args:
        conn: Database connection
    """
    # Migration 1: Add cooldown_type column to cooldowns table if not exists
    cursor = conn.execute("PRAGMA table_info(cooldowns)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'cooldown_type' not in columns:
        logger.info("Running migration: Adding cooldown_type to cooldowns table")
        conn.execute("ALTER TABLE cooldowns ADD COLUMN cooldown_type TEXT NOT NULL DEFAULT 'entry'")
        logger.info("Migration complete: cooldown_type column added")

    # Migration 3: Add trailing profit columns to positions table
    cursor = conn.execute("PRAGMA table_info(positions)")
    position_columns = [row[1] for row in cursor.fetchall()]

    trailing_columns = [
        ('peak_pnl_pct', 'REAL'),
        ('peak_pnl_amount', 'REAL'),
        ('peak_pnl_time', 'DATETIME'),
        ('trailing_active', 'INTEGER DEFAULT 0'),
        ('trailing_floor_pct', 'REAL'),
        ('breakeven_locked', 'INTEGER DEFAULT 0'),
        ('exit_in_progress', 'INTEGER DEFAULT 0'),  # Guard against duplicate exits
    ]

    for col_name, col_type in trailing_columns:
        if col_name not in position_columns:
            logger.info(f"Running migration: Adding {col_name} to positions table")
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type}")
            logger.info(f"Migration complete: {col_name} column added")

    # Migration 2: Fix cooldowns table to allow NULL position_id
    # Check if position_id has NOT NULL constraint (by checking if we can insert NULL)
    try:
        # Check table schema for notnull flag on position_id
        cursor = conn.execute("PRAGMA table_info(cooldowns)")
        for row in cursor.fetchall():
            col_name = row[1]
            notnull = row[3]
            if col_name == 'position_id' and notnull:
                logger.info("Running migration: Fixing cooldowns table to allow NULL position_id")
                # Recreate the table with correct schema
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS cooldowns_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        position_id INTEGER REFERENCES positions(id),
                        cooldown_type TEXT NOT NULL DEFAULT 'entry',
                        exit_date DATE NOT NULL,
                        cooldown_end DATE NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("INSERT INTO cooldowns_new SELECT * FROM cooldowns")
                conn.execute("DROP TABLE cooldowns")
                conn.execute("ALTER TABLE cooldowns_new RENAME TO cooldowns")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_cooldowns_end ON cooldowns(cooldown_end)")
                logger.info("Migration complete: cooldowns table recreated with nullable position_id")
                break
    except Exception as e:
        logger.warning(f"Migration 2 check failed: {e}")


_migrations_run = False  # Track if migrations have been run this session


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Get database connection with WAL mode enabled.

    Args:
        db_path: Optional path to database file

    Returns:
        Database connection with row factory set
    """
    global _migrations_run
    db_path = db_path or DATABASE_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")

    # Run migrations once per session
    if not _migrations_run:
        _run_migrations(conn)
        conn.commit()
        _migrations_run = True

    return conn


@contextmanager
def get_db_session(db_path: Optional[Path] = None):
    """
    Context manager for database sessions with automatic cleanup.

    Yields:
        Database connection

    Example:
        with get_db_session() as conn:
            conn.execute("SELECT * FROM positions")
    """
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()


# =============================================================================
# POSITION OPERATIONS
# =============================================================================

def get_active_position() -> Optional[Position]:
    """
    Get the currently active position, if any.

    Returns:
        Position object or None if no active position
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM positions WHERE status = 'active' LIMIT 1"
        )
        row = cursor.fetchone()

        if row:
            return _row_to_position(row)
        return None


def get_position_by_id(position_id: int) -> Optional[Position]:
    """
    Get position by ID.

    Args:
        position_id: Position ID

    Returns:
        Position object or None
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM positions WHERE id = ?",
            (position_id,)
        )
        row = cursor.fetchone()

        if row:
            return _row_to_position(row)
        return None


def create_position(
    status: str,
    entry_time: datetime,
    expiry_date: date,
    atm_strike: int,
    wing_distance: int,
    lot_size: int,
    entry_premium: float,
    straddle_credit: float,
    wing_debit: float,
    max_profit: float,
    max_loss: float,
    margin_deployed: float,
    entry_charges: float
) -> int:
    """
    Create a new position record.

    Args:
        status: Initial status ('pending' or 'active')
        entry_time: Entry timestamp
        expiry_date: Option expiry date
        atm_strike: ATM strike price
        wing_distance: Wing distance in points
        lot_size: Lot size
        entry_premium: Net credit per unit
        straddle_credit: Straddle credit per unit
        wing_debit: Wing debit per unit
        max_profit: Maximum profit in INR
        max_loss: Maximum loss in INR
        margin_deployed: Margin used in INR
        entry_charges: Entry transaction charges

    Returns:
        New position ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO positions (
                status, entry_time, expiry_date, atm_strike, wing_distance,
                lot_size, entry_premium, straddle_credit, wing_debit,
                max_profit, max_loss, margin_deployed, entry_charges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                status,
                entry_time.isoformat(),
                expiry_date.isoformat(),
                atm_strike,
                wing_distance,
                lot_size,
                entry_premium,
                straddle_credit,
                wing_debit,
                max_profit,
                max_loss,
                margin_deployed,
                entry_charges
            )
        )
        position_id = cursor.lastrowid
        logger.info(f"Created position {position_id}")
        return position_id


def update_position_status(
    position_id: int,
    status: str,
    exit_time: Optional[datetime] = None,
    exit_reason: Optional[str] = None,
    realized_pnl: Optional[float] = None
) -> None:
    """
    Update position status.

    Args:
        position_id: Position ID
        status: New status
        exit_time: Exit timestamp (for closing)
        exit_reason: Reason for exit
        realized_pnl: Realized P&L
    """
    with get_db_session() as conn:
        if exit_time and exit_reason:
            conn.execute(
                """UPDATE positions SET status = ?, exit_time = ?, exit_reason = ?,
                   net_pnl = ?, updated_at = ? WHERE id = ?""",
                (status, exit_time.isoformat(), exit_reason, realized_pnl,
                 datetime.now().isoformat(), position_id)
            )
        else:
            conn.execute(
                "UPDATE positions SET status = ?, updated_at = ? WHERE id = ?",
                (status, datetime.now().isoformat(), position_id)
            )
        logger.info(f"Updated position {position_id} status to {status}")


# =============================================================================
# TRAILING PROFIT FUNCTIONS
# =============================================================================

def update_trailing_state(
    position_id: int,
    peak_pnl_pct: Optional[float] = None,
    peak_pnl_amount: Optional[float] = None,
    peak_pnl_time: Optional[datetime] = None,
    trailing_active: Optional[bool] = None,
    trailing_floor_pct: Optional[float] = None,
    breakeven_locked: Optional[bool] = None
) -> None:
    """
    Update trailing profit state for a position.

    Only updates fields that are provided (not None).

    Args:
        position_id: Position ID
        peak_pnl_pct: New peak P&L percentage
        peak_pnl_amount: New peak P&L amount
        peak_pnl_time: When peak was hit
        trailing_active: Whether trailing is now active
        trailing_floor_pct: Current trailing floor
        breakeven_locked: Whether breakeven is locked
    """
    updates: List[str] = []
    params: List[Any] = []

    if peak_pnl_pct is not None:
        updates.append("peak_pnl_pct = ?")
        params.append(peak_pnl_pct)

    if peak_pnl_amount is not None:
        updates.append("peak_pnl_amount = ?")
        params.append(peak_pnl_amount)

    if peak_pnl_time is not None:
        updates.append("peak_pnl_time = ?")
        params.append(peak_pnl_time.isoformat())

    if trailing_active is not None:
        updates.append("trailing_active = ?")
        params.append(1 if trailing_active else 0)

    if trailing_floor_pct is not None:
        updates.append("trailing_floor_pct = ?")
        params.append(trailing_floor_pct)

    if breakeven_locked is not None:
        updates.append("breakeven_locked = ?")
        params.append(1 if breakeven_locked else 0)

    if not updates:
        return  # Nothing to update

    updates.append("updated_at = ?")
    params.append(datetime.now().isoformat())
    params.append(position_id)

    sql = f"UPDATE positions SET {', '.join(updates)} WHERE id = ?"

    with get_db_session() as conn:
        conn.execute(sql, params)
        logger.debug(f"Updated trailing state for position {position_id}")


def activate_trailing(
    position_id: int,
    current_pnl_pct: float,
    current_pnl_amount: float,
    trail_pct: float
) -> float:
    """
    Activate trailing profit for a position.

    Called when P&L first hits the activation threshold.

    Args:
        position_id: Position ID
        current_pnl_pct: Current P&L percentage (becomes initial peak)
        current_pnl_amount: Current P&L amount
        trail_pct: Trailing percentage from config

    Returns:
        Initial trailing floor percentage
    """
    floor_pct = round(current_pnl_pct - trail_pct, 4)

    update_trailing_state(
        position_id=position_id,
        peak_pnl_pct=current_pnl_pct,
        peak_pnl_amount=current_pnl_amount,
        peak_pnl_time=datetime.now(),
        trailing_active=True,
        trailing_floor_pct=floor_pct,
        breakeven_locked=False
    )

    logger.info(
        f"Trailing ACTIVATED for position {position_id}: "
        f"peak={current_pnl_pct:.2f}%, floor={floor_pct:.2f}%"
    )

    return floor_pct


def update_trailing_peak(
    position_id: int,
    new_peak_pct: float,
    new_peak_amount: float,
    trail_pct: float,
    lock_breakeven_at: float,
    current_breakeven_locked: bool
) -> Tuple[float, bool]:
    """
    Update trailing peak when a new high is reached.

    Args:
        position_id: Position ID
        new_peak_pct: New peak P&L percentage
        new_peak_amount: New peak P&L amount
        trail_pct: Trailing percentage from config
        lock_breakeven_at: Threshold to lock breakeven
        current_breakeven_locked: Whether breakeven is already locked

    Returns:
        Tuple of (new_floor_pct, breakeven_locked)
    """
    # Calculate new floor
    new_floor = round(new_peak_pct - trail_pct, 4)

    # Check if breakeven should be locked
    should_lock_breakeven = new_peak_pct >= lock_breakeven_at

    # If breakeven is locked, floor can never go below 0
    if should_lock_breakeven or current_breakeven_locked:
        new_floor = max(0.0, new_floor)

    update_trailing_state(
        position_id=position_id,
        peak_pnl_pct=new_peak_pct,
        peak_pnl_amount=new_peak_amount,
        peak_pnl_time=datetime.now(),
        trailing_floor_pct=new_floor,
        breakeven_locked=should_lock_breakeven or current_breakeven_locked
    )

    if should_lock_breakeven and not current_breakeven_locked:
        logger.info(
            f"BREAKEVEN LOCKED for position {position_id}: "
            f"peak={new_peak_pct:.2f}%, floor={new_floor:.2f}%"
        )
    else:
        logger.debug(
            f"Trailing peak updated for position {position_id}: "
            f"peak={new_peak_pct:.2f}%, floor={new_floor:.2f}%"
        )

    return new_floor, should_lock_breakeven or current_breakeven_locked


def get_trailing_state(position_id: int) -> Dict[str, Any]:
    """
    Get current trailing profit state for a position.

    Args:
        position_id: Position ID

    Returns:
        Dict with trailing state fields, or empty dict if not found
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT peak_pnl_pct, peak_pnl_amount, peak_pnl_time,
                      trailing_active, trailing_floor_pct, breakeven_locked
               FROM positions WHERE id = ?""",
            (position_id,)
        )
        row = cursor.fetchone()

        if not row:
            return {}

        # Parse peak_pnl_time if present
        peak_time = None
        if row['peak_pnl_time']:
            try:
                peak_time = datetime.fromisoformat(row['peak_pnl_time'])
            except (ValueError, TypeError):
                pass

        return {
            'peak_pnl_pct': row['peak_pnl_pct'],
            'peak_pnl_amount': row['peak_pnl_amount'],
            'peak_pnl_time': peak_time,
            'trailing_active': bool(row['trailing_active']) if row['trailing_active'] is not None else False,
            'trailing_floor_pct': row['trailing_floor_pct'],
            'breakeven_locked': bool(row['breakeven_locked']) if row['breakeven_locked'] is not None else False,
        }


def reset_trailing_state(position_id: int) -> None:
    """
    Reset trailing profit state for a position.

    Called when position is closed or needs to start fresh.
    Uses direct SQL to NULL out peak/floor fields because
    update_trailing_state() skips None values by design.

    Args:
        position_id: Position ID
    """
    with get_db_session() as conn:
        conn.execute(
            """UPDATE positions SET
               trailing_active = 0,
               trailing_floor_pct = NULL,
               peak_pnl_pct = NULL,
               peak_pnl_amount = NULL,
               breakeven_locked = 0,
               updated_at = ?
               WHERE id = ?""",
            (datetime.now().isoformat(), position_id)
        )
    logger.info(f"Trailing state reset for position {position_id}")


# =============================================================================
# EXIT IN PROGRESS GUARD
# =============================================================================
# These functions prevent duplicate exit triggers (CRITICAL BUG FIX)
# When exit is triggered, we set exit_in_progress=True IMMEDIATELY
# This prevents the monitor from triggering another exit before the first completes

def set_exit_in_progress(position_id: int) -> bool:
    """
    Set exit_in_progress flag to prevent duplicate exit triggers.

    Uses atomic update with WHERE clause to prevent race conditions.
    Only sets the flag if it's not already set.

    Args:
        position_id: Position ID

    Returns:
        True if flag was set successfully, False if already in progress
    """
    with get_db_session() as conn:
        # Atomic check-and-set using WHERE clause
        cursor = conn.execute(
            """UPDATE positions
               SET exit_in_progress = 1, updated_at = ?
               WHERE id = ? AND (exit_in_progress = 0 OR exit_in_progress IS NULL)""",
            (datetime.now().isoformat(), position_id)
        )

        if cursor.rowcount > 0:
            logger.info(f"Exit in progress SET for position {position_id}")
            return True
        else:
            logger.warning(f"Exit already in progress for position {position_id} - blocking duplicate")
            return False


def is_exit_in_progress(position_id: int) -> bool:
    """
    Check if an exit is already in progress for a position.

    Args:
        position_id: Position ID

    Returns:
        True if exit is in progress, False otherwise
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT exit_in_progress FROM positions WHERE id = ?",
            (position_id,)
        )
        row = cursor.fetchone()

        if not row:
            return False

        return bool(row['exit_in_progress']) if row['exit_in_progress'] is not None else False


def clear_exit_in_progress(position_id: int) -> None:
    """
    Clear exit_in_progress flag after exit completes or fails.

    Called after exit manager finishes (success or failure).

    Args:
        position_id: Position ID
    """
    with get_db_session() as conn:
        conn.execute(
            "UPDATE positions SET exit_in_progress = 0, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), position_id)
        )
        logger.info(f"Exit in progress CLEARED for position {position_id}")


def record_failed_exit(
    position_id: int,
    legs_closed: List[str],
    legs_failed: Dict[str, str],
    error: str,
    partial: bool = False
) -> None:
    """
    Record a failed or partial exit attempt for audit trail.

    ISSUE-008: When exit verification fails or partial exit occurs,
    we must record what happened so:
    1. Position shows correct status (not 'active')
    2. User/admin knows which legs were closed
    3. Manual intervention can be informed by the data

    Args:
        position_id: Position ID
        legs_closed: List of leg types successfully closed
        legs_failed: Dict of leg type -> error message for failures
        error: Overall error message
        partial: True if some legs closed (partial exit)
    """
    # Use 'exiting' status (valid in schema) to indicate incomplete exit
    # Position requires manual intervention
    status = 'exiting'

    # Build detailed error info for logging (schema doesn't have notes column)
    notes_parts = []
    if legs_closed:
        notes_parts.append(f"Closed: {', '.join(legs_closed)}")
    if legs_failed:
        failed_details = "; ".join(f"{k}: {v}" for k, v in legs_failed.items())
        notes_parts.append(f"Failed: {failed_details}")
    notes_parts.append(f"Error: {error}")
    notes_text = " | ".join(notes_parts)

    with get_db_session() as conn:
        # Update position to 'exiting' status - requires manual intervention
        # Cannot use 'exit_failed' as exit_reason (not in schema CHECK constraint)
        conn.execute(
            """UPDATE positions SET
                status = ?,
                updated_at = ?
            WHERE id = ?""",
            (status, datetime.now().isoformat(), position_id)
        )

    # Log full details for audit trail since we can't store in DB
    logger.critical(
        f"RECORDED FAILED EXIT: position={position_id}, status={status}, "
        f"closed={legs_closed}, failed={list(legs_failed.keys())}, "
        f"details={notes_text}"
    )


def close_position(
    position_id: int,
    exit_time: datetime,
    exit_premium: float,
    exit_charges: float,
    net_pnl: float,
    pnl_percent: float,
    exit_reason: str
) -> None:
    """
    Close a position with exit details.

    Args:
        position_id: Position ID
        exit_time: Exit timestamp
        exit_premium: Exit premium per unit
        exit_charges: Exit transaction charges
        net_pnl: Net P&L in INR
        pnl_percent: P&L as percentage of margin
        exit_reason: Reason for exit
    """
    with get_db_session() as conn:
        conn.execute(
            """UPDATE positions SET
                status = 'closed',
                exit_time = ?,
                exit_premium = ?,
                exit_charges = ?,
                net_pnl = ?,
                pnl_percent = ?,
                exit_reason = ?,
                updated_at = ?
            WHERE id = ?""",
            (
                exit_time.isoformat(),
                exit_premium,
                exit_charges,
                net_pnl,
                pnl_percent,
                exit_reason,
                datetime.now().isoformat(),
                position_id
            )
        )
        logger.info(f"Closed position {position_id} with P&L ₹{net_pnl:.2f}")


def mark_position_unverified(position_id: int) -> None:
    """
    Mark a position as unverified (mismatch detected).

    Args:
        position_id: Position ID
    """
    with get_db_session() as conn:
        conn.execute(
            "UPDATE positions SET verified = 0, updated_at = ? WHERE id = ?",
            (datetime.now().isoformat(), position_id)
        )
        logger.warning(f"Position {position_id} marked as unverified")


def save_position(position: Any) -> int:
    """
    Save a position object to database.

    Args:
        position: Position object or dataclass

    Returns:
        Position ID
    """
    # Handle both dataclass and dict-like objects
    if hasattr(position, '__dict__'):
        data = position.__dict__ if not hasattr(position, 'asdict') else position.asdict()
    else:
        data = dict(position)

    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO positions (
                status, entry_time, expiry_date, atm_strike, wing_distance,
                lot_size, entry_premium, straddle_credit, wing_debit,
                max_profit, max_loss, margin_deployed, entry_charges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get('status', 'active'),
                data.get('entry_time', datetime.now()).isoformat() if isinstance(data.get('entry_time'), datetime) else data.get('entry_time'),
                data.get('expiry', data.get('expiry_date', date.today())).isoformat() if isinstance(data.get('expiry', data.get('expiry_date')), date) else data.get('expiry', data.get('expiry_date')),
                data.get('atm_strike', 0),
                data.get('wing_distance', 0),
                data.get('quantity', data.get('lot_size', 75)),
                data.get('entry_credit', data.get('entry_premium', 0)),
                data.get('entry_credit', data.get('straddle_credit', 0)),
                data.get('wing_debit', 0),
                data.get('max_profit', 0),
                data.get('max_loss', 0),
                data.get('margin_deployed', 0),
                data.get('entry_charges', 0)
            )
        )
        position_id = cursor.lastrowid
        logger.info(f"Saved position {position_id}")
        return position_id


def save_position_leg(leg: Any) -> int:
    """
    Save a position leg to database.

    Args:
        leg: Position leg object

    Returns:
        Leg ID
    """
    if hasattr(leg, '__dict__'):
        data = leg.__dict__
    else:
        data = dict(leg)

    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO position_legs (
                position_id, leg_type, option_type, strike,
                tradingsymbol, entry_price, quantity, instrument_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get('position_id'),
                data.get('leg_type'),
                data.get('option_type'),
                data.get('strike'),
                data.get('tradingsymbol'),
                data.get('entry_price'),
                data.get('quantity'),
                data.get('instrument_token', data.get('entry_order_id', ''))
            )
        )
        return cursor.lastrowid


def save_position_with_legs(position: Any, legs: List[Any]) -> int:
    """
    Save position and all legs in a single atomic transaction.

    This ensures database consistency - either all data is saved or none.
    Prevents orphaned position records if leg insertion fails.

    Args:
        position: Position object or dataclass
        legs: List of position leg objects

    Returns:
        Position ID

    Raises:
        Exception: If any part of the save fails (entire transaction rolled back)
    """
    # Handle both dataclass and dict-like objects for position
    if hasattr(position, '__dict__'):
        pos_data = position.__dict__ if not hasattr(position, 'asdict') else position.asdict()
    else:
        pos_data = dict(position)

    with get_db_session() as conn:
        # Insert position
        cursor = conn.execute(
            """INSERT INTO positions (
                status, entry_time, expiry_date, atm_strike, wing_distance,
                lot_size, entry_premium, straddle_credit, wing_debit,
                max_profit, max_loss, margin_deployed, entry_charges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos_data.get('status', 'active'),
                pos_data.get('entry_time', datetime.now()).isoformat() if isinstance(pos_data.get('entry_time'), datetime) else pos_data.get('entry_time'),
                pos_data.get('expiry', pos_data.get('expiry_date', date.today())).isoformat() if isinstance(pos_data.get('expiry', pos_data.get('expiry_date')), date) else pos_data.get('expiry', pos_data.get('expiry_date')),
                pos_data.get('atm_strike', 0),
                pos_data.get('wing_distance', 0),
                pos_data.get('quantity', pos_data.get('lot_size', 75)),
                pos_data.get('entry_credit', pos_data.get('entry_premium', 0)),
                pos_data.get('entry_credit', pos_data.get('straddle_credit', 0)),
                pos_data.get('wing_debit', 0),
                pos_data.get('max_profit', 0),
                pos_data.get('max_loss', 0),
                pos_data.get('margin_deployed', 0),
                pos_data.get('entry_charges', 0)
            )
        )
        position_id = cursor.lastrowid

        # Insert all legs with the new position_id
        for leg in legs:
            if hasattr(leg, '__dict__'):
                leg_data = leg.__dict__
            else:
                leg_data = dict(leg)

            conn.execute(
                """INSERT INTO position_legs (
                    position_id, leg_type, option_type, strike,
                    tradingsymbol, entry_price, quantity, instrument_token
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    position_id,  # Use the newly created position_id
                    leg_data.get('leg_type'),
                    leg_data.get('option_type'),
                    leg_data.get('strike'),
                    leg_data.get('tradingsymbol'),
                    leg_data.get('entry_price'),
                    leg_data.get('quantity'),
                    leg_data.get('instrument_token', leg_data.get('entry_order_id', ''))
                )
            )

        logger.info(f"Saved position {position_id} with {len(legs)} legs (atomic)")
        return position_id


def save_order(order: Any) -> int:
    """
    Save an order to database.

    Args:
        order: Order object

    Returns:
        Order ID
    """
    if hasattr(order, '__dict__'):
        data = order.__dict__
    else:
        data = dict(order)

    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO orders (
                position_id, kite_order_id, order_type, transaction_type,
                tradingsymbol, strike, option_type, price, quantity, status,
                fill_price, slippage
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get('position_id'),
                data.get('order_id', data.get('kite_order_id')),
                data.get('order_type', 'LIMIT'),
                data.get('transaction_type'),
                data.get('tradingsymbol'),
                data.get('strike', 0),
                data.get('option_type', ''),
                data.get('price'),
                data.get('quantity'),
                data.get('status'),
                data.get('fill_price'),
                data.get('slippage')
            )
        )
        return cursor.lastrowid


def save_pnl_snapshot(snapshot: Any) -> int:
    """
    Save a P&L snapshot to database.

    Args:
        snapshot: PnL snapshot object (PnLSnapshot dataclass)

    Returns:
        Snapshot ID
    """
    if hasattr(snapshot, '__dict__'):
        data = snapshot.__dict__
    else:
        data = dict(snapshot)

    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO pnl_snapshots (
                position_id, current_pnl, pnl_percent, nifty_spot, vix,
                ce_bid, ce_ask, pe_bid, pe_ask,
                wing_ce_bid, wing_ce_ask, wing_pe_bid, wing_pe_ask
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.get('position_id'),
                data.get('current_pnl', 0),
                data.get('pnl_percent', 0),
                data.get('nifty_spot', 0),
                data.get('vix', 0),
                data.get('ce_bid', 0),
                data.get('ce_ask', 0),
                data.get('pe_bid', 0),
                data.get('pe_ask', 0),
                data.get('wing_ce_bid'),
                data.get('wing_ce_ask'),
                data.get('wing_pe_bid'),
                data.get('wing_pe_ask')
            )
        )
        return cursor.lastrowid


def save_claude_decision(
    position_id: Optional[int],
    trigger_type: str,
    prompt: str,
    response: str,
    decision: Optional[str],
    model_used: str = 'haiku',
    tokens_used: Optional[int] = None
) -> int:
    """
    Save a Claude decision to database.

    Args:
        position_id: Position ID (if applicable)
        trigger_type: Decision trigger type (pre_entry, stop_loss_advisory, etc.)
        prompt: The prompt sent to Claude
        response: Claude's full response text
        decision: The parsed decision (ENTER, EXIT, HOLD, etc.)
        model_used: Model used (haiku or sonnet)
        tokens_used: Number of tokens used

    Returns:
        Decision ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO claude_decisions (
                position_id, trigger_type, prompt, response, decision, model_used, tokens_used
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id,
                trigger_type,
                prompt,
                response,
                decision,
                model_used,
                tokens_used
            )
        )
        return cursor.lastrowid


# =============================================================================
# POSITION LEGS OPERATIONS
# =============================================================================

def create_position_leg(
    position_id: int,
    leg_type: str,
    option_type: str,
    strike: int,
    tradingsymbol: str,
    entry_price: float,
    quantity: int,
    instrument_token: str
) -> int:
    """
    Create a position leg record.

    Args:
        position_id: Parent position ID
        leg_type: Leg type (straddle_ce, straddle_pe, wing_ce, wing_pe)
        option_type: CE or PE
        strike: Strike price
        tradingsymbol: Trading symbol
        entry_price: Entry price
        quantity: Quantity
        instrument_token: Instrument token

    Returns:
        New leg ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO position_legs (
                position_id, leg_type, option_type, strike,
                tradingsymbol, entry_price, quantity, instrument_token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id, leg_type, option_type, strike,
                tradingsymbol, entry_price, quantity, instrument_token
            )
        )
        return cursor.lastrowid


def get_position_legs(position_id: int) -> List[PositionLeg]:
    """
    Get all legs for a position.

    Args:
        position_id: Position ID

    Returns:
        List of PositionLeg objects
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM position_legs WHERE position_id = ?",
            (position_id,)
        )
        return [_row_to_leg(row) for row in cursor.fetchall()]


def update_leg_exit_price(leg_id: int, exit_price: float) -> None:
    """
    Update leg exit price.

    Args:
        leg_id: Leg ID
        exit_price: Exit price
    """
    with get_db_session() as conn:
        conn.execute(
            "UPDATE position_legs SET exit_price = ? WHERE id = ?",
            (exit_price, leg_id)
        )


# =============================================================================
# ORDER OPERATIONS
# =============================================================================

def create_order(
    kite_order_id: str,
    order_type: str,
    transaction_type: str,
    tradingsymbol: str,
    strike: int,
    option_type: str,
    price: float,
    quantity: int,
    status: str,
    position_id: Optional[int] = None
) -> int:
    """
    Create an order record.

    Args:
        kite_order_id: Kite order ID
        order_type: LIMIT or MARKET
        transaction_type: BUY or SELL
        tradingsymbol: Trading symbol
        strike: Strike price
        option_type: CE or PE
        price: Order price
        quantity: Order quantity
        status: Order status
        position_id: Optional position ID

    Returns:
        New order ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO orders (
                position_id, kite_order_id, order_type, transaction_type,
                tradingsymbol, strike, option_type, price, quantity, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id, kite_order_id, order_type, transaction_type,
                tradingsymbol, strike, option_type, price, quantity, status
            )
        )
        return cursor.lastrowid


def update_order_status(
    kite_order_id: str,
    status: str,
    fill_price: Optional[float] = None,
    slippage: Optional[float] = None
) -> None:
    """
    Update order status.

    Args:
        kite_order_id: Kite order ID
        status: New status
        fill_price: Fill price if filled
        slippage: Slippage if any
    """
    with get_db_session() as conn:
        if fill_price is not None:
            conn.execute(
                """UPDATE orders SET status = ?, fill_price = ?, slippage = ?,
                   updated_at = ? WHERE kite_order_id = ?""",
                (status, fill_price, slippage, datetime.now().isoformat(), kite_order_id)
            )
        else:
            conn.execute(
                "UPDATE orders SET status = ?, updated_at = ? WHERE kite_order_id = ?",
                (status, datetime.now().isoformat(), kite_order_id)
            )


def link_orders_to_position(position_id: int, order_ids: List[str]) -> None:
    """
    Link orders to a position after position creation.

    Args:
        position_id: Position ID
        order_ids: List of Kite order IDs
    """
    with get_db_session() as conn:
        placeholders = ','.join(['?' for _ in order_ids])
        conn.execute(
            f"""UPDATE orders SET position_id = ?
                WHERE kite_order_id IN ({placeholders})""",
            [position_id] + order_ids
        )
        logger.info(f"Linked {len(order_ids)} orders to position {position_id}")


# =============================================================================
# P&L SNAPSHOT OPERATIONS
# =============================================================================

def create_pnl_snapshot(
    position_id: int,
    current_pnl: float,
    pnl_percent: float,
    nifty_spot: float,
    vix: float,
    ce_bid: float,
    ce_ask: float,
    pe_bid: float,
    pe_ask: float,
    wing_ce_bid: Optional[float] = None,
    wing_ce_ask: Optional[float] = None,
    wing_pe_bid: Optional[float] = None,
    wing_pe_ask: Optional[float] = None
) -> int:
    """
    Create a P&L snapshot.

    Args:
        position_id: Position ID
        current_pnl: Current P&L in INR
        pnl_percent: P&L percentage
        nifty_spot: NIFTY spot price
        vix: India VIX value
        ce_bid/ask: ATM CE bid/ask
        pe_bid/ask: ATM PE bid/ask
        wing_ce_bid/ask: Wing CE bid/ask (optional)
        wing_pe_bid/ask: Wing PE bid/ask (optional)

    Returns:
        Snapshot ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO pnl_snapshots (
                position_id, current_pnl, pnl_percent, nifty_spot, vix,
                ce_bid, ce_ask, pe_bid, pe_ask,
                wing_ce_bid, wing_ce_ask, wing_pe_bid, wing_pe_ask
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id, current_pnl, pnl_percent, nifty_spot, vix,
                ce_bid, ce_ask, pe_bid, pe_ask,
                wing_ce_bid, wing_ce_ask, wing_pe_bid, wing_pe_ask
            )
        )
        return cursor.lastrowid


def get_latest_pnl_snapshot(position_id: int) -> Optional[PnLSnapshot]:
    """
    Get the latest P&L snapshot for a position.

    Args:
        position_id: Position ID

    Returns:
        PnLSnapshot object or None
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT * FROM pnl_snapshots
               WHERE position_id = ?
               ORDER BY created_at DESC LIMIT 1""",
            (position_id,)
        )
        row = cursor.fetchone()
        if row:
            return _row_to_pnl_snapshot(row)
        return None


def get_pnl_snapshots_for_position(
    position_id: int,
    target_date: Optional[date] = None
) -> List[PnLSnapshot]:
    """
    Get all P&L snapshots for a position, optionally filtered by date.

    Args:
        position_id: Position ID
        target_date: Optional date to filter (defaults to all)

    Returns:
        List of PnLSnapshot objects ordered by timestamp
    """
    with get_db_session() as conn:
        if target_date:
            start_str = f"{target_date} 00:00:00"
            end_str = f"{target_date} 23:59:59"
            cursor = conn.execute(
                """SELECT * FROM pnl_snapshots
                   WHERE position_id = ?
                   AND created_at BETWEEN ? AND ?
                   ORDER BY created_at ASC""",
                (position_id, start_str, end_str)
            )
        else:
            cursor = conn.execute(
                """SELECT * FROM pnl_snapshots
                   WHERE position_id = ?
                   ORDER BY created_at ASC""",
                (position_id,)
            )
        return [_row_to_pnl_snapshot(row) for row in cursor.fetchall()]


# =============================================================================
# COOLDOWN OPERATIONS
# =============================================================================

def check_cooldown_active() -> bool:
    """
    Check if cooldown is active (cannot enter new position).

    Returns:
        True if cooldown active, False if clear to trade
    """
    today = date.today()
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM cooldowns WHERE cooldown_end > ? LIMIT 1",
            (today.isoformat(),)
        )
        row = cursor.fetchone()

        if row:
            logger.info(f"Cooldown active until {row['cooldown_end']}")
            return True
        return False


def is_on_cooldown(cooldown_type: str) -> bool:
    """
    Check if a specific cooldown type is active.

    Args:
        cooldown_type: Type of cooldown ('entry', etc.)

    Returns:
        True if cooldown active
    """
    # Use datetime for precise comparison (cooldown_end stores datetime)
    now = datetime.now()
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM cooldowns WHERE cooldown_type = ? AND cooldown_end > ? LIMIT 1",
            (cooldown_type, now.isoformat())
        )
        row = cursor.fetchone()

        if row:
            logger.info(f"Cooldown '{cooldown_type}' active until {row['cooldown_end']}")
            return True
        return False


def get_cooldown_remaining(cooldown_type: str) -> Optional[int]:
    """
    Get remaining cooldown time in seconds.

    Args:
        cooldown_type: Type of cooldown ('entry', 'user_skip', etc.)

    Returns:
        Remaining seconds, or None if no active cooldown
    """
    now = datetime.now()
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT cooldown_end FROM cooldowns WHERE cooldown_type = ? AND cooldown_end > ? LIMIT 1",
            (cooldown_type, now.isoformat())
        )
        row = cursor.fetchone()

        if row:
            cooldown_end = datetime.fromisoformat(row['cooldown_end'])
            remaining = (cooldown_end - now).total_seconds()
            return max(0, int(remaining))
        return None


def set_cooldown(cooldown_type: str, duration_seconds: int, position_id: Optional[int] = None) -> None:
    """
    Set cooldown after position exit.

    Args:
        cooldown_type: Type of cooldown ('entry', etc.)
        duration_seconds: Duration in seconds
        position_id: Optional position ID that triggered the cooldown
    """
    with get_db_session() as conn:
        # Use datetime for precise cooldown end time (not just date)
        cooldown_end = datetime.now() + timedelta(seconds=duration_seconds)

        conn.execute(
            """INSERT INTO cooldowns (position_id, cooldown_type, exit_date, cooldown_end)
               VALUES (?, ?, ?, ?)""",
            (position_id, cooldown_type, date.today().isoformat(), cooldown_end.isoformat())
        )
        logger.info(f"Cooldown '{cooldown_type}' set until {cooldown_end.strftime('%Y-%m-%d %H:%M:%S')}")


def clear_cooldown(cooldown_type: Optional[str] = None) -> int:
    """
    Clear active cooldowns.

    Args:
        cooldown_type: Specific cooldown type to clear, or None for all

    Returns:
        Number of cooldowns cleared
    """
    with get_db_session() as conn:
        # Use datetime for precise comparison (cooldown_end stores datetime)
        now = datetime.now()
        if cooldown_type:
            cursor = conn.execute(
                "DELETE FROM cooldowns WHERE cooldown_type = ? AND cooldown_end > ?",
                (cooldown_type, now.isoformat())
            )
        else:
            cursor = conn.execute(
                "DELETE FROM cooldowns WHERE cooldown_end > ?",
                (now.isoformat(),)
            )
        count = cursor.rowcount
        logger.info(f"Cleared {count} cooldown(s)" + (f" of type '{cooldown_type}'" if cooldown_type else ""))
        return count


# =============================================================================
# PENDING DECISION TRACKING
# =============================================================================

def set_pending_decision(decision_type: str, position_id: int) -> None:
    """
    Mark that a decision alert was sent and awaiting user response.

    Uses cooldowns table with 'pending_' prefix and 7-day expiry.
    Alert will not be resent until user responds or position closes.

    Args:
        decision_type: Type of decision ('stop_loss', 'wing_approach', 'vix_warning', 'friday')
        position_id: Position ID this decision is for
    """
    cooldown_type = f"pending_{decision_type}"
    # Set 7-day expiry (effectively "until user responds")
    duration = 7 * 24 * 3600  # 7 days in seconds

    with get_db_session() as conn:
        cooldown_end = datetime.now() + timedelta(seconds=duration)

        # Delete existing pending decision for this type/position first
        conn.execute(
            "DELETE FROM cooldowns WHERE cooldown_type = ? AND position_id = ?",
            (cooldown_type, position_id)
        )
        # Insert new pending decision
        conn.execute(
            """INSERT INTO cooldowns (position_id, cooldown_type, exit_date, cooldown_end)
               VALUES (?, ?, ?, ?)""",
            (position_id, cooldown_type, date.today().isoformat(), cooldown_end.isoformat())
        )
        logger.info(f"Pending decision '{decision_type}' set for position {position_id}")


def has_pending_decision(decision_type: str, position_id: int) -> bool:
    """
    Check if a decision alert is pending user response.

    Args:
        decision_type: Type of decision ('stop_loss', 'wing_approach', 'vix_warning', 'friday')
        position_id: Position ID to check

    Returns:
        True if alert was sent and awaiting response
    """
    cooldown_type = f"pending_{decision_type}"
    now = datetime.now()

    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT * FROM cooldowns
               WHERE cooldown_type = ? AND position_id = ? AND cooldown_end > ?
               LIMIT 1""",
            (cooldown_type, position_id, now.isoformat())
        )
        row = cursor.fetchone()

        if row:
            logger.debug(f"Pending decision '{decision_type}' exists for position {position_id}")
            return True
        return False


def clear_pending_decision(decision_type: str, position_id: Optional[int] = None) -> int:
    """
    Clear pending decision when user responds.

    Args:
        decision_type: Type of decision ('stop_loss', 'wing_approach', 'vix_warning', 'friday')
        position_id: Optional position ID (clears all if None)

    Returns:
        Number of records cleared
    """
    cooldown_type = f"pending_{decision_type}"

    with get_db_session() as conn:
        if position_id is not None:
            cursor = conn.execute(
                "DELETE FROM cooldowns WHERE cooldown_type = ? AND position_id = ?",
                (cooldown_type, position_id)
            )
        else:
            cursor = conn.execute(
                "DELETE FROM cooldowns WHERE cooldown_type = ?",
                (cooldown_type,)
            )
        count = cursor.rowcount
        if count > 0:
            logger.info(f"Cleared pending decision '{decision_type}'" +
                       (f" for position {position_id}" if position_id else ""))
        return count


def clear_all_pending_decisions(position_id: int) -> int:
    """
    Clear all pending decisions for a position (e.g., when position closes).

    Args:
        position_id: Position ID

    Returns:
        Number of records cleared
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "DELETE FROM cooldowns WHERE cooldown_type LIKE 'pending_%' AND position_id = ?",
            (position_id,)
        )
        count = cursor.rowcount
        if count > 0:
            logger.info(f"Cleared {count} pending decision(s) for position {position_id}")
        return count


def get_next_trading_day(from_date: Optional[date] = None) -> date:
    """
    Get next trading day (skip weekends).

    Args:
        from_date: Starting date (default: today)

    Returns:
        Next trading day

    Note:
        Does not account for holidays - use with holiday calendar for accuracy.
    """
    from_date = from_date or date.today()
    next_day = from_date + timedelta(days=1)

    # Skip weekends
    while next_day.weekday() >= 5:  # 5=Sat, 6=Sun
        next_day += timedelta(days=1)

    return next_day


# =============================================================================
# MARKET DATA OPERATIONS
# =============================================================================

def store_market_data(
    trading_date: date,
    atr_14: float,
    previous_close: float
) -> None:
    """
    Store daily market data.

    Args:
        trading_date: Trading date
        atr_14: 14-day ATR
        previous_close: Previous day close
    """
    with get_db_session() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO market_data
               (date, atr_14, previous_close, updated_at)
               VALUES (?, ?, ?, ?)""",
            (trading_date.isoformat(), atr_14, previous_close, datetime.now().isoformat())
        )
        logger.info(f"Stored market data for {trading_date}")


def get_today_market_data() -> Optional[MarketData]:
    """
    Get today's market data.

    Returns:
        MarketData object or None
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            "SELECT * FROM market_data WHERE date = ?",
            (date.today().isoformat(),)
        )
        row = cursor.fetchone()
        if row:
            return _row_to_market_data(row)
        return None


def get_week_market_data() -> List[MarketData]:
    """
    Get this week's market data (Monday to today).

    Returns:
        List of MarketData objects for the week
    """
    from datetime import timedelta

    today = date.today()
    # Find Monday of this week
    monday = today - timedelta(days=today.weekday())

    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT * FROM market_data
               WHERE date >= ? AND date <= ?
               ORDER BY date ASC""",
            (monday.isoformat(), today.isoformat())
        )
        rows = cursor.fetchall()
        return [_row_to_market_data(row) for row in rows]


def capture_day_open(nifty_price: float) -> None:
    """
    Capture day open at first monitor run (9:16 AM).

    Args:
        nifty_price: Opening NIFTY price
    """
    with get_db_session() as conn:
        conn.execute(
            """UPDATE market_data
               SET day_open = ?, day_open_captured_at = ?,
                   day_high = ?, day_low = ?, updated_at = ?
               WHERE date = ?""",
            (
                nifty_price, datetime.now().isoformat(),
                nifty_price, nifty_price, datetime.now().isoformat(),
                date.today().isoformat()
            )
        )
        logger.info(f"Day open captured: {nifty_price}")


def update_day_high_low(current_price: float) -> None:
    """
    Update day high/low during monitoring.

    Args:
        current_price: Current NIFTY price
    """
    market_data = get_today_market_data()
    if not market_data:
        return

    day_high = max(market_data.day_high or 0, current_price)
    day_low = min(market_data.day_low or float('inf'), current_price)

    with get_db_session() as conn:
        conn.execute(
            """UPDATE market_data
               SET day_high = ?, day_low = ?, updated_at = ?
               WHERE date = ?""",
            (day_high, day_low, datetime.now().isoformat(), date.today().isoformat())
        )


# =============================================================================
# SYSTEM STATUS OPERATIONS
# =============================================================================

def get_current_system_status() -> SystemStatus:
    """
    Get current system status.

    Returns:
        SystemStatus object
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT * FROM system_status
               WHERE cleared_at IS NULL
               ORDER BY set_at DESC LIMIT 1"""
        )
        row = cursor.fetchone()
        if row:
            return _row_to_system_status(row)

        # Default to normal if no status found
        return SystemStatus(
            id=0,
            status='normal',
            reason='Default status',
            set_at=datetime.now(),
            set_by='system'
        )


def set_system_status(status: str, reason: str, set_by: str = 'system') -> None:
    """
    Update system status.

    Args:
        status: New status (normal, monitoring_paused, maintenance, error)
        reason: Reason for status change
        set_by: Who set the status (system or user)
    """
    with get_db_session() as conn:
        # Clear any existing active status
        conn.execute(
            "UPDATE system_status SET cleared_at = ?, cleared_by = ? WHERE cleared_at IS NULL",
            (datetime.now().isoformat(), set_by)
        )

        # Insert new status
        conn.execute(
            """INSERT INTO system_status (status, reason, set_by)
               VALUES (?, ?, ?)""",
            (status, reason, set_by)
        )
        logger.info(f"System status changed to: {status} - {reason}")


def check_system_ready(allowed_statuses: Optional[List[str]] = None) -> Tuple[bool, str]:
    """
    Check if system is ready for operations.

    TDD Section 4.3: System Status Checks

    Should be called at the start of each workflow to ensure
    the system is not in error/maintenance state.

    Args:
        allowed_statuses: List of statuses considered "ready".
                         Defaults to ['normal', 'monitoring_paused']

    Returns:
        Tuple of (is_ready, message)

    Example:
        >>> is_ready, msg = check_system_ready()
        >>> if not is_ready:
        ...     logger.warning(f"System not ready: {msg}")
        ...     return
    """
    if allowed_statuses is None:
        allowed_statuses = ['normal', 'monitoring_paused']

    status = get_current_system_status()

    if status.status in allowed_statuses:
        return True, f"System status: {status.status}"

    # System is in a blocked state
    return False, f"System blocked: {status.status} - {status.reason or 'No reason given'}"


def require_system_ready(allowed_statuses: Optional[List[str]] = None) -> None:
    """
    Decorator-friendly function that raises if system not ready.

    Args:
        allowed_statuses: List of allowed statuses

    Raises:
        RuntimeError: If system is not ready
    """
    is_ready, message = check_system_ready(allowed_statuses)
    if not is_ready:
        raise RuntimeError(f"System not ready for operation: {message}")


# =============================================================================
# ENTRY ATTEMPTS OPERATIONS
# =============================================================================

def log_entry_attempt(
    result: str,
    block_reason: Optional[str] = None,
    checklist: Optional[Dict] = None
) -> int:
    """
    Log an entry attempt.

    Args:
        result: 'success', 'blocked', or 'failed'
        block_reason: Reason if blocked
        checklist: Full checklist result as dict

    Returns:
        Entry attempt ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO entry_attempts (attempt_time, result, block_reason, checklist_json)
               VALUES (?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                result,
                block_reason,
                json.dumps(checklist) if checklist else None
            )
        )
        entry_id = cursor.lastrowid
        logger.info(f"Entry attempt logged: {result} (ID: {entry_id})")
        return entry_id


# =============================================================================
# ALERT OPERATIONS
# =============================================================================

def create_alert(
    alert_type: str,
    message: str,
    priority: str,
    position_id: Optional[int] = None,
    requires_response: bool = False,
    timeout_action: Optional[str] = None
) -> int:
    """
    Create an alert in the queue.

    Args:
        alert_type: Type of alert
        message: Alert message
        priority: Priority level (low, medium, high, critical)
        position_id: Optional position ID
        requires_response: Whether user response is needed
        timeout_action: Action if no response

    Returns:
        Alert ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO alert_queue (
                position_id, alert_type, message, priority,
                requires_response, timeout_action
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (position_id, alert_type, message, priority, requires_response, timeout_action)
        )
        return cursor.lastrowid


def mark_alert_sent(alert_id: int) -> None:
    """
    Mark an alert as sent.

    Args:
        alert_id: Alert ID
    """
    with get_db_session() as conn:
        conn.execute(
            "UPDATE alert_queue SET status = 'sent', sent_at = ? WHERE id = ?",
            (datetime.now().isoformat(), alert_id)
        )


def get_pending_response_alert() -> Optional[Dict]:
    """
    Get the most recent alert awaiting response.

    Returns:
        Alert dict or None
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT * FROM alert_queue
               WHERE requires_response = 1
               AND status = 'sent'
               ORDER BY created_at DESC LIMIT 1"""
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        return None


def queue_response(alert_id: int, user_response: str) -> int:
    """
    Queue a user response.

    Args:
        alert_id: Alert ID being responded to
        user_response: User's response text

    Returns:
        Response ID
    """
    with get_db_session() as conn:
        cursor = conn.execute(
            """INSERT INTO response_queue (alert_id, user_response, status)
               VALUES (?, ?, 'received')""",
            (alert_id, user_response)
        )
        # Update alert status
        conn.execute(
            "UPDATE alert_queue SET status = 'responded' WHERE id = ?",
            (alert_id,)
        )
        return cursor.lastrowid


def was_friday_decision_sent_today(position_id: int) -> bool:
    """
    Check if a Friday decision was already recorded today for this position.

    Args:
        position_id: Position ID to check

    Returns:
        True if friday_decision already exists for today
    """
    today = date.today().isoformat()
    with get_db_session() as conn:
        cursor = conn.execute(
            """SELECT COUNT(*) FROM claude_decisions
               WHERE position_id = ?
               AND trigger_type = 'friday_decision'
               AND date(created_at) = ?""",
            (position_id, today)
        )
        count = cursor.fetchone()[0]
        return count > 0


# =============================================================================
# DATA CLEANUP OPERATIONS
# =============================================================================

def cleanup_old_data(
    entry_attempts_days: int = 90,
    alert_queue_days: int = 30
) -> Dict[str, int]:
    """
    Clean up old data per retention policy.

    Args:
        entry_attempts_days: Days to keep entry attempts (default 90)
        alert_queue_days: Days to keep alerts/responses (default 30)

    Returns:
        Dict with counts of deleted records
    """
    deleted = {}
    today = date.today()

    with get_db_session() as conn:
        # Clean entry_attempts older than 90 days
        entry_cutoff = (today - timedelta(days=entry_attempts_days)).isoformat()
        cursor = conn.execute(
            "DELETE FROM entry_attempts WHERE date(attempt_time) < ?",
            (entry_cutoff,)
        )
        deleted['entry_attempts'] = cursor.rowcount

        # Clean alert_queue older than 30 days
        alert_cutoff = (today - timedelta(days=alert_queue_days)).isoformat()

        # First get alert IDs to delete
        cursor = conn.execute(
            "SELECT id FROM alert_queue WHERE date(created_at) < ?",
            (alert_cutoff,)
        )
        old_alert_ids = [row['id'] for row in cursor.fetchall()]

        if old_alert_ids:
            # Delete responses first
            placeholders = ','.join(['?' for _ in old_alert_ids])
            cursor = conn.execute(
                f"DELETE FROM response_queue WHERE alert_id IN ({placeholders})",
                old_alert_ids
            )
            deleted['response_queue'] = cursor.rowcount

            # Then delete alerts
            cursor = conn.execute(
                f"DELETE FROM alert_queue WHERE id IN ({placeholders})",
                old_alert_ids
            )
            deleted['alert_queue'] = cursor.rowcount
        else:
            deleted['response_queue'] = 0
            deleted['alert_queue'] = 0

        logger.info(f"Data cleanup complete: {deleted}")
        return deleted


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _row_to_position(row: sqlite3.Row) -> Position:
    """Convert database row to Position object."""
    # Handle trailing fields that may not exist in older databases
    row_keys = row.keys()

    # Parse peak_pnl_time if it exists and has value
    peak_pnl_time = None
    if 'peak_pnl_time' in row_keys and row['peak_pnl_time']:
        try:
            peak_pnl_time = datetime.fromisoformat(row['peak_pnl_time'])
        except (ValueError, TypeError):
            peak_pnl_time = None

    return Position(
        id=row['id'],
        status=row['status'],
        entry_time=datetime.fromisoformat(row['entry_time']),
        exit_time=datetime.fromisoformat(row['exit_time']) if row['exit_time'] else None,
        expiry_date=date.fromisoformat(row['expiry_date']),
        atm_strike=row['atm_strike'],
        wing_distance=row['wing_distance'],
        lot_size=row['lot_size'],
        entry_premium=row['entry_premium'],
        straddle_credit=row['straddle_credit'],
        wing_debit=row['wing_debit'],
        max_profit=row['max_profit'],
        max_loss=row['max_loss'],
        margin_deployed=row['margin_deployed'],
        entry_charges=row['entry_charges'],
        exit_premium=row['exit_premium'],
        exit_charges=row['exit_charges'],
        net_pnl=row['net_pnl'],
        pnl_percent=row['pnl_percent'],
        exit_reason=row['exit_reason'],
        verified=bool(row['verified']),
        # Trailing profit fields (with safe defaults for older DBs)
        peak_pnl_pct=row['peak_pnl_pct'] if 'peak_pnl_pct' in row_keys else None,
        peak_pnl_amount=row['peak_pnl_amount'] if 'peak_pnl_amount' in row_keys else None,
        peak_pnl_time=peak_pnl_time,
        trailing_active=bool(row['trailing_active']) if 'trailing_active' in row_keys and row['trailing_active'] else False,
        trailing_floor_pct=row['trailing_floor_pct'] if 'trailing_floor_pct' in row_keys else None,
        breakeven_locked=bool(row['breakeven_locked']) if 'breakeven_locked' in row_keys and row['breakeven_locked'] else False,
        exit_in_progress=bool(row['exit_in_progress']) if 'exit_in_progress' in row_keys and row['exit_in_progress'] else False,
    )


def _row_to_leg(row: sqlite3.Row) -> PositionLeg:
    """Convert database row to PositionLeg object."""
    return PositionLeg(
        id=row['id'],
        position_id=row['position_id'],
        leg_type=row['leg_type'],
        option_type=row['option_type'],
        strike=row['strike'],
        tradingsymbol=row['tradingsymbol'],
        entry_price=row['entry_price'],
        exit_price=row['exit_price'],
        quantity=row['quantity'],
        instrument_token=row['instrument_token']
    )


def _row_to_pnl_snapshot(row: sqlite3.Row) -> PnLSnapshot:
    """Convert database row to PnLSnapshot object."""
    return PnLSnapshot(
        id=row['id'],
        position_id=row['position_id'],
        current_pnl=row['current_pnl'],
        pnl_percent=row['pnl_percent'],
        nifty_spot=row['nifty_spot'],
        vix=row['vix'],
        ce_bid=row['ce_bid'],
        ce_ask=row['ce_ask'],
        pe_bid=row['pe_bid'],
        pe_ask=row['pe_ask'],
        wing_ce_bid=row['wing_ce_bid'],
        wing_ce_ask=row['wing_ce_ask'],
        wing_pe_bid=row['wing_pe_bid'],
        wing_pe_ask=row['wing_pe_ask'],
        timestamp=datetime.fromisoformat(row['created_at']) if row['created_at'] else None
    )


def _row_to_market_data(row: sqlite3.Row) -> MarketData:
    """Convert database row to MarketData object."""
    return MarketData(
        id=row['id'],
        date=date.fromisoformat(row['date']),
        atr_14=row['atr_14'],
        previous_close=row['previous_close'],
        day_open=row['day_open'],
        day_open_captured_at=datetime.fromisoformat(row['day_open_captured_at']) if row['day_open_captured_at'] else None,
        day_high=row['day_high'],
        day_low=row['day_low']
    )


def _row_to_system_status(row: sqlite3.Row) -> SystemStatus:
    """Convert database row to SystemStatus object."""
    return SystemStatus(
        id=row['id'],
        status=row['status'],
        reason=row['reason'],
        set_at=datetime.fromisoformat(row['set_at']),
        set_by=row['set_by'],
        cleared_at=datetime.fromisoformat(row['cleared_at']) if row['cleared_at'] else None,
        cleared_by=row['cleared_by']
    )


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'init':
        init_database()
        print(f"Database initialized at {DATABASE_PATH}")
    elif len(sys.argv) > 1 and sys.argv[1] == 'cleanup':
        result = cleanup_old_data()
        print(f"Cleanup complete: {result}")
    else:
        print("Usage: python -m src.utils.db [init|cleanup]")
