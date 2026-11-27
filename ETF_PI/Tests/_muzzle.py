"""Neutralise every live side effect of importing/driving option_buy from a test.

WHY THIS EXISTS (2026-09-01). Two incidents in one session, same root cause — a test that
drives production code inherits production's side effects, and the ones that matter are the
ones that leave the process:

  1. `monitor_positions()` sends Telegram from INSIDE the exit ladder, not only from
     `_close_pos`. Stubbing the close path let a stream of synthetic
     "T1 HIT | CE 24050 15M" alerts into the user's LIVE channel, indistinguishable from
     real signals.
  2. `option_buy` attaches a FileHandler to `logs/option_buy_<today>.log` at import, so the
     same tests appended 1,107 synthetic trade lines into a LIVE session's log — the file
     `digest.py`, `analyze_trade_obs.py` and every performance review read.

Import this module FIRST in any test that touches option_buy:

    sys.path.insert(0, <repo root>)
    import option_buy as ob
    from Tests import _muzzle          # or: import _muzzle
    _muzzle.assert_clean()             # optional, asserts nothing leaked

Muzzling at the MODULE boundary (not per call site) is deliberate: a notification or log
call added to the ladder later is covered automatically, with no test change.
"""
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import option_buy as ob  # noqa: E402

#: Every Telegram body a muzzled test tried to send.
SENT = []
#: Every log record a muzzled test emitted, kept in memory instead of on disk.
LOGGED = []


class _Capture(logging.Handler):
    def emit(self, record):
        try:
            LOGGED.append(record.getMessage())
        except Exception:
            pass


def _install():
    ob.common.sendMsgTelegram = lambda *a, **k: SENT.append(a[0] if a else '')
    # Detach the file handler from every logger option_buy may have configured, then keep
    # the records in memory. propagate=False stops them reaching a root handler that some
    # other module may have pointed at a file.
    for lg in {ob.log, logging.getLogger('option_buy')}:
        for h in list(getattr(lg, 'handlers', [])):
            lg.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        lg.propagate = False
        lg.addHandler(_Capture())


#: Markers that can ONLY come from a test. Deliberately narrow: these are position keys no
#: live scan can produce (real keys are NIFTY_<CE|PE>_<tf>_<yyyymmdd>). Do NOT add a strike
#: number here — 24500 was a genuine strike on 2026-07-17 and matching on it flags real
#: history as pollution. Synthetic CONFIRM lines are prevented structurally by _install()
#: detaching the FileHandler, so they need no marker.
_TEST_MARKERS = ('_TEST', 'REPLAY')


def live_log_leaks():
    """Count lines in the real logs that could only have come from a test."""
    import glob
    n = 0
    for f in glob.glob(os.path.join(_ROOT, 'logs', 'option_buy_*.log')):
        try:
            with open(f, encoding='utf-8', errors='replace') as fh:
                n += sum(1 for line in fh if any(m in line for m in _TEST_MARKERS))
        except OSError:
            pass
    return n


def assert_clean():
    """Raise if the muzzle is not actually installed, or if anything has leaked.

    Called from the test bodies so a future refactor that reorders imports fails loudly in
    CI rather than silently in the user's Telegram or in a live log file.
    """
    if getattr(ob.common.sendMsgTelegram, '__name__', '') != '<lambda>':
        raise AssertionError("Telegram is NOT muzzled — a test would message the live channel")
    for lg in {ob.log, logging.getLogger('option_buy')}:
        for h in getattr(lg, 'handlers', []):
            if isinstance(h, logging.FileHandler):
                raise AssertionError("option_buy still has a FileHandler — a test would "
                                     "write into the live log at %r" % h.baseFilename)
    n = live_log_leaks()
    if n:
        raise AssertionError("%d test line(s) have leaked into a live option_buy log" % n)
    return True


_install()
