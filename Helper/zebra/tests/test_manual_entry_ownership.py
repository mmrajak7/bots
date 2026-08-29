"""Who owns a HAND-ENTERED trade — the defect that would have bitten first.

Found by the 2026-08-29 arming review. `add_signal` stamps every signal
`paper: True`; `mark_entered` (the CLI's `zebra enter`, and the documented way
to capture a manual fill) never changed it; only `mark_entered_bcs` did, and
only from `_auto_enter_bcs`.

The arming order puts `auto_entry: true` LAST, after an independent review. So
for the whole first phase of live trading — "the alert IS the order ticket;
entry is manual" — every hand-entered real trade would have been recorded
`paper: True`. Then:

  * `is_paper_record` says paper, so `_exits_external` keeps the position with
    the PAPER engine and `_paper_auto_close` books its exit at mid;
  * `close_spread` correctly refuses it, so the armed monitor will not manage
    it either;
  * the startup broker-leg check SKIPS it ("PAPER record, no broker legs
    expected") — the one sweep that could notice the contradiction is disabled
    by the same flag that causes it.

On the first trigger the record leaves `entered` on a paper booking and the
REAL legs stay open at the broker, with no engine, in no open book, and no
alert that says so. C5 fixed exactly this on the automated door and not on this
one: `feedback_the_copy_you_did_not_open`, on the paper/real boundary itself.

The bitter part: `zebra enter` already READ the broker and printed "VERIFIED:
the broker shows both legs at the recorded size". It observed that the record
was real and still filed it as paper. This makes that observation the decision.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_manual_entry_ownership.py -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zebra import __main__ as zmain           # noqa: E402

L, S = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
ENTRY = {'long_symbol': L, 'short_symbol': S}


class _Args:
    live = False
    paper = False


def _pos(sym, qty=700):
    return {'tradingsymbol': sym, 'quantity': qty}


def _store(tmp_path, monkeypatch, name):
    from zebra import config as zcfg
    from zebra.trade_store import ZebraStore

    d = tmp_path / name
    d.mkdir()
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')
    (d / 'zebra_trades.json').write_text(json.dumps([{
        'id': 1, 'status': 'triggered', 'stock': 'TESTCO', 'direction': 'CE',
        'version': 1, 'paper': True, 'st_value': 1400.0,
        'trigger_spot': 1360.0, 'signal_price': 1360.0,
        'timeframe': 'monthly', 'cohort': zcfg.COHORT_START,
    }]))
    s = ZebraStore(config={'google_drive': {'enabled': False}})
    s.initialize()
    return s


ENTRY_DATA = {
    'long_strike': 1340, 'short_strike': 1390,
    'long_symbol': L, 'short_symbol': S, 'debit': 13.55,
    'lot_size': 700, 'lots': 1, 'expiry': '2026-09-29', 'structure': 'bcs',
}


# ── the broker decides, when it can ─────────────────────────────────────────

def test_both_legs_live_means_the_trade_is_REAL():
    paper, why = zmain._entry_ownership(
        _Args(), ENTRY, [_pos(S, -700), _pos(L, 700)], None)
    assert paper is False
    assert 'both legs' in why


def test_neither_leg_live_means_PAPER():
    paper, why = zmain._entry_ownership(_Args(), ENTRY, [], None)
    assert paper is True
    assert 'neither' in why


def test_a_flat_row_is_not_a_live_leg():
    """A zero-quantity row is the broker saying "closed", not "held"."""
    paper, _ = zmain._entry_ownership(
        _Args(), ENTRY, [_pos(S, 0), _pos(L, 0)], None)
    assert paper is True


def test_a_HALF_filled_spread_refuses_rather_than_guessing():
    """Half a spread is not an ownership answer. Calling it paper strands a
    real leg; calling it real hands the order engine a record whose other leg
    does not exist."""
    paper, why = zmain._entry_ownership(_Args(), ENTRY, [_pos(L, 700)], None)
    assert paper is None
    assert 'half a spread' in why


def test_a_SIZE_mismatch_is_still_REAL():
    """Leg PRESENCE decides ownership, not size — `verify_entry` reports a
    size mismatch separately, and a half-filled real position is still real.
    Answering "paper" because the quantity was short would be the original bug
    wearing a stricter face."""
    paper, _ = zmain._entry_ownership(
        _Args(), ENTRY, [_pos(S, -100), _pos(L, 100)], None)
    assert paper is False


# ── unknown is not a default ────────────────────────────────────────────────

def test_an_unreadable_broker_REFUSES():
    """Guessing 'real' hands the live order path a record with no legs;
    guessing 'paper' is the defect itself. This one bit decides which engine
    manages the trade for the rest of its life, so it is not defaulted."""
    paper, why = zmain._entry_ownership(_Args(), ENTRY, None, 'timeout')
    assert paper is None
    assert 'broker could not be read' in why


def test_unknown_symbols_refuse():
    paper, _ = zmain._entry_ownership(_Args(), {}, [], None)
    assert paper is None


# ── the operator can always override ────────────────────────────────────────

def test_live_flag_wins_over_a_broker_that_says_otherwise():
    """There are real cases the broker cannot settle: a fill placed in another
    account, or a paper entry recorded while real positions in the same symbol
    are open for a different reason."""
    a = _Args()
    a.live = True
    paper, why = zmain._entry_ownership(a, ENTRY, [], None)
    assert paper is False and 'you said' in why


def test_paper_flag_wins_too():
    a = _Args()
    a.paper = True
    paper, _ = zmain._entry_ownership(
        a, ENTRY, [_pos(S, -700), _pos(L, 700)], None)
    assert paper is True


def test_both_flags_is_a_refusal_not_a_precedence_rule():
    a = _Args()
    a.live = a.paper = True
    paper, why = zmain._entry_ownership(a, ENTRY, [], None)
    assert paper is None and 'contradictory' in why


def test_an_unreadable_broker_can_be_overridden_by_hand():
    """The refusal must be escapable, or a broker outage blocks recording a
    trade that has already happened."""
    a = _Args()
    a.live = True
    paper, _ = zmain._entry_ownership(a, ENTRY, None, 'timeout')
    assert paper is False


# ── the store honours it ────────────────────────────────────────────────────

def test_the_store_writes_the_flag_the_caller_decided(tmp_path, monkeypatch):
    from zebra.trade_store import is_paper_record

    store = _store(tmp_path, monkeypatch, 'z1')
    t = store.mark_entered(1, dict(ENTRY_DATA, paper=False))

    assert t['paper'] is False
    assert is_paper_record(t) is False, (
        'the record still reads as paper — the live monitor will decline it '
        'and the paper engine will book its exit at mid')


def test_the_default_is_UNCHANGED_when_the_caller_says_nothing(tmp_path,
                                                               monkeypatch):
    """Callers that do not pass `paper` must not be silently re-owned. Only
    an explicit decision moves the flag."""
    store = _store(tmp_path, monkeypatch, 'z2')
    t = store.mark_entered(1, dict(ENTRY_DATA))
    assert t['paper'] is True


# ── the CLI wiring ──────────────────────────────────────────────────────────

def test_the_cli_decides_ownership_BEFORE_it_writes_the_record():
    """Reading the broker after the write would leave a window where the
    record exists with the wrong owner — and `mark_entered` is what makes it
    visible to both engines."""
    src = Path(zmain.__file__).read_text(encoding='utf-8')
    body = src[src.index('def cmd_enter('):]
    body = body[:body.index('\ndef ', 1)]
    assert body.index('_entry_ownership') < body.index('store.mark_entered')


def test_the_cli_reads_the_broker_ONCE():
    """Two reads could disagree, and then the record's owner and the VERIFIED
    line printed under it would be answering different questions."""
    src = Path(zmain.__file__).read_text(encoding='utf-8')
    body = src[src.index('def cmd_enter('):]
    body = body[:body.index('\ndef ', 1)]
    assert body.count('_broker_positions()') == 1
    assert '_get_kite().positions()' not in body


def test_the_ownership_check_reads_the_RESOLVED_symbols():
    """`zebra enter` recovers the leg symbols from the alert when they are not
    typed out, so `args.long_symbol` is None on the ordinary path. Asking the
    broker about None would refuse every entry that did not spell its symbols
    by hand — caught while writing this, not by a test."""
    src = Path(zmain.__file__).read_text(encoding='utf-8')
    fn = src[src.index('def _entry_ownership('):]
    fn = fn[:fn.index('\ndef ', 1)]
    assert 'entry_data.get(' in fn
    assert 'args.long_symbol' not in fn
