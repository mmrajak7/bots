#!/usr/bin/env python3
"""
Striker CLI - Interactive Options Trading Terminal

A conversational CLI for options trading on NEO API.
Understands natural language commands for strategy building and execution.

Usage:
    run.bat              (Windows - recommended)
    py -3.11 striker.py  (Windows - manual)
    python3.11 striker.py (Linux/Mac)

Examples:
    > ICICIBANK debit spread
    > NIFTY iron condor
    > trade
    > positions
    > exit
"""

import sys
import os

# Python version check - NEO API v2 requires Python 3.11+
if sys.version_info < (3, 11):
    print(f"\n  ERROR: Python 3.11+ required. You have {sys.version_info.major}.{sys.version_info.minor}")
    print("  Run with: py -3.11 striker.py")
    print("  Or use:   run.bat\n")
    sys.exit(1)

# Set UTF-8 encoding for Windows console (for Unicode symbols)
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
import logging
import json
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

# Add Scalper to path for reusing modules (SessionManager, OrderManager, etc.)
# Must be added BEFORE Striker so Striker's core/ takes precedence
_scalper_path = os.path.join(os.path.dirname(__file__), '..', 'Scalper')
sys.path.insert(0, _scalper_path)

# Add Striker directory for local imports (core, strategies)
# Insert at 0 so Striker's core/ shadows Scalper's core/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from striker_core.option_chain import OptionChain
from striker_core.command_parser import CommandParser, CommandType, ParsedCommand
from strategies.builder import StrategyBuilder, Strategy, StrategyType


@dataclass
class TradeLog:
    """Record of an executed trade."""
    timestamp: datetime
    symbol: str
    strategy: str
    action: str  # 'ENTRY' or 'EXIT'
    legs: List[Dict]
    net_premium: float
    order_ids: List[str]

    def to_dict(self) -> Dict:
        return {
            'timestamp': self.timestamp.isoformat(),
            'symbol': self.symbol,
            'strategy': self.strategy,
            'action': self.action,
            'legs': self.legs,
            'net_premium': self.net_premium,
            'order_ids': self.order_ids
        }


@dataclass
class PositionSLTarget:
    """Track SL and Target for a position."""
    symbol: str
    entry_premium: float  # Net premium at entry (positive = credit, negative = debit)
    current_value: float  # Current position value
    lot_size: int
    qty: int  # Total quantity

    # Stop Loss
    sl_value: Optional[float] = None  # SL trigger value (loss amount)
    sl_percentage: Optional[float] = None  # SL as % of premium
    sl_order_id: Optional[str] = None  # NEO order ID for SL

    # Target
    target_value: Optional[float] = None  # Target value (profit amount)
    target_percentage: Optional[float] = None  # Target as % of max profit
    target_order_id: Optional[str] = None  # NEO order ID for target

    # Trailing SL
    tsl_enabled: bool = False
    tsl_trail_pct: float = 50  # Trail at 50% of profit by default
    highest_profit: float = 0  # Track highest profit seen

    @property
    def unrealized_pnl(self) -> float:
        """Calculate unrealized P&L."""
        return self.current_value - self.entry_premium

    @property
    def sl_trigger_price(self) -> Optional[float]:
        """Get SL trigger in terms of position value."""
        if self.sl_value:
            return self.entry_premium - self.sl_value
        return None

    @property
    def target_trigger_price(self) -> Optional[float]:
        """Get target trigger in terms of position value."""
        if self.target_value:
            return self.entry_premium + self.target_value
        return None

    def should_trail_sl(self, current_pnl: float) -> bool:
        """Check if SL should be trailed."""
        if not self.tsl_enabled or current_pnl <= 0:
            return False

        # Update highest profit
        if current_pnl > self.highest_profit:
            self.highest_profit = current_pnl
            return True  # New high, should trail

        return False

    def get_new_tsl_value(self) -> float:
        """Calculate new TSL value based on highest profit."""
        if self.highest_profit > 0:
            # Trail at tsl_trail_pct of highest profit
            locked_profit = self.highest_profit * (self.tsl_trail_pct / 100)
            return locked_profit
        return 0


@dataclass
class SessionStats:
    """Track session trading statistics."""
    start_time: datetime = field(default_factory=datetime.now)
    trades_executed: int = 0
    total_credits: float = 0
    total_debits: float = 0
    realized_pnl: float = 0
    daily_loss_limit: float = 0
    daily_loss_used: float = 0
    trade_logs: List[TradeLog] = field(default_factory=list)

    @property
    def net_premium(self) -> float:
        return self.total_credits - self.total_debits

    @property
    def loss_limit_remaining(self) -> float:
        if self.daily_loss_limit > 0:
            return self.daily_loss_limit - self.daily_loss_used
        return float('inf')

    @property
    def is_loss_limit_breached(self) -> bool:
        if self.daily_loss_limit > 0:
            return self.daily_loss_used >= self.daily_loss_limit
        return False

class MarketHours:
    """Market hours utility for NSE F&O."""

    # NSE F&O market hours
    MARKET_OPEN = time(9, 15)
    MARKET_CLOSE = time(15, 30)
    PRE_OPEN_START = time(9, 0)

    # MIS square-off time (for options, typically 3:15 PM warning)
    MIS_SQUAREOFF_WARNING = time(15, 15)
    MIS_SQUAREOFF = time(15, 25)

    @classmethod
    def is_market_open(cls) -> bool:
        """Check if market is currently open."""
        now = datetime.now().time()
        return cls.MARKET_OPEN <= now <= cls.MARKET_CLOSE

    @classmethod
    def is_pre_open(cls) -> bool:
        """Check if in pre-open session."""
        now = datetime.now().time()
        return cls.PRE_OPEN_START <= now < cls.MARKET_OPEN

    @classmethod
    def get_market_status(cls) -> str:
        """Get current market status."""
        now = datetime.now()

        # Check if weekend
        if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
            return "CLOSED (Weekend)"

        current_time = now.time()

        if current_time < cls.PRE_OPEN_START:
            return "CLOSED (Pre-market)"
        elif cls.PRE_OPEN_START <= current_time < cls.MARKET_OPEN:
            return "PRE-OPEN"
        elif cls.MARKET_OPEN <= current_time < cls.MIS_SQUAREOFF_WARNING:
            return "OPEN"
        elif cls.MIS_SQUAREOFF_WARNING <= current_time < cls.MIS_SQUAREOFF:
            return "OPEN (MIS Squareoff Soon!)"
        elif cls.MIS_SQUAREOFF <= current_time <= cls.MARKET_CLOSE:
            return "OPEN (MIS Squareoff Active)"
        else:
            return "CLOSED (After-hours)"

    @classmethod
    def can_trade(cls, product: str = 'MIS') -> tuple[bool, str]:
        """Check if trading is allowed.

        Returns:
            Tuple of (can_trade, reason)
        """
        now = datetime.now()

        # Weekend check
        if now.weekday() >= 5:
            return False, "Market closed on weekends"

        current_time = now.time()

        # Pre-market
        if current_time < cls.MARKET_OPEN:
            return False, f"Market opens at {cls.MARKET_OPEN.strftime('%H:%M')}"

        # After-hours
        if current_time > cls.MARKET_CLOSE:
            return False, "Market is closed for the day"

        # MIS-specific checks
        if product == 'MIS':
            if current_time >= cls.MIS_SQUAREOFF:
                return False, "MIS orders blocked - squareoff in progress"
            elif current_time >= cls.MIS_SQUAREOFF_WARNING:
                return True, "WARNING: MIS squareoff at 15:25. Use NRML for late trades."

        return True, "Market is open"

    @classmethod
    def time_to_close(cls) -> timedelta:
        """Get time remaining until market close."""
        now = datetime.now()
        close_dt = datetime.combine(now.date(), cls.MARKET_CLOSE)
        if now > close_dt:
            return timedelta(0)
        return close_dt - now


# Try to import Scalper modules
try:
    from core.session_manager import SessionManager
    from core.order_manager import OrderManager, OrderParams
    from core.symbol_mapper import SymbolMapper
    SCALPER_AVAILABLE = True
except ImportError:
    SCALPER_AVAILABLE = False
    print("Warning: Scalper modules not available. Running in demo mode.")

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class StrikerCLI:
    """Interactive options trading CLI."""

    BANNER = """
+==============================================================+
|                                                              |
|    STRIKER CLI v1.0 - Options Trading Terminal               |
|                                                              |
|    Type 'help' for commands or start with a symbol           |
|    Example: NIFTY iron condor                                |
|                                                              |
+==============================================================+
"""

    def __init__(self):
        """Initialize CLI - reuses Scalper's config and session."""
        # Load config from Scalper
        self.scalper_path = os.path.join(os.path.dirname(__file__), '..', 'Scalper')
        self.config = self._load_scalper_config()

        # Load Striker-specific settings (optional overrides)
        self.striker_config = self._load_striker_config()

        # Initialize components
        self.parser = CommandParser()
        self.option_chain = OptionChain()
        self.strategy_builder = None

        # NEO/Kite clients - will be set from Scalper's session
        self.neo_client = None
        self.kite_client = None
        self.order_manager = None
        self.symbol_mapper = None
        self.session_manager = None  # Scalper's session manager

        # State
        self.last_strategy: Optional[Strategy] = None
        self.pending_trade: Optional[Strategy] = None
        self.is_connected = False

        # Session tracking - use Striker config for daily loss limit
        daily_limit = self.striker_config.get('risk', {}).get('daily_loss_limit', 0)
        if not daily_limit:
            daily_limit = self.config.get('risk_management', {}).get('max_loss_per_day', 0)
        self.session = SessionStats(daily_loss_limit=daily_limit)

        # Trade log file path - use Scalper's logs directory
        self.log_dir = os.path.join(self.scalper_path, 'logs')
        os.makedirs(self.log_dir, exist_ok=True)
        self.trade_log_file = os.path.join(
            self.log_dir,
            f"striker_trades_{datetime.now().strftime('%Y%m%d')}.json"
        )

        # Active SL/Target tracking per symbol
        # Key: symbol (e.g., 'NIFTY'), Value: PositionSLTarget
        self.active_sl_targets: Dict[str, PositionSLTarget] = {}

    def _load_scalper_config(self) -> Dict[str, Any]:
        """Load Scalper's main configuration (shared settings and credentials)."""
        import yaml

        config_path = os.path.join(self.scalper_path, 'config', 'settings.yaml')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return yaml.safe_load(f) or {}

        logger.warning(f"Scalper config not found at {config_path}")
        return {}

    def _load_striker_config(self) -> Dict[str, Any]:
        """Load Striker-specific settings (optional overrides)."""
        import yaml

        config_path = os.path.join(os.path.dirname(__file__), 'config', 'settings.yaml')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return yaml.safe_load(f) or {}

        return {}

    def connect(self) -> bool:
        """Connect to NEO and Kite APIs - reuses Scalper's session."""
        print("\n  Connecting to trading APIs (reusing Scalper session)...")

        try:
            if not SCALPER_AVAILABLE:
                print("  ✗ Scalper modules not available.")
                self.strategy_builder = StrategyBuilder(self.option_chain)
                return False

            # Load credentials from Scalper's config
            creds_path = os.path.join(self.scalper_path, 'config', 'credentials.yaml')
            if not os.path.exists(creds_path):
                print(f"  ✗ Credentials not found at {creds_path}")
                self.strategy_builder = StrategyBuilder(self.option_chain)
                return False

            import yaml
            with open(creds_path, 'r') as f:
                creds = yaml.safe_load(f)

            # Check for existing NEO session file (shared with Scalper)
            neo_session_path = os.path.join(self.scalper_path, 'data', 'session.json')

            if os.path.exists(neo_session_path):
                # Reuse existing session
                print("  Found existing NEO session...")
                try:
                    with open(neo_session_path, 'r') as f:
                        session_data = json.load(f)

                    # Check if session is from today
                    session_date = session_data.get('date', '')
                    today = datetime.now().strftime('%Y-%m-%d')

                    if session_date == today:
                        # Session is valid, initialize client with existing tokens
                        self.session_manager = SessionManager(creds)
                        if self.session_manager.restore_session(session_data):
                            self.neo_client = self.session_manager.get_client()
                            print("  ✓ NEO session restored (shared with Scalper)")
                        else:
                            print("  Session restore failed, creating new...")
                            if self.session_manager.login():
                                self.neo_client = self.session_manager.get_client()
                                self._save_neo_session()
                                print("  ✓ NEO API connected (new session)")
                    else:
                        print("  Session expired, creating new...")
                        self.session_manager = SessionManager(creds)
                        if self.session_manager.login():
                            self.neo_client = self.session_manager.get_client()
                            self._save_neo_session()
                            print("  ✓ NEO API connected (new session)")

                except (json.JSONDecodeError, KeyError) as e:
                    logger.warning(f"Session file invalid: {e}")
                    self.session_manager = SessionManager(creds)
                    if self.session_manager.login():
                        self.neo_client = self.session_manager.get_client()
                        self._save_neo_session()
                        print("  ✓ NEO API connected (new session)")
            else:
                # No existing session, create new and save
                print("  No existing session, logging in...")
                self.session_manager = SessionManager(creds)
                if self.session_manager.login():
                    self.neo_client = self.session_manager.get_client()
                    self._save_neo_session()
                    print("  ✓ NEO API connected (new session saved)")

            # Initialize order manager and symbol mapper
            if self.neo_client:
                self.order_manager = OrderManager(self.neo_client, self.config)
                self.symbol_mapper = SymbolMapper(self.neo_client)
                print("  ✓ Order manager and symbol mapper initialized")

            # Initialize Kite client from shared BOTS/data/kite_access_token.json
            kite_token_path = os.path.join(self.scalper_path, '..', 'data', 'kite_access_token.json')

            if os.path.exists(kite_token_path):
                with open(kite_token_path, 'r') as f:
                    kite_data = json.load(f)

                api_key = kite_data.get('api_key')
                access_token = kite_data.get('access_token')

                if api_key and access_token:
                    from kiteconnect import KiteConnect
                    self.kite_client = KiteConnect(api_key=api_key)
                    self.kite_client.set_access_token(access_token)
                    print("  ✓ Kite API connected (shared token)")

            # Initialize option chain with Kite client
            if self.kite_client:
                self.option_chain.set_kite_client(self.kite_client)
                self.strategy_builder = StrategyBuilder(self.option_chain)
                self.is_connected = True
                return True

            print("  ⚠ Running in limited mode (no live data)")
            self.strategy_builder = StrategyBuilder(self.option_chain)
            return False

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            print(f"  ✗ Connection failed: {e}")
            self.strategy_builder = StrategyBuilder(self.option_chain)
            return False

    def _save_neo_session(self):
        """Save NEO session for sharing with Scalper and other bots."""
        if not self.session_manager:
            return

        try:
            session_data = self.session_manager.get_session_data()

            # Save to Scalper's session file (shared location)
            session_path = os.path.join(self.scalper_path, 'data', 'session.json')
            os.makedirs(os.path.dirname(session_path), exist_ok=True)

            with open(session_path, 'w') as f:
                json.dump(session_data, f, indent=2)

            logger.info(f"NEO session saved to {session_path}")

        except Exception as e:
            logger.warning(f"Failed to save NEO session: {e}")

    def _check_margin(self, strategy: Strategy) -> tuple[bool, float, str]:
        """
        Check if sufficient margin is available for the strategy.

        Returns:
            Tuple of (sufficient, available_margin, message)
        """
        if not self.neo_client:
            return True, 0, "Cannot check margin - not connected"

        try:
            limits = self.neo_client.limits()
            data = limits.get('data', {}) if limits else {}

            available = float(
                data.get('availableCash', 0) or
                data.get('net', 0) or
                data.get('available', {}).get('cash', 0) or 0
            )

            # Estimate margin required
            # For spreads, margin = max_loss + some buffer
            # This is a rough estimate - actual margin depends on exchange rules
            if strategy.max_loss != float('inf'):
                estimated_margin = strategy.max_loss * 1.2  # 20% buffer
            else:
                # For unlimited loss strategies (naked options), use spot * lot * 10%
                estimated_margin = strategy.spot * strategy.lot_size * 0.1 * len(strategy.legs)

            if available < estimated_margin:
                return False, available, f"Insufficient margin. Need ~₹{estimated_margin:,.0f}, have ₹{available:,.0f}"

            return True, available, f"Margin OK. Available: ₹{available:,.0f}"

        except Exception as e:
            logger.warning(f"Margin check failed: {e}")
            return True, 0, f"Could not verify margin: {e}"

    def _log_trade(self, trade: TradeLog):
        """Log trade to file and session stats."""
        self.session.trade_logs.append(trade)
        self.session.trades_executed += 1

        if trade.net_premium > 0:
            self.session.total_credits += trade.net_premium
        else:
            self.session.total_debits += abs(trade.net_premium)

        # Write to file
        try:
            existing_logs = []
            if os.path.exists(self.trade_log_file):
                with open(self.trade_log_file, 'r') as f:
                    existing_logs = json.load(f)

            existing_logs.append(trade.to_dict())

            with open(self.trade_log_file, 'w') as f:
                json.dump(existing_logs, f, indent=2)

        except Exception as e:
            logger.warning(f"Failed to write trade log: {e}")

    def _show_session_stats(self):
        """Display session trading statistics."""
        print(f"\n{'='*50}")
        print("  SESSION STATISTICS")
        print(f"{'='*50}")
        print(f"  Session Start:    {self.session.start_time.strftime('%H:%M:%S')}")
        print(f"  Trades Executed:  {self.session.trades_executed}")
        print(f"{'-'*50}")
        print(f"  Total Credits:    ₹{self.session.total_credits:,.2f}")
        print(f"  Total Debits:     ₹{self.session.total_debits:,.2f}")

        net = self.session.net_premium
        color = "\033[92m" if net >= 0 else "\033[91m"
        print(f"  Net Premium:      {color}₹{net:,.2f}\033[0m")

        if self.session.realized_pnl != 0:
            pnl_color = "\033[92m" if self.session.realized_pnl >= 0 else "\033[91m"
            print(f"  Realized P&L:     {pnl_color}₹{self.session.realized_pnl:,.2f}\033[0m")

        if self.session.daily_loss_limit > 0:
            print(f"{'-'*50}")
            remaining = self.session.loss_limit_remaining
            used_pct = (self.session.daily_loss_used / self.session.daily_loss_limit * 100)
            print(f"  Daily Loss Limit: ₹{self.session.daily_loss_limit:,.0f}")
            print(f"  Loss Used:        ₹{self.session.daily_loss_used:,.0f} ({used_pct:.1f}%)")
            print(f"  Remaining:        ₹{remaining:,.0f}")

        print(f"{'='*50}\n")

    def run(self):
        """Main CLI loop."""
        print(self.BANNER)

        # Connect to APIs
        self.connect()

        # Show market status
        market_status = MarketHours.get_market_status()
        print(f"\n  Market Status: {market_status}")
        print("  Ready. Type a command or 'help' for options.\n")

        while True:
            try:
                # Show prompt with market status indicator
                if self.is_connected:
                    if MarketHours.is_market_open():
                        prompt = "\033[92mstriker>\033[0m "  # Green when market open
                    else:
                        prompt = "\033[93mstriker[closed]>\033[0m "  # Yellow when closed
                else:
                    prompt = "\033[91mstriker[offline]>\033[0m "  # Red when offline

                user_input = input(prompt).strip()

                if not user_input:
                    continue

                # Parse command
                cmd = self.parser.parse(user_input)

                # Execute command
                self._execute_command(cmd)

            except KeyboardInterrupt:
                print("\n\n  Use 'quit' to exit.\n")
            except EOFError:
                break
            except Exception as e:
                logger.error(f"Error: {e}", exc_info=True)
                print(f"\n  Error: {e}\n")

        print("\n  Goodbye!\n")

    def _execute_command(self, cmd: ParsedCommand):
        """Execute a parsed command."""
        if cmd.command_type == CommandType.QUIT:
            print("\n  Exiting Striker CLI...\n")
            sys.exit(0)

        elif cmd.command_type == CommandType.HELP:
            print(self.parser.get_help_text())

        elif cmd.command_type == CommandType.STRATEGY:
            self._handle_strategy(cmd)

        elif cmd.command_type == CommandType.TRADE:
            self._handle_trade()

        elif cmd.command_type == CommandType.POSITIONS:
            self._handle_positions(cmd.raw_text if hasattr(cmd, 'raw_text') else '')

        elif cmd.command_type == CommandType.ORDERS:
            self._handle_orders()

        elif cmd.command_type == CommandType.EXIT:
            self._handle_exit(cmd.symbol)

        elif cmd.command_type == CommandType.EXIT_ALL:
            self._handle_exit_all()

        elif cmd.command_type == CommandType.CHAIN:
            self._handle_chain(cmd.symbol)

        elif cmd.command_type == CommandType.SPOT:
            self._handle_spot(cmd.symbol)

        elif cmd.command_type == CommandType.MARGIN:
            self._handle_margin()

        elif cmd.command_type == CommandType.REFRESH:
            self.option_chain.clear_cache()
            print("\n  ✓ Cache cleared. Data will refresh on next request.\n")

        elif cmd.command_type == CommandType.STATUS:
            self._show_session_stats()

        elif cmd.command_type == CommandType.PARTIAL_EXIT:
            self._handle_partial_exit(cmd.symbol, cmd.exit_percentage)

        elif cmd.command_type == CommandType.ROLL:
            self._handle_roll(cmd.symbol)

        elif cmd.command_type == CommandType.SET_SL:
            self._handle_set_sl(cmd.symbol, cmd.value, cmd.is_percentage)

        elif cmd.command_type == CommandType.SET_TARGET:
            self._handle_set_target(cmd.symbol, cmd.value, cmd.is_percentage)

        elif cmd.command_type == CommandType.TSL:
            self._handle_tsl(cmd.symbol, cmd.value, cmd.is_percentage)

        elif cmd.command_type == CommandType.CANCEL_SL:
            self._handle_cancel_sl(cmd.symbol)

        elif cmd.command_type == CommandType.CANCEL_TARGET:
            self._handle_cancel_target(cmd.symbol)

        else:
            print(f"\n  Unknown command. Type 'help' for available commands.\n")

    def _handle_strategy(self, cmd: ParsedCommand):
        """Build and display a strategy."""
        if not cmd.symbol:
            print("\n  Please specify a symbol. Example: NIFTY iron condor\n")
            return

        if not self.strategy_builder:
            print("\n  Strategy builder not initialized.\n")
            return

        try:
            strategy = None
            symbol = cmd.symbol
            lots = cmd.lots
            width = cmd.width

            # Build strategy based on type
            if cmd.strategy in ['bull_call', 'debit_call', 'bull_call_spread']:
                strategy = self.strategy_builder.build_spread(symbol, 'bull_call', width, lots)
            elif cmd.strategy in ['bear_call', 'credit_call', 'bear_call_spread']:
                strategy = self.strategy_builder.build_spread(symbol, 'bear_call', width, lots)
            elif cmd.strategy in ['bull_put', 'credit_put', 'bull_put_spread']:
                strategy = self.strategy_builder.build_spread(symbol, 'bull_put', width, lots)
            elif cmd.strategy in ['bear_put', 'debit_put', 'bear_put_spread']:
                strategy = self.strategy_builder.build_spread(symbol, 'bear_put', width, lots)
            elif cmd.strategy in ['spread', 'debit']:
                # Default debit spread to bull call
                strategy = self.strategy_builder.build_spread(symbol, 'bull_call', width, lots)
            elif cmd.strategy in ['credit']:
                # Default credit spread to bull put
                strategy = self.strategy_builder.build_spread(symbol, 'bull_put', width, lots)
            elif cmd.strategy == 'straddle':
                direction = cmd.direction or 'long'
                strategy = self.strategy_builder.build_straddle(symbol, direction, lots)
            elif cmd.strategy == 'strangle':
                direction = cmd.direction or 'long'
                strategy = self.strategy_builder.build_strangle(symbol, direction, width, lots)
            elif cmd.strategy == 'iron_condor':
                strategy = self.strategy_builder.build_iron_condor(symbol, width, lots=lots)
            elif cmd.strategy == 'iron_butterfly':
                strategy = self.strategy_builder.build_iron_butterfly(symbol, width, lots)
            else:
                # Try to infer from direction
                if cmd.direction in ['bull', 'long']:
                    strategy = self.strategy_builder.build_spread(symbol, 'bull_call', width, lots)
                elif cmd.direction in ['bear', 'short']:
                    strategy = self.strategy_builder.build_spread(symbol, 'bear_put', width, lots)
                else:
                    # Default to showing chain
                    self._handle_chain(symbol)
                    return

            if strategy:
                self.last_strategy = strategy
                self.pending_trade = strategy
                print(strategy.get_summary())
                print("  Type 'trade' to execute this strategy.\n")

        except Exception as e:
            logger.error(f"Strategy build failed: {e}", exc_info=True)
            print(f"\n  Failed to build strategy: {e}\n")

    def _handle_trade(self):
        """Execute the pending trade with all safety checks."""
        if not self.pending_trade:
            print("\n  No pending trade. Build a strategy first.\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API. Cannot execute trade.\n")
            print(f"  Pending strategy: {self.pending_trade.name}\n")
            return

        strategy = self.pending_trade

        # === SAFETY CHECK 1: Market Hours ===
        can_trade, market_msg = MarketHours.can_trade('MIS')
        if not can_trade:
            print(f"\n  ✗ BLOCKED: {market_msg}\n")
            return

        if "WARNING" in market_msg:
            print(f"\n  ⚠ {market_msg}")

        # === SAFETY CHECK 2: Daily Loss Limit ===
        if self.session.is_loss_limit_breached:
            print(f"\n  ✗ BLOCKED: Daily loss limit breached!")
            print(f"    Loss used: ₹{self.session.daily_loss_used:,.0f}")
            print(f"    Limit: ₹{self.session.daily_loss_limit:,.0f}")
            print("\n  Trading halted for the day. Reset tomorrow or increase limit.\n")
            return

        # Warn if approaching loss limit
        if self.session.daily_loss_limit > 0:
            remaining_pct = (self.session.loss_limit_remaining / self.session.daily_loss_limit * 100)
            if remaining_pct < 30:
                print(f"\n  ⚠ WARNING: Only {remaining_pct:.0f}% of daily loss limit remaining!")

        # === SAFETY CHECK 3: Margin Check ===
        margin_ok, available_margin, margin_msg = self._check_margin(strategy)
        print(f"\n  Margin Check: {margin_msg}")

        if not margin_ok:
            print(f"\n  ✗ BLOCKED: {margin_msg}\n")
            return

        # === SAFETY CHECK 4: Strategy Staleness ===
        if strategy.is_stale:
            print(f"\n  ⚠ Strategy data is stale (built {(datetime.now() - strategy.built_at).seconds}s ago)")
            print("  Consider rebuilding with fresh prices.")

        # === SAFETY CHECK 5: Liquidity Warnings ===
        if strategy.has_warnings:
            print("\n  ⚠ STRATEGY WARNINGS:")
            if strategy.dte_warning:
                print(f"    {strategy.dte_warning}")
            for warning in strategy.liquidity_warnings:
                print(f"    {warning}")

        # Confirm
        print(f"\n  About to execute: {strategy.name}")
        print(f"  Symbol: {strategy.symbol}")
        print(f"  Legs: {len(strategy.legs)}")

        if strategy.net_premium > 0:
            print(f"  Net Credit: ₹{strategy.net_premium:,.2f}")
        else:
            print(f"  Net Debit: ₹{abs(strategy.net_premium):,.2f}")

        # Check for price staleness - refresh prices before trading
        print("\n  Refreshing prices...")
        try:
            fresh_chain = self.option_chain.get_option_chain(
                strategy.symbol, strategy.expiry, strikes_around_atm=15
            )

            # Check each leg for price movement
            price_warnings = []
            for leg in strategy.legs:
                if leg.option_type == 'CE':
                    fresh_premium = fresh_chain['calls'].get(leg.strike, {}).get('ltp', 0)
                else:
                    fresh_premium = fresh_chain['puts'].get(leg.strike, {}).get('ltp', 0)

                if fresh_premium and leg.premium:
                    change_pct = abs((fresh_premium - leg.premium) / leg.premium * 100)
                    if change_pct > 5:  # More than 5% price change
                        price_warnings.append(
                            f"    {leg.strike} {leg.option_type}: ₹{leg.premium:.2f} → ₹{fresh_premium:.2f} ({change_pct:.1f}% change)"
                        )

            if price_warnings:
                print("\n  ⚠ PRICE WARNING - Premiums have moved significantly:")
                for warning in price_warnings:
                    print(warning)
                print("\n  Consider rebuilding the strategy with fresh prices.")
                proceed = input("  Continue anyway? (yes/no): ").strip().lower()
                if proceed not in ['yes', 'y']:
                    print("\n  Trade cancelled. Use the strategy command again to get fresh prices.\n")
                    return
            else:
                print("  ✓ Prices are current")

        except Exception as e:
            logger.warning(f"Price refresh failed: {e}")
            print(f"  ⚠ Could not verify current prices: {e}")

        confirm = input("\n  Confirm trade? (yes/no): ").strip().lower()

        if confirm not in ['yes', 'y']:
            print("\n  Trade cancelled.\n")
            return

        # CRITICAL: Sort legs - BUY FIRST, then SELL
        # This prevents naked short positions if later legs fail
        sorted_legs = sorted(strategy.legs, key=lambda l: (0 if l.action == 'BUY' else 1))

        print("\n  Execution order (BUY legs first for safety):")
        for i, leg in enumerate(sorted_legs, 1):
            print(f"    {i}. {leg.action} {leg.strike} {leg.option_type}")

        # Execute each leg in safe order
        print("\n  Executing trades...")
        success_count = 0
        fail_count = 0
        executed_orders = []  # Track for potential rollback

        for i, leg in enumerate(sorted_legs, 1):
            try:
                # Map symbol to NEO format
                neo_symbol = leg.trading_symbol
                if self.symbol_mapper and not neo_symbol:
                    # Try to map from Kite format
                    mapping = self.symbol_mapper.kite_to_neo(leg.trading_symbol)
                    if mapping:
                        neo_symbol = mapping.get('trading_symbol', '')

                if not neo_symbol:
                    print(f"    Leg {i}: ✗ Could not map symbol")
                    fail_count += 1

                    # CRITICAL: If SELL leg fails, we may have orphan BUY positions
                    if leg.action == 'SELL' and executed_orders:
                        print(f"\n  ⚠ WARNING: SELL leg failed but BUY legs executed!")
                        print(f"  You have {len(executed_orders)} open BUY position(s) without hedge.")
                        self._offer_rollback(executed_orders, strategy)
                    continue

                # Determine transaction type
                txn_type = 'B' if leg.action == 'BUY' else 'S'

                # Create order params
                params = OrderParams(
                    symbol=neo_symbol,
                    exchange_segment='nse_fo',
                    instrument_token=str(leg.token) if leg.token else '',
                    transaction_type=txn_type,
                    quantity=leg.quantity * strategy.lot_size,
                    product='MIS',
                    order_type='L',
                    price=leg.premium,
                    tag=f'STRIKER_{strategy.strategy_type.value}'
                )

                # Place order
                result = self.order_manager.place_order(params)

                if result.success:
                    print(f"    Leg {i}: ✓ {leg.action} {leg.strike} {leg.option_type} @ ₹{leg.premium} -> {result.order_id}")
                    success_count += 1
                    # Track for potential rollback
                    executed_orders.append({
                        'order_id': result.order_id,
                        'leg': leg,
                        'symbol': neo_symbol,
                        'quantity': leg.quantity * strategy.lot_size
                    })
                else:
                    print(f"    Leg {i}: ✗ {result.message}")
                    fail_count += 1

                    # CRITICAL: If SELL leg fails after BUY legs succeeded
                    if leg.action == 'SELL' and executed_orders:
                        print(f"\n  ⚠ WARNING: SELL leg failed but BUY legs executed!")
                        print(f"  You have unhedged BUY position(s).")
                        self._offer_rollback(executed_orders, strategy)

            except Exception as e:
                print(f"    Leg {i}: ✗ Error: {e}")
                fail_count += 1

                # Check for rollback need
                if leg.action == 'SELL' and executed_orders:
                    print(f"\n  ⚠ WARNING: SELL leg errored but BUY legs executed!")
                    self._offer_rollback(executed_orders, strategy)

        print(f"\n  Completed: {success_count} success, {fail_count} failed\n")

        # Log the trade if any legs executed
        if success_count > 0:
            trade_log = TradeLog(
                timestamp=datetime.now(),
                symbol=strategy.symbol,
                strategy=strategy.name,
                action='ENTRY',
                legs=[{
                    'strike': o['leg'].strike,
                    'type': o['leg'].option_type,
                    'action': o['leg'].action,
                    'premium': o['leg'].premium,
                    'qty': o['quantity']
                } for o in executed_orders],
                net_premium=strategy.net_premium,
                order_ids=[o['order_id'] for o in executed_orders]
            )
            self._log_trade(trade_log)

            print(f"  Trade logged. Session stats: {self.session.trades_executed} trades, Net: ₹{self.session.net_premium:,.2f}")

        if success_count == len(sorted_legs):
            self.pending_trade = None
        elif success_count > 0:
            print("  ⚠ Partial execution - check positions manually.\n")

    def _offer_rollback(self, executed_orders: list, strategy):
        """Offer to rollback/close executed orders after partial failure."""
        print(f"\n  Executed orders that need attention:")
        for order in executed_orders:
            leg = order['leg']
            print(f"    - {leg.action} {leg.strike} {leg.option_type} (ID: {order['order_id']})")

        rollback = input("\n  Rollback (close) these positions? (yes/no): ").strip().lower()

        if rollback in ['yes', 'y']:
            print("\n  Rolling back...")
            for order in executed_orders:
                try:
                    leg = order['leg']
                    # To close a BUY, we SELL; to close a SELL, we BUY
                    close_txn = 'S' if leg.action == 'BUY' else 'B'

                    params = OrderParams(
                        symbol=order['symbol'],
                        exchange_segment='nse_fo',
                        instrument_token=str(leg.token) if leg.token else '',
                        transaction_type=close_txn,
                        quantity=order['quantity'],
                        product='MIS',
                        order_type='MKT',  # Market order for quick exit
                        price=0,
                        tag='STRIKER_ROLLBACK'
                    )

                    result = self.order_manager.place_order(params)
                    if result.success:
                        print(f"    ✓ Closed {leg.strike} {leg.option_type}")
                    else:
                        print(f"    ✗ Failed to close {leg.strike} {leg.option_type}: {result.message}")

                except Exception as e:
                    print(f"    ✗ Rollback error: {e}")

            print("\n  Rollback complete. Check positions.\n")
        else:
            print("\n  Keeping positions open. Manage manually via 'positions' command.\n")

    def _handle_positions(self, raw_text: str = ''):
        """Show positions with SL/Target info.

        Args:
            raw_text: 'pos', 'pos open', 'pos closed', 'pos all'
        """
        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        # Parse filter from raw_text
        raw_lower = raw_text.lower().strip()
        if 'closed' in raw_lower:
            show_filter = 'closed'
        elif 'all' in raw_lower:
            show_filter = 'all'
        else:
            show_filter = 'open'  # Default

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []

            # Separate open and closed positions
            open_positions = [p for p in pos_list if int(p.get('qty', 0)) != 0]
            closed_positions = [p for p in pos_list if int(p.get('qty', 0)) == 0 and float(p.get('pnl', p.get('urmTom', 0)) or 0) != 0]

            # Determine which to display
            if show_filter == 'open':
                display_positions = open_positions
                title = "OPEN POSITIONS"
            elif show_filter == 'closed':
                display_positions = closed_positions
                title = "CLOSED POSITIONS (Today)"
            else:  # all
                display_positions = pos_list
                title = "ALL POSITIONS"

            if not display_positions:
                print(f"\n  No {show_filter} positions.\n")
                return

            print("\n" + "="*80)
            print(f"  {title}")
            print("="*80)

            # Group positions by underlying symbol
            grouped = {}
            for pos in display_positions:
                symbol = pos.get('trdSym', pos.get('tradingSymbol', pos.get('symbol', '')))
                # Extract underlying (e.g., NIFTY from NIFTY24JAN23500CE)
                underlying = symbol.split('24')[0] if '24' in symbol else symbol.split('25')[0] if '25' in symbol else symbol.split('26')[0] if '26' in symbol else symbol[:5]
                if underlying not in grouped:
                    grouped[underlying] = []
                grouped[underlying].append(pos)

            total_pnl = 0
            total_realized = 0

            for underlying, pos_group in grouped.items():
                group_pnl = 0
                print(f"\n  {underlying}")
                print(f"  {'-'*76}")

                for pos in pos_group:
                    symbol = pos.get('trdSym', pos.get('tradingSymbol', pos.get('symbol', '')))
                    qty = int(pos.get('qty', 0))
                    avg_price = float(pos.get('avgPrc', 0) or 0)
                    ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)
                    pnl = float(pos.get('pnl', pos.get('urmTom', 0)) or 0)
                    realized = float(pos.get('realPnl', pos.get('realisedPnl', 0)) or 0)

                    group_pnl += pnl
                    total_pnl += pnl
                    total_realized += realized

                    if qty != 0:
                        side = "LONG" if qty > 0 else "SHORT"
                    else:
                        side = "CLOSED"

                    pnl_color = "\033[92m" if pnl >= 0 else "\033[91m"

                    # Extract strike and option type from symbol
                    strike_info = symbol
                    for prefix in [underlying, 'NFO:', 'BFO:']:
                        strike_info = strike_info.replace(prefix, '')

                    if qty != 0:
                        print(f"    {side:6} {abs(qty):4} @ ₹{avg_price:8.2f}  |  LTP: ₹{ltp:8.2f}  |  P&L: {pnl_color}₹{pnl:>+10,.2f}\033[0m  [{strike_info}]")
                    else:
                        print(f"    {side:6} {'':4}   ₹{avg_price:8.2f}  |  {'':14}  |  P&L: {pnl_color}₹{pnl:>+10,.2f}\033[0m  [{strike_info}]")

                # Show group P&L
                pnl_color = "\033[92m" if group_pnl >= 0 else "\033[91m"
                print(f"  {'-'*76}")
                print(f"  {underlying} P&L: {pnl_color}₹{group_pnl:>+,.2f}\033[0m")

                # Show SL/Target if set for this underlying (only for open positions)
                if show_filter in ['open', 'all'] and underlying in self.active_sl_targets:
                    sl_target = self.active_sl_targets[underlying]
                    sl_info = []

                    if sl_target.sl_value:
                        sl_str = f"SL: ₹{sl_target.sl_value:,.0f}"
                        if sl_target.sl_percentage:
                            sl_str += f" ({sl_target.sl_percentage}%)"
                        if sl_target.tsl_enabled:
                            sl_str = "TSL: " + sl_str[4:]  # Replace SL with TSL
                        sl_info.append(sl_str)

                    if sl_target.target_value:
                        tgt_str = f"Target: ₹{sl_target.target_value:,.0f}"
                        if sl_target.target_percentage:
                            tgt_str += f" ({sl_target.target_percentage}%)"
                        sl_info.append(tgt_str)

                    if sl_info:
                        print(f"  \033[93m[ {' | '.join(sl_info)} ]\033[0m")

            print("\n" + "="*80)
            pnl_color = "\033[92m" if total_pnl >= 0 else "\033[91m"
            print(f"  TOTAL P&L: {pnl_color}₹{total_pnl:>+,.2f}\033[0m")
            if total_realized != 0:
                realized_color = "\033[92m" if total_realized >= 0 else "\033[91m"
                print(f"  REALIZED:  {realized_color}₹{total_realized:>+,.2f}\033[0m")
            print("="*80 + "\n")

        except Exception as e:
            print(f"\n  Failed to fetch positions: {e}\n")

    def _handle_orders(self):
        """Show pending orders."""
        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            order_report = self.neo_client.order_report()
            orders = order_report.get('data', []) if order_report else []

            # Filter for pending orders
            pending = [o for o in orders if o.get('ordSt', '').lower() in
                      ['pending', 'open', 'trigger pending', 'after market order req received']]

            if not pending:
                print("\n  No pending orders.\n")
                return

            print("\n" + "="*70)
            print("  PENDING ORDERS")
            print("="*70)

            for order in pending:
                order_id = order.get('nOrdNo', '')
                symbol = order.get('trdSym', '')
                txn = order.get('trnsTp', '')
                qty = order.get('qty', '')
                price = order.get('prc', order.get('trgPrc', ''))
                status = order.get('ordSt', '')

                action = "BUY" if txn == 'B' else "SELL"
                print(f"  {order_id[:8]}  {action} {symbol} x{qty} @ ₹{price}  [{status}]")

            print("="*70 + "\n")

        except Exception as e:
            print(f"\n  Failed to fetch orders: {e}\n")

    def _handle_exit(self, symbol: str):
        """Exit a specific position."""
        if not symbol:
            print("\n  Please specify symbol to exit. Example: exit NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []

            # Find matching positions
            matching = []
            for pos in pos_list:
                pos_symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
                if qty != 0 and symbol.upper() in pos_symbol.upper():
                    matching.append(pos)

            if not matching:
                print(f"\n  No open position found for {symbol}\n")
                return

            print(f"\n  Found {len(matching)} position(s) matching '{symbol}':")
            for i, pos in enumerate(matching, 1):
                sym = pos.get('tradingSymbol', '')
                qty = int(pos.get('qty', 0))
                print(f"    {i}. {sym} ({qty} qty)")

            confirm = input("\n  Exit all matching positions? (yes/no): ").strip().lower()

            if confirm not in ['yes', 'y']:
                print("\n  Cancelled.\n")
                return

            # CRITICAL: Sort positions - exit SHORTS first (buy back), then LONGS (sell)
            # Shorts have negative qty, longs have positive qty
            # Exiting shorts first reduces margin requirement and naked risk
            sorted_positions = sorted(matching, key=lambda p: int(p.get('qty', 0)))

            print("\n  Exit order (SHORTS first for safety):")
            for pos in sorted_positions:
                qty = int(pos.get('qty', 0))
                side = "SHORT" if qty < 0 else "LONG"
                print(f"    {side}: {pos.get('tradingSymbol', '')} ({qty})")

            # Exit each position in safe order
            print("\n  Executing exits...")
            for pos in sorted_positions:
                result = self.order_manager.exit_position(pos, 100)
                sym = pos.get('tradingSymbol', '')
                if result.success:
                    print(f"  ✓ Exit order placed for {sym}")
                else:
                    print(f"  ✗ Failed to exit {sym}: {result.message}")

            print()

        except Exception as e:
            print(f"\n  Failed to exit position: {e}\n")

    def _handle_exit_all(self):
        """Exit all positions - SHORTS first, then LONGS."""
        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []
            open_positions = [p for p in pos_list if int(p.get('qty', 0)) != 0]

            if not open_positions:
                print("\n  No open positions to exit.\n")
                return

            # Sort: SHORTS first (negative qty), then LONGS (positive qty)
            sorted_positions = sorted(open_positions, key=lambda p: int(p.get('qty', 0)))

            print(f"\n  Will exit {len(sorted_positions)} positions (SHORTS first):")
            for pos in sorted_positions:
                qty = int(pos.get('qty', 0))
                side = "SHORT" if qty < 0 else "LONG"
                print(f"    {side}: {pos.get('tradingSymbol', '')} ({qty})")

            confirm = input("\n  Exit ALL positions? This cannot be undone. (yes/no): ").strip().lower()

            if confirm not in ['yes', 'y']:
                print("\n  Cancelled.\n")
                return

            print("\n  Executing exits...")
            success = 0
            failed = 0

            for pos in sorted_positions:
                result = self.order_manager.exit_position(pos, 100)
                sym = pos.get('tradingSymbol', '')
                if result.success:
                    print(f"  ✓ {sym}")
                    success += 1
                else:
                    print(f"  ✗ {sym}: {result.message}")
                    failed += 1

            print(f"\n  Exit all: {success} success, {failed} failed\n")

        except Exception as e:
            print(f"\n  Failed to exit positions: {e}\n")

    def _handle_partial_exit(self, symbol: str, percentage: int):
        """Exit a percentage of a position."""
        if not symbol:
            print("\n  Please specify symbol. Example: exit 50 NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        if percentage <= 0 or percentage > 100:
            print(f"\n  Invalid percentage: {percentage}. Use 1-100.\n")
            return

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []

            # Find matching positions
            matching = []
            for pos in pos_list:
                pos_symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
                if qty != 0 and symbol.upper() in pos_symbol.upper():
                    matching.append(pos)

            if not matching:
                print(f"\n  No open position found for {symbol}\n")
                return

            print(f"\n  Partial Exit: {percentage}% of positions matching '{symbol}':")
            for pos in matching:
                qty = int(pos.get('qty', 0))
                exit_qty = int(abs(qty) * percentage / 100)
                print(f"    {pos.get('tradingSymbol', '')} ({qty}) -> Exit {exit_qty}")

            confirm = input("\n  Proceed with partial exit? (yes/no): ").strip().lower()

            if confirm not in ['yes', 'y']:
                print("\n  Cancelled.\n")
                return

            # Sort: SHORTS first for margin safety
            sorted_positions = sorted(matching, key=lambda p: int(p.get('qty', 0)))

            print("\n  Executing partial exits...")
            for pos in sorted_positions:
                result = self.order_manager.exit_position(pos, percentage)
                sym = pos.get('tradingSymbol', '')
                if result.success:
                    print(f"  ✓ {percentage}% exit placed for {sym}")
                else:
                    print(f"  ✗ Failed: {sym}: {result.message}")

            print()

        except Exception as e:
            print(f"\n  Failed to partial exit: {e}\n")

    def _handle_roll(self, symbol: str):
        """Roll position to next expiry."""
        if not symbol:
            print("\n  Please specify symbol to roll. Example: roll NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []

            # Find matching positions
            matching = []
            for pos in pos_list:
                pos_symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
                if qty != 0 and symbol.upper() in pos_symbol.upper():
                    matching.append(pos)

            if not matching:
                print(f"\n  No open position found for {symbol}\n")
                return

            print(f"\n  Roll positions matching '{symbol}':")
            for pos in matching:
                qty = int(pos.get('qty', 0))
                side = "SHORT" if qty < 0 else "LONG"
                ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)
                print(f"    {side}: {pos.get('tradingSymbol', '')} ({qty}) @ ₹{ltp:.2f}")

            # Get next expiry
            next_expiry = self.option_chain.suggest_expiry(symbol)
            print(f"\n  Roll to next expiry: {next_expiry.strftime('%d-%b-%Y')}")

            print("\n  ⚠ WARNING: Rolling will:")
            print("    1. Close all current positions at MARKET")
            print("    2. Open equivalent positions in next expiry at MARKET")
            print("    There may be slippage and price difference between expiries.")

            confirm = input("\n  Proceed with roll? (yes/no): ").strip().lower()

            if confirm not in ['yes', 'y']:
                print("\n  Roll cancelled.\n")
                return

            # Step 1: Exit current positions (SHORTS first)
            print("\n  Step 1: Closing current positions...")
            sorted_positions = sorted(matching, key=lambda p: int(p.get('qty', 0)))

            for pos in sorted_positions:
                result = self.order_manager.exit_position(pos, 100)
                sym = pos.get('tradingSymbol', '')
                if result.success:
                    print(f"    ✓ Closed {sym}")
                else:
                    print(f"    ✗ Failed to close {sym}: {result.message}")
                    print("\n  Roll aborted due to close failure. Check positions manually.\n")
                    return

            # Step 2: Get chain for new expiry and open new positions
            print("\n  Step 2: Opening new positions in next expiry...")

            # For now, just inform user to manually build new strategy
            # Full implementation would parse the old positions, find equivalent strikes
            # in the new expiry, and place new orders
            print("\n  ⚠ Positions closed. To complete the roll:")
            print(f"    1. Build your strategy for the new expiry:")
            print(f"       > {symbol} <your strategy> (e.g., NIFTY iron condor)")
            print(f"    2. Execute with 'trade'\n")

            # Log the exit
            trade_log = TradeLog(
                timestamp=datetime.now(),
                symbol=symbol,
                strategy=f"ROLL_EXIT",
                action='EXIT',
                legs=[{
                    'symbol': pos.get('tradingSymbol', ''),
                    'qty': pos.get('qty', 0)
                } for pos in sorted_positions],
                net_premium=0,  # Will be realized P&L
                order_ids=[]
            )
            self._log_trade(trade_log)

        except Exception as e:
            print(f"\n  Failed to roll position: {e}\n")

    def _get_position_info(self, symbol: str) -> Optional[Dict]:
        """Get position info and calculate current P&L."""
        if not self.neo_client:
            return None

        try:
            positions = self.neo_client.positions()
            pos_list = positions.get('data', []) if positions else []

            # Find matching positions
            matching = []
            total_qty = 0
            total_value = 0

            for pos in pos_list:
                pos_symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
                if qty != 0 and symbol.upper() in pos_symbol.upper():
                    matching.append(pos)
                    total_qty += qty
                    # Calculate current value
                    ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)
                    avg_price = float(pos.get('avgPrice', pos.get('buyAvgPrice', pos.get('sellAvgPrice', 0))) or 0)
                    pnl = float(pos.get('pnl', pos.get('mtm', 0)) or 0)
                    total_value += pnl

            if not matching:
                return None

            return {
                'positions': matching,
                'total_qty': total_qty,
                'current_pnl': total_value,
                'symbol': symbol.upper()
            }

        except Exception as e:
            logger.error(f"Failed to get position info: {e}")
            return None

    def _handle_set_sl(self, symbol: str, value: float, is_percentage: bool):
        """Set stop loss for a position."""
        if not symbol:
            print("\n  Please specify symbol. Example: sl NIFTY 50%\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            # Get current position
            pos_info = self._get_position_info(symbol)
            if not pos_info:
                print(f"\n  No open position found for {symbol}\n")
                return

            current_pnl = pos_info['current_pnl']
            positions = pos_info['positions']

            # Calculate SL value
            if is_percentage:
                # SL at X% of premium means exit if loss exceeds X% of entry value
                # For credit strategies: SL at 50% means exit if premium doubles (50% loss of max profit)
                # Get entry premium from tracking or estimate from current
                if symbol in self.active_sl_targets:
                    entry_premium = self.active_sl_targets[symbol].entry_premium
                else:
                    # Estimate entry premium - for credit strategies, entry_premium is positive
                    entry_premium = abs(current_pnl) + sum(
                        float(p.get('pnl', 0) or 0) for p in positions
                    )

                sl_value = entry_premium * (value / 100)
                print(f"\n  Setting SL at {value}% of entry premium (₹{sl_value:,.2f})")
            else:
                sl_value = value
                print(f"\n  Setting SL at ₹{sl_value:,.2f} loss")

            # Store SL info
            if symbol not in self.active_sl_targets:
                self.active_sl_targets[symbol] = PositionSLTarget(
                    symbol=symbol,
                    entry_premium=0,  # Will be set properly when trade is executed
                    current_value=current_pnl,
                    lot_size=1,
                    qty=pos_info['total_qty']
                )

            self.active_sl_targets[symbol].sl_value = sl_value
            self.active_sl_targets[symbol].sl_percentage = value if is_percentage else None

            # Place SL orders for each leg
            print(f"\n  Placing SL orders for {len(positions)} position(s)...")

            for pos in positions:
                qty = int(pos.get('qty', 0))
                trading_symbol = pos.get('tradingSymbol', '')
                ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)

                # Calculate SL trigger price for this leg
                # For a SHORT (qty < 0), SL triggers when price rises (buy back)
                # For a LONG (qty > 0), SL triggers when price falls (sell)
                if qty < 0:  # SHORT position
                    # SL trigger when we need to buy back at higher price
                    # Trigger at current price + SL buffer
                    sl_trigger = ltp * (1 + value/100) if is_percentage else ltp + (sl_value / abs(qty))
                    sl_price = sl_trigger * 1.01  # Limit price 1% above trigger
                    txn_type = 'B'  # Buy to close
                else:  # LONG position
                    # SL trigger when we need to sell at lower price
                    sl_trigger = ltp * (1 - value/100) if is_percentage else ltp - (sl_value / abs(qty))
                    sl_price = sl_trigger * 0.99  # Limit price 1% below trigger
                    txn_type = 'S'  # Sell to close

                sl_trigger = max(0.05, sl_trigger)  # Min price
                sl_price = max(0.05, sl_price)

                print(f"    {trading_symbol}: SL Trigger ₹{sl_trigger:.2f}, Limit ₹{sl_price:.2f}")

                # Place SL-L order via NEO
                try:
                    order_result = self.neo_client.place_order(
                        exchange_segment='nse_fo',
                        product='MIS',
                        price=str(round(sl_price, 2)),
                        order_type='SL',  # Stop Loss Limit
                        quantity=str(abs(qty)),
                        validity='DAY',
                        trading_symbol=trading_symbol,
                        transaction_type=txn_type,
                        trigger_price=str(round(sl_trigger, 2)),
                        tag='STRIKER_SL'
                    )

                    if order_result and order_result.get('stat') == 'Ok':
                        order_id = order_result.get('nOrdNo', 'N/A')
                        print(f"    ✓ SL order placed: {order_id}")
                        self.active_sl_targets[symbol].sl_order_id = order_id
                    else:
                        error = order_result.get('errMsg', 'Unknown error') if order_result else 'No response'
                        print(f"    ✗ SL order failed: {error}")

                except Exception as e:
                    print(f"    ✗ SL order error: {e}")

            print()

        except Exception as e:
            print(f"\n  Failed to set SL: {e}\n")

    def _handle_set_target(self, symbol: str, value: float, is_percentage: bool):
        """Set target for a position."""
        if not symbol:
            print("\n  Please specify symbol. Example: target NIFTY 80%\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            # Get current position
            pos_info = self._get_position_info(symbol)
            if not pos_info:
                print(f"\n  No open position found for {symbol}\n")
                return

            current_pnl = pos_info['current_pnl']
            positions = pos_info['positions']

            # Calculate target value
            if is_percentage:
                # Target at X% means capture X% of max profit
                # For credit strategies, max profit = entry premium
                if symbol in self.active_sl_targets:
                    entry_premium = self.active_sl_targets[symbol].entry_premium
                else:
                    entry_premium = abs(current_pnl)

                target_value = entry_premium * (value / 100)
                print(f"\n  Setting target at {value}% profit (₹{target_value:,.2f})")
            else:
                target_value = value
                print(f"\n  Setting target at ₹{target_value:,.2f} profit")

            # Store target info
            if symbol not in self.active_sl_targets:
                self.active_sl_targets[symbol] = PositionSLTarget(
                    symbol=symbol,
                    entry_premium=0,
                    current_value=current_pnl,
                    lot_size=1,
                    qty=pos_info['total_qty']
                )

            self.active_sl_targets[symbol].target_value = target_value
            self.active_sl_targets[symbol].target_percentage = value if is_percentage else None

            # Place target/limit orders for each leg
            print(f"\n  Placing Target orders for {len(positions)} position(s)...")

            for pos in positions:
                qty = int(pos.get('qty', 0))
                trading_symbol = pos.get('tradingSymbol', '')
                ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)

                # Calculate target price for this leg
                # For SHORT: target when price falls (buy back cheaper)
                # For LONG: target when price rises (sell higher)
                if qty < 0:  # SHORT position - buy back cheaper
                    target_price = ltp * (1 - value/100) if is_percentage else max(0.05, ltp - (target_value / abs(qty)))
                    txn_type = 'B'
                else:  # LONG position - sell higher
                    target_price = ltp * (1 + value/100) if is_percentage else ltp + (target_value / abs(qty))
                    txn_type = 'S'

                target_price = max(0.05, target_price)

                print(f"    {trading_symbol}: Target ₹{target_price:.2f}")

                # Place limit order via NEO
                try:
                    order_result = self.neo_client.place_order(
                        exchange_segment='nse_fo',
                        product='MIS',
                        price=str(round(target_price, 2)),
                        order_type='L',  # Limit
                        quantity=str(abs(qty)),
                        validity='DAY',
                        trading_symbol=trading_symbol,
                        transaction_type=txn_type,
                        tag='STRIKER_TARGET'
                    )

                    if order_result and order_result.get('stat') == 'Ok':
                        order_id = order_result.get('nOrdNo', 'N/A')
                        print(f"    ✓ Target order placed: {order_id}")
                        self.active_sl_targets[symbol].target_order_id = order_id
                    else:
                        error = order_result.get('errMsg', 'Unknown error') if order_result else 'No response'
                        print(f"    ✗ Target order failed: {error}")

                except Exception as e:
                    print(f"    ✗ Target order error: {e}")

            print()

        except Exception as e:
            print(f"\n  Failed to set target: {e}\n")

    def _handle_tsl(self, symbol: str, value: Optional[float], is_percentage: bool):
        """Trail stop loss to lock in profits."""
        if not symbol:
            print("\n  Please specify symbol. Example: tsl NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            # Get current position
            pos_info = self._get_position_info(symbol)
            if not pos_info:
                print(f"\n  No open position found for {symbol}\n")
                return

            current_pnl = pos_info['current_pnl']
            positions = pos_info['positions']

            # Default trail percentage
            trail_pct = value if value else 50

            print(f"\n  Trailing Stop Loss for {symbol}")
            print(f"  Current P&L: ₹{current_pnl:,.2f}")

            if current_pnl <= 0:
                print(f"\n  ⚠ Position is in loss. TSL only works when in profit.")
                print(f"  Consider setting a regular SL instead: sl {symbol} <value>\n")
                return

            # Calculate new SL to lock in trail_pct of current profit
            locked_profit = current_pnl * (trail_pct / 100)
            print(f"  Locking in {trail_pct}% of profit = ₹{locked_profit:,.2f}")

            # Update tracking
            if symbol in self.active_sl_targets:
                self.active_sl_targets[symbol].tsl_enabled = True
                self.active_sl_targets[symbol].tsl_trail_pct = trail_pct
                self.active_sl_targets[symbol].highest_profit = max(
                    self.active_sl_targets[symbol].highest_profit,
                    current_pnl
                )

                # Cancel existing SL order if any
                if self.active_sl_targets[symbol].sl_order_id:
                    print(f"\n  Cancelling existing SL order...")
                    try:
                        self.neo_client.cancel_order(
                            order_id=self.active_sl_targets[symbol].sl_order_id,
                            isVerify='false'
                        )
                        print(f"    ✓ Old SL cancelled")
                    except Exception as e:
                        print(f"    ⚠ Could not cancel old SL: {e}")
            else:
                self.active_sl_targets[symbol] = PositionSLTarget(
                    symbol=symbol,
                    entry_premium=0,
                    current_value=current_pnl,
                    lot_size=1,
                    qty=pos_info['total_qty'],
                    tsl_enabled=True,
                    tsl_trail_pct=trail_pct,
                    highest_profit=current_pnl
                )

            # Place new SL orders at trailed level
            print(f"\n  Placing trailed SL orders...")

            for pos in positions:
                qty = int(pos.get('qty', 0))
                trading_symbol = pos.get('tradingSymbol', '')
                ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)
                avg_price = float(pos.get('avgPrice', pos.get('buyAvgPrice', pos.get('sellAvgPrice', ltp))) or ltp)

                # Calculate trailed SL trigger
                # Lock in trail_pct of profit
                if qty < 0:  # SHORT - bought at avg, now worth ltp
                    # Profit when ltp < avg (we sold high, now buying low)
                    profit_per_unit = avg_price - ltp
                    lock_per_unit = profit_per_unit * (trail_pct / 100)
                    sl_trigger = avg_price - lock_per_unit  # Allow price to rise to here
                    sl_price = sl_trigger * 1.01
                    txn_type = 'B'
                else:  # LONG
                    profit_per_unit = ltp - avg_price
                    lock_per_unit = profit_per_unit * (trail_pct / 100)
                    sl_trigger = avg_price + lock_per_unit  # Allow price to fall to here
                    sl_price = sl_trigger * 0.99
                    txn_type = 'S'

                sl_trigger = max(0.05, sl_trigger)
                sl_price = max(0.05, sl_price)

                print(f"    {trading_symbol}: Trailed SL Trigger ₹{sl_trigger:.2f}")

                # Place SL order
                try:
                    order_result = self.neo_client.place_order(
                        exchange_segment='nse_fo',
                        product='MIS',
                        price=str(round(sl_price, 2)),
                        order_type='SL',
                        quantity=str(abs(qty)),
                        validity='DAY',
                        trading_symbol=trading_symbol,
                        transaction_type=txn_type,
                        trigger_price=str(round(sl_trigger, 2)),
                        tag='STRIKER_TSL'
                    )

                    if order_result and order_result.get('stat') == 'Ok':
                        order_id = order_result.get('nOrdNo', 'N/A')
                        print(f"    ✓ TSL order placed: {order_id}")
                        self.active_sl_targets[symbol].sl_order_id = order_id
                    else:
                        error = order_result.get('errMsg', 'Unknown error') if order_result else 'No response'
                        print(f"    ✗ TSL order failed: {error}")

                except Exception as e:
                    print(f"    ✗ TSL order error: {e}")

            print(f"\n  TSL active. Run 'tsl {symbol}' again to update as profits increase.\n")

        except Exception as e:
            print(f"\n  Failed to set TSL: {e}\n")

    def _handle_cancel_sl(self, symbol: str):
        """Cancel SL order for a position."""
        if not symbol:
            print("\n  Please specify symbol. Example: cancel sl NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            if symbol not in self.active_sl_targets or not self.active_sl_targets[symbol].sl_order_id:
                print(f"\n  No active SL order found for {symbol}\n")
                return

            order_id = self.active_sl_targets[symbol].sl_order_id

            print(f"\n  Cancelling SL order {order_id} for {symbol}...")

            result = self.neo_client.cancel_order(
                order_id=order_id,
                isVerify='false'
            )

            if result and result.get('stat') == 'Ok':
                print(f"  ✓ SL order cancelled")
                self.active_sl_targets[symbol].sl_order_id = None
                self.active_sl_targets[symbol].sl_value = None
                self.active_sl_targets[symbol].tsl_enabled = False
            else:
                error = result.get('errMsg', 'Unknown error') if result else 'No response'
                print(f"  ✗ Failed to cancel: {error}")

            print()

        except Exception as e:
            print(f"\n  Failed to cancel SL: {e}\n")

    def _handle_cancel_target(self, symbol: str):
        """Cancel target order for a position."""
        if not symbol:
            print("\n  Please specify symbol. Example: cancel target NIFTY\n")
            return

        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            if symbol not in self.active_sl_targets or not self.active_sl_targets[symbol].target_order_id:
                print(f"\n  No active target order found for {symbol}\n")
                return

            order_id = self.active_sl_targets[symbol].target_order_id

            print(f"\n  Cancelling target order {order_id} for {symbol}...")

            result = self.neo_client.cancel_order(
                order_id=order_id,
                isVerify='false'
            )

            if result and result.get('stat') == 'Ok':
                print(f"  ✓ Target order cancelled")
                self.active_sl_targets[symbol].target_order_id = None
                self.active_sl_targets[symbol].target_value = None
            else:
                error = result.get('errMsg', 'Unknown error') if result else 'No response'
                print(f"  ✗ Failed to cancel: {error}")

            print()

        except Exception as e:
            print(f"\n  Failed to cancel target: {e}\n")

    def _handle_chain(self, symbol: str):
        """Show option chain for symbol."""
        if not symbol:
            print("\n  Please specify a symbol. Example: NIFTY\n")
            return

        try:
            # Get suggested expiry
            expiry = self.option_chain.suggest_expiry(symbol)

            # Get chain
            chain = self.option_chain.get_option_chain(symbol, expiry, strikes_around_atm=5)

            print("\n" + "="*70)
            print(f"  OPTION CHAIN: {chain['symbol']}")
            print(f"  Spot: ₹{chain['spot']:,.2f}  |  ATM: {chain['atm']}  |  DTE: {chain['dte']} days")
            print(f"  Expiry: {chain['expiry'].strftime('%d-%b-%Y')}  |  Lot: {chain['lot_size']}")
            print("="*70)
            print(f"  {'CALLS':^25}  STRIKE  {'PUTS':^25}")
            print("-"*70)

            for strike in chain['strikes']:
                call = chain['calls'].get(strike, {})
                put = chain['puts'].get(strike, {})

                call_ltp = call.get('ltp', 0)
                put_ltp = put.get('ltp', 0)

                # Highlight ATM
                if strike == chain['atm']:
                    print(f"  {call_ltp:>10.2f}              \033[93m{strike:^7}\033[0m              {put_ltp:<10.2f}")
                else:
                    print(f"  {call_ltp:>10.2f}              {strike:^7}              {put_ltp:<10.2f}")

            print("="*70 + "\n")

        except Exception as e:
            print(f"\n  Failed to fetch chain: {e}\n")

    def _handle_spot(self, symbol: str):
        """Show spot price for symbol."""
        if not symbol:
            print("\n  Please specify a symbol.\n")
            return

        try:
            spot = self.option_chain.get_spot_price(symbol)
            if spot:
                print(f"\n  {symbol.upper()}: ₹{spot:,.2f}\n")
            else:
                print(f"\n  Could not fetch spot for {symbol}\n")
        except Exception as e:
            print(f"\n  Error: {e}\n")

    def _handle_margin(self):
        """Show available margin."""
        if not self.neo_client:
            print("\n  Not connected to NEO API.\n")
            return

        try:
            limits = self.neo_client.limits()
            data = limits.get('data', {}) if limits else {}

            available = float(
                data.get('availableCash', 0) or
                data.get('net', 0) or
                data.get('available', {}).get('cash', 0) or 0
            )

            used = float(data.get('utilizedMargin', 0) or 0)

            print("\n" + "="*40)
            print("  MARGIN")
            print("="*40)
            print(f"  Available: ₹{available:,.2f}")
            print(f"  Used:      ₹{used:,.2f}")
            print("="*40 + "\n")

        except Exception as e:
            print(f"\n  Failed to fetch margin: {e}\n")


def main():
    """Entry point."""
    cli = StrikerCLI()
    cli.run()


if __name__ == '__main__':
    main()
