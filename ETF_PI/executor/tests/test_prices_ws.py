"""Review B3 (2026-11-28): the Kite WS rebuild back-off and per-ticker callbacks."""
import json
import os
import sys

ETF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ETF)
from executor.prices import PriceFeed  # noqa: E402


class FakeTicker:
    MODE_FULL = 'full'

    def __init__(self):
        self.closed, self.subs = False, []

    def connect(self, threaded=True):
        pass                                          # the handshake outlasts one loop

    def close(self):
        self.closed = True

    def subscribe(self, t):
        self.subs += t

    def set_mode(self, m, t):
        pass


def _feed(tmp_path):
    tok = tmp_path / 't.json'
    tok.write_text(json.dumps({'api_key': 'x', 'access_token': 'y'}))
    clock, built = [1000.0], []

    def factory(k, a):
        t = FakeTicker()
        built.append(t)
        return t
    f = PriceFeed({'price_stale_s': 5, 'rest_poll_s': 2}, str(tok), str(tmp_path),
                  rest_quote=lambda k: None, ticker_factory=factory, clock=lambda: clock[0])
    return f, clock, built


def test_b3_a_down_feed_is_rebuilt_once_a_minute_not_every_loop(tmp_path):
    f, clock, built = _feed(tmp_path)
    f.start()
    built[0].on_close(None, 1006, 'net down')
    for _ in range(250):                              # 125 s of 0.5 s loops
        clock[0] += 0.5
        f.maybe_restart()
    assert len(built) == 3, len(built)                # the first + one rebuild per 60 s


def test_b3_a_replaced_tickers_late_close_leaves_the_live_one_healthy(tmp_path):
    f, clock, built = _feed(tmp_path)
    f.start()
    old = built[0]
    clock[0] += 61
    old.on_close(None, 1006, 'x')
    clock[0] += 61
    assert f.maybe_restart()
    new = built[-1]
    new.on_connect(new, {})
    assert f.ws_healthy()
    old.on_close(None, 1000, 'closed by us')          # the old one reports late
    old.on_error(None, 1000, 'late')
    assert f.ws_healthy()
    f.watch('NIFTYX', 999)
    assert 999 in new.subs


def test_b3_a_failing_build_also_backs_off(tmp_path):
    f, clock, built = _feed(tmp_path)
    os.remove(f.token_path)                           # no token file yet (before 08:30)
    f.start()
    for _ in range(240):                              # 120 s
        clock[0] += 0.5
        f.maybe_restart()
    assert 'ticker build failed' in f.ws_error() and built == []
    assert f._ws_started >= 1000.0 + 60               # it did retry, on the minute
