-- ============================================================================
-- SNAIL Database Schema
-- ============================================================================
-- @file        schema.sql
-- @description Complete SQLite database schema for SNAIL trading system
-- @author      SNAIL Development Team
-- @created     2025-12-04
-- @version     1.0.0
-- @references  TECHNICAL_DESIGN.md Section 4
-- ============================================================================

-- Database configuration
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA synchronous=NORMAL;

-- ============================================================================
-- CORE TABLES
-- ============================================================================

-- Positions: Main position tracking table
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'active', 'exiting', 'closed')),
    entry_time DATETIME NOT NULL,
    exit_time DATETIME,
    expiry_date DATE NOT NULL,
    atm_strike INTEGER NOT NULL,
    wing_distance INTEGER NOT NULL,
    lot_size INTEGER NOT NULL,

    -- Entry premiums (per unit values)
    entry_premium REAL NOT NULL,
    straddle_credit REAL NOT NULL,
    wing_debit REAL NOT NULL,

    -- Calculated at entry (amounts in INR)
    max_profit REAL NOT NULL,
    max_loss REAL NOT NULL,
    margin_deployed REAL NOT NULL,
    entry_charges REAL NOT NULL,

    -- Exit values
    exit_premium REAL,
    exit_charges REAL,
    net_pnl REAL,
    pnl_percent REAL,

    exit_reason TEXT CHECK(exit_reason IN (
        'profit_target', 'stop_loss', 'friday_exit',
        'vix_spike', 'manual', 'timeout', 'adjustment', 'expiry_day'
    )),

    verified BOOLEAN DEFAULT TRUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Position Legs: Individual option legs of each position
CREATE TABLE IF NOT EXISTS position_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    leg_type TEXT NOT NULL CHECK(leg_type IN ('straddle_ce', 'straddle_pe', 'wing_ce', 'wing_pe')),
    option_type TEXT NOT NULL CHECK(option_type IN ('CE', 'PE')),
    strike INTEGER NOT NULL,
    tradingsymbol TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    quantity INTEGER NOT NULL,
    instrument_token TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Orders: All order records for audit trail
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    kite_order_id TEXT NOT NULL,
    order_type TEXT NOT NULL CHECK(order_type IN ('LIMIT', 'MARKET')),
    transaction_type TEXT NOT NULL CHECK(transaction_type IN ('BUY', 'SELL')),
    tradingsymbol TEXT NOT NULL,
    strike INTEGER NOT NULL,
    option_type TEXT NOT NULL CHECK(option_type IN ('CE', 'PE')),
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'placed', 'filled', 'cancelled', 'rejected')),
    fill_price REAL,
    slippage REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- MONITORING & TRACKING TABLES
-- ============================================================================

-- P&L Snapshots: Point-in-time P&L records
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    current_pnl REAL NOT NULL,
    pnl_percent REAL NOT NULL,
    nifty_spot REAL NOT NULL,
    vix REAL NOT NULL,
    ce_bid REAL NOT NULL,
    ce_ask REAL NOT NULL,
    pe_bid REAL NOT NULL,
    pe_ask REAL NOT NULL,
    wing_ce_bid REAL,
    wing_ce_ask REAL,
    wing_pe_bid REAL,
    wing_pe_ask REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Claude Decisions: AI advisory logs
CREATE TABLE IF NOT EXISTS claude_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    trigger_type TEXT NOT NULL CHECK(trigger_type IN (
        'pre_entry', 'stop_loss_advisory', 'wing_approach',
        'vix_spike', 'eod_decision', 'friday_decision',
        'adjustment', 'market_event', 'user_query',
        'gap_beyond_wing', 'significant_gap'
    )),
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    decision TEXT,
    model_used TEXT NOT NULL CHECK(model_used IN ('haiku', 'sonnet')),
    tokens_used INTEGER,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Market Data: Daily OHLC and ATR data
CREATE TABLE IF NOT EXISTS market_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL UNIQUE,
    atr_14 REAL NOT NULL,
    previous_close REAL NOT NULL,
    day_open REAL,
    day_open_captured_at DATETIME,
    day_high REAL,
    day_low REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- ALERT & RESPONSE TABLES
-- ============================================================================

-- Alert Queue: Outgoing alerts
CREATE TABLE IF NOT EXISTS alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    alert_type TEXT NOT NULL CHECK(alert_type IN (
        'entry', 'exit', 'stop_loss', 'wing_approach',
        'vix_spike', 'vix_warning', 'profit_milestone', 'claude_advisory',
        'adjustment_strategies', 'error', 'daily_summary', 'system',
        'gap_beyond_wing', 'significant_gap'
    )),
    message TEXT NOT NULL,
    priority TEXT NOT NULL CHECK(priority IN ('low', 'medium', 'high', 'critical')),
    requires_response BOOLEAN DEFAULT FALSE,
    timeout_action TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    sent_at DATETIME,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'responded', 'timed_out')) DEFAULT 'pending'
);

-- Response Queue: Incoming user responses
CREATE TABLE IF NOT EXISTS response_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES alert_queue(id),
    user_response TEXT NOT NULL,
    parsed_action TEXT,
    received_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    processed_at DATETIME,
    status TEXT NOT NULL CHECK(status IN ('received', 'parsed', 'executed', 'failed')) DEFAULT 'received'
);

-- Alert Cooldowns: Deduplication tracking
CREATE TABLE IF NOT EXISTS alert_cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions(id),
    alert_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_alert_time DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(alert_type, content_hash)
);

-- ============================================================================
-- OPERATIONAL TABLES
-- ============================================================================

-- Cooldowns: Post-exit cooldown tracking
CREATE TABLE IF NOT EXISTS cooldowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    exit_date DATE NOT NULL,
    cooldown_end DATE NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Entry Attempts: Entry attempt audit log
CREATE TABLE IF NOT EXISTS entry_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_time DATETIME NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('success', 'blocked', 'failed')),
    block_reason TEXT,
    checklist_json TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- System Status: System operational status
CREATE TABLE IF NOT EXISTS system_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK(status IN ('normal', 'monitoring_paused', 'maintenance', 'error')),
    reason TEXT,
    set_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    set_by TEXT NOT NULL CHECK(set_by IN ('system', 'user')),
    cleared_at DATETIME,
    cleared_by TEXT CHECK(cleared_by IN ('system', 'user'))
);

-- ============================================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_entry_time ON positions(entry_time);
CREATE INDEX IF NOT EXISTS idx_positions_exit_time ON positions(exit_time);
CREATE INDEX IF NOT EXISTS idx_pnl_position ON pnl_snapshots(position_id);
CREATE INDEX IF NOT EXISTS idx_pnl_created ON pnl_snapshots(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_position ON orders(position_id);
CREATE INDEX IF NOT EXISTS idx_orders_kite_id ON orders(kite_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alert_queue(status);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alert_queue(created_at);
CREATE INDEX IF NOT EXISTS idx_cooldowns_end ON cooldowns(cooldown_end);
CREATE INDEX IF NOT EXISTS idx_alert_cooldowns ON alert_cooldowns(position_id, trigger_type);
CREATE INDEX IF NOT EXISTS idx_market_data_date ON market_data(date);
CREATE INDEX IF NOT EXISTS idx_entry_attempts_time ON entry_attempts(attempt_time);
CREATE INDEX IF NOT EXISTS idx_claude_decisions_position ON claude_decisions(position_id);

-- ============================================================================
-- INITIAL DATA
-- ============================================================================

-- Initialize system status to normal
INSERT OR IGNORE INTO system_status (id, status, reason, set_by)
VALUES (1, 'normal', 'System initialized', 'system');
