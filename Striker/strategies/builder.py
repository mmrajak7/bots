"""
Striker CLI - Strategy Builder

Builds option strategies from option chain data.
Supports: spreads, strangles, straddles, iron condors, butterflies, ratio spreads.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from enum import Enum
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class StrategyType(Enum):
    """Option strategy types."""
    # Single leg
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    SHORT_CALL = "short_call"
    SHORT_PUT = "short_put"

    # Vertical spreads
    BULL_CALL_SPREAD = "bull_call_spread"       # Debit call spread
    BEAR_CALL_SPREAD = "bear_call_spread"       # Credit call spread
    BULL_PUT_SPREAD = "bull_put_spread"         # Credit put spread
    BEAR_PUT_SPREAD = "bear_put_spread"         # Debit put spread

    # Straddles & Strangles
    LONG_STRADDLE = "long_straddle"
    SHORT_STRADDLE = "short_straddle"
    LONG_STRANGLE = "long_strangle"
    SHORT_STRANGLE = "short_strangle"

    # Iron strategies
    IRON_CONDOR = "iron_condor"
    IRON_BUTTERFLY = "iron_butterfly"

    # Butterflies
    LONG_CALL_BUTTERFLY = "long_call_butterfly"
    LONG_PUT_BUTTERFLY = "long_put_butterfly"

    # Ratio spreads
    CALL_RATIO_SPREAD = "call_ratio_spread"
    PUT_RATIO_SPREAD = "put_ratio_spread"


@dataclass
class OptionLeg:
    """Single leg of an option strategy."""
    strike: int
    option_type: str          # 'CE' or 'PE'
    action: str               # 'BUY' or 'SELL'
    quantity: int             # Number of lots
    premium: float            # Premium per unit (bid-ask adjusted)
    trading_symbol: str = ""
    token: Optional[str] = None

    # Bid-ask data for realistic pricing
    bid: float = 0            # Best bid price
    ask: float = 0            # Best ask price
    ltp: float = 0            # Last traded price (reference)
    spread: float = 0         # Bid-ask spread
    spread_pct: float = 0     # Spread as percentage

    # Liquidity metrics
    oi: int = 0               # Open interest
    volume: int = 0           # Day's volume
    liquidity: str = "UNKNOWN"  # HIGH/MEDIUM/LOW/ILLIQUID

    # Greeks
    delta: float = 0
    theta: float = 0

    @property
    def is_long(self) -> bool:
        return self.action == 'BUY'

    @property
    def execution_price(self) -> float:
        """Get realistic execution price based on action.
        BUY at ASK (higher), SELL at BID (lower)."""
        if self.action == 'BUY':
            return self.ask if self.ask > 0 else self.premium
        else:
            return self.bid if self.bid > 0 else self.premium

    @property
    def total_premium(self) -> float:
        """Total premium using execution price (positive for credit, negative for debit)."""
        exec_price = self.execution_price
        if self.action == 'BUY':
            return -exec_price * self.quantity
        else:
            return exec_price * self.quantity

    @property
    def slippage(self) -> float:
        """Estimated slippage from LTP to execution price."""
        if self.ltp > 0:
            return abs(self.execution_price - self.ltp)
        return 0

    @property
    def is_liquid(self) -> bool:
        """Check if leg has reasonable liquidity."""
        return self.liquidity in ['HIGH', 'MEDIUM']


@dataclass
class Strategy:
    """Complete option strategy with analysis."""
    name: str
    strategy_type: StrategyType
    symbol: str
    expiry: datetime
    spot: float
    legs: List[OptionLeg]
    lot_size: int

    # Calculated fields
    net_premium: float = 0          # Net credit (+) or debit (-)
    max_profit: float = 0
    max_loss: float = 0
    breakeven: List[float] = field(default_factory=list)
    risk_reward: float = 0
    dte: int = 0

    # Bid-ask adjusted values
    net_premium_at_ltp: float = 0   # Premium at LTP for comparison
    total_slippage: float = 0       # Expected slippage from LTP to execution
    total_spread_cost: float = 0    # Cost of bid-ask spreads

    # Greeks (net position)
    net_delta: float = 0
    net_theta: float = 0

    # Risk warnings
    liquidity_warnings: List[str] = field(default_factory=list)
    dte_warning: str = ""

    # Position info
    margin_required: float = 0
    charges_estimate: float = 0

    # Timestamp for staleness check
    built_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        """Calculate strategy metrics after initialization."""
        self._calculate_metrics()
        self._calculate_greeks()
        self._check_liquidity()
        self._check_dte()

    def _calculate_metrics(self):
        """Calculate all strategy metrics."""
        # Net premium (total credit - total debit)
        self.net_premium = sum(leg.total_premium for leg in self.legs) * self.lot_size

        # DTE
        self.dte = (self.expiry - datetime.now()).days

        # Calculate max P&L and breakeven based on strategy type
        self._calculate_pnl_profile()

        # Risk-reward ratio
        if self.max_loss != 0 and self.max_loss != float('inf'):
            self.risk_reward = abs(self.max_profit / self.max_loss)
        else:
            self.risk_reward = float('inf') if self.max_profit > 0 else 0

    def _calculate_pnl_profile(self):
        """Calculate max profit, max loss, and breakeven points."""
        st = self.strategy_type

        # Get sorted strikes
        strikes = sorted(set(leg.strike for leg in self.legs))

        if st == StrategyType.BULL_CALL_SPREAD:
            # Buy lower strike call, sell higher strike call
            lower_leg = min(self.legs, key=lambda l: l.strike)
            upper_leg = max(self.legs, key=lambda l: l.strike)
            width = upper_leg.strike - lower_leg.strike

            self.max_loss = abs(self.net_premium)  # Debit paid
            self.max_profit = (width * self.lot_size) - self.max_loss
            self.breakeven = [lower_leg.strike + abs(self.net_premium / self.lot_size)]

        elif st == StrategyType.BEAR_CALL_SPREAD:
            # Sell lower strike call, buy higher strike call
            lower_leg = min(self.legs, key=lambda l: l.strike)
            upper_leg = max(self.legs, key=lambda l: l.strike)
            width = upper_leg.strike - lower_leg.strike

            self.max_profit = self.net_premium  # Credit received
            self.max_loss = (width * self.lot_size) - self.max_profit
            self.breakeven = [lower_leg.strike + (self.net_premium / self.lot_size)]

        elif st == StrategyType.BULL_PUT_SPREAD:
            # Sell higher strike put, buy lower strike put
            lower_leg = min(self.legs, key=lambda l: l.strike)
            upper_leg = max(self.legs, key=lambda l: l.strike)
            width = upper_leg.strike - lower_leg.strike

            self.max_profit = self.net_premium  # Credit received
            self.max_loss = (width * self.lot_size) - self.max_profit
            self.breakeven = [upper_leg.strike - (self.net_premium / self.lot_size)]

        elif st == StrategyType.BEAR_PUT_SPREAD:
            # Buy higher strike put, sell lower strike put
            lower_leg = min(self.legs, key=lambda l: l.strike)
            upper_leg = max(self.legs, key=lambda l: l.strike)
            width = upper_leg.strike - lower_leg.strike

            self.max_loss = abs(self.net_premium)  # Debit paid
            self.max_profit = (width * self.lot_size) - self.max_loss
            self.breakeven = [upper_leg.strike - abs(self.net_premium / self.lot_size)]

        elif st == StrategyType.LONG_STRADDLE:
            # Buy ATM call + ATM put
            atm_strike = self.legs[0].strike
            total_premium = abs(self.net_premium)

            self.max_loss = total_premium
            self.max_profit = float('inf')  # Unlimited
            self.breakeven = [
                atm_strike - (total_premium / self.lot_size),
                atm_strike + (total_premium / self.lot_size)
            ]

        elif st == StrategyType.SHORT_STRADDLE:
            # Sell ATM call + ATM put
            atm_strike = self.legs[0].strike

            self.max_profit = self.net_premium
            self.max_loss = float('inf')  # Unlimited
            self.breakeven = [
                atm_strike - (self.net_premium / self.lot_size),
                atm_strike + (self.net_premium / self.lot_size)
            ]

        elif st == StrategyType.LONG_STRANGLE:
            # Buy OTM call + OTM put
            call_leg = next(l for l in self.legs if l.option_type == 'CE')
            put_leg = next(l for l in self.legs if l.option_type == 'PE')
            total_premium = abs(self.net_premium)

            self.max_loss = total_premium
            self.max_profit = float('inf')
            self.breakeven = [
                put_leg.strike - (total_premium / self.lot_size),
                call_leg.strike + (total_premium / self.lot_size)
            ]

        elif st == StrategyType.SHORT_STRANGLE:
            # Sell OTM call + OTM put
            call_leg = next(l for l in self.legs if l.option_type == 'CE')
            put_leg = next(l for l in self.legs if l.option_type == 'PE')

            self.max_profit = self.net_premium
            self.max_loss = float('inf')
            self.breakeven = [
                put_leg.strike - (self.net_premium / self.lot_size),
                call_leg.strike + (self.net_premium / self.lot_size)
            ]

        elif st == StrategyType.IRON_CONDOR:
            # Sell OTM call spread + Sell OTM put spread
            put_legs = sorted([l for l in self.legs if l.option_type == 'PE'], key=lambda l: l.strike)
            call_legs = sorted([l for l in self.legs if l.option_type == 'CE'], key=lambda l: l.strike)

            # Width of spreads (assuming symmetric)
            put_width = put_legs[1].strike - put_legs[0].strike
            call_width = call_legs[1].strike - call_legs[0].strike
            max_width = max(put_width, call_width)

            self.max_profit = self.net_premium
            self.max_loss = (max_width * self.lot_size) - self.max_profit
            self.breakeven = [
                put_legs[1].strike - (self.net_premium / self.lot_size),
                call_legs[0].strike + (self.net_premium / self.lot_size)
            ]

        elif st == StrategyType.IRON_BUTTERFLY:
            # Sell ATM straddle + Buy OTM strangle
            atm_strike = [l.strike for l in self.legs if l.action == 'SELL'][0]
            otm_call = max(l.strike for l in self.legs if l.option_type == 'CE' and l.action == 'BUY')
            otm_put = min(l.strike for l in self.legs if l.option_type == 'PE' and l.action == 'BUY')

            wing_width = max(otm_call - atm_strike, atm_strike - otm_put)

            self.max_profit = self.net_premium
            self.max_loss = (wing_width * self.lot_size) - self.max_profit
            self.breakeven = [
                atm_strike - (self.net_premium / self.lot_size),
                atm_strike + (self.net_premium / self.lot_size)
            ]

        else:
            # Default calculation for other strategies
            self.max_loss = abs(self.net_premium) if self.net_premium < 0 else 0
            self.max_profit = self.net_premium if self.net_premium > 0 else 0

    def _calculate_greeks(self):
        """Calculate net portfolio Greeks."""
        self.net_delta = 0
        self.net_theta = 0

        for leg in self.legs:
            multiplier = leg.quantity if leg.action == 'BUY' else -leg.quantity
            self.net_delta += leg.delta * multiplier * self.lot_size
            self.net_theta += leg.theta * multiplier * self.lot_size

        self.net_delta = round(self.net_delta, 2)
        self.net_theta = round(self.net_theta, 2)

        # Calculate slippage from LTP
        self.total_slippage = sum(leg.slippage * leg.quantity * self.lot_size for leg in self.legs)
        self.total_spread_cost = sum(leg.spread * leg.quantity * self.lot_size for leg in self.legs) / 2

        # Calculate premium at LTP for comparison
        ltp_premium = 0
        for leg in self.legs:
            if leg.action == 'BUY':
                ltp_premium -= leg.ltp * leg.quantity
            else:
                ltp_premium += leg.ltp * leg.quantity
        self.net_premium_at_ltp = ltp_premium * self.lot_size

    def _check_liquidity(self):
        """Check liquidity of all legs and add warnings."""
        self.liquidity_warnings = []

        for leg in self.legs:
            if leg.liquidity == 'ILLIQUID':
                self.liquidity_warnings.append(
                    f"ILLIQUID: {leg.strike} {leg.option_type} - OI: {leg.oi}, Vol: {leg.volume}, Spread: {leg.spread_pct:.1f}%"
                )
            elif leg.liquidity == 'LOW':
                self.liquidity_warnings.append(
                    f"LOW LIQ: {leg.strike} {leg.option_type} - OI: {leg.oi}, Vol: {leg.volume}, Spread: {leg.spread_pct:.1f}%"
                )
            elif leg.spread_pct > 2:
                self.liquidity_warnings.append(
                    f"WIDE SPREAD: {leg.strike} {leg.option_type} - Spread: {leg.spread_pct:.1f}%"
                )

    def _check_dte(self):
        """Check DTE and add warnings for risky expiries."""
        self.dte_warning = ""

        # Weekly indices
        weekly_indices = {'NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY'}

        if self.dte <= 0:
            self.dte_warning = "EXPIRY DAY - Extreme gamma risk! Consider rolling."
        elif self.dte == 1:
            self.dte_warning = "1 DTE - High gamma, rapid theta decay. Exercise caution."
        elif self.dte <= 3 and self.symbol.upper() not in weekly_indices:
            self.dte_warning = f"LOW DTE ({self.dte} days) for stock options - Gamma risk elevated."
        elif self.dte <= 2 and self.symbol.upper() in weekly_indices:
            # Even for indices, < 2 DTE is risky for credit strategies
            st = self.strategy_type
            if st in [StrategyType.SHORT_STRADDLE, StrategyType.SHORT_STRANGLE,
                      StrategyType.IRON_CONDOR, StrategyType.IRON_BUTTERFLY,
                      StrategyType.BULL_PUT_SPREAD, StrategyType.BEAR_CALL_SPREAD]:
                self.dte_warning = f"LOW DTE ({self.dte} days) - Consider wider strikes or roll."

    @property
    def has_warnings(self) -> bool:
        """Check if strategy has any warnings."""
        return bool(self.liquidity_warnings) or bool(self.dte_warning)

    @property
    def is_stale(self) -> bool:
        """Check if strategy data is stale (> 2 minutes old)."""
        return (datetime.now() - self.built_at).seconds > 120

    def get_summary(self) -> str:
        """Get formatted strategy summary with bid-ask, Greeks, and warnings."""
        lines = []
        lines.append(f"\n{'='*70}")
        lines.append(f"  {self.name}")
        lines.append(f"{'='*70}")
        lines.append(f"  Symbol: {self.symbol}  |  Spot: ₹{self.spot:,.2f}  |  DTE: {self.dte} days")
        lines.append(f"  Expiry: {self.expiry.strftime('%d-%b-%Y')}  |  Lot Size: {self.lot_size}")
        lines.append(f"{'-'*70}")

        # Legs with bid-ask details
        lines.append("  LEGS:")
        lines.append(f"  {'#':<3} {'Action':<5} {'Strike':<8} {'Type':<4} {'Bid':>8} {'Ask':>8} {'Exec':>8} {'Liq':>8}")
        lines.append(f"  {'-'*66}")

        for i, leg in enumerate(self.legs, 1):
            action_str = "BUY" if leg.action == 'BUY' else "SELL"
            exec_price = leg.execution_price
            lines.append(
                f"  {i:<3} {action_str:<5} {leg.strike:<8} {leg.option_type:<4} "
                f"{leg.bid:>8.2f} {leg.ask:>8.2f} {exec_price:>8.2f} {leg.liquidity:>8}"
            )

        lines.append(f"{'-'*70}")

        # P&L Analysis - Bid-Ask Adjusted
        lines.append("  ANALYSIS (Bid-Ask Adjusted):")
        if self.net_premium > 0:
            lines.append(f"    Net Credit:       ₹{self.net_premium:,.2f}")
            if abs(self.net_premium_at_ltp - self.net_premium) > 0.01:
                lines.append(f"    (LTP shows:       ₹{self.net_premium_at_ltp:,.2f})")
        else:
            lines.append(f"    Net Debit:        ₹{abs(self.net_premium):,.2f}")
            if abs(self.net_premium_at_ltp - self.net_premium) > 0.01:
                lines.append(f"    (LTP shows:       ₹{abs(self.net_premium_at_ltp):,.2f})")

        if self.total_slippage > 0:
            lines.append(f"    Est. Slippage:    ₹{self.total_slippage:,.2f}")

        lines.append("")

        if self.max_profit == float('inf'):
            lines.append(f"    Max Profit:       UNLIMITED")
        else:
            lines.append(f"    Max Profit:       ₹{self.max_profit:,.2f}")

        if self.max_loss == float('inf'):
            lines.append(f"    Max Loss:         UNLIMITED")
        else:
            lines.append(f"    Max Loss:         ₹{self.max_loss:,.2f}")

        if self.breakeven:
            be_str = ", ".join([f"₹{be:,.2f}" for be in self.breakeven])
            lines.append(f"    Breakeven:        {be_str}")

        if self.risk_reward != float('inf') and self.risk_reward > 0:
            lines.append(f"    Risk:Reward:      1:{self.risk_reward:.2f}")

        lines.append(f"{'-'*70}")

        # Greeks
        lines.append("  GREEKS:")
        lines.append(f"    Net Delta:        {self.net_delta:+.2f}")
        lines.append(f"    Net Theta:        ₹{self.net_theta:+.2f}/day")

        # Warnings section
        if self.has_warnings:
            lines.append(f"{'-'*70}")
            lines.append("  ⚠ WARNINGS:")
            if self.dte_warning:
                lines.append(f"    {self.dte_warning}")
            for warning in self.liquidity_warnings:
                lines.append(f"    {warning}")

        lines.append(f"{'='*70}\n")

        return "\n".join(lines)


class StrategyBuilder:
    """Builds option strategies from chain data."""

    def __init__(self, option_chain):
        """
        Initialize with option chain fetcher.

        Args:
            option_chain: OptionChain instance
        """
        self.chain = option_chain

    def _create_leg(self, option_data: Dict[str, Any], strike: int,
                    option_type: str, action: str, lots: int) -> OptionLeg:
        """
        Create an OptionLeg with full bid-ask and liquidity data.

        Args:
            option_data: Data from option chain
            strike: Strike price
            option_type: 'CE' or 'PE'
            action: 'BUY' or 'SELL'
            lots: Number of lots

        Returns:
            OptionLeg with all data populated
        """
        ltp = option_data.get('ltp', 0)
        bid = option_data.get('bid', 0)
        ask = option_data.get('ask', 0)

        # Use execution price based on action
        if action == 'BUY':
            premium = ask if ask > 0 else ltp
        else:
            premium = bid if bid > 0 else ltp

        return OptionLeg(
            strike=strike,
            option_type=option_type,
            action=action,
            quantity=lots,
            premium=premium,
            trading_symbol=option_data.get('trading_symbol', ''),
            token=option_data.get('token'),
            bid=bid,
            ask=ask,
            ltp=ltp,
            spread=option_data.get('spread', 0),
            spread_pct=option_data.get('spread_pct', 0),
            oi=option_data.get('oi', 0),
            volume=option_data.get('volume', 0),
            liquidity=option_data.get('liquidity', 'UNKNOWN'),
            delta=option_data.get('delta', 0),
            theta=option_data.get('theta', 0)
        )

    def build_spread(self, symbol: str, spread_type: str, width: int = None,
                     lots: int = 1, expiry: datetime = None) -> Strategy:
        """
        Build a vertical spread strategy.

        Args:
            symbol: Underlying symbol
            spread_type: 'bull_call', 'bear_call', 'bull_put', 'bear_put'
            width: Strike width (uses default if None)
            lots: Number of lots
            expiry: Expiry date (auto-selected if None)

        Returns:
            Strategy object
        """
        # Get expiry
        if expiry is None:
            expiry = self.chain.suggest_expiry(symbol, 'spread')

        # Get chain
        chain_data = self.chain.get_option_chain(symbol, expiry)
        spot = chain_data['spot']
        atm = chain_data['atm']
        gap = chain_data['strike_gap']
        lot_size = chain_data['lot_size']

        # Default width
        if width is None:
            width = gap * 2  # 2 strikes wide

        legs = []
        strategy_type = None
        name = ""

        spread_type = spread_type.lower().replace(' ', '_').replace('-', '_')

        if spread_type in ['bull_call', 'debit_call', 'bull_call_spread']:
            # Buy lower CE, Sell higher CE
            lower_strike = atm
            upper_strike = atm + width

            lower_call = chain_data['calls'].get(lower_strike, {})
            upper_call = chain_data['calls'].get(upper_strike, {})

            legs = [
                self._create_leg(lower_call, lower_strike, 'CE', 'BUY', lots),
                self._create_leg(upper_call, upper_strike, 'CE', 'SELL', lots)
            ]
            strategy_type = StrategyType.BULL_CALL_SPREAD
            name = f"Bull Call Spread {lower_strike}/{upper_strike}"

        elif spread_type in ['bear_call', 'credit_call', 'bear_call_spread']:
            # Sell lower CE, Buy higher CE
            lower_strike = atm + gap  # Slightly OTM
            upper_strike = lower_strike + width

            lower_call = chain_data['calls'].get(lower_strike, {})
            upper_call = chain_data['calls'].get(upper_strike, {})

            legs = [
                self._create_leg(lower_call, lower_strike, 'CE', 'SELL', lots),
                self._create_leg(upper_call, upper_strike, 'CE', 'BUY', lots)
            ]
            strategy_type = StrategyType.BEAR_CALL_SPREAD
            name = f"Bear Call Spread {lower_strike}/{upper_strike}"

        elif spread_type in ['bull_put', 'credit_put', 'bull_put_spread']:
            # Sell higher PE, Buy lower PE
            upper_strike = atm - gap  # Slightly OTM
            lower_strike = upper_strike - width

            lower_put = chain_data['puts'].get(lower_strike, {})
            upper_put = chain_data['puts'].get(upper_strike, {})

            legs = [
                self._create_leg(upper_put, upper_strike, 'PE', 'SELL', lots),
                self._create_leg(lower_put, lower_strike, 'PE', 'BUY', lots)
            ]
            strategy_type = StrategyType.BULL_PUT_SPREAD
            name = f"Bull Put Spread {lower_strike}/{upper_strike}"

        elif spread_type in ['bear_put', 'debit_put', 'bear_put_spread']:
            # Buy higher PE, Sell lower PE
            upper_strike = atm
            lower_strike = atm - width

            lower_put = chain_data['puts'].get(lower_strike, {})
            upper_put = chain_data['puts'].get(upper_strike, {})

            legs = [
                self._create_leg(upper_put, upper_strike, 'PE', 'BUY', lots),
                self._create_leg(lower_put, lower_strike, 'PE', 'SELL', lots)
            ]
            strategy_type = StrategyType.BEAR_PUT_SPREAD
            name = f"Bear Put Spread {lower_strike}/{upper_strike}"

        else:
            raise ValueError(f"Unknown spread type: {spread_type}")

        return Strategy(
            name=name,
            strategy_type=strategy_type,
            symbol=symbol.upper(),
            expiry=expiry,
            spot=spot,
            legs=legs,
            lot_size=lot_size
        )

    def build_straddle(self, symbol: str, direction: str = 'long',
                       lots: int = 1, expiry: datetime = None) -> Strategy:
        """Build ATM straddle."""
        if expiry is None:
            expiry = self.chain.suggest_expiry(symbol, 'straddle')

        chain_data = self.chain.get_option_chain(symbol, expiry)
        atm = chain_data['atm']
        lot_size = chain_data['lot_size']

        atm_call = chain_data['calls'].get(atm, {})
        atm_put = chain_data['puts'].get(atm, {})

        action = 'BUY' if direction.lower() == 'long' else 'SELL'
        strategy_type = StrategyType.LONG_STRADDLE if action == 'BUY' else StrategyType.SHORT_STRADDLE
        name = f"{'Long' if action == 'BUY' else 'Short'} Straddle {atm}"

        legs = [
            self._create_leg(atm_call, atm, 'CE', action, lots),
            self._create_leg(atm_put, atm, 'PE', action, lots)
        ]

        return Strategy(
            name=name,
            strategy_type=strategy_type,
            symbol=symbol.upper(),
            expiry=expiry,
            spot=chain_data['spot'],
            legs=legs,
            lot_size=lot_size
        )

    def build_strangle(self, symbol: str, direction: str = 'long',
                       width: int = None, lots: int = 1,
                       expiry: datetime = None) -> Strategy:
        """Build OTM strangle."""
        if expiry is None:
            expiry = self.chain.suggest_expiry(symbol, 'strangle')

        chain_data = self.chain.get_option_chain(symbol, expiry)
        atm = chain_data['atm']
        gap = chain_data['strike_gap']
        lot_size = chain_data['lot_size']

        if width is None:
            width = gap * 2  # 2 strikes OTM each side

        call_strike = atm + width
        put_strike = atm - width

        otm_call = chain_data['calls'].get(call_strike, {})
        otm_put = chain_data['puts'].get(put_strike, {})

        action = 'BUY' if direction.lower() == 'long' else 'SELL'
        strategy_type = StrategyType.LONG_STRANGLE if action == 'BUY' else StrategyType.SHORT_STRANGLE
        name = f"{'Long' if action == 'BUY' else 'Short'} Strangle {put_strike}/{call_strike}"

        legs = [
            self._create_leg(otm_call, call_strike, 'CE', action, lots),
            self._create_leg(otm_put, put_strike, 'PE', action, lots)
        ]

        return Strategy(
            name=name,
            strategy_type=strategy_type,
            symbol=symbol.upper(),
            expiry=expiry,
            spot=chain_data['spot'],
            legs=legs,
            lot_size=lot_size
        )

    def build_iron_condor(self, symbol: str, width: int = None,
                          wing_width: int = None, lots: int = 1,
                          expiry: datetime = None) -> Strategy:
        """Build iron condor (sell OTM strangle + buy further OTM strangle)."""
        if expiry is None:
            expiry = self.chain.suggest_expiry(symbol, 'iron_condor')

        chain_data = self.chain.get_option_chain(symbol, expiry)
        atm = chain_data['atm']
        gap = chain_data['strike_gap']
        lot_size = chain_data['lot_size']

        if width is None:
            width = gap * 2  # Short strikes 2 gaps OTM
        if wing_width is None:
            wing_width = gap  # Wings 1 gap further

        # Short strikes
        short_call_strike = atm + width
        short_put_strike = atm - width

        # Long strikes (protection)
        long_call_strike = short_call_strike + wing_width
        long_put_strike = short_put_strike - wing_width

        legs = [
            self._create_leg(chain_data['calls'].get(short_call_strike, {}), short_call_strike, 'CE', 'SELL', lots),
            self._create_leg(chain_data['calls'].get(long_call_strike, {}), long_call_strike, 'CE', 'BUY', lots),
            self._create_leg(chain_data['puts'].get(short_put_strike, {}), short_put_strike, 'PE', 'SELL', lots),
            self._create_leg(chain_data['puts'].get(long_put_strike, {}), long_put_strike, 'PE', 'BUY', lots)
        ]

        name = f"Iron Condor {long_put_strike}/{short_put_strike}/{short_call_strike}/{long_call_strike}"

        return Strategy(
            name=name,
            strategy_type=StrategyType.IRON_CONDOR,
            symbol=symbol.upper(),
            expiry=expiry,
            spot=chain_data['spot'],
            legs=legs,
            lot_size=lot_size
        )

    def build_iron_butterfly(self, symbol: str, wing_width: int = None,
                              lots: int = 1, expiry: datetime = None) -> Strategy:
        """Build iron butterfly (sell ATM straddle + buy OTM strangle)."""
        if expiry is None:
            expiry = self.chain.suggest_expiry(symbol, 'iron_butterfly')

        chain_data = self.chain.get_option_chain(symbol, expiry)
        atm = chain_data['atm']
        gap = chain_data['strike_gap']
        lot_size = chain_data['lot_size']

        if wing_width is None:
            wing_width = gap * 2

        call_wing = atm + wing_width
        put_wing = atm - wing_width

        legs = [
            self._create_leg(chain_data['calls'].get(atm, {}), atm, 'CE', 'SELL', lots),
            self._create_leg(chain_data['puts'].get(atm, {}), atm, 'PE', 'SELL', lots),
            self._create_leg(chain_data['calls'].get(call_wing, {}), call_wing, 'CE', 'BUY', lots),
            self._create_leg(chain_data['puts'].get(put_wing, {}), put_wing, 'PE', 'BUY', lots)
        ]

        name = f"Iron Butterfly {put_wing}/{atm}/{call_wing}"

        return Strategy(
            name=name,
            strategy_type=StrategyType.IRON_BUTTERFLY,
            symbol=symbol.upper(),
            expiry=expiry,
            spot=chain_data['spot'],
            legs=legs,
            lot_size=lot_size
        )

    def suggest_strategy(self, symbol: str, view: str, risk_appetite: str = 'moderate',
                         lots: int = 1) -> List[Strategy]:
        """
        Suggest strategies based on market view.

        Args:
            symbol: Underlying symbol
            view: 'bullish', 'bearish', 'neutral', 'volatile'
            risk_appetite: 'low', 'moderate', 'high'
            lots: Number of lots

        Returns:
            List of suggested strategies
        """
        strategies = []
        view = view.lower()
        risk = risk_appetite.lower()

        if view == 'bullish':
            if risk == 'low':
                strategies.append(self.build_spread(symbol, 'bull_put', lots=lots))
            elif risk == 'moderate':
                strategies.append(self.build_spread(symbol, 'bull_call', lots=lots))
                strategies.append(self.build_spread(symbol, 'bull_put', lots=lots))
            else:
                strategies.append(self.build_spread(symbol, 'bull_call', lots=lots))

        elif view == 'bearish':
            if risk == 'low':
                strategies.append(self.build_spread(symbol, 'bear_call', lots=lots))
            elif risk == 'moderate':
                strategies.append(self.build_spread(symbol, 'bear_put', lots=lots))
                strategies.append(self.build_spread(symbol, 'bear_call', lots=lots))
            else:
                strategies.append(self.build_spread(symbol, 'bear_put', lots=lots))

        elif view == 'neutral':
            if risk == 'low':
                strategies.append(self.build_iron_condor(symbol, lots=lots))
            elif risk == 'moderate':
                strategies.append(self.build_iron_butterfly(symbol, lots=lots))
                strategies.append(self.build_strangle(symbol, 'short', lots=lots))
            else:
                strategies.append(self.build_straddle(symbol, 'short', lots=lots))

        elif view == 'volatile':
            if risk == 'low':
                strategies.append(self.build_strangle(symbol, 'long', lots=lots))
            else:
                strategies.append(self.build_straddle(symbol, 'long', lots=lots))

        return strategies
