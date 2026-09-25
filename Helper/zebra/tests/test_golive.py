"""The go-live measurements: IV and VIX at entry, the stop ladder, and the
decision pack that reads them. Mostly about what must NOT happen -- a
measurement that raises into a cycle, a ladder that books a print the live
stop could not act on, and a report that flatters itself.
"""
from datetime import date, datetime

import pytest

from zebra import golive
from zebra import ivcalc
from zebra import structure_shadow as ss


def _trade(**kw):
    t = {'id': 1, 'status': 'entered', 'cohort': '2026-08-14', 'stock': 'ACME',
         'direction': 'CE', 'long_symbol': 'ACME26SEP100CE',
         'short_symbol': 'ACME26SEP104CE', 'tp_spot': 104.0,
         'expiry': '2026-09-24', 'quantity': 100, 'entry_spot': 100.0,
         'entry_date': '2026-09-01', 'entry_time': '09:30:00',
         'long_ask_entry': 4.0, 'long_mid_entry': 3.9, 'short_bid_entry': 2.0,
         'debit': 2.0, 'width': 4.0, 'long_strike': 100.0, 'short_strike': 104.0}
    t.update(kw)
    return t


def _q(bid, ask, reliable=True):
    return {'bid': bid, 'ask': ask, 'reliable': reliable, 'unreliable_reason': ''}


MID = datetime(2026, 9, 10, 12, 30)
TODAY = date(2026, 9, 10)


# -- implied volatility ------------------------------------------------------

@pytest.mark.parametrize('kind,S,K,days,sigma', [
    ('CE', 100, 100, 30, 0.30), ('PE', 560, 560, 35, 0.35),
    ('CE', 1383.5, 1380, 39, 0.22), ('PE', 327.35, 325, 35, 0.60)])
def test_implied_vol_round_trips_the_price(kind, S, K, days, sigma):
    p = ivcalc.bs_price(kind, S, K, days / 365.0, 0.065, sigma)
    assert ivcalc.implied_vol(kind, p, S, K, days / 365.0, 0.065) == pytest.approx(sigma, abs=1e-3)


@pytest.mark.parametrize('kind,price,S,K,T', [
    ('CE', 0.0, 100, 100, 0.1),      # no price
    ('CE', 150, 100, 100, 0.1),      # beyond anything a volatility produces
    ('CE', 9.0, 110, 100, 0.1),      # below intrinsic: no time value to explain
    ('XX', 4.0, 100, 100, 0.1),      # unknown type
    ('CE', 4.0, 100, 100, 0.0),      # expired
    ('CE', 'x', 100, 100, 0.1)])     # garbage
def test_implied_vol_refuses_rather_than_guesses(kind, price, S, K, T):
    assert ivcalc.implied_vol(kind, price, S, K, T, 0.065) is None


# -- entry context -----------------------------------------------------------

def test_the_context_carries_what_the_go_live_call_needs():
    c = ss.entry_context(_trade(), 14.2, None)
    assert c['vix'] == 14.2 and c['capital_naked'] == 400.0 and c['capital_spread'] == 200.0
    assert c['premium_pct_spot'] == 4.0 and c['dte'] == 23 and c['iv_basis'] == 'mid'
    assert 5 < c['iv_long'] < 200


def test_a_malformed_trade_cannot_break_the_context():
    c = ss.entry_context(_trade(expiry='garbage', long_strike=None), None, 'x')
    assert 'error' in c or c.get('iv_long') is None


class _Kite:
    def __init__(self, vix=14.5, boom=None):
        self.vix, self.boom, self.calls = vix, boom, 0

    def ltp(self, syms):
        self.calls += 1
        if self.boom:
            raise self.boom
        return {ss.VIX_SYMBOL: {'last_price': self.vix}}


def test_vix_is_read_live_and_never_raises(monkeypatch):
    monkeypatch.setattr(ss.strikes_mod, '_cooldown_error', lambda: None)
    assert ss.fetch_vix(_Kite(14.5)) == (14.5, None)
    assert ss.fetch_vix(None) == (None, 'no_broker')
    v, why = ss.fetch_vix(_Kite(boom=RuntimeError('down')))
    assert v is None and why.startswith('error')


def test_vix_stands_down_in_the_quote_cooldown(monkeypatch):
    """It shares the broker's quote budget with both engines; a read inside
    the 429 cooldown would extend the very window that blinds the monitor."""
    k = _Kite()
    monkeypatch.setattr(ss.strikes_mod, '_cooldown_error', lambda: object())
    assert ss.fetch_vix(k) == (None, 'quote_cooldown') and k.calls == 0


def test_vix_is_read_once_per_cycle_not_once_per_shadow(monkeypatch):
    monkeypatch.setattr(ss.strikes_mod, '_cooldown_error', lambda: None)
    monkeypatch.setattr(ss, '_open_wide', lambda t, kite, ts='': (None, 'test'))
    k = _Kite()
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    ss.open_shadows(state, [_trade(id=1), _trade(id=2)], '2026-09-01 09:35:00', kite=k)
    assert k.calls == 1
    assert all(sh['context']['vix'] == 14.5 for sh in state['shadows'].values())


# -- the stop ladder ---------------------------------------------------------

def _shadow():
    # Opened within MAX_OPEN_LAG_SEC of the 09:30 entry, so NOT partial.
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    ss.open_shadows(state, [_trade()], '2026-09-01 09:35:00')
    assert state['shadows']['1']['opened_late'] is False
    return state['shadows']['1']


def test_only_the_no_stop_arm_carries_a_ladder():
    sh = _shadow()
    assert sh['arms'][ss.LADDER_ARM]['ladder'] == {}
    assert all('ladder' not in a for k, a in sh['arms'].items() if k != ss.LADDER_ARM)


def test_the_ladder_keeps_the_FIRST_breach_of_each_level():
    sh = _shadow()                                    # naked entry 4.0
    ss.poll_one(sh, 99.0, _q(2.7, 2.8), _q(1, 1.1), MID, TODAY)           # -32.5%
    ss.poll_one(sh, 98.0, _q(2.3, 2.4), _q(1, 1.1),
                datetime(2026, 9, 10, 12, 35), TODAY)                     # -42.5%
    ss.poll_one(sh, 97.0, _q(1.0, 1.1), _q(1, 1.1),
                datetime(2026, 9, 10, 12, 40), TODAY)                     # -75%
    lad = sh['arms'][ss.LADDER_ARM]['ladder']
    assert lad['l30']['value'] == 2.7 and lad['l40']['value'] == 2.3
    assert lad['l50']['value'] == lad['l70']['value'] == 1.0
    assert sh['arms'][ss.LADDER_ARM]['status'] == 'open'   # it has no stop


def test_the_ladder_obeys_the_live_stops_opening_blindness():
    sh = _shadow()
    ss.poll_one(sh, 99.0, _q(1.0, 1.1), _q(1, 1.1), datetime(2026, 9, 10, 9, 20), TODAY)
    assert sh['arms'][ss.LADDER_ARM]['ladder'] == {}


def test_a_breach_on_the_first_armed_poll_of_a_session_is_a_gap():
    sh = _shadow()
    ss.poll_one(sh, 100.0, _q(3.9, 4.0), _q(1, 1.1), datetime(2026, 9, 9, 14, 0), date(2026, 9, 9))
    ss.poll_one(sh, 95.0, _q(1.5, 1.6), _q(1, 1.1), datetime(2026, 9, 10, 9, 31), TODAY)
    ss.poll_one(sh, 94.0, _q(1.1, 1.2), _q(1, 1.1), datetime(2026, 9, 10, 9, 36), TODAY)
    lad = sh['arms'][ss.LADDER_ARM]['ladder']
    assert lad['l50']['gap'] is True           # overnight: first armed poll
    assert lad['l70']['gap'] is False          # reached during the session


def test_a_malformed_ladder_config_falls_back_loudly(monkeypatch, caplog):
    from zebra import config as cfg
    monkeypatch.setitem(cfg._runtime, 'shadow_stop_ladder', [0.3, 1.5])
    assert cfg._stop_ladder() == tuple(cfg._DEFAULTS['shadow_stop_ladder'])
    assert 'shadow_stop_ladder' in caplog.text


# -- the decision pack -------------------------------------------------------

def _row(start, end, capital, net=None, pct=None, resolved=True):
    return {'start': datetime.strptime(start, '%Y-%m-%d %H:%M'),
            'end': datetime.strptime(end, '%Y-%m-%d %H:%M'), 'capital': capital,
            'resolved': resolved, 'priced': pct is not None, 'pct': pct,
            'net': net, 'gross': net, 'fees': 0.0, 'stress': 10.0, 'ctx': {}}


def test_peak_is_capital_tied_up_AT_ONCE_not_the_sum_of_premiums():
    """The owner's correction: summing premiums charged the same rupee
    several times over and understated return ~4x."""
    rows = [_row('2026-09-01 10:00', '2026-09-03 10:00', 100),
            _row('2026-09-02 10:00', '2026-09-04 10:00', 200),
            _row('2026-09-05 10:00', '2026-09-06 10:00', 250)]
    c = golive.peak_concurrent(rows)
    assert c['peak'] == 300 and c['max_open'] == 2


def test_an_exit_and_an_entry_at_the_same_instant_do_not_stack():
    rows = [_row('2026-09-01 10:00', '2026-09-02 10:00', 100),
            _row('2026-09-02 10:00', '2026-09-03 10:00', 100)]
    assert golive.peak_concurrent(rows)['peak'] == 100


def test_a_position_cap_skips_signals_while_the_book_is_full():
    rows = [_row('2026-09-01 10:00', '2026-09-05 10:00', 100, 10, 10),
            _row('2026-09-02 10:00', '2026-09-03 10:00', 100, -5, -5),
            _row('2026-09-04 10:00', '2026-09-06 10:00', 100, 7, 7)]
    s = golive.cap_simulation(rows, 1)
    assert s['taken'] == 1 and s['skipped'] == 2 and s['net'] == 10


def test_an_uncosted_row_is_counted_not_added_in_free():
    rows = [_row('2026-09-01 10:00', '2026-09-02 10:00', 100, 50, 50),
            _row('2026-09-01 10:00', '2026-09-02 10:00', 100, None, 30)]
    s = golive.summary(rows)
    assert s['n'] == 2 and s['uncosted'] == 1 and s['net'] == 50


def test_every_stop_level_is_scored_on_the_same_path():
    sh = _shadow()                                    # naked entry 4.0, qty 100
    a = sh['arms'][ss.LADDER_ARM]
    a['ladder'] = {'l30': {'at': 'x', 'value': 2.8, 'gap': False},
                   'l40': {'at': 'x', 'value': 2.4, 'gap': True}}
    a['status'] = 'exited'
    a['exit'] = {'value': 6.0, 'pnl_pct': 50.0, 'reason': 'tp', 'at': '2026-09-12 10:00:00'}
    res = golive.ladder_outcomes({'shadows': {'1': sh}}, MID)
    assert res['l30']['stopped'] == 1 and res['l30']['pcts'] == [pytest.approx(-30.0)]
    assert res['l40']['gaps'] == 1
    assert res['l50']['stopped'] == 0 and res['l50']['pcts'] == [pytest.approx(50.0)]
    assert res['none']['pcts'] == [pytest.approx(50.0)]
    assert res['none']['net'] < 200.0            # 2.0 x 100 gross, less real fees


def test_a_partial_shadow_is_kept_out_of_the_ladder():
    sh = _shadow()
    sh['opened_late'] = True
    a = sh['arms'][ss.LADDER_ARM]
    a['status'], a['exit'] = 'exited', {'value': 6.0, 'pnl_pct': 50.0, 'at': 'x'}
    assert golive.ladder_outcomes({'shadows': {'1': sh}}, MID)['none']['n'] == 0


def test_the_report_leads_with_the_100_trade_test(monkeypatch):
    sh = _shadow()
    monkeypatch.setattr(ss, '_load', lambda: {'schema': ss.SCHEMA, 'shadows': {'1': sh}})
    monkeypatch.setattr(golive.replay, 'universe', lambda: [])

    class Store:
        def load_trades(self):
            return [_trade()]
    out = golive.report(Store(), now=MID)
    assert 'THE 100-TRADE TEST' in out and 'VERDICT: IN PROGRESS' in out
    assert '1 STRUCTURE' in out and '6 BANDS' in out
    assert '7 SELECTION' in out and '8 VETTING' in out
    assert out.index('THE 100-TRADE TEST') < out.index('1 STRUCTURE')


def test_the_ladders_50_level_books_what_the_live_50_stop_books():
    """naked_long IS the -50% stop. If the ladder's l50 disagreed with it on
    the same prints, every other level on the ladder would be suspect too."""
    sh = _shadow()
    ticks = [(12, 30, 3.5), (12, 35, 2.4), (12, 40, 1.9), (12, 45, 1.2)]
    for h, m, bid in ticks:
        ss.poll_one(sh, 99.0, _q(bid, bid + 0.1), _q(1, 1.1),
                    datetime(2026, 9, 10, h, m), TODAY)
    stop = sh['arms']['naked_long']['exit']
    assert stop['reason'] == 'stop'
    assert sh['arms'][ss.LADDER_ARM]['ladder']['l50']['value'] == stop['value'] == 1.9


# -- review fixes 2026-09-24 -------------------------------------------------

def test_a_mid_session_first_poll_is_not_an_overnight_gap():
    """A shadow opened at 11:00 whose FIRST armed poll already sits past a
    level was watched from entry -- nothing overnight happened to it."""
    sh = _shadow()
    ss.poll_one(sh, 95.0, _q(1.5, 1.6), _q(1, 1.1), datetime(2026, 9, 10, 11, 0), TODAY)
    assert sh['arms'][ss.LADDER_ARM]['ladder']['l50']['gap'] is False


def test_capital_counts_partial_positions_but_outcomes_do_not():
    """A partial shadow's parent tied up real money: leaving it out of the
    capital plan understates what the plan needs."""
    rows = [dict(_row('2026-09-01 10:00', '2026-09-03 10:00', 100, 50, 50), partial=True),
            dict(_row('2026-09-02 10:00', '2026-09-04 10:00', 200, -20, -20), partial=False)]
    s = golive.cap_simulation(rows, 10)
    assert s['peak'] == 300              # both occupy capital
    assert s['n'] == 1 and s['net'] == -20   # only the watched one is scored
    assert golive.cap_simulation(rows, 1)['taken'] == 1   # the partial one held the slot


def test_the_ladder_says_what_it_could_not_score():
    sh_open = _shadow()
    sh_unpriced = _shadow()
    a = sh_unpriced['arms'][ss.LADDER_ARM]
    a['status'], a['exit'] = 'exited', {'value': None, 'pnl_pct': None, 'at': 'x'}
    res = golive.ladder_outcomes({'shadows': {'1': sh_open, '2': sh_unpriced}}, MID)
    assert res['_skipped'] == {'open': 1, 'unpriced': 1, 'partial': 0}


def test_context_capture_is_logged_with_the_reason_vix_is_missing(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(ss.strikes_mod, '_cooldown_error', lambda: object())
    monkeypatch.setattr(ss, '_open_wide', lambda t, kite, ts='': (None, 'test'))
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    with caplog.at_level(logging.INFO, logger=ss.logger.name):
        ss.open_shadows(state, [_trade()], '2026-09-01 09:35:00', kite=_Kite())
    assert 'SHADOW context #1 ACME: VIX MISSING (quote_cooldown)' in caplog.text


def test_backfill_does_not_log_a_context_it_cannot_have(caplog):
    import logging
    state = {'schema': ss.SCHEMA, 'shadows': {}}
    with caplog.at_level(logging.INFO, logger=ss.logger.name):
        ss.open_shadows(state, [_trade()], '2026-09-01 09:35:00')
    assert 'SHADOW context' not in caplog.text


def test_bad_cli_numbers_are_refused_cleanly(capsys):
    from types import SimpleNamespace
    from zebra.__main__ import cmd_golive
    assert cmd_golive(SimpleNamespace(caps='6,x', cuts=None)) == 2
    assert 'whole numbers' in capsys.readouterr().out


# -- the 100-trade test ------------------------------------------------------

def test_the_test_counts_only_fully_watched_closed_and_costed_trades():
    rows = [dict(_row('2026-09-01 10:00', '2026-09-02 10:00', 1000, 100, 10), partial=False),
            dict(_row('2026-09-01 10:00', '2026-09-02 10:00', 1000, 100, 10), partial=True),
            dict(_row('2026-09-01 10:00', '2026-09-02 10:00', 1000, None, 10), partial=False),
            dict(_row('2026-09-01 10:00', '2026-09-02 10:00', 1000, None, None), partial=False)]
    t = golive.test_rows(rows)
    assert len(t) == 1 and t[0]['net_pct'] == pytest.approx(10.0)


def test_no_verdict_before_100_however_good_the_run():
    """Stopping on a good early run is how a lucky patch becomes an 'edge'."""
    v = golive.verdict([80.0] * 99, -10.0)
    assert v['state'] == 'IN PROGRESS'


def _hundred(mean, n=100):
    """n nets averaging `mean`, spread so the top 3 do not carry it."""
    return [mean + (20.0 if k % 2 else -20.0) for k in range(n)]


def test_pass_needs_all_three_conditions():
    assert golive.verdict(_hundred(15.0), 0.0)['state'] == 'PASS'
    assert golive.verdict(_hundred(15.0), 20.0)['state'] != 'PASS'     # does not beat the replay
    carried = [600.0, 600.0, 600.0] + [-5.0] * 97                      # mean +13.2%, all of it 3 trades
    assert golive.verdict(carried, 0.0)['state'] != 'PASS'


def test_below_the_fail_line_fails_and_between_extends():
    assert golive.verdict(_hundred(3.0), 0.0)['state'] == 'FAIL'
    assert golive.verdict(_hundred(8.0), 0.0)['state'] == 'INCONCLUSIVE'


def test_at_the_extension_the_bar_is_lower_and_there_is_no_third_chance():
    assert golive.verdict(_hundred(11.0, 150), 0.0)['state'] == 'PASS'
    assert golive.verdict(_hundred(8.0, 150), 0.0)['state'] == 'FAIL'


def test_selection_compares_against_the_replay_of_the_same_dates():
    tests = golive.test_rows([
        dict(_row('2026-09-10 10:00', '2026-09-12 10:00', 1000, 200, 20), partial=False),
        dict(_row('2026-09-15 10:00', '2026-09-16 10:00', 1000, -500, -50), partial=False)])
    replayed = [{'net_pct': -10.0}, {'net_pct': 30.0}, {'reason': 'open'}]
    s = golive.selection(tests, replayed)
    assert (s['since'], s['until']) == ('2026-09-10', '2026-09-15')
    assert s['live'] == pytest.approx(-15.0) and s['replay'] == pytest.approx(10.0)
    assert s['n'] == 2 and s['open'] == 1


def _vet(i, state, stock='ACME', st=100.0, day='2026-09-10'):
    return {'id': i, 'stock': stock, 'direction': 'CE', 'st_value': st,
            'triggered_at': day + 'T10:00:00', 'vet': {'state': state}}


def test_a_re_vetoed_setup_is_one_row_not_many():
    """ADANIGREEN was vetoed six times on one ST line: one outcome, not six."""
    trades = [_vet(1, 'vetoed'), _vet(2, 'vetoed'), _vet(3, 'vetoed'),
              _vet(4, 'vetoed', stock='OTHER'), _vet(5, 'allowed', stock='THIRD')]
    v = golive.vetting(trades, replay_fn=lambda t: {'net_pct': 40.0 if t['stock'] == 'ACME' else -50.0})
    g = v['groups']['vetoed']
    assert g['n'] == 2 and g['repeats'] == 2 and g['nets'] == [40.0, -50.0]
    assert v['allowed_mean'] == -50.0


def test_an_unpriceable_record_does_not_hide_a_later_repeat():
    trades = [_vet(1, 'vetoed'), _vet(2, 'vetoed', day='2026-09-11')]
    v = golive.vetting(trades, replay_fn=lambda t: None if t['id'] == 1 else {'net_pct': 5.0})
    g = v['groups']['vetoed']
    assert g['n'] == 1 and g['unpriced'] == 1 and g['nets'] == [5.0]


def test_signals_before_the_test_window_are_left_out():
    v = golive.vetting([_vet(1, 'vetoed', day='2026-08-01')], replay_fn=lambda t: {'net_pct': 1.0})
    assert v['groups'] == {}


def test_the_vetting_review_trigger_needs_both_the_sample_and_the_gap():
    few = [_vet(i, 'vetoed', stock='V%d' % i) for i in range(10)] + [_vet(99, 'allowed', stock='A')]
    fn = lambda t: {'net_pct': 30.0 if t['vet']['state'] == 'vetoed' else 0.0}
    assert golive.vetting(few, fn)['review'] is False                 # gap, but too few
    many = [_vet(i, 'vetoed', stock='V%d' % i) for i in range(golive.VET_REVIEW_MIN_N)] + few[-1:]
    assert golive.vetting(many, fn)['review'] is True


# -- the time-exit hypothesis ------------------------------------------------

def test_the_arm_records_the_LAST_priced_poll_of_each_session():
    sh = _shadow()
    for h, bid in ((11, 4.4), (15, 4.6)):
        ss.poll_one(sh, 100.5, _q(bid, bid + 0.1), _q(1, 1.1), datetime(2026, 9, 10, h, 0), TODAY)
    ss.poll_one(sh, 100.5, _q(9.9, 10.0, reliable=False), _q(1, 1.1),
                datetime(2026, 9, 10, 15, 25), TODAY)            # unusable book: no mark
    marks = sh['arms'][ss.MARKS_ARM]['marks']
    assert marks == {'2026-09-10': {'at': '2026-09-10 15:00:00', 'value': 4.6}}
    assert all('marks' not in a for k, a in sh['arms'].items() if k != ss.MARKS_ARM)


@pytest.fixture
def weekdays(monkeypatch):
    from common import nse_holidays
    monkeypatch.setattr(nse_holidays, 'is_session', lambda d: d.weekday() < 5)


def _te_row(start, end, net_pct, reason='stop'):
    r = _row(start, end, 400.0, net_pct * 4, net_pct)
    return dict(r, id='1', net_pct=net_pct, reason=reason, partial=False)


def test_session_counting_skips_the_weekend(weekdays):
    assert golive._session_after(date(2026, 9, 11), 1) == date(2026, 9, 14)   # Fri -> Mon


def test_an_exit_before_the_cutoff_stands_unchanged(weekdays):
    sh = _shadow()
    r = _te_row('2026-09-01 09:35', '2026-09-02 11:00', 50.0, 'tp')
    assert golive.time_exit_outcome(r, sh, 3) == {'net_pct': 50.0, 'cut': False}


def test_a_trade_still_open_at_the_cutoff_books_that_sessions_close_mark(weekdays):
    sh = _shadow()                                    # naked entry 4.0 x 100
    sh['arms'][ss.MARKS_ARM]['marks'] = {'2026-09-04': {'at': 'x', 'value': 3.0}}
    r = _te_row('2026-09-01 09:35', '2026-09-09 10:00', -52.0)
    o = golive.time_exit_outcome(r, sh, 3)            # Tue + 3 sessions = Fri 09-04
    assert o['cut'] is True
    # -25% gross, less the real (flat, so large on a Rs 400 test lot) fees
    assert -50.0 < o['net_pct'] < -25.0


def test_the_value_paths_fill_in_for_a_shadow_without_marks(weekdays):
    sh = _shadow()
    r = _te_row('2026-09-01 09:35', '2026-09-09 10:00', -52.0)
    o = golive.time_exit_outcome(r, sh, 3, {'2026-09-04': 5.0})     # +25% gross
    assert o['cut'] is True and 0.0 < o['net_pct'] < 25.0


def test_no_mark_anywhere_is_unpriced_never_guessed(weekdays):
    sh = _shadow()
    r = _te_row('2026-09-01 09:35', '2026-09-09 10:00', -52.0)
    assert golive.time_exit_outcome(r, sh, 3, {}) == {'unpriced': True}
    te = golive.time_exit([r], {'shadows': {'1': sh}}, 3, {})
    assert te['n'] == 0 and te['unpriced'] == 1


def test_the_time_exit_verdict_waits_for_100_and_needs_two_standard_errors(weekdays, monkeypatch):
    rows = [dict(_te_row('2026-09-01 09:35', '2026-09-09 10:00', -52.0), id=str(k))
            for k in range(golive.TEST_N)]
    state = {'shadows': {str(k): _shadow() for k in range(golive.TEST_N)}}
    few = golive.time_exit(rows[:10], state, 3, {str(k): {'2026-09-04': 3.0} for k in range(100)})
    assert few['state'] == 'IN PROGRESS' and few['gain'] > 0
    # every stopped trade (-52%) cut at a 3.0 mark instead: a consistent gain
    clean = golive.time_exit(rows, state, 3, {str(k): {'2026-09-04': 3.0} for k in range(100)})
    assert clean['state'] == 'SUPPORTED' and clean['gain'] > 0
    # against a -20% baseline, marks alternating 4.0 / 1.0 lose on average
    for k in range(100):
        rows[k]['net_pct'] = -20.0
    pm = {str(k): {'2026-09-04': (4.0 if k % 2 else 1.0)} for k in range(100)}
    noisy = golive.time_exit(rows, state, 3, pm)
    assert noisy['gain'] < 0 and noisy['state'] == 'NOT SUPPORTED'


# -- the approach hypothesis -------------------------------------------------

def _daily(closes, start='2026-08-24'):
    from datetime import timedelta
    d, out = date.fromisoformat(start), []
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append({'date': d.isoformat(), 'open': c, 'high': c, 'low': c, 'close': c})
        d += timedelta(days=1)
    return out


def test_the_approach_is_the_move_toward_the_line_before_entry():
    from zebra import replay
    d = _daily([90, 91, 92, 95, 99])                  # entry session = the 5th
    # 3 sessions before the entry session closed at 91: 99 / 91 - 1
    assert replay.approach_move(d, d[4]['date'], 99.0, 'CE') == pytest.approx(100 * (99 / 91 - 1))
    assert replay.approach_move(d, d[4]['date'], 99.0, 'PE') == pytest.approx(-100 * (99 / 91 - 1))
    assert replay.approach_move(d, d[1]['date'], 91.0, 'CE') is None    # not 3 sessions of history
    assert replay.approach_move(d, '2030-01-01', 91.0, 'CE') is None    # entry day not in candles


def test_test_trades_split_fast_and_rest_and_missing_candles_are_unknown():
    d = _daily([90, 91, 92, 95, 99, 99])
    rows = [dict(_te_row('%s 10:00' % d[4]['date'], '%s 15:00' % d[5]['date'], 30.0), id='1'),
            dict(_te_row('%s 10:00' % d[4]['date'], '%s 15:00' % d[5]['date'], -50.0), id='2'),
            dict(_te_row('%s 10:00' % d[4]['date'], '%s 15:00' % d[5]['date'], 10.0), id='3')]
    state = {'shadows': {'1': {'stock': 'FAST', 'entry_spot': 99.0, 'direction': 'CE'},
                         '2': {'stock': 'SLOW', 'entry_spot': 92.5, 'direction': 'CE'},
                         '3': {'stock': 'NOCANDLES', 'entry_spot': 99.0, 'direction': 'CE'}}}
    load = lambda s: None if s == 'NOCANDLES' else d
    ap = golive.approach(rows, state, load)
    assert (ap['fast_n'], ap['rest_n'], ap['unknown']) == (1, 1, 1)
    assert ap['fast_mean'] == 30.0 and ap['rest_mean'] == -50.0
    assert ap['state'] == 'IN PROGRESS'


def test_at_100_live_must_not_contradict_the_replay():
    d = _daily([90, 91, 92, 95, 99, 99])
    rows, shadows = [], {}
    for k in range(golive.TEST_N):
        fast = k % 5 == 0
        rows.append(dict(_te_row('%s 10:00' % d[4]['date'], '%s 15:00' % d[5]['date'],
                                 -10.0 if fast else 5.0), id=str(k)))
        shadows[str(k)] = {'stock': 'X', 'entry_spot': 99.0 if fast else 92.5, 'direction': 'CE'}
    ap = golive.approach(rows, {'shadows': shadows}, lambda s: d)
    assert ap['state'] == 'CONTRADICTS the replay'


def test_a_future_trade_is_captured_end_to_end(weekdays):
    """Open -> live polls over several sessions -> stop -> the pack: the time
    exit is priced from the marks the polls recorded (no value-path
    fallback), and the trade counts in the test population."""
    sh = _shadow()                                        # entered Tue 2026-09-01, naked entry 4.0
    days = ['2026-09-01', '2026-09-02', '2026-09-03', '2026-09-04', '2026-09-07']
    for day in days:
        d = date.fromisoformat(day)
        for h, m in ((12, 30), (15, 20)):
            ss.poll_one(sh, 100.2, _q(3.6, 3.7), _q(1, 1.1), datetime(d.year, d.month, d.day, h, m), d)
    ss.poll_one(sh, 99.0, _q(1.9, 2.0), _q(0.5, 0.6), datetime(2026, 9, 8, 12, 0), date(2026, 9, 8))
    a = sh['arms'][ss.MARKS_ARM]
    assert a['status'] == 'exited' and a['exit']['reason'] == 'stop'
    assert sorted(a['marks']) == days + ['2026-09-08']
    state = {'schema': ss.SCHEMA, 'shadows': {'1': sh}}
    rows = golive.arm_rows(state, {'1': _trade()}, 'naked_long', datetime(2026, 9, 9, 10, 0))
    tests = golive.test_rows(rows)
    assert len(tests) == 1 and tests[0]['net_pct'] < -50.0
    te = golive.time_exit(tests, state, golive.TIME_EXIT_N, {})
    assert te['n'] == 1 and te['cut'] == 1 and te['unpriced'] == 0
    assert te['variant'] > tests[0]['net_pct']            # cut at the 3.6 session-3 mark, not the stop
