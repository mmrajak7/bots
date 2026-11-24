"""Both engines' market-open checks must say CLOSED on a weekday holiday.

2026-11-24 (Guru Nanak Jayanti): the calendar file was missing the day AND
neither engine consulted it for "is today a session" -- zebra tested weekends
only, `bcs.spread_monitor` tested the clock only. Both alerted every 5 minutes
into a closed market. The file is fixed by the scraper; this pins the second
half: a holiday IN the file closes the market for both engines.

And the inverse, because a guard that can silence an exit engine on a trading
day is the expensive direction: an unreadable calendar must leave the engines
OPEN on a weekday (noisy, never blind).

Run:  cd Helper && python -m pytest common/tests/test_market_open_respects_holidays.py -v
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common import nse_holidays as h             # noqa: E402
from bcs import spread_monitor as sm             # noqa: E402
from zebra import monitor as zm                  # noqa: E402

HOLIDAY = date(2026, 11, 24)      # Tuesday
TRADING = date(2026, 11, 25)      # Wednesday
NOON = (12, 0)


@pytest.fixture
def calendar(tmp_path, monkeypatch):
    def write(text):
        f = tmp_path / 'holiday_calendar.json'
        f.write_text(text, encoding='utf-8')
        monkeypatch.setattr(h, 'HOLIDAY_FILE', f)
        h.reload()
    yield write
    h.reload()


def _good(write):
    write(json.dumps({'2026': [{'date': HOLIDAY.isoformat(), 'name': 'Guru Nanak'}]}))


def _pin(monkeypatch, d):
    naive = datetime(d.year, d.month, d.day, *NOON)
    monkeypatch.setattr(sm, 'now_ist', lambda: naive)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(d.year, d.month, d.day, *NOON, tzinfo=tz)
    monkeypatch.setattr(zm, 'datetime', _Clock)


@pytest.mark.parametrize('engine', ['bcs', 'zebra'])
def test_a_listed_weekday_holiday_closes_the_market(engine, calendar, monkeypatch):
    _good(calendar)
    _pin(monkeypatch, HOLIDAY)
    is_open = sm.is_market_open if engine == 'bcs' else zm._is_market_open
    assert is_open() is False


@pytest.mark.parametrize('engine', ['bcs', 'zebra'])
def test_the_next_trading_day_is_open(engine, calendar, monkeypatch):
    _good(calendar)
    _pin(monkeypatch, TRADING)
    is_open = sm.is_market_open if engine == 'bcs' else zm._is_market_open
    assert is_open() is True


@pytest.mark.parametrize('engine', ['bcs', 'zebra'])
def test_an_unreadable_calendar_never_blinds_an_engine(engine, calendar, monkeypatch):
    calendar('{not json')
    _pin(monkeypatch, HOLIDAY)
    is_open = sm.is_market_open if engine == 'bcs' else zm._is_market_open
    assert is_open() is True


def test_the_monitor_stands_down_loudly_once_per_day(calendar, monkeypatch, tmp_path):
    """A calendar that can switch an exit engine off must say so the day it
    does -- once, not every 5-minute relaunch."""
    _good(calendar)
    _pin(monkeypatch, HOLIDAY)
    sent = []
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'send_telegram', lambda msg, *a, **k: sent.append(msg))
    monkeypatch.setattr(sm, 'log', lambda *a, **k: None)
    assert sm.holiday_standdown() is True
    assert sm.holiday_standdown() is True
    assert len(sent) == 1 and 'NOT being watched' in sent[0]


def test_no_standdown_on_a_trading_day(calendar, monkeypatch, tmp_path):
    _good(calendar)
    _pin(monkeypatch, TRADING)
    monkeypatch.setattr(sm, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(sm, 'send_telegram', lambda *a, **k: pytest.fail('alerted'))
    assert sm.holiday_standdown() is False
