"""
Striker CLI - Natural Language Command Parser

Parses trading commands in natural language.
Examples:
    "ICICIBANK debit spread"
    "NIFTY iron condor"
    "short straddle on BANKNIFTY"
    "bear put spread RELIANCE 2 lots"
"""

import re
from typing import Dict, Optional, Tuple, Any, List
from dataclasses import dataclass
from enum import Enum
import logging

logger = logging.getLogger(__name__)


class CommandType(Enum):
    """Types of CLI commands."""
    STRATEGY = "strategy"
    POSITIONS = "positions"
    ORDERS = "orders"
    EXIT = "exit"
    EXIT_ALL = "exit_all"
    PARTIAL_EXIT = "partial_exit"  # Exit with percentage
    TRADE = "trade"
    CHAIN = "chain"
    SPOT = "spot"
    HELP = "help"
    QUIT = "quit"
    REFRESH = "refresh"
    MARGIN = "margin"
    STATUS = "status"  # Session stats
    ROLL = "roll"  # Roll to next expiry
    SET_SL = "set_sl"  # Set stop loss
    SET_TARGET = "set_target"  # Set target
    TSL = "tsl"  # Trailing stop loss
    CANCEL_SL = "cancel_sl"  # Cancel SL order
    CANCEL_TARGET = "cancel_target"  # Cancel target order
    UNKNOWN = "unknown"


@dataclass
class ParsedCommand:
    """Parsed command with extracted parameters."""
    command_type: CommandType
    symbol: Optional[str] = None
    strategy: Optional[str] = None
    direction: Optional[str] = None  # 'long', 'short', 'bull', 'bear'
    lots: int = 1
    width: Optional[int] = None
    expiry: Optional[str] = None
    exit_percentage: int = 100  # For partial exits (25, 50, 75, 100)
    value: Optional[float] = None  # For SL/Target amounts
    is_percentage: bool = False  # True if value is percentage
    raw_text: str = ""
    confidence: float = 1.0
    extra_params: Dict[str, Any] = None

    def __post_init__(self):
        if self.extra_params is None:
            self.extra_params = {}


class CommandParser:
    """Parses natural language trading commands."""

    # Known symbols (indices + popular F&O stocks)
    INDICES = {
        'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX', 'MIDCPNIFTY', 'BANKEX',
        'NIFTY50', 'BANK NIFTY', 'FIN NIFTY'
    }

    # Common F&O stocks
    FNO_STOCKS = {
        'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'BHARTIARTL',
        'ITC', 'HINDUNILVR', 'KOTAKBANK', 'LT', 'AXISBANK', 'BAJFINANCE',
        'MARUTI', 'ASIANPAINT', 'TITAN', 'SUNPHARMA', 'WIPRO', 'HCLTECH',
        'ULTRACEMCO', 'TATAMOTORS', 'ONGC', 'NTPC', 'POWERGRID', 'COALINDIA',
        'TATASTEEL', 'JSWSTEEL', 'HINDALCO', 'ADANIENT', 'ADANIPORTS',
        'BAJAJ-AUTO', 'BAJAJFINSV', 'BRITANNIA', 'CIPLA', 'DIVISLAB',
        'DRREDDY', 'EICHERMOT', 'GRASIM', 'HEROMOTOCO', 'INDUSINDBK',
        'M&M', 'NESTLEIND', 'SBILIFE', 'SHREECEM', 'TECHM', 'TATACONSUM',
        'APOLLOHOSP', 'DMART', 'PIDILITIND', 'HAVELLS', 'SIEMENS', 'DLF',
        'GODREJCP', 'DABUR', 'MARICO', 'BERGEPAINT', 'COLPAL', 'TRENT',
        'ABB', 'CANBK', 'FEDERALBNK', 'PNB', 'BANKBARODA', 'IDFCFIRSTB',
        'ZOMATO', 'PAYTM', 'NYKAA', 'POLICYBZR', 'IRCTC', 'HAL', 'BEL',
        'BHEL', 'GAIL', 'IOC', 'BPCL', 'RECLTD', 'PFC', 'IRFC'
    }

    # Strategy keywords mapping
    STRATEGY_PATTERNS = {
        # Spreads
        r'\b(bull\s*call|debit\s*call|call\s*debit)\s*(spread)?\b': ('spread', 'bull_call'),
        r'\b(bear\s*call|credit\s*call|call\s*credit)\s*(spread)?\b': ('spread', 'bear_call'),
        r'\b(bull\s*put|credit\s*put|put\s*credit)\s*(spread)?\b': ('spread', 'bull_put'),
        r'\b(bear\s*put|debit\s*put|put\s*debit)\s*(spread)?\b': ('spread', 'bear_put'),
        r'\b(debit)\s*(spread)?\b': ('spread', 'debit'),  # Will infer call/put from context
        r'\b(credit)\s*(spread)?\b': ('spread', 'credit'),

        # Straddles
        r'\b(long|buy)\s*straddle\b': ('straddle', 'long'),
        r'\b(short|sell)\s*straddle\b': ('straddle', 'short'),
        r'\bstraddle\b': ('straddle', 'long'),  # Default to long

        # Strangles
        r'\b(long|buy)\s*strangle\b': ('strangle', 'long'),
        r'\b(short|sell)\s*strangle\b': ('strangle', 'short'),
        r'\bstrangle\b': ('strangle', 'long'),

        # Iron strategies
        r'\biron\s*condor\b': ('iron_condor', None),
        r'\biron\s*fly\b': ('iron_butterfly', None),
        r'\biron\s*butterfly\b': ('iron_butterfly', None),
        r'\bcondor\b': ('iron_condor', None),

        # Butterflies
        r'\b(call|ce)\s*butterfly\b': ('call_butterfly', 'long'),
        r'\b(put|pe)\s*butterfly\b': ('put_butterfly', 'long'),
        r'\bbutterfly\b': ('call_butterfly', 'long'),

        # Ratio spreads
        r'\b(call|ce)\s*ratio\b': ('call_ratio', None),
        r'\b(put|pe)\s*ratio\b': ('put_ratio', None),
    }

    # Direction keywords
    DIRECTION_KEYWORDS = {
        'bullish': 'bull',
        'bearish': 'bear',
        'long': 'long',
        'short': 'short',
        'buy': 'long',
        'sell': 'short',
        'bull': 'bull',
        'bear': 'bear',
        'up': 'bull',
        'down': 'bear',
    }

    # Command keywords
    COMMAND_KEYWORDS = {
        'positions': CommandType.POSITIONS,
        'position': CommandType.POSITIONS,
        'pos': CommandType.POSITIONS,
        'portfolio': CommandType.POSITIONS,
        'holdings': CommandType.POSITIONS,

        'orders': CommandType.ORDERS,
        'order': CommandType.ORDERS,
        'orderbook': CommandType.ORDERS,

        'exit': CommandType.EXIT,
        'close': CommandType.EXIT,
        'square off': CommandType.EXIT,
        'squareoff': CommandType.EXIT,

        'exit all': CommandType.EXIT_ALL,
        'close all': CommandType.EXIT_ALL,
        'square off all': CommandType.EXIT_ALL,

        'trade': CommandType.TRADE,
        'execute': CommandType.TRADE,
        'place': CommandType.TRADE,
        'do it': CommandType.TRADE,
        'go': CommandType.TRADE,
        'yes': CommandType.TRADE,

        'chain': CommandType.CHAIN,
        'option chain': CommandType.CHAIN,
        'oc': CommandType.CHAIN,

        'spot': CommandType.SPOT,
        'price': CommandType.SPOT,
        'ltp': CommandType.SPOT,

        'help': CommandType.HELP,
        '?': CommandType.HELP,
        'commands': CommandType.HELP,

        'quit': CommandType.QUIT,
        'exit cli': CommandType.QUIT,
        'bye': CommandType.QUIT,
        'q': CommandType.QUIT,

        'refresh': CommandType.REFRESH,
        'reload': CommandType.REFRESH,

        'margin': CommandType.MARGIN,
        'funds': CommandType.MARGIN,
        'balance': CommandType.MARGIN,

        'status': CommandType.STATUS,
        'stats': CommandType.STATUS,
        'session': CommandType.STATUS,
        'pnl': CommandType.STATUS,

        'roll': CommandType.ROLL,
        'rollover': CommandType.ROLL,

        'sl': CommandType.SET_SL,
        'stoploss': CommandType.SET_SL,
        'stop': CommandType.SET_SL,

        'target': CommandType.SET_TARGET,
        'tp': CommandType.SET_TARGET,
        'takeprofit': CommandType.SET_TARGET,

        'tsl': CommandType.TSL,
        'trail': CommandType.TSL,
        'trailing': CommandType.TSL,

        'cancel sl': CommandType.CANCEL_SL,
        'cancelsl': CommandType.CANCEL_SL,
        'remove sl': CommandType.CANCEL_SL,

        'cancel target': CommandType.CANCEL_TARGET,
        'canceltarget': CommandType.CANCEL_TARGET,
        'remove target': CommandType.CANCEL_TARGET,
    }

    def __init__(self):
        """Initialize parser with compiled patterns."""
        self._compiled_patterns = {
            re.compile(pattern, re.IGNORECASE): value
            for pattern, value in self.STRATEGY_PATTERNS.items()
        }

    def parse(self, text: str) -> ParsedCommand:
        """
        Parse a natural language command.

        Args:
            text: User input text

        Returns:
            ParsedCommand with extracted parameters
        """
        text = text.strip()
        if not text:
            return ParsedCommand(command_type=CommandType.UNKNOWN, raw_text=text)

        text_lower = text.lower()

        # Check for partial exit command (exit 50 NIFTY, exit 25% RELIANCE)
        partial_exit_match = re.match(r'^(exit|close)\s+(\d+)%?\s*(.*)$', text_lower)
        if partial_exit_match:
            pct = int(partial_exit_match.group(2))
            remaining = partial_exit_match.group(3).strip()
            symbol = self._extract_symbol(remaining) if remaining else None

            return ParsedCommand(
                command_type=CommandType.PARTIAL_EXIT,
                symbol=symbol,
                exit_percentage=pct,
                raw_text=text
            )

        # Check for SL command (sl NIFTY 50%, sl NIFTY 2000)
        sl_match = re.match(r'^(sl|stoploss|stop)\s+(\w+)\s+(\d+\.?\d*)(%)?$', text_lower)
        if sl_match:
            symbol = sl_match.group(2).upper()
            value = float(sl_match.group(3))
            is_pct = sl_match.group(4) == '%'

            return ParsedCommand(
                command_type=CommandType.SET_SL,
                symbol=symbol,
                value=value,
                is_percentage=is_pct,
                raw_text=text
            )

        # Check for Target command (target NIFTY 80%, target NIFTY 3000)
        target_match = re.match(r'^(target|tp|takeprofit)\s+(\w+)\s+(\d+\.?\d*)(%)?$', text_lower)
        if target_match:
            symbol = target_match.group(2).upper()
            value = float(target_match.group(3))
            is_pct = target_match.group(4) == '%'

            return ParsedCommand(
                command_type=CommandType.SET_TARGET,
                symbol=symbol,
                value=value,
                is_percentage=is_pct,
                raw_text=text
            )

        # Check for TSL command (tsl NIFTY, tsl NIFTY 50%)
        tsl_match = re.match(r'^(tsl|trail|trailing)\s+(\w+)(?:\s+(\d+\.?\d*)(%)?)?$', text_lower)
        if tsl_match:
            symbol = tsl_match.group(2).upper()
            value = float(tsl_match.group(3)) if tsl_match.group(3) else None
            is_pct = tsl_match.group(4) == '%' if tsl_match.group(4) else True  # Default to %

            return ParsedCommand(
                command_type=CommandType.TSL,
                symbol=symbol,
                value=value,
                is_percentage=is_pct,
                raw_text=text
            )

        # Check for cancel SL/Target (cancel sl NIFTY)
        cancel_sl_match = re.match(r'^(cancel\s*sl|cancelsl|remove\s*sl)\s+(\w+)$', text_lower)
        if cancel_sl_match:
            symbol = cancel_sl_match.group(2).upper()
            return ParsedCommand(
                command_type=CommandType.CANCEL_SL,
                symbol=symbol,
                raw_text=text
            )

        cancel_target_match = re.match(r'^(cancel\s*target|canceltarget|remove\s*target)\s+(\w+)$', text_lower)
        if cancel_target_match:
            symbol = cancel_target_match.group(2).upper()
            return ParsedCommand(
                command_type=CommandType.CANCEL_TARGET,
                symbol=symbol,
                raw_text=text
            )

        # Check for simple commands first
        for keyword, cmd_type in self.COMMAND_KEYWORDS.items():
            if text_lower == keyword or text_lower.startswith(keyword + ' '):
                # Extract symbol if present after command
                remaining = text_lower.replace(keyword, '').strip()
                symbol = self._extract_symbol(remaining) if remaining else None

                return ParsedCommand(
                    command_type=cmd_type,
                    symbol=symbol,
                    raw_text=text
                )

        # Try to parse as strategy command
        return self._parse_strategy_command(text)

    def _parse_strategy_command(self, text: str) -> ParsedCommand:
        """Parse a strategy-related command."""
        text_lower = text.lower()

        # Extract symbol
        symbol = self._extract_symbol(text)

        # Extract strategy type and direction
        strategy, direction = self._extract_strategy(text_lower)

        # Extract lots
        lots = self._extract_lots(text_lower)

        # Extract width if specified
        width = self._extract_width(text_lower)

        # Determine command type
        if strategy:
            cmd_type = CommandType.STRATEGY
        elif symbol:
            # Just a symbol - show chain/spot
            cmd_type = CommandType.CHAIN
        else:
            cmd_type = CommandType.UNKNOWN

        return ParsedCommand(
            command_type=cmd_type,
            symbol=symbol,
            strategy=strategy,
            direction=direction,
            lots=lots,
            width=width,
            raw_text=text
        )

    def _extract_symbol(self, text: str) -> Optional[str]:
        """Extract stock/index symbol from text."""
        text_upper = text.upper()

        # Check indices first
        for idx in self.INDICES:
            # Handle variations
            normalized = idx.replace(' ', '')
            if normalized in text_upper.replace(' ', ''):
                # Return canonical name
                if 'NIFTY50' in text_upper or ('NIFTY' in text_upper and 'BANK' not in text_upper and 'FIN' not in text_upper and 'MIDCP' not in text_upper):
                    return 'NIFTY'
                elif 'BANKNIFTY' in text_upper.replace(' ', '') or 'BANK NIFTY' in text_upper:
                    return 'BANKNIFTY'
                elif 'FINNIFTY' in text_upper.replace(' ', '') or 'FIN NIFTY' in text_upper:
                    return 'FINNIFTY'
                elif 'MIDCPNIFTY' in text_upper.replace(' ', ''):
                    return 'MIDCPNIFTY'
                elif 'SENSEX' in text_upper:
                    return 'SENSEX'
                elif 'BANKEX' in text_upper:
                    return 'BANKEX'

        # Check F&O stocks
        for stock in self.FNO_STOCKS:
            if stock in text_upper:
                return stock

        # Try to find any word that looks like a symbol (all caps, 2-15 chars)
        words = text.split()
        for word in words:
            word_clean = word.upper().strip('.,!?')
            if word_clean.isalpha() and 2 <= len(word_clean) <= 15:
                if word_clean not in {'ON', 'OF', 'THE', 'AND', 'OR', 'FOR', 'WITH', 'BUY', 'SELL',
                                      'LONG', 'SHORT', 'BULL', 'BEAR', 'CALL', 'PUT', 'CE', 'PE',
                                      'DEBIT', 'CREDIT', 'SPREAD', 'IRON', 'CONDOR', 'BUTTERFLY',
                                      'STRADDLE', 'STRANGLE', 'LOT', 'LOTS', 'WIDTH', 'STRIKE',
                                      'STRIKES', 'WEEKLY', 'MONTHLY', 'EXPIRY'}:
                    return word_clean

        return None

    def _extract_strategy(self, text: str) -> Tuple[Optional[str], Optional[str]]:
        """Extract strategy type and direction from text."""
        for pattern, (strategy, direction) in self._compiled_patterns.items():
            if pattern.search(text):
                # If direction is 'debit' or 'credit', need to determine call/put
                if direction == 'debit':
                    # Debit spread - check for bull/bear context
                    if 'bull' in text or 'up' in text:
                        return 'bull_call', 'bull'
                    elif 'bear' in text or 'down' in text:
                        return 'bear_put', 'bear'
                    else:
                        return 'bull_call', 'bull'  # Default debit to bull call
                elif direction == 'credit':
                    if 'bull' in text or 'up' in text:
                        return 'bull_put', 'bull'
                    elif 'bear' in text or 'down' in text:
                        return 'bear_call', 'bear'
                    else:
                        return 'bull_put', 'bull'  # Default credit to bull put

                return strategy, direction

        # Check for direction keywords to infer strategy
        for keyword, dir_value in self.DIRECTION_KEYWORDS.items():
            if keyword in text:
                return None, dir_value

        return None, None

    def _extract_lots(self, text: str) -> int:
        """Extract number of lots from text."""
        # Pattern: "X lot(s)" or "X qty"
        patterns = [
            r'(\d+)\s*lot',
            r'(\d+)\s*qty',
            r'qty\s*(\d+)',
            r'quantity\s*(\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        return 1  # Default

    def _extract_width(self, text: str) -> Optional[int]:
        """Extract strike width from text."""
        patterns = [
            r'(\d+)\s*width',
            r'width\s*(\d+)',
            r'(\d+)\s*strike',
            r'(\d+)\s*point',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return int(match.group(1))

        return None

    def get_help_text(self) -> str:
        """Get help text for available commands."""
        return """
╔══════════════════════════════════════════════════════════════╗
║                    STRIKER CLI - COMMANDS                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  STRATEGY COMMANDS (natural language):                       ║
║  ─────────────────────────────────────                       ║
║    <SYMBOL> debit spread         Bull call spread            ║
║    <SYMBOL> credit spread        Bull put spread             ║
║    <SYMBOL> bear call spread     Credit call spread          ║
║    <SYMBOL> bear put spread      Debit put spread            ║
║    <SYMBOL> straddle             Long straddle               ║
║    <SYMBOL> short straddle       Short straddle              ║
║    <SYMBOL> strangle             Long strangle               ║
║    <SYMBOL> iron condor          Iron condor                 ║
║    <SYMBOL> iron butterfly       Iron butterfly              ║
║                                                              ║
║  MODIFIERS:                                                  ║
║  ──────────                                                  ║
║    ... 2 lots                    Set quantity                ║
║    ... 100 width                 Set strike width            ║
║                                                              ║
║  ACTION COMMANDS:                                            ║
║  ────────────────                                            ║
║    trade / go / yes              Execute last strategy       ║
║    positions / pos               Show current positions      ║
║    orders                        Show pending orders         ║
║    exit <SYMBOL>                 Exit 100% of position       ║
║    exit 50 <SYMBOL>              Exit 50% of position        ║
║    exit all                      Exit all positions          ║
║    roll <SYMBOL>                 Roll to next expiry         ║
║                                                              ║
║  RISK MANAGEMENT:                                            ║
║  ────────────────                                            ║
║    sl <SYMBOL> 50%               Set SL at 50% of premium    ║
║    sl <SYMBOL> 2000              Set SL at Rs.2000 loss      ║
║    target <SYMBOL> 80%           Set target at 80% profit    ║
║    target <SYMBOL> 3000          Set target at Rs.3000       ║
║    tsl <SYMBOL>                  Trail SL to lock profits    ║
║    tsl <SYMBOL> 50%              Trail SL at 50% of profit   ║
║    cancel sl <SYMBOL>            Cancel SL order             ║
║    cancel target <SYMBOL>        Cancel target order         ║
║                                                              ║
║  INFO COMMANDS:                                              ║
║  ──────────────                                              ║
║    <SYMBOL>                      Show option chain           ║
║    spot <SYMBOL>                 Show spot price             ║
║    margin / funds                Show available margin       ║
║    status / pnl                  Show session P&L            ║
║    refresh                       Refresh data                ║
║                                                              ║
║  SYSTEM:                                                     ║
║  ───────                                                     ║
║    help / ?                      Show this help              ║
║    quit / q                      Exit CLI                    ║
║                                                              ║
║  EXAMPLES:                                                   ║
║  ─────────                                                   ║
║    > ICICIBANK debit spread                                  ║
║    > NIFTY iron condor 2 lots                                ║
║    > short straddle on BANKNIFTY                             ║
║    > RELIANCE bear put spread 100 width                      ║
║    > trade                                                   ║
║    > exit 50 NIFTY                                           ║
║    > roll NIFTY                                              ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
"""
