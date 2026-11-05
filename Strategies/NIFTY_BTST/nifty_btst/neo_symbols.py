"""Kite instrument -> NEO scrip-master row, by (underlying, expiry, strike, type).

NEVER by string. Monthly NIFTY symbols happen to match Kite's spelling; weekly
ones did not match a single row in Scalper's 2026-09-25 cache (audit Q4). The
BTST mark already carries the Kite row (expiry, strike, instrument_type,
lot_size), so the match is on those four facts and the trading symbol and
token are READ from the NEO row that matches.

Scrip master facts (SDK 2.0.0 `scrip_search.py`, Scalper `symbol_mapper.py`):
  pTrdSymbol   trading symbol sent as `ts`
  pSymbol      instrument token sent as `tk`
  pSymbolName  underlying ("NIFTY")
  lLotSize     lot size
  dTickSize    tick in PAISE (5 -> Rs 0.05)
  pExpiryDate  seconds on a 1980-based epoch: UNIX = value + 315511200
  dStrikePrice; strike x 100 (the trailing semicolon is in the file)
  pOptionType  CE / PE
The epoch offset is applied exactly as the SDK applies it (UTC). It is
UNVERIFIED against the live file until Stage 0 prints a weekly row beside its
Kite expiry; `neo-probe` does that.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

EPOCH_OFFSET_SEC = 315511200
_EPOCH_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)


class ScripMasterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScripRow:
    trading_symbol: str
    token: str
    underlying: str
    expiry: date
    strike: float
    option_type: str
    lot_size: int
    tick_size: float


def expiry_from_epoch(value: str) -> date:
    """`pExpiryDate` -> calendar date, the SDK's own arithmetic."""
    secs = int(float(value)) + EPOCH_OFFSET_SEC
    return (_EPOCH_1970 + timedelta(seconds=secs)).date()


def _col(row: Dict[str, str], *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n]).strip()
    return ''


def parse_scrip_master(text: str, underlying: str) -> List[ScripRow]:
    """Option rows of `underlying` from the nse_fo CSV. Rows that do not
    parse are skipped and COUNTED, never silently."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ScripMasterError('scrip master is empty')
    fields = [f.strip() for f in reader.fieldnames]
    needed = ('pTrdSymbol', 'pSymbol', 'pSymbolName', 'lLotSize', 'pExpiryDate',
              'pOptionType')
    missing = [n for n in needed if n not in fields]
    if missing:
        raise ScripMasterError('scrip master lacks columns %s; has %s'
                               % (missing, fields[:25]))
    out: List[ScripRow] = []
    bad = 0
    for raw in reader:
        row = {(k or '').strip(): v for k, v in raw.items()}
        if _col(row, 'pSymbolName').upper() != underlying.upper():
            continue
        opt = _col(row, 'pOptionType').upper()
        if opt not in ('CE', 'PE'):
            continue
        try:
            strike = float(_col(row, 'dStrikePrice;', 'dStrikePrice')) / 100.0
            out.append(ScripRow(
                trading_symbol=_col(row, 'pTrdSymbol'),
                token=_col(row, 'pSymbol'),
                underlying=underlying.upper(),
                expiry=expiry_from_epoch(_col(row, 'pExpiryDate')),
                strike=strike,
                option_type=opt,
                lot_size=int(float(_col(row, 'lLotSize'))),
                tick_size=float(_col(row, 'dTickSize') or '5') / 100.0,
            ))
        except (ValueError, OverflowError):
            bad += 1
    if bad:
        logger.warning('scrip master: %d %s option rows did not parse', bad, underlying)
    if not out:
        raise ScripMasterError('scrip master has no %s option rows' % underlying)
    return out


class ScripMap:
    def __init__(self, rows: Iterable[ScripRow]):
        self._by_key: Dict[Tuple[date, float, str], List[ScripRow]] = {}
        for r in rows:
            self._by_key.setdefault((r.expiry, round(r.strike, 2), r.option_type), []).append(r)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_key.values())

    def expiries(self) -> List[date]:
        return sorted({k[0] for k in self._by_key})

    def find(self, expiry: date, strike: float, option_type: str) -> ScripRow:
        """Exactly one row or an error. Two rows for one key is as fatal as
        none: we would not know which symbol the exchange means."""
        hits = self._by_key.get((expiry, round(float(strike), 2), option_type.upper()), [])
        if len(hits) != 1:
            raise ScripMasterError('%d NEO rows for %s %s %s (expiries in file: %s)'
                                   % (len(hits), expiry, strike, option_type,
                                      ', '.join(e.isoformat() for e in self.expiries()[:6])))
        return hits[0]

    def find_for_mark(self, mark: Dict[str, object]) -> ScripRow:
        """From a BTST mark / position record (Kite row fields)."""
        return self.find(date.fromisoformat(str(mark['expiry'])),
                         float(str(mark['strike'])), str(mark['side']))

    @staticmethod
    def pick_fo_path(paths: List[str]) -> Optional[str]:
        for p in paths:
            if 'nse_fo' in p.lower():
                return p
        return None
