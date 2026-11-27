"""import-legacy (one-time cutover) and the CLI's --final alert."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kite_login import core as C, migrate as M  # noqa: E402
import kite_login.__main__ as CLI  # noqa: E402


def _legacy(root, fifty_user='CU0002'):
    (root / 'SNAIL' / 'config').mkdir(parents=True)
    (root / 'SNAIL' / 'config' / 'creds.env').write_text(
        '# comment\nZERODHA_API_KEY=pk\nZERODHA_API_SECRET="ps"\nZERODHA_USER_ID=PU0001\n'
        "ZERODHA_PASSWORD='Pw9zQx'\nZERODHA_TOTP_SECRET=PT\nANTHROPIC_API_KEY=not-copied\n")
    (root / 'FIFTY' / 'data').mkdir(parents=True)
    (root / 'FIFTY' / 'data' / 'kite_credentials.json').write_text(json.dumps(
        {'api_key': 'ck', 'api_secret': 'cs', 'user_id': fifty_user, 'password': 'cp',
         'totp_secret': 'CT'}))


def test_import_builds_both_accounts_once(tmp_path):
    _legacy(tmp_path)
    cfg = C.load_config()
    ok, msg = M.import_legacy(cfg, tmp_path)
    assert ok and 'Pw9zQx' not in msg
    d = json.loads((tmp_path / 'data' / 'kite_credentials.json').read_text())
    assert d['personal'] == {'api_key': 'pk', 'api_secret': 'ps', 'user_id': 'PU0001',
                             'password': 'Pw9zQx', 'totp_secret': 'PT'}
    assert d['company']['user_id'] == 'CU0002'
    assert 'not-copied' not in json.dumps(d)
    ok2, msg2 = M.import_legacy(cfg, tmp_path)
    assert not ok2 and 'not overwritten' in msg2
    # the file it writes is exactly what core reads
    assert C.load_credentials(tmp_path / 'data' / 'kite_credentials.json', 'company')['api_key'] == 'ck'


def test_import_refuses_same_user_in_both(tmp_path):
    _legacy(tmp_path, fifty_user='PU0001')
    ok, msg = M.import_legacy(C.load_config(), tmp_path)
    assert not ok and 'SAME user_id' in msg
    assert not (tmp_path / 'data' / 'kite_credentials.json').exists()


def test_import_refuses_missing_key(tmp_path):
    _legacy(tmp_path)
    p = tmp_path / 'SNAIL' / 'config' / 'creds.env'
    p.write_text(p.read_text().replace('ZERODHA_TOTP_SECRET=PT\n', ''))
    ok, msg = M.import_legacy(C.load_config(), tmp_path)
    assert not ok and 'totp_secret' in msg


@pytest.mark.parametrize('final,alerts', [(False, 0), (True, 1)])
def test_cli_alerts_only_on_the_final_slot(monkeypatch, final, alerts):
    sent = []
    monkeypatch.setattr(CLI.C, 'run', lambda aliases, cfg: [C.Result('personal', C.FAILED, 'x')])
    monkeypatch.setattr(CLI.H, 'refresh', lambda path, force=False: (True, 'ok'))
    monkeypatch.setattr(CLI, 'send_telegram', lambda text, cfg: sent.append(text) or True)
    rc = CLI.main(['morning'] + (['--final'] if final else []))
    assert rc == 1 and len(sent) == alerts
    if alerts:
        assert 'personal token: FAILED' in sent[0]


def test_cli_good_morning_is_silent_and_zero(monkeypatch):
    sent = []
    monkeypatch.setattr(CLI.C, 'run', lambda aliases, cfg: [C.Result('personal', C.NOOP, ''),
                                                            C.Result('company', C.LOGGED_IN, '')])
    monkeypatch.setattr(CLI.H, 'refresh', lambda path, force=False: (True, 'ok'))
    monkeypatch.setattr(CLI, 'send_telegram', lambda text, cfg: sent.append(text) or True)
    assert CLI.main(['morning', '--final']) == 0 and not sent
