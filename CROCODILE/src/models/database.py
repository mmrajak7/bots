"""Database Models for Crocodile Trading Bot"""

from datetime import datetime, date
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Date, Boolean, JSON, Text, Enum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum
from loguru import logger

from src.utils.config_manager import config
from src.utils.timezone_helper import ist_now_naive

Base = declarative_base()


# Enums
class OrderStatus(enum.Enum):
    """Order status types"""
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PositionStatus(enum.Enum):
    """Position status types"""
    OPEN = "OPEN"
    CLOSED_SL = "CLOSED_SL"  # Stop loss hit
    CLOSED_MANUAL = "CLOSED_MANUAL"  # Manual closure
    CLOSED_TARGET = "CLOSED_TARGET"  # Target hit (if applicable)


class TransactionType(enum.Enum):
    """Transaction types"""
    ENTRY = "ENTRY"
    EXIT_SL = "EXIT_SL"
    EXIT_MANUAL = "EXIT_MANUAL"
    EXIT_TARGET = "EXIT_TARGET"


# Models

class ProcessedSignal(Base):
    """Track processed signals to avoid duplicates"""
    __tablename__ = 'processed_signals'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    date = Column(Date, nullable=False, index=True)
    script = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)  # D, W, M
    processed_at = Column(DateTime, default=ist_now_naive, nullable=False)  # IST timezone
    signal_valid = Column(Boolean, default=True)  # Was signal validated?
    entry_attempted = Column(Boolean, default=False)  # Did we try to enter?
    entry_successful = Column(Boolean, default=False)  # Did entry succeed?
    rejection_reason = Column(String(255), nullable=True)  # Why rejected (if applicable)

    # Idempotency fields (added for crash recovery)
    processing_status = Column(String(20), nullable=False, default='PENDING', index=True)
    # Values: PENDING, PROCESSING, SUCCESS, FAILED, SKIPPED
    started_at = Column(DateTime, nullable=True)  # When processing started
    completed_at = Column(DateTime, nullable=True)  # When processing completed
    order_id = Column(String(50), nullable=True)  # Zerodha order ID (if placed)
    failure_count = Column(Integer, default=0)  # Number of failures
    last_error = Column(Text, nullable=True)  # Last error message

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class OpenOrder(Base):
    """Track open orders (pending fills)"""
    __tablename__ = 'open_orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    script = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    order_id = Column(String(50), nullable=False, unique=True, index=True)  # Zerodha order ID
    order_type = Column(String(20), nullable=False)  # LIMIT, MARKET
    order_price = Column(Float, nullable=True)  # For LIMIT orders
    quantity = Column(Integer, nullable=False)  # Original order quantity
    filled_qty = Column(Integer, default=0, nullable=False)  # Quantity filled so far (for partial fills)
    capital_deployed = Column(Float, nullable=False)  # Reserved capital for this order
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False)
    placed_at = Column(DateTime, default=ist_now_naive, nullable=False)
    filled_at = Column(DateTime, nullable=True)  # When first fill occurred
    fill_price = Column(Float, nullable=True)  # Average fill price
    position_created = Column(Boolean, default=False, nullable=False)  # Track if position already created
    rejection_reason = Column(String(255), nullable=True)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class OpenPosition(Base):
    """Track open positions"""
    __tablename__ = 'open_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    script = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, index=True)  # D, W, M
    entry_date = Column(Date, nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    capital_deployed = Column(Float, nullable=False)  # Entry price * quantity

    # Stop Loss Tracking
    current_sl = Column(Float, nullable=False)  # Current stop loss price
    initial_sl = Column(Float, nullable=False)  # Entry day LOW (never changes)
    highest_sl = Column(Float, nullable=True)  # Highest SL ever achieved
    entry_atr = Column(Float, nullable=True)  # Weekly ATR at entry (for ATR-based SL strategy)
    sl_movements = Column(Integer, default=0)  # Count of SL updates
    last_sl_update = Column(DateTime, nullable=True)

    # GTT Tracking
    current_gtt_id = Column(String(50), nullable=True)  # Zerodha GTT ID
    gtt_placed_at = Column(DateTime, nullable=True)

    # Position Status
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN, nullable=False, index=True)
    days_held = Column(Integer, default=0)  # Updated daily

    # Exit Information (filled when closed)
    exit_date = Column(Date, nullable=True)
    exit_price = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)  # SL_HIT, MANUAL, etc.

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive, nullable=False)
    updated_at = Column(DateTime, default=ist_now_naive, onupdate=ist_now_naive, nullable=False)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class ClosedPosition(Base):
    """Historical closed positions with P&L"""
    __tablename__ = 'closed_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    script = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False, index=True)

    # Entry Details
    entry_date = Column(Date, nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    capital_deployed = Column(Float, nullable=False)

    # Exit Details
    exit_date = Column(Date, nullable=False, index=True)
    exit_price = Column(Float, nullable=False)
    exit_reason = Column(String(50), nullable=False)  # SL_HIT, MANUAL, etc.

    # P&L Calculation
    gross_pnl = Column(Float, nullable=False)  # Before costs
    transaction_costs = Column(Float, nullable=False)  # Total costs
    cost_breakdown = Column(JSON, nullable=True)  # Detailed cost breakdown
    net_pnl = Column(Float, nullable=False)  # After costs
    pnl_percent = Column(Float, nullable=False)  # Net P&L %

    # Trade Metrics
    days_held = Column(Integer, nullable=False)
    sl_movements = Column(Integer, default=0)  # How many times SL trailed
    highest_sl_achieved = Column(Float, nullable=True)
    entry_atr = Column(Float, nullable=True)  # Weekly ATR at entry (for ATR-based SL strategy)

    # Timestamps
    closed_at = Column(DateTime, default=ist_now_naive, nullable=False)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class CapitalLedger(Base):
    """Daily capital tracking"""
    __tablename__ = 'capital_ledger'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    date = Column(Date, nullable=False, index=True)  # Removed unique=True for multi-bot support

    # Capital Metrics
    opening_capital = Column(Float, nullable=False)  # From Zerodha at 9 AM
    deployed_capital = Column(Float, default=0.0)  # Sum of open positions
    free_capital = Column(Float, default=0.0)  # opening - deployed

    # Capital Allocation (NEW - for multi-bot architecture)
    allocated_capital = Column(Float, nullable=True)  # Allocated capital for this bot (NULL = unlimited)
    allocation_method = Column(String(20), nullable=True)  # 'amount' or 'percent'
    total_account_margin = Column(Float, nullable=True)  # Total account margin (for reference)
    reserve_buffer = Column(Float, default=0.0)  # Reserve buffer amount

    # P&L Tracking
    realized_pnl_today = Column(Float, default=0.0)  # Closed trades P&L
    unrealized_pnl = Column(Float, default=0.0)  # Open positions M2M
    total_capital = Column(Float, default=0.0)  # opening + realized + unrealized

    # Trade Counts
    num_open_positions = Column(Integer, default=0)
    num_trades_today = Column(Integer, default=0)  # Entries + exits
    num_entries_today = Column(Integer, default=0)
    num_exits_today = Column(Integer, default=0)

    # Monthly Tracking (for drawdown)
    starting_capital_month = Column(Float, nullable=True)  # Reset on 1st of month
    monthly_drawdown_pct = Column(Float, default=0.0)

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive)
    updated_at = Column(DateTime, default=ist_now_naive, onupdate=ist_now_naive)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class GTTUpdateLog(Base):
    """Log all GTT updates for audit trail"""
    __tablename__ = 'gtt_update_log'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    position_id = Column(Integer, nullable=False, index=True)  # Reference to OpenPosition
    script = Column(String(50), nullable=False)
    timeframe = Column(String(10), nullable=False)
    update_date = Column(Date, nullable=False, index=True)

    # GTT Details
    old_sl = Column(Float, nullable=True)  # Previous SL (null for first GTT)
    new_sl = Column(Float, nullable=False)  # New SL
    old_gtt_id = Column(String(50), nullable=True)  # Previous GTT ID
    new_gtt_id = Column(String(50), nullable=True)  # New GTT ID

    # Status
    status = Column(String(20), nullable=False)  # SUCCESS, FAILED
    error_message = Column(Text, nullable=True)  # If failed
    retry_count = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


class TransactionHistory(Base):
    """Complete transaction history for reporting"""
    __tablename__ = 'transaction_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    bot_instance_id = Column(String(50), nullable=False, default='crocodile_main', index=True)  # Bot identifier
    transaction_type = Column(Enum(TransactionType), nullable=False, index=True)
    script = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(10), nullable=False)
    transaction_date = Column(Date, nullable=False, index=True)

    # Transaction Details
    price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    value = Column(Float, nullable=False)  # price * quantity

    # References
    order_id = Column(String(50), nullable=True)  # Zerodha order ID
    gtt_id = Column(String(50), nullable=True)  # GTT ID if applicable
    position_id = Column(Integer, nullable=True)  # Link to position

    # Additional Info
    notes = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4'},
    )


# Database Engine and Session
def get_engine():
    """Get database engine from config"""
    db_url = config.get('database.url', 'sqlite:///data/trading.db')
    echo = config.get('database.echo', False)
    return create_engine(db_url, echo=echo)


def get_session():
    """Get database session"""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


def _migrate_database(engine):
    """Run database migrations for schema changes.

    Adds new columns to existing tables if they don't exist.
    Safe to call multiple times - checks column existence before ALTER.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    migrations = [
        ('open_positions', 'entry_atr', 'FLOAT'),
        ('closed_positions', 'entry_atr', 'FLOAT'),
    ]

    with engine.connect() as conn:
        for table_name, column_name, column_type in migrations:
            if table_name not in inspector.get_table_names():
                continue
            existing_columns = [col['name'] for col in inspector.get_columns(table_name)]
            if column_name not in existing_columns:
                logger.info(f"Migration: Adding column {column_name} to {table_name}")
                conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                ))
                conn.commit()


def init_database():
    """Initialize database tables"""
    engine = get_engine()
    _migrate_database(engine)
    Base.metadata.create_all(engine)
    return engine
