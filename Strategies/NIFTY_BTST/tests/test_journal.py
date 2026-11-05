from datetime import date

import pytest

from conftest import T
from nifty_btst.journal import Journal, make_tag, parse_tag


def test_tag_round_trip():
    tag = make_tag(date(2026, 10, 26), 7, 'EXIT', 3)
    assert tag == '20261026-7-EXIT-3'
    assert parse_tag(tag) == {'day': date(2026, 10, 26), 'id': 7, 'kind': 'EXIT', 'attempt': 3}
    assert parse_tag('garbage') is None and parse_tag('20261026-x-EXIT-1') is None
    with pytest.raises(ValueError):
        make_tag(date(2026, 10, 26), 1, 'STOP', 1)


def test_unresolved_and_counts(tmp_path):
    j = Journal(tmp_path)
    t = T('2026-10-26', '10:00:00')
    assert j.unresolved() == [] and j.sends_today(t.date()) == 0
    j.intent(t, '20261026-1-ENTRY-1', 'ENTRY', 1, 'S', 'B', 65, '10.00')
    j.placed(t, '20261026-1-ENTRY-1', '111')
    j.intent(t, '20261026-1-ENTRY-2', 'ENTRY', 1, 'S', 'B', 65, '10.05')     # fate unknown
    j.intent(t, '20261026-2-EXIT-1', 'EXIT', 2, 'S', 'S', 65, '9.00')
    j.not_placed(t, '20261026-2-EXIT-1', 'rejected')
    j.intent(t, '20261025-9-EXIT-1', 'EXIT', 9, 'S', 'S', 65, '9.00')         # yesterday
    j.bound(t, '20261025-9-EXIT-1', '222', 'tag')
    assert [u['tag'] for u in j.unresolved()] == ['20261026-1-ENTRY-2']
    assert j.sends_today(t.date()) == 3
    assert j.sends_today(t.date(), 'ENTRY') == 2 and j.sends_today(t.date(), 'EXIT') == 1


def test_torn_last_line_does_not_hide_earlier_intents(tmp_path):
    j = Journal(tmp_path)
    t = T('2026-10-26', '10:00:00')
    j.intent(t, '20261026-1-ENTRY-1', 'ENTRY', 1, 'S', 'B', 65, '10.00')
    with open(j.path, 'a') as f:
        f.write('{"ev": "PLACED", "tag": "20261026-1-ENT')      # power cut mid-line
    assert [u['tag'] for u in j.unresolved()] == ['20261026-1-ENTRY-1']
    assert any(line.get('ev') == 'TORN' for line in j.lines())
