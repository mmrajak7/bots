"""The veto shadow measures; it must never act or reach the 100-trade test.

Mostly about what the module cannot do: touch the test's book, score a repeat,
backfill an unobserved head, or book a win nobody could have taken.
"""
import ast
import json
from datetime import datetime
from pathlib import Path

import pytest

from zebra import golive
from zebra import structure_shadow as ss
from zebra import veto_naked as vn
from zebra import vet as vet_mod

NOW = datetime(2026, 11, 6, 11, 0, 0)


class _Clock(datetime):
    """Pins `poll`'s market clock to the same moment as the vet clock."""
    at = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.at


TODAY = '2026-11-06T10:58:00'


def _veto(tid=1, stock='ACME', direction='CE', st=110.0, decided=TODAY, state='vetoed', **kw):
    t = {'id': tid, 'status': 'triggered', 'stock': stock, 'direction': direction,
         'st_value': st, 'timeframe': 'weekly', 'signal_date': '2026-11-06',
         'vet': {'state': state, 'decided_at': decided, 'decision_id': 7,
                 'context': {'expiry': '2026-11-24', 'lot_size': 100, 'swing_tp': None,
                             'bcs': {'long_symbol': 'ACME26NOV100CE', 'long_strike': 100.0,
                                     'target_spot': st, 'long_ask': 4.0}}}}
    t.update(kw)
    return t


class _Store:
    def __init__(self, trades):
        self.trades = trades

    def load_trades(self):
        return self.trades


def _q(bid=5.0, ask=5.2, reliable=True):
    return {'bid': bid, 'ask': ask, 'reliable': reliable, 'unreliable_reason': ''}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Isolated books, a pinned clock, and a settable quote + spot."""
    monkeypatch.setattr(vn, 'STATE_FILE', tmp_path / 'veto.json')
    monkeypatch.setattr(ss, 'STATE_FILE', tmp_path / 'structures.json')
    monkeypatch.setattr(vet_mod, '_now', lambda: NOW)
    monkeypatch.setattr(vn, 'datetime', _Clock)
    box = {'q': _q(), 'spot': 100.0}
    monkeypatch.setattr(vn.strikes_mod, 'prefetch_quotes', lambda k, s: 0)
    monkeypatch.setattr(vn.strikes_mod, '_quote_option', lambda k, s: box['q'])
    monkeypatch.setattr(vn, '_spots', lambda k, n: {x: box['spot'] for x in n})
    return box


def _row(tid='1'):
    return vn.load_state()['shadows'][tid]


# -- opening -----------------------------------------------------------------

def test_a_veto_opens_at_the_live_ask_not_the_stale_one(env):
    vn.poll(_Store([_veto()]), None)
    sh = _row()
    assert sh['arms'][vn.ARM]['entry_value'] == 5.2      # live ask
    assert sh['ask_at_request'] == 4.0                   # kept, not used
    assert (sh['quantity'], sh['target_spot'], sh['opened_late']) == (100, 110.0, False)


def test_a_veto_passes_the_real_population_filter(env):
    """A veto never carries a cohort stamp. `scored()` would drop every one, and
    the fixtures must not hide that by stubbing the filter out."""
    t = _veto()
    assert 'cohort' not in t
    vn.poll(_Store([t]), None)
    assert '1' in vn.load_state()['shadows']


def test_the_cycle_calls_it(env, monkeypatch):
    """Not wired in means no measurement, and nothing would ever fail."""
    from zebra import monitor
    calls = []
    monkeypatch.setattr(monitor.veto_naked, 'poll', lambda s, k: calls.append(s))
    monkeypatch.setattr(monitor.structure_shadow, 'poll', lambda s, k: None)
    store = _Store([])
    for name in dir(monitor):
        if name.startswith(('check_', '_alert_', '_arming_', '_run_vet', '_reap_')):
            fn = getattr(monitor, name)
            if callable(fn):
                monkeypatch.setattr(monitor, name, lambda *a, **k: None)
    monitor.run_cycle(store, kite=None, dry_run=True, do_scan=False)
    assert calls == [store]


def test_an_allowed_signal_opens_nothing(env):
    vn.poll(_Store([_veto(state='allowed')]), None)
    assert vn.load_state()['shadows'] == {}


def test_a_past_veto_is_never_backfilled_or_reported(env):
    """First run on the box must not open the whole history at today's prices,
    nor report all of it as missed."""
    vn.poll(_Store([_veto(decided='2026-11-05T10:00:00'), _veto(2, st=111.0)]), None)
    st = vn.load_state()
    assert list(st['shadows']) == ['2']
    assert st['unopened'] == {}


def test_a_verdict_after_the_last_cycle_is_reported_not_lost(env, monkeypatch):
    """Cycles stop at the close, so a veto landing after the last one never gets
    a row. An outcome that vanishes silently biases the count."""
    vn.poll(_Store([_veto(1)]), None)             # book started
    late = _veto(2, st=111.0, decided='2026-11-06T15:29:50')
    monkeypatch.setattr(vet_mod, '_now', lambda: datetime(2026, 11, 7, 9, 20))
    vn.poll(_Store([_veto(1), late]), None)
    vn.poll(_Store([_veto(1), late]), None)
    st = vn.load_state()
    assert '2' not in st['shadows']
    assert st['unopened']['2']['reason'] == 'missed_session'
    assert st['unopened']['2']['attempts'] == 1


def test_a_miss_keeps_the_reason_it_actually_failed_on(env, monkeypatch):
    env['q'] = _q(reliable=False)
    vn.poll(_Store([_veto()]), None)
    monkeypatch.setattr(vet_mod, '_now', lambda: datetime(2026, 11, 7, 9, 20))
    vn.poll(_Store([_veto()]), None)
    assert vn.load_state()['unopened']['1']['reason'] == 'no_quote'


def test_yesterdays_failure_is_not_counted_as_waiting(env, monkeypatch, caplog):
    env['q'] = _q(reliable=False)
    vn.poll(_Store([_veto()]), None)
    monkeypatch.setattr(vet_mod, '_now', lambda: datetime(2026, 11, 7, 9, 20))
    caplog.set_level('INFO', logger=vn.logger.name)
    vn.poll(_Store([_veto()]), None)
    assert '0 waiting on a price' in caplog.text


def test_two_vetoes_of_one_setup_in_one_cycle_open_one_row(env):
    """The first still waiting on a quote when the second arrives."""
    vn.poll(_Store([_veto(1), _veto(2)]), None)
    st = vn.load_state()
    assert list(st['shadows']) == ['1']
    vn.poll(_Store([_veto(1), _veto(2)]), None)
    assert vn.load_state()['shadows']['1']['repeat_ids'] == ['2']


def test_a_shadow_record_does_not_open_a_second_row(env):
    vn.poll(_Store([_veto(shadow_of=9)]), None)
    assert vn.load_state()['shadows'] == {}


def test_one_setup_one_row_and_repeats_are_counted_not_scored(env):
    vn.poll(_Store([_veto(1)]), None)
    vn.poll(_Store([_veto(1), _veto(2)]), None)
    st = vn.load_state()
    assert list(st['shadows']) == ['1']
    assert st['shadows']['1']['repeat_ids'] == ['2']


def test_a_new_line_is_a_new_setup(env):
    vn.poll(_Store([_veto(1), _veto(2, st=111.0)]), None)
    assert sorted(vn.load_state()['shadows']) == ['1', '2']


def test_an_applied_swing_target_is_used(env):
    t = _veto()
    t['vet']['context']['swing_tp'] = {'applied': True, 'tp_spot': 105.0}
    vn.poll(_Store([t]), None)
    assert _row()['target_spot'] == 105.0


def test_an_unapplied_swing_target_is_ignored(env):
    t = _veto()
    t['vet']['context']['swing_tp'] = {'applied': False, 'tp_spot': 105.0}
    vn.poll(_Store([t]), None)
    assert _row()['target_spot'] == 110.0


@pytest.mark.parametrize('direction,spot', [('CE', 110.0), ('PE', 90.0)])
def test_already_through_the_line_is_refused_not_an_instant_win(env, direction, spot):
    env['spot'] = spot
    st = 110.0 if direction == 'CE' else 95.0
    vn.poll(_Store([_veto(direction=direction, st=st)]), None)
    state = vn.load_state()
    assert state['shadows'] == {}
    assert state['unopened']['1']['reason'] == 'already_at_target'


def test_no_quote_waits_then_opens_and_clears_the_note(env):
    env['q'] = _q(reliable=False)
    vn.poll(_Store([_veto()]), None)
    assert vn.load_state()['unopened']['1']['reason'] == 'no_quote'
    env['q'] = _q()
    vn.poll(_Store([_veto()]), None)
    st = vn.load_state()
    assert '1' in st['shadows'] and st['unopened'] == {}


def test_missing_contract_is_reported_and_not_retried(env):
    t = _veto()
    t['vet']['context']['bcs'].pop('long_symbol')
    vn.poll(_Store([t]), None)
    vn.poll(_Store([t]), None)
    assert vn.load_state()['unopened']['1']['attempts'] == 1


def test_a_late_row_is_marked_partial(env):
    vn.poll(_Store([_veto(decided='2026-11-06T09:30:00')]), None)
    assert _row()['opened_late'] is True


# -- it cannot touch the test --------------------------------------------------

def test_the_test_book_is_never_written(env, tmp_path):
    vn.poll(_Store([_veto()]), None)
    assert not (tmp_path / 'structures.json').exists()


def test_the_book_is_its_own_file():
    """The fixtures repoint both paths, so only this can catch a shared file.

    RETIRES WHEN: the 100-trade test has reached its verdict, because the
    population it protects is then closed.
    """
    src = Path(vn.__file__).read_text(encoding='utf-8')
    assert "STATE_FILE = cfg.LOG_DIR / 'shadow_veto_naked.json'" in src


def test_a_veto_row_cannot_block_a_real_shadow_on_the_same_id(env):
    """Shared keys would make `open_shadows` skip it, costing the test a trade."""
    vn.poll(_Store([_veto()]), None)
    assert '1' in vn.load_state()['shadows']
    state = ss._load()
    assert state['shadows'] == {}
    entered = {'id': 1, 'status': 'entered', 'cohort': '2026-08-14', 'stock': 'ACME',
               'direction': 'CE', 'long_symbol': 'ACME26NOV100CE', 'tp_spot': 110.0,
               'expiry': '2026-11-24', 'quantity': 100, 'entry_spot': 100.0,
               'entry_date': '2026-11-06', 'long_ask_entry': 5.0, 'debit': 2.0, 'width': 4.0}
    assert ss.open_shadows(state, [entered], '2026-11-06 11:00:00') == 1


def test_a_row_never_enters_the_test_population(env):
    vn.poll(_Store([_veto()]), None)
    rows = golive.arm_rows(ss._load(), {}, 'naked_long', NOW)
    assert rows == []


def test_the_module_cannot_act():
    """Structural: no order, alert or store write can be reached from here.

    RETIRES WHEN: this book is folded into a live path, at which point it needs
    the money path's own tests instead of a promise that it never acts.
    """
    src = Path(vn.__file__).read_text(encoding='utf-8')
    names = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Name):
            names.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update(a.name for a in n.names)
    assert not names & {'place_order', 'modify_order', 'cancel_order', 'add_trade',
                        'mark_entered_bcs', 'mark_exited', 'mark_triggered', 'mark_cancelled',
                        'update_trade', 'save_trades', '_mutate', 'requests', 'send_message'}
    assert 'telegram' not in src.lower()


# -- same exits as the arm the test scores --------------------------------------

def _advance(sh, spot, q, when):
    ss.poll_one(sh, spot, q, None, when, when.date())


def test_it_exits_by_the_same_cascade_as_naked_long(env):
    vn.poll(_Store([_veto()]), None)
    sh = _row()
    twin = {'direction': 'CE', 'target_spot': 110.0, 'expiry': '2026-11-24',
            'entry_spot': 100.0, 'quantity': 100, 'polls': 0, 'long_ask_entry': 5.2,
            'arms': {vn.ARM: json.loads(json.dumps(sh['arms'][vn.ARM]))}}
    t0 = datetime(2026, 11, 6, 12, 0)
    for i, (spot, q) in enumerate([(99.0, _q(2.5, 2.7)), (98.0, _q(2.4, 2.6)), (97.0, _q(2.3, 2.5))]):
        when = t0.replace(minute=5 * i)
        _advance(sh, spot, q, when)
        _advance(twin, spot, q, when)
    assert sh['arms'][vn.ARM]['exit']['reason'] == 'stop'
    keep = ('reason', 'value', 'pnl_pct')
    assert ({k: sh['arms'][vn.ARM]['exit'][k] for k in keep}
            == {k: twin['arms'][vn.ARM]['exit'][k] for k in keep})


def test_poll_advances_a_row_to_its_exit(env):
    """Through `poll`, not `poll_one`: without this the loop could go and every
    other test would still pass."""
    store = _Store([_veto()])
    vn.poll(store, None)
    assert _row()['polls'] == 0                 # never polled on the cycle it opens
    env.update(spot=110.5, q=_q(9.0, 9.4))
    assert vn.poll(store, None)['closed'] == 1
    ex = _row()['arms'][vn.ARM]['exit']
    assert (ex['reason'], ex['value']) == ('tp', 9.0)
    assert vn.poll(store, None)['live'] == 0


def test_the_veto_changing_after_opening_leaves_the_row_alone(env):
    t = _veto()
    vn.poll(_Store([t]), None)
    t['vet']['state'] = 'allowed'
    env.update(spot=110.5, q=_q(9.0, 9.4))
    vn.poll(_Store([t]), None)
    assert _row()['arms'][vn.ARM]['status'] == 'exited'


def test_a_clean_veto_takes_over_a_setup_from_a_late_row(env):
    """A late row is not scored. On deploy day that morning's vetoes all open late,
    and without this their setups could never be scored at all."""
    late = _veto(1, decided='2026-11-06T09:30:00')
    vn.poll(_Store([late]), None)
    assert _row('1')['opened_late'] is True
    vn.poll(_Store([late, _veto(2)]), None)
    st = vn.load_state()
    assert st['setups']['ACME|CE|110.00'] == '2' and st['shadows']['2']['opened_late'] is False


def test_a_late_veto_does_not_take_over_from_a_late_row(env):
    vn.poll(_Store([_veto(1, decided='2026-11-06T09:30:00')]), None)
    vn.poll(_Store([_veto(1, decided='2026-11-06T09:30:00'),
                    _veto(2, decided='2026-11-06T09:31:00')]), None)
    assert vn.load_state()['shadows']['1']['repeat_ids'] == ['2']


def test_a_target_books_at_the_bid_and_fees_can_be_costed(env):
    vn.poll(_Store([_veto()]), None)
    sh = _row()
    _advance(sh, 110.5, _q(9.0, 9.4), datetime(2026, 11, 6, 12, 0))
    a = sh['arms'][vn.ARM]
    assert (a['exit']['reason'], a['exit']['value']) == ('tp', 9.0)
    assert ss.arm_fees(ss.ARMS[vn.ARM], sh, a) is not None


# -- failure ----------------------------------------------------------------------

def test_one_bad_candidate_does_not_stop_the_others(env):
    bad = _veto(2, st=111.0)
    bad['vet']['context']['swing_tp'] = 'oops'
    good = _veto(1)
    vn.poll(_Store([good]), None)
    env.update(spot=110.5, q=_q(9.0, 9.4))
    assert vn.poll(_Store([good, bad]), None)['closed'] == 1
    assert vn.load_state()['unopened']['2']['reason'] == 'bad_record'


def test_one_unreadable_row_does_not_stop_the_others(env, tmp_path):
    good = _veto(1)
    vn.poll(_Store([good]), None)
    st = vn.load_state()
    st['shadows']['99'] = {'stock': 'BAD'}
    ss._save(st, vn.STATE_FILE)
    env.update(spot=110.5, q=_q(9.0, 9.4))
    assert vn.poll(_Store([good]), None)['closed'] == 1


def test_an_open_that_raises_is_reported(env):
    t = _veto()
    t['vet']['context']['bcs']['target_spot'] = 'abc'
    vn.poll(_Store([t]), None)
    assert vn.load_state()['unopened']['1']['reason'] == 'error'


def test_a_cooldown_sends_nothing_to_the_broker(env, monkeypatch):
    """An LTP or quote sent during a 429 cooldown extends it, and this engine has
    been blinded that way before."""
    def boom(*a):
        raise AssertionError('broker called during cooldown')
    monkeypatch.setattr(vn.strikes_mod, '_cooldown_error', lambda: object())
    monkeypatch.setattr(vn, '_spots', boom)
    monkeypatch.setattr(vn.strikes_mod, 'prefetch_quotes', boom)
    monkeypatch.setattr(vn.strikes_mod, '_quote_option', boom)
    assert vn.poll(_Store([_veto()]), None) == {'cooldown': True}


def test_one_cycle_asks_the_broker_once_for_each_kind(env, monkeypatch):
    calls = {'spots': 0, 'prefetch': 0}

    def spots(k, n):
        calls['spots'] += 1
        return {x: 100.0 for x in n}

    def prefetch(k, s):
        calls['prefetch'] += 1
        return 1
    monkeypatch.setattr(vn, '_spots', spots)
    monkeypatch.setattr(vn.strikes_mod, 'prefetch_quotes', prefetch)
    vn.poll(_Store([_veto(i, st=110.0 + i) for i in range(1, 6)]), None)
    assert calls == {'spots': 1, 'prefetch': 1}


def test_an_idle_cycle_with_nothing_open_asks_the_broker_nothing(env, monkeypatch):
    def boom(*a):
        raise AssertionError('broker called with nothing to do')
    monkeypatch.setattr(vn, '_spots', boom)
    monkeypatch.setattr(vn.strikes_mod, 'prefetch_quotes', boom)
    assert vn.poll(_Store([]), None)['live'] == 0


def test_the_start_day_survives_an_idle_cycle(env, monkeypatch):
    """Otherwise a veto after the first day's last cycle leaves no trace."""
    vn.poll(_Store([]), None)
    monkeypatch.setattr(vet_mod, '_now', lambda: datetime(2026, 11, 7, 9, 20))
    vn.poll(_Store([_veto(decided='2026-11-06T15:29:50')]), None)
    assert vn.load_state()['unopened']['1']['reason'] == 'missed_session'


def test_poll_never_raises(env, monkeypatch):
    def boom(*a):
        raise RuntimeError('quote down')
    monkeypatch.setattr(vn.strikes_mod, '_quote_option', boom)
    assert isinstance(vn.poll(_Store([_veto()]), None), dict)
    monkeypatch.setattr(vn, 'load_state', boom)
    assert vn.poll(_Store([]), None) == {}


def test_a_spot_outage_defers_rather_than_opens_blind(env, monkeypatch):
    def boom(*a):
        raise RuntimeError('ltp down')
    monkeypatch.setattr(vn, '_spots', boom)
    vn.poll(_Store([_veto()]), None)
    assert vn.load_state()['unopened']['1']['reason'] == 'no_spot'


def test_the_switch_stops_every_quote(env, monkeypatch):
    def boom(*a):
        raise AssertionError('quoted while disabled')
    monkeypatch.setattr(vn.cfg, 'STRUCTURE_SHADOW_ENABLED', False)
    monkeypatch.setattr(vn, '_spots', boom)
    assert vn.poll(_Store([_veto()]), None) == {'disabled': True}


def test_a_corrupt_book_moves_aside_without_stopping_the_cycle(env, tmp_path):
    (tmp_path / 'veto.json').write_text('{not json')
    assert vn.poll(_Store([_veto()]), None)['opened'] == 1
    assert (tmp_path / 'veto.json.unreadable').exists()


# -- the reader ---------------------------------------------------------------------

def _closed_state(tid, stock, entry, exit_, opened_late=False, since='2026-11-06 11:00:00'):
    return {'stock': stock, 'direction': 'CE', 'quantity': 100, 'since': since,
            'opened_late': opened_late, 'long_ask_entry': entry, 'repeat_ids': [],
            'context': {'dte': 18},
            'arms': {vn.ARM: {'status': 'exited', 'entry_value': entry, 'fills': 2,
                              'exit': {'at': '2026-11-07 11:00:00', 'reason': 'tp', 'value': exit_,
                                       'pnl_pct': 100.0 * (exit_ - entry) / entry,
                                       'legs': {'long': {'bid': exit_, 'ask': exit_ + 0.1}, 'short': None}}}}}


def test_the_read_compares_the_same_dates_and_leaves_partials_out():
    st = {'shadows': {'1': _closed_state('1', 'A', 5.0, 10.0),
                      '2': _closed_state('2', 'B', 5.0, 2.0, opened_late=True)},
          'setups': {'A|CE|110.00': '1'}, 'unopened': {'9': {'reason': 'no_quote'}}}
    early = {'start': datetime(2026, 11, 1), 'partial': False, 'resolved': True, 'priced': True,
             'net': 900.0, 'capital': 1000.0, 'dir': 'CE', 'pct': 90.0}
    same = dict(early, start=datetime(2026, 11, 6, 11), net=-500.0)
    allowed_flip = {'stock': 'A', 'direction': 'CE', 'st_value': 110.0, 'status': 'exited',
                    'vet': {'state': 'allowed'}}
    r = golive.veto_naked_read([early, same], NOW, [allowed_flip], state=st)
    assert (r['rows'], r['partial'], r['flipped']) == (2, 1, 1)
    assert r['vetoed']['n'] == 1 and r['vetoed']['mean'] > 0
    assert r['allowed']['n'] == 1 and r['allowed']['mean'] == -50.0   # `early` excluded
    assert r['unopened'] == {'no_quote': 1}


def _report_with(monkeypatch, st):
    monkeypatch.setattr(golive.veto_naked, 'load_state', lambda: st)
    monkeypatch.setattr(ss, '_load', lambda path=None: {'schema': ss.SCHEMA, 'shadows': {}})
    monkeypatch.setattr(golive.replay, 'universe', lambda: [])

    class Store:
        def load_trades(self):
            return []
    out = golive.report(Store(), now=NOW)
    return out[out.index('8b VETO_NAKED'):out.index('9 TIME EXIT')]


@pytest.mark.parametrize('bad', [
    {'shadows': {'1': {'stock': 'NOARMS', 'since': '2026-11-06 11:00:00'}}},
    {'shadows': {'1': dict(_closed_state('1', 'A', 5.0, 10.0), quantity='x')}},
])
def test_a_bad_row_loses_section_8b_but_not_the_pack(monkeypatch, bad):
    assert 'UNAVAILABLE' in _report_with(monkeypatch, bad)


def test_a_reason_missing_from_a_note_does_not_break_the_sort():
    st = {'shadows': {}, 'unopened': {'1': {}, '2': {'reason': 'no_quote'}}}
    assert golive.veto_naked_read([], NOW, [], state=st)['unopened'] == {'None': 1, 'no_quote': 1}


def _allowed(day, direction='CE', net=100.0, resolved=True, priced=True):
    return {'start': day, 'dir': direction, 'partial': False, 'resolved': resolved,
            'priced': priced, 'net': net, 'capital': 1000.0, 'pct': 10.0}


def test_allowed_rows_after_the_last_veto_are_not_compared():
    st = {'shadows': {'1': _closed_state('1', 'A', 5.0, 10.0)}, 'setups': {}}
    later = _allowed(datetime(2026, 11, 20))
    r = golive.veto_naked_read([later], NOW, [], state=st)
    assert r['allowed']['n'] == 0


def test_the_matched_rows_keep_only_weeks_and_sides_both_traded():
    st = {'shadows': {'1': _closed_state('1', 'A', 5.0, 10.0, since='2026-11-02 10:00:00'),
                      '2': dict(_closed_state('2', 'B', 5.0, 2.0, since='2026-11-20 10:00:00'),
                                direction='PE')},
          'setups': {}}
    same = _allowed(datetime(2026, 11, 3))
    other_side = _allowed(datetime(2026, 11, 4), direction='PE')
    r = golive.veto_naked_read([same, other_side], NOW, [], state=st)
    assert r['vetoed']['n'] == 2 and r['allowed']['n'] == 2
    assert r['vetoed_matched']['n'] == 1 and r['allowed_matched']['n'] == 1


def test_a_dead_book_cannot_flatter_either_side():
    """Scored as the test scores it: an unpriced exit is -100%, never left out."""
    un = dict(_closed_state('1', 'A', 5.0, 10.0))
    un['arms'] = {vn.ARM: {'status': 'exited', 'entry_value': 5.0, 'fills': 2,
                           'exit': {'at': '2026-11-07 11:00:00', 'reason': 'time', 'value': None,
                                    'pnl_pct': None, 'legs': {}}}}
    r = golive.veto_naked_read([], NOW, [], state={'shadows': {'1': un}, 'setups': {}})
    assert r['vetoed']['unpriced'] == 1 and r['vetoed']['mean'] == golive.UNPRICED_NET_PCT


def test_the_pack_prints_rows(monkeypatch):
    """This branch has no rows on the box until the first veto, so a format bug
    here would first appear as a crashed go-live pack."""
    st = {'shadows': {'1': _closed_state('1', 'A', 5.0, 10.0),
                      '3': dict(_closed_state('3', 'C', 5.0, 2.0),
                                arms={vn.ARM: {'status': 'open', 'entry_value': 5.0, 'exit': None}})},
          'setups': {}, 'unopened': {'9': {'reason': 'no_quote'}}}
    section = _report_with(monkeypatch, st)
    assert 'also entered' in section and 'same wk+side' in section and 'CENSORED' in section
    assert 'UNAVAILABLE' not in section
    assert 'never opened: no_quote 1' in section


def test_an_allowed_signal_that_never_entered_is_not_a_flip():
    st = {'shadows': {'1': _closed_state('1', 'A', 5.0, 10.0)}, 'setups': {'A|CE|110.00': '1'}}
    t = {'stock': 'A', 'direction': 'CE', 'st_value': 110.0, 'status': 'cancelled',
         'vet': {'state': 'allowed'}}
    assert golive.veto_naked_read([], NOW, [t], state=st)['flipped'] == 0


def test_the_read_is_empty_before_any_veto():
    r = golive.veto_naked_read([], NOW, [], state={'shadows': {}})
    assert (r['rows'], r['vetoed'], r['since']) == (0, None, None)
