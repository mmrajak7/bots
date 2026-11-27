"""turbulence.py — B6: deterministic abrupt-move tripwire.

WHAT THIS IS FOR
----------------
"On some days like election, war, etc options can swing wild — those days, stop trading
or manage open positions." That request splits in two, and this module is the half that
can be built honestly today:

  * SCHEDULED events (RBI policy, budget, election counting, FOMC) are a CALENDAR
    problem. Not this module.
  * UNSCHEDULED violence (a war headline, a flash crash, a surprise policy) cannot be
    pre-listed. It has to be recognised from the tape. That is this module.

WHY DETERMINISTIC AND NOT AN LLM
--------------------------------
An abrupt move is decided in seconds. A `claude -p` run takes minutes, so an agent asked
"is this a wild day?" arrives after the damage every time. Worse, spawning one every scan
to answer "no" on the ~98% of days that are ordinary is pure cost. So the tripwire is
arithmetic, and it runs on every tick for free. An agent's proper job is the daily review
of what the tripwire said — not the call itself.

THE THRESHOLDS BELOW ARE GUESSES. THEY ARE NOT MEASURED.
--------------------------------------------------------
Every other filter in this system — NO_ENTRY_WINDOWS, MIN_T1_DISTANCE, the 30M removal —
was priced on the ledger at several thresholds before it was allowed to gate anything.
This one has not been, because doing so needs daily NIFTY and India VIX history that was
not reachable when it was written (the Kite token had expired).

So `BLOCK_ENTRIES` is **False** and this module gates NOTHING. It records what it would
have done and alerts a human. Arming it before the measurement would risk exactly the
failure the design warned about: the ledger's best single day was +Rs7,605 and its worst
was -Rs4,313, so a filter that wrongly closes the desk can cost more than the worst day
ever lost. Run `analyze_turbulence.py --price` once market history is available, then
decide.

WHAT ESCALATES, AND WHAT DELIBERATELY DOES NOT
----------------------------------------------
VIX level, VIX jump and the overnight gap can raise the verdict. Intraday RANGE cannot,
and that is on purpose: range grows monotonically through the session, so any fixed range
threshold trips on ordinary afternoons and would read as "turbulent" most days by 14:00.
Judging it needs a time-of-day-normalised baseline (or a trailing ATR) that does not
exist yet. It is recorded so the measurement can build one; it never votes.
"""
import datetime
import json
import os

import pytz

IST = pytz.timezone('Asia/Kolkata')
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))

#: MASTER SWITCH FOR GATING. False = record and alert only, block nothing.
#: Do not set True until the thresholds below have been priced on the ledger.
BLOCK_ENTRIES = False

#: Verdicts, worst last. `_WORSE` ranks them.
CALM, ELEVATED, TURBULENT = 'CALM', 'ELEVATED', 'TURBULENT'
_RANK = {CALM: 0, ELEVATED: 1, TURBULENT: 2}

# --- UNMEASURED THRESHOLDS (see module docstring) --------------------------
#: India VIX absolute level. NIFTY VIX sits ~11-15 in calm regimes; high-teens is a
#: nervous market and 25+ has historically accompanied genuine dislocations.
VIX_ELEVATED = 18.0
VIX_TURBULENT = 25.0

#: India VIX move vs its own previous close, in percent. A spike matters more than a
#: level: a market that was calm yesterday and is not today is the regime break.
VIX_JUMP_ELEVATED = 10.0
VIX_JUMP_TURBULENT = 20.0

#: NIFTY overnight gap, absolute percent of previous close. NIFTY rarely gaps 1%+
#: without a reason, and the reason is usually the sort of event this exists to catch.
GAP_ELEVATED = 1.00
GAP_TURBULENT = 1.75

#: Recorded, never voted on. See the module docstring.
RANGE_NOTE_PCT = 1.50

_STATE_FILE = 'turbulence_state.json'
_LOG_FILE = 'turbulence.jsonl'


def _state_path():
    return os.path.join(CURRENT_DIR, 'logs', _STATE_FILE)


def _log_path():
    return os.path.join(CURRENT_DIR, 'logs', _LOG_FILE)


def _logger():
    import logging
    return logging.getLogger('option_buy')


def _pct(a, b):
    """(a - b) / b * 100, or None when EITHER side is unusable.

    Both operands must be strictly positive. Every quantity compared here is a price or
    an index level, so a zero means "no data", never "worth nothing" — and treating it
    as a real number is not a rounding problem, it is a −100% move.

    THE BUG THIS EXISTS TO PREVENT (found in review, 2026-08-21): `_scan_turbulence`
    runs on every cron tick from 09:00, but the market does not open until 09:15, so
    Kite's `ohlc.open` is 0 until the first print. Guarding only the denominator gave
    gap = (0 − 24480) / 24480 = −100%, whose absolute value clears every threshold —
    so the tripwire latched TURBULENT and fired a Telegram alert EVERY morning before
    the open, and would have closed the desk every day had BLOCK_ENTRIES been on.
    """
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if a <= 0 or b <= 0:
        return None
    return (a - b) / b * 100.0


# ---------------------------------------------------------------------------
# The verdict — a pure function, so it is trivially testable
# ---------------------------------------------------------------------------

def evaluate(vix=None, vix_prev_close=None, spot=None, prev_close=None,
             day_open=None, day_high=None, day_low=None, now=None):
    """Judge the tape. Returns a verdict dict; never raises.

    Every input is optional because every input can fail to arrive. A missing signal is
    recorded as unavailable and simply does not vote — it must never be read as calm.
    `signals_ok` counts how many actually contributed, so a reader can tell "calm" from
    "we could not see anything".
    """
    now = now or datetime.datetime.now(IST)
    reasons = []
    verdict = CALM
    ok = 0

    def escalate(level, why):
        nonlocal verdict
        reasons.append(why)
        if _RANK[level] > _RANK[verdict]:
            verdict = level

    # --- India VIX absolute level
    vix_val = None
    try:
        vix_val = float(vix) if vix is not None else None
    except (TypeError, ValueError):
        vix_val = None
    if vix_val and vix_val > 0:
        ok += 1
        if vix_val >= VIX_TURBULENT:
            escalate(TURBULENT, f"VIX {vix_val:.1f} >= {VIX_TURBULENT}")
        elif vix_val >= VIX_ELEVATED:
            escalate(ELEVATED, f"VIX {vix_val:.1f} >= {VIX_ELEVATED}")

    # --- India VIX jump vs its own previous close
    vix_jump = _pct(vix, vix_prev_close)
    if vix_jump is not None:
        ok += 1
        if vix_jump >= VIX_JUMP_TURBULENT:
            escalate(TURBULENT, f"VIX jumped {vix_jump:+.1f}% vs prev close")
        elif vix_jump >= VIX_JUMP_ELEVATED:
            escalate(ELEVATED, f"VIX jumped {vix_jump:+.1f}% vs prev close")

    # --- NIFTY overnight gap. ABSOLUTE: a gap down and a gap up are both events, and
    # this book trades both CE and PE, so direction does not make one of them benign.
    gap = _pct(day_open, prev_close)
    if gap is not None:
        ok += 1
        if abs(gap) >= GAP_TURBULENT:
            escalate(TURBULENT, f"NIFTY gapped {gap:+.2f}% overnight")
        elif abs(gap) >= GAP_ELEVATED:
            escalate(ELEVATED, f"NIFTY gapped {gap:+.2f}% overnight")

    # --- Intraday range. RECORDED, NEVER VOTES. See module docstring.
    range_pct = None
    try:
        if day_high and day_low and prev_close and float(prev_close) > 0:
            range_pct = (float(day_high) - float(day_low)) / float(prev_close) * 100.0
    except (TypeError, ValueError):
        range_pct = None

    return {
        'ts': now.isoformat(),
        'day': now.strftime('%Y%m%d'),
        'verdict': verdict,
        'reasons': reasons,
        'signals_ok': ok,
        'vix': vix_val,
        'vix_prev_close': vix_prev_close,
        'vix_jump_pct': None if vix_jump is None else round(vix_jump, 2),
        'spot': spot,
        'prev_close': prev_close,
        'day_open': day_open,
        'gap_pct': None if gap is None else round(gap, 3),
        'day_high': day_high,
        'day_low': day_low,
        'range_pct': None if range_pct is None else round(range_pct, 3),
        'range_notable': bool(range_pct is not None and range_pct >= RANGE_NOTE_PCT),
        'block_enabled': BLOCK_ENTRIES,
    }


# ---------------------------------------------------------------------------
# Day state — latch, dedupe, persist
# ---------------------------------------------------------------------------

def _load_state():
    try:
        with open(_state_path(), encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_state(st):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(st, fh)
    os.replace(tmp, path)


def _append_log(rec):
    path = _log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, default=str) + '\n')


def observe(v, notify=None):
    """Persist a verdict, latch the day's worst, and alert once per escalation.

    LATCHING: the day's verdict only ever ratchets UP. A market that spiked at 09:20 and
    looks calm at 09:40 had an event; forgetting it because the average settled is how a
    guard misses the second leg. The latch resets with the date, nothing else.

    Records on CHANGE, not every scan: a line per minute would be ~375/day of which 374
    say the same thing, and the one that matters would be invisible in it.

    `notify` is injected rather than imported so this module has no dependency on the
    Telegram layer and stays testable without one.
    """
    log = _logger()
    st = _load_state()
    day = v.get('day')
    if st.get('day') != day:
        st = {'day': day, 'worst': CALM, 'alerted': [], 'first_seen': v.get('ts')}

    prev_worst = st.get('worst', CALM)
    now_verdict = v.get('verdict', CALM)
    if _RANK.get(now_verdict, 0) > _RANK.get(prev_worst, 0):
        st['worst'] = now_verdict
        st['worst_at'] = v.get('ts')
        st['worst_reasons'] = v.get('reasons', [])

    changed = st.get('worst') != prev_worst
    if changed or not st.get('logged_once'):
        st['logged_once'] = True
        try:
            _append_log(dict(v, latched_worst=st.get('worst')))
        except Exception as e:
            log.warning(f"TURBULENCE: could not write record: {e}")

    if changed and st['worst'] != CALM and st['worst'] not in st.get('alerted', []):
        st.setdefault('alerted', []).append(st['worst'])
        why = '; '.join(v.get('reasons', [])) or 'no reason recorded'
        gate = ('ENTRIES BLOCKED for the day' if BLOCK_ENTRIES
                else 'recording only — entries NOT blocked')
        log.warning(f"TURBULENCE {st['worst']}: {why} | {gate}")
        if notify:
            try:
                notify(f"<b>⚠️ TURBULENCE {st['worst']}</b>\n{why}\n<i>{gate}</i>")
            except Exception as e:
                log.error(f"TURBULENCE alert failed: {e}")

    try:
        _save_state(st)
    except Exception as e:
        log.warning(f"TURBULENCE: could not save state: {e}")
    return st


def should_block(now=None):
    """Should new entries be suppressed right now?

    Returns False unless BLOCK_ENTRIES is True AND today has latched TURBULENT. With the
    flag off this is unconditionally False, which is what makes the whole module inert
    while its thresholds remain unmeasured.

    Deliberately reads the LATCH, not a fresh evaluation: once a day has been judged
    turbulent it stays judged, so a momentary lull cannot reopen the desk.
    """
    if not BLOCK_ENTRIES:
        return False
    now = now or datetime.datetime.now(IST)
    st = _load_state()
    if st.get('day') != now.strftime('%Y%m%d'):
        return False
    return st.get('worst') == TURBULENT


def day_summary(now=None):
    """What the tripwire concluded today — for the digest and the morning report."""
    now = now or datetime.datetime.now(IST)
    st = _load_state()
    if st.get('day') != now.strftime('%Y%m%d'):
        return {'day': now.strftime('%Y%m%d'), 'worst': CALM, 'reasons': [],
                'observed': False}
    return {'day': st.get('day'), 'worst': st.get('worst', CALM),
            'reasons': st.get('worst_reasons', []), 'worst_at': st.get('worst_at'),
            'observed': True, 'blocked': should_block(now)}
