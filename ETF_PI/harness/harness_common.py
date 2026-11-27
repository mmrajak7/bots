"""Shared plumbing for the NIFTY record/replay harness (spec §12, step 0).

The harness proves that a refactored option_buy.py makes the SAME decisions as the baseline
(sha256 2b897a4c...) on real market data. It never edits option_buy.py: it drives an unmodified
copy inside a sandbox directory and controls every input that is not a file in that directory:

  * Kite      -> RecordingKite (record mode) / ReplayKite (replay mode)
  * clock     -> the `datetime` module attribute of option_buy / trade_observer / turbulence is a
                 shim whose datetime.now() returns the run's frozen start time. datetime.datetime
                 is a C type, so `now` cannot be patched on it — the module attribute is swapped
                 at import time instead (pandas/kiteconnect keep the real module).
  * sleep     -> no-op in replay (rate-limit sleeps only; no logic depends on them)
  * Telegram  -> common.sendMsgTelegram captured to <sandbox>/telegram.jsonl, never sent

WHY freeze `now` at run start in BOTH modes: the recorder's own decisions then use exactly the
clock a replay will use, so replay-old must reproduce the recording byte-for-byte (self-check).
A wall-clock recorder would differ from any replay in every timestamp it writes.
"""
import datetime as _real_dt
import json
import os
import pickle
import sys
import types

PICKLE_PROTOCOL = 4          # readable by the Pi's 3.11 and Windows' 3.8
# Copied when present in the source tree (underlyings.py exists only from step 1 onwards).
BOT_MODULES = ['option_buy.py', 'trade_observer.py', 'turbulence.py', 'indicators.py', 'common.py',
               'underlyings.py', 'market_calendar.py', 'event_calendar.py',
               # config read by the modules above: shipped with the code, so taken from the
               # source tree being tested (not from the recording)
               'market_holidays.yaml', 'events.yaml', 'events_override.yaml']
BOT_DATA = ['config_trade.yaml', 'secret.json']
# Files the bot reads and writes inside its own directory (state carried between runs).
STATE_FILES = ['st_opt_state.json', 'st_opt_positions.json']
STATE_GLOBS = ['st_opt_levels_*.json']
LOG_STATE_FILES = ['turbulence_state.json']           # under <sandbox>/logs


def make_datetime_shim(frozen_aware):
    """Return a module object that is `datetime` except datetime.now() is frozen.

    `frozen_aware` must be timezone-aware (IST). now(tz) converts it; now() with no tz returns
    the naive local wall time, matching the real behaviour on the Pi and Windows (both IST).
    """
    real = _real_dt.datetime

    class FrozenDateTime(real):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return real(*frozen_aware.timetuple()[:6], frozen_aware.microsecond)
            return frozen_aware.astimezone(tz)

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            return frozen_aware.astimezone(_real_dt.timezone.utc).replace(tzinfo=None)

    shim = types.ModuleType('datetime')
    for name in dir(_real_dt):
        if not name.startswith('__'):
            setattr(shim, name, getattr(_real_dt, name))
    shim.datetime = FrozenDateTime
    return shim


class TelegramSink:
    """Replaces common.sendMsgTelegram; appends every body to <sandbox>/telegram.jsonl."""
    def __init__(self, path, run_id):
        self.path, self.run_id = path, run_id

    def __call__(self, message, type=None, *a, **k):
        with open(self.path, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'run': self.run_id, 'type': type, 'body': message}) + '\n')
        return True


def import_bot(sandbox, frozen_aware, kite, telegram_sink, sleep_noop):
    """Import the bot from `sandbox` with the clock frozen and all externals replaced.

    Must run in a FRESH interpreter (one per simulated cron minute): option_buy keeps module
    state (_KITE, _INSTRUMENTS, TF_TIME_SL, logging handlers) exactly as a cron process would.
    """
    sys.path.insert(0, sandbox)
    # Real modules first: pandas/numpy/kiteconnect/pytz must bind the REAL datetime.
    import pandas, numpy, pytz, yaml, kiteconnect  # noqa: F401
    import common
    import indicators  # noqa: F401
    common.sendMsgTelegram = telegram_sink
    # Modules that take times as ARGUMENTS (and may isinstance-check them) must bind the REAL
    # datetime: only the modules that read the clock themselves get the frozen shim. Absent in
    # the baseline tree, hence optional.
    for mod in ('underlyings', 'market_calendar', 'event_calendar'):
        if os.path.exists(os.path.join(sandbox, mod + '.py')):
            __import__(mod)
    if sleep_noop:
        import time
        time.sleep = lambda *_a, **_k: None
    shim = make_datetime_shim(frozen_aware)
    real = sys.modules['datetime']
    sys.modules['datetime'] = shim
    try:
        import trade_observer, turbulence  # noqa: F401
        import option_buy as ob
    finally:
        sys.modules['datetime'] = real
    ob.get_kite = lambda: kite
    ob._KITE = kite
    return ob


def run_main(ob):
    """Run option_buy.main() as cron would; SystemExit (weekend/off-hours/lock) is normal."""
    try:
        ob.main()
    except SystemExit:
        pass
    finally:
        try:
            ob.release_lock()
        except Exception:
            pass


def save_pickle(obj, path):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(obj, f, protocol=PICKLE_PROTOCOL)
    os.replace(tmp, path)


def load_pickle(path):
    with open(path, 'rb') as f:
        return pickle.load(f)


def norm_args(method, args, kwargs):
    """Canonical, hashable form of a Kite call for matching replay requests to recordings.

    ltp and quote are ONE family: the recorder answers ltp() from a quote() call, so a refactor
    that switches the scan from ltp to quote (spec H5) still matches the same recorded response.
    """
    fam = 'quote' if method in ('ltp', 'quote') else method
    if fam == 'quote':
        keys = args[0] if args else kwargs.get('instruments', [])
        if isinstance(keys, str):
            keys = [keys]
        return (fam, tuple(sorted(keys)))
    return (fam, repr(args), repr(sorted(kwargs.items())))


def ltp_view(quote_resp):
    """Shape a quote() response like kite.ltp() returns it."""
    return {k: {'instrument_token': v.get('instrument_token'), 'last_price': v.get('last_price')}
            for k, v in (quote_resp or {}).items()}
