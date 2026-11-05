"""Arming, the live config loader, and the live commands end to end on fakes."""
import json
import logging
from datetime import date, timedelta

import pytest

from conftest import APP, FakeMarket, T, default_premiums, sym
from live_helpers import SCRIPS, neo_raw
from nifty_btst import arming, cli, compare, config as config_mod, live_cli, live_config
from nifty_btst.broker import FakeBroker
from nifty_btst.kite_io import TokenError
from nifty_btst.live_config import MAX_LIVE_LOTS, build_neo, load_live
from test_probe_safety import FakeReader
from test_units import _pair_rows

TODAY = date(2026, 10, 26)
CE = sym('2026-10-27', 22850, 'CE')


def _raw(**over):
    raw = json.load(open(APP / 'btst_config.defaults.json'))
    raw['telegram_enabled'] = False
    raw.update(over)
    return raw


# ------------------------------------------------------------ neo config
def test_paper_config_carries_no_live_keys_and_the_defaults_validate():
    cfg = config_mod.build(_raw())
    assert not hasattr(cfg, 'execution') and not hasattr(cfg, 'neo')          # NEO-13/22
    neo = build_neo(neo_raw())
    assert neo.product == 'NRML' and neo.entry_cap_pct == 5.0 and neo.kill_drawdown_rs == 75000
    assert neo.unknown_grace_sec == 600 and neo.poll_seconds == 3 and neo.arm_max_days == 35
    assert neo.position_qty_unit == 'shares' and neo.empty_report_stcode == 5203
    assert neo.auth_stcodes == (1003,) and neo.transient_stcodes == (1005,)
    assert neo.rejected_stcodes == (1004, 1006, 1007, 1009, 100008)       # R3-09: wrong-IP is NOT placed
    assert neo.retry_stcodes == (100008, 429) and neo.telegram_enabled    # R3-09, R3-07
    assert not neo.holdings_source_enabled
    assert neo.positions_lag_sec == 60 and neo.empty_per_report == {}
    assert neo.credentials_file.name == 'neo_credentials.json' and neo.brokerage_per_order == 0.0


@pytest.mark.parametrize('key,val,match', [
    ('product', 'MIS', 'NRML'),
    ('max_orders_per_day', 1, 'max_orders_per_day'),
    ('exit_retry_slow_sec', 1, 'exit_retry_slow_sec'),
    ('entry_cap_pct', -1, 'entry_cap_pct'),
    ('max_entry_attempts', 1.5, 'whole number'),
    ('http_timeout_sec', 0, 'http_timeout_sec'),
    ('expiry_backstop_time', '9:30', 'HH:MM:SS'),
    ('unknown_grace_sec', 1, 'unknown_grace_sec'),
    ('position_qty_unit', 'contracts', 'position_qty_unit'),
    ('empty_report_positions', 'x', 'empty_report_positions'),
    ('rejected_stcodes', 'x', 'list of integers'),
    ('poll_seconds', 0.5, 'rate limit'),
    ('empty_ok_without_data', 'no', 'true or false'),
    ('auth_stcodes', 'x', 'list of integers'),
    ('empty_report_stcode', 'abc', 'integer'),
])
def test_neo_block_rejects(key, val, match):
    with pytest.raises(config_mod.ConfigError, match=match):
        build_neo(neo_raw(**{key: val}))


def test_load_live_reads_defaults_plus_the_live_overlay_only(tmp_path):
    defaults = tmp_path / 'd.json'
    defaults.write_text(json.dumps(_raw()))
    ov = tmp_path / 'live.json'
    assert load_live(defaults, ov)[0] == 'paper'
    ov.write_text(json.dumps({'execution': 'live', 'neo': {'kill_drawdown_rs': 50000}}))
    execution, neo = load_live(defaults, ov)
    assert execution == 'live' and neo.kill_drawdown_rs == 50000 and neo.entry_cap_pct == 5.0
    ov.write_text(json.dumps({'execution': 'LIVE'}))
    with pytest.raises(config_mod.ConfigError, match='execution'):
        load_live(defaults, ov)
    ov.write_text(json.dumps({'neo': 5}))
    with pytest.raises(config_mod.ConfigError, match='neo'):
        load_live(defaults, ov)
    ov.write_text('[1]')
    with pytest.raises(config_mod.ConfigError):
        load_live(defaults, ov)
    with pytest.raises(config_mod.ConfigError, match='neo'):
        build_neo({})
    with pytest.raises(config_mod.ConfigError, match='credentials_file'):
        build_neo({'login_url': 'x'})


# ---------------------------------------------------------------- arming
def test_read_arm_dates_and_horizon(tmp_path):
    assert arming.read_arm(tmp_path, TODAY) is None
    arm = tmp_path / arming.ARM_FILE
    arm.write_text('2026-10-26\n')
    assert arming.read_arm(tmp_path, TODAY) == TODAY
    arm.write_text('2026-10-25')
    assert arming.read_arm(tmp_path, TODAY) is None               # expired
    arm.write_text('2026-11-20 # through the window')
    assert arming.read_arm(tmp_path, TODAY) == date(2026, 11, 20)
    arm.write_text('2099-01-01')
    assert arming.read_arm(tmp_path, TODAY) is None               # beyond the horizon (NEO-18)
    assert arming.read_arm(tmp_path, TODAY, max_days=100000) == date(2099, 1, 1)
    arm.write_text('yes')
    assert arming.read_arm(tmp_path, TODAY) is None               # a flag is not a date
    arm.write_text('')
    assert arming.read_arm(tmp_path, TODAY) is None


def test_probe_arm_is_today_only_and_separate(tmp_path):
    assert not arming.read_probe_arm(tmp_path, TODAY)
    (tmp_path / arming.ARM_FILE).write_text('2026-10-26')           # strategy ARM
    assert not arming.read_probe_arm(tmp_path, TODAY)               # does not arm the probe
    (tmp_path / arming.PROBE_ARM_FILE).write_text('2026-10-25')
    assert not arming.read_probe_arm(tmp_path, TODAY)               # yesterday's probe arm
    (tmp_path / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    assert arming.read_probe_arm(tmp_path, TODAY)


def pass_fixture(tmp_path):
    """A paper book + Volrix CSV that read PASS (30 clean pairs)."""
    paper, vol = _pair_rows(30, paper_exit=170.0, volrix_exit=170.0)
    for i, p in enumerate(paper, 1):
        p.update({'id': i, 'status': 'closed', 'symbol': 'S', 'exit_reason': 'next_day_0925'})
    book = tmp_path / 'paper_book.json'
    book.write_text(json.dumps({'version': 1, 'next_id': 31, 'days': {}, 'positions': paper}))
    csv = tmp_path / 'v.csv'
    lines = ['Entry Date,Instrument,Entry Price,Exit Price,PnL,Remarks']
    for v in vol:
        lines.append('%s,"%s",%s,%s,%s,%s' % (v['entry_date'].isoformat(), v['instrument'],
                                             v['entry_price'], v['exit_price'], v['pnl'], v['remarks']))
    csv.write_text('\n'.join(lines) + '\n')
    return book, csv


def test_gate_verdict_fails_closed(tmp_path):
    neo = build_neo(neo_raw())
    assert arming.gate_verdict(neo, [])[0] == 'NOT EVALUATED'
    neo = build_neo(neo_raw(volrix_csv=str(tmp_path / 'missing.csv')))
    assert arming.gate_verdict(neo, [])[0] == 'NOT EVALUATED'
    csv = tmp_path / 'v.csv'
    csv.write_text('a,b\n1,2\n')
    neo = build_neo(neo_raw(volrix_csv=str(csv)))
    assert arming.gate_verdict(neo, [])[0] == 'ERROR'
    csv.write_text('Entry Date,Instrument,Entry Price,Exit Price,PnL\n2026-10-30,NIFTY 23000 CE\n')
    assert arming.gate_verdict(neo, [])[0] == 'ERROR'               # short row (NEO-09)
    csv.write_text('Entry Date,Instrument,Entry Price,Exit Price,PnL\n'
                   '30 Oct 2026,NIFTY 22850 CE,153,160,400\n')
    assert arming.gate_verdict(neo, [])[0] == 'IN PROGRESS'
    book, csv = pass_fixture(tmp_path)
    neo = build_neo(neo_raw(volrix_csv=str(csv)))
    assert arming.gate_verdict(neo, arming.read_paper_positions(book))[0] == 'PASS'
    assert compare.gate(arming.read_paper_positions(book), compare.load_volrix(csv))['verdict'] == 'PASS'
    assert arming.read_paper_positions(tmp_path / 'none.json') == []
    (tmp_path / 'garbage.json').write_text('{{{')
    assert arming.read_paper_positions(tmp_path / 'garbage.json') == []


def test_check_refuses_paper_missing_credentials_and_lots(tmp_path):
    cfg = config_mod.build(_raw())
    neo = build_neo(neo_raw(credentials_file=str(tmp_path / 'none.json')))
    rep = arming.check(cfg, 'paper', neo, tmp_path, TODAY, [])
    assert not rep.may_run and len(rep.problems) == 2
    assert any('paper' in p for p in rep.problems) and any('credentials' in p for p in rep.problems)
    assert 'entries blocked' in rep.text() and 'NOT ARMED' in rep.text()
    creds = tmp_path / 'c.json'
    creds.write_text('{}')
    cfg = config_mod.build(_raw(lots=MAX_LIVE_LOTS + 1))
    neo = build_neo(neo_raw(credentials_file=str(creds)))
    rep = arming.check(cfg, 'live', neo, tmp_path, TODAY, [])
    assert not rep.may_run and 'MAX_LIVE_LOTS' in rep.text()


def test_check_live_needs_arm_and_pass_and_rehearsal_is_always_armed(tmp_path):
    """NEO-07: ARM alone never arms; PASS alone never arms; both do."""
    creds = tmp_path / 'c.json'
    creds.write_text('{}')
    cfg = config_mod.build(_raw())
    neo = build_neo(neo_raw(credentials_file=str(creds)))
    rep = arming.check(cfg, 'live', neo, tmp_path, TODAY, [])
    assert rep.may_run and not rep.armed and 'NOT ARMED' in rep.text()
    (tmp_path / arming.ARM_FILE).write_text('2026-10-30')
    rep = arming.check(cfg, 'live', neo, tmp_path, TODAY, [])
    assert not rep.armed and 'not PASS' in rep.text()
    book, csv = pass_fixture(tmp_path)
    neo = build_neo(neo_raw(credentials_file=str(creds), volrix_csv=str(csv)))
    rep = arming.check(cfg, 'live', neo, tmp_path, TODAY, arming.read_paper_positions(book))
    assert rep.armed_until == date(2026, 10, 30) and 'reads PASS' in rep.text()
    (tmp_path / arming.ARM_FILE).unlink()
    rep = arming.check(cfg, 'live', neo, tmp_path, TODAY, arming.read_paper_positions(book))
    assert not rep.armed                                             # PASS alone is not enough
    rep = arming.check(cfg, 'rehearsal', neo, tmp_path, TODAY, [])
    assert rep.armed and 'rehearsal' in rep.text()
    reader = arming.make_arm_reader(neo, tmp_path, book)
    assert reader(TODAY) is None
    (tmp_path / arming.ARM_FILE).write_text('2026-10-30')
    assert reader(TODAY) == date(2026, 10, 30)


# ------------------------------------------------------------------- CLI
@pytest.fixture
def wired(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    live_state = tmp_path / 'state_live'
    tok = tmp_path / 'tok.json'
    tok.write_text(json.dumps({'access_token': 'a', 'api_key': 'k',
                               'generated_at': '2026-10-26T08:45:00'}))
    creds = tmp_path / 'neo_credentials.json'
    creds.write_text(json.dumps({'consumer_key': 'c', 'mobile_number': '+911', 'ucc': 'u',
                                 'mpin': '1', 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}))
    raw = _raw()
    raw['neo']['credentials_file'] = str(creds)
    raw['neo']['fake_fill_delay_sec'] = 0
    defaults = tmp_path / 'd.json'
    defaults.write_text(json.dumps(raw))
    live_overlay = tmp_path / 'btst_live_config.json'
    live_overlay.write_text(json.dumps({'execution': 'rehearsal'}))
    monkeypatch.setattr(config_mod, 'STATE_DIR', state)
    monkeypatch.setattr(config_mod, 'KITE_TOKEN_FILE', tok)
    monkeypatch.setattr(config_mod, 'DEFAULTS_FILE', defaults)
    monkeypatch.setattr(config_mod, 'OVERLAY_FILE', tmp_path / 'none.json')
    monkeypatch.setattr(config_mod, 'TELEGRAM_CONFIG', tmp_path / 'tg.json')
    monkeypatch.setattr(live_config, 'DEFAULTS_FILE', defaults)
    monkeypatch.setattr(live_config, 'LIVE_OVERLAY_FILE', live_overlay)
    monkeypatch.setenv('BTST_LIVE_STATE_DIR', str(live_state))
    fm = FakeMarket()
    fm.today = TODAY
    default_premiums(fm, '2026-10-27')
    monkeypatch.setattr(live_cli.KiteMarket, 'from_token', classmethod(lambda c, t, **k: fm))
    monkeypatch.setattr(cli.KiteMarket, 'from_token', classmethod(lambda c, t, **k: fm))
    clock = {'now': T('2026-10-26', '09:20:05')}
    monkeypatch.setattr(live_cli, 'now_ist', lambda: clock['now'])
    monkeypatch.setattr(cli, 'now_ist', lambda: clock['now'])
    monkeypatch.setattr(live_cli.time, 'sleep', lambda s: None)
    monkeypatch.setattr(cli.time, 'sleep', lambda s: None)
    fbs = []

    class R:
        ready = False
        creds = {'ucc': 'UCCX'}

        def login(self):
            self.ready = True

    def build(execution, neo, sdir, clock_):
        # a NEW fake per process, loaded from disk: what rehearsal really does
        fb = FakeBroker(clock_, fill_delay=neo.fake_fill_delay_sec, path=sdir / 'fake_broker.json')
        fbs.append(fb)
        return fb, R()
    monkeypatch.setattr(live_cli, 'build_broker', build)
    monkeypatch.setattr(live_cli, 'make_scrip_loader', lambda *a, **k: (lambda: SCRIPS))
    monkeypatch.setattr(live_cli, 'pin_ipv4', lambda: None)     # never patch the test process
    return {'state': state, 'live': live_state, 'defaults': defaults, 'raw': raw, 'fm': fm,
            'clock': clock, 'fbs': fbs, 'overlay': live_overlay, 'tok': tok, 'tmp': tmp_path}


def _set_execution(w, mode, **neo):
    w['overlay'].write_text(json.dumps({'execution': mode, 'neo': neo}))


def _book(w):
    return json.loads((w['live'] / 'book.json').read_text())


def _token_for(w, day):
    tok = json.loads(w['tok'].read_text())
    tok['generated_at'] = '%sT08:45:00' % day
    w['tok'].write_text(json.dumps(tok))


def _run_entry(w):
    assert cli.main(['live-run', '--once']) == 0
    w['fm'].set(CE, 153.5, bid=153.3, ask=153.8)
    w['clock']['now'] = T('2026-10-26', '10:00:00')
    assert cli.main(['live-run', '--once']) == 0
    w['clock']['now'] = T('2026-10-26', '10:00:02')
    assert cli.main(['live-run', '--once']) == 0


def test_live_run_refuses_in_paper_mode_and_paper_run_ignores_live_config(wired):
    _set_execution(wired, 'paper')
    assert cli.main(['live-run', '--once']) == live_cli.EXIT_REFUSED
    assert not wired['live'].exists()
    assert cli.main(['run', '--once']) == 0
    assert (wired['state'] / 'book.json').exists() and not wired['live'].exists()


def test_paper_is_not_stopped_by_a_broken_live_overlay_or_a_live_typo_in_its_own(wired):
    """NEO-13: paper must not validate, coerce or be stopped by live keys."""
    wired['overlay'].write_text('{{{ garbage')
    assert cli.main(['run', '--once']) == 0
    wired['overlay'].write_text(json.dumps({'execution': 'lve', 'neo': 5}))
    assert cli.main(['run', '--once']) == 0
    paper_overlay = wired['tmp'] / 'paper_overlay.json'
    paper_overlay.write_text(json.dumps({'execution': 'lve', 'neo': 5, 'lots': 1}))
    import nifty_btst.config as c
    c_overlay = c.OVERLAY_FILE
    try:
        c.OVERLAY_FILE = paper_overlay
        assert cli.main(['run', '--once']) == 0
    finally:
        c.OVERLAY_FILE = c_overlay
    assert cli.main(['live-run', '--once']) == 5               # the LIVE process says so


def test_live_run_once_in_rehearsal_marks_maps_and_keeps_paper_untouched(wired, capsys):
    assert cli.main(['live-run', '--once']) == 0
    day = _book(wired)['days']['2026-10-26']
    assert day['mark_done'] and day['marks']['CE']['neo']['symbol'].startswith('NEO-')
    assert 'arming_said' in day['flags']
    assert not (wired['state'] / 'book.json').exists()           # paper's dir untouched
    assert cli.main(['live-status']) == 0
    out = capsys.readouterr().out
    assert 'REHEARSAL' in out and 'kill: ok' in out and 'NEO-' in out
    assert cli.main(['live-report']) == 0
    wired['clock']['now'] = T('2026-10-26', '15:45:00')
    assert cli.main(['live-digest']) == 0
    assert _book(wired)['days']['2026-10-26']['eod_sent']


def test_rehearsal_survives_a_restart_and_an_overnight_on_the_persisted_fake(wired):
    """NEO-12: every `live-run` builds a new fake, loaded from disk."""
    _run_entry(wired)
    [p] = _book(wired)['positions']
    assert p['status'] == 'open' and p['entry_price'] == 153.8 and p['live']['mode'] == 'rehearsal'
    assert (wired['live'] / 'fake_broker.json').exists() and len(wired['fbs']) == 3
    default_premiums(wired['fm'], '2026-11-03')
    wired['fm'].set(CE, 170.0, bid=169.8, ask=170.2)
    wired['fm'].today = date(2026, 10, 27)
    _token_for(wired, '2026-10-27')
    wired['clock']['now'] = T('2026-10-27', '09:25:01')
    assert cli.main(['live-run', '--once']) == 0
    wired['clock']['now'] = T('2026-10-27', '09:25:03')
    assert cli.main(['live-run', '--once']) == 0
    [p] = _book(wired)['positions']
    assert p['status'] == 'closed' and p['exit_reason'] == 'next_day_0925' and p['exit_price'] == 169.8
    assert not _book(wired)['kill']['tripped']


def test_live_mode_arm_without_pass_runs_exits_only_and_pass_arms(wired, caplog):
    """NEO-07: the strategy needs ARM + PASS; the probe's arm never counts."""
    caplog.set_level(logging.INFO)
    _set_execution(wired, 'live')
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.ARM_FILE).write_text('2026-10-26')
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    _run_entry(wired)
    assert any('not PASS' in r.message for r in caplog.records)
    assert not any(q.side == 'B' for fb in wired['fbs'] for q in fb.sent)
    [p] = _book(wired)['positions']
    # R3-24: a side that triggers while entries are blocked is ABANDONED at
    # once, so arming later can never buy a stale trigger
    assert p['status'] == 'closed' and p['never_filled'] and p['exit_reason'] == 'entry_abandoned'
    book, csv = pass_fixture(wired['tmp'])
    _set_execution(wired, 'live', volrix_csv=str(csv))
    import shutil
    wired['state'].mkdir(parents=True, exist_ok=True)
    shutil.copy(book, wired['state'] / 'book.json')
    wired['clock']['now'] = T('2026-10-26', '10:01:05')
    assert cli.main(['live-run', '--once']) == 0
    assert any('reads PASS' in r.message for r in caplog.records)
    assert not any(q.side == 'B' for fb in wired['fbs'] for q in fb.sent)   # the 10:00 trigger is gone


def test_live_run_after_close_sends_digest_and_weekend_does_nothing(wired):
    wired['clock']['now'] = T('2026-10-26', '15:45:00')
    assert cli.main(['live-run']) == 0
    assert _book(wired)['days']['2026-10-26']['eod_sent']
    wired['clock']['now'] = T('2026-10-31', '10:00:00')
    assert cli.main(['live-run']) == 0


def test_stale_or_corrupt_kite_token_does_not_stop_the_live_process(wired, caplog):
    """NEO-08: the engine starts and services NEO without quotes; the alert
    names the open position."""
    caplog.set_level(logging.INFO)
    _run_entry(wired)
    assert _book(wired)['positions'][0]['status'] == 'open'
    tok = json.loads(wired['tok'].read_text())
    tok['generated_at'] = '2026-10-25T08:45:00'
    wired['tok'].write_text(json.dumps(tok))
    caplog.clear()
    wired['clock']['now'] = T('2026-10-26', '10:05:00')
    assert cli.main(['live-run', '--once']) == 0                    # not 2
    msgs = [r.message for r in caplog.records if 'ALERT' in r.message]
    assert any('NO KITE QUOTES' in m and CE in m for m in msgs)
    assert len(wired['fbs']) == 4                                    # the broker was built and used
    wired['tok'].write_text('{not json')
    assert cli.main(['live-run', '--once']) == 0


def test_corrupt_paper_book_or_short_volrix_row_does_not_stop_live(wired):
    """NEO-09: paper's problems are optional inputs; a live problem alerts."""
    wired['state'].mkdir(parents=True, exist_ok=True)
    (wired['state'] / 'book.json').write_text('garbage')
    csvf = wired['tmp'] / 'v.csv'
    csvf.write_text('Entry Date,Instrument,Entry Price,Exit Price,PnL\n2026-10-30,NIFTY 23000 CE\n')
    _set_execution(wired, 'rehearsal', volrix_csv=str(csvf))
    assert cli.main(['live-run', '--once']) == 0
    assert _book(wired)['days']['2026-10-26']['mark_done']


def test_bad_live_record_is_refused_with_an_alert_not_a_traceback(wired, caplog):
    caplog.set_level(logging.INFO)
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / 'book.json').write_text(json.dumps(
        {'version': 1, 'next_id': 2, 'days': {},
         'positions': [{'id': 1, 'status': 'open', 'symbol': 'X'}]}))
    assert cli.main(['live-run', '--once']) == 3
    assert any('LIVE BOOK CORRUPT' in r.message for r in caplog.records)
    (wired['live'] / 'book.json').write_text('garbage')
    assert cli.main(['live-run', '--once']) == 3                    # alerted once a day only


def test_unexpected_startup_exception_is_alerted(wired, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(live_cli.LiveEngine, 'start', lambda self, now: 1 / 0)
    assert cli.main(['live-run', '--once']) == 1
    assert any('LIVE RUN CRASHED' in r.message for r in caplog.records)


def test_live_resume_writes_a_request_the_loop_applies(wired, capsys):
    """NEO-17: no unlocked save over a running loop's memory."""
    assert cli.main(['live-run', '--once']) == 0
    assert cli.main(['live-resume']) == 0                            # nothing tripped
    book_path = wired['live'] / 'book.json'
    book = json.loads(book_path.read_text())
    book['kill'] = {'tripped': True, 'reason': 'test', 'at': 'x', 'peak': 10.0, 'equity': 3.0,
                    'resets': [], 'loud': False}
    book_path.write_text(json.dumps(book))
    assert cli.main(['live-resume']) == 1
    assert cli.main(['live-resume', '--yes']) == 0
    k = json.loads(book_path.read_text())['kill']
    assert not k['tripped'] and k['peak'] == 3.0 and k['resets'][0]['by'].startswith('owner')
    assert not (wired['live'] / 'RESUME').exists()


def test_order_probe_cli_refusals(wired, capsys):
    assert cli.main(['neo-order-probe', '--symbol', 'X']) == live_cli.EXIT_REFUSED
    assert 'execution is' in capsys.readouterr().out
    _set_execution(wired, 'live')
    assert cli.main(['neo-order-probe', '--symbol', 'X']) == live_cli.EXIT_REFUSED
    assert '--confirm-real-order' in capsys.readouterr().out
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.ARM_FILE).write_text('2026-10-26')       # strategy ARM is not enough
    assert cli.main(['neo-order-probe', '--symbol', 'X', '--confirm-real-order']) == live_cli.EXIT_REFUSED
    assert 'PROBE_ARM' in capsys.readouterr().out
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    assert cli.main(['neo-order-probe', '--symbol', 'X', '--confirm-real-order',
                     '--max-premium', '5000']) == live_cli.EXIT_REFUSED
    assert 'code ceiling' in capsys.readouterr().out
    wired['clock']['now'] = T('2026-10-26', '16:00:00')
    assert cli.main(['neo-order-probe', '--symbol', 'X', '--confirm-real-order']) == live_cli.EXIT_REFUSED
    assert 'outside market hours' in capsys.readouterr().out


def test_order_probe_cli_runs_against_the_fake_and_always_notifies(wired, capsys, caplog):
    caplog.set_level(logging.INFO)
    _set_execution(wired, 'live')
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    wired['clock']['now'] = T('2026-10-26', '10:00:00')
    s = sym('2026-10-27', 22000, 'CE')                               # far OTM, cheap
    wired['fm'].set(s, 5.0, bid=4.8, ask=5.0)
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order']) == 0
    assert (wired['live'] / 'intents.jsonl').exists()
    # the fake fills A's resting order at once, so B is skipped (R2-17)
    assert [r.side for r in wired['fbs'][-1].sent] == ['B', 'S']
    assert any('ORDER PROBE [all] OK' in r.message for r in caplog.records)
    wired['fm'].set(s, 50.0, bid=49.8, ask=50.0)                     # too dear for the probe
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order']) == 1
    out = capsys.readouterr().out
    assert 'pick a cheaper' in out
    assert any('ORDER PROBE [all] ATTENTION' in r.message for r in caplog.records)
    wired['fm'].books.pop(s)
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order']) == live_cli.EXIT_REFUSED
    assert 'no Kite quote' in capsys.readouterr().out
    assert not any(q.side == 'B' and q.qty != 65 for fb in wired['fbs'] for q in fb.sent)


def test_order_probe_cli_hold_and_sell_steps(wired):
    _set_execution(wired, 'live')
    wired['live'].mkdir(parents=True, exist_ok=True)
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-26')
    wired['clock']['now'] = T('2026-10-26', '10:00:00')
    s = sym('2026-10-27', 22000, 'CE')
    wired['fm'].set(s, 5.0, bid=4.8, ask=5.0)
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order', '--step', 'hold']) == 0
    wired['clock']['now'] = T('2026-10-27', '09:21:00')
    wired['fm'].today = date(2026, 10, 27)
    _token_for(wired, '2026-10-27')
    (wired['live'] / arming.PROBE_ARM_FILE).write_text('2026-10-27')
    assert cli.main(['neo-order-probe', '--symbol', s, '--confirm-real-order', '--step', 'sell']) == 0
    fb = wired['fbs'][-1]
    assert fb.true_net('NEO-' + s[::-1]) == 0


def test_neo_probe_cli_uses_the_reader_only(wired, monkeypatch, capsys):
    seen = {}

    def fake_probe(reader, underlying, today, kite_row=None, out=print, net_field='Net'):
        seen['reader'] = type(reader).__name__
        seen['row'] = kite_row
        return 0
    monkeypatch.setattr(live_cli, 'run_probe', fake_probe)
    assert cli.main(['neo-probe', '--symbol', CE]) == 0
    assert seen['reader'] == 'NeoReader' and seen['row']['strike'] == 22850.0
    assert not (wired['state'] / 'book.json').exists()
    assert not list(wired['state'].glob('instruments_*')) if wired['state'].exists() else True


# ---------------------------------------- the loop, the loader, the order probe
def _advancing_clock(wired, monkeypatch, start):
    clock = wired['clock']
    clock['now'] = start

    def advance(s):
        clock['now'] += timedelta(seconds=max(s, 1.0))
    monkeypatch.setattr(live_cli.time, 'sleep', advance)
    return clock


def test_live_loop_runs_to_the_close_and_survives_a_dead_kite_token(wired, monkeypatch):
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:39:50'))
    fm = wired['fm']
    real_quote = fm.quote
    calls = {'n': 0}

    def quote(keys):
        calls['n'] += 1
        if calls['n'] == 1:
            raise TokenError('token rejected')
        return real_quote(keys)
    fm.quote = quote
    assert cli.main(['live-run']) == 0
    day = _book(wired)['days']['2026-10-26']
    assert day['eod_sent'] and 'token_alert' in day['flags'] and calls['n'] >= 1


def test_live_loop_tick_errors_are_counted_not_fatal(wired, monkeypatch):
    _advancing_clock(wired, monkeypatch, T('2026-10-26', '15:39:55'))

    def boom(self, now):
        raise RuntimeError('tick exploded')
    monkeypatch.setattr(live_cli.LiveEngine, 'service_orders', boom)
    assert cli.main(['live-run']) == 0


def test_live_loop_waits_for_the_open(wired, monkeypatch):
    clock = wired['clock']
    clock['now'] = T('2026-10-26', '09:14:50')
    slept = []

    def advance(s):
        slept.append(s)
        clock['now'] += timedelta(seconds=max(s, 1.0))
        if len(slept) > 5:
            clock['now'] = T('2026-10-26', '15:41:00')
    monkeypatch.setattr(live_cli.time, 'sleep', advance)
    assert cli.main(['live-run']) == 0
    assert slept and slept[0] <= 30.0


def test_make_scrip_loader_downloads_then_caches(tmp_path):
    neo = build_neo(neo_raw())
    old = tmp_path / 'scrip_master_nse_fo_20261020.csv'
    old.write_text('stale')
    reader = FakeReader()
    load = live_cli.make_scrip_loader(reader, neo, tmp_path, TODAY, 'NIFTY')
    with pytest.raises(live_cli.NeoUnavailable, match='the worker waits'):
        load()                                                  # R3-34: the loader never logs in
    assert not reader.ready and not (tmp_path / 'scrip_master_nse_fo_20261026.csv').exists()
    reader.login()
    m = load()
    assert len(m) == 2 and reader.ready and not old.exists()
    cache = tmp_path / 'scrip_master_nse_fo_20261026.csv'
    assert cache.exists()
    reader.fail.add('paths')                                    # the cache must carry it now
    assert len(load()) == 2
    cache.unlink()
    with pytest.raises(Exception):
        load()
    (tmp_path / 'x').mkdir()
    nofo = FakeReader({'nofo'})
    nofo.login()
    with pytest.raises(Exception, match='nse_fo'):
        live_cli.make_scrip_loader(nofo, neo, tmp_path / 'x', TODAY, 'NIFTY')()
