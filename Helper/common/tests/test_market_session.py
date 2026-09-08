"""The cash market stops printing at 15:15, and two guards read that wrongly.

## What was measured

NSE cash-market spot for every open position freezes at 15:15 and does not move
again until one new price at ~15:29-15:31. On the 5-second monitor log:

    2026-08-27  24 identical polls  15:15:05 -> 15:28:04   (12m)
    2026-08-28  25                  15:15:32 -> 15:28:58   (13m)
    2026-09-01  29                  15:15:02 -> 15:29:48   (14m)
    2026-09-02  28                  15:15:18 -> 15:29:33   (14m)
    2026-09-03  26                  15:15:24 -> 15:28:33   (13m)

On the 5-minute path record: identical at 15:15/15:20/15:25 on **125 of 125**
position-sessions, against 0 of 122 at 12:15/12:20/12:25. **And the option
books moved on 125 of 125** — same feed, different segment, so this is a cash
CALL AUCTION and not a broken feed.

## Why it is a bug

`_spot_corroborates`, in BOTH engines, vetoes an exit when structure value
collapses >= 35% while spot moves < 0.4%. In that window spot moves exactly
0.00% BY MARKET DESIGN, so a genuine collapse is refused as "uncorroborated"
for a reason that has nothing to do with the market — while the option book,
which is what collapsed, is still trading. Never fired there yet; the cost when
it lands is holding a collapsing position overnight.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests/test_market_session.py -v
"""
import sys
from datetime import datetime, time as dtime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import market_session as ms      # noqa: E402


def at(h, m, s=0):
    return datetime(2026, 9, 4, h, m, s, tzinfo=ms.IST)


# -- the window --------------------------------------------------------------

@pytest.mark.parametrize('h,m,frozen', [
    (9, 20, False), (12, 0, False), (15, 14, False),
    (15, 15, True), (15, 20, True), (15, 29, True),
    (15, 30, False), (15, 31, False), (16, 0, False),
])
def test_the_window_is_1515_to_the_close(h, m, frozen):
    assert ms.cash_price_is_frozen(at(h, m)) is frozen


def test_1515_exactly_is_INSIDE_the_window():
    """The measured freeze starts at 15:15:02-15:15:32 every session, so the
    15:15 poll is already inside it. An exclusive bound here would leave the
    veto live on the first frozen print of the day."""
    assert ms.cash_price_is_frozen(at(15, 15, 0)) is True
    assert ms.cash_price_is_frozen(at(15, 14, 59)) is False


def test_the_close_itself_is_OUTSIDE_it():
    """By 15:30 the question has become "is anything executable", which
    `_exits_executable` answers. Two guards claiming the same minute for
    different reasons is how a log stops being readable."""
    assert ms.cash_price_is_frozen(at(15, 30, 0)) is False


# -- it must not drift from the engines' own close ---------------------------

def test_it_agrees_with_zebras_market_close():
    from zebra import config as cfg
    ms.assert_agrees_with(cfg.MARKET_CLOSE)


def test_it_agrees_with_the_live_monitors_market_close():
    """A third copy of the close that silently drifted would make this module
    wrong in the direction nobody checks — reporting a frozen price as live.

    Read from SOURCE because `bcs.spread_monitor` cannot be imported without a
    Kite client, and the value is a module-level constant evaluated at import,
    so there is no input that distinguishes the two readings.

    RETIRES WHEN: both engines take their session times from one module (this
    one, or `common.layered_config`), at which point there is no second copy
    to drift and the assertion is a tautology.
    """
    import re
    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    m = re.search(r'^MARKET_CLOSE = dtime\((\d+), (\d+)\)', src, re.M)
    assert m, 'the live monitor no longer declares MARKET_CLOSE where expected'
    ms.assert_agrees_with((int(m.group(1)), int(m.group(2))))


def test_a_disagreeing_close_RAISES_rather_than_being_absorbed():
    """The negative control. Without it `assert_agrees_with` could be a no-op
    and both tests above would pass against a module that checks nothing."""
    with pytest.raises(ValueError, match='drifted apart'):
        ms.assert_agrees_with(dtime(15, 45))


# -- the note ----------------------------------------------------------------

def test_the_note_is_empty_outside_the_window():
    assert ms.spot_staleness_note(at(12, 0)) == ''


def test_the_note_names_the_auction_and_the_time_it_started():
    note = ms.spot_staleness_note(at(15, 20))
    assert 'STALE' in note and 'auction' in note and '15:15' in note


# -- the guard that has to change --------------------------------------------

def test_the_spot_veto_stands_down_in_the_auction_window(monkeypatch):
    """THE POINT OF THE MODULE. A 50% value collapse on a 0.00% spot move is
    the NHPC signature outside the window and a market-structure artefact
    inside it — spot CANNOT move there, so its stillness says nothing."""
    from zebra import monitor
    from zebra import config as cfg

    class _Store:
        def corroboration_ref(self, tid):
            return {'spot': 100.0, 'value': 10.0, 't': 1_000_000.0}

    trade = {'id': 1, 'stock': 'TESTCO', 'direction': 'CE'}
    # Same inputs, both sides of the boundary: value halved, spot unmoved.
    monkeypatch.setattr(cfg, 'SPOT_VETO_ENABLED', True)

    monkeypatch.setattr(ms, 'cash_price_is_frozen', lambda now=None: False)
    ok, reason, _ = monitor._spot_corroborates(
        _Store(), trade, 100.0, 5.0, True, now=1_000_060.0)
    assert ok is False and 'uncorroborated' in reason, (
        'the veto no longer fires outside the auction — this test would then '
        'prove nothing about the case inside it')

    monkeypatch.setattr(ms, 'cash_price_is_frozen', lambda now=None: True)
    ok, reason, patch = monitor._spot_corroborates(
        _Store(), trade, 100.0, 5.0, True, now=1_000_060.0)
    assert ok is True and reason == '', (
        'a genuine collapse in the last fifteen minutes is being refused '
        'because the cash market cannot print')
    assert patch is None, (
        'the corroboration reference advanced on a frozen print — the next '
        'session would then be judged against a price from inside an auction')


def test_the_live_engines_veto_stands_down_too(monkeypatch):
    """It is a COPY of the zebra one and places REAL orders. The pair have to
    move together (`feedback_copy_pasted_modules_fix_once`)."""
    from bcs import spread_monitor as sm

    state = {'spot': 100.0, 'spread': 10.0, 't': 1_000_000.0}
    monkeypatch.setattr(sm.market_session, 'cash_price_is_frozen',
                        lambda now=None: False)
    ok, reason = sm.spot_corroborates(dict(state), 100.0, 5.0,
                                      now=1_000_060.0)
    assert ok is False and 'uncorroborated' in reason

    monkeypatch.setattr(sm.market_session, 'cash_price_is_frozen',
                        lambda now=None: True)
    ok, reason = sm.spot_corroborates(dict(state), 100.0, 5.0,
                                      now=1_000_060.0)
    assert ok is True and reason == ''


def test_both_engines_consult_the_SAME_window():
    """Not two copies of 15:15. The whole reason this lives in `common/`."""
    from zebra import monitor
    from bcs import spread_monitor as sm
    assert monitor.market_session is sm.market_session is ms


# -- the declaration checks itself -------------------------------------------

#: Matches the pinned date in `at()` above — the clock is never the wall's.
DAY = '2026-09-04'


def _ref(spot, day=DAY, t='15:15:20'):
    return {'spot': spot, 'day': day, 't': t}


def _verdict(ref, spot, when):
    """`auction_watch` returns (looks_wrong, patch, compared_to)."""
    return ms.auction_watch(ref, spot, when)


def test_the_first_reading_inside_the_window_becomes_the_reference():
    """It cannot be a finding: there is nothing yet to have moved away from.

    This is the whole fix. The first cut compared against `corrob_spot`, which
    stops advancing at 15:15 and therefore holds the last PRE-auction price —
    so it reported the ordinary 15:10->15:15 move as drift. Measured on
    2026-09-07: spot differed between those two polls on 8 of 8 open positions,
    so it fired on every position, three times each, in a session where the
    window behaved perfectly.
    """
    wrong, patch, compared = _verdict(None, 100.0, at(15, 15))
    assert wrong is False and compared is None
    assert patch['spot'] == 100.0 and patch['day'] == DAY
    assert patch['t'] == '15:15:00', 'the reference must record WHEN it was taken'


def test_a_moving_price_inside_the_window_is_reported_as_drift():
    """The window is a DECLARATION, and a declaration nothing checks is the
    shape this codebase keeps paying for. If NSE moves the auction, every guard
    keyed to 15:15 silently starts answering the wrong question."""
    wrong, patch, compared = _verdict(_ref(100.0), 100.5, at(15, 20))
    assert wrong is True
    assert patch is None
    assert compared == 100.0, 'the log line needs the number actually compared'


def test_a_still_price_inside_the_window_is_the_healthy_case():
    assert _verdict(_ref(100.0), 100.0, at(15, 25)) == (False, None, 100.0)


def test_a_pre_window_move_is_NOT_drift():
    """THE REGRESSION. 2026-09-07: every one of 8 positions moved between the
    15:10 and 15:15 polls — normal continuous trading right up to the auction —
    and the old check called all 8 of them a broken window."""
    # 15:10 price 667.65 (SBICARD), frozen 15:15 price 672.00.
    wrong, patch, _ = _verdict(None, 672.00, at(15, 15))
    assert wrong is False, 'the move INTO the freeze is not a move INSIDE it'
    assert patch['spot'] == 672.00
    # ...and the rest of the window is then silent, which is the point.
    for hh, mm in ((15, 20), (15, 25)):
        assert _verdict(patch, 672.00, at(hh, mm))[0] is False


def test_a_reference_from_another_DAY_is_ignored():
    """The old check read `corrob_spot` with no regard for `corrob_t` at all,
    so a position whose book had been unreliable could be judged against a
    price from an earlier session and report a fabricated move of any size."""
    wrong, patch, _ = _verdict(_ref(100.0, '2026-09-01'), 140.0, at(15, 20))
    assert wrong is False
    assert patch['spot'] == 140.0 and patch['day'] == DAY


def test_it_says_nothing_outside_the_window():
    """Spot moving at 12:00 is the market working."""
    assert _verdict(_ref(100.0), 100.5, at(12, 0)) == (False, None, None)


def test_it_takes_no_reference_outside_the_window():
    """A reference stamped at 12:00 would be a pre-auction price wearing the
    day stamp that makes it look in-window — the original bug, re-imported."""
    assert _verdict(None, 100.0, at(14, 0)) == (False, None, None)


def test_missing_or_garbage_prices_are_not_drift():
    """It runs once per open position per poll, in the exit path. A detector
    that can throw is a new way to fail an exit."""
    for ref, cur in ((None, None), (_ref(100.0), None), (_ref(100.0), 'x'),
                     (_ref(100.0), 0), (_ref(100.0), -1),
                     (_ref(100.0), float('nan')), (_ref(100.0), float('inf')),
                     ({'spot': 'x', 'day': DAY}, 100.0),
                     ({'spot': float('nan'), 'day': DAY}, 100.0),
                     ({'day': DAY}, 100.0), ('not a dict', 100.0)):
        wrong = _verdict(ref, cur, at(15, 20))[0]
        assert wrong is False, (ref, cur)


def test_a_garbage_reference_is_REPLACED_not_merely_ignored():
    """Otherwise the position spends the whole window unable to take a good
    reference, and the check is silently dead for it."""
    _, patch, _c = _verdict({'spot': 'x', 'day': DAY}, 100.0, at(15, 20))
    assert patch['spot'] == 100.0 and patch['day'] == DAY


def test_it_is_not_a_veto_and_not_a_trigger():
    """It returns a fact for a log line. A detector that could halt the engine
    would be a worse bug than the one it watches for."""
    assert _verdict(_ref(100.0), 105.0, at(15, 20))[0] in (True, False)


def test_a_NaN_never_becomes_the_reference():
    """`nan <= 0` is False, so NaN used to be accepted — and `nan != nan` is
    True, so it then reported a finding on every remaining cycle of the day."""
    wrong, patch, _c = _verdict(None, float('nan'), at(15, 15))
    assert (wrong, patch) == (False, None)


def test_the_compared_value_is_returned_as_a_NUMBER():
    """The caller formats it with `%.2f`. A ref holding the STRING '100'
    compares fine through float() and then blew up inside `logging`, which
    swallows the error — so the warning was lost entirely."""
    wrong, _p, compared = _verdict({'spot': '100', 'day': DAY}, 105.0,
                                   at(15, 20))
    assert wrong is True
    assert isinstance(compared, float) and compared == 100.0

def test_the_derivatives_close_is_LATER_than_the_cash_auction():
    """The fact a first cut of this work got wrong. It gated BOOKING at 15:30
    on the theory that nothing fills there — but the option book moved between
    the 15:25 and 15:30 polls on 125 of 125 position-sessions, and F&O trading
    was EXTENDED to 15:40 when CAS launched. Only SPOT dies early.

    A guard that refuses a genuine exit is the expensive direction: it pushes
    the position overnight, which is how this book's worst loss happened."""
    assert ms.FNO_CLOSE > ms.CASH_AUCTION_END
    assert ms.FNO_CLOSE == dtime(15, 40)


def test_nothing_here_gates_BOOKING(monkeypatch):
    """This module answers "can spot print", never "may we trade". The two
    were conflated once and the result blocked exits that would have filled.

    Read from SOURCE because the guard is about what this module must NOT grow
    into; there is no input that distinguishes "has no trading gate" from "has
    one nobody called today".

    RETIRES WHEN: a real session model exists that owns "may we trade" — one
    that knows F&O runs to FNO_CLOSE — at which point the separation is
    structural rather than a naming rule this test enforces.
    """
    import inspect
    src = inspect.getsource(ms)
    for word in ('book', 'exit_', 'executable'):
        assert 'def %s' % word not in src, (
            'market_session grew a trading gate; it is a statement about the '
            'CASH FEED and must stay one')


def test_the_engines_no_longer_carry_a_close_booking_gate():
    """`_exits_executable` was removed after the premise was refuted. A dropped
    guard that stays callable keeps deciding
    (`feedback_dropped_but_still_wired`).

    RETIRES WHEN: the engines run to FNO_CLOSE and the session model is taken
    from this module, at which point "may we trade" has a real home and this
    assertion is about the wrong thing.
    """
    from zebra import monitor
    assert not hasattr(monitor, '_exits_executable')
