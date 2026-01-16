"""
NEO Trade Terminal - Symbol Mapper

Maps Kite/Zerodha symbol format to NEO instrument tokens.
Downloads both Kite and NEO instruments at login, builds mapping cache.
"""

import pandas as pd
import os
import json
from datetime import datetime, date
import re
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)

# Shared Kite instruments path
KITE_INSTRUMENTS_PATH = "C:/Users/mail2/Documents/Projects/BOTS/data/index_options.csv"
KITE_STOCK_INSTRUMENTS_PATH = "C:/Users/mail2/Documents/Projects/BOTS/data/stock_instruments.csv"


class SymbolMapper:
    """Maps Kite symbols to NEO trading parameters."""

    def __init__(self, neo_client, config: Dict[str, Any] = None):
        self.client = neo_client
        self.config = config or {}
        self.cache_dir = self.config.get('paths', {}).get('scrip_master', 'data/scrip_master')
        self.nse_fo_df: Optional[pd.DataFrame] = None
        self.nse_cm_df: Optional[pd.DataFrame] = None
        self.bse_fo_df: Optional[pd.DataFrame] = None
        self.loaded_date: Optional[str] = None

        # Kite instruments DataFrame
        self.kite_instruments: Optional[pd.DataFrame] = None

        # Pre-computed mapping: kite_tradingsymbol -> NEO params
        self.kite_to_neo_map: Dict[str, Dict[str, Any]] = {}
        self.mapping_ready = False

        # Default lot sizes (fallback)
        self.lot_sizes = self.config.get('lot_sizes', {
            'NIFTY': 65,      # Updated
            'BANKNIFTY': 30,  # Updated
            'FINNIFTY': 60,   # Updated
            'SENSEX': 10,
            'BANKEX': 15,
            'MIDCPNIFTY': 120,  # Updated
        })

        # Index info from config
        self.indices = self.config.get('indices', {
            'NIFTY': {'strike_gap': 50, 'exchange': 'NFO'},
            'BANKNIFTY': {'strike_gap': 100, 'exchange': 'NFO'},
            'FINNIFTY': {'strike_gap': 50, 'exchange': 'NFO'},
            'SENSEX': {'strike_gap': 100, 'exchange': 'BFO'},
            'BANKEX': {'strike_gap': 100, 'exchange': 'BFO'},
        })

    def build_mapping_cache(self) -> Tuple[bool, str]:
        """
        Build Kite to NEO mapping cache at login time.
        Should be called once at startup after NEO login.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            # Step 1: Load Kite instruments
            kite_count = self._load_kite_instruments()
            if kite_count == 0:
                return False, "No Kite instruments loaded"

            # Step 2: Download NEO instruments for active underlyings
            neo_count = self._download_neo_instruments()

            # Step 3: Build mapping
            map_count = self._build_mapping()

            self.mapping_ready = True
            return True, f"Mapping ready: {kite_count} Kite, {neo_count} NEO, {map_count} mapped"

        except Exception as e:
            logger.error(f"Failed to build mapping cache: {e}", exc_info=True)
            return False, f"Mapping failed: {str(e)}"

    def _load_kite_instruments(self) -> int:
        """Load Kite instruments from shared BOTS data folder."""
        try:
            dfs = []

            # Load index options
            if os.path.exists(KITE_INSTRUMENTS_PATH):
                df = pd.read_csv(KITE_INSTRUMENTS_PATH, dtype=str)
                dfs.append(df)
                logger.info(f"Loaded {len(df)} index options from Kite")

            # Load stock instruments (F&O)
            if os.path.exists(KITE_STOCK_INSTRUMENTS_PATH):
                df = pd.read_csv(KITE_STOCK_INSTRUMENTS_PATH, dtype=str)
                # Filter for F&O only
                if 'segment' in df.columns:
                    df = df[df['segment'].isin(['NFO-OPT', 'NFO-FUT', 'BFO-OPT', 'BFO-FUT'])]
                dfs.append(df)
                logger.info(f"Loaded {len(df)} stock F&O from Kite")

            if dfs:
                self.kite_instruments = pd.concat(dfs, ignore_index=True)
                return len(self.kite_instruments)
            else:
                logger.warning("No Kite instrument files found")
                return 0

        except Exception as e:
            logger.error(f"Failed to load Kite instruments: {e}")
            return 0

    def _download_neo_instruments(self) -> int:
        """Download NEO instruments for active underlyings."""
        try:
            # Get unique underlyings from Kite instruments
            if self.kite_instruments is None:
                return 0

            # Common underlyings for F&O
            underlyings = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX']

            all_neo_instruments = []

            for underlying in underlyings:
                try:
                    exchange = 'bse_fo' if underlying in ['SENSEX', 'BANKEX'] else 'nse_fo'
                    result = self.client.search_scrip(
                        exchange_segment=exchange,
                        symbol=underlying
                    )
                    if isinstance(result, list):
                        for item in result:
                            item['_underlying'] = underlying
                            item['_exchange'] = exchange
                        all_neo_instruments.extend(result)
                        logger.info(f"Downloaded {len(result)} NEO instruments for {underlying}")
                except Exception as e:
                    logger.warning(f"Failed to download NEO instruments for {underlying}: {e}")

            if all_neo_instruments:
                self.nse_fo_df = pd.DataFrame(all_neo_instruments)
                return len(all_neo_instruments)

            return 0

        except Exception as e:
            logger.error(f"Failed to download NEO instruments: {e}")
            return 0

    def _build_mapping(self) -> int:
        """Build Kite to NEO symbol mapping."""
        if self.kite_instruments is None or self.nse_fo_df is None:
            return 0

        mapped = 0

        # Find trading symbol column in Kite data
        kite_symbol_col = None
        for col in ['option_symbol', 'tradingsymbol', 'trading_symbol', 'TradingSymbol', 'symbol']:
            if col in self.kite_instruments.columns:
                kite_symbol_col = col
                break

        if not kite_symbol_col:
            logger.warning("No trading symbol column found in Kite instruments")
            return 0

        # Build NEO symbol lookup dict
        neo_lookup = {}
        for _, row in self.nse_fo_df.iterrows():
            neo_symbol = str(row.get('pTrdSymbol', '')).upper()
            if neo_symbol:
                neo_lookup[neo_symbol] = row.to_dict()

        # Map Kite symbols to NEO
        for _, kite_row in self.kite_instruments.iterrows():
            kite_symbol = str(kite_row.get(kite_symbol_col, '')).upper()
            if not kite_symbol:
                continue

            # Direct match
            if kite_symbol in neo_lookup:
                neo_row = neo_lookup[kite_symbol]
                self.kite_to_neo_map[kite_symbol] = {
                    'trading_symbol': neo_row.get('pTrdSymbol', kite_symbol),
                    'exchange_segment': neo_row.get('_exchange', 'nse_fo'),
                    'lot_size': int(neo_row.get('lLotSize', 1)),
                    'instrument_token': str(neo_row.get('pSymbol', '')),
                    'tick_size': float(neo_row.get('dTickSize', 5)) / 100,
                    'kite_token': kite_row.get('instrument_token', ''),
                }
                mapped += 1

        logger.info(f"Built mapping for {mapped} symbols")
        return mapped

    def get_neo_params(self, kite_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get NEO trading parameters for a Kite symbol.
        Uses pre-computed mapping for instant lookup.
        Does NOT fallback to live search to avoid UI blocking.

        Args:
            kite_symbol: Kite trading symbol

        Returns:
            Dict with NEO parameters or None if not found
        """
        kite_symbol = kite_symbol.upper()

        # Check pre-computed mapping only (instant)
        if kite_symbol in self.kite_to_neo_map:
            return self.kite_to_neo_map[kite_symbol]

        # No live search fallback - return None immediately
        # Use search_and_map for weekly symbols
        return None

    def search_and_map(self, kite_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Live search for a symbol not in cache (e.g., weekly options).
        Slower than cached lookup but necessary for weekly symbols.

        Args:
            kite_symbol: Kite trading symbol (e.g., NIFTY2612225700CE)

        Returns:
            Dict with NEO parameters or None if not found
        """
        if not self.client:
            return None

        kite_symbol = kite_symbol.upper()
        try:
            parsed = self.parse_kite_symbol(kite_symbol)
        except ValueError:
            return None

        if not parsed:
            logger.warning(f"Could not parse symbol: {kite_symbol}")
            return None

        underlying = parsed.get('underlying', '')
        strike = parsed.get('strike', '')
        opt_type = parsed.get('option_type', '')

        # Determine exchange
        exchange = 'bse_fo' if underlying in ['SENSEX', 'BANKEX'] else 'nse_fo'

        try:
            # Search NEO for this symbol
            results = self.client.search_scrip(
                exchange_segment=exchange,
                symbol=underlying
            )

            if not results:
                logger.warning(f"No NEO results for {underlying}")
                return None

            # Find matching instrument
            for item in results:
                neo_symbol = item.get('pTrdSymbol', item.get('tradingSymbol', ''))
                neo_strike = str(item.get('dStrikePrice', item.get('strikePrice', ''))).replace('.0', '')
                neo_opt = item.get('cOptionType', item.get('optionType', ''))

                # Match strike and option type
                if neo_strike == strike and neo_opt == opt_type:
                    # Get lot size
                    lot_size = int(item.get('lLotSize', item.get('lotSize', 1)) or 1)

                    # Get lot size from config if available
                    lot_sizes = self.config.get('lot_sizes', {})
                    if underlying in lot_sizes:
                        lot_size = lot_sizes[underlying]

                    mapping = {
                        'kite_symbol': kite_symbol,
                        'trading_symbol': neo_symbol,
                        'instrument_token': str(item.get('pInstToken', item.get('instrumentToken', ''))),
                        'exchange_segment': exchange,
                        'lot_size': lot_size,
                        'underlying': underlying,
                        'strike': strike,
                        'option_type': opt_type
                    }

                    # Add to cache for future lookups
                    self.kite_to_neo_map[kite_symbol] = mapping
                    logger.info(f"Live mapped: {kite_symbol} -> {neo_symbol}")
                    return mapping

            logger.warning(f"No matching NEO instrument for {kite_symbol}")
            return None

        except Exception as e:
            logger.error(f"Live search failed for {kite_symbol}: {e}")
            return None

    def initialize(self) -> Tuple[bool, str]:
        """
        Download and load scrip master files.
        Should be called once at startup before market hours.

        Returns:
            Tuple of (success: bool, message: str)
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        today = date.today().isoformat()

        # Check if today's cache exists
        nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{today}.csv")

        if os.path.exists(nse_fo_file):
            self._load_from_cache(today)
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Loaded scrip master from cache ({today}) - {count} instruments"

        # Download fresh
        try:
            return self._download_scrip_masters(today)
        except Exception as e:
            logger.error(f"Failed to download scrip master: {e}", exc_info=True)
            # Try to load most recent cache
            return self._load_latest_cache()

    def _download_scrip_masters(self, today: str) -> Tuple[bool, str]:
        """Download scrip masters from NEO API."""
        try:
            # Download NSE F&O scrip master
            logger.info("Downloading NSE F&O scrip master...")
            nse_fo_data = self.client.scrip_master(exchange_segment="nse_fo")

            if nse_fo_data and 'data' in nse_fo_data:
                self.nse_fo_df = pd.DataFrame(nse_fo_data['data'])
            elif isinstance(nse_fo_data, list):
                self.nse_fo_df = pd.DataFrame(nse_fo_data)
            else:
                self.nse_fo_df = pd.DataFrame(nse_fo_data)

            nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{today}.csv")
            self.nse_fo_df.to_csv(nse_fo_file, index=False)
            logger.info(f"Saved {len(self.nse_fo_df)} NSE F&O instruments")

            # Download NSE Cash scrip master
            try:
                logger.info("Downloading NSE Cash scrip master...")
                nse_cm_data = self.client.scrip_master(exchange_segment="nse_cm")
                if nse_cm_data and 'data' in nse_cm_data:
                    self.nse_cm_df = pd.DataFrame(nse_cm_data['data'])
                elif isinstance(nse_cm_data, list):
                    self.nse_cm_df = pd.DataFrame(nse_cm_data)
                else:
                    self.nse_cm_df = pd.DataFrame(nse_cm_data)
                nse_cm_file = os.path.join(self.cache_dir, f"nse_cm_{today}.csv")
                self.nse_cm_df.to_csv(nse_cm_file, index=False)
            except Exception as e:
                logger.warning(f"NSE Cash download failed: {e}")

            # Download BSE F&O scrip master (for SENSEX/BANKEX)
            try:
                logger.info("Downloading BSE F&O scrip master...")
                bse_fo_data = self.client.scrip_master(exchange_segment="bse_fo")
                if bse_fo_data and 'data' in bse_fo_data:
                    self.bse_fo_df = pd.DataFrame(bse_fo_data['data'])
                elif isinstance(bse_fo_data, list):
                    self.bse_fo_df = pd.DataFrame(bse_fo_data)
                else:
                    self.bse_fo_df = pd.DataFrame(bse_fo_data)
                bse_fo_file = os.path.join(self.cache_dir, f"bse_fo_{today}.csv")
                self.bse_fo_df.to_csv(bse_fo_file, index=False)
            except Exception as e:
                logger.warning(f"BSE F&O download failed: {e}")

            self.loaded_date = today
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Downloaded fresh scrip master ({today}) - {count} instruments"

        except Exception as e:
            return False, f"Failed to download scrip master: {str(e)}"

    def _load_from_cache(self, date_str: str):
        """Load from cached CSV files."""
        nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{date_str}.csv")
        if os.path.exists(nse_fo_file):
            self.nse_fo_df = pd.read_csv(nse_fo_file, dtype=str)
            logger.info(f"Loaded {len(self.nse_fo_df)} NSE F&O instruments from cache")

        nse_cm_file = os.path.join(self.cache_dir, f"nse_cm_{date_str}.csv")
        if os.path.exists(nse_cm_file):
            self.nse_cm_df = pd.read_csv(nse_cm_file, dtype=str)

        bse_fo_file = os.path.join(self.cache_dir, f"bse_fo_{date_str}.csv")
        if os.path.exists(bse_fo_file):
            self.bse_fo_df = pd.read_csv(bse_fo_file, dtype=str)

        self.loaded_date = date_str

    def _load_latest_cache(self) -> Tuple[bool, str]:
        """Load most recent cache file."""
        try:
            files = [f for f in os.listdir(self.cache_dir) if f.startswith('nse_fo_')]
            if not files:
                return False, "No cached scrip master found"

            # Get most recent
            files.sort(reverse=True)
            latest = files[0]
            date_str = latest.replace('nse_fo_', '').replace('.csv', '')

            self._load_from_cache(date_str)
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Loaded scrip master from cache ({date_str}) - {count} instruments"
        except Exception as e:
            return False, f"Failed to load cache: {str(e)}"

    def parse_kite_symbol(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Parse Kite symbol format into components.

        Examples:
            NIFTY25JAN24000CE -> {underlying: NIFTY, expiry: 25JAN, strike: 24000, opt_type: CE}
            BANKNIFTY25JAN52000PE -> {underlying: BANKNIFTY, expiry: 25JAN, strike: 52000, opt_type: PE}
            NIFTY25115024000CE -> {underlying: NIFTY, expiry: 251150, strike: 24000, opt_type: CE} (weekly)
            NIFTY25JANFUT -> {underlying: NIFTY, expiry: 25JAN, instrument: FUT}

        Returns:
            Parsed symbol dict
        """
        kite_symbol = kite_symbol.upper().strip()

        # Pattern for monthly options: SYMBOL + YYMM + STRIKE + CE/PE
        # e.g., NIFTY25JAN24000CE, BANKNIFTY25FEB52000PE
        monthly_option_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)$'

        # Pattern for weekly options: SYMBOL + YYMMDD + STRIKE + CE/PE
        # e.g., NIFTY2511624000CE (16th Jan 2025)
        weekly_option_pattern = r'^([A-Z]+)(\d{5,7})(\d+)(CE|PE)$'

        # Pattern for futures: SYMBOL + YYMM + FUT
        future_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(FUT)$'

        # Try monthly options pattern
        match = re.match(monthly_option_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION',
                'expiry_type': 'MONTHLY'
            }

        # Try weekly options pattern
        match = re.match(weekly_option_pattern, kite_symbol)
        if match:
            # Parse YYMMDD format
            expiry_str = match.group(2)
            return {
                'underlying': match.group(1),
                'expiry': expiry_str,
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION',
                'expiry_type': 'WEEKLY'
            }

        # Try futures pattern
        match = re.match(future_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'option_type': None,
                'strike': None,
                'instrument_type': 'FUTURE'
            }

        raise ValueError(f"Unable to parse symbol: {kite_symbol}")

    def map_to_neo(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Map Kite symbol to NEO trading parameters.

        Args:
            kite_symbol: Kite format symbol like NIFTY25JAN24000CE

        Returns:
            Dict ready for place_order():
            {
                'exchange_segment': 'nse_fo',
                'trading_symbol': 'NIFTY25JAN24000CE',
                'instrument_token': '12345',
                'lot_size': 75,
                'tick_size': 0.05
            }
        """
        parsed = self.parse_kite_symbol(kite_symbol)
        underlying = parsed['underlying']

        # Determine exchange (NSE F&O or BSE F&O)
        index_info = self.indices.get(underlying, {})
        exchange = index_info.get('exchange', 'NFO')

        if exchange == 'BFO':
            df = self.bse_fo_df
            exchange_segment = 'bse_fo'
        else:
            df = self.nse_fo_df
            exchange_segment = 'nse_fo'

        if df is None or df.empty:
            raise ValueError("Scrip master not loaded. Call initialize() first.")

        # Build search criteria
        # NEO column names may vary - common ones: pSymbol, pTradingSymbol, pOptionType, pStrikePrice
        possible_symbol_cols = ['pSymbol', 'pSymbolName', 'symbol', 'Symbol']
        possible_trading_cols = ['pTradingSymbol', 'tradingSymbol', 'TradingSymbol', 'trading_symbol']
        possible_option_cols = ['pOptionType', 'optionType', 'OptionType', 'option_type']
        possible_strike_cols = ['pStrikePrice', 'strikePrice', 'StrikePrice', 'strike_price']
        possible_token_cols = ['pScripCode', 'scripCode', 'ScripCode', 'instrument_token', 'token']
        possible_lot_cols = ['pLotSize', 'lotSize', 'LotSize', 'lot_size']
        possible_tick_cols = ['pTickSize', 'tickSize', 'TickSize', 'tick_size']

        def get_col(df, possible_names):
            for col in possible_names:
                if col in df.columns:
                    return col
            return None

        symbol_col = get_col(df, possible_symbol_cols)
        trading_col = get_col(df, possible_trading_cols)
        option_col = get_col(df, possible_option_cols)
        strike_col = get_col(df, possible_strike_cols)
        token_col = get_col(df, possible_token_cols)
        lot_col = get_col(df, possible_lot_cols)
        tick_col = get_col(df, possible_tick_cols)

        # Search by trading symbol first (exact match)
        if trading_col:
            mask = df[trading_col].str.upper() == kite_symbol.upper()
            matches = df[mask]

            if not matches.empty:
                row = matches.iloc[0]
                return self._build_result(
                    row, exchange_segment, parsed,
                    trading_col, token_col, lot_col, tick_col
                )

        # Search by components
        if symbol_col and option_col and strike_col:
            mask = df[symbol_col].str.upper() == underlying

            if parsed['instrument_type'] == 'OPTION':
                mask &= df[option_col].str.upper() == parsed['option_type']
                # Strike comparison - handle float/string conversion
                strike_val = parsed['strike']
                mask &= df[strike_col].astype(str).str.replace('.0', '', regex=False) == strike_val

            matches = df[mask]

            if not matches.empty:
                # If multiple matches, try to filter by expiry
                if len(matches) > 1:
                    # Try expiry column filtering
                    expiry_cols = ['pExpiryDate', 'expiryDate', 'ExpiryDate', 'expiry']
                    expiry_col = get_col(df, expiry_cols)
                    if expiry_col:
                        expiry_pattern = parsed['expiry']
                        expiry_mask = matches[expiry_col].str.contains(expiry_pattern, case=False, na=False)
                        if expiry_mask.any():
                            matches = matches[expiry_mask]

                row = matches.iloc[0]
                return self._build_result(
                    row, exchange_segment, parsed,
                    trading_col, token_col, lot_col, tick_col
                )

        raise ValueError(f"Symbol not found in NEO scrip master: {kite_symbol}")

    def _build_result(self, row: pd.Series, exchange_segment: str, parsed: Dict,
                      trading_col: str, token_col: str, lot_col: str, tick_col: str) -> Dict[str, Any]:
        """Build result dict from matched row."""
        trading_symbol = row[trading_col] if trading_col and trading_col in row else parsed.get('underlying', '')
        instrument_token = str(row[token_col]) if token_col and token_col in row else ''

        lot_size = self._get_lot_size(row, lot_col, parsed['underlying'])
        tick_size = float(row[tick_col]) if tick_col and tick_col in row else 0.05

        return {
            'exchange_segment': exchange_segment,
            'trading_symbol': trading_symbol,
            'instrument_token': instrument_token,
            'lot_size': lot_size,
            'tick_size': tick_size,
            'underlying': parsed['underlying'],
            'strike': parsed.get('strike'),
            'option_type': parsed.get('option_type'),
            'expiry': parsed.get('expiry'),
            'instrument_type': parsed.get('instrument_type', 'OPTION')
        }

    def _get_lot_size(self, row: pd.Series, lot_col: str, underlying: str) -> int:
        """Get lot size from row or fallback to defaults."""
        if lot_col and lot_col in row:
            try:
                return int(float(row[lot_col]))
            except (ValueError, TypeError):
                pass
        return self.lot_sizes.get(underlying, 1)

    def get_default_lot_size(self, underlying: str) -> int:
        """Get default lot size for underlying."""
        return self.lot_sizes.get(underlying, 1)

    def search_symbol(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search for symbols matching query.
        Useful for autocomplete in GUI.

        Args:
            query: Search query
            limit: Max results to return

        Returns:
            List of matching symbols with basic info
        """
        if self.nse_fo_df is None:
            return []

        query = query.upper()

        # Find trading symbol column
        possible_cols = ['pTradingSymbol', 'tradingSymbol', 'TradingSymbol', 'trading_symbol']
        trading_col = None
        for col in possible_cols:
            if col in self.nse_fo_df.columns:
                trading_col = col
                break

        if not trading_col:
            return []

        mask = self.nse_fo_df[trading_col].str.contains(query, na=False, case=False)
        matches = self.nse_fo_df[mask].head(limit)

        # Build results
        results = []
        token_cols = ['pScripCode', 'scripCode', 'instrument_token']
        lot_cols = ['pLotSize', 'lotSize', 'lot_size']

        token_col = None
        for col in token_cols:
            if col in matches.columns:
                token_col = col
                break

        lot_col = None
        for col in lot_cols:
            if col in matches.columns:
                lot_col = col
                break

        for _, row in matches.iterrows():
            result = {
                'trading_symbol': row[trading_col],
                'token': row[token_col] if token_col else '',
                'lot_size': int(float(row[lot_col])) if lot_col and pd.notna(row[lot_col]) else 0
            }
            results.append(result)

        return results

    def get_instrument_token(self, trading_symbol: str) -> Optional[str]:
        """Get instrument token for a trading symbol."""
        try:
            result = self.map_to_neo(trading_symbol)
            return result.get('instrument_token')
        except ValueError:
            return None

    def map_kite_to_neo(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Map Kite symbol to NEO using search_scrip API.
        This is the preferred method - no scrip master download needed.

        Args:
            kite_symbol: Kite format symbol like NIFTY26JAN25800CE

        Returns:
            Dict with NEO trading parameters:
            {
                'trading_symbol': 'NIFTY26JAN25800CE',
                'exchange_segment': 'nse_fo',
                'lot_size': 65,
                'instrument_token': '12345',
                'tick_size': 0.05,
                'expiry_date': '27 Jan, 2026'
            }
        """
        # Step 1: Parse Kite symbol
        parsed = self.parse_kite_symbol(kite_symbol)
        underlying = parsed['underlying']
        strike = parsed.get('strike')
        option_type = parsed.get('option_type')

        # Determine exchange segment
        if underlying in ['SENSEX', 'BANKEX']:
            exchange_segment = 'bse_fo'
        else:
            exchange_segment = 'nse_fo'

        # Step 2: Search in NEO using search_scrip
        try:
            search_params = {
                'exchange_segment': exchange_segment,
                'symbol': underlying,
            }

            if option_type:
                search_params['option_type'] = option_type
            if strike:
                search_params['strike_price'] = str(strike)

            result = self.client.search_scrip(**search_params)

            if not result or isinstance(result, dict) and 'message' in result:
                raise ValueError(f"Symbol not found in NEO: {kite_symbol}")

            # Step 3: Find exact match from results
            if isinstance(result, list):
                # Match by trading symbol
                for item in result:
                    neo_symbol = item.get('pTrdSymbol', '')
                    if neo_symbol.upper() == kite_symbol.upper():
                        return self._build_neo_result(item, exchange_segment)

                # If no exact match, find by expiry pattern
                expiry_pattern = parsed.get('expiry', '')
                for item in result:
                    neo_symbol = item.get('pTrdSymbol', '')
                    if expiry_pattern.upper() in neo_symbol.upper():
                        if strike and str(strike) in neo_symbol:
                            if option_type and option_type in neo_symbol:
                                return self._build_neo_result(item, exchange_segment)

                # Return first match if available
                if result:
                    return self._build_neo_result(result[0], exchange_segment)

            raise ValueError(f"No matching symbol found for: {kite_symbol}")

        except Exception as e:
            logger.error(f"Failed to map {kite_symbol} to NEO: {e}")
            raise ValueError(f"Failed to map symbol: {str(e)}")

    def _build_neo_result(self, item: Dict, exchange_segment: str) -> Dict[str, Any]:
        """Build result dict from NEO search_scrip response."""
        return {
            'trading_symbol': item.get('pTrdSymbol', ''),
            'exchange_segment': exchange_segment,
            'lot_size': int(item.get('lLotSize', 1)),
            'instrument_token': str(item.get('pSymbol', '')),
            'tick_size': float(item.get('dTickSize', 5)) / 100,  # Convert paise to rupees
            'expiry_date': item.get('pScripRefKey', ''),
            'symbol_name': item.get('pSymbolName', ''),
            'instrument_type': item.get('pInstType', ''),
            'option_type': item.get('pOptionType', ''),
        }

    def get_lot_size_from_neo(self, kite_symbol: str) -> int:
        """
        Get lot size for a Kite symbol by querying NEO.

        Args:
            kite_symbol: Kite format symbol

        Returns:
            Lot size from NEO
        """
        try:
            result = self.map_kite_to_neo(kite_symbol)
            return result.get('lot_size', 1)
        except Exception as e:
            logger.warning(f"Failed to get lot size for {kite_symbol}: {e}")
            # Fallback to defaults
            parsed = self.parse_kite_symbol(kite_symbol)
            return self.lot_sizes.get(parsed['underlying'], 1)
