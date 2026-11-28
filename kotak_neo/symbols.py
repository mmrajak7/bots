"""Kite instrument -> NEO scrip-master row, for NIFTY AND stock options.

Matched on FACTS -- (underlying, expiry, strike, CE/PE) -- never on a symbol
string; the NEO trading symbol and token are READ from the matching row
(generalised from BTST's `neo_symbols.py`, which handled one underlying).

Scrip master columns (verified on the Pi's 2026-11-27 nse_fo file):
  pTrdSymbol  `ts`      pSymbol    `tk` (token)    pSymbolName  underlying
  pInstType   OPTIDX / OPTSTK / FUT...             pOptionType  CE / PE
  lLotSize    lot       dTickSize  tick in PAISE   dStrikePrice; strike x 100
  pExpiryDate seconds on a 1980 epoch: UNIX = value + 315511200
                       (RELIANCE ...26DEC... rows decode to 2026-12-29)

`load(c)` downloads the nse_fo file once a day into the shared HOME (one
copy for every bot) and parses it before caching it.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import session as S

logger = logging.getLogger(__name__)

EPOCH_OFFSET_SEC = 315511200
_EPOCH_1970 = datetime(1970, 1, 1, tzinfo=timezone.utc)
_IST = timezone(timedelta(hours=5, minutes=30))
#: A full nse_fo file holds tens of thousands of option rows; fewer than this
#: means a truncated download, which must never be cached and trusted all day.
MIN_ROWS = 1000
OPTION_INST = ('OPTIDX', 'OPTSTK')


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
    inst_type: str


def expiry_from_epoch(value: str) -> date:
    """The SDK's arithmetic, read on the IST calendar (the exchange's).
    Measured: RELIANCE ...26DEC... rows -> 2026-12-29 14:00 IST."""
    t = _EPOCH_1970 + timedelta(seconds=int(float(value)) + EPOCH_OFFSET_SEC)
    return t.astimezone(_IST).date()


def _col(row: Dict[str, str], *names: str) -> str:
    for n in names:
        if n in row and row[n] is not None:
            return str(row[n]).strip()
    return ''


def parse_scrip_master(text: str, underlyings: Optional[Iterable[str]] = None) -> List[ScripRow]:
    """Option rows (all, or only `underlyings`). Bad rows are COUNTED, never silent."""
    want = {u.upper() for u in underlyings} if underlyings else None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ScripMasterError('scrip master is empty')
    fields = [f.strip() for f in reader.fieldnames]
    needed = ('pTrdSymbol', 'pSymbol', 'pSymbolName', 'lLotSize', 'pExpiryDate', 'pOptionType')
    missing = [n for n in needed if n not in fields]
    if missing:
        raise ScripMasterError('scrip master lacks columns %s' % missing)
    out, bad = [], 0
    for raw in reader:
        row = {(k or '').strip(): v for k, v in raw.items()}
        und = _col(row, 'pSymbolName').upper()
        if want is not None and und not in want:
            continue
        opt = _col(row, 'pOptionType').upper()
        inst = _col(row, 'pInstType').upper()
        if opt not in ('CE', 'PE') or (inst and inst not in OPTION_INST):
            continue
        try:
            out.append(ScripRow(
                trading_symbol=_col(row, 'pTrdSymbol'), token=_col(row, 'pSymbol'),
                underlying=und, expiry=expiry_from_epoch(_col(row, 'pExpiryDate')),
                strike=float(_col(row, 'dStrikePrice;', 'dStrikePrice')) / 100.0,
                option_type=opt, lot_size=int(float(_col(row, 'lLotSize'))),
                tick_size=float(_col(row, 'dTickSize') or '5') / 100.0, inst_type=inst))
        except (ValueError, OverflowError):
            bad += 1
    if bad:
        logger.warning('scrip master: %d option rows did not parse', bad)
    if not out:
        raise ScripMasterError('scrip master has no option rows for %s'
                               % (sorted(want) if want else 'any underlying'))
    return out


class ScripMap:
    def __init__(self, rows: Iterable[ScripRow]):
        self._by: Dict[Tuple[str, date, float, str], List[ScripRow]] = {}
        for r in rows:
            self._by.setdefault((r.underlying, r.expiry, round(r.strike, 2), r.option_type),
                                []).append(r)

    def __len__(self) -> int:
        return sum(len(v) for v in self._by.values())

    def find(self, underlying: str, expiry: date, strike: float, option_type: str) -> ScripRow:
        """Exactly one row, or an error: two rows for one key is as fatal as none."""
        k = (underlying.upper(), expiry, round(float(strike), 2), option_type.upper())
        hits = self._by.get(k, [])
        if len(hits) != 1:
            exps = sorted({key[1] for key in self._by if key[0] == k[0]})[:6]
            raise ScripMasterError('%d NEO rows for %s %s %s %s (expiries: %s)' % (
                len(hits), k[0], expiry, strike, k[3], ', '.join(e.isoformat() for e in exps)))
        return hits[0]

    def from_kite(self, kite_row: Dict[str, Any]) -> ScripRow:
        """A Kite instrument / option-CSV row -> its NEO row, lot size CROSS-CHECKED.
        Needs: name (underlying), expiry, strike, instrument_type, lot_size."""
        exp = kite_row['expiry']
        if not isinstance(exp, date):
            exp = date.fromisoformat(str(exp)[:10])
        row = self.find(str(kite_row['name']), exp, float(kite_row['strike']),
                        str(kite_row['instrument_type']))
        if int(kite_row['lot_size']) != row.lot_size:
            raise ScripMasterError('lot size NEO %d != Kite %s for %s' % (
                row.lot_size, kite_row['lot_size'], row.trading_symbol))
        return row

    @staticmethod
    def pick_fo_path(paths: List[str]) -> Optional[str]:
        for p in paths:
            if 'nse_fo' in p.lower():
                return p
        return None


def load(c: Any, today: Optional[date] = None, max_age_h: float = 20.0,
         underlyings: Optional[Iterable[str]] = None, min_rows: int = MIN_ROWS) -> ScripMap:
    """Today's nse_fo map from the shared cache, downloading at most once a day
    under a lock (any bot may be first; two never interleave a write). The
    text is PARSED and checked for a sane row count before it is cached.
    `underlyings` narrows the parse (the file stays whole on disk)."""
    import os
    today = today or S._now().date()
    S._ensure_home()
    cache = S.HOME / ('scrip_nse_fo_%s.csv' % today.strftime('%Y%m%d'))

    def fresh() -> Optional[str]:
        if cache.exists() and (time.time() - cache.stat().st_mtime) / 3600.0 <= max_age_h:
            return cache.read_text(encoding='utf-8')
        return None

    with S.file_lock('scrip.lock', 120.0) as got:
        if not got:
            raise ScripMasterError('scrip master lock busy')
        text = fresh()
        if text is not None:
            try:
                return ScripMap(parse_scrip_master(text, underlyings))
            except ScripMasterError as e:
                # K12 (review 2026-11-28): only a BAD FILE is deleted. When the
                # whole file is fine, the caller's filter matched nothing: an
                # unknown underlying, or a file that lacks it (review R10). ONE
                # re-download a day settles which -- the good cache stays until
                # a good replacement lands -- then it is the filter's fault.
                try:
                    whole_ok = len(parse_scrip_master(text)) >= min_rows
                except ScripMasterError:
                    whole_ok = False
                if whole_ok and underlyings:
                    marker = S.HOME / ('scrip_refetch_%s' % today.strftime('%Y%m%d'))
                    if marker.exists():
                        raise ScripMasterError('%s (the cached file itself is fine; '
                                               're-downloaded once today already)' % e)
                    marker.write_text(str(sorted(u.upper() for u in underlyings)),
                                      encoding='utf-8')
                    logger.warning('scrip master: %s -- re-downloading once today', e)
                else:
                    cache.unlink()
        fo = ScripMap.pick_fo_path(c.scrip_master_paths())
        if not fo:
            raise ScripMasterError('no nse_fo scrip master link')
        text = c.fetch_text(fo)
        n = len(parse_scrip_master(text))
        if n < min_rows:
            raise ScripMasterError('scrip master has only %d option rows (< %d): truncated?'
                                   % (n, min_rows))
        tmp = cache.with_name('%s.%d.tmp' % (cache.name, os.getpid()))
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(cache)
        keep = S.HOME / ('scrip_refetch_%s' % today.strftime('%Y%m%d'))
        for old in list(S.HOME.glob('scrip_nse_fo_*.csv')) + list(S.HOME.glob('scrip_refetch_*')):
            if old not in (cache, keep):
                try:
                    old.unlink()
                except FileNotFoundError:
                    pass
        return ScripMap(parse_scrip_master(text, underlyings))
