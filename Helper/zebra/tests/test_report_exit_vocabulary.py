# -*- coding: utf-8 -*-
"""N3 — the phone summary had its own copy of the exit vocabulary.

`zebra/report.py` interpreted `exit_reason` in three places, and each did the
same thing: `.replace('paper:', '')`, then present whatever was left. That
reads zebra's paper engine correctly and the OTHER engine not at all.

    zebra/monitor.py::_paper_auto_close   ->  'paper:debit_sl'
    bcs/spread_monitor.py                 ->  'SL_SPREAD'        (bridged)
    bcs/spread_monitor.py, recovery path  ->  'ALREADY_FLAT_TP'

So a bridged stop printed `SL_SPREAD` and opened its OWN `by_reason` bucket
beside `debit_sl`: one trigger, two rows, each under-counting the other, on
the summary the owner reads on his phone. Cosmetic in blast radius — this
feeds no gate — but it is the third and fourth copy of the mapping that C0
collapsed into `zebra/outcomes.py`, and the repo's most frequent bug shape is
the copy nobody opened.

What these tests pin:

  1. the CLASSIFICATION is shared — report.py routes through
     `outcomes.classify`, so both engines' names for one trigger land in one
     bucket and print one word;
  2. the WORDING stays local — `already_flat_` survives as a visible
     qualifier (no close machinery ran, the price was recovered rather than
     transacted, and `is_stop_exit` turns on exactly that distinction), and an
     unrecognised string is shown verbatim rather than tidied away;
  3. no fourth copy — there is no `.replace('paper:'` left in the module.

Every reason string below is taken from the writers, not from memory: grep
`_paper_auto_close(` in `zebra/monitor.py` and `close_spread(` /
`ALREADY_FLAT_` in `bcs/spread_monitor.py`.
"""

import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import outcomes                    # noqa: E402
from zebra import report as R                 # noqa: E402


#: (what the monitor writes, what it means). The adapter lowercases on the way
#: into the store, so both cases are exercised below.
BRIDGED = [
    ('SL_SPREAD', outcomes.DEBIT_SL),
    ('SL_SPOT', outcomes.SPOT_SL),
    ('SL_TRAIL', outcomes.TRAIL),
    ('EXPIRY_FORCE_CLOSE', outcomes.EXPIRY),
    ('TP', outcomes.TP),
]

#: what zebra's paper engine writes for the same five triggers.
PAPER = [
    ('paper:debit_sl', outcomes.DEBIT_SL),
    ('paper:spot_sl', outcomes.SPOT_SL),
    ('paper:trail', outcomes.TRAIL),
    ('paper:expiry', outcomes.EXPIRY),
    ('paper:tp', outcomes.TP),
]


def _closed(i, reason, pnl=500.0, **kw):
    t = {'id': i, 'stock': 'TESTCO', 'direction': 'CE', 'status': 'exited',
         'long_strike': 100, 'short_strike': 110, 'pnl': pnl, 'pnl_pct': 12.0,
         'entry_date': '2026-08-20', 'exit_date': '2026-08-25',
         'exit_reason': reason, 'structure': 'bcs'}
    t.update(kw)
    return t


def _report(closed, typ='daily'):
    return {'date': '2026-08-25', 'type': typ, 'closed': closed, 'open': [],
            'closed_summary': R._summarize_exits(closed), 'unrealized': {},
            'week_start': '2026-08-24', 'week_end': '2026-08-28'}


# ── 1. one classification, both engines ─────────────────────────────────────

@pytest.mark.parametrize('raw,kind', BRIDGED + PAPER)
def test_every_writers_reason_prints_as_its_canonical_kind(raw, kind):
    """The core of N3: a bridged `SL_SPREAD` is a `debit_sl` here too.

    Pre-fix this asserted `SL_SPREAD == debit_sl` and failed for all five
    bridged strings — the report simply echoed the monitor's own vocabulary.
    """
    assert R._reason_label(raw) == kind


@pytest.mark.parametrize('raw,kind', BRIDGED)
def test_the_adapter_lowercases_and_that_still_classifies(raw, kind):
    """`bcs/zebra_adapter.py` lowercases before storing, so the string that
    actually reaches this report is `sl_spread`, not `SL_SPREAD`."""
    assert R._reason_label(raw.lower()) == kind


def test_the_two_engines_names_for_one_trigger_share_a_bucket():
    """The visible defect: two rows for one trigger on the phone summary.

    Pre-fix `by_reason` held {'debit_sl': 1, 'SL_SPREAD': 1} — the reader sees
    two stops of different kinds where there was one kind, twice.
    """
    s = R._summarize_exits([_closed(1, 'paper:debit_sl', pnl=-300.0),
                            _closed(2, 'SL_SPREAD', pnl=-700.0)])
    assert set(s['by_reason']) == {outcomes.DEBIT_SL}
    assert s['by_reason'][outcomes.DEBIT_SL]['count'] == 2
    assert s['by_reason'][outcomes.DEBIT_SL]['pnl'] == -1000.0


def test_the_bridged_reason_reaches_both_rendered_summaries():
    """Both formatters, because the drift was in both — and in the aggregate.

    A test on `_reason_label` alone would pass with one of the three call
    sites still doing its own `.replace`.
    """
    r = _report([_closed(1, 'SL_SPREAD', pnl=-700.0)])
    text, tg = R.format_text(r), R.format_telegram(r)
    for rendered in (text, tg):
        assert 'debit_sl' in rendered
        assert 'SL_SPREAD' not in rendered, \
            'the monitor’s private name leaked into the owner’s summary'
    # and the by-reason block in the text report, which is a third call site
    assert 'debit_sl' in text.split('By exit reason:')[1]


# ── 2. the wording that must NOT be shared away ─────────────────────────────

def test_an_already_flat_close_keeps_its_qualifier():
    """`already_flat_tp` IS a take-profit — and it is not evidence.

    No close machinery ran: the legs were flat before the monitor acted, so
    the price was recovered from order history rather than transacted (and
    degrades to 0.0 when the history yields nothing). `is_stop_exit` refuses
    it for exactly that reason, so a reader of this summary has to be able to
    see it. Folding it into plain `tp` would hide the shape that the
    arm-against-paper-positions defect MANUFACTURES.
    """
    label = R._reason_label('ALREADY_FLAT_TP')
    assert label.startswith(outcomes.TP)
    assert 'already-flat' in label
    assert R._reason_label('ALREADY_FLAT_SL_SPREAD').startswith(
        outcomes.DEBIT_SL)


def test_a_recovered_close_is_not_bucketed_with_a_transacted_one():
    s = R._summarize_exits([_closed(1, 'paper:tp'),
                            _closed(2, 'ALREADY_FLAT_TP')])
    assert len(s['by_reason']) == 2, \
        'a recovered close and a real one are different evidence'


def test_an_unrecognised_reason_is_shown_verbatim_not_swallowed():
    """Vocabulary drift must stay LOUD.

    `classify` returns kind=None and logs a WARNING; the report must not turn
    that into a tidy bucket. Printing the raw string is what lets a human spot
    a writer nobody taught the vocabulary about.
    """
    assert R._reason_label('SL_GAMMA_PANIC') == 'sl_gamma_panic'
    r = _report([_closed(1, 'SL_GAMMA_PANIC')])
    assert 'sl_gamma_panic' in R.format_text(r)


def test_a_missing_reason_reads_unknown_in_every_place():
    """It already did in `by_reason`; the two formatters printed `[]`."""
    for reason in (None, '', '   '):
        assert R._reason_label(reason) == 'unknown'
    r = _report([_closed(1, None)])
    assert 'unknown' in R.format_text(r)
    assert 'unknown' in R.format_telegram(r)


def test_the_telegram_formatter_still_escapes_the_reason():
    """`html.escape` is applied to the reason in the Telegram block, and the
    label is now computed rather than echoed — the escape must survive that.
    One bare `<` 400-rejects the whole message silently."""
    r = _report([_closed(1, 'SL_<GAMMA>')])
    tg = R.format_telegram(r)
    assert 'sl_&lt;gamma&gt;' in tg
    assert '<gamma>' not in tg


# ── 3. no fourth copy ───────────────────────────────────────────────────────

def test_the_report_no_longer_carries_its_own_copy_of_the_vocabulary():
    """Structural, and the one that keeps this fixed.

    Three call sites each re-derived the mapping; a fourth added later would
    reintroduce the bug without failing any behavioural test above, because it
    would be somewhere none of them look. Pin the absence at the source.
    """
    src = inspect.getsource(R)
    assert "replace('paper:'" not in src and 'replace("paper:"' not in src, \
        'report.py is re-deriving the exit vocabulary again — route it ' \
        'through zebra.outcomes.classify instead'
    assert 'outcomes.classify' in src


def test_the_label_helper_never_raises():
    """`classify` is documented never to raise, and this report is the last
    thing that runs at 15:30 — a malformed reason must not take the whole EOD
    summary down with it."""
    for junk in (None, '', 0, 3.7, [], {}, object()):
        assert isinstance(R._reason_label(junk), str)
