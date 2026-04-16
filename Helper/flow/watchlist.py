"""Thread 1: Chartink scanner — maintains UP/DOWN watchlists.

Scans Chartink every 5 min for stocks where W+D+H+30M ST are all aligned.
Stocks automatically drop when any TF breaks alignment (next scan won't return them).
"""

import logging
from typing import Dict, List, Set

import requests
from bs4 import BeautifulSoup

from . import config as cfg

logger = logging.getLogger(__name__)


def _fix_symbol(sym: str) -> str:
    """Fix Chartink symbol mismatches with NSE/Kite naming."""
    return cfg.CHARTINK_TO_NSE.get(sym, sym)


def scan_chartink(scan_clause: str) -> List[str]:
    """Scrape Chartink screener API. Returns list of NSE symbols."""
    try:
        with requests.Session() as s:
            r = s.get('https://chartink.com/screener/', timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            csrf_tag = soup.select_one("[name='csrf-token']")
            if not csrf_tag:
                logger.error("Chartink CSRF token not found")
                return []
            s.headers['x-csrf-token'] = csrf_tag['content']

            r = s.post(cfg.CHARTINK_URL, data={'scan_clause': scan_clause},
                       timeout=15)
            resp = r.json()

            data = resp.get('data', [])
            if data and isinstance(data[0], dict):
                return [_fix_symbol(item['nsecode'])
                        for item in data
                        if isinstance(item, dict) and 'nsecode' in item]

            # Fallback: aggregatedStockList
            agg = resp.get('aggregatedStockList', [])
            if agg and isinstance(agg, list):
                flat = agg[0] if isinstance(agg[0], list) else agg
                if flat and isinstance(flat[0], str):
                    return [_fix_symbol(flat[i])
                            for i in range(0, len(flat) - 2, 3)
                            if flat[i] and isinstance(flat[i], str)]

            logger.error("Chartink: no usable data (keys: %s)", list(resp.keys()))
            return []
    except Exception as e:
        logger.error("Chartink scan failed: %s", e)
        return []


def scan_both() -> Dict[str, Set[str]]:
    """Scan Chartink for both UP and DOWN aligned stocks.

    Returns dict with 'UP' and 'DOWN' keys, each a set of NSE symbols.
    """
    up_stocks = scan_chartink(cfg.CHARTINK_UP)
    down_stocks = scan_chartink(cfg.CHARTINK_DOWN)

    logger.info("Chartink scan: UP=%d, DOWN=%d", len(up_stocks), len(down_stocks))

    return {
        'UP': set(up_stocks),
        'DOWN': set(down_stocks),
    }


def diff_watchlist(old: Dict[str, Set[str]],
                   new: Dict[str, Set[str]]) -> Dict[str, Dict[str, Set[str]]]:
    """Compare old vs new watchlist, return added/removed per side."""
    result = {}
    for side in ('UP', 'DOWN'):
        old_set = old.get(side, set())
        new_set = new.get(side, set())
        result[side] = {
            'added': new_set - old_set,
            'removed': old_set - new_set,
        }
    return result
