"""Offline replay: drive the REAL `monitor_all` against a recorded session.

Required by `feedback_live_automation_bar` before any auto-order code goes
live. The unit tests in this package each prove one guard in isolation; this
proves the guards compose — that the whole poll loop, with its market-open
buffers, debounce, re-verify and close sequence wired together, does the right
thing on a session that actually happened.

That distinction is not academic. Every individual guard for the Feb-2026
incident existed in some form by July, and the July incident still cost money,
because the question "does the loop as assembled refuse this book" was never
asked of anything but prose.

What is faked and what is not
-----------------------------
FAKE: the broker (`fakes.FakeBroker`), the stores (`fakes.MemoryStore`),
Telegram, and the clock. **Everything else is the production code path** —
`monitor_all` itself, `get_spread_value`, `leg_quote_reliable`, the intrinsic
floor, `spot_corroborates`, the debounce, `close_spread`, `close_leg`.

The clock is the interesting one. `is_market_settled` and `is_spread_settled`
read `datetime.now()`, not `time.time()`, so a `time`-only fake leaves the two
most important buffers in this file reading the real wall clock — and a replay
of a 09:15 book would run with both buffers already satisfied, silently
skipping the guard the recording exists to test. `ReplayClock` drives both
surfaces from one timeline.
"""
from datetime import date as _date, datetime as _datetime, timedelta

from bcs.tests.fakes import FakeBroker, MemoryStore, TelegramSpy


class ReplayClock:
    """One timeline behind `time.time()`, `datetime.now()` and `date.today()`.

    Patching `module.datetime` with a `datetime` SUBCLASS keeps `.combine`,
    `.strptime` and arithmetic working; only `.now()` is redirected.
    """

    MAX_STEPS = 50_000

    def __init__(self, day, at='09:15:00'):
        h, m, s = (int(x) for x in at.split(':'))
        self.dt = _datetime.combine(day, _datetime.min.time()).replace(
            hour=h, minute=m, second=s)
        self.stop_at = None          # jump here once the recording runs out
        self.steps = 0
        self.slept = []

    # -- the `time` module surface ------------------------------------------
    def time(self):
        return self.dt.timestamp()

    def sleep(self, sec):
        self.slept.append(sec)
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            raise AssertionError(
                'the replay ran %d poll cycles without the monitor exiting — '
                'failing loudly instead of hanging' % self.steps)
        self.dt += timedelta(seconds=sec)
        if self.stop_at is not None and self.dt >= self.stop_at:
            # Past the end of the recording: walk the clock to after the close
            # so `monitor_all` exits through its OWN market-close branch rather
            # than through something the harness invented.
            self.dt = _datetime.combine(self.dt.date(), _datetime.min.time()
                                        ).replace(hour=15, minute=31)
            self.stop_at = None

    def advance(self, sec):
        self.dt += timedelta(seconds=sec)

    def install(self, monkeypatch, module):
        monkeypatch.setattr(module.time, 'time', self.time)
        monkeypatch.setattr(module.time, 'sleep', self.sleep)
        clock = self

        class _DT(_datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.dt

        class _D(_date):
            @classmethod
            def today(cls):
                return clock.dt.date()

        monkeypatch.setattr(module, 'datetime', _DT)
        monkeypatch.setattr(module, 'date', _D)
        return self


class Tick:
    """One recorded observation: spot plus both legs' top of book.

    A leg is `{'bid':, 'ask':, 'bid_qty':, 'ask_qty':, 'ltp':, 'prev_close':}`.
    `bid=0` means the book had no bid at all, which is the shape that started
    both real incidents — it is a recording, not a placeholder.
    """

    def __init__(self, at, spot, long, short, note=''):
        h, m, s = (int(x) for x in at.split(':'))
        self.at = (h, m, s)
        self.spot = spot
        self.long = dict(long)
        self.short = dict(short)
        self.note = note

    def when(self, day):
        return _datetime.combine(day, _datetime.min.time()).replace(
            hour=self.at[0], minute=self.at[1], second=self.at[2])


def _full(book):
    d = {'bid': 0.0, 'ask': 0.0, 'bid_qty': 0, 'ask_qty': 0,
         'ltp': 0.0, 'prev_close': 0.0}
    d.update(book)
    return d


class TickBroker(FakeBroker):
    """A FakeBroker whose quotes come from whichever tick the clock is in.

    Fills still MOVE positions (inherited), so "no order was placed" and "the
    short leg went to +2100" are both assertions about what the code did.
    """

    def __init__(self, clock, day, ticks, trade, faults=None, **kw):
        self._clock = clock
        self._day = day
        self._ticks = sorted(ticks, key=lambda t: t.at)
        self._trade = trade
        # [(what, 'HH:MM:SS', exc)] — a broker that starts failing PART WAY
        # through a session. A broker that fails from the first call is a
        # different and much easier test: the interesting failures (an expired
        # token, a symbol renamed by a corp action) arrive with positions
        # already open and the loop already running.
        self._faults = [(w, tuple(int(x) for x in at.split(':')), e)
                        for w, at, e in (faults or [])]
        self.seen = []               # every tick the run actually reached
        super().__init__(books={}, spots={}, **kw)

    def _fault(self, what):
        now = (self._clock.dt.hour, self._clock.dt.minute, self._clock.dt.second)
        for w, at, exc in self._faults:
            if w == what and now >= at:
                return exc
        return None

    def _current(self):
        now = self._clock.dt
        cur = self._ticks[0]
        for t in self._ticks:
            if t.when(self._day) <= now:
                cur = t
            else:
                break
        if not self.seen or self.seen[-1] is not cur:
            self.seen.append(cur)
        return cur

    # -- market data, resolved per call --------------------------------------
    def ltp(self, symbols):
        exc = self.ltp_raises or self._fault('ltp')
        if exc:
            raise exc
        tick = self._current()
        return {s: {'last_price': tick.spot} for s in _as_list(symbols)}

    def quote(self, symbols):
        exc = self.quote_raises or self._fault('quote')
        if exc:
            raise exc
        tick = self._current()
        legs = {self._trade['long_symbol']: _full(tick.long),
                self._trade['short_symbol']: _full(tick.short)}
        out = {}
        for s in _as_list(symbols):
            sym = s.split(':', 1)[1] if ':' in s else s
            if sym not in legs:
                raise KeyError(s)
            b = legs[sym]
            out[s] = {
                'depth': {
                    'buy': [{'price': b['bid'], 'quantity': b['bid_qty']}] if b['bid'] else [],
                    'sell': [{'price': b['ask'], 'quantity': b['ask_qty']}] if b['ask'] else [],
                },
                'last_price': b['ltp'],
                'ohlc': {'close': b['prev_close']},
                'last_trade_time': self._clock.dt,
            }
        return out


def _as_list(symbols):
    return [symbols] if isinstance(symbols, str) else list(symbols)


def run_session(monkeypatch, sm, trade, ticks, day, positions,
                dry_run=False, fill_policy=None, faults=None):
    """Replay `ticks` through the real `monitor_all`. Returns the evidence.

    Only the boundary is stubbed: stores, Telegram, the watchlist alert check,
    and the log file destination. `is_market_open` is NOT stubbed — the clock
    is walked past 15:30 when the recording ends, so the loop leaves through
    its own market-close branch.
    """
    clock = ReplayClock(day, at='%02d:%02d:%02d' % sorted(ticks, key=lambda t: t.at)[0].at)
    clock.install(monkeypatch, sm)
    last = sorted(ticks, key=lambda t: t.at)[-1]
    # Margin, not +1s: the loop only OBSERVES a tick when a poll lands at or
    # after its timestamp, so ending the recording the instant the last tick
    # begins replays every tick but the last one. Several poll intervals of
    # slack means the final book is actually looked at.
    clock.stop_at = last.when(day) + timedelta(seconds=30)

    store = MemoryStore(trades=[trade])
    empty = MemoryStore()
    monkeypatch.setattr(sm, 'get_store', lambda: store)
    monkeypatch.setattr(sm, 'get_bps_store', lambda: empty)
    monkeypatch.setattr(sm, 'get_fh_store', lambda: MemoryStore())
    monkeypatch.setattr(sm, 'get_watchlist_store', lambda: MemoryStore())
    monkeypatch.setattr(sm, 'check_watchlist_alerts',
                        lambda *a, **k: None)
    monkeypatch.setattr(sm, 'set_log_file', lambda p: None)
    spy = TelegramSpy().install(monkeypatch, sm)

    kite = TickBroker(clock, day, ticks, trade, positions=positions,
                      fill_policy=fill_policy, faults=faults)

    sm.reset_poll_state()      # trail_state is a monitor_all local, not global
    sm.monitor_all(kite, dry_run=dry_run)
    return clock, kite, store, spy
