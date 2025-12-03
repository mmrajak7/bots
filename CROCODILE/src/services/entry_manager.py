"""Entry Manager - Signal Processing and Order Execution"""

import os
import csv
from datetime import datetime, date, timedelta
from typing import List, Dict, Tuple, Optional
from loguru import logger
from sqlalchemy.orm import Session

from src.models.database import ProcessedSignal, OpenOrder, OpenPosition, ClosedPosition, TransactionHistory, get_session, OrderStatus
from src.services.capital_manager import capital_manager
from src.indicators.supertrend_calculator import SuperTrendCalculator
from src.indicators.nifty_data_fetcher import NiftyDataFetcher
from src.indicators.nifty_trend_filter import NiftyTrendFilter
from src.api.kite_trade_client import KiteTradeClient
from src.utils.config_manager import config
from src.utils.price_rounder import round_price, PriceRounder
from src.reporting.telegram_client import telegram


class Signal:
    """Signal data class"""
    def __init__(self, date_str: str, script: str, timeframe: str, status: str = ''):
        self.date = datetime.strptime(date_str, '%Y-%m-%d').date()
        self.script = script.upper().strip()
        self.timeframe = timeframe.upper().strip()
        self.status = status.strip() if status else ''

    def __repr__(self):
        return f"Signal(date={self.date}, script={self.script}, tf={self.timeframe})"


class EntryManager:
    """
    Manages signal processing and entry order execution

    Workflow:
    1. Read signals from CSV
    2. Check duplicates (already processed)
    3. Validate signal (ST + NIFTY filter)
    4. Check capital availability
    5. Place order (LIMIT or MARKET)
    6. Track in database
    """

    def __init__(self):
        """Initialize entry manager"""
        self.kite_client = KiteTradeClient()
        self.st_calculator = SuperTrendCalculator()
        self.nifty_fetcher = NiftyDataFetcher()
        self.nifty_filter = NiftyTrendFilter()

        self.signals_file = config.get('signals.csv_file', 'data/signals.csv')
        self.ignore_list_file = config.get('signals.ignore_list_file', 'data/ignore_list.csv')

        self.ignore_list = self._load_ignore_list()

        logger.info("Entry Manager initialized")

    def _load_ignore_list(self) -> set:
        """Load ignore list from CSV"""
        ignore_set = set()

        if not os.path.exists(self.ignore_list_file):
            logger.info(f"Ignore list not found: {self.ignore_list_file}")
            return ignore_set

        try:
            with open(self.ignore_list_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    script = row.get('Script', '').strip().upper()
                    if script:
                        ignore_set.add(script)

            logger.info(f"Loaded {len(ignore_set)} scripts in ignore list")
            return ignore_set

        except Exception as e:
            logger.error(f"Failed to load ignore list: {e}")
            return ignore_set

    def read_signals_from_csv(self) -> List[Signal]:
        """
        Read signals from CSV file with comprehensive error handling

        Expected format: Date,Script,TF
        Example: 2024-04-29,VOLTAS,D

        Returns:
            List of Signal objects
        """
        signals = []

        if not os.path.exists(self.signals_file):
            logger.warning(f"Signals file not found: {self.signals_file}")
            return signals

        try:
            # Read entire file first for validation (atomic read)
            with open(self.signals_file, 'r') as f:
                content = f.read()

            # Validate file is not empty
            if not content or not content.strip():
                logger.error(f"Signals file is empty: {self.signals_file}")
                telegram.send_alert(
                    f"⚠️ **CSV READ ERROR**\n"
                    f"Signals file is empty: {self.signals_file}\n"
                    f"No signals will be processed."
                )
                return signals

            # Parse CSV from content
            from io import StringIO
            reader = csv.DictReader(StringIO(content))

            # Validate required columns exist
            required_columns = {'Date', 'Script', 'TF'}
            if reader.fieldnames is None:
                logger.error(f"CSV file has no header row: {self.signals_file}")
                telegram.send_alert(
                    f"⚠️ **CSV READ ERROR**\n"
                    f"Signals file has no header row\n"
                    f"Expected: Date,Script,TF\n"
                    f"File: {self.signals_file}"
                )
                return signals

            actual_columns = set(reader.fieldnames)
            missing_columns = required_columns - actual_columns

            if missing_columns:
                logger.error(
                    f"CSV missing required columns: {missing_columns}. "
                    f"Found: {actual_columns}"
                )
                telegram.send_alert(
                    f"⚠️ **CSV READ ERROR**\n"
                    f"Missing columns: {', '.join(missing_columns)}\n"
                    f"Expected: Date, Script, TF\n"
                    f"Found: {', '.join(actual_columns)}\n"
                    f"File: {self.signals_file}"
                )
                return signals

            # Parse rows and track stats
            total_rows = 0
            invalid_rows = 0
            already_processed = 0
            has_status_column = 'Status' in actual_columns

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (after header)
                total_rows += 1
                try:
                    # Get status (empty = new signal, needs processing)
                    status = row.get('Status', '').strip() if has_status_column else ''
                    
                    # Skip signals that already have a status (already processed)
                    if status:
                        already_processed += 1
                        continue
                    
                    signal = Signal(
                        date_str=row['Date'],
                        script=row['Script'],
                        timeframe=row['TF'],
                        status=status
                    )
                    signals.append(signal)
                except Exception as e:
                    invalid_rows += 1
                    logger.warning(f"Invalid signal at row {row_num}: {row} - {e}")
                    continue

            # Report results
            new_signals = len(signals)
            logger.info(
                f"CSV read complete: {new_signals} new signals to process "
                f"(skipped {already_processed} already processed, {invalid_rows} invalid out of {total_rows} total)"
            )

            # Alert only if there are many invalid rows (>20% of total)
            # Note: new_signals=0 is normal when all signals already processed
            if total_rows > 0 and invalid_rows > 0 and (invalid_rows / total_rows) > 0.2:
                logger.warning(
                    f"High invalid rate: {invalid_rows}/{total_rows} "
                    f"({invalid_rows/total_rows*100:.1f}%)"
                )

            return signals

        except FileNotFoundError:
            logger.error(f"Signals file not found during read: {self.signals_file}")
            telegram.send_alert(
                f"⚠️ **CSV READ ERROR**\n"
                f"File not found: {self.signals_file}"
            )
            return []

        except PermissionError:
            logger.error(f"Permission denied reading signals file: {self.signals_file}")
            telegram.send_alert(
                f"⚠️ **CSV READ ERROR**\n"
                f"Permission denied: {self.signals_file}\n"
                f"Check file permissions"
            )
            return []

        except Exception as e:
            logger.error(f"Failed to read signals CSV: {e}")
            telegram.send_alert(
                f"⚠️ **CSV READ ERROR**\n"
                f"File: {self.signals_file}\n"
                f"Error: {str(e)}\n"
                f"No signals will be processed"
            )
            return []


    def update_signal_status_in_csv(self, signal: Signal, status: str) -> bool:
        """
        Update the status of a signal in the CSV file
        
        Status codes:
            S = Success (order placed)
            R = Rejected (validation failed)
            D = Duplicate (already exists)
            I = Ignored (in ignore list)
            X = Expired (signal too old)
            E = Error (processing failed)
        
        Args:
            signal: Signal object to update
            status: Status code to set
            
        Returns:
            True if update successful, False otherwise
        """
        import pandas as pd
        import tempfile
        import shutil
        
        try:
            # Read current CSV
            df = pd.read_csv(self.signals_file)
            
            # Ensure Status column exists
            if 'Status' not in df.columns:
                df['Status'] = ''
            
            # Find matching row(s) - match by Date, Script, TF
            mask = (
                (df['Date'] == str(signal.date)) & 
                (df['Script'] == signal.script) & 
                (df['TF'] == signal.timeframe)
            )
            
            matches = df[mask]
            
            if len(matches) == 0:
                logger.warning(f"Signal not found in CSV for status update: {signal}")
                return False
            
            # Update status for matching rows
            df.loc[mask, 'Status'] = status
            
            # Write atomically using temp file
            temp_file = self.signals_file + '.tmp'
            df.to_csv(temp_file, index=False)
            shutil.move(temp_file, self.signals_file)
            
            logger.debug(f"Updated CSV status: {signal.script} ({signal.timeframe}) -> {status}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to update signal status in CSV: {e}")
            return False

    def is_signal_already_processed(self, signal: Signal, session: Session) -> bool:
        """Check if signal already processed (by this bot)"""
        existing = session.query(ProcessedSignal).filter_by(
            bot_instance_id=config.get_bot_instance_id(),
            date=signal.date,
            script=signal.script,
            timeframe=signal.timeframe
        ).first()

        return existing is not None

    def calculate_cooldown_days(self, timeframe: str) -> int:
        """
        Calculate cooldown period in days based on timeframe

        Logic: 7 candles cooldown
        - Daily: 7 daily candles = 7 days
        - Weekly: 7 weekly candles = 7 weeks = 49 days
        - Monthly: 7 monthly candles = 7 months = 210 days

        Args:
            timeframe: 'D', 'W', or 'M'

        Returns:
            Number of days for cooldown period
        """
        tf_upper = timeframe.upper()

        if tf_upper == 'D':
            return 7  # 7 daily candles
        elif tf_upper == 'W':
            return 49  # 7 weekly candles (7 weeks)
        elif tf_upper == 'M':
            return 210  # 7 monthly candles (~7 months)
        else:
            logger.warning(f"Unknown timeframe '{timeframe}', defaulting to 7 days cooldown")
            return 7

    def is_duplicate_position_or_order(self, script: str, timeframe: str, session: Session) -> Tuple[bool, str]:
        """
        Check if we already have position, pending order, or recent exit for this script+timeframe

        Cooldown logic (7 candles):
        - Daily: 7 days
        - Weekly: 7 weeks (49 days)
        - Monthly: 7 months (210 days)

        Returns:
            (is_duplicate, reason)
        """
        # Check open positions (by this bot)
        open_pos = session.query(OpenPosition).filter_by(
            bot_instance_id=config.get_bot_instance_id(),
            script=script,
            timeframe=timeframe,
            status='OPEN'
        ).first()

        if open_pos:
            return True, f"Open position exists for {script} {timeframe}"

        # Check pending orders (by this bot)
        pending_order = session.query(OpenOrder).filter_by(
            bot_instance_id=config.get_bot_instance_id(),
            script=script,
            timeframe=timeframe,
            status=OrderStatus.PENDING
        ).first()

        if pending_order:
            return True, f"Pending order exists for {script} {timeframe}"

        # Check for recent exit within cooldown period (7 candles)
        cooldown_days = self.calculate_cooldown_days(timeframe)
        cooldown_date = date.today() - timedelta(days=cooldown_days)

        recent_exit = session.query(ClosedPosition).filter(
            ClosedPosition.script == script,
            ClosedPosition.timeframe == timeframe,
            ClosedPosition.exit_date >= cooldown_date
        ).first()

        if recent_exit:
            days_since_exit = (date.today() - recent_exit.exit_date.date()).days
            return True, (
                f"Recently exited {script} {timeframe} ({days_since_exit} days ago, "
                f"cooldown: {cooldown_days} days / 7 candles)"
            )

        return False, ""

    def validate_signal(self, signal: Signal, session: Session) -> Tuple[bool, str, Optional[float]]:
        """
        Validate signal using SuperTrend and NIFTY filter

        Returns:
            (is_valid, reason, st_price)
        """
        try:
            # Step 1: Validate NIFTY weekly filter
            logger.info(f"Validating NIFTY filter for {signal}")

            is_bullish = self.nifty_filter.is_nifty_weekly_bullish()

            if not is_bullish:
                reason = "NIFTY weekly filter failed (NIFTY not above weekly ST)"
                logger.info(f"Signal rejected: {reason}")
                return False, reason, None

            # Step 2: Calculate SuperTrend for the stock
            # Use completed candle only (ignore current incomplete candle during market hours)
            logger.info(f"Calculating SuperTrend for {signal.script} ({signal.timeframe})")

            is_valid, st_price, metadata = self.st_calculator.verify_signal(
                script=signal.script,
                timeframe=signal.timeframe,
                signal_date=signal.date.strftime('%Y-%m-%d'),
                use_completed_candle_only=True  # Ignore incomplete current candle during market hours
            )

            if not is_valid:
                reason = f"SuperTrend validation failed: {metadata.get('trend', 'N/A')}"
                logger.info(f"Signal rejected: {reason}")
                return False, reason, None

            logger.info(
                f"Signal VALID: {signal.script} ({signal.timeframe}) - ST={st_price:.2f}, "
                f"Close={metadata.get('latest_close', 0):.2f}"
            )

            return True, "Valid signal", st_price

        except Exception as e:
            reason = f"Validation error: {str(e)}"
            logger.error(f"Signal validation failed for {signal}: {e}")
            return False, reason, None

    def place_entry_order(
        self,
        signal: Signal,
        st_price: float,
        quantity: int,
        capital_deployed: float,
        session: Session
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Place entry order (LIMIT or MARKET based on current price)

        Includes comprehensive price validation to prevent invalid orders

        Returns:
            (success, message, order_id)
        """
        try:
            # Get current LTP
            instrument_token = self.kite_client.get_instrument_token(signal.script)
            current_ltp = self.kite_client.get_instrument_ltp(instrument_token)

            # === VALIDATION 1: LTP sanity checks ===
            if current_ltp is None:
                error_msg = f"LTP is None for {signal.script}"
                logger.error(error_msg)
                return False, error_msg, None

            if current_ltp <= 0:
                error_msg = f"Invalid LTP (≤0) for {signal.script}: Rs.{current_ltp}"
                logger.error(error_msg)
                return False, error_msg, None

            if current_ltp < 0.05:
                error_msg = f"LTP below minimum tick size for {signal.script}: Rs.{current_ltp}"
                logger.error(error_msg)
                return False, error_msg, None

            # === VALIDATION 2: SuperTrend price sanity checks ===
            if st_price <= 0:
                error_msg = f"Invalid SuperTrend price (≤0) for {signal.script}: Rs.{st_price}"
                logger.error(error_msg)
                return False, error_msg, None

            # Round ST price to tick size
            st_price_rounded = round_price(st_price)

            # === VALIDATION 3: Price relationship sanity check ===
            # If LTP is 10x higher or lower than ST, something is wrong
            price_ratio = current_ltp / st_price_rounded if st_price_rounded > 0 else 0
            if price_ratio > 10 or price_ratio < 0.1:
                error_msg = (
                    f"Suspicious price difference for {signal.script}: "
                    f"LTP=Rs.{current_ltp:.2f}, ST=Rs.{st_price_rounded:.2f} "
                    f"(ratio: {price_ratio:.2f}x)"
                )
                logger.error(error_msg)
                return False, error_msg, None

            # === VALIDATION 4: Tick size validation ===
            # Re-round to ensure tick size compliance (handles float precision issues)
            # Use dynamic tick size based on NSE rules
            tick_size = PriceRounder.get_nse_tick_size(st_price_rounded)
            remainder = abs(st_price_rounded % tick_size)
            # Check if remainder is close to 0 OR close to tick_size (due to float precision)
            is_valid_tick = (remainder < 0.001) or (abs(remainder - tick_size) < 0.001)
            if not is_valid_tick:
                error_msg = (
                    f"ST price not multiple of tick size (Rs.{tick_size}) for {signal.script}: "
                    f"Rs.{st_price_rounded} (remainder: {remainder})"
                )
                logger.error(error_msg)
                return False, error_msg, None

            # === VALIDATION 5: Quantity validation ===
            if quantity <= 0:
                error_msg = f"Invalid quantity (≤0) for {signal.script}: {quantity}"
                logger.error(error_msg)
                return False, error_msg, None

            # === VALIDATION 6: Minimum order value ===
            min_order_value = 10  # Rs.10 minimum (NSE requirement)
            order_value = st_price_rounded * quantity
            if order_value < min_order_value:
                error_msg = (
                    f"Order value too small for {signal.script}: "
                    f"Rs.{order_value:.2f} (min: Rs.{min_order_value})"
                )
                logger.error(error_msg)
                return False, error_msg, None

            # All validations passed - proceed with order placement
            logger.debug(
                f"Price validation passed for {signal.script}: "
                f"LTP=Rs.{current_ltp:.2f}, ST=Rs.{st_price_rounded:.2f}, "
                f"Qty={quantity}, Value=Rs.{order_value:.2f}"
            )

            # Determine order type
            if current_ltp >= st_price_rounded:
                # Case 1: Price above ST → Place LIMIT order at ST level
                order_type = "LIMIT"
                order_price = st_price_rounded
                logger.info(
                    f"Placing LIMIT order: {signal.script} {quantity} shares @ Rs.{order_price:.2f} "
                    f"(LTP=Rs.{current_ltp:.2f})"
                )
            else:
                # Case 2: Price below ST → Place MARKET order (better entry)
                order_type = "MARKET"
                order_price = None
                logger.info(
                    f"Placing MARKET order: {signal.script} {quantity} shares "
                    f"(LTP=Rs.{current_ltp:.2f} < ST=Rs.{st_price_rounded:.2f})"
                )

            # Place order via Kite API (with tick size error retry)
            max_retries = 1
            retry_count = 0
            last_error = None

            while retry_count <= max_retries:
                try:
                    order_response = self.kite_client.place_order(
                        tradingsymbol=signal.script,
                        exchange="NSE",
                        transaction_type="BUY",
                        quantity=quantity,
                        order_type=order_type,
                        price=order_price,
                        product="CNC"  # Delivery
                    )

                    order_id = order_response.get('order_id')

                    if not order_id:
                        raise Exception("No order_id in response")

                    # Success - break out of retry loop
                    break

                except Exception as order_error:
                    error_msg = str(order_error)
                    last_error = order_error

                    # Check if it's a tick size error
                    if retry_count == 0 and "tick size" in error_msg.lower():
                        # Extract tick size from error message
                        # Example: "Tick size for this script is 0.10. Kindly enter price..."
                        import re
                        tick_match = re.search(r'tick size.*?is\s+(0\.\d+)', error_msg, re.IGNORECASE)

                        if tick_match and order_type == "LIMIT":
                            required_tick = float(tick_match.group(1))
                            logger.warning(
                                f"Tick size error for {signal.script}. "
                                f"Zerodha requires {required_tick}, retrying with corrected price..."
                            )

                            # Re-round the price with correct tick size
                            order_price = PriceRounder.round_to_tick(st_price_rounded, tick_size=required_tick)

                            logger.info(
                                f"Corrected price: Rs.{st_price_rounded:.2f} → Rs.{order_price:.2f} "
                                f"(tick size: {required_tick})"
                            )

                            retry_count += 1
                            continue  # Retry with corrected price

                    # Not a tick size error or already retried - raise the error
                    raise last_error

            # After successful order placement
            if not order_id:
                raise Exception("Failed to get order_id after retries")

            # Create OpenOrder entry
            open_order = OpenOrder(
                bot_instance_id=config.get_bot_instance_id(),
                script=signal.script,
                timeframe=signal.timeframe,
                order_id=order_id,
                order_type=order_type,
                order_price=order_price if order_type == "LIMIT" else current_ltp,
                quantity=quantity,
                capital_deployed=capital_deployed,
                status=OrderStatus.PENDING
            )

            session.add(open_order)
            session.commit()

            logger.info(f"Order placed successfully: {order_id}")
            return True, f"Order placed: {order_id}", order_id

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Failed to place order for {signal.script}: {error_msg}")

            # Send Telegram alert for order failure with meaningful details
            telegram.send_alert(
                f"❌ **ORDER PLACEMENT FAILED**\n\n"
                f"**Symbol:** {signal.script}\n"
                f"**Timeframe:** {signal.timeframe}\n"
                f"**Error:**\n{error_msg}\n\n"
                f"Please check logs for details.",
                critical=False
            )

            return False, f"Order placement failed: {error_msg}", None

    def check_duplicate_order(
        self,
        script: str,
        signal_date: date,
        timeframe: str,
        session: Session
    ) -> Dict[str, any]:
        """
        3-Layer duplicate detection to prevent duplicate orders

        Checks:
        1. ProcessedSignal table (signal already processed?)
        2. OpenOrder table (order already exists in DB?)
        3. Zerodha API (order already placed today?)

        Returns:
            {
                'is_duplicate': bool,
                'reason': str,
                'existing_order_id': str (if found),
                'source': 'database' | 'zerodha' | 'processed_signal'
            }
        """
        # Layer 1: Check ProcessedSignal table for already processed signals (by this bot)
        processed = session.query(ProcessedSignal).filter(
            ProcessedSignal.bot_instance_id == config.get_bot_instance_id(),
            ProcessedSignal.date == signal_date,
            ProcessedSignal.script == script,
            ProcessedSignal.timeframe == timeframe,
            ProcessedSignal.processing_status.in_(['SUCCESS', 'PROCESSING'])
        ).first()

        if processed:
            return {
                'is_duplicate': True,
                'reason': f'Signal already processed with status: {processed.processing_status}',
                'existing_order_id': processed.order_id,
                'source': 'processed_signal'
            }

        # Layer 2: Check OpenOrder table for existing orders (by this bot)
        today_start = datetime.combine(signal_date, datetime.min.time())
        today_end = datetime.combine(signal_date, datetime.max.time())

        order = session.query(OpenOrder).filter(
            OpenOrder.bot_instance_id == config.get_bot_instance_id(),
            OpenOrder.script == script,
            OpenOrder.timeframe == timeframe,
            OpenOrder.placed_at >= today_start,
            OpenOrder.placed_at <= today_end
        ).first()

        if order:
            return {
                'is_duplicate': True,
                'reason': 'Order already exists in database',
                'existing_order_id': order.order_id,
                'source': 'database'
            }

        # Layer 3: Check Zerodha API for today's orders
        try:
            zerodha_orders = self.kite_client.get_all_orders()
            today_orders = [
                o for o in zerodha_orders
                if script in o.get('tradingsymbol', '')
                and str(signal_date) in o.get('order_timestamp', '')
                and o.get('status') in ['OPEN', 'COMPLETE', 'TRIGGER PENDING']
            ]

            if today_orders:
                return {
                    'is_duplicate': True,
                    'reason': 'Order already exists on Zerodha',
                    'existing_order_id': today_orders[0]['order_id'],
                    'source': 'zerodha'
                }
        except Exception as e:
            logger.warning(f"Could not check Zerodha for duplicates: {e}")
            # Continue - better to potentially place duplicate than skip valid order

        # No duplicate found
        return {
            'is_duplicate': False,
            'reason': 'No duplicate found',
            'existing_order_id': None,
            'source': None
        }

    def process_signal(self, signal: Signal, session: Session) -> Tuple[bool, str]:
        """
        Process a single signal through complete workflow with TWO-PHASE COMMIT

        Phase 1: Mark as PROCESSING
        Phase 2: Execute workflow
        Phase 3: Mark as SUCCESS or FAILED

        This ensures no duplicates even if script crashes mid-processing

        Returns:
            (success, message)
        """
        logger.info(f"Processing signal: {signal}")

        # ====== DUPLICATE DETECTION (3-Layer Check) ======
        dup_check = self.check_duplicate_order(
            signal.script, signal.date, signal.timeframe, session
        )

        if dup_check['is_duplicate']:
            logger.warning(
                f"DUPLICATE DETECTED: {signal.script} - {dup_check['reason']} "
                f"(Source: {dup_check['source']}, Order ID: {dup_check['existing_order_id']})"
            )
            return False, f"Duplicate: {dup_check['reason']}"

        # ====== PHASE 1: MARK AS PROCESSING ======
        # Create record IMMEDIATELY to prevent duplicates
        from src.utils.timezone_helper import ist_now_naive

        processed_signal = ProcessedSignal(
            bot_instance_id=config.get_bot_instance_id(),
            date=signal.date,
            script=signal.script,
            timeframe=signal.timeframe,
            processing_status='PROCESSING',  # Key status - prevents duplicates!
            started_at=ist_now_naive(),
            signal_valid=False,  # Will update after validation
            entry_attempted=False,
            entry_successful=False
        )

        session.add(processed_signal)
        session.commit()  # COMMIT IMMEDIATELY

        logger.info(f"✅ Marked signal as PROCESSING: {signal}")

        try:
            # ====== PHASE 2: EXECUTE WORKFLOW ======

            # Check ignore list
            if signal.script in self.ignore_list:
                logger.info(f"Script in ignore list: {signal.script}")
                processed_signal.processing_status = 'SKIPPED'
                processed_signal.rejection_reason = "In ignore list"
                processed_signal.completed_at = ist_now_naive()
                session.commit()
                return False, "Script in ignore list"

            # Check for duplicate position/order (old method for backwards compatibility)
            is_dup, dup_reason = self.is_duplicate_position_or_order(signal.script, signal.timeframe, session)
            if is_dup:
                logger.info(f"Duplicate position/order: {dup_reason}")
                processed_signal.processing_status = 'SKIPPED'
                processed_signal.rejection_reason = dup_reason
                processed_signal.completed_at = ist_now_naive()
                session.commit()
                return False, dup_reason

            # Validate signal
            is_valid, reason, st_price = self.validate_signal(signal, session)

            processed_signal.signal_valid = is_valid
            session.commit()

            if not is_valid:
                processed_signal.processing_status = 'FAILED'
                processed_signal.rejection_reason = reason
                processed_signal.completed_at = ist_now_naive()
                session.commit()
                return False, reason

            # Check capital availability
            can_take, cap_reason, quantity, capital_deployed = capital_manager.can_take_new_position(
                entry_price=st_price,
                session=session
            )

            if not can_take:
                # IMPORTANT: Distinguish between position limit (retry later) vs real errors (mark failed)
                is_position_limit = ("position limit reached" in cap_reason.lower() or
                                    "pending order limit reached" in cap_reason.lower())

                if is_position_limit:
                    # Position limit reached - LEAVE BLANK for retry in next cycle
                    logger.warning(f"Position limit reached, will retry next cycle: {cap_reason}")
                    # Don't mark as FAILED - signal stays blank and will be retried
                    return False, cap_reason
                else:
                    # Real error (insufficient margin, etc.) - Mark as FAILED
                    logger.warning(f"Cannot take position (permanent error): {cap_reason}")
                    processed_signal.processing_status = 'FAILED'
                    processed_signal.rejection_reason = cap_reason
                    processed_signal.completed_at = ist_now_naive()
                    session.commit()
                    return False, cap_reason

            # ====== CRITICAL POINT: PLACING ORDER ON ZERODHA ======

            # SAFETY CHECK: Never place orders after 15:29:59
            current_time = ist_now_naive().time()
            from datetime import time as dt_time
            ORDER_CUTOFF_TIME = dt_time(15, 29, 59)  # 15:29:59

            if current_time > ORDER_CUTOFF_TIME:
                logger.warning(f"Order blocked - past cutoff time 15:29:59 (current: {current_time})")
                processed_signal.processing_status = 'FAILED'
                processed_signal.rejection_reason = f"Past order cutoff time (15:29:59), current: {current_time}"
                processed_signal.completed_at = ist_now_naive()
                session.commit()
                return False, "Order blocked - market closing"

            processed_signal.entry_attempted = True
            session.commit()

            success, msg, order_id = self.place_entry_order(
                signal, st_price, quantity, capital_deployed, session
            )

            # ====== PHASE 3: UPDATE FINAL STATUS ======
            if success:
                processed_signal.processing_status = 'SUCCESS'
                processed_signal.entry_successful = True
                processed_signal.order_id = order_id
                processed_signal.completed_at = ist_now_naive()
                session.commit()

                logger.info(f"✅ Signal processed successfully: {signal} → Order {order_id}")
                return True, f"Order placed: {order_id}"
            else:
                processed_signal.processing_status = 'FAILED'
                processed_signal.entry_successful = False
                processed_signal.rejection_reason = msg
                processed_signal.failure_count += 1
                processed_signal.last_error = msg
                processed_signal.completed_at = ist_now_naive()
                session.commit()

                logger.warning(f"❌ Signal processed but order failed: {signal} → {msg}")
                return False, msg

        except Exception as e:
            # ====== ERROR HANDLING: MARK AS FAILED ======
            logger.error(f"Error processing signal: {signal} - {e}", exc_info=True)

            processed_signal.processing_status = 'FAILED'
            processed_signal.failure_count += 1
            processed_signal.last_error = str(e)
            processed_signal.completed_at = ist_now_naive()
            session.commit()

            return False, f"Processing error: {str(e)}"

    def _mark_signal_processed(
        self,
        signal: Signal,
        session: Session,
        signal_valid: bool = False,
        rejection_reason: str = None,
        entry_attempted: bool = False,
        entry_successful: bool = False
    ):
        """Mark signal as processed in database"""
        processed = ProcessedSignal(
            bot_instance_id=config.get_bot_instance_id(),
            date=signal.date,
            script=signal.script,
            timeframe=signal.timeframe,
            signal_valid=signal_valid,
            entry_attempted=entry_attempted,
            entry_successful=entry_successful,
            rejection_reason=rejection_reason
        )

        session.add(processed)
        session.commit()

    def process_all_signals(self) -> Dict[str, int]:
        """
        Process all signals from CSV file

        Returns:
            Stats dict with counts
        """
        session = get_session()
        stats = {
            'total': 0,
            'processed': 0,
            'skipped': 0,
            'success': 0,
            'failed': 0
        }

        try:
            signals = self.read_signals_from_csv()
            stats['total'] = len(signals)

            if not signals:
                logger.info("No signals to process")
                return stats

            for signal in signals:
                success, message = self.process_signal(signal, session)

                # Determine status code for CSV update
                if success:
                    csv_status = 'S'  # Success - order placed
                    stats['success'] += 1
                    stats['processed'] += 1
                elif "Duplicate" in message:
                    csv_status = 'D'  # Duplicate
                    stats['failed'] += 1
                    stats['processed'] += 1
                elif "ignore list" in message.lower():
                    csv_status = 'I'  # Ignored
                    stats['skipped'] += 1
                elif "validation failed" in message.lower() or "SuperTrend" in message or "NIFTY" in message:
                    csv_status = 'R'  # Rejected (validation failed)
                    stats['failed'] += 1
                    stats['processed'] += 1
                elif message == "Already processed":
                    csv_status = 'D'  # Already processed = duplicate
                    stats['skipped'] += 1
                else:
                    csv_status = 'E'  # Error
                    stats['failed'] += 1
                    stats['processed'] += 1
                
                # Update CSV status (atomic update for each signal)
                self.update_signal_status_in_csv(signal, csv_status)

            logger.info(
                f"Signal processing complete: Total={stats['total']}, "
                f"Processed={stats['processed']}, "
                f"Success={stats['success']}, "
                f"Failed={stats['failed']}, "
                f"Skipped={stats['skipped']}"
            )

            return stats

        except Exception as e:
            logger.error(f"Error processing signals: {e}")
            session.rollback()  # Rollback any uncommitted changes
            raise
        finally:
            session.close()


# Singleton instance
entry_manager = EntryManager()
