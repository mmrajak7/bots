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


def test_the_report_says_when_it_cannot_decide_yet(monkeypatch):
    sh = _shadow()
    monkeypatch.setattr(ss, '_load', lambda: {'schema': ss.SCHEMA, 'shadows': {'1': sh}})

    class Store:
        def load_trades(self):
            return [_trade()]
    out = golive.report(Store(), now=MID)
    assert 'NOT YET DECIDABLE' in out and '1 STRUCTURE' in out and '6 BANDS' in out


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
