"""
neo.symbols — NIFTY-only Kite→Neo symbol mapper.

Uses Neo search_scrip API to find Neo trading symbols for NIFTY options.
Cache: neo/data/symbol_cache_{date}.json (refreshed daily).
"""

import os
import re
import json
import logging
import datetime
import pytz

log = logging.getLogger('neo.symbols')
IST = pytz.timezone('Asia/Kolkata')

# Kite symbol format: NIFTY{YY}{M}{DD}{strike}{type}
# M = 1-9 for Jan-Sep, O/N/D for Oct-Dec
_MONTH_MAP = {
    '1': 1, '2': 2, '3': 3, '4': 4, '5': 5, '6': 6,
    '7': 7, '8': 8, '9': 9, 'O': 10, 'N': 11, 'D': 12,
}
# Reverse: month number -> Kite code
_MONTH_REV = {v: k for k, v in _MONTH_MAP.items()}

# Kite monthly format uses 3-letter month: NIFTY{YY}{MON}{strike}{type}
_MONTH_ABBR = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}


def parse_kite_symbol(sym):
    """Parse Kite trading symbol into components.

    Handles both weekly (NIFTY26O0822700CE) and monthly (NIFTY26APR22700CE) formats.

    Returns:
        Dict with keys: underlying, year, month, day, strike, option_type, expiry_str
        or None if parsing fails.
    """
    # Weekly: NIFTY{YY}{M}{DD}{strike}{CE|PE}
    m = re.match(r'^(NIFTY)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$', sym)
    if m:
        underlying, yy, mc, dd, strike, ot = m.groups()
        month = _MONTH_MAP.get(mc)
        if month:
            return {
                'underlying': underlying,
                'year': 2000 + int(yy),
                'month': month,
                'day': int(dd),
                'strike': int(strike),
                'option_type': ot,
                'expiry_str': f"20{yy}-{month:02d}-{int(dd):02d}",
            }

    # Monthly: NIFTY{YY}{MON}{strike}{CE|PE}
    m = re.match(r'^(NIFTY)(\d{2})([A-Z]{3})(\d+)(CE|PE)$', sym)
    if m:
        underlying, yy, mon, strike, ot = m.groups()
        month = _MONTH_ABBR.get(mon)
        if month:
            return {
                'underlying': underlying,
                'year': 2000 + int(yy),
                'month': month,
                'day': None,  # monthly — day not in symbol
                'strike': int(strike),
                'option_type': ot,
                'expiry_str': None,
            }

    log.warning(f"Cannot parse Kite symbol: {sym}")
    return None


class SymbolMapper:
    """Maps Kite option symbols to Neo trading parameters.

    Uses Neo search_scrip API for lookup, caches results daily.
    """

    def __init__(self, neo_client, data_dir, lot_size_fallback=75):
        self._client = neo_client
        self._data_dir = data_dir
        self._cache = {}  # kite_symbol -> neo_params dict
        self._lot_size = lot_size_fallback
        self._cache_loaded = False

        os.makedirs(data_dir, exist_ok=True)
        self._load_cache()

    def map_symbol(self, kite_symbol):
        """Map Kite trading symbol to Neo params.

        Returns:
            Dict: {trading_symbol, exchange_segment, instrument_token} or None.
        """
        # Check cache
        if kite_symbol in self._cache:
            return self._cache[kite_symbol]

        # Live search fallback
        result = self._search_live(kite_symbol)
        if result:
            self._cache[kite_symbol] = result
            self._save_cache()
        return result

    def get_lot_size(self):
        """Return NIFTY lot size from cache or fallback."""
        return self._lot_size

    def _search_live(self, kite_symbol):
        """Search Neo API for a specific symbol."""
        if not self._client:
            return None

        parsed = parse_kite_symbol(kite_symbol)
        if not parsed:
            return None

        try:
            # Search by the Kite symbol directly — Neo often uses similar format
            resp = self._client.search_scrip(
                exchange_segment='nse_fo',
                symbol=kite_symbol,
            )

            # search_scrip returns a list of matches
            results = []
            if isinstance(resp, list):
                results = resp
            elif isinstance(resp, dict):
                results = resp.get('data', resp.get('results', []))
                if isinstance(results, dict):
                    results = [results]

            if not results:
                # Try broader search: NIFTY + strike + CE/PE
                search_term = f"NIFTY {parsed['strike']} {parsed['option_type']}"
                resp = self._client.search_scrip(
                    exchange_segment='nse_fo',
                    symbol=search_term,
                )
                if isinstance(resp, list):
                    results = resp
                elif isinstance(resp, dict):
                    results = resp.get('data', resp.get('results', []))

            # Find best match by strike and option type
            for item in (results if isinstance(results, list) else []):
                sym = (item.get('trdSym', '') or item.get('tradingSymbol', '') or
                       item.get('symbol', ''))
                if not sym:
                    continue
                # Match strike and option type
                if (str(parsed['strike']) in sym and
                        parsed['option_type'] in sym and
                        'NIFTY' in sym.upper()):
                    neo_params = {
                        'trading_symbol': sym,
                        'exchange_segment': 'nse_fo',
                        'instrument_token': str(
                            item.get('tok', item.get('token',
                                     item.get('instrument_token', '')))),
                    }
                    # Extract lot size if available
                    ls = item.get('lotSize', item.get('lot_size', 0))
                    if ls and int(ls) > 0:
                        self._lot_size = int(ls)
                    log.info(f"Symbol mapped: {kite_symbol} -> {sym}")
                    return neo_params

            log.warning(f"Symbol not found in Neo: {kite_symbol}")
            return None

        except Exception as e:
            log.error(f"search_scrip failed for {kite_symbol}: {e}")
            return None

    def _cache_path(self):
        today = datetime.datetime.now(IST).strftime('%Y-%m-%d')
        return os.path.join(self._data_dir, f'symbol_cache_{today}.json')

    def _load_cache(self):
        path = self._cache_path()
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self._cache = data.get('symbols', {})
            ls = data.get('lot_size', 0)
            if ls > 0:
                self._lot_size = ls
            self._cache_loaded = True
            log.info(f"Symbol cache loaded: {len(self._cache)} entries")
        except Exception as e:
            log.warning(f"Symbol cache load failed: {e}")

    def _save_cache(self):
        path = self._cache_path()
        data = {
            'date': datetime.datetime.now(IST).strftime('%Y-%m-%d'),
            'lot_size': self._lot_size,
            'symbols': self._cache,
        }
        try:
            tmp = path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            log.warning(f"Symbol cache save failed: {e}")
