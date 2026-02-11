"""
Iron Condor Strategy for Expiry Day Trading

VIX-based wing calculation and strike selection.

@file        strategy.py
@description Iron condor strategy logic for 0DTE expiry trading
"""

import pandas as pd
from datetime import date
from pathlib import Path
from typing import Dict, Optional, Tuple, Any
from dataclasses import dataclass
from loguru import logger


@dataclass
class Strike:
    """Option strike details."""
    strike: float
    symbol: str
    instrument_token: int
    option_type: str  # CE or PE
    lot_size: int
    expiry: date


@dataclass
class IronCondorSetup:
    """Complete iron condor setup."""
    spot: float
    vix: float
    wing_distance: int
    atm_strike: float
    expiry: date
    lot_size: int
    spread_width: int  # Distance between short and long strikes

    # Strikes
    short_ce: Strike
    short_pe: Strike
    long_ce: Strike
    long_pe: Strike

    # Premiums (from bid/ask)
    short_ce_premium: float = 0.0
    short_pe_premium: float = 0.0
    long_ce_premium: float = 0.0
    long_pe_premium: float = 0.0

    @property
    def total_credit(self) -> float:
        """Total credit received per share."""
        return (
            self.short_ce_premium + self.short_pe_premium -
            self.long_ce_premium - self.long_pe_premium
        )

    @property
    def max_profit(self) -> float:
        """Max profit = total credit."""
        return self.total_credit

    @property
    def max_loss(self) -> float:
        """Max loss per share = spread width - credit received."""
        return self.spread_width - self.total_credit


class ExpiryChecker:
    """Check if today is expiry day using instruments file."""

    def __init__(self, instruments_path: Path, instrument_name: str = "NIFTY"):
        """
        Initialize expiry checker.

        Args:
            instruments_path: Path to instruments.csv
            instrument_name: Instrument to check (NIFTY, SENSEX, etc.)
        """
        self.instruments_path = Path(instruments_path)
        self.instrument_name = instrument_name
        self._df: Optional[pd.DataFrame] = None

    def load_instruments(self) -> pd.DataFrame:
        """Load instruments file."""
        if self._df is None:
            self._df = pd.read_csv(self.instruments_path)
            # Parse expiry dates
            self._df['expiry'] = pd.to_datetime(self._df['expiry']).dt.date
            logger.debug(f"Loaded {len(self._df)} instruments")
        return self._df

    def is_expiry_today(self) -> Tuple[bool, Optional[date]]:
        """
        Check if today is expiry day.

        Returns:
            Tuple of (is_expiry, expiry_date)
        """
        df = self.load_instruments()
        today = date.today()

        # Filter for our instrument's options
        options = df[
            (df['name'] == self.instrument_name) &
            (df['instrument_type'].isin(['CE', 'PE']))
        ]

        if options.empty:
            logger.warning(f"No options found for {self.instrument_name}")
            return False, None

        # Get unique expiry dates
        expiries = sorted(options['expiry'].unique())

        if not expiries:
            return False, None

        # Check if today matches any expiry
        for exp in expiries:
            if exp == today:
                logger.info(f"Today {today} is expiry day!")
                return True, exp

        # Find nearest expiry for logging
        future_expiries = [e for e in expiries if e >= today]
        if future_expiries:
            nearest = min(future_expiries)
            logger.info(f"Today {today} is NOT expiry. Nearest: {nearest}")
            return False, nearest

        return False, None

    def get_expiry_date(self) -> Optional[date]:
        """Get today's expiry date if it's expiry day."""
        is_expiry, exp_date = self.is_expiry_today()
        return exp_date if is_expiry else None

    def get_lot_size(self, expiry_date: Optional[date] = None) -> int:
        """
        Get lot size from instruments file.

        Args:
            expiry_date: Specific expiry date (uses nearest if None)

        Returns:
            Lot size for the instrument
        """
        df = self.load_instruments()

        # Filter for our instrument's options
        options = df[
            (df['name'] == self.instrument_name) &
            (df['instrument_type'].isin(['CE', 'PE']))
        ]

        if expiry_date:
            options = options[options['expiry'] == expiry_date]

        if options.empty:
            from src.state_manager import NIFTY_LOT_SIZE
            logger.warning(f"No options found, using default lot size {NIFTY_LOT_SIZE}")
            return NIFTY_LOT_SIZE

        # Get lot size from first matching option
        lot_size = int(options.iloc[0]['lot_size'])
        logger.debug(f"Lot size for {self.instrument_name}: {lot_size}")
        return lot_size

    def get_option_chain(self, expiry_date: date) -> pd.DataFrame:
        """
        Get option chain for specific expiry.

        Args:
            expiry_date: Expiry date

        Returns:
            DataFrame with options for the expiry
        """
        df = self.load_instruments()

        options = df[
            (df['name'] == self.instrument_name) &
            (df['instrument_type'].isin(['CE', 'PE'])) &
            (df['expiry'] == expiry_date)
        ]

        return options.sort_values('strike')


class StrategyCalculator:
    """Calculate iron condor strategy parameters."""

    # VIX to daily volatility conversion factor
    VIX_DAILY_FACTOR = 15.87  # sqrt(252)

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize strategy calculator.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.vix_multipliers = config.get('vix_multipliers', {})
        self.strategy_config = config.get('strategy', {})
        self.min_wing = self.strategy_config.get('min_wing_distance', 100)
        self.wing_rounding = self.strategy_config.get('wing_rounding', 50)
        self.spread_width = self.strategy_config.get('spread_width', 50)

    def get_vix_multiplier(self, vix: float) -> float:
        """
        Get wing multiplier based on VIX level.

        Args:
            vix: Current VIX value

        Returns:
            Multiplier for wing distance
        """
        if vix < 11:
            return self.vix_multipliers.get('below_11', 0.90)
        elif vix < 13:
            return self.vix_multipliers.get('11_to_13', 1.00)
        elif vix < 15:
            return self.vix_multipliers.get('13_to_15', 1.10)
        elif vix < 18:
            return self.vix_multipliers.get('15_to_18', 1.25)
        elif vix < 22:
            return self.vix_multipliers.get('18_to_22', 1.40)
        else:
            return self.vix_multipliers.get('above_22', 1.60)

    def calculate_expected_move(self, spot: float, vix: float) -> float:
        """
        Calculate expected daily move from VIX.

        Formula: Expected Move = Spot × (VIX% / 100) / sqrt(252)

        VIX is annualized volatility in percentage terms.
        Divide by sqrt(252) to get daily volatility.

        Args:
            spot: Current spot price
            vix: Current VIX value (percentage, e.g., 9.59 for 9.59%)

        Returns:
            Expected move in points
        """
        return spot * (vix / 100) / self.VIX_DAILY_FACTOR

    def calculate_wing_distance(self, spot: float, vix: float) -> int:
        """
        Calculate wing distance based on VIX.

        Wing = Expected Move × Multiplier, rounded to 50

        Args:
            spot: Current spot price
            vix: Current VIX value

        Returns:
            Wing distance in points
        """
        expected_move = self.calculate_expected_move(spot, vix)
        multiplier = self.get_vix_multiplier(vix)

        wing = expected_move * multiplier
        wing = round(wing / self.wing_rounding) * self.wing_rounding
        wing = max(wing, self.min_wing)

        logger.info(
            f"Wing calculation: Expected={expected_move:.0f}, "
            f"Multiplier={multiplier}x, Wing={wing}"
        )

        return int(wing)

    def find_atm_strike(self, spot: float, strike_interval: int = 50) -> float:
        """
        Find ATM strike nearest to spot.

        Args:
            spot: Current spot price
            strike_interval: Strike interval (50 for NIFTY)

        Returns:
            ATM strike price
        """
        return round(spot / strike_interval) * strike_interval

    def build_iron_condor(
        self,
        spot: float,
        vix: float,
        option_chain: pd.DataFrame,
        expiry_date: date
    ) -> Optional[IronCondorSetup]:
        """
        Build iron condor setup from market data.

        Iron Condor Structure:
        - Short CE: ATM + wing_distance (OTM call sold)
        - Short PE: ATM - wing_distance (OTM put sold)
        - Long CE: Short CE + spread_width (further OTM call bought)
        - Long PE: Short PE - spread_width (further OTM put bought)

        Args:
            spot: Current spot price
            vix: Current VIX
            option_chain: DataFrame with available options
            expiry_date: Expiry date

        Returns:
            IronCondorSetup or None if strikes not found
        """
        wing_distance = self.calculate_wing_distance(spot, vix)
        atm_strike = self.find_atm_strike(spot)

        # Calculate short strikes (OTM strangle)
        short_ce_strike = atm_strike + wing_distance
        short_pe_strike = atm_strike - wing_distance

        # Calculate long strikes (further OTM for protection)
        long_ce_strike = short_ce_strike + self.spread_width
        long_pe_strike = short_pe_strike - self.spread_width

        logger.info(
            f"Building IC: ATM={atm_strike}, Wing={wing_distance}, Spread={self.spread_width}"
        )
        logger.info(
            f"Strikes: Short CE={short_ce_strike}, Short PE={short_pe_strike}, "
            f"Long CE={long_ce_strike}, Long PE={long_pe_strike}"
        )

        # Find strikes in option chain
        def find_strike(strike_price: float, opt_type: str) -> Optional[Strike]:
            match = option_chain[
                (option_chain['strike'] == strike_price) &
                (option_chain['instrument_type'] == opt_type)
            ]
            if match.empty:
                logger.warning(f"Strike not found: {strike_price} {opt_type}")
                return None
            row = match.iloc[0]
            return Strike(
                strike=strike_price,
                symbol=row['tradingsymbol'],
                instrument_token=int(row['instrument_token']),
                option_type=opt_type,
                lot_size=int(row['lot_size']),
                expiry=expiry_date
            )

        # Short strangle (OTM options)
        short_ce = find_strike(short_ce_strike, 'CE')
        short_pe = find_strike(short_pe_strike, 'PE')

        # Long wings (further OTM for protection)
        long_ce = find_strike(long_ce_strike, 'CE')
        long_pe = find_strike(long_pe_strike, 'PE')

        # Validate all strikes found
        if short_ce is None or short_pe is None or long_ce is None or long_pe is None:
            logger.error("Could not find all required strikes")
            return None

        lot_size = short_ce.lot_size

        return IronCondorSetup(
            spot=spot,
            vix=vix,
            wing_distance=wing_distance,
            atm_strike=atm_strike,
            expiry=expiry_date,
            lot_size=lot_size,
            spread_width=self.spread_width,
            short_ce=short_ce,
            short_pe=short_pe,
            long_ce=long_ce,
            long_pe=long_pe
        )

    def calculate_target_and_sl(
        self,
        total_credit: float,
        num_lots: int,
        lot_size: int
    ) -> Tuple[float, float]:
        """
        Calculate target and stop loss P&L.

        Target: 50% of premium decay
        Stop Loss: 2x premium collected

        Args:
            total_credit: Credit per share
            num_lots: Number of lots
            lot_size: Lot size

        Returns:
            Tuple of (target_pnl, stop_loss_pnl) in rupees
        """
        total_qty = num_lots * lot_size
        credit_value = total_credit * total_qty

        target_decay_pct = self.strategy_config.get('target_decay_pct', 50)
        sl_multiplier = self.strategy_config.get('stop_loss_multiplier', 2.0)

        target_pnl = credit_value * (target_decay_pct / 100)
        stop_loss_pnl = -credit_value * sl_multiplier

        logger.info(
            f"P&L targets: Credit={credit_value:.0f}, "
            f"Target={target_pnl:.0f}, SL={stop_loss_pnl:.0f}"
        )

        return target_pnl, stop_loss_pnl


def calculate_premium_from_quotes(
    setup: IronCondorSetup,
    quotes: Dict[str, Any]
) -> IronCondorSetup:
    """
    Update setup with actual premiums from quotes.

    Uses bid for SELL legs, ask for BUY legs.

    Args:
        setup: Iron condor setup
        quotes: Quote data from Kite API

    Returns:
        Updated setup with premiums
    """
    def get_quote_key(symbol: str) -> str:
        return f"NFO:{symbol}"

    # Short legs - use BID (what we receive)
    short_ce_key = get_quote_key(setup.short_ce.symbol)
    short_pe_key = get_quote_key(setup.short_pe.symbol)

    if short_ce_key in quotes:
        setup.short_ce_premium = quotes[short_ce_key].bid
    if short_pe_key in quotes:
        setup.short_pe_premium = quotes[short_pe_key].bid

    # Long legs - use ASK (what we pay)
    long_ce_key = get_quote_key(setup.long_ce.symbol)
    long_pe_key = get_quote_key(setup.long_pe.symbol)

    if long_ce_key in quotes:
        setup.long_ce_premium = quotes[long_ce_key].ask
    if long_pe_key in quotes:
        setup.long_pe_premium = quotes[long_pe_key].ask

    logger.info(
        f"Premiums: Short CE={setup.short_ce_premium:.2f}, "
        f"Short PE={setup.short_pe_premium:.2f}, "
        f"Long CE={setup.long_ce_premium:.2f}, "
        f"Long PE={setup.long_pe_premium:.2f}, "
        f"Net Credit={setup.total_credit:.2f}"
    )

    return setup
