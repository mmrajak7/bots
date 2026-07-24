#!/usr/bin/env python3
"""
Butterfly Trade Analyzer

Analyzes and executes long butterfly trades for stocks and indices.
Uses premium-based wing spread calculation (similar to SNAIL iron fly logic).

Usage:
    python butterfly_analyzer.py RELIANCE BUY
    python butterfly_analyzer.py NIFTY SELL
    python butterfly_analyzer.py INFY BUY --multiplier 1.2 --lots 2
    python butterfly_analyzer.py NIFTY BUY --execute  # Actually place orders

Features:
    - Fetches LTP via Kite API
    - Calculates ATM strike from underlying price
    - Dynamic wing spread based on ATM option premium
    - Filters: Bid-Ask spread, OI, Volume, DTE
    - Returns: Max profit, R:R ratio, debit, capital needed
    - Order execution with margin-optimized sequence
    - JSON logging of all analyses
    - Future provision for debit spreads

@author   Helper Bot
@created  2025-12-29
"""

import os
import sys
import csv
import json
import argparse
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional, List, Dict, Tuple, Any
from enum import Enum
from pathlib import Path

# Add SNAIL to path for Kite client reuse
SCRIPT_DIR = Path(__file__).parent.resolve()
SNAIL_PATH = SCRIPT_DIR.parent / 'SNAIL'
sys.path.insert(0, str(SNAIL_PATH))

# Also add parent for module resolution
sys.path.insert(0, str(SNAIL_PATH.parent))

try:
    from dotenv import load_dotenv
    # Load env files
    env_file = SNAIL_PATH / '.env'
    creds_file = SNAIL_PATH / 'creds.env'
    if env_file.exists():
        load_dotenv(env_file)
    if creds_file.exists():
        load_dotenv(creds_file)
except ImportError:
    pass

# Change to SNAIL directory for proper imports
os.chdir(str(SNAIL_PATH))


# =============================================================================
# CONFIGURATION & CONSTANTS
# =============================================================================

class Direction(Enum):
    """Trade direction."""
    BUY = "BUY"    # Bullish
    SELL = "SELL"  # Bearish


class Strategy(Enum):
    """
    Strategy type for options trades.

    Currently supported:
    - BUTTERFLY: Long butterfly spread (debit)

    Future strategies (provision for extension):
    - DEBIT_SPREAD: Bull call spread or Bear put spread
    - IRON_CONDOR: Short iron condor
    """
    BUTTERFLY = "BUTTERFLY"
    DEBIT_SPREAD = "DEBIT_SPREAD"  # Future: Bull Call / Bear Put Spread


class InstrumentType(Enum):
    """Type of underlying instrument."""
    STOCK = "STOCK"
    INDEX = "INDEX"


# Filter thresholds
FILTERS = {
    'bid_ask_spread_max': 1.50,       # Max Rs per leg for slippage control
    'oi_atm_min': 5000,               # Min OI for ATM strike (liquidity)
    'volume_min': 50000,              # Min daily volume (entry/exit ease)
    'dte_min_stock': 20,              # Min DTE for stock options
    'dte_min_index': 6,               # Min DTE for index options
}

# Strike intervals for rounding
STRIKE_INTERVALS = {
    'NIFTY': 50,
    'SENSEX': 100,
    'BANKNIFTY': 100,
    'DEFAULT': 50,  # For stocks
}

# Lot sizes (approximate, will be read from CSV)
DEFAULT_LOT_SIZES = {
    'NIFTY': 75,
    'SENSEX': 10,
    'BANKNIFTY': 15,
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OptionLeg:
    """Single option leg details."""
    tradingsymbol: str
    strike: float
    option_type: str  # CE or PE
    expiry: str
    instrument_token: str
    lot_size: int
    bid: float = 0.0
    ask: float = 0.0
    ltp: float = 0.0
    oi: int = 0
    volume: int = 0

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        return self.ask - self.bid

    @property
    def mid_price(self) -> float:
        """Mid price between bid and ask."""
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ltp


@dataclass
class ButterflyAnalysis:
    """Complete butterfly trade analysis."""
    underlying: str
    underlying_price: float
    direction: Direction
    instrument_type: InstrumentType
    expiry: str
    dte: int

    # Strikes
    atm_strike: float
    itm_strike: float
    otm_strike: float
    wing_distance: int

    # Legs (buy ITM, sell 2x ATM, buy OTM)
    itm_leg: Optional[OptionLeg] = None
    atm_leg: Optional[OptionLeg] = None
    otm_leg: Optional[OptionLeg] = None

    # Pricing (using bid-ask)
    lot_size: int = 0
    num_lots: int = 1

    # Costs (per lot)
    itm_buy_cost: float = 0.0      # Ask price (we buy)
    atm_sell_credit: float = 0.0  # Bid price x 2 (we sell)
    otm_buy_cost: float = 0.0     # Ask price (we buy)

    # Metrics
    net_debit: float = 0.0        # Total cost to enter (per lot)
    max_profit: float = 0.0       # Max profit (per lot)
    max_loss: float = 0.0         # Max loss = net debit (per lot)
    risk_reward: float = 0.0      # R:R ratio (risk/reward)
    breakeven_lower: float = 0.0
    breakeven_upper: float = 0.0

    # Total values (all lots)
    total_debit: float = 0.0
    total_max_profit: float = 0.0
    total_max_loss: float = 0.0
    capital_required: float = 0.0

    # Breakeven & Probability Analysis
    profit_zone_width: float = 0.0          # Width of profit zone in points
    profit_zone_pct: float = 0.0            # Profit zone as % of underlying
    distance_to_lower_be: float = 0.0       # Distance from LTP to lower breakeven
    distance_to_upper_be: float = 0.0       # Distance from LTP to upper breakeven
    distance_to_lower_be_pct: float = 0.0   # As percentage
    distance_to_upper_be_pct: float = 0.0   # As percentage
    expected_move: float = 0.0              # Expected move based on ATM premium
    probability_of_profit: float = 0.0      # Estimated POP (%)
    breakeven_assessment: str = ""          # Good/Moderate/Risky

    # Filter results
    filters_passed: bool = True
    filter_warnings: List[str] = field(default_factory=list)
    filter_failures: List[str] = field(default_factory=list)

    # Recommendation
    recommendation: str = ""

    def calculate_metrics(self):
        """Calculate all P&L metrics after legs are populated."""
        if not all([self.itm_leg, self.atm_leg, self.otm_leg]):
            return

        # Get prices (bid for sell, ask for buy)
        self.itm_buy_cost = self.itm_leg.ask
        self.atm_sell_credit = self.atm_leg.bid * 2  # Sell 2 lots
        self.otm_buy_cost = self.otm_leg.ask

        # Net debit per share
        # Long Butterfly: Buy ITM + Buy OTM - Sell 2x ATM
        debit_per_share = self.itm_buy_cost + self.otm_buy_cost - self.atm_sell_credit

        # Per lot values
        self.net_debit = debit_per_share * self.lot_size

        # Max profit = wing_distance - net_debit (at ATM strike at expiry)
        # For butterfly: max profit occurs when price is exactly at ATM
        max_profit_per_share = self.wing_distance - debit_per_share
        self.max_profit = max_profit_per_share * self.lot_size

        # Max loss = net debit (if price moves beyond wings)
        self.max_loss = self.net_debit

        # Risk:Reward ratio (lower is better)
        if self.max_profit > 0:
            self.risk_reward = self.max_loss / self.max_profit

        # Breakeven points
        self.breakeven_lower = self.atm_strike - (self.wing_distance - debit_per_share)
        self.breakeven_upper = self.atm_strike + (self.wing_distance - debit_per_share)

        # Total values (all lots)
        self.total_debit = self.net_debit * self.num_lots
        self.total_max_profit = self.max_profit * self.num_lots
        self.total_max_loss = self.max_loss * self.num_lots

        # Capital required = total debit (since it's a debit spread)
        self.capital_required = self.total_debit

        # =====================================================================
        # BREAKEVEN & PROBABILITY ANALYSIS
        # =====================================================================

        # Profit zone width (range where we make profit)
        self.profit_zone_width = self.breakeven_upper - self.breakeven_lower
        self.profit_zone_pct = (self.profit_zone_width / self.underlying_price) * 100

        # Distance from current price to breakevens
        self.distance_to_lower_be = self.underlying_price - self.breakeven_lower
        self.distance_to_upper_be = self.breakeven_upper - self.underlying_price
        self.distance_to_lower_be_pct = (self.distance_to_lower_be / self.underlying_price) * 100
        self.distance_to_upper_be_pct = (self.distance_to_upper_be / self.underlying_price) * 100

        # Expected move estimation (based on ATM straddle premium)
        # ATM straddle premium ≈ expected move by expiry (rough approximation)
        # We only have ATM call/put, so estimate using 2x ATM premium
        atm_premium = self.atm_leg.mid_price if self.atm_leg else 0
        self.expected_move = atm_premium * 2  # Straddle approximation

        # Probability of Profit estimation
        # Simple method: Compare profit zone to expected move
        # If profit zone > expected move, higher POP
        # POP ≈ (profit_zone_width / (2 * expected_move)) * 100, capped at 80%
        if self.expected_move > 0:
            # Probability that price stays within profit zone
            # Using a simplified normal distribution approximation
            # Expected move covers ~1 standard deviation (~68% of moves)
            pop_raw = (self.profit_zone_width / (2 * self.expected_move)) * 68
            self.probability_of_profit = min(pop_raw, 80)  # Cap at 80%
        else:
            self.probability_of_profit = 0

        # Breakeven assessment based on distance and POP
        min_distance_pct = min(self.distance_to_lower_be_pct, self.distance_to_upper_be_pct)

        if self.probability_of_profit >= 50 and min_distance_pct >= 1.0:
            self.breakeven_assessment = "GOOD - High POP, comfortable buffer"
        elif self.probability_of_profit >= 35 and min_distance_pct >= 0.5:
            self.breakeven_assessment = "MODERATE - Reasonable POP, tight buffer"
        elif self.probability_of_profit >= 25:
            self.breakeven_assessment = "FAIR - Lower POP, needs price stability"
        else:
            self.breakeven_assessment = "RISKY - Low POP, price too close to breakeven"


@dataclass
class FilterResult:
    """Result of filter checks."""
    passed: bool
    warnings: List[str]
    failures: List[str]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

@dataclass
class ForwardPriceResult:
    """Result of forward price calculation."""
    spot_price: float
    forward_price: float
    futures_price: float = 0.0
    futures_premium: float = 0.0
    pro_rated_premium: float = 0.0
    method: str = "spot"  # "futures" or "spot"
    futures_symbol: str = ""
    futures_dte: int = 0
    options_dte: int = 0


def get_strike_interval(underlying: str) -> int:
    """Get strike interval for the underlying."""
    return STRIKE_INTERVALS.get(underlying.upper(), STRIKE_INTERVALS['DEFAULT'])


def round_to_strike(price: float, interval: int) -> int:
    """Round price to nearest strike interval."""
    return int(round(price / interval) * interval)


def calculate_forward_price(
    kite,
    underlying: str,
    instrument_type: InstrumentType,
    options_expiry_date: date
) -> ForwardPriceResult:
    """
    Calculate forward price for ATM strike selection.

    For INDICES (NIFTY, BANKNIFTY):
        - Get spot price
        - Get nearest futures LTP
        - Calculate futures premium = futures - spot
        - Pro-rate premium for options expiry: premium * (options_dte / futures_dte)
        - Forward = spot + pro-rated premium

    For STOCKS:
        - Use spot price directly (stock futures less liquid)

    Args:
        kite: Authenticated Kite client
        underlying: Stock or index name
        instrument_type: STOCK or INDEX
        options_expiry_date: Expiry date of the options being analyzed

    Returns:
        ForwardPriceResult with spot, forward, and calculation details
    """
    today = date.today()
    options_dte = max((options_expiry_date - today).days, 1)

    # Get spot price first
    spot_price = 0.0
    if instrument_type == InstrumentType.INDEX:
        if underlying.upper() == 'NIFTY':
            spot_price = kite.get_nifty_spot()
        elif underlying.upper() == 'SENSEX':
            result = kite.ltp(["BSE:SENSEX"])
            spot_price = result.get("BSE:SENSEX", 0)
        elif underlying.upper() == 'BANKNIFTY':
            result = kite.ltp(["NSE:NIFTY BANK"])
            spot_price = result.get("NSE:NIFTY BANK", 0)
    else:
        # Stock
        symbol = f"NSE:{underlying.upper()}"
        result = kite.ltp([symbol])
        spot_price = result.get(symbol, 0)

    if spot_price <= 0:
        raise ValueError(f"Could not fetch spot price for {underlying}")

    # For stocks, just use spot
    if instrument_type == InstrumentType.STOCK:
        return ForwardPriceResult(
            spot_price=spot_price,
            forward_price=spot_price,
            method="spot",
            options_dte=options_dte
        )

    # For indices, try to get futures price
    # Build futures symbol based on underlying
    # Format: NIFTY25JANFUT, BANKNIFTY25JANFUT
    try:
        # Try to get instruments DataFrame from SNAIL for futures lookup
        from src.utils.symbol_builder import get_nearest_futures_contract
        import pandas as pd

        # Load instruments from Kite
        instruments_df = pd.DataFrame(kite.instruments("NFO"))

        futures_contract = get_nearest_futures_contract(instruments_df)

        if futures_contract and underlying.upper() == 'NIFTY':
            # Get futures LTP
            futures_symbol = f"NFO:{futures_contract.tradingsymbol}"
            futures_result = kite.ltp([futures_symbol])
            futures_price = futures_result.get(futures_symbol, 0)

            if futures_price > 0:
                futures_dte = max(futures_contract.dte, 1)

                # Calculate pro-rated premium
                futures_premium = futures_price - spot_price
                pro_rated_premium = futures_premium * (options_dte / futures_dte)
                forward_price = spot_price + pro_rated_premium

                print(f"\n[FORWARD PRICE CALCULATION]")
                print(f"  Spot: {spot_price:.2f}")
                print(f"  Futures ({futures_contract.tradingsymbol}): {futures_price:.2f}")
                print(f"  Futures Premium: {futures_premium:.2f}")
                print(f"  Futures DTE: {futures_dte}, Options DTE: {options_dte}")
                print(f"  Pro-rated Premium: {pro_rated_premium:.2f}")
                print(f"  Forward Price: {forward_price:.2f}")

                return ForwardPriceResult(
                    spot_price=spot_price,
                    forward_price=forward_price,
                    futures_price=futures_price,
                    futures_premium=futures_premium,
                    pro_rated_premium=pro_rated_premium,
                    method="futures",
                    futures_symbol=futures_contract.tradingsymbol,
                    futures_dte=futures_dte,
                    options_dte=options_dte
                )

    except ImportError:
        print("  [INFO] SNAIL symbol_builder not available, using spot price")
    except Exception as e:
        print(f"  [INFO] Futures lookup failed ({e}), using spot price")

    # Fallback to spot
    return ForwardPriceResult(
        spot_price=spot_price,
        forward_price=spot_price,
        method="spot",
        options_dte=options_dte
    )


def calculate_wing_distance(
    atm_premium: float,
    multiplier: float = 1.0,
    round_to: float = 50,
    min_distance: float = 100
) -> float:
    """
    Calculate dynamic wing distance based on ATM premium.

    Formula: Wing_Distance = ATM_Premium * Multiplier

    Args:
        atm_premium: ATM option premium (either CE or PE based on direction)
        multiplier: Multiplier for wing distance (1.0 = equals premium)
        round_to: Round to nearest interval (can be float like 2.5)
        min_distance: Minimum wing distance

    Returns:
        Wing distance in strike points (can be float)
    """
    adjusted = atm_premium * multiplier
    calculated = round(adjusted / round_to) * round_to
    return max(calculated, min_distance)


def load_options_from_csv(
    underlying: str,
    instrument_type: InstrumentType,
    stock_csv: str = 'nse_stocks_options.csv',
    index_csv: str = 'index_options.csv'
) -> List[Dict]:
    """
    Load options for the underlying from CSV.

    Args:
        underlying: Stock or index name
        instrument_type: STOCK or INDEX
        stock_csv: Path to stock options CSV
        index_csv: Path to index options CSV

    Returns:
        List of option dictionaries
    """
    csv_path = index_csv if instrument_type == InstrumentType.INDEX else stock_csv

    # Use SCRIPT_DIR (Helper folder) for CSV files
    full_path = SCRIPT_DIR / csv_path

    if not full_path.exists():
        raise FileNotFoundError(f"Options CSV not found: {full_path}")

    today = date.today()
    options = []

    with open(full_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if instrument_type == InstrumentType.INDEX:
                if row.get('index_name', '').upper() == underlying.upper():
                    options.append({
                        'tradingsymbol': row['tradingsymbol'],
                        'strike': float(row['strike']),
                        'option_type': row['option_type'],
                        'expiry': row['expiry'],
                        'instrument_token': row['instrument_token'],
                        'lot_size': int(row.get('lot_size', DEFAULT_LOT_SIZES.get(underlying, 75))),
                        'dte': int(row.get('dte', 0)),
                    })
            else:
                if row.get('stock_symbol', '').upper() == underlying.upper():
                    # Stock options - calculate DTE from expiry
                    expiry = row.get('option_expiry', '')
                    dte = 0
                    if expiry:
                        try:
                            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
                            dte = (exp_date - today).days
                        except ValueError:
                            pass

                    options.append({
                        'tradingsymbol': row['option_tradingsymbol'],
                        'strike': float(row['option_strike']) if row.get('option_strike') else 0,
                        'option_type': row['option_type'],
                        'expiry': expiry,
                        'instrument_token': row['option_instrument_token'],
                        'lot_size': int(row.get('option_lot_size', 50)) if row.get('option_lot_size') else 50,
                        'dte': dte,
                    })

    return options


def get_instrument_type(underlying: str) -> InstrumentType:
    """Determine if underlying is a stock or index."""
    indices = {'NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY'}
    return InstrumentType.INDEX if underlying.upper() in indices else InstrumentType.STOCK


def check_filters(analysis: ButterflyAnalysis) -> FilterResult:
    """
    Check all filters against the butterfly analysis.

    Returns:
        FilterResult with pass/fail status and messages
    """
    warnings = []
    failures = []

    # DTE check
    min_dte = (FILTERS['dte_min_index']
               if analysis.instrument_type == InstrumentType.INDEX
               else FILTERS['dte_min_stock'])

    if analysis.dte < min_dte:
        failures.append(f"DTE {analysis.dte} < minimum {min_dte} days")

    # Bid-ask spread checks for all legs
    for leg_name, leg in [('ITM', analysis.itm_leg),
                          ('ATM', analysis.atm_leg),
                          ('OTM', analysis.otm_leg)]:
        if leg and leg.spread > FILTERS['bid_ask_spread_max']:
            failures.append(f"{leg_name} spread Rs{leg.spread:.2f} > Rs{FILTERS['bid_ask_spread_max']:.2f}")
        elif leg and leg.spread > FILTERS['bid_ask_spread_max'] * 0.8:
            warnings.append(f"{leg_name} spread Rs{leg.spread:.2f} approaching limit")

        # WIDE/SUSPECT QUOTE flag: the absolute Rs1.50 spread check above can
        # pass a one-sided/crossed/thin book on a cheap option (e.g. Rs1.40
        # width on a Rs2 mid = 70%). Mirrors bcs/spread_monitor.py's
        # leg_quote_reliable() relative gate (post 2026-07-24 NHPC incident,
        # Rs 7,297 loss from acting on an unformed book). Warning only —
        # does not change pass/fail, just surfaces the risk before --execute.
        if leg:
            leg_mid = leg.mid_price if (leg.bid > 0 and leg.ask > 0) else 0
            if (leg.bid <= 0 or leg.ask <= 0 or leg.bid > leg.ask
                    or (leg_mid > 0 and leg.spread > max(0.30, 0.25 * leg_mid))):
                warnings.append(
                    f"{leg_name} WIDE/SUSPECT QUOTE (bid={leg.bid:.2f}, ask={leg.ask:.2f}) "
                    f"— one-sided/crossed/wide book, pricing may be unreliable"
                )

    # OI check for ATM
    if analysis.atm_leg and analysis.atm_leg.oi < FILTERS['oi_atm_min']:
        if analysis.atm_leg.oi < FILTERS['oi_atm_min'] * 0.5:
            failures.append(f"ATM OI {analysis.atm_leg.oi:,} < {FILTERS['oi_atm_min']:,} (very low liquidity)")
        else:
            warnings.append(f"ATM OI {analysis.atm_leg.oi:,} < {FILTERS['oi_atm_min']:,} (low liquidity)")

    # Volume check for ATM
    if analysis.atm_leg and analysis.atm_leg.volume < FILTERS['volume_min']:
        if analysis.atm_leg.volume < FILTERS['volume_min'] * 0.2:
            failures.append(f"ATM Volume {analysis.atm_leg.volume:,} < {FILTERS['volume_min']:,} (very low)")
        else:
            warnings.append(f"ATM Volume {analysis.atm_leg.volume:,} < {FILTERS['volume_min']:,}")

    # R:R check
    if analysis.risk_reward > 3:
        warnings.append(f"R:R ratio {analysis.risk_reward:.2f}:1 is unfavorable (>3:1)")
    elif analysis.risk_reward > 5:
        failures.append(f"R:R ratio {analysis.risk_reward:.2f}:1 is very unfavorable (>5:1)")

    # Debit check (if negative, it's a credit which shouldn't happen for long butterfly)
    if analysis.net_debit <= 0:
        failures.append(f"Invalid pricing: net_debit={analysis.net_debit:.2f} (should be positive)")

    passed = len(failures) == 0
    return FilterResult(passed=passed, warnings=warnings, failures=failures)


# =============================================================================
# KITE API INTEGRATION
# =============================================================================

def get_kite_client():
    """
    Get authenticated Kite client from SNAIL.

    Returns:
        SNAILKiteClient instance
    """
    try:
        from src.api.kite_client import SNAILKiteClient
        from src.utils.config import load_config

        config = load_config()
        client = SNAILKiteClient(config)
        client.ensure_authenticated()
        return client
    except ImportError as e:
        print(f"Error: Could not import SNAIL Kite client: {e}")
        print("Make sure SNAIL folder exists and has dependencies installed")
        sys.exit(1)
    except Exception as e:
        print(f"Error authenticating with Kite: {e}")
        sys.exit(1)


def fetch_underlying_price(kite, underlying: str, instrument_type: InstrumentType) -> float:
    """
    Fetch current price of the underlying.

    Args:
        kite: Kite client
        underlying: Stock or index name
        instrument_type: STOCK or INDEX

    Returns:
        Current LTP
    """
    if instrument_type == InstrumentType.INDEX:
        if underlying.upper() == 'NIFTY':
            return kite.get_nifty_spot()
        elif underlying.upper() == 'SENSEX':
            result = kite.ltp(["BSE:SENSEX"])
            return result.get("BSE:SENSEX", 0)
        elif underlying.upper() == 'BANKNIFTY':
            result = kite.ltp(["NSE:NIFTY BANK"])
            return result.get("NSE:NIFTY BANK", 0)
    else:
        # Stock
        symbol = f"NSE:{underlying.upper()}"
        result = kite.ltp([symbol])
        return result.get(symbol, 0)

    return 0


def get_exchange_for_option(tradingsymbol: str) -> str:
    """
    Determine exchange for an option based on trading symbol.

    SENSEX options are on BFO, everything else on NFO.
    """
    # SENSEX options start with SENSEX
    if tradingsymbol.upper().startswith('SENSEX'):
        return 'BFO'
    return 'NFO'


def fetch_option_quotes(kite, legs: List[OptionLeg], instrument_type: InstrumentType) -> Dict[str, dict]:
    """
    Fetch quotes for all option legs.

    Args:
        kite: Kite client
        legs: List of OptionLeg objects
        instrument_type: STOCK or INDEX

    Returns:
        Dictionary of quotes keyed by trading symbol
    """
    # Build instrument list with correct exchange prefix
    # SENSEX options are on BFO, everything else on NFO
    instruments = []
    exchange_map = {}
    for leg in legs:
        if leg:
            exchange = get_exchange_for_option(leg.tradingsymbol)
            key = f"{exchange}:{leg.tradingsymbol}"
            instruments.append(key)
            exchange_map[leg.tradingsymbol] = exchange

    if not instruments:
        return {}

    quotes = kite.quote(instruments)

    # Update legs with quote data
    for leg in legs:
        if leg:
            exchange = exchange_map.get(leg.tradingsymbol, 'NFO')
            key = f"{exchange}:{leg.tradingsymbol}"
            if key in quotes:
                q = quotes[key]
                leg.bid = q.bid
                leg.ask = q.ask
                leg.ltp = q.ltp
                leg.oi = q.oi
                leg.volume = q.volume

    return quotes


# =============================================================================
# JSON LOGGING
# =============================================================================

LOGS_DIR = SCRIPT_DIR / 'logs'


def ensure_logs_dir():
    """Create logs directory if it doesn't exist."""
    LOGS_DIR.mkdir(exist_ok=True)


def log_analysis_to_json(analysis: 'ButterflyAnalysis', execution_details: Optional[Dict] = None):
    """
    Log analysis results to JSON file.

    Args:
        analysis: ButterflyAnalysis object
        execution_details: Optional execution details if trade was placed
    """
    ensure_logs_dir()
    log_file = LOGS_DIR / 'analyses.json'

    # Build log entry
    entry = {
        'timestamp': datetime.now().isoformat(),
        'underlying': analysis.underlying,
        'direction': analysis.direction.value,
        'instrument_type': analysis.instrument_type.value,
        'expiry': analysis.expiry,
        'dte': analysis.dte,
        'underlying_price': analysis.underlying_price,
        'strikes': {
            'itm': analysis.itm_strike,
            'atm': analysis.atm_strike,
            'otm': analysis.otm_strike
        },
        'wing_distance': analysis.wing_distance,
        'lot_size': analysis.lot_size,
        'num_lots': analysis.num_lots,
        'pricing': {
            'itm_ask': analysis.itm_buy_cost,
            'atm_bid_x2': analysis.atm_sell_credit,
            'otm_ask': analysis.otm_buy_cost,
            'net_debit': analysis.net_debit
        },
        'metrics': {
            'max_profit': analysis.max_profit,
            'max_loss': analysis.max_loss,
            'risk_reward': analysis.risk_reward,
            'breakeven_lower': analysis.breakeven_lower,
            'breakeven_upper': analysis.breakeven_upper
        },
        'probability': {
            'profit_zone_width': analysis.profit_zone_width,
            'profit_zone_pct': analysis.profit_zone_pct,
            'expected_move': analysis.expected_move,
            'distance_to_lower_be': analysis.distance_to_lower_be,
            'distance_to_lower_be_pct': analysis.distance_to_lower_be_pct,
            'distance_to_upper_be': analysis.distance_to_upper_be,
            'distance_to_upper_be_pct': analysis.distance_to_upper_be_pct,
            'probability_of_profit': analysis.probability_of_profit,
            'assessment': analysis.breakeven_assessment
        },
        'totals': {
            'total_debit': analysis.total_debit,
            'total_max_profit': analysis.total_max_profit,
            'total_max_loss': analysis.total_max_loss,
            'capital_required': analysis.capital_required
        },
        'filters': {
            'passed': analysis.filters_passed,
            'warnings': analysis.filter_warnings,
            'failures': analysis.filter_failures
        },
        'recommendation': analysis.recommendation,
        'executed': execution_details is not None,
        'execution_details': execution_details
    }

    # Load existing entries or start fresh
    entries = []
    if log_file.exists():
        try:
            with open(log_file, 'r') as f:
                entries = json.load(f)
        except (json.JSONDecodeError, IOError):
            entries = []

    # Append new entry
    entries.append(entry)

    # Write back
    with open(log_file, 'w') as f:
        json.dump(entries, f, indent=2, default=str)

    print(f"\nAnalysis logged to: {log_file}")


# =============================================================================
# ORDER EXECUTION
# =============================================================================

@dataclass
class ExecutionResult:
    """Result of butterfly execution."""
    success: bool
    orders: List[Dict] = field(default_factory=list)
    total_debit_actual: float = 0.0
    slippage_total: float = 0.0
    error: Optional[str] = None
    partial: bool = False
    legs_executed: List[str] = field(default_factory=list)
    legs_failed: List[str] = field(default_factory=list)


def execute_butterfly(
    kite,
    analysis: 'ButterflyAnalysis',
    slippage_ticks: int = 2
) -> ExecutionResult:
    """
    Execute butterfly trade with margin-optimized order sequence.

    Order Sequence (for margin benefit):
    1. BUY ITM (long) - buy longs first
    2. BUY OTM (long) - buy longs first
    3. SELL 2x ATM (short) - short after longs are in place

    Args:
        kite: Authenticated Kite client
        analysis: ButterflyAnalysis with all leg details
        slippage_ticks: Slippage tolerance in ticks (0.05 per tick)

    Returns:
        ExecutionResult with order details
    """
    result = ExecutionResult(success=False)
    tick_size = 0.05

    # Verify filters passed
    if not analysis.filters_passed:
        result.error = f"Cannot execute: filters failed - {analysis.filter_failures}"
        return result

    # Build order sequence: LONGS first, then SHORT
    # This is critical for margin optimization
    orders_to_place = [
        {
            'leg': 'ITM',
            'tradingsymbol': analysis.itm_leg.tradingsymbol,
            'transaction_type': 'BUY',
            'quantity': analysis.lot_size * analysis.num_lots,
            'base_price': analysis.itm_leg.ask,
            'price': analysis.itm_leg.ask + (slippage_ticks * tick_size),  # Add slippage for buy
        },
        {
            'leg': 'OTM',
            'tradingsymbol': analysis.otm_leg.tradingsymbol,
            'transaction_type': 'BUY',
            'quantity': analysis.lot_size * analysis.num_lots,
            'base_price': analysis.otm_leg.ask,
            'price': analysis.otm_leg.ask + (slippage_ticks * tick_size),  # Add slippage for buy
        },
        {
            'leg': 'ATM',
            'tradingsymbol': analysis.atm_leg.tradingsymbol,
            'transaction_type': 'SELL',
            'quantity': analysis.lot_size * analysis.num_lots * 2,  # 2x for butterfly
            'base_price': analysis.atm_leg.bid,
            'price': analysis.atm_leg.bid - (slippage_ticks * tick_size),  # Subtract slippage for sell
        },
    ]

    print("\n" + "=" * 50)
    print("EXECUTING BUTTERFLY (Margin-Optimized Sequence)")
    print("=" * 50)
    print("Order: BUY ITM -> BUY OTM -> SELL 2x ATM")
    print("-" * 50)

    executed_orders = []
    total_cost = 0.0

    for order_params in orders_to_place:
        leg = order_params['leg']
        print(f"\n[{leg}] {order_params['transaction_type']} {order_params['quantity']} "
              f"{order_params['tradingsymbol']} @ {order_params['price']:.2f}")

        try:
            # Place order via Kite
            order_id = kite.place_order(
                tradingsymbol=order_params['tradingsymbol'],
                transaction_type=order_params['transaction_type'],
                quantity=order_params['quantity'],
                price=round(order_params['price'] / tick_size) * tick_size,  # Round to tick
                order_type='LIMIT'
            )

            # Get order status (for fill price)
            import time
            time.sleep(1)  # Wait for order to process
            order_status = kite.get_order_status(order_id)

            fill_price = order_status.get('average_price', order_params['price'])
            slippage = abs(fill_price - order_params['base_price'])

            order_result = {
                'leg': leg,
                'order_id': order_id,
                'tradingsymbol': order_params['tradingsymbol'],
                'transaction_type': order_params['transaction_type'],
                'quantity': order_params['quantity'],
                'order_price': order_params['price'],
                'fill_price': fill_price,
                'slippage': slippage,
                'status': order_status.get('status', 'UNKNOWN')
            }

            executed_orders.append(order_result)
            result.legs_executed.append(leg)

            # Calculate cost contribution
            if order_params['transaction_type'] == 'BUY':
                total_cost += fill_price * order_params['quantity']
            else:
                total_cost -= fill_price * order_params['quantity']

            result.slippage_total += slippage

            print(f"    Order ID: {order_id}")
            print(f"    Fill: {fill_price:.2f} (slippage: {slippage:.2f})")
            print(f"    Status: {order_status.get('status', 'UNKNOWN')}")

        except Exception as e:
            error_msg = f"Order failed for {leg}: {str(e)}"
            print(f"    ERROR: {error_msg}")
            result.legs_failed.append(leg)
            result.error = error_msg

            # CRITICAL: Stop on failure - don't leave partial positions
            if executed_orders:
                result.partial = True
                print("\n*** PARTIAL EXECUTION - MANUAL INTERVENTION REQUIRED ***")
                print(f"Executed legs: {result.legs_executed}")
                print(f"Failed legs: {result.legs_failed}")

            result.orders = executed_orders
            return result

    # All orders successful
    result.success = True
    result.orders = executed_orders
    result.total_debit_actual = total_cost

    print("\n" + "-" * 50)
    print(f"EXECUTION COMPLETE")
    print(f"Total Debit (Actual): Rs {total_cost:,.2f}")
    print(f"Total Slippage: Rs {result.slippage_total:.2f}")
    print("=" * 50)

    return result


# =============================================================================
# MAIN ANALYSIS FUNCTION
# =============================================================================

def analyze_butterfly(
    underlying: str,
    direction: Direction,
    multiplier: float = 1.0,
    num_lots: int = 1,
    kite=None
) -> ButterflyAnalysis:
    """
    Analyze a long butterfly trade.

    Args:
        underlying: Stock or index name (e.g., RELIANCE, NIFTY)
        direction: BUY (bullish call butterfly) or SELL (bearish put butterfly)
        multiplier: Wing distance multiplier (1.0 = ATM premium)
        num_lots: Number of lots to trade
        kite: Optional pre-authenticated Kite client

    Returns:
        ButterflyAnalysis with complete trade analysis
    """
    # Determine instrument type
    instrument_type = get_instrument_type(underlying)

    # Get Kite client if not provided
    if kite is None:
        kite = get_kite_client()

    # Load options from CSV first to get expiry for forward calculation
    options = load_options_from_csv(underlying, instrument_type)
    if not options:
        raise ValueError(f"No options found for {underlying}")

    # Get strike interval - detect from actual options if possible
    strike_interval = get_strike_interval(underlying)

    # Detect actual strike interval from options data
    all_strikes = sorted(set(o['strike'] for o in options if o['strike'] > 0))
    if len(all_strikes) >= 2:
        # Calculate minimum gap between consecutive strikes
        gaps = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
        detected_interval = min(gaps)
        if detected_interval > 0:
            strike_interval = detected_interval

    # Determine option type based on direction
    # BUY (bullish) -> Call butterfly, SELL (bearish) -> Put butterfly
    opt_type = 'CE' if direction == Direction.BUY else 'PE'

    # Filter options by type and find suitable expiry
    typed_options = [o for o in options if o['option_type'] == opt_type]

    if not typed_options:
        raise ValueError(f"No {opt_type} options found for {underlying}")

    # Get unique expiries sorted
    expiries = sorted(set(o['expiry'] for o in typed_options))

    # Find first expiry meeting DTE requirement
    today = date.today()
    min_dte = (FILTERS['dte_min_index']
               if instrument_type == InstrumentType.INDEX
               else FILTERS['dte_min_stock'])

    selected_expiry = None
    selected_dte = 0
    selected_expiry_date = None

    for exp in expiries:
        try:
            exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
            dte = (exp_date - today).days
            if dte >= min_dte:
                selected_expiry = exp
                selected_dte = dte
                selected_expiry_date = exp_date
                break
        except ValueError:
            continue

    if not selected_expiry:
        # Take nearest available expiry even if below min DTE
        selected_expiry = expiries[0] if expiries else None
        if selected_expiry:
            try:
                exp_date = datetime.strptime(selected_expiry, '%Y-%m-%d').date()
                selected_dte = (exp_date - today).days
                selected_expiry_date = exp_date
            except ValueError:
                selected_dte = 0

    if not selected_expiry:
        raise ValueError(f"No valid expiry found for {underlying}")

    # Calculate forward price (uses futures for indices, spot for stocks)
    # This is used for ATM strike selection
    forward_result = calculate_forward_price(
        kite=kite,
        underlying=underlying,
        instrument_type=instrument_type,
        options_expiry_date=selected_expiry_date
    )

    # Use forward price for ATM calculation, but track spot for P&L display
    underlying_price = forward_result.spot_price  # For display/P&L
    atm_calculation_price = forward_result.forward_price  # For ATM strike selection

    # Calculate ATM strike using FORWARD price
    atm_strike = round_to_strike(atm_calculation_price, strike_interval)

    print(f"\n[ATM STRIKE SELECTION]")
    print(f"  Method: {forward_result.method.upper()}")
    print(f"  Spot Price: {forward_result.spot_price:.2f}")
    if forward_result.method == "futures":
        print(f"  Forward Price: {forward_result.forward_price:.2f}")
    print(f"  ATM Strike: {atm_strike}")

    # Filter to selected expiry
    expiry_options = [o for o in typed_options if o['expiry'] == selected_expiry]

    # Find ATM option - pick closest strike to calculated ATM
    if not expiry_options:
        raise ValueError(f"No {opt_type} options found for expiry {selected_expiry}")

    # Sort by distance from ATM strike and pick closest
    sorted_by_distance = sorted(expiry_options, key=lambda o: abs(o['strike'] - atm_strike))
    atm_opt = sorted_by_distance[0]

    # Update ATM strike to actual available strike
    atm_strike = atm_opt['strike']
    lot_size = atm_opt.get('lot_size', DEFAULT_LOT_SIZES.get(underlying.upper(), 50))

    # Create ATM leg to fetch premium
    atm_leg = OptionLeg(
        tradingsymbol=atm_opt['tradingsymbol'],
        strike=atm_opt['strike'],
        option_type=atm_opt['option_type'],
        expiry=atm_opt['expiry'],
        instrument_token=atm_opt['instrument_token'],
        lot_size=lot_size
    )

    # Fetch ATM quote to determine wing distance
    fetch_option_quotes(kite, [atm_leg], instrument_type)

    # Calculate wing distance based on ATM premium
    atm_premium = atm_leg.mid_price if atm_leg.mid_price > 0 else atm_leg.ltp
    # Min distance = at least 2 strike intervals, but minimum 5 for meaningful spread
    min_wing = max(strike_interval * 2, 5)
    wing_distance = calculate_wing_distance(
        atm_premium,
        multiplier=multiplier,
        round_to=strike_interval,
        min_distance=min_wing
    )

    # Get available strikes to validate wing distance
    available_strikes = sorted(set(o['strike'] for o in expiry_options))

    # Find max distance we can go from ATM within available strikes
    strikes_below = [s for s in available_strikes if s < atm_strike]
    strikes_above = [s for s in available_strikes if s > atm_strike]

    if strikes_below and strikes_above:
        max_below = atm_strike - min(strikes_below)
        max_above = max(strikes_above) - atm_strike
        max_available_distance = min(max_below, max_above)

        # Cap wing distance to available range
        if wing_distance > max_available_distance:
            wing_distance = max_available_distance
            # Round to strike interval
            wing_distance = round(wing_distance / strike_interval) * strike_interval
            if wing_distance < strike_interval:
                wing_distance = strike_interval

    # Calculate ITM and OTM strikes
    if direction == Direction.BUY:
        # Call butterfly: ITM (lower) -> ATM -> OTM (higher)
        itm_strike = atm_strike - wing_distance
        otm_strike = atm_strike + wing_distance
    else:
        # Put butterfly: OTM (lower) -> ATM -> ITM (higher)
        # For puts: lower strike = OTM, higher strike = ITM
        otm_strike = atm_strike - wing_distance
        itm_strike = atm_strike + wing_distance

    # Find nearest available strikes if exact strikes don't exist
    def find_nearest_strike(target, strikes_list):
        if not strikes_list:
            return None
        return min(strikes_list, key=lambda s: abs(s - target))

    itm_strike = find_nearest_strike(itm_strike, available_strikes)
    otm_strike = find_nearest_strike(otm_strike, available_strikes)

    # Recalculate wing distance based on actual strikes found
    if itm_strike and otm_strike:
        wing_distance = min(abs(atm_strike - itm_strike), abs(otm_strike - atm_strike))

    # Find ITM and OTM options
    itm_opts = [o for o in expiry_options if o['strike'] == itm_strike]
    otm_opts = [o for o in expiry_options if o['strike'] == otm_strike]

    # Create analysis object
    analysis = ButterflyAnalysis(
        underlying=underlying.upper(),
        underlying_price=underlying_price,
        direction=direction,
        instrument_type=instrument_type,
        expiry=selected_expiry,
        dte=selected_dte,
        atm_strike=atm_strike,
        itm_strike=itm_strike,
        otm_strike=otm_strike,
        wing_distance=wing_distance,
        lot_size=lot_size,
        num_lots=num_lots,
    )

    # Check if strikes exist
    if not itm_opts:
        analysis.filter_failures.append(f"ITM strike {itm_strike} not available")
        analysis.filters_passed = False

    if not otm_opts:
        analysis.filter_failures.append(f"OTM strike {otm_strike} not available")
        analysis.filters_passed = False

    if not analysis.filters_passed:
        analysis.recommendation = "ABORT: Required strikes not available"
        return analysis

    # Create leg objects
    analysis.atm_leg = atm_leg

    analysis.itm_leg = OptionLeg(
        tradingsymbol=itm_opts[0]['tradingsymbol'],
        strike=itm_opts[0]['strike'],
        option_type=itm_opts[0]['option_type'],
        expiry=itm_opts[0]['expiry'],
        instrument_token=itm_opts[0]['instrument_token'],
        lot_size=lot_size
    )

    analysis.otm_leg = OptionLeg(
        tradingsymbol=otm_opts[0]['tradingsymbol'],
        strike=otm_opts[0]['strike'],
        option_type=otm_opts[0]['option_type'],
        expiry=otm_opts[0]['expiry'],
        instrument_token=otm_opts[0]['instrument_token'],
        lot_size=lot_size
    )

    # Fetch quotes for all legs
    fetch_option_quotes(kite, [analysis.itm_leg, analysis.atm_leg, analysis.otm_leg], instrument_type)

    # Calculate metrics
    analysis.calculate_metrics()

    # Run filters
    filter_result = check_filters(analysis)
    analysis.filters_passed = filter_result.passed
    analysis.filter_warnings = filter_result.warnings
    analysis.filter_failures = filter_result.failures

    # Generate recommendation
    if not filter_result.passed:
        analysis.recommendation = "NOT RECOMMENDED: " + "; ".join(filter_result.failures)
    elif filter_result.warnings:
        analysis.recommendation = "PROCEED WITH CAUTION: " + "; ".join(filter_result.warnings)
    else:
        analysis.recommendation = "RECOMMENDED: All filters passed"

    return analysis


# =============================================================================
# OUTPUT FORMATTING
# =============================================================================

def print_analysis(analysis: ButterflyAnalysis):
    """Print analysis in a formatted table."""

    print("\n" + "=" * 70)
    print(f"BUTTERFLY TRADE ANALYSIS: {analysis.underlying}")
    print("=" * 70)

    # Basic info
    print(f"\nUnderlying:      {analysis.underlying} ({analysis.instrument_type.value})")
    print(f"Current Price:   Rs{analysis.underlying_price:,.2f}")
    print(f"Direction:       {analysis.direction.value} ({'Call' if analysis.direction == Direction.BUY else 'Put'} Butterfly)")

    # Format expiry with month name
    try:
        from datetime import datetime
        exp_date = datetime.strptime(analysis.expiry, '%Y-%m-%d')
        expiry_formatted = exp_date.strftime('%d-%b-%Y').upper()  # e.g., 27-JAN-2026
        expiry_month = exp_date.strftime('%B %Y')  # e.g., January 2026
    except:
        expiry_formatted = analysis.expiry
        expiry_month = ""

    print(f"Expiry:          {expiry_formatted} ({analysis.dte} DTE) - {expiry_month}")

    # Strikes
    print(f"\n--- STRIKES ---")
    print(f"ITM Strike:      {analysis.itm_strike}")
    print(f"ATM Strike:      {analysis.atm_strike} (sell 2x)")
    print(f"OTM Strike:      {analysis.otm_strike}")
    print(f"Wing Distance:   {analysis.wing_distance}")

    # Legs with bid-ask
    print(f"\n--- LEGS (using Bid-Ask) ---")
    print(f"{'Leg':<10} {'Symbol':<25} {'Bid':>10} {'Ask':>10} {'Spread':>10} {'OI':>12}")
    print("-" * 77)

    for leg_name, leg, action in [
        ('ITM', analysis.itm_leg, 'BUY'),
        ('ATM', analysis.atm_leg, 'SELL x2'),
        ('OTM', analysis.otm_leg, 'BUY')
    ]:
        if leg:
            spread_flag = '*' if leg.spread > FILTERS['bid_ask_spread_max'] else ''
            print(f"{leg_name:<10} {leg.tradingsymbol:<25} {leg.bid:>10.2f} {leg.ask:>10.2f} "
                  f"{leg.spread:>9.2f}{spread_flag} {leg.oi:>12,}")

    # Pricing
    print(f"\n--- PRICING (per lot of {analysis.lot_size}) ---")
    print(f"ITM Buy Cost:    Rs{analysis.itm_buy_cost:,.2f} (ask)")
    print(f"ATM Sell Credit: Rs{analysis.atm_sell_credit:,.2f} (bid x 2)")
    print(f"OTM Buy Cost:    Rs{analysis.otm_buy_cost:,.2f} (ask)")
    print(f"Net Debit:       Rs{analysis.net_debit:,.2f}")

    # Metrics
    print(f"\n--- P&L METRICS (per lot) ---")
    print(f"Max Profit:      Rs{analysis.max_profit:,.2f} (at ATM strike)")
    print(f"Max Loss:        Rs{analysis.max_loss:,.2f} (beyond wings)")
    print(f"Risk:Reward:     {analysis.risk_reward:.2f}:1")

    # Breakeven & Probability Analysis
    print(f"\n--- BREAKEVEN & PROBABILITY ---")
    print(f"Breakeven Range: Rs{analysis.breakeven_lower:,.2f} - Rs{analysis.breakeven_upper:,.2f}")
    print(f"Profit Zone:     {analysis.profit_zone_width:,.2f} pts ({analysis.profit_zone_pct:.2f}% of price)")
    print(f"Expected Move:   {analysis.expected_move:,.2f} pts (based on ATM premium)")
    print(f"")
    print(f"Distance to Lower BE: {analysis.distance_to_lower_be:,.2f} pts ({analysis.distance_to_lower_be_pct:.2f}%)")
    print(f"Distance to Upper BE: {analysis.distance_to_upper_be:,.2f} pts ({analysis.distance_to_upper_be_pct:.2f}%)")
    print(f"")
    print(f"Probability of Profit: {analysis.probability_of_profit:.1f}%")
    print(f"Assessment: {analysis.breakeven_assessment}")

    # Total values
    print(f"\n--- TOTAL ({analysis.num_lots} lot{'s' if analysis.num_lots > 1 else ''}) ---")
    print(f"Total Debit:     Rs{analysis.total_debit:,.2f}")
    print(f"Total Max Profit:Rs{analysis.total_max_profit:,.2f}")
    print(f"Total Max Loss:  Rs{analysis.total_max_loss:,.2f}")
    print(f"Capital Required:Rs{analysis.capital_required:,.2f}")

    # Filter results
    print(f"\n--- FILTER RESULTS ---")
    status = "PASSED" if analysis.filters_passed else "FAILED"
    print(f"Status: {status}")

    if analysis.filter_warnings:
        print("\nWarnings:")
        for w in analysis.filter_warnings:
            print(f"  - {w}")

    if analysis.filter_failures:
        print("\nFailures:")
        for f in analysis.filter_failures:
            print(f"  - {f}")

    # Recommendation
    print(f"\n--- RECOMMENDATION ---")
    print(analysis.recommendation)

    print("\n" + "=" * 70)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Analyze and execute Long Butterfly trades for stocks and indices',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python butterfly_analyzer.py RELIANCE BUY
  python butterfly_analyzer.py NIFTY SELL --multiplier 1.2
  python butterfly_analyzer.py INFY BUY --lots 2
  python butterfly_analyzer.py NIFTY BUY --execute  # Place actual orders

Filters:
  - Bid-Ask Spread: < Rs{:.2f}/leg
  - Open Interest:  > {:,} contracts (ATM)
  - Daily Volume:   > {:,} options
  - DTE (Stock):    >= {} days
  - DTE (Index):    >= {} days

Execution Order (Margin Optimized):
  1. BUY ITM (long)   - buy longs first
  2. BUY OTM (long)   - buy longs first
  3. SELL 2x ATM      - short after longs
""".format(
            FILTERS['bid_ask_spread_max'],
            FILTERS['oi_atm_min'],
            FILTERS['volume_min'],
            FILTERS['dte_min_stock'],
            FILTERS['dte_min_index']
        )
    )

    parser.add_argument('underlying', help='Stock or index name (e.g., RELIANCE, NIFTY, INFY)')
    parser.add_argument('direction', choices=['BUY', 'SELL'],
                        help='BUY for bullish (call butterfly), SELL for bearish (put butterfly)')
    parser.add_argument('--multiplier', '-m', type=float, default=1.0,
                        help='Wing distance multiplier (default: 1.0 = ATM premium)')
    parser.add_argument('--lots', '-l', type=int, default=1,
                        help='Number of lots (default: 1)')
    parser.add_argument('--execute', '-x', action='store_true',
                        help='Execute the trade (place actual orders via Kite)')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Skip confirmation prompt (use with --execute)')
    parser.add_argument('--slippage', '-s', type=int, default=2,
                        help='Slippage ticks for execution (default: 2)')

    args = parser.parse_args()

    try:
        direction = Direction.BUY if args.direction.upper() == 'BUY' else Direction.SELL

        print(f"\nAnalyzing {args.underlying.upper()} {direction.value} butterfly...")
        print(f"Wing multiplier: {args.multiplier}x, Lots: {args.lots}")
        if args.execute:
            print("*** EXECUTION MODE - Orders will be placed ***")

        analysis = analyze_butterfly(
            underlying=args.underlying,
            direction=direction,
            multiplier=args.multiplier,
            num_lots=args.lots
        )

        print_analysis(analysis)

        execution_details = None

        # Execute if requested and filters passed
        if args.execute:
            if not analysis.filters_passed:
                print("\n*** EXECUTION BLOCKED: Filters failed ***")
                print("Cannot execute trade with failed filters.")
            else:
                # Confirm before execution (skip if --yes flag)
                if args.yes:
                    confirm = 'YES'
                    print("\n*** Auto-confirmed with --yes flag ***")
                else:
                    print("\n" + "=" * 50)
                    print("CONFIRM EXECUTION")
                    print("=" * 50)
                    print(f"You are about to place REAL ORDERS for:")
                    print(f"  {analysis.underlying} {analysis.direction.value} Butterfly")
                    print(f"  Lots: {analysis.num_lots}")
                    print(f"  Capital Required: Rs {analysis.capital_required:,.2f}")
                    print()
                    confirm = input("Type 'YES' to confirm execution: ").strip()

                if confirm == 'YES':
                    print("\nExecuting trade...")
                    kite = get_kite_client()

                    exec_result = execute_butterfly(
                        kite=kite,
                        analysis=analysis,
                        slippage_ticks=args.slippage
                    )

                    if exec_result.success:
                        execution_details = {
                            'executed_at': datetime.now().isoformat(),
                            'orders': exec_result.orders,
                            'total_debit_actual': exec_result.total_debit_actual,
                            'slippage_total': exec_result.slippage_total,
                            'status': 'COMPLETE'
                        }
                        print("\nTrade executed successfully!")
                    elif exec_result.partial:
                        execution_details = {
                            'executed_at': datetime.now().isoformat(),
                            'orders': exec_result.orders,
                            'total_debit_actual': exec_result.total_debit_actual,
                            'slippage_total': exec_result.slippage_total,
                            'status': 'PARTIAL',
                            'error': exec_result.error,
                            'legs_executed': exec_result.legs_executed,
                            'legs_failed': exec_result.legs_failed
                        }
                        print("\n*** PARTIAL EXECUTION - MANUAL INTERVENTION REQUIRED ***")
                    else:
                        execution_details = {
                            'executed_at': datetime.now().isoformat(),
                            'status': 'FAILED',
                            'error': exec_result.error
                        }
                        print(f"\nExecution failed: {exec_result.error}")
                else:
                    print("\nExecution cancelled by user.")

        # Log analysis to JSON (always log, regardless of execution)
        log_analysis_to_json(analysis, execution_details)

        # Exit with error code if filters failed
        sys.exit(0 if analysis.filters_passed else 1)

    except KeyboardInterrupt:
        print("\nAborted by user")
        sys.exit(130)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
