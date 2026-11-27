"""The real-order probe, end to end against FakeNeo (no network)."""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import client as C, probe as P, session as S, symbols as Y   # noqa: E402
from kotak_neo.fake import FakeNeo                                          # noqa: E402

SESS = S.Session(3, '2026-11-30', 'c', 'https://cis.kotaksecurities.com', 'T', 'SID', None,
                 'CK', 'U')
ROW = Y.ScripRow('RELIANCE26DEC1540CE', '144284', 'RELIANCE', date(2026, 12, 29), 1540.0,
                 'CE', 500, 0.05, 'OPTSTK')
NOW = datetime(2026, 11, 30, 11, 0, tzinfo=S.IST)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    S._ensure_home()
    (S.HOME / 'PROBE_ARM').write_text('2026-11-30', encoding='utf-8')
    neo = FakeNeo()
    neo.set_quote(ROW.trading_symbol, 2.0, 2.1)
    c = C.NeoClient('prb', http=neo, current_fn=lambda: SESS,
                    refresh_fn=lambda g, w: S.Result(S.STILL_ALIVE, SESS))
    return neo, c


def go(c, neo, **kw):
    lines, clk = [], Clock()
    kw.setdefault('confirm', True)
    kw.setdefault('now', NOW)
    rc = P.run(c, ROW, 500, lambda: neo.quotes[ROW.trading_symbol], sleep=clk.sleep,
               clock=clk, out=lines.append, **kw)
    return rc, '\n'.join(lines)


def test_the_full_probe_ends_flat(env):
    neo, c = env
    rc, text = go(c, neo)
    assert rc == 0 and neo.net(ROW.trading_symbol) == 0
    assert '1) BUY' in text and 'filled 500' in text
    assert '2) CANCEL of the filled buy: rejected stCode 1006' in text
    assert '3) SL sell' in text and 'resting' in text and '4) cancel SL -> cancelled' in text
    assert '5) chase -> done sold 500' in text and 'FLAT' in text


@pytest.mark.parametrize('kw,needle', [
    (dict(confirm=False), 'confirm'), (dict(now=NOW.replace(hour=15, minute=20)), 'outside'),
    (dict(ceiling=5000), 'code limit'), (dict(ceiling=500), 'ceiling')])
def test_refusals(env, kw, needle):
    neo, c = env
    with pytest.raises(P.ProbeRefused, match=needle):
        go(c, neo, **kw)
    assert neo.placed() == []


def test_not_armed_refuses(env):
    neo, c = env
    (S.HOME / 'PROBE_ARM').write_text('2026-11-29', encoding='utf-8')
    with pytest.raises(P.ProbeRefused, match='PROBE_ARM'):
        go(c, neo)


def test_lot_mismatch_refuses(env):
    neo, c = env
    with pytest.raises(P.ProbeRefused, match='lot size'):
        P.run(c, ROW, 250, lambda: neo.quotes[ROW.trading_symbol], confirm=True, now=NOW)
