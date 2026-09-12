"""Same-stock re-entries are TAGGED and MEASURED, never blocked.

Owner, 2026-09-11: re-entering names traded in the last two weeks looked like
a defect. Measured on the cohort, the 8 re-entries (all on the same weekly ST
line) went 4/0 closed for +Rs 15,384 net while first entries went 13/9 for
−Rs 13,209 — too small a sample to block or to prefer, so the decision was to
tag every entry and score both populations forward (`zebra/reentry.py`).

Pinned here: the prior is found as of the entry and never from the future;
the stamp is written at entry and can never stop one; the scorecard and the
vet context both see it; and nothing refuses a signal.

Run:  cd Helper && python -m pytest zebra/tests/test_reentry_tag.py -v
"""
import contextlib
import io
from datetime import date

from zebra import reentry
from zebra.tests.test_quote_prefetch import one_open_position  # noqa: F401


def _pos(id, stock='ACME', status='exited', entry=('2026-09-01', '10:00:00'),
         exit_=('2026-09-03', '11:00:00'), direction='PE', timeframe='weekly',
         st=100.0, net=-500.0, reason='paper:debit_sl', cohort='2026-08-14'):
    t = {'id': id, 'stock': stock, 'status': status, 'entry_date': entry[0],
         'entry_time': entry[1], 'direction': direction, 'timeframe': timeframe,
         'st_value': st, 'cohort': cohort}
    if status == 'exited':
        t.update(exit_date=exit_[0], exit_time=exit_[1], exit_reason=reason,
                 pnl=net, pnl_net=net, pnl_net_pct=net / 10.0)
    return t


# ── the prior, as of the entry ──────────────────────────────────────────────

def test_the_most_recent_prior_position_on_the_stock_is_found():
    older = _pos(1, entry=('2026-08-10', '10:00:00'), exit_=('2026-08-12', '10:00:00'))
    last = _pos(2, entry=('2026-09-01', '10:00:00'), exit_=('2026-09-03', '11:00:00'))
    other = _pos(3, stock='OTHER', entry=('2026-09-04', '10:00:00'))
    this = _pos(4, status='entered', entry=('2026-09-05', '09:30:00'))
    p = reentry.prior_position([older, last, other, this], this)
    assert p['id'] == 2 and p['days_since'] == 2
    assert p['closed_before_entry'] and p['pnl_net'] == -500.0
    assert p['same_direction'] and p['same_timeframe'] and p['same_st']
    assert reentry.is_reentry(p)


def test_nothing_from_after_the_entry_is_used():
    """A later position is not a prior, and a prior's result that was not yet
    known at entry is not reported — the look-ahead that made two earlier
    measurements in this book wrong."""
    this = _pos(1, status='entered', entry=('2026-09-05', '09:30:00'))
    later = _pos(2, entry=('2026-09-08', '10:00:00'), exit_=('2026-09-09', '10:00:00'))
    assert reentry.prior_position([this, later], this) is None

    straddles = _pos(3, entry=('2026-09-01', '10:00:00'), exit_=('2026-09-06', '10:00:00'))
    p = reentry.prior_position([straddles, this], this)
    assert p['closed_before_entry'] is False
    assert p['pnl_net'] is None and p['exit_reason'] is None
    assert p['days_since'] == 4                       # counted from its entry


def test_the_same_day_order_is_decided_by_the_time():
    """BHARATFORG #515 closed on its trail at 12:45 and #533 entered later the
    same afternoon."""
    trail = _pos(1, entry=('2026-09-10', '10:00:00'), exit_=('2026-09-11', '12:45:21'),
                 net=2945.0, reason='paper:trail')
    again = _pos(2, status='entered', entry=('2026-09-11', '13:10:00'))
    p = reentry.prior_position([trail, again], again)
    assert p['id'] == 1 and p['days_since'] == 0 and p['closed_before_entry']

    before = _pos(3, status='entered', entry=('2026-09-11', '09:15:00'))
    assert reentry.prior_position([trail, before], before)['id'] == 1
    assert reentry.prior_position([trail, before], before)['closed_before_entry'] is False


def test_a_new_st_line_is_a_different_signal():
    prior = _pos(1, st=100.0)
    moved = _pos(2, status='entered', entry=('2026-09-05', '10:00:00'), st=103.0)
    assert reentry.prior_position([prior, moved], moved)['same_st'] is False
    nost = dict(moved, st_value=None)
    assert reentry.prior_position([prior, nost], nost)['same_st'] is None


def test_a_first_entry_is_not_a_reentry_and_the_window_is_inclusive():
    this = _pos(1, status='entered')
    assert reentry.prior_position([this], this) is None
    assert not reentry.is_reentry(None)
    assert reentry.is_reentry({'days_since': reentry.WINDOW_DAYS})
    assert not reentry.is_reentry({'days_since': reentry.WINDOW_DAYS + 1})


def test_the_summary_scores_on_net_and_counts_open_separately():
    s = reentry.summarise([_pos(1, net=100.0), _pos(2, net=-50.0),
                           _pos(3, net=0.0), _pos(4, status='entered')])
    assert s == {'entries': 4, 'closed': 3, 'wins': 1, 'losses': 2,
                 'net': 50, 'open': 1}


def test_a_stamped_prior_is_used_as_stamped():
    stamped = dict(_pos(2, status='entered'), prior_position={'id': 99, 'days_since': 1})
    assert reentry.prior_for([], stamped)['id'] == 99


# ── the stamp, at entry ─────────────────────────────────────────────────────

def _enter_again(store, monkeypatch):
    from zebra import monitor
    from zebra.tests import test_bcs_only as bo
    t1 = store.find(1)
    store.mark_exited(1, float(t1['entry_spot']), float(t1['debit']), 'paper:trail')
    store.add_signal(dict(bo.SIGNAL))
    monitor.check_watching(store, kite=None, dry_run=True)
    return [t for t in store.load_trades() if t['id'] != 1][-1]


def test_an_entry_is_stamped_with_its_prior_position(one_open_position, monkeypatch):  # noqa: F811
    store, first = one_open_position
    assert reentry.FIELD in first and first[reentry.FIELD] is None, 'a first entry'
    second = _enter_again(store, monkeypatch)
    assert second['status'] == 'entered', second.get('status')
    p = second[reentry.FIELD]
    assert p['id'] == 1 and p['closed_before_entry'] and p['days_since'] == 0
    assert p['exit_reason'] == 'paper:trail'


def test_a_failing_tag_never_stops_an_entry(one_open_position, monkeypatch):  # noqa: F811
    store, _first = one_open_position

    def boom(book, trade):
        raise RuntimeError('tag broke')
    monkeypatch.setattr(reentry, 'prior_position', boom)
    second = _enter_again(store, monkeypatch)
    assert second['status'] == 'entered', 'a measurement stopped an entry'
    assert reentry.FIELD not in second


# ── the readers ─────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, book):
        self.book = book

    def load_trades(self):
        return self.book


def test_the_scorecard_splits_reentries_from_first_entries(monkeypatch):
    from zebra import __main__ as cli
    from zebra import trade_store
    book = [_pos(1, entry=('2026-09-01', '10:00:00'), exit_=('2026-09-03', '10:00:00'),
                 net=-500.0),
            _pos(2, entry=('2026-09-05', '10:00:00'), exit_=('2026-09-08', '10:00:00'),
                 net=900.0, reason='paper:tp'),
            _pos(3, stock='OTHER', status='entered', entry=('2026-09-06', '10:00:00'))]
    assert len(trade_store.scored(book)) == 3, 'fixture not in the cohort'
    monkeypatch.setattr(trade_store, 'get_store', lambda: _Store(book))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_reentry(type('A', (), {'days': None})())
    text = out.getvalue()
    assert 'TAGGED, NEVER BLOCKED' in text
    assert 're-entry <=14d   entries   1 | closed   1  W/L   1/0' in text, text
    assert 'first entry      entries   2 | closed   1  W/L   0/1' in text, text
    assert 'CENSORED' in text


def test_the_vet_sees_the_names_recent_positions_and_how_to_read_them(monkeypatch):
    from zebra import __main__ as cli
    from zebra import trade_store
    today = date.today().isoformat()
    book = [_pos(1, entry=(today, '10:00:00'), exit_=(today, '11:00:00')),
            _pos(2, stock='OTHER', entry=(today, '10:00:00'), exit_=(today, '11:00:00')),
            _pos(3, entry=('2020-01-01', '10:00:00'), exit_=('2020-01-02', '10:00:00'))]
    monkeypatch.setattr(trade_store, 'get_store', lambda: _Store(book))
    ctx = cli._recent_same_stock_for({'id': 9, 'stock': 'ACME'})
    assert [p['id'] for p in ctx['positions']] == [1]
    assert 'not a veto ground' in ctx['note']


def test_nothing_in_the_entry_path_refuses_a_signal_on_reentry():
    """TAGGED, never blocked — until the scorecard can carry a rule, no gate
    may read the tag.

    RETIRES WHEN: the owner turns the re-entry scorecard into a rule, at which
    point this guard is replaced by the rule's own tests.
    """
    import inspect
    from zebra import monitor, scanner, strikes
    for mod in (monitor, scanner, strikes):
        src = inspect.getsource(mod)
        assert 'reentry' not in src and 'prior_position' not in src, mod.__name__
