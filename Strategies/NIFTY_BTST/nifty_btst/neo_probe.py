"""Stage 0: the read-only NEO probe. Login, baseUrl, scrip master, positions,
limits, order book, trade book. Prints the RAW envelopes so the shapes the
audit could not verify (empty-book marker, limits field names, weekly
`pTrdSymbol`, the `pExpiryDate` offset) are seen before any order exists.

This module is built on `NeoReader` and must stay that way: it has no import
of `NeoTrader`, no `place`, no `cancel`. `tests/test_probe_safety.py` asserts
it structurally.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from .neo_client import (PATH_LIMITS, PATH_ORDER_BOOK, PATH_POSITIONS, PATH_TRADES, NeoAuthError,
                         NeoReader, NeoUnavailable)
from .broker import redact_row
from .neo_symbols import ScripMap, ScripMasterError, parse_scrip_master

Out = Callable[[str], None]


def _redact(d: Dict[str, Any]) -> Dict[str, Any]:
    # Tokens, and also the UCC and the account name: probe output gets pasted
    # into chats (NEO-35).
    return {k: ('<redacted>' if k.lower() in ('token', 'sid', 'auth', 'rid', 'ucc',
                                              'greetingname') else v)
            for k, v in d.items()}


def _show(out: Out, title: str, data: Any, limit: int = 1500) -> None:
    text = json.dumps(data, indent=1, default=str, sort_keys=True)
    out('--- %s ---' % title)
    out(text if len(text) <= limit else text[:limit] + '\n... (%d chars)' % len(text))


def run_probe(reader: NeoReader, underlying: str, today: date,
              kite_row: Optional[Dict[str, Any]] = None, out: Out = print,
              net_field: str = 'Net') -> int:
    """Returns 0 when every read answered, 1 when something could not be read.
    Never raises on a NEO failure: each section reports its own."""
    rc = 0
    out('NEO PROBE (read-only) %s' % today)
    try:
        summary = reader.login()
    except NeoAuthError as exc:
        out('LOGIN FAILED: %s' % exc)
        return 1
    _show(out, 'login (redacted)', _redact(summary))
    ucc = str(reader.creds.get('ucc', ''))
    raw_out = out

    def out_safe(text: str) -> None:
        raw_out(text.replace(ucc, '<redacted>') if ucc else text)
    out = out_safe

    try:
        paths = reader.scrip_master_paths()
        _show(out, 'scrip master paths', paths)
        fo = ScripMap.pick_fo_path(paths)
        if not fo:
            out('NO nse_fo scrip master link in filesPaths')
            rc = 1
        else:
            text = reader.fetch_text(fo)
            rows = parse_scrip_master(text, underlying)
            smap = ScripMap(rows)
            out('scrip master: %d %s option rows, expiries %s'
                % (len(smap), underlying, ', '.join(e.isoformat() for e in smap.expiries()[:8])))
            sample = sorted(rows, key=lambda r: (r.expiry, r.strike))[0]
            _show(out, 'sample row (first expiry)', sample.__dict__)
            if kite_row:
                try:
                    hit = smap.find(date.fromisoformat(str(kite_row['expiry'])),
                                    float(kite_row['strike']), str(kite_row['instrument_type']))
                    out('KITE %s (exp %s strike %s %s lot %s) -> NEO pTrdSymbol=%s pSymbol=%s '
                        'lot=%d tick=%.2f expiry(derived)=%s  %s'
                        % (kite_row['tradingsymbol'], kite_row['expiry'], kite_row['strike'],
                           kite_row['instrument_type'], kite_row.get('lot_size'),
                           hit.trading_symbol, hit.token, hit.lot_size, hit.tick_size,
                           hit.expiry, 'LOT MATCH' if hit.lot_size == int(kite_row.get('lot_size', -1))
                           else 'LOT MISMATCH'))
                except ScripMasterError as exc:
                    out('KITE %s -> NEO: NOT FOUND (%s)' % (kite_row['tradingsymbol'], exc))
                    rc = 1
    except (NeoUnavailable, NeoAuthError, ScripMasterError) as exc:
        out('SCRIP MASTER FAILED: %s' % exc)
        rc = 1

    for title, path, jdata in (('positions', PATH_POSITIONS, None),
                               ('order book', PATH_ORDER_BOOK, None),
                               ('trade book', PATH_TRADES, None),
                               ('limits', PATH_LIMITS, {'seg': 'ALL', 'exch': 'ALL', 'prod': 'ALL'})):
        try:
            raw = reader.raw_report(path, jdata)
        except (NeoUnavailable, NeoAuthError) as exc:
            out('%s FAILED: %s' % (title.upper(), exc))
            rc = 1
            continue
        env = redact_row({k: v for k, v in raw.items() if k != 'data'})   # R3-30
        data = raw.get('data')
        env['data_type'] = type(data).__name__
        env['data_len'] = len(data) if isinstance(data, (list, dict)) else None
        _show(out, '%s envelope' % title, env)
        if isinstance(data, list) and data:
            first = data[0]
            _show(out, '%s first row' % title, redact_row(first) if isinstance(first, dict) else first)
        elif isinstance(data, dict):
            _show(out, '%s data' % title, redact_row(data))
        if title == 'limits':
            keys: List[str] = sorted(raw.keys())
            if isinstance(data, dict):
                keys += ['data.' + k for k in sorted(data.keys())]
            out('limits keys: %s' % ', '.join(keys))
            # The envelope print is truncated and the field sits past the cut, so
            # say its value outright: the owner compares it with the NEO app.
            net = data.get(net_field) if isinstance(data, dict) else None
            out('limits %s = %s  (must equal AVAILABLE rupees in the NEO app)'
                % (net_field, raw.get(net_field, net)))
            out('neo.limits_net_field must name the AVAILABLE rupees above.')
    out('PROBE %s' % ('OK' if rc == 0 else 'INCOMPLETE'))
    return rc
