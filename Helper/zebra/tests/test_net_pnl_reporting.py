"""The EOD report must not print GROSS under a label that says "net".

2026-09-07: the daily report said `net Rs -6,142` for #487 HEROMOTOCO, whose
record reads `pnl -6142.5, pnl_net -6277.44`. Over the cohort the same line
read +14,091 where the realised figure is +11,203 — **Rs 2,888 on Rs 11,203,
26% overstated**.

That is not cosmetic. Fee drag is the term that decides whether this strategy
is profitable at all (+0.90% gross / -0.79% net, `bcs_economics_fee_drag`), so
a daily report labelling gross as net hides precisely the number go-live turns
on, in the optimistic direction, in the one artefact the owner reads nightly.
"""

import zebra.report as R


def _t(tid, pnl, pnl_net=None, **kw):
    t = {'id': tid, 'stock': 'X', 'direction': 'CE', 'pnl': pnl,
         'pnl_pct': 10.0, 'exit_reason': 'paper:tp', 'structure': 'bcs',
         'entry_date': '2026-09-01', 'exit_date': '2026-09-05',
         'long_strike': 100, 'short_strike': 110}
    if pnl_net is not None:
        t['pnl_net'] = pnl_net
    t.update(kw)
    return t


# -- the money ---------------------------------------------------------------

def test_the_summary_reports_NET_when_costs_are_known():
    s = R._summarize_exits([_t(1, 1000.0, 800.0), _t(2, -500.0, -600.0)])
    assert s['net_pnl'] == 200.0, 'gross would have been +500'
    assert s['uncosted'] == 0
    assert s['basis_note'] == ''


def test_a_trade_that_wins_gross_and_LOSES_net_is_counted_as_a_LOSS():
    """The count and the rupee figure must not disagree. A "win" that loses
    money after costs is not a win — and with this book's margins (3 winners of
    33 carry it) that is a live case, not a contrived one."""
    s = R._summarize_exits([_t(1, 50.0, -80.0)])
    assert (s['wins'], s['losses']) == (0, 1)
    assert s['win_rate'] == 0.0
    assert s['net_pnl'] == -80.0


def test_best_and_worst_are_chosen_on_NET():
    s = R._summarize_exits([_t(1, 900.0, 100.0), _t(2, 800.0, 700.0)])
    assert s['best']['id'] == 2
    assert s['worst']['id'] == 1


def test_by_reason_totals_are_net_too():
    s = R._summarize_exits([_t(1, 1000.0, 800.0, exit_reason='paper:tp')])
    assert s['by_reason']['tp']['pnl'] == 800.0


# -- the fallback, and saying so ---------------------------------------------

def test_an_uncosted_trade_falls_back_to_GROSS():
    """Unavoidable: only 37 of 252 historical exits carry `pnl_net` and 179 can
    never be costed. Dropping them would be worse than counting them gross."""
    s = R._summarize_exits([_t(1, 1000.0)])
    assert s['net_pnl'] == 1000.0
    assert s['uncosted'] == 1


def test_a_MIXED_sum_declares_itself():
    """A silent mixed-basis total is the same defect one level up: a number
    presented as cleaner than it is."""
    s = R._summarize_exits([_t(1, 1000.0, 800.0), _t(2, 500.0)])
    assert '[MIXED — 1 of 2 uncosted' in s['basis_note']


def test_an_ALL_GROSS_sum_declares_itself_differently():
    s = R._summarize_exits([_t(1, 1000.0), _t(2, 500.0)])
    assert 'GROSS' in s['basis_note'] and 'MIXED' not in s['basis_note']


def test_the_printed_header_carries_the_note():
    report = {'type': 'daily', 'date': '2026-09-07', 'closed': [_t(1, 100.0)],
              'closed_summary': R._summarize_exits([_t(1, 100.0)]),
              'open': [], 'unrealized': {}}
    assert 'GROSS' in R.format_text(report)


# -- the splits, which are three copies of the same sum -----------------------

def test_the_alignment_split_is_net():
    """`feedback_copy_pasted_modules_fix_once`: three aggregators computed the
    same wrong sum, so a fix to one of them is not a fix."""
    ex = [_t(1, 1000.0, 800.0, st_direction='UP', direction='CE')]
    a = R._alignment_split(ex)
    assert a['aligned']['net_pnl'] + a['counter']['net_pnl'] == 800.0


def test_the_structure_split_is_net():
    st = R._structure_split([_t(1, 1000.0, 800.0, structure='bcs')])
    assert st['bcs']['net_pnl'] == 800.0
    assert st['bcs']['uncosted'] == 0


# -- the line the owner actually reads ---------------------------------------

def test_the_per_trade_line_shows_net_rupees_and_net_percent():
    line = R._fmt_trade_line(_t(487, -6142.5, -6277.44, stock='HEROMOTOCO',
                                pnl_pct=-52.37, pnl_net_pct=-53.52))
    assert '-6,277' in line and '-53.5%' in line
    assert '-6,142' not in line and '-52.4%' not in line


# -- mutation-driven: the gaps an adversarial review found on 2026-09-07 -----
#
# The first cut of this file pinned the summary aggregator well and left EIGHT
# non-equivalent mutants alive — including both SPLITS reverting their win
# counts to gross (the exact "three copies" revert this file's docstring claims
# to guard) and the ENTIRE Telegram formatter, which is the artefact the owner
# actually reads. A test file that names a failure mode it does not detect is
# the decorative-guard shape.

GROSS_WIN_NET_LOSS = dict(pnl=50.0, pnl_net=-80.0)


def test_the_alignment_split_counts_wins_on_NET():
    ex = [_t(1, st_direction='UP', direction='CE', **GROSS_WIN_NET_LOSS)]
    a = R._alignment_split(ex)
    side = a['aligned'] if a['aligned']['count'] else a['counter']
    assert side['win_rate'] == 0.0, 'a gross win that loses money is not a win'


def test_the_structure_split_counts_wins_on_NET():
    st = R._structure_split([_t(1, structure='bcs', **GROSS_WIN_NET_LOSS)])
    assert st['bcs']['win_rate'] == 0.0


def test_the_splits_report_uncosted_when_there_IS_one():
    """The first cut asserted `uncosted == 0` on a costed trade, so a
    hard-coded 0 survived."""
    st = R._structure_split([_t(1, 100.0, structure='bcs')])
    assert st['bcs']['uncosted'] == 1
    a = R._alignment_split([_t(1, 100.0, st_direction='UP', direction='CE')])
    assert a['aligned']['uncosted'] + a['counter']['uncosted'] == 1


def _report(*trades):
    return {'type': 'daily', 'date': '2026-09-07', 'closed': list(trades),
            'closed_summary': R._summarize_exits(list(trades)),
            'open': [], 'unrealized': {}}


def test_the_TELEGRAM_reports_net_rupees_and_the_basis_note():
    """F1 of the review: `format_text` disclosed a mixed basis and the Telegram
    did not — the same defect one level up, in the message that is read."""
    out = R.format_telegram(_report(_t(1, 1000.0, 800.0), _t(2, 500.0)))
    assert 'Rs +1,300' in out, 'gross would be +1,500'
    assert 'MIXED' in out and '1 of 2 uncosted' in out


def test_the_TELEGRAM_marks_a_gross_win_that_loses_money_RED():
    out = R.format_telegram(_report(_t(1, **GROSS_WIN_NET_LOSS)))
    assert '\U0001F534' in out and '\U0001F7E2' not in out


def test_BOTH_formatters_sort_on_NET():
    """Two trades whose gross and net orderings differ. Best-first must follow
    the money, not the pre-fee number."""
    a = _t(1, 900.0, 100.0, stock='LOWNET')
    b = _t(2, 800.0, 700.0, stock='HIGHNET')
    for out in (R.format_text(_report(a, b)), R.format_telegram(_report(a, b))):
        assert out.index('HIGHNET') < out.index('LOWNET')


def test_pnl_net_PRESENT_BUT_NONE_counts_as_uncosted():
    """`.get('pnl_net', pnl)` returns None for this record rather than falling
    back — the mutant that reverts `_net` to it must die here."""
    t = _t(1, 1000.0)
    t['pnl_net'] = None
    s = R._summarize_exits([t])
    assert s['net_pnl'] == 1000.0
    assert s['uncosted'] == 1


def test_a_trade_that_nets_exactly_ZERO_is_a_loss_not_a_disappearance():
    """`<= 0` vs `< 0`: with the wrong one the trade is in neither bucket and
    the counts stop summing to the total."""
    s = R._summarize_exits([_t(1, 100.0, 0.0)])
    assert s['wins'] + s['losses'] == s['count'] == 1
    assert s['losses'] == 1


def test_a_non_numeric_pnl_does_not_take_the_whole_report_down():
    s = R._summarize_exits([_t(1, 1000.0, 'garbage')])
    assert s['count'] == 1


def test_the_empty_summary_has_the_SAME_SHAPE_as_a_populated_one():
    empty, full = R._summarize_exits([]), R._summarize_exits([_t(1, 1.0, 1.0)])
    assert set(empty) == set(full)


# ── a sum that spans two fee models has to say so ─────────────────────────

def _costed(model, pnl=100.0):
    return {'id': 1, 'stock': 'X', 'pnl': pnl, 'pnl_net': pnl,
            'fees': {'model': model, 'total': 10.0, 'basis': 'full'}}


def test_one_fee_model_reads_clean():
    from zebra import report
    assert report._basis_note(0, 3, {2}) == ''


def test_a_sum_spanning_two_fee_models_announces_itself():
    """The books are deliberately NOT restamped (owner, 2026-09-09) — v2
    applies forward only. That is cheap and safe, but `fees.py` says a figure
    from one model must be recomputed, not compared, so the blend must be
    visible rather than silently added up."""
    from zebra import report
    note = report._basis_note(0, 5, {1, 2})
    assert 'MIXED FEE MODEL' in note and 'v1/v2' in note


def test_the_two_contaminations_are_reported_independently():
    """Uncosted trades and mixed fee models are different problems. A set can
    have either, both or neither, and collapsing them would hide one."""
    from zebra import report
    both = report._basis_note(2, 5, {1, 2})
    assert 'uncosted' in both and 'MIXED FEE MODEL' in both
    assert report._basis_note(2, 5, {2}).count('MIXED') == 1
    assert report._basis_note(5, 5, {2}) == ' [GROSS — no trade in this set carries costs]'


def test_the_model_mix_is_read_off_the_stored_records():
    from zebra import report
    assert report._fee_models([_costed(1), _costed(2)]) == {1, 2}
    assert report._fee_models([_costed(1), _costed(1)]) == {1}
    # An uncosted record contributes no version — it is the OTHER contamination.
    assert report._fee_models([{'pnl': 1.0}]) == set()


def test_BOTH_formatters_disclose_a_mixed_FEE_MODEL():
    """The same defect as the mixed-basis one above, one version later.

    `format_text` and `format_telegram` render the summary separately, and a
    previous review found the note reaching one and not the other. The book is
    deliberately not restamped after `fees.MODEL_VERSION` went 1 -> 2, so this
    blend is guaranteed to occur — it must be visible in the message that is
    actually READ, not only in the one that is not.
    """
    a = _t(1, 1000.0, 900.0, fees={'model': 1, 'total': 100.0, 'basis': 'modelled'})
    b = _t(2, 500.0, 450.0, fees={'model': 2, 'total': 50.0, 'basis': 'full'})
    for out in (R.format_text(_report(a, b)), R.format_telegram(_report(a, b))):
        assert 'MIXED FEE MODEL' in out and 'v1/v2' in out


def test_one_fee_model_leaves_the_report_unmarked():
    """The negative control. Every assertion above passes trivially if the note
    were simply always present, and a permanent warning is one nobody reads."""
    a = _t(1, 1000.0, 900.0, fees={'model': 2, 'total': 100.0, 'basis': 'full'})
    b = _t(2, 500.0, 450.0, fees={'model': 2, 'total': 50.0, 'basis': 'full'})
    for out in (R.format_text(_report(a, b)), R.format_telegram(_report(a, b))):
        assert 'MIXED' not in out and 'GROSS' not in out
