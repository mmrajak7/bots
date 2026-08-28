"""H2 — the dry-run week must not Telegram a fabricated loss.

`wait_for_fill`'s dry stub returns `average_price: 0.0`. That is CORRECT —
no order was placed, so there is no fill to report — but every close path
then does arithmetic on it:

    exit_net      = long_fill - short_fill   = 0.0
    pnl_per_share = exit_net - entry_net     = -entry_net
    total_pnl     = -entry_net * quantity    = the MAXIMUM LOSS

So every dry-run cohort trigger sent "BCS CLOSED ... P&L Rs -(full debit)",
arriving next to zebra's real paper booking of the same position — including
on the four take-profit WINNERS. The arming gate is read off exactly this
evidence, and the fabrication points the wrong way: it looks like the
strategy failing.

The FH twin is the same defect in the flattering direction: with all four
fills at 0.0 the close cost is 0, so it reports the FULL credit banked.

What is fixed is the NUMBER, not the alert. "The monitor would have closed
here, for this reason, at this spot" is the entire point of running the week.

Run:  cd Helper && python -m pytest bcs/tests/test_dry_run_close_alert.py -v
"""
import re
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy)

LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY, DEBIT = 700, 13.55

BOOKS = {
    f'NFO:{LONG}':  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    f'NFO:{SHORT}': {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}

#: What the fabricated arithmetic produces: -13.55 * 700.
FABRICATED = -DEBIT * QTY


def _trade():
    return {'id': 1, 'stock': 'TESTCO', 'status': 'open',
            'long_symbol': LONG, 'short_symbol': SHORT, 'quantity': QTY,
            'exchange': 'NFO', 'net_debit': DEBIT, 'spot_symbol': 'NSE:TESTCO'}


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_trade()])


def _close(store, dry_run):
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    return sm._close_spread_inner(kite, store, _trade(), spot=1400.0,
                                  reason='TP', dry_run=dry_run, label='BCS')


def _closed_alert(spy):
    msgs = [m for m in spy.sent if 'CLOSE' in m and 'TRIGGERED' not in m]
    assert len(msgs) == 1, f'expected one close alert, got {msgs}'
    return msgs[0]


# ── the bug, end to end ─────────────────────────────────────────────────────

def test_a_dry_run_close_reports_no_pnl_figure(env):
    """The one assertion this whole file exists for."""
    spy, store = env
    assert _close(store, dry_run=True) is True
    msg = _closed_alert(spy)
    assert 'P&L: Rs' not in msg
    assert f'{FABRICATED:+,.0f}' not in msg, (
        'the alert still carries the fabricated maximum loss')
    assert not re.search(r'Rs\s*[-+]?[\d,]+', msg), (
        f'a rupee figure survived in a dry-run close alert: {msg!r}')


def test_a_dry_run_close_says_it_is_a_dry_run(env):
    """It arrives beside zebra's REAL paper booking of the same position. If
    the two cannot be told apart in the phone's notification list, the
    evidence week produces a mixed record nobody can unpick afterwards."""
    spy, store = env
    _close(store, dry_run=True)
    msg = _closed_alert(spy)
    # 2026-08-28: the head gained a leading marker emoji, so the check is
    # "the first LINE says dry run" rather than "the first CHARACTERS do".
    assert '[DRY RUN]' in msg.splitlines()[0]
    assert 'WOULD CLOSE' in msg


def test_the_alert_is_not_suppressed(env):
    """Knowing that the monitor WOULD have closed, and why, is the point of
    running the week at all. Silence would be a different kind of wrong."""
    spy, store = env
    _close(store, dry_run=True)
    msg = _closed_alert(spy)
    assert 'TESTCO' in msg
    assert 'Reason: TP' in msg
    assert 'Spot: 1400' in msg


def test_the_exit_value_is_not_reported_either(env):
    """`exit_net` is 0.00 from the same stub. Printing "Exit spread: 0.00"
    beside a real entry debit is the same fabrication one line up."""
    spy, store = env
    _close(store, dry_run=True)
    msg = _closed_alert(spy)
    assert 'Exit spread: 0.00' not in msg
    # Wording changed 2026-08-28 ("not priced (no fill)" read as a
    # malfunction on a phone); the contract — say WHY there is no number —
    # did not.
    assert 'no order placed' in msg


def test_a_live_close_still_reports_everything(env):
    """Negative control. Without it, every test above passes just as well
    against a close alert that has been gutted for all callers."""
    spy, store = env
    assert _close(store, dry_run=False) is True
    msg = _closed_alert(spy)
    assert msg.startswith('BCS CLOSED: TESTCO')
    assert 'P&L: Rs' in msg
    assert 'DRY RUN' not in msg


# ── the FH twin, which fabricates in the flattering direction ───────────────

FH_LEGS = {'short_call_symbol': 'TESTCO26SEP3000CE',
           'short_put_symbol': 'TESTCO26SEP2600PE',
           'long_put_symbol': 'TESTCO26SEP2550PE'}
FH_BOOKS = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.4, 'bid_qty': 800,
                         'ask_qty': 800, 'ltp': 10.2, 'prev_close': 10.0}
            for s in FH_LEGS.values()}


def _fh_trade():
    t = {'id': 1, 'stock': 'TESTCO', 'status': 'open', 'quantity': 400,
         'exchange': 'NFO', 'total_credit': 97.75, 'spot_symbol': 'NSE:TESTCO',
         'long_call_symbol': None}
    t.update(FH_LEGS)
    return t


def _fh_close(store, dry_run):
    kite = FakeBroker(books=FH_BOOKS, positions=[
        {'tradingsymbol': FH_LEGS['short_call_symbol'], 'quantity': -400},
        {'tradingsymbol': FH_LEGS['short_put_symbol'], 'quantity': -400},
        {'tradingsymbol': FH_LEGS['long_put_symbol'], 'quantity': 400}])
    return sm._close_fh_inner(kite, store, _fh_trade(), spot=2700.0,
                              reason='SL_SPOT', dry_run=dry_run)


def test_the_fh_twin_is_fixed_too(env):
    """`feedback_copy_pasted_modules_fix_once`: fixing the copy you happened
    to open first has shipped an untested FH twin twice running here. With
    every fill at 0.0 the close cost is 0, so the fabrication reads as the
    full credit BANKED — wrong in the direction nobody questions."""
    spy, store = env
    assert _fh_close(store, dry_run=True) is True
    msg = _closed_alert(spy)
    assert '[DRY RUN] FH WOULD CLOSE' in msg.splitlines()[0]
    assert 'P&L: Rs' not in msg
    assert '+39,100' not in msg          # 97.75 * 400, the flattering fiction
    assert 'Close cost: 0.00' not in msg


def test_a_dry_run_close_of_a_PAPER_record_is_not_telegrammed_at_all(env):
    """The 2026-08-28 scope change. A real record in dry run still alerts (the
    tests above) because nothing else books it; a PAPER record does not,
    because zebra sends the message that carries the real P&L a few minutes
    later and two messages for one position is the confusion the owner asked
    to lose. The LOG still has everything."""
    spy, store = env
    paper = _trade()
    paper['paper'] = True
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    sm._close_spread_inner(kite, store, paper, spot=1400.0, reason='TP',
                           dry_run=True, label='BCS')
    assert not [m for m in spy.sent if 'CLOSE' in m], (
        'a paper rehearsal reached Telegram: %r' % spy.sent)
    withheld = [m for _c, m in spy.suppressed if 'WOULD CLOSE' in m
                or 'SHADOW CLOSE' in m]
    assert withheld, 'the close alert was never even offered to the policy'
    assert 'zebra (paper)' in withheld[0], (
        'the withheld message must still name the booking engine, because it '
        'is what the log now carries')


def test_a_live_fh_close_still_reports_everything(env):
    spy, store = env
    _fh_close(store, dry_run=False)
    msg = _closed_alert(spy)
    assert msg.startswith('FH CLOSED: TESTCO')
    assert 'P&L: Rs' in msg


# ── one definition, both books ──────────────────────────────────────────────

def test_both_close_paths_render_through_one_formatter():
    """Pinned on the SOURCE. Two hand-rolled f-strings is how this defect got
    two copies in the first place, and a fix applied to one of them is the
    single most repeated bug shape in this codebase."""
    import inspect
    src = inspect.getsource(sm)
    assert src.count('close_alert_text(') == 3, (
        'expected one definition and exactly two call sites (BCS + FH)')
    for dead in ('f"{label} CLOSED: {stock}\\n"', 'f"FH CLOSED: {stock}\\n"'):
        assert dead not in src, f'a hand-rolled close alert came back: {dead}'


@pytest.mark.parametrize('dry', [True, False])
def test_the_formatter_keeps_the_reason_and_the_spot_either_way(dry):
    msg = sm.close_alert_text('BCS', 'TESTCO', 'SL_SPREAD', 1400.0,
                              ['Entry: 13.55'], -9485.0, dry)
    assert 'Reason: SL_SPREAD' in msg
    assert 'Spot: 1400' in msg
    assert ('P&L: Rs -9,485' in msg) is (not dry)


def test_the_dry_stub_that_causes_this_is_still_what_it_was():
    """The fix is at the RENDERING, not at the stub — a stub inventing a
    plausible fill would be worse. If this ever changes, the reason the
    formatter suppresses the number changes with it."""
    fill = sm.wait_for_fill(kite=None, order_id='x', dry_run=True)
    assert fill['average_price'] == 0.0
    assert fill['filled_quantity'] == 0
    assert fill['status'] == 'COMPLETE'
