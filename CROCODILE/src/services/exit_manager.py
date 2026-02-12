"""Exit Manager - GTT Management and Timeframe-Based Trailing Stop Loss"""

from datetime import datetime, date, timedelta
from typing import Dict, Tuple, Optional, List
from loguru import logger
from sqlalchemy.orm import Session
import pandas as pd
import time

from src.models.database import (
    OpenPosition, ClosedPosition, GTTUpdateLog, TransactionHistory, CapitalLedger,
    PositionStatus, TransactionType, get_session
)
from src.api.broker_factory import get_broker
from src.utils.config_manager import config
from src.utils.price_rounder import round_price
from src.utils.cost_calculator import cost_calculator
from src.utils.timezone_helper import ist_now_naive, now_ist, days_from_now_ist
from src.reporting.telegram_client import telegram


class ExitManager:
    """
    Manages position exits via GTT orders and timeframe-based trailing stop loss

    Responsibilities:
    1. Place dummy protective GTT when order fills
    2. Update GTT based on timeframe:
       - Daily (D): Every day with today's LOW
       - Weekly (W): Only on Fridays with week's LOW
       - Monthly (M): Only on last trading day with month's LOW
    3. Handle GTT placement failures with retries
    4. Detect position closures (SL hit or manual)
    5. Calculate P&L and update database
    """

    def __init__(self):
        """Initialize exit manager"""
        self.kite_client = get_broker()
        self.dummy_sl_percent = config.get('trading.dummy_sl_percent', 15) / 100
        self.gtt_expiry_days = 365  # 1 year

        # ATR-based SL strategy settings
        sl_config = config.get('trading.sl_strategy', {})
        self.sl_strategy_enabled = sl_config.get('enabled', False)
        self.initial_atr_multiplier = sl_config.get('initial_atr_multiplier', 2.0)
        self.trailing_activation_multiplier = sl_config.get('trailing_activation_multiplier', 1.5)
        self.trailing_atr_multiplier = sl_config.get('trailing_atr_multiplier', 2.0)
        self.atr_period = sl_config.get('atr_period', 14)
        self.fallback_sl_percent = sl_config.get('fallback_sl_percent', 10) / 100

        if self.sl_strategy_enabled:
            logger.info(
                f"ATR SL Strategy: Enabled | Initial={self.initial_atr_multiplier}×ATR, "
                f"TrailActivation={self.trailing_activation_multiplier}×ATR, "
                f"Trailing={self.trailing_atr_multiplier}×ATR, Period={self.atr_period}"
            )

        # GTT verification settings
        self.gtt_verification_enabled = config.get('api_resilience.gtt_verification.enabled', True)
        self.gtt_initial_delay = config.get('api_resilience.gtt_verification.initial_delay', 2.0)
        self.gtt_verification_retries = config.get('api_resilience.gtt_verification.max_retries', 3)
        self.gtt_retry_delays = config.get('api_resilience.gtt_verification.retry_delays', [1.0, 3.0, 5.0])
        self.gtt_max_placement_attempts = config.get('api_resilience.gtt_verification.max_placement_attempts', 3)

        logger.info(
            f"GTT Verification: Enabled={self.gtt_verification_enabled}, "
            f"InitialDelay={self.gtt_initial_delay}s, Retries={self.gtt_verification_retries}, "
            f"RetryDelays={self.gtt_retry_delays}"
        )

    # ==================== GTT VERIFICATION METHODS ====================

    def _verify_gtt_exists(
        self,
        gtt_id: str,
        script: str,
        max_retries: Optional[int] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify that a GTT order actually exists in Zerodha after placement

        This is CRITICAL - API might return success but GTT might not be created
        due to network glitches, Zerodha issues, or silent failures.

        Implements exponential backoff with initial delay to allow Zerodha sync time.

        Args:
            gtt_id: GTT trigger ID to verify
            script: Trading symbol (for logging)
            max_retries: Number of verification attempts (default: from config)

        Returns:
            (exists, error_message)
                - exists: True if GTT found, False otherwise
                - error_message: Details if verification failed
        """
        if not self.gtt_verification_enabled:
            logger.warning("GTT verification is disabled - skipping check (NOT RECOMMENDED)")
            return True, None

        retries = max_retries if max_retries is not None else self.gtt_verification_retries
        start_time = time.time()

        # CRITICAL: Initial delay to allow Zerodha to sync the GTT
        logger.debug(f"Waiting {self.gtt_initial_delay}s for Zerodha to sync GTT {gtt_id}")
        time.sleep(self.gtt_initial_delay)

        for attempt in range(1, retries + 1):
            try:
                logger.debug(
                    f"Verifying GTT {gtt_id} for {script} (attempt {attempt}/{retries})"
                )

                # Fetch all active GTT orders from Zerodha
                gtt_orders = self.kite_client.get_gtt_orders()

                # Check if our GTT ID exists
                for gtt in gtt_orders:
                    if str(gtt.get('id')) == str(gtt_id):
                        elapsed = time.time() - start_time
                        logger.info(
                            f"✅ GTT verification passed: {gtt_id} found for {script} "
                            f"(attempt {attempt}/{retries}, elapsed: {elapsed:.2f}s)"
                        )
                        return True, None

                # GTT not found
                elapsed = time.time() - start_time
                logger.warning(
                    f"GTT {gtt_id} not found in Zerodha for {script} "
                    f"(attempt {attempt}/{retries}, elapsed: {elapsed:.2f}s)"
                )

                # Wait before retry with exponential backoff (except on last attempt)
                if attempt < retries:
                    # Get delay for this retry (use last delay if attempt exceeds list)
                    delay_index = min(attempt - 1, len(self.gtt_retry_delays) - 1)
                    retry_delay = self.gtt_retry_delays[delay_index]
                    logger.debug(f"Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)

            except Exception as e:
                elapsed = time.time() - start_time
                logger.error(
                    f"Error during GTT verification for {script}: {e} "
                    f"(attempt {attempt}/{retries}, elapsed: {elapsed:.2f}s)"
                )

                # Wait before retry with exponential backoff (except on last attempt)
                if attempt < retries:
                    delay_index = min(attempt - 1, len(self.gtt_retry_delays) - 1)
                    retry_delay = self.gtt_retry_delays[delay_index]
                    logger.debug(f"Waiting {retry_delay}s before retry...")
                    time.sleep(retry_delay)

        # All verification attempts failed
        total_elapsed = time.time() - start_time
        error_msg = (
            f"GTT verification failed: {gtt_id} not found in Zerodha after {retries} attempts "
            f"({total_elapsed:.2f}s total). Position may be UNPROTECTED!"
        )
        logger.error(error_msg)
        return False, error_msg

    # ==================== ATR CALCULATION ====================

    def _calculate_weekly_atr(self, script: str, period: Optional[int] = None) -> Optional[float]:
        """
        Calculate current Weekly ATR for a given script using Wilder's smoothing.

        Fetches weekly resampled data via broker API and computes ATR.
        Uses the same formula as supertrend_calculator.py for consistency.

        Args:
            script: Trading symbol (e.g., 'RELIANCE')
            period: ATR period (default: from config, typically 14)

        Returns:
            Float ATR value, or None if calculation fails
        """
        if period is None:
            period = self.atr_period

        try:
            instrument_token = self.kite_client.get_instrument_token(script)

            # Fetch weekly resampled data (needs enough history for ATR smoothing)
            # 1.5 years gives ~78 weekly candles, plenty for 14-period ATR
            df = self.kite_client.get_historical_data_sampled(
                instrument_token=instrument_token,
                timeframe='weekly',
                years_back=1.5
            )

            if df is None or df.empty:
                logger.warning(f"No weekly data for {script} - ATR calculation failed")
                return None

            if len(df) < period:
                logger.warning(
                    f"Insufficient weekly data for {script}: {len(df)} candles < {period} period"
                )
                return None

            # True Range calculation (Wilder's method - same as supertrend_calculator.py)
            df = df.copy()
            df['prev_close'] = df['Close'].shift(1)
            df['tr1'] = df['High'] - df['Low']
            df['tr2'] = abs(df['High'] - df['prev_close'])
            df['tr3'] = abs(df['Low'] - df['prev_close'])
            df['tr'] = df[['tr1', 'tr2', 'tr3']].max(axis=1)

            # ATR using Wilder's smoothing (EMA with alpha=1/period)
            atr_series = df['tr'].ewm(alpha=1/period, adjust=False).mean()

            # Return the latest ATR value
            current_atr = float(atr_series.iloc[-1])

            if current_atr <= 0:
                logger.warning(f"Invalid ATR value for {script}: {current_atr}")
                return None

            logger.info(f"{script}: Weekly ATR({period}) = Rs.{current_atr:.2f}")
            return current_atr

        except Exception as e:
            logger.error(f"ATR calculation failed for {script}: {e}")
            return None

    # ==================== HELPER METHODS FOR TIMEFRAME-BASED UPDATES ====================

    def _is_last_trading_day_of_week(self, check_date: date) -> bool:
        """
        Check if given date is the last trading day of the week

        - Normally Friday (weekday = 4)
        - Could be Thursday if Friday is a market holiday (future enhancement)

        Args:
            check_date: Date to check

        Returns:
            True if it's the last trading day of the week
        """
        # Friday is weekday 4 (Monday = 0, Sunday = 6)
        return check_date.weekday() == 4

    def _is_last_trading_day_of_month(self, check_date: date) -> bool:
        """
        Check if given date is the last trading day of the month

        Handles cases where month ends on weekend:
        - If month ends on Sat/Sun, last trading day is the preceding Friday
        - If month ends on weekday, that weekday is the last trading day

        Examples:
        - Aug 31, 2024 (Sat) → Aug 30 (Fri) is last trading day
        - Sep 30, 2024 (Mon) → Sep 30 (Mon) is last trading day
        - Mar 31, 2025 (Mon) → Mar 31 (Mon) is last trading day

        Args:
            check_date: Date to check

        Returns:
            True if it's the last trading day of the month

        Note: Does not account for market holidays (future enhancement)
        """
        # Today must be a weekday (bot doesn't run on weekends)
        if check_date.weekday() >= 5:  # Sat=5, Sun=6
            return False

        # Calculate last calendar day of this month
        if check_date.month == 12:
            # December → next month is January next year
            next_month_first = date(check_date.year + 1, 1, 1)
        else:
            next_month_first = date(check_date.year, check_date.month + 1, 1)

        last_calendar_day = next_month_first - timedelta(days=1)

        # Walk backwards from last calendar day to find last trading day (weekday)
        last_trading_day = last_calendar_day
        while last_trading_day.weekday() >= 5:  # Skip Sat/Sun
            last_trading_day -= timedelta(days=1)

        # Check if today is the last trading day
        return check_date == last_trading_day

    def should_update_gtt_today(self, position: OpenPosition, check_date: date) -> bool:
        """
        Determine if GTT should be updated today based on position's timeframe

        Rules:
        - Daily (D): Update every day
        - Weekly (W): Update only on Fridays (end of weekly candle)
        - Monthly (M): Update only on last trading day of month (end of monthly candle)

        Args:
            position: OpenPosition object
            check_date: Date to check (typically today)

        Returns:
            True if GTT should be updated today, False otherwise
        """
        timeframe = position.timeframe.upper()

        if timeframe == 'D':
            # Daily: Update every day
            return True

        elif timeframe == 'W':
            # Weekly: Update only on Fridays
            is_update_day = self._is_last_trading_day_of_week(check_date)
            if not is_update_day:
                logger.info(
                    f"{position.script} {position.timeframe}: Skipping GTT update "
                    f"(Weekly position, today is {check_date.strftime('%A')}, waiting for Friday)"
                )
            return is_update_day

        elif timeframe == 'M':
            # Monthly: Update only on last trading day of month
            is_update_day = self._is_last_trading_day_of_month(check_date)
            if not is_update_day:
                logger.info(
                    f"{position.script} {position.timeframe}: Skipping GTT update "
                    f"(Monthly position, not last trading day of month)"
                )
            return is_update_day

        else:
            logger.warning(f"Unknown timeframe '{position.timeframe}' for {position.script}, skipping update")
            return False

    def get_trailing_sl_for_timeframe(
        self,
        position: OpenPosition,
        check_date: date
    ) -> Optional[float]:
        """
        Get the appropriate trailing SL based on position's timeframe and strategy.

        For Weekly positions with entry_atr (ATR strategy):
          - Check activation gate: friday_close > entry + 1.5 * entry_atr
          - If not activated: return current_sl (no change)
          - If activated: return friday_close - 2.0 * current_weekly_atr

        For Weekly positions without entry_atr (legacy):
          - Use week's LOW (Mon-Fri) as trailing SL

        For Daily/Monthly: unchanged (period LOW)

        Args:
            position: OpenPosition object
            check_date: Current date (typically today)

        Returns:
            Trailing SL price, or None if data unavailable
        """
        try:
            instrument_token = self.kite_client.get_instrument_token(position.script)
            timeframe = position.timeframe.upper()

            if timeframe == 'D':
                # Daily: Get today's LOW (unchanged)
                return self._get_period_low(instrument_token, position, check_date, check_date)

            elif timeframe == 'W':
                # Weekly: ATR-based or legacy LOW-based
                if self.sl_strategy_enabled and position.entry_atr is not None and position.entry_atr > 0:
                    return self._get_atr_trailing_sl(instrument_token, position, check_date)
                else:
                    # Legacy: week's LOW
                    week_start = check_date - timedelta(days=check_date.weekday())
                    if position.entry_date > week_start:
                        logger.debug(
                            f"{position.script} {position.timeframe}: Position entered mid-week "
                            f"({position.entry_date}), but using full week LOW from {week_start}"
                        )
                    return self._get_period_low(instrument_token, position, week_start, check_date)

            elif timeframe == 'M':
                # Monthly: month's LOW (unchanged)
                month_start = check_date.replace(day=1)
                if position.entry_date > month_start:
                    logger.debug(
                        f"{position.script} {position.timeframe}: Position entered mid-month "
                        f"({position.entry_date}), but using full month LOW from {month_start}"
                    )
                return self._get_period_low(instrument_token, position, month_start, check_date)

            else:
                logger.error(f"Unknown timeframe '{timeframe}' for {position.script}")
                return None

        except Exception as e:
            logger.error(
                f"Error getting trailing SL for {position.script} {position.timeframe}: {e}"
            )
            return None

    def _get_period_low(
        self,
        instrument_token: str,
        position: OpenPosition,
        start_date: date,
        end_date: date
    ) -> Optional[float]:
        """Get the minimum LOW price for a date range (used by Daily/Monthly/legacy Weekly)."""
        try:
            df = self.kite_client.get_historical_data(
                instrument_token=instrument_token,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                interval='day'
            )

            if df is None or df.empty:
                logger.warning(
                    f"No historical data for {position.script} from {start_date} to {end_date}"
                )
                return None

            trailing_low = float(df['Low'].min())

            logger.info(
                f"{position.script} {position.timeframe}: Trailing LOW from "
                f"{start_date} to {end_date} = Rs.{trailing_low:.2f} "
                f"(based on {len(df)} candles)"
            )

            return trailing_low

        except Exception as e:
            logger.error(
                f"Error getting period LOW for {position.script}: {e}"
            )
            return None

    def _get_atr_trailing_sl(
        self,
        instrument_token: str,
        position: OpenPosition,
        check_date: date
    ) -> Optional[float]:
        """
        ATR-based trailing SL for Weekly positions.

        Logic:
        1. Get Friday close (LTP on Friday EOD, or historical close for catch-up)
        2. Check activation: friday_close > entry + activation_multiplier * entry_atr
        3. If not activated: return current_sl (triggers "no_change" in caller)
        4. If activated: compute friday_close - trailing_multiplier * current_weekly_atr

        Falls back to entry_atr if current ATR calculation fails.
        """
        try:
            script = position.script
            entry_price = position.entry_price
            entry_atr = position.entry_atr

            # Get Friday close price
            today = date.today()
            if check_date == today:
                # Real-time (Friday EOD at 3:50 PM): LTP IS the close price
                friday_close = self.kite_client.get_instrument_ltp(instrument_token)
            else:
                # Catch-up (e.g., Monday morning for missed Friday update):
                # Use historical data to get the actual close for that date
                df = self.kite_client.get_historical_data(
                    instrument_token=instrument_token,
                    start_date=check_date.strftime('%Y-%m-%d'),
                    end_date=check_date.strftime('%Y-%m-%d'),
                    interval='day'
                )
                if df is not None and not df.empty:
                    friday_close = float(df['Close'].iloc[-1])
                    logger.info(
                        f"{script} W [ATR]: Catch-up mode - using historical close "
                        f"Rs.{friday_close:.2f} for {check_date}"
                    )
                else:
                    logger.warning(
                        f"{script}: Could not get historical close for {check_date}, "
                        f"falling back to current LTP"
                    )
                    friday_close = self.kite_client.get_instrument_ltp(instrument_token)

            if friday_close is None or friday_close <= 0:
                logger.warning(f"{script}: Could not get Friday close price for ATR trailing")
                return None

            # Activation gate: has the position moved enough to start trailing?
            activation_threshold = entry_price + (self.trailing_activation_multiplier * entry_atr)

            if friday_close <= activation_threshold:
                logger.info(
                    f"{script} W [ATR]: Trailing NOT activated - "
                    f"Close Rs.{friday_close:.2f} <= Threshold Rs.{activation_threshold:.2f} "
                    f"(Entry {entry_price:.2f} + {self.trailing_activation_multiplier}×ATR {entry_atr:.2f})"
                )
                # Return current SL to trigger "no_change" path in caller
                return position.current_sl

            # Trailing activated - calculate new SL
            # Try to get current weekly ATR (may differ from entry ATR if volatility changed)
            current_atr = self._calculate_weekly_atr(script, self.atr_period)

            if current_atr is None:
                # Fallback to entry ATR if current calc fails
                current_atr = entry_atr
                logger.warning(
                    f"{script} W [ATR]: Current ATR calc failed, using entry_atr Rs.{entry_atr:.2f}"
                )

            new_sl = friday_close - (self.trailing_atr_multiplier * current_atr)

            logger.info(
                f"{script} W [ATR]: Trailing ACTIVATED - "
                f"Close Rs.{friday_close:.2f} > Threshold Rs.{activation_threshold:.2f} | "
                f"New SL = {friday_close:.2f} - {self.trailing_atr_multiplier}×{current_atr:.2f} = Rs.{new_sl:.2f}"
            )

            return new_sl

        except Exception as e:
            logger.error(f"ATR trailing SL calculation failed for {position.script}: {e}")
            return None

    # ==================== GTT PLACEMENT METHODS ====================

    def place_dummy_gtt(
        self,
        position: OpenPosition,
        entry_price: float,
        session: Session,
        zerodha_symbol: Optional[str] = None
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Place dummy protective GTT immediately after entry with verification

        Includes retry logic:
        - Attempts GTT placement up to max_placement_attempts times
        - Each placement is verified by checking get_gtt_orders()
        - If verification fails, retries entire placement process
        - Sends CRITICAL alert if all attempts fail

        Args:
            position: OpenPosition object
            entry_price: Entry price for the position
            session: Database session
            zerodha_symbol: Trading symbol from Zerodha (optional, defaults to position.script)

        Returns:
            (success, gtt_id, error_message)
        """
        try:
            # Use Zerodha symbol if provided, otherwise fall back to position.script
            trading_symbol = zerodha_symbol if zerodha_symbol else position.script

            # Calculate initial SL based on strategy
            if self.sl_strategy_enabled and position.entry_atr is not None and position.entry_atr > 0:
                # ATR-based initial SL: Entry - multiplier * ATR
                dummy_sl = entry_price - (self.initial_atr_multiplier * position.entry_atr)
                sl_label = f"ATR-based ({self.initial_atr_multiplier}×{position.entry_atr:.2f})"
            elif self.sl_strategy_enabled and position.entry_atr is None:
                # ATR calc failed at entry - use fallback percentage
                dummy_sl = entry_price * (1 - self.fallback_sl_percent)
                sl_label = f"Fallback ({self.fallback_sl_percent*100:.0f}%)"
            else:
                # Legacy: fixed percentage SL
                dummy_sl = entry_price * (1 - self.dummy_sl_percent)
                sl_label = f"Legacy ({self.dummy_sl_percent*100:.0f}%)"

            # Safety floor: SL never more than 50% below entry
            min_sl = entry_price * 0.50
            if dummy_sl < min_sl:
                logger.warning(
                    f"SL Rs.{dummy_sl:.2f} exceeds 50% floor for {trading_symbol}, "
                    f"capping at Rs.{min_sl:.2f}"
                )
                dummy_sl = min_sl

            dummy_sl_rounded = round_price(dummy_sl)

            logger.info(
                f"Placing initial GTT for {trading_symbol} {position.timeframe}: "
                f"Entry=Rs.{entry_price:.2f}, SL=Rs.{dummy_sl_rounded:.2f} ({sl_label})"
            )

            # Get current LTP for GTT placement
            instrument_token = self.kite_client.get_instrument_token(position.script)
            current_ltp = self.kite_client.get_instrument_ltp(instrument_token)

            # Retry placement with verification
            last_error = None
            for attempt in range(1, self.gtt_max_placement_attempts + 1):
                logger.info(
                    f"GTT placement attempt {attempt}/{self.gtt_max_placement_attempts} "
                    f"for {trading_symbol}"
                )

                # Attempt to place GTT (using Zerodha symbol)
                success, gtt_id, error = self._place_gtt_order(
                    script=trading_symbol,
                    quantity=position.quantity,
                    sl_price=dummy_sl_rounded,
                    current_ltp=current_ltp
                )

                if not success:
                    last_error = error
                    logger.warning(
                        f"GTT placement failed (attempt {attempt}): {error}"
                    )
                    # Wait before retry (except on last attempt)
                    if attempt < self.gtt_max_placement_attempts:
                        time.sleep(1)
                    continue

                # GTT placement succeeded - SAVE IT IMMEDIATELY before verification
                # This prevents duplicate GTTs if verification fails but GTT actually exists
                position.current_sl = dummy_sl_rounded
                position.initial_sl = dummy_sl_rounded  # Will be updated at EOD
                position.current_gtt_id = str(gtt_id)  # Ensure string format
                position.gtt_placed_at = ist_now_naive()
                session.commit()
                logger.info(f"GTT ID {gtt_id} saved to DB for {trading_symbol}")

                # Now verify it exists in Zerodha
                verified, verify_error = self._verify_gtt_exists(
                    gtt_id=gtt_id,
                    script=trading_symbol
                )

                if verified:
                    logger.info(
                        f"✅ Dummy GTT placed and verified: {gtt_id} for {trading_symbol}"
                    )
                    return True, gtt_id, None

                else:
                    # Verification failed but GTT ID already saved to DB
                    # DON'T cancel - GTT might exist but not propagated yet
                    # DON'T retry - would create duplicate GTTs
                    logger.warning(
                        f"⚠️ GTT {gtt_id} verification failed but ID saved to DB: {verify_error}. "
                        f"GTT may exist - will be verified by monitoring. NOT retrying to avoid duplicates."
                    )

                    # Return success with warning - GTT is placed but unverified
                    # The monitoring system will detect if it's actually missing
                    return True, gtt_id, f"GTT placed but verification failed: {verify_error}"

            # All attempts failed
            critical_error = (
                f"🚨 **CRITICAL: DUMMY GTT PLACEMENT FAILED**\n\n"
                f"**Position Details:**\n"
                f"• Script: {trading_symbol} ({position.timeframe})\n"
                f"• Entry Price: Rs.{entry_price:.2f}\n"
                f"• Quantity: {position.quantity}\n"
                f"• Capital at Risk: Rs.{position.capital_deployed:,.2f}\n"
                f"• Intended SL: Rs.{dummy_sl_rounded:.2f}\n\n"
                f"**Error:**\n"
                f"• Placement attempts: {self.gtt_max_placement_attempts}\n"
                f"• Last error: {last_error}\n\n"
                f"⚠️ **URGENT:** Position is UNPROTECTED!\n"
                f"**Action:** Manually place GTT stop loss @ Rs.{dummy_sl_rounded:.2f} or exit position"
            )
            logger.critical(critical_error)

            # Send Telegram alert
            telegram.send_alert(critical_error, critical=True)

            return False, None, last_error

        except Exception as e:
            error_msg = str(e)
            # Use trading_symbol if defined, otherwise fall back to position.script
            script_name = trading_symbol if 'trading_symbol' in locals() else position.script
            logger.error(f"Error placing dummy GTT for {script_name}: {e}")

            # Send critical alert
            critical_error = (
                f"🚨 **CRITICAL: GTT PLACEMENT ERROR**\n\n"
                f"**Position Details:**\n"
                f"• Script: {script_name} ({position.timeframe})\n"
                f"• Entry Price: Rs.{entry_price:.2f}\n"
                f"• Quantity: {position.quantity}\n"
                f"• Capital at Risk: Rs.{entry_price * position.quantity:,.2f}\n\n"
                f"**Error:**\n"
                f"{error_msg}\n\n"
                f"⚠️ **URGENT:** Position is UNPROTECTED!\n"
                f"**Action:** Manually place stop loss or exit position"
            )
            telegram.send_alert(critical_error, critical=True)

            return False, None, error_msg

    def _place_gtt_order(
        self,
        script: str,
        quantity: int,
        sl_price: float,
        current_ltp: float,
        retry_with_buffer: bool = False
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Place GTT order on Zerodha

        Args:
            script: Trading symbol
            quantity: Number of shares
            sl_price: Stop loss price
            current_ltp: Current LTP (for trigger validation)
            retry_with_buffer: If True, add 0.2% buffer to SL

        Returns:
            (success, gtt_id, error_message)
        """
        try:
            # Apply buffer if retrying
            # Zerodha requires trigger price to be > 0.25% away from LTP
            # We use 0.3% buffer (0.25% requirement + 0.05% safety margin)
            if retry_with_buffer:
                sl_price = sl_price * 0.997  # 0.3% buffer
                sl_price = round_price(sl_price)
                logger.info(f"Retrying GTT with 0.3% buffer: SL=Rs.{sl_price:.2f}")

            # Calculate trigger price (same as SL for LIMIT order)
            trigger_price = sl_price

            # Calculate expiry date (IST timezone)
            expiry_date_ist = days_from_now_ist(self.gtt_expiry_days)
            expiry_str = expiry_date_ist.strftime("%Y-%m-%d %H:%M:%S")

            # Build GTT payload (form data format - condition and orders will be JSON-stringified)
            payload = {
                "condition": {
                    "exchange": "NSE",
                    "tradingsymbol": script,
                    "trigger_values": [trigger_price],
                    "last_price": current_ltp
                },
                "orders": [{
                    "exchange": "NSE",
                    "tradingsymbol": script,
                    "transaction_type": "SELL",
                    "quantity": quantity,
                    "price": sl_price,
                    "order_type": "LIMIT",
                    "product": "CNC"
                }],
                "type": "single",
                "expires_at": expiry_str
            }

            # Place GTT via Kite API
            response = self.kite_client.place_gtt_order(payload)

            gtt_id = response.get('trigger_id')

            if gtt_id:
                logger.info(f"GTT placed: {gtt_id} for {script} @ Rs.{sl_price:.2f}")
                return True, gtt_id, None
            else:
                error = "No trigger_id in GTT response"
                logger.error(error)
                return False, None, error

        except Exception as e:
            error_str = str(e)

            # Check if error is due to price being too close to LTP
            # Zerodha error: "Trigger price was too close to the last price. (difference should be more than 0.25%)"
            price_too_close = (
                "too close" in error_str.lower() or
                "0.25%" in error_str or
                "0.2%" in error_str.lower()
            )

            if price_too_close and not retry_with_buffer:
                logger.warning("GTT rejected - trigger price too close to LTP, retrying with 0.3% buffer")
                return self._place_gtt_order(script, quantity, sl_price, current_ltp, retry_with_buffer=True)

            logger.error(f"GTT placement failed: {e}")
            return False, None, error_str

    def cancel_gtt_order(self, gtt_id: str) -> bool:
        """
        Cancel existing GTT order

        Args:
            gtt_id: GTT trigger ID

        Returns:
            Success status
        """
        try:
            self.kite_client.cancel_gtt_order(gtt_id)
            logger.info(f"GTT cancelled: {gtt_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to cancel GTT {gtt_id}: {e}")
            return False

    def update_gtt_with_trailing_sl(
        self,
        position: OpenPosition,
        trailing_sl: float,
        session: Session
    ) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Update GTT with timeframe-appropriate trailing SL (with verification)

        - Daily: Today's LOW
        - Weekly (ATR): ATR-based trailing SL with activation gate
        - Weekly (legacy): Week's LOW (Mon-Fri)
        - Monthly: Month's LOW

        CRITICAL WORKFLOW:
        1. Cancel old GTT
        2. Place new GTT
        3. VERIFY new GTT exists in Zerodha
        4. If verification fails, retry placement (up to max_placement_attempts)
        5. If all attempts fail, position is UNPROTECTED → Send critical alert

        Args:
            position: OpenPosition object
            trailing_sl: Trailing SL price for the timeframe
            session: Database session

        Returns:
            (success, error_message, update_details)
            - update_details: Dict with {'old_sl': float, 'new_sl': float} when updated, None otherwise
        """
        try:
            new_sl_rounded = round_price(trailing_sl)

            # Store original SL before any modifications
            original_sl = position.current_sl

            # Check if new SL > current SL (only trail UP, never DOWN)
            if new_sl_rounded <= position.current_sl:
                logger.info(
                    f"No GTT update needed for {position.script} {position.timeframe}: "
                    f"New SL=Rs.{new_sl_rounded:.2f} <= Current SL=Rs.{position.current_sl:.2f}"
                )
                return True, "no_change", None  # Success but no update needed

            old_sl = position.current_sl
            old_gtt_id = position.current_gtt_id

            logger.info(
                f"Updating GTT for {position.script} {position.timeframe}: "
                f"SL Rs.{old_sl:.2f} → Rs.{new_sl_rounded:.2f}"
            )

            # Step 1: Cancel old GTT
            old_gtt_cancelled = False
            if old_gtt_id:
                cancel_success = self.cancel_gtt_order(old_gtt_id)
                if cancel_success:
                    old_gtt_cancelled = True
                    logger.info(f"Old GTT cancelled: {old_gtt_id}")
                else:
                    logger.warning(
                        f"Failed to cancel old GTT {old_gtt_id}, continuing with new placement..."
                    )

            # Step 2: Place new GTT with verification and retry
            instrument_token = self.kite_client.get_instrument_token(position.script)
            current_ltp = self.kite_client.get_instrument_ltp(instrument_token)

            last_error = None
            new_gtt_id = None

            for attempt in range(1, self.gtt_max_placement_attempts + 1):
                logger.info(
                    f"GTT update attempt {attempt}/{self.gtt_max_placement_attempts} "
                    f"for {position.script}"
                )

                # Attempt to place new GTT
                success, gtt_id, error = self._place_gtt_order(
                    script=position.script,
                    quantity=position.quantity,
                    sl_price=new_sl_rounded,
                    current_ltp=current_ltp
                )

                if not success:
                    last_error = error
                    logger.warning(f"GTT placement failed (attempt {attempt}): {error}")
                    # Wait before retry (except on last attempt)
                    if attempt < self.gtt_max_placement_attempts:
                        time.sleep(1)
                    continue

                # GTT placement succeeded - SAVE IT IMMEDIATELY before verification
                # This prevents duplicate GTTs if verification fails but GTT actually exists
                new_gtt_id = gtt_id
                position.current_gtt_id = str(new_gtt_id)  # Ensure string format
                session.commit()
                logger.info(f"New GTT ID {new_gtt_id} saved to DB for {position.script}")

                # Now verify it exists in Zerodha
                verified, verify_error = self._verify_gtt_exists(
                    gtt_id=gtt_id,
                    script=position.script
                )

                if verified:
                    # SUCCESS!
                    break
                else:
                    # Verification failed but GTT ID already saved to DB
                    # DON'T cancel - GTT might exist but not propagated yet
                    # DON'T retry - would create duplicate GTTs
                    logger.warning(
                        f"⚠️ GTT {gtt_id} verification failed but ID saved to DB: {verify_error}. "
                        f"GTT may exist - will be verified by monitoring. NOT retrying to avoid duplicates."
                    )
                    # Break out of retry loop - GTT ID is saved, monitoring will verify
                    break

            # Check if we successfully placed new GTT (verified or unverified)
            if new_gtt_id is None:
                # CRITICAL: Old GTT cancelled but new one failed verification!
                critical_error = (
                    f"🚨 **CRITICAL: GTT UPDATE FAILED**\n\n"
                    f"**Position Details:**\n"
                    f"• Script: {position.script} ({position.timeframe})\n"
                    f"• Entry Price: Rs.{position.entry_price:.2f}\n"
                    f"• Quantity: {position.quantity}\n"
                    f"• Capital Deployed: Rs.{position.capital_deployed:,.2f}\n\n"
                    f"**GTT Update Issue:**\n"
                    f"• Old SL: Rs.{old_sl:.2f} (GTT {'✓ cancelled' if old_gtt_cancelled else '✗ failed to cancel'})\n"
                    f"• New SL: Rs.{new_sl_rounded:.2f} (✗ GTT placement failed)\n"
                    f"• Placement attempts: {self.gtt_max_placement_attempts}\n"
                    f"• Error: {last_error}\n\n"
                    f"⚠️ **URGENT:** Position is UNPROTECTED!\n"
                    f"**Action:** Manually place GTT @ Rs.{new_sl_rounded:.2f} or exit position"
                )
                logger.critical(critical_error)

                # Send Telegram alert
                telegram.send_alert(critical_error, critical=True)

                # Log failure
                self._log_gtt_update(
                    position=position,
                    old_sl=old_sl,
                    new_sl=new_sl_rounded,
                    old_gtt_id=old_gtt_id,
                    new_gtt_id=None,
                    status="FAILED_VERIFICATION",
                    error_message=last_error,
                    session=session
                )

                return False, critical_error, None

            # Step 3: Update position
            position.current_sl = new_sl_rounded
            position.current_gtt_id = new_gtt_id
            position.sl_movements += 1
            position.last_sl_update = ist_now_naive()

            # Update highest SL if applicable
            if position.highest_sl is None or new_sl_rounded > position.highest_sl:
                position.highest_sl = new_sl_rounded

            # Set initial_sl on first real SL update (replacing dummy/initial SL)
            if position.sl_movements == 1:
                position.initial_sl = new_sl_rounded

            session.commit()

            # Log success
            self._log_gtt_update(
                position=position,
                old_sl=old_sl,
                new_sl=new_sl_rounded,
                old_gtt_id=old_gtt_id,
                new_gtt_id=new_gtt_id,
                status="SUCCESS",
                error_message=None,
                session=session
            )

            logger.info(
                f"✅ GTT updated and verified for {position.script}: "
                f"Rs.{old_sl:.2f} → Rs.{new_sl_rounded:.2f} (GTT: {new_gtt_id})"
            )

            # Return update details for telegram reporting
            update_details = {
                'old_sl': original_sl,
                'new_sl': new_sl_rounded
            }
            return True, None, update_details

        except Exception as e:
            error_msg = f"GTT update error: {str(e)}"
            logger.error(f"Failed to update GTT for {position.script}: {e}")

            # Send critical alert
            critical_error = (
                f"🚨 **CRITICAL: GTT UPDATE ERROR**\n\n"
                f"**Position Details:**\n"
                f"• Script: {position.script} ({position.timeframe})\n"
                f"• Entry Price: Rs.{position.entry_price:.2f}\n"
                f"• Quantity: {position.quantity}\n"
                f"• Current SL: Rs.{position.current_sl:.2f}\n"
                f"• Capital Deployed: Rs.{position.capital_deployed:,.2f}\n\n"
                f"**Error:**\n"
                f"{error_msg}\n\n"
                f"⚠️ **URGENT:** Check position protection status!\n"
                f"**Action:** Verify GTT exists on Zerodha or place manually"
            )
            telegram.send_alert(critical_error, critical=True)

            # Log failure
            self._log_gtt_update(
                position=position,
                old_sl=position.current_sl,
                new_sl=new_sl_rounded if 'new_sl_rounded' in locals() else 0,
                old_gtt_id=position.current_gtt_id,
                new_gtt_id=None,
                status="FAILED",
                error_message=error_msg,
                session=session
            )

            return False, error_msg, None

    def _log_gtt_update(
        self,
        position: OpenPosition,
        old_sl: float,
        new_sl: float,
        old_gtt_id: Optional[str],
        new_gtt_id: Optional[str],
        status: str,
        error_message: Optional[str],
        session: Session
    ):
        """Log GTT update to audit trail"""
        log_entry = GTTUpdateLog(
            bot_instance_id=config.get_bot_instance_id(),
            position_id=position.id,
            script=position.script,
            timeframe=position.timeframe,
            update_date=date.today(),
            old_sl=old_sl,
            new_sl=new_sl,
            old_gtt_id=old_gtt_id,
            new_gtt_id=new_gtt_id,
            status=status,
            error_message=error_message
        )

        session.add(log_entry)
        session.commit()

    def close_position(
        self,
        position: OpenPosition,
        exit_price: float,
        exit_reason: str,
        session: Session
    ) -> ClosedPosition:
        """
        Close position and calculate P&L

        Args:
            position: OpenPosition object
            exit_price: Exit price
            exit_reason: Reason for exit (SL_HIT, MANUAL, etc.)
            session: Database session

        Returns:
            ClosedPosition object
        """
        try:
            # Calculate P&L
            pnl_details = cost_calculator.calculate_pnl(
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=position.quantity
            )

            # Calculate days held
            days_held = (date.today() - position.entry_date).days

            # Create ClosedPosition entry
            closed = ClosedPosition(
                script=position.script,
                timeframe=position.timeframe,
                entry_date=position.entry_date,
                entry_price=position.entry_price,
                quantity=position.quantity,
                capital_deployed=position.capital_deployed,
                exit_date=date.today(),
                exit_price=exit_price,
                exit_reason=exit_reason,
                gross_pnl=pnl_details['gross_pnl'],
                transaction_costs=pnl_details['total_cost'],
                cost_breakdown=pnl_details['cost_breakdown'],
                net_pnl=pnl_details['net_pnl'],
                pnl_percent=pnl_details['pnl_percent'],
                days_held=days_held,
                sl_movements=position.sl_movements,
                highest_sl_achieved=position.highest_sl,
                entry_atr=position.entry_atr
            )

            # Update OpenPosition status
            position.status = PositionStatus.CLOSED_SL if "SL" in exit_reason else PositionStatus.CLOSED_MANUAL
            position.exit_date = date.today()
            position.exit_price = exit_price
            position.exit_reason = exit_reason

            # Add to transaction history
            transaction = TransactionHistory(
                transaction_type=TransactionType.EXIT_SL if "SL" in exit_reason else TransactionType.EXIT_MANUAL,
                script=position.script,
                timeframe=position.timeframe,
                transaction_date=date.today(),
                price=exit_price,
                quantity=position.quantity,
                value=exit_price * position.quantity,
                position_id=position.id,
                notes=f"Exit reason: {exit_reason}"
            )

            session.add(closed)
            session.add(transaction)

            # Update capital ledger - free up slot and update realized P&L
            today = date.today()
            ledger = session.query(CapitalLedger).filter_by(date=today).first()
            if ledger:
                ledger.deployed_capital -= position.capital_deployed
                ledger.free_capital += position.capital_deployed
                ledger.num_open_positions -= 1
                ledger.num_exits_today += 1
                ledger.realized_pnl_today += pnl_details['net_pnl']
                logger.info(
                    f"Capital ledger updated: Released Rs.{position.capital_deployed:.2f}, "
                    f"P&L: Rs.{pnl_details['net_pnl']:+.2f}, Today's realized: Rs.{ledger.realized_pnl_today:+.2f}"
                )

            session.commit()

            logger.info(
                f"Position closed: {position.script} {position.timeframe} - "
                f"Entry=Rs.{position.entry_price:.2f}, Exit=Rs.{exit_price:.2f}, "
                f"P&L=Rs.{pnl_details['net_pnl']:.2f} ({pnl_details['pnl_percent']:.2f}%), "
                f"Days={days_held}, Reason={exit_reason}"
            )

            return closed

        except Exception as e:
            logger.error(f"Failed to close position {position.script}: {e}")
            session.rollback()
            raise

    def update_all_positions_eod(self, session: Optional[Session] = None) -> Dict[str, int]:
        """
        Update all open positions with timeframe-appropriate trailing LOW (EOD workflow at 3:50 PM)

        Logic:
        - Daily positions: Update every day with today's LOW
        - Weekly positions: Update only on Fridays with week's LOW
        - Monthly positions: Update only on last trading day with month's LOW

        Returns:
            Stats dict with update counts
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        stats = {
            'total_positions': 0,
            'updated': 0,
            'no_change': 0,
            'skipped': 0,  # Positions not updated today (wrong day for timeframe)
            'failed': 0,
            'errors': [],
            'updates': []  # List of {script, timeframe, old_sl, new_sl} for detailed reporting
        }

        try:
            # Get all open positions (for this bot)
            open_positions = session.query(OpenPosition).filter_by(
                bot_instance_id=config.get_bot_instance_id(),
                status=PositionStatus.OPEN
            ).all()

            stats['total_positions'] = len(open_positions)

            if not open_positions:
                logger.info("No open positions to update")
                return stats

            today = date.today()
            logger.info(
                f"EOD GTT Update - {today.strftime('%A, %Y-%m-%d')}: "
                f"Processing {len(open_positions)} open positions"
            )

            for position in open_positions:
                try:
                    # Check if we should update this position today based on its timeframe
                    if not self.should_update_gtt_today(position, today):
                        stats['skipped'] += 1
                        continue

                    # Get the appropriate trailing SL for this position's timeframe
                    trailing_sl = self.get_trailing_sl_for_timeframe(position, today)

                    if trailing_sl is None:
                        logger.warning(
                            f"Could not get trailing SL for {position.script} {position.timeframe}, "
                            f"skipping GTT update"
                        )
                        stats['failed'] += 1
                        stats['errors'].append(f"{position.script}: No historical data")
                        continue

                    # Update GTT with trailing SL
                    success, error, update_details = self.update_gtt_with_trailing_sl(position, trailing_sl, session)

                    if success:
                        if error == "no_change":  # Success but no update needed
                            stats['no_change'] += 1
                        elif update_details:  # Actually updated
                            stats['updated'] += 1
                            # Track update details for telegram alert
                            stats['updates'].append({
                                'script': position.script,
                                'timeframe': position.timeframe,
                                'old_sl': update_details['old_sl'],
                                'new_sl': update_details['new_sl'],
                                'entry_price': position.entry_price,
                                'quantity': position.quantity,
                                'entry_atr': position.entry_atr
                            })
                    else:
                        stats['failed'] += 1
                        stats['errors'].append(f"{position.script}: {error}")

                except Exception as e:
                    logger.error(f"Error updating position {position.script} {position.timeframe}: {e}")
                    stats['failed'] += 1
                    stats['errors'].append(f"{position.script}: {str(e)}")

            logger.info(
                f"EOD GTT update complete: Total={stats['total_positions']}, "
                f"Updated={stats['updated']}, NoChange={stats['no_change']}, "
                f"Skipped={stats['skipped']}, Failed={stats['failed']}"
            )

            return stats

        except Exception as e:
            logger.error(f"Error in EOD position update workflow: {e}")
            if close_session:
                session.rollback()
            raise
        finally:
            if close_session:
                session.close()


# Singleton instance
exit_manager = ExitManager()
