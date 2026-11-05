from datetime import date, datetime, timezone

import pytest

from nifty_btst.neo_symbols import (EPOCH_OFFSET_SEC, ScripMap, ScripMasterError,
                                    expiry_from_epoch, parse_scrip_master)

HEADER = ('pSymbol,pTrdSymbol,pSymbolName,pExpiryDate,dStrikePrice;,pOptionType,lLotSize,'
          'dTickSize,pExchSeg\n')


def epoch_for(d: date) -> int:
    """The file's 1980-based value for an expiry at 14:30 UTC on `d`."""
    unix = int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp())
    return unix - EPOCH_OFFSET_SEC


def row(tok, sym, name, exp, strike, opt, lot=65, tick=5):
    return '%s,%s,%s,%d,%d,%s,%d,%d,nse_fo\n' % (tok, sym, name, epoch_for(exp), strike * 100,
                                                 opt, lot, tick)


def test_epoch_offset_round_trips_the_sdk_arithmetic():
    d = date(2026, 11, 3)
    assert expiry_from_epoch(str(epoch_for(d))) == d
    assert expiry_from_epoch('%d.0' % epoch_for(d)) == d


def test_parse_filters_underlying_and_reads_fields():
    text = (HEADER + row(1, 'NIFTY26N0323000CE', 'NIFTY', date(2026, 11, 3), 23000, 'CE')
            + row(2, 'BANKNIFTY26NOV50000CE', 'BANKNIFTY', date(2026, 11, 26), 50000, 'CE', 30)
            + row(3, 'NIFTY26NOVFUT', 'NIFTY', date(2026, 11, 24), 0, 'XX')
            + ' pSymbol ,x,NIFTY,bad,1,PE,65,5,nse_fo\n')
    rows = parse_scrip_master(text, 'nifty')
    assert len(rows) == 1
    r = rows[0]
    assert (r.trading_symbol, r.token, r.strike, r.option_type, r.lot_size, r.tick_size,
            r.expiry) == ('NIFTY26N0323000CE', '1', 23000.0, 'CE', 65, 0.05, date(2026, 11, 3))


def test_parse_refuses_missing_columns_and_empty():
    with pytest.raises(ScripMasterError, match='lacks columns'):
        parse_scrip_master('a,b\n1,2\n', 'NIFTY')
    with pytest.raises(ScripMasterError, match='empty'):
        parse_scrip_master('', 'NIFTY')
    with pytest.raises(ScripMasterError, match='no NIFTY'):
        parse_scrip_master(HEADER + row(1, 'X', 'BANKNIFTY', date(2026, 11, 3), 1, 'CE'), 'NIFTY')


def test_map_finds_by_facts_not_strings_and_refuses_ambiguity():
    text = (HEADER + row(1, 'WEIRD-WEEKLY-SPELLING', 'NIFTY', date(2026, 11, 3), 23000, 'CE')
            + row(2, 'NIFTY26NOV23000CE', 'NIFTY', date(2026, 11, 24), 23000, 'CE')
            + row(3, 'DUP-A', 'NIFTY', date(2026, 11, 10), 22000, 'PE')
            + row(4, 'DUP-B', 'NIFTY', date(2026, 11, 10), 22000, 'PE'))
    m = ScripMap(parse_scrip_master(text, 'NIFTY'))
    assert len(m) == 4 and m.expiries()[0] == date(2026, 11, 3)
    hit = m.find(date(2026, 11, 3), 23000, 'ce')
    assert hit.trading_symbol == 'WEIRD-WEEKLY-SPELLING' and hit.token == '1'
    assert m.find_for_mark({'expiry': '2026-11-24', 'strike': 23000.0, 'side': 'CE'}).token == '2'
    with pytest.raises(ScripMasterError, match='0 NEO rows'):
        m.find(date(2026, 11, 3), 23050, 'CE')
    with pytest.raises(ScripMasterError, match='2 NEO rows'):
        m.find(date(2026, 11, 10), 22000, 'PE')


def test_pick_fo_path():
    assert ScripMap.pick_fo_path(['https://x/nse_cm-v1.csv', 'https://x/NSE_FO-v1.csv']).endswith('NSE_FO-v1.csv')
    assert ScripMap.pick_fo_path(['https://x/nse_cm.csv']) is None
