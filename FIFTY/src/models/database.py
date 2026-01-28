"""Database Models for FIFTY Trading Bot

FIFTY-specific schema for monthly CSP signal trading with Telegram approval workflow.
"""

from datetime import datetime, date
from typing import Optional
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Date, Boolean, Text, Enum, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import enum

from src.utils.config_manager import config
from src.utils.timezone_helper import ist_now_naive

Base = declarative_base()


# Enums for Signal Status
class SignalStatus(enum.Enum):
    """Signal lifecycle status"""
    PENDING = "pending"           # New signal, not yet notified
    NOTIFIED = "notified"         # Telegram notification sent, awaiting response
    APPROVED = "approved"         # User approved, ready for order
    REJECTED = "rejected"         # User rejected
    HOLD = "hold"                 # User wants to decide later
    AWAITING_PRICE = "awaiting_price"  # User clicked revise, waiting for price input
    ENTERED = "entered"           # Entry GTT placed
    FILLED = "filled"             # Entry filled, position created
    INVALIDATED = "invalidated"   # Monthly close below SuperTrend
    EXPIRED = "expired"           # Month ended without fill


class OrderStatus(enum.Enum):
    """Order status types"""
    PLACING = "PLACING"      # FIX ARCH-C1: Idempotency - record created before API call
    PENDING = "PENDING"      # GTT successfully placed, waiting for trigger
    TRIGGERED = "TRIGGERED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    INVALIDATED = "INVALIDATED"


class PositionStatus(enum.Enum):
    """Position status types"""
    OPEN = "OPEN"
    CLOSED_SL = "CLOSED_SL"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CLOSED_EMERGENCY = "CLOSED_EMERGENCY"


# Models

class SignalQueue(Base):
    """
    Track all CSP signals from detection through lifecycle.
    One signal per script per month.
    """
    __tablename__ = 'signal_queue'

    id = Column(Integer, primary_key=True, autoincrement=True)
    script = Column(String(50), nullable=False, index=True)
    signal_date = Column(Date, nullable=False, index=True)
    signal_level = Column(Float, nullable=False)  # Monthly SuperTrend value at detection
    status = Column(Enum(SignalStatus), default=SignalStatus.PENDING, nullable=False, index=True)

    # Timing
    first_seen_at = Column(DateTime, default=ist_now_naive, nullable=False)
    last_notified_at = Column(DateTime, nullable=True)

    # User interaction
    user_price = Column(Float, nullable=True)  # User-revised entry price
    telegram_msg_id = Column(Integer, nullable=True)  # For button handling
    notes = Column(Text, nullable=True)

    # FIX TR-004: Store quantity at notification time for consistent position sizing
    calculated_quantity = Column(Integer, nullable=True)  # Quantity calculated at notification

    # Processing metadata
    nifty_filter_passed = Column(Boolean, nullable=True)  # NIFTY weekly filter result
    rejection_reason = Column(String(255), nullable=True)

    # Monthly uniqueness (one signal per script per month)
    signal_month = Column(String(7), nullable=False, index=True)  # 'YYYY-MM' format

    __table_args__ = (
        Index('idx_script_month', 'script', 'signal_month', unique=True),
    )


class OpenOrder(Base):
    """
    Track pending GTT entry orders waiting for fill.
    """
    __tablename__ = 'open_orders'

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, nullable=False, index=True)  # Reference to signal_queue
    gtt_id = Column(String(50), nullable=True, index=True)  # Zerodha GTT ID
    script = Column(String(50), nullable=False, index=True)
    exchange = Column(String(10), default='NSE', nullable=False)

    # Order details
    trigger_price = Column(Float, nullable=False)  # GTT trigger price
    limit_price = Column(Float, nullable=False)    # Order price
    quantity = Column(Integer, nullable=False)
    capital_deployed = Column(Float, nullable=False)

    # Status tracking
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False, index=True)
    placed_at = Column(DateTime, default=ist_now_naive, nullable=False)
    triggered_at = Column(DateTime, nullable=True)
    filled_at = Column(DateTime, nullable=True)
    fill_price = Column(Float, nullable=True)

    # Error tracking
    last_error = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Position link (after fill)
    position_created = Column(Boolean, default=False, nullable=False)


class OpenPosition(Base):
    """
    Track active held positions.
    """
    __tablename__ = 'open_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(Integer, nullable=False, index=True)
    script = Column(String(50), nullable=False, index=True)

    # Entry details
    entry_date = Column(Date, nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    capital_deployed = Column(Float, nullable=False)

    # Stop Loss Tracking (20% initial, trail to monthly LOW)
    initial_sl = Column(Float, nullable=False)      # 20% below entry (never changes)
    current_sl = Column(Float, nullable=False)      # Current GTT trigger price
    highest_sl = Column(Float, nullable=True)       # Best SL ever achieved
    sl_movements = Column(Integer, default=0)       # Count of SL updates
    last_sl_update = Column(DateTime, nullable=True)

    # GTT Tracking
    gtt_id = Column(String(50), nullable=True)
    gtt_placed_at = Column(DateTime, nullable=True)
    gtt_verified = Column(Boolean, default=False)

    # Status
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN, nullable=False, index=True)
    days_held = Column(Integer, default=0)

    # 30% drop alert tracking
    drop_alert_sent = Column(Boolean, default=False)
    drop_alert_date = Column(Date, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive, nullable=False)
    updated_at = Column(DateTime, default=ist_now_naive, onupdate=ist_now_naive, nullable=False)


class ClosedPosition(Base):
    """
    Historical closed positions with P&L.
    """
    __tablename__ = 'closed_positions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    script = Column(String(50), nullable=False, index=True)

    # Entry Details
    entry_date = Column(Date, nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    capital_deployed = Column(Float, nullable=False)

    # Exit Details
    exit_date = Column(Date, nullable=False, index=True)
    exit_price = Column(Float, nullable=False)
    exit_reason = Column(String(50), nullable=False)  # SL_HIT, MANUAL, EMERGENCY_EXIT

    # P&L Calculation
    gross_pnl = Column(Float, nullable=False)
    transaction_costs = Column(Float, nullable=False)
    net_pnl = Column(Float, nullable=False)
    pnl_percent = Column(Float, nullable=False)

    # Trade Metrics
    days_held = Column(Integer, nullable=False)
    sl_movements = Column(Integer, default=0)
    highest_sl_achieved = Column(Float, nullable=True)

    # Timestamps
    closed_at = Column(DateTime, default=ist_now_naive, nullable=False)


class CapitalLedger(Base):
    """
    Daily snapshot of capital.
    """
    __tablename__ = 'capital_ledger'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False, unique=True, index=True)

    # Capital Metrics
    opening_capital = Column(Float, nullable=False)
    deployed_capital = Column(Float, default=0.0)
    free_capital = Column(Float, default=0.0)

    # P&L Tracking
    realized_pnl = Column(Float, default=0.0)  # Cumulative realized P&L
    monthly_pnl = Column(Float, default=0.0)   # Current month P&L

    # Position Counts
    num_open_positions = Column(Integer, default=0)
    num_pending_orders = Column(Integer, default=0)

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive)
    updated_at = Column(DateTime, default=ist_now_naive, onupdate=ist_now_naive)


class GTTUpdateLog(Base):
    """
    Audit trail for SL GTT changes.
    """
    __tablename__ = 'gtt_update_log'

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, nullable=False, index=True)
    script = Column(String(50), nullable=False)
    update_date = Column(Date, nullable=False, index=True)

    # GTT Details
    old_sl = Column(Float, nullable=True)
    new_sl = Column(Float, nullable=False)
    old_gtt_id = Column(String(50), nullable=True)
    new_gtt_id = Column(String(50), nullable=True)

    # Status
    status = Column(String(20), nullable=False)  # SUCCESS, FAILED
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, default=0)

    # Reason for update
    update_reason = Column(String(50), nullable=True)  # MONTHLY_TRAIL, MANUAL, INITIAL

    # Timestamps
    created_at = Column(DateTime, default=ist_now_naive)


class BotState(Base):
    """
    Key-value store for bot state.
    """
    __tablename__ = 'bot_state'

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=ist_now_naive, onupdate=ist_now_naive)


class TelegramCallback(Base):
    """
    Track processed Telegram callbacks to avoid duplicate handling.
    """
    __tablename__ = 'telegram_callbacks'

    id = Column(Integer, primary_key=True, autoincrement=True)
    update_id = Column(Integer, unique=True, nullable=False, index=True)
    callback_data = Column(String(255), nullable=True)
    message_text = Column(Text, nullable=True)
    processed_at = Column(DateTime, default=ist_now_naive)
    action_taken = Column(String(100), nullable=True)


# Database Engine and Session Management
# FIX DB-003: Thread-safe session factory initialization
import threading

_engine = None
_SessionLocal = None
_db_lock = threading.Lock()


def get_engine():
    """Get database engine from config (thread-safe)"""
    global _engine
    if _engine is None:
        with _db_lock:
            if _engine is None:
                db_url = config.get('database.url', 'sqlite:///data/trading.db')
                echo = config.get('database.echo', False)
                _engine = create_engine(db_url, echo=echo)
    return _engine


def get_session():
    """
    Get database session (thread-safe).

    FIX DB-003: Each call returns an independent session.
    Caller MUST close the session after use.
    """
    global _SessionLocal
    if _SessionLocal is None:
        with _db_lock:
            if _SessionLocal is None:
                engine = get_engine()
                _SessionLocal = sessionmaker(bind=engine)
    return _SessionLocal()


# FIX SYS-H3: Context manager for session handling
from contextlib import contextmanager


@contextmanager
def session_scope():
    """
    Context manager for database session.

    FIX SYS-H3: Provides automatic commit/rollback/close handling.

    Usage:
        with session_scope() as session:
            # do work
            session.add(obj)
            # commit happens automatically on success
        # session is closed automatically

    Rollback happens automatically on exception.
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database():
    """Initialize database tables"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


# Utility functions for BotState

def get_bot_state(key: str, default: str = None) -> Optional[str]:
    """Get a value from bot_state table"""
    session = get_session()
    try:
        state = session.query(BotState).filter(BotState.key == key).first()
        return state.value if state else default
    finally:
        session.close()


def set_bot_state(key: str, value: str) -> None:
    """Set a value in bot_state table"""
    session = get_session()
    try:
        state = session.query(BotState).filter(BotState.key == key).first()
        if state:
            state.value = value
            state.updated_at = ist_now_naive()
        else:
            state = BotState(key=key, value=value)
            session.add(state)
        session.commit()
    finally:
        session.close()


def is_kill_switch_active() -> bool:
    """Check if kill switch is active"""
    return get_bot_state('kill_switch', 'inactive') == 'active'


def set_kill_switch(active: bool) -> None:
    """Set kill switch state"""
    set_bot_state('kill_switch', 'active' if active else 'inactive')


def get_telegram_offset() -> int:
    """Get last processed Telegram update offset"""
    offset = get_bot_state('telegram_offset', '0')
    return int(offset)


def set_telegram_offset(offset: int) -> None:
    """Set Telegram update offset"""
    set_bot_state('telegram_offset', str(offset))
