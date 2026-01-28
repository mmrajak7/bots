"""
Signal Processor - CSP signal detection and processing

Responsibilities:
- Read signals from op_signals.csv
- Filter for CSP signals only
- Calculate monthly SuperTrend level
- Deduplicate (one signal per script per month)
- Check NIFTY weekly filter
- Queue for Telegram notification
"""

import os
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import pandas as pd
import numpy as np
from loguru import logger

from src.api.dual_kite_client import get_kite_client
from src.models.database import (
    get_session, SignalQueue, OpenPosition, OpenOrder,
    SignalStatus, PositionStatus, OrderStatus, ist_now_naive
)
from src.telegram.bot import telegram
from src.utils.config_manager import config
from src.utils.timezone_helper import today_ist, now_ist


class SignalProcessor:
    """Process CSP signals from CSV file"""

    def __init__(self):
        """Initialize signal processor"""
        self.signals_file = config.get('signals.file_path')
        self.filter_tf = config.get('signals.filter_tf', 'CSP')
        self.kite = get_kite_client()

        # SuperTrend parameters
        self.st_period = config.get('supertrend.period', 10)
        self.st_multiplier = config.get('supertrend.multiplier', 3)

        # Load ignore list (safety feature from CROCODILE)
        self.ignore_list = self._load_ignore_list()

        logger.info(f"Signal processor initialized (file: {self.signals_file}, ignored: {len(self.ignore_list)} scripts)")

    def _load_ignore_list(self) -> set:
        """
        Load ignore list from CSV file.

        Safety feature ported from CROCODILE.
        Scripts in ignore list will be skipped during signal processing.

        Returns:
            Set of script names to ignore
        """
        ignore_file = config.get('signals.ignore_list_file', 'data/ignore_list.csv')

        # Resolve relative path from bot root
        if not os.path.isabs(ignore_file):
            bot_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            ignore_file = os.path.join(bot_root, ignore_file)

        if not os.path.exists(ignore_file):
            logger.debug(f"Ignore list file not found: {ignore_file}")
            return set()

        try:
            df = pd.read_csv(ignore_file)

            # Look for Script column
            script_col = None
            for col in df.columns:
                if col.lower() in ['script', 'symbol', 'scrip']:
                    script_col = col
                    break

            if script_col is None:
                logger.warning("Ignore list file has no Script column")
                return set()

            ignore_set = set(df[script_col].str.upper().str.strip().dropna())

            if ignore_set:
                logger.info(f"Loaded {len(ignore_set)} scripts to ignore: {sorted(ignore_set)}")

            return ignore_set

        except Exception as e:
            logger.error(f"Error loading ignore list: {e}")
            return set()

    def reload_ignore_list(self) -> int:
        """
        Reload ignore list from file.

        Returns:
            Number of scripts in updated ignore list
        """
        self.ignore_list = self._load_ignore_list()
        return len(self.ignore_list)

    def is_ignored(self, script: str) -> bool:
        """
        Check if a script is in the ignore list.

        Args:
            script: Stock symbol

        Returns:
            True if script should be ignored
        """
        return script.upper().strip() in self.ignore_list

    def process_new_signals(self) -> List[Dict[str, Any]]:
        """
        Read CSV, detect new CSP signals, calculate SuperTrend level

        Returns:
            List of new signals added to queue
        """
        new_signals = []

        try:
            # Read signals file
            signals_df = self._read_signals_csv()
            if signals_df is None or signals_df.empty:
                logger.debug("No signals in CSV or file not found")
                return []

            # Filter for CSP signals
            csp_signals = self._filter_csp_signals(signals_df)
            if csp_signals.empty:
                logger.debug("No CSP signals found")
                return []

            # Filter by start date (ignore historical signals before bot start)
            csp_signals = self._filter_by_start_date(csp_signals)
            if csp_signals.empty:
                logger.debug("No CSP signals after start_date filter")
                return []

            # Get unique scripts (dedupe within CSV itself)
            script_col = None
            for col in csp_signals.columns:
                if col.lower() in ['scrip', 'script', 'symbol', 'ticker', 'stock']:
                    script_col = col
                    break

            if script_col:
                unique_scripts = csp_signals[script_col].str.upper().str.strip().unique()
            else:
                unique_scripts = []

            logger.info(f"Found {len(unique_scripts)} unique CSP signals in CSV")

            # Track skip reasons for summary
            skipped_ignored = []
            skipped_duplicate = []
            skipped_calc_failed = []

            # Process each unique script
            for script in unique_scripts:
                if not script:
                    continue

                # Check ignore list (safety feature from CROCODILE)
                if self.is_ignored(script):
                    skipped_ignored.append(script)
                    continue

                # Check if signal already exists for this month
                if self._is_duplicate_signal(script):
                    skipped_duplicate.append(script)
                    continue

                # Calculate monthly SuperTrend level
                signal_level = self._calculate_signal_level(script)
                if signal_level is None:
                    skipped_calc_failed.append(script)
                    continue

                # Add to signal queue
                signal_data = self._add_to_queue(script, signal_level)
                if signal_data:
                    new_signals.append(signal_data)
                    logger.info(f"New signal: {script} @ {signal_level:.2f}")

            # Log skip summary (only if something was skipped)
            if skipped_ignored:
                logger.info(f"Skipped (ignore list): {skipped_ignored}")
            if skipped_duplicate:
                logger.info(f"Skipped (already in queue): {skipped_duplicate}")
            if skipped_calc_failed:
                logger.warning(f"Skipped (SuperTrend calc failed): {skipped_calc_failed}")

        except Exception as e:
            logger.error(f"Error processing signals: {e}")

        return new_signals

    def _read_signals_csv(self) -> Optional[pd.DataFrame]:
        """
        Read signals CSV file.

        Expected columns (case-insensitive):
        - scrip/script/symbol: Stock symbol
        - tf/timeframe/type: Signal timeframe (CSP)
        - date/signal_date: Signal date (for filtering)
        """
        if not self.signals_file or not os.path.exists(self.signals_file):
            logger.warning(f"Signals file not found: {self.signals_file}")
            return None

        try:
            df = pd.read_csv(self.signals_file)

            if df.empty:
                logger.debug("Signals CSV is empty")
                return df

            # Validate required columns exist
            cols_lower = [c.lower() for c in df.columns]

            # Check for script/symbol column
            has_script = any(c in cols_lower for c in ['scrip', 'script', 'symbol', 'ticker', 'stock'])
            if not has_script:
                logger.error(f"Signals CSV missing script column. Found: {list(df.columns)}")
                return None

            # Check for timeframe column (needed for CSP filter)
            has_tf = any(c in cols_lower for c in ['tf', 'timeframe', 'type'])
            if not has_tf:
                logger.warning(f"Signals CSV missing TF column. All signals will be processed.")

            return df
        except Exception as e:
            logger.error(f"Error reading signals CSV: {e}")
            return None

    def _filter_csp_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter DataFrame for CSP signals only"""
        # Look for TF column with CSP value
        tf_columns = [col for col in df.columns if col.lower() in ['tf', 'timeframe', 'type']]

        if not tf_columns:
            logger.warning("No TF/timeframe column found in signals CSV")
            return df  # Return all if no filter column

        tf_col = tf_columns[0]
        filtered = df[df[tf_col].str.upper().str.strip() == self.filter_tf.upper()]

        return filtered

    def _filter_by_start_date(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Filter signals to only include those after configured start_date.

        This allows ignoring historical signals when starting the bot fresh.

        Expected CSV columns for date: 'date', 'signal_date', 'dt', 'timestamp'
        Expected config format: YYYY-MM-DD (e.g., '2026-01-26')
        """
        start_date_str = config.get('signals.start_date')
        if not start_date_str:
            return df  # No filter if not configured

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        except ValueError:
            logger.warning(f"Invalid start_date format: {start_date_str} (expected YYYY-MM-DD)")
            return df

        try:

            # Look for date column in CSV
            date_columns = [col for col in df.columns if col.lower() in ['date', 'signal_date', 'dt', 'timestamp']]

            if not date_columns:
                logger.warning("No date column found in signals CSV, cannot filter by start_date")
                return df

            date_col = date_columns[0]

            # Convert to datetime
            df_copy = df.copy()
            df_copy[date_col] = pd.to_datetime(df_copy[date_col], errors='coerce')

            # Filter
            filtered = df_copy[df_copy[date_col].dt.date >= start_date]

            if len(filtered) < len(df):
                logger.info(f"Date filter: {len(df)} -> {len(filtered)} signals (after {start_date_str})")

            return filtered

        except Exception as e:
            logger.warning(f"Error filtering by start_date: {e}")
            return df

    def _is_duplicate_signal(self, script: str) -> bool:
        """
        Check if signal already exists for this script this month.

        FIX DI-3: Only check for ACTIVE signals that haven't completed their lifecycle.
        A FILLED signal means position was opened and closed - allow new signal.
        """
        session = get_session()
        try:
            today = today_ist()
            current_month = today.strftime('%Y-%m')

            # Only check for signals that are still in progress
            # FILLED means position opened - but if position closed, allow new signal
            # For now, block if any non-terminal signal exists
            active_statuses = [
                SignalStatus.PENDING,
                SignalStatus.NOTIFIED,
                SignalStatus.HOLD,
                SignalStatus.AWAITING_PRICE,
                SignalStatus.APPROVED,
                SignalStatus.ENTERED,
                SignalStatus.FILLED  # Position still open from this signal
            ]

            existing = session.query(SignalQueue).filter(
                SignalQueue.script == script,
                SignalQueue.signal_month == current_month,
                SignalQueue.status.in_(active_statuses)
            ).first()

            return existing is not None
        finally:
            session.close()

    def _calculate_signal_level(self, script: str) -> Optional[float]:
        """Calculate monthly SuperTrend value for signal level"""
        try:
            # Get instrument token
            instrument_token = self.kite.get_instrument_token(script)

            # Get monthly historical data
            years_back = config.get('historical_data.monthly_lookback_years', 3.0)
            df = self.kite.get_historical_data_sampled(
                instrument_token=instrument_token,
                timeframe='monthly',
                years_back=years_back
            )

            if df is None or df.empty:
                return None

            # Calculate SuperTrend
            df_with_st = self._calculate_supertrend(df)

            # FIX TR-C4: Validate DataFrame has sufficient data
            if df_with_st is None or len(df_with_st) < 2:
                logger.warning(
                    f"Insufficient data for SuperTrend calculation for {script}: "
                    f"got {len(df_with_st) if df_with_st is not None else 0} rows, need at least 2"
                )
                return None

            # Get SuperTrend value from the previous COMPLETED candle
            # Current candle (iloc[-1]) is incomplete/running
            # Previous candle (iloc[-2]) is confirmed/completed
            signal_level = float(df_with_st['supertrend'].iloc[-2])
            prev_trend = int(df_with_st['trend'].iloc[-2])
            trend_str = "UP" if prev_trend == 1 else "DOWN"  # trend=1 is UP in TradingView style
            logger.debug(
                f"ST calc for {script}: ST={signal_level:.2f} trend={trend_str}"
            )

            return signal_level

        except Exception as e:
            logger.error(f"Error calculating signal level for {script}: {e}")
            return None

    def _calculate_supertrend(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate SuperTrend indicator (TradingView-compatible)

        SuperTrend 10,3 means: period=10, multiplier=3
        Convention:
        - trend = 1: UPTREND (bullish), SuperTrend = lower band (green support line)
        - trend = -1: DOWNTREND (bearish), SuperTrend = upper band (red resistance line)

        Key: Compare current close to PREVIOUS bands for trend determination
        """
        import numpy as np
        df = df.copy()

        # Calculate ATR using Wilder's smoothing (RMA)
        df['prev_close'] = df['Close'].shift(1)
        df['tr'] = pd.concat([
            df['High'] - df['Low'],
            abs(df['High'] - df['prev_close']),
            abs(df['Low'] - df['prev_close'])
        ], axis=1).max(axis=1)
        atr = df['tr'].ewm(alpha=1/self.st_period, adjust=False).mean()

        # Calculate basic bands
        hl2 = (df['High'] + df['Low']) / 2
        df['basic_lb'] = hl2 - (self.st_multiplier * atr)  # Lower band
        df['basic_ub'] = hl2 + (self.st_multiplier * atr)  # Upper band

        # Initialize arrays
        n = len(df)
        final_lb = np.zeros(n)
        final_ub = np.zeros(n)
        trend = np.zeros(n)
        supertrend = np.zeros(n)

        # First candle
        final_lb[0] = df.iloc[0]['basic_lb']
        final_ub[0] = df.iloc[0]['basic_ub']
        trend[0] = 1  # Start assuming uptrend
        supertrend[0] = final_lb[0]

        for i in range(1, n):
            curr_basic_lb = df.iloc[i]['basic_lb']
            curr_basic_ub = df.iloc[i]['basic_ub']
            prev_close = df.iloc[i-1]['Close']
            curr_close = df.iloc[i]['Close']

            # Final Lower Band: trails up in uptrend
            # TradingView: up := close[1] > up[1] ? max(up, up[1]) : up
            if prev_close > final_lb[i-1]:
                final_lb[i] = max(curr_basic_lb, final_lb[i-1])
            else:
                final_lb[i] = curr_basic_lb

            # Final Upper Band: trails down in downtrend
            # TradingView: dn := close[1] < dn[1] ? min(dn, dn[1]) : dn
            if prev_close < final_ub[i-1]:
                final_ub[i] = min(curr_basic_ub, final_ub[i-1])
            else:
                final_ub[i] = curr_basic_ub

            # Trend determination: compare CURRENT close to PREVIOUS bands
            # TradingView: trend := close > dn[1] ? 1 : close < up[1] ? -1 : trend[1]
            if curr_close > final_ub[i-1]:
                trend[i] = 1   # Switch/stay in UPTREND
            elif curr_close < final_lb[i-1]:
                trend[i] = -1  # Switch/stay in DOWNTREND
            else:
                trend[i] = trend[i-1]  # Keep previous trend

            # Set SuperTrend value based on trend
            if trend[i] == 1:
                supertrend[i] = final_lb[i]  # Uptrend: ST is lower band (support)
            else:
                supertrend[i] = final_ub[i]  # Downtrend: ST is upper band (resistance)

        # Assign to dataframe
        df['final_lb'] = final_lb
        df['final_ub'] = final_ub
        df['supertrend'] = supertrend
        df['trend'] = trend

        return df

    def _add_to_queue(self, script: str, signal_level: float) -> Optional[Dict[str, Any]]:
        """Add new signal to queue"""
        session = get_session()
        try:
            today = today_ist()
            current_month = today.strftime('%Y-%m')

            signal = SignalQueue(
                script=script,
                signal_date=today,
                signal_level=signal_level,
                status=SignalStatus.PENDING,
                first_seen_at=ist_now_naive(),
                signal_month=current_month
            )

            session.add(signal)
            session.commit()

            return {
                'id': signal.id,
                'script': script,
                'signal_level': signal_level,
                'status': SignalStatus.PENDING.value
            }

        except Exception as e:
            logger.error(f"Error adding signal to queue: {e}")
            session.rollback()
            return None
        finally:
            session.close()

    def check_nifty_weekly_filter(self) -> bool:
        """
        Check NIFTY weekly SuperTrend for filter.

        FIX TR-M2: Returns False (block) on error instead of True (allow).
        Conservative approach - don't take entries when uncertain.

        Returns:
            True if NIFTY weekly trend is bullish (allow entries)
            False if bearish OR on error (block entries)
        """
        if not config.get('nifty_filter.enabled', True):
            return True

        try:
            nifty_token = config.get('nifty_filter.instrument_token', '256265')

            # Get weekly data
            df = self.kite.get_historical_data_sampled(
                instrument_token=nifty_token,
                timeframe='weekly',
                years_back=1.5
            )

            if df is None or df.empty:
                # FIX TR-M2: Block entries if can't determine NIFTY trend
                logger.warning("Could not fetch NIFTY weekly data - BLOCKING entries (safety)")
                return False

            # Calculate SuperTrend
            df_with_st = self._calculate_supertrend(df)

            # Check trend: trend == 1 means UPTREND (bullish), trend == -1 means DOWNTREND (bearish)
            current_trend = int(df_with_st['trend'].iloc[-1])
            is_bullish = current_trend == 1

            logger.info(f"NIFTY weekly filter: trend={current_trend}, {'BULLISH (allow)' if is_bullish else 'BEARISH (block)'}")
            return is_bullish

        except Exception as e:
            # FIX TR-M2: Block entries on error (safer than allowing)
            logger.error(f"Error checking NIFTY filter: {e} - BLOCKING entries (safety)")
            return False

    def can_send_notification(self) -> Tuple[bool, str]:
        """
        Check if we can send new signal notifications

        Returns:
            (can_notify, reason)
        """
        session = get_session()
        try:
            # Check position count
            max_positions = config.get('trading.max_positions', 5)
            open_count = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).count()

            if open_count >= max_positions:
                return False, f"Max positions reached ({open_count}/{max_positions})"

            # Check pending orders count
            max_pending = config.get('trading.max_pending_orders', 3)
            pending_count = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).count()

            if pending_count >= max_pending:
                return False, f"Max pending orders reached ({pending_count}/{max_pending})"

            # Check NIFTY filter
            if not self.check_nifty_weekly_filter():
                return False, "NIFTY weekly filter bearish"

            return True, ""

        finally:
            session.close()

    def get_pending_notifications(self) -> List[Dict[str, Any]]:
        """Get signals pending notification"""
        session = get_session()
        try:
            signals = session.query(SignalQueue).filter(
                SignalQueue.status == SignalStatus.PENDING,
                SignalQueue.telegram_msg_id.is_(None)
            ).all()

            result = []
            for sig in signals:
                result.append({
                    'id': sig.id,
                    'script': sig.script,
                    'signal_level': sig.signal_level,
                    'signal_date': sig.signal_date,
                    'status': sig.status.value
                })

            return result

        finally:
            session.close()

    def send_signal_notification(self, signal_id: int) -> bool:
        """Send Telegram notification for a signal"""
        session = get_session()
        try:
            signal = session.query(SignalQueue).filter(
                SignalQueue.id == signal_id
            ).first()

            if not signal:
                return False

            script = signal.script
            signal_level = signal.signal_level

            # DUPLICATE POSITION CHECK (safety feature from CROCODILE)
            # Skip notification if we already have position in this script
            existing_position = session.query(OpenPosition).filter(
                OpenPosition.script == script,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if existing_position:
                logger.info(f"{script}: Skipping notification - already have position")
                # Mark signal as rejected to avoid re-processing
                signal.status = SignalStatus.REJECTED
                signal.rejection_reason = f"Already holding position (Entry: {existing_position.entry_price:.2f})"
                session.commit()
                return False

            # DUPLICATE PENDING ORDER CHECK
            # Skip notification if we already have pending order for this script
            existing_order = session.query(OpenOrder).filter(
                OpenOrder.script == script,
                OpenOrder.status == OrderStatus.PENDING
            ).first()

            if existing_order:
                logger.info(f"{script}: Skipping notification - already have pending order")
                # Mark signal as rejected to avoid re-processing
                signal.status = SignalStatus.REJECTED
                signal.rejection_reason = f"Already have pending order (GTT: {existing_order.gtt_id})"
                session.commit()
                return False

            # IGNORE LIST CHECK (safety feature from CROCODILE)
            if self.is_ignored(script):
                logger.info(f"{script}: Skipping notification - in ignore list")
                signal.status = SignalStatus.REJECTED
                signal.rejection_reason = "In ignore list"
                session.commit()
                return False

            # Get current LTP
            try:
                instrument_token = self.kite.get_instrument_token(script)
                ltp = self.kite.get_instrument_ltp(instrument_token)
            except Exception as e:
                logger.error(f"Could not get LTP for {script}: {e}")
                ltp = signal_level

            # Calculate quantity
            per_trade = config.get('trading.per_trade_amount', 20000)
            quantity = int(per_trade / signal_level) if signal_level > 0 else 1

            if config.is_test_mode():
                quantity = 1

            position_value = quantity * signal_level

            # Get available capital
            initial_capital = config.get('trading.initial_capital', 100000)
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            deployed = sum(p.capital_deployed for p in positions)
            available = initial_capital - deployed

            # Send notification
            msg_id = telegram.send_signal_notification(
                signal_id=signal.id,
                script=script,
                signal_level=signal_level,
                ltp=ltp,
                quantity=quantity,
                position_value=position_value,
                available_capital=available
            )

            if msg_id:
                signal.status = SignalStatus.NOTIFIED
                signal.telegram_msg_id = msg_id
                signal.last_notified_at = ist_now_naive()
                session.commit()
                return True

            return False

        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            session.rollback()
            return False
        finally:
            session.close()

    def resend_hold_signals(self) -> int:
        """Re-send notifications for signals on HOLD"""
        session = get_session()
        try:
            hold_signals = session.query(SignalQueue).filter(
                SignalQueue.status == SignalStatus.HOLD
            ).all()

            count = 0
            for signal in hold_signals:
                # Reset status to pending for re-notification
                signal.status = SignalStatus.PENDING
                signal.telegram_msg_id = None
                count += 1

            session.commit()
            logger.info(f"Reset {count} HOLD signals for re-notification")
            return count

        finally:
            session.close()


# Singleton instance
signal_processor = SignalProcessor()
