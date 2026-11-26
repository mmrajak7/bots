"""twin: live fills against the paper twin (replaces the Volrix gate as the
implementation check, owner 2026-11-26)."""
import json

from nifty_btst import twin


def _p(date, side, entry, exit_, sym='NIFTY26D0826000CE', status='closed', **kw):
    d = {'entry_date': date, 'side': side, 'entry_price': entry, 'exit_price': exit_,
         'symbol': sym, 'status': status, 'qty': 65, 'exit_reason': 'next_day_0925'}
    d.update(kw)
    return d


def test_extra_cost_splits_into_entry_and_exit():
    live = [_p('2026-12-01', 'CE', 152.0, 200.0)]
    paper = [_p('2026-12-01', 'CE', 150.0, 201.0)]
    res = twin.pair(live, paper)
    r = res['pairs'][0]
    assert r['entry_cost'] == 2.0 and r['exit_cost'] == 1.0 and r['extra_cost'] == 3.0
    assert r['extra_rs'] == 195.0 and res['mean_extra'] == 3.0 and res['total_extra_rs'] == 195.0
    assert 'extra +3.00 = Rs +195' in twin.render(res)


def test_live_better_than_paper_is_negative():
    res = twin.pair([_p('2026-12-01', 'PE', 149.0, 80.0)], [_p('2026-12-01', 'PE', 150.0, 79.0)])
    assert res['pairs'][0]['extra_cost'] == -2.0


def test_different_strike_is_kept_and_flagged():
    live = [_p('2026-12-01', 'CE', 150.0, 160.0, sym='A')]
    paper = [_p('2026-12-01', 'CE', 150.0, 170.0, sym='B')]
    res = twin.pair(live, paper)
    assert len(res['pairs']) == 1 and not res['pairs'][0]['same_strike']
    assert res['mean_extra'] == 10.0 and res['mean_extra_same_strike'] is None
    assert 'DIFFERENT STRIKE' in twin.render(res)


def test_unpaired_open_and_never_filled_are_not_pairs():
    live = [_p('2026-12-01', 'CE', 150.0, 160.0),
            _p('2026-12-02', 'CE', 150.0, 0.0, status='open'),
            _p('2026-12-03', 'PE', 150.0, 150.0, never_filled=True),
            _p('2026-12-04', 'PE', 150.0, 140.0)]
    paper = [_p('2026-12-01', 'CE', 150.0, 160.0), _p('2026-12-02', 'CE', 150.0, 170.0),
             _p('2026-12-03', 'PE', 150.0, 120.0), _p('2026-12-05', 'CE', 150.0, 99.0)]
    res = twin.pair(live, paper)
    assert [(r['date'], r['side']) for r in res['pairs']] == [('2026-12-01', 'CE')]
    assert res['live_only'] == [('2026-12-04', 'PE')]
    assert res['paper_only'] == [('2026-12-02', 'CE'), ('2026-12-03', 'PE'), ('2026-12-05', 'CE')]
    text = twin.render(res)
    assert 'PAPER only (live missed it): 2026-12-02 CE' in text and 'LIVE only' in text


def test_empty_and_garbage_books_never_raise(tmp_path):
    assert twin.pair([], [])['mean_extra'] is None
    bad = tmp_path / 'bad.json'
    bad.write_text('{{{')
    assert twin.load_positions(bad) == [] and twin.load_positions(tmp_path / 'none.json') == []
    (tmp_path / 'list.json').write_text('[]')
    assert twin.load_positions(tmp_path / 'list.json') == []
    junk = [{'status': 'closed', 'entry_price': 'x', 'exit_price': 1}, 'nope']
    assert twin.pair(junk, junk)['pairs'] == []           # type: ignore[arg-type]
    assert '0 pairs' in twin.report(bad, bad)


def test_report_since_filters_both_books(tmp_path):
    lb, pb = tmp_path / 'l.json', tmp_path / 'p.json'
    lb.write_text(json.dumps({'positions': [_p('2026-11-30', 'CE', 1, 2), _p('2026-12-01', 'CE', 1, 2)]}))
    pb.write_text(json.dumps({'positions': [_p('2026-11-20', 'PE', 1, 2), _p('2026-12-01', 'CE', 1, 3)]}))
    text = twin.report(lb, pb, since='2026-12-01')
    assert text.startswith('LIVE vs PAPER: 1 pairs') and 'PAPER only' not in text


def test_bad_qty_is_skipped_and_estimated_or_external_rows_stay_out_of_the_means():
    live = [_p('2026-12-01', 'CE', 150.0, 160.0, qty='x'),
            _p('2026-12-02', 'CE', 150.0, 160.0, entry_price_estimated=True),
            _p('2026-12-03', 'CE', 150.0, 160.0, exit_reason='closed_externally'),
            _p('2026-12-04', 'CE', 150.0, 160.0)]
    paper = [_p(d, 'CE', 150.0, 170.0) for d in ('2026-12-01', '2026-12-02', '2026-12-03')]
    paper.append(_p('2026-12-04', 'CE', 150.0, 162.0))
    res = twin.pair(live, paper)
    assert res['skipped'] == 1 and res['flagged'] == 2 and len(res['pairs']) == 3
    assert res['mean_extra'] == 2.0 and res['total_extra_rs'] == 130.0
    text = twin.render(res)
    assert 'estimated price: not in the means' in text and 'external price' in text
    assert '1 pair(s) skipped' in text
