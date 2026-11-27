"""kite_login.holidays: the 2026-11-24 parse (content-based, NSE only) + safe writes."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kite_login import holidays as H  # noqa: E402

# Zerodha's layout: trading table Date|Day|Holiday|Exchanges; settlement + MCX tables
# put Day first. 2026 weekday closures below = 9 (above the floor of 8).
TRADING = [('26 Jan 2026 Long weekend', 'Monday', 'Republic Day'),
           ('03 Mar 2026', 'Tuesday', 'Holi'), ('26 Mar 2026', 'Thursday', 'Ram Navami'),
           ('31 Mar 2026', 'Tuesday', 'Mahavir Jayanti'), ('03 Apr 2026', 'Friday', 'Good Friday'),
           ('14 Apr 2026', 'Tuesday', 'Ambedkar Jayanti'), ('01 May 2026', 'Friday', 'Maharashtra Day'),
           ('02 Oct 2026', 'Friday', 'Gandhi Jayanti'), ('24 Nov 2026', 'Tuesday', 'Guru Nanak Jayanti')]


def page(trading=TRADING, extra=''):
    rows = ''.join('<tr><td>%s</td><td>%s</td><td>%s</td><td>nse bse</td></tr>' % r
                   for r in trading)
    return ('<html><body><table><tr><th>Date</th><th>Day</th><th>Holiday</th><th>Exchanges</th></tr>'
            '%s</table><table><tr><td>Wednesday</td><td>19 Feb 2026</td><td>Shivaji Jayanti</td>'
            '<td>settlement</td></tr><tr><td>Friday</td><td>02 Jan 2026</td><td>MCX thing</td>'
            '<td>mcx</td></tr></table>%s</body></html>' % (rows, extra)) + ' ' * 1000


def test_parse_keeps_nse_closures_and_drops_settlement_and_mcx():
    got = H.parse(page(), 2026)
    dates = [h['date'] for h in got]
    assert '2026-11-24' in dates and '2026-01-26' in dates
    assert '2026-02-19' not in dates and '2026-01-02' not in dates
    assert next(h for h in got if h['date'] == '2026-11-24')['name'] == 'Guru Nanak Jayanti'
    assert dates == sorted(dates)


def test_refresh_writes_and_keeps_other_years(tmp_path):
    f = tmp_path / 'holiday_calendar.json'
    f.write_text(json.dumps({'2025': [{'date': '2025-12-25', 'name': 'Christmas'}]}))
    ok, msg = H.refresh(f, today=date(2026, 11, 30), fetch=page)
    assert ok, msg
    d = json.loads(f.read_text())
    assert d['2025'][0]['name'] == 'Christmas' and len(d['2026']) == 9
    assert d['_last_updated'] == '2026-11-30'


def test_refresh_is_once_a_day_unless_forced(tmp_path):
    f = tmp_path / 'h.json'
    calls = []

    def fetch():
        calls.append(1)
        return page()
    H.refresh(f, today=date(2026, 11, 30), fetch=fetch)
    ok, msg = H.refresh(f, today=date(2026, 11, 30), fetch=fetch)
    assert ok and 'already' in msg and len(calls) == 1
    H.refresh(f, force=True, today=date(2026, 11, 30), fetch=fetch)
    assert len(calls) == 2


def test_layout_change_refuses_to_overwrite(tmp_path):
    f = tmp_path / 'h.json'
    f.write_text('{"2026": [{"date": "2026-11-24", "name": "keep me"}]}')
    ok, msg = H.refresh(f, today=date(2026, 11, 30), fetch=lambda: page(TRADING[:3]))
    assert not ok and 'NOT overwritten' in msg
    assert json.loads(f.read_text())['2026'][0]['name'] == 'keep me'


def test_fetch_failure_keeps_yesterdays_file(tmp_path):
    f = tmp_path / 'h.json'
    f.write_text('{"2026": []}')

    def boom():
        raise ConnectionError('dns')
    ok, msg = H.refresh(f, today=date(2026, 11, 30), fetch=boom)
    assert not ok and 'kept' in msg and f.read_text() == '{"2026": []}'


def test_unreadable_existing_file_is_rebuilt(tmp_path):
    f = tmp_path / 'h.json'
    f.write_text('{broken')
    ok, _ = H.refresh(f, today=date(2026, 11, 30), fetch=page)
    assert ok and len(json.loads(f.read_text())['2026']) == 9


def test_same_answer_as_the_snail_parser_on_the_same_page():
    """Port check: the moved parser and SNAIL's give the same NSE dates."""
    snail = Path(__file__).resolve().parents[2] / 'SNAIL'
    if not (snail / 'src' / 'utils' / 'holiday_scraper.py').exists():
        return                                     # SNAIL deleted: nothing to compare
    import importlib.util
    try:
        import loguru  # noqa: F401
    except ImportError:
        return
    sys.path.insert(0, str(snail))
    try:
        spec = importlib.util.spec_from_file_location(
            'snail_hs', str(snail / 'src' / 'utils' / 'holiday_scraper.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return                                     # SNAIL config not importable here
    finally:
        sys.path.remove(str(snail))
    old = [h['date'] for h in mod.parse_holidays(page())]
    assert old == [h['date'] for h in H.parse(page(), date.today().year)]


# 2027: nine real weekdays (shifting the 2026 dates would land some on weekends)
NEXT = [('26 Jan 2027', 'Tuesday', 'Republic Day'), ('22 Mar 2027', 'Monday', 'Holi'),
        ('26 Mar 2027', 'Friday', 'Good Friday'), ('14 Apr 2027', 'Wednesday', 'Ambedkar Jayanti'),
        ('15 Apr 2027', 'Thursday', 'Ram Navami'), ('03 May 2027', 'Monday', 'X'),
        ('01 Oct 2027', 'Friday', 'Y'), ('29 Oct 2027', 'Friday', 'Diwali'),
        ('12 Nov 2027', 'Friday', 'Z')]


def test_page_already_rolled_to_next_year_still_refreshes(tmp_path):
    """Review 2026-11-27 #2: in December Zerodha may show only next year. That must
    refresh (next year written, this year kept), not fail every morning."""
    f = tmp_path / 'h.json'
    f.write_text('{"2026": [{"date": "2026-11-24", "name": "keep me"}]}')
    ok, msg = H.refresh(f, today=date(2026, 12, 15), fetch=lambda: page(NEXT))
    assert ok, msg
    d = json.loads(f.read_text())
    assert d['2026'][0]['name'] == 'keep me' and len(d['2027']) == 9


def test_a_thin_year_never_overwrites_a_good_one(tmp_path):
    """Full 2027 + three stray 2026 rows: 2027 written, the good 2026 kept."""
    f = tmp_path / 'h.json'
    f.write_text('{"2026": [{"date": "2026-11-24", "name": "keep me"}]}')
    ok, msg = H.refresh(f, today=date(2026, 12, 15), fetch=lambda: page(NEXT + TRADING[:3]))
    assert ok, msg
    d = json.loads(f.read_text())
    assert d['2026'] == [{'date': '2026-11-24', 'name': 'keep me'}] and len(d['2027']) == 9
