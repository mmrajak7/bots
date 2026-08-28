"""The TP-latch evidence, and the fifth copy of the exit vocabulary.

Two defects, one file, because they are the same defect twice: something the
system RECORDS that nothing READS, and something the system already knows how
to say that a second reader says differently.

**Job 1 — evidence with no reader.** `zebra/trade_store.py` stamps three kinds
of fact about the take-profit latch and, until this file existed, nothing read
any of them. They are not decoration: they answer two questions the owner has
open right now.

  * *Is same-day the right bound?* Every touch that reached the end of its
    session unbooked is appended to `tp_latch_expired`. An empty list means the
    bound is free; a full one means touches are not converting inside a session
    and the rule is costing exits.
  * *Is M12 worth building?* M12 consumes the exit vet inside the same cycle
    rather than on the next tick, saving ~3 minutes of a measured ~4m50s
    latency. `tp_touch_to_exit_sec` is that latency and `tp_touch_spot_move*`
    is what price did during it.

The hard part is ABSENCE. Every record written before 2026-08-28 carries none
of these fields — including COFORGE #436 and CROMPTON #450, the two exits that
booked on the day the latch shipped, hours before it was committed. "No touch
has ever been recorded" and "touches are recorded and none has ever lapsed" are
opposite answers to question 1, and a renderer that prints `0 expired` for both
has destroyed the distinction it was built to report.

**Job 2 — the fifth copy.** The Closed table did its own
`exit_reason.replace('paper:', '')`, so a bridged `SL_SPREAD` printed raw in the
one file that owns the arming gate. Three copies in `report.py` were repointed
at `outcomes.classify`; this was the fourth reader and the fifth copy.
"""

from __future__ import annotations

import inspect

import pytest

from zebra import digest as D
from zebra import outcomes
from zebra import report as R
from zebra import trade_store as TS


# ── fixtures: records shaped like the real ones ─────────────────────────────

def _closed(tid, stock, reason='paper:tp', **kw):
    t = {'id': tid, 'stock': stock, 'status': 'exited', 'cohort': '2026-08-14',
         'direction': 'CE', 'structure': 'bcs', 'exit_reason': reason,
         'entry_date': '2026-08-21', 'exit_date': '2026-08-28',
         'pnl': 1000.0, 'pnl_net': 900.0, 'pnl_pct': 10.0, 'pnl_net_pct': 9.0,
         'quantity': 100, 'debit': 10.0, 'exit_debit': 12.0,
         'fees': {'basis': 'modelled'}}
    t.update(kw)
    return t


def _latched(tid, stock, sec, move_pct, gave_back, **kw):
    """A closed record carrying the touch->fill stamp `tp_touch_to_fill` writes."""
    return _closed(tid, stock,
                   **{TS.TP_TOUCHED_AT: '2026-08-28T09:20:33+05:30',
                      TS.TP_TOUCH_SPOT: 1931.91,
                      R.TP_TOUCH_SEC: sec,
                      R.TP_TOUCH_MOVE: round(1931.91 * move_pct / 100, 2),
                      R.TP_TOUCH_MOVE_PCT: move_pct,
                      R.TP_TOUCH_GAVE_BACK: gave_back,
                      'tp_spot': 1931.91},
                   **kw)


def _lapsed(tid, stock, touched='2026-08-27T15:29:10+05:30', status='entered'):
    return {'id': tid, 'stock': stock, 'status': status,
            'cohort': '2026-08-14', 'direction': 'CE', 'tp_spot': 1931.91,
            TS.TP_LATCH_EXPIRED: [{'touched_at': touched,
                                   'touch_spot': 1933.4,
                                   'noticed_at': '2026-08-28T09:15:12+05:30'}]}


def _digest(rows, day='2026-08-28'):
    """`build()`'s assembly, minus the log parsing and the real store.

    Deliberately the module's own functions rather than a hand-written dict:
    a test that invents the shape stops testing the shape the renderer is
    handed the moment `build` changes.
    """
    cyc, fun = D._cycles([]), D._funnel([])
    vet, tr = D._vetting([], day), D._trades(rows, day)
    coh, warn = D._cohort(rows), {}
    tpl = R.tp_latch_evidence(rows, day)
    return {'date': day, 'cycles': cyc, 'funnel': fun, 'vetting': vet,
            'trades': {k: (v if not isinstance(v, list) else len(v))
                       for k, v in tr.items()},
            'cohort': coh, 'warnings': warn, 'engines': None, 'tp_latch': tpl,
            'flags': D._flags(cyc, vet, tr, warn, coh, None, None, tpl),
            '_detail': tr}


def _render(rows, day='2026-08-28'):
    return D.render(_digest(rows, day))


# ── 1. the field names, pinned at the writer ────────────────────────────────

def test_this_reader_knows_every_key_the_writer_stamps():
    """The structural pin, and the one that keeps the rest honest.

    `tp_touch_to_fill` spells its output keys as literals, so this reader
    spells them as literals too. A rename there would otherwise make every
    statistic below read as "no data" — the failure mode this whole section
    exists to prevent, arriving silently and looking like a quiet week.
    """
    t = {TS.TP_TOUCHED_AT: TS._ist().isoformat(), TS.TP_TOUCH_SPOT: 100.0,
         'tp_spot': 100.0}
    gap = TS.tp_touch_to_fill(t, 99.0, rising=True, now=TS._ist())
    assert gap, 'the writer produced nothing — this fixture is wrong, not the code'
    known = {R.TP_TOUCH_SEC, R.TP_TOUCH_MOVE, R.TP_TOUCH_MOVE_PCT,
             R.TP_TOUCH_GAVE_BACK}
    assert set(gap) <= known, (
        'zebra.trade_store.tp_touch_to_fill now stamps a field this reader '
        'does not know: %s. Add it to zebra/report.py or the digest will '
        'report it as absent.' % (set(gap) - known))


# ── 2. touch->fill is REPORTED when it is there ─────────────────────────────

def test_touch_to_fill_is_reported_with_a_median_and_a_worst_case():
    """Not a mean. One bad give-back IS the answer to "does the lag cost
    money"; a mean dilutes it against every exit that booked on its touch."""
    out = _render([_latched(436, 'COFORGE', 296.0, -0.31, True),
                   _latched(450, 'CROMPTON', 4.0, 0.01, False)])
    assert '## TP latch' in out
    body = out.split('## TP latch')[1]
    assert 'no data' not in body.split('**Expired unbooked**')[0]
    assert 'median 150s' in body and 'worst 296s' in body
    assert 'COFORGE' in body, 'the worst case must be NAMED, not just counted'
    # the per-trade rows, so a distribution of two is readable as two facts
    assert '| 436 |' in body and '| 450 |' in body


def test_the_spot_give_back_is_normalised_not_blended():
    """A CE that gave back reads negative and a PE that gave back reads
    positive; averaging them cancels the very thing being measured.

    The owner's standing lesson is that a blended statistic hides the answer,
    and this is the blend that would have hidden it.
    """
    ce = _latched(1, 'COFORGE', 300.0, -0.40, True, direction='CE')
    pe = _latched(2, 'CROMPTON', 300.0, +0.40, True, direction='PE')
    s = R.tp_touch_stats([ce, pe])
    assert s['median_adverse_pct'] == pytest.approx(0.40), \
        'two give-backs of the same size cancelled to zero'
    assert s['gave_back'] == 2


def test_a_favourable_move_is_not_counted_as_a_give_back():
    s = R.tp_touch_stats([_latched(1, 'X', 300.0, +0.20, False,
                                   direction='CE')])
    assert s['gave_back'] == 0
    assert s['median_adverse_pct'] == pytest.approx(-0.20)


def test_the_rupee_give_back_is_only_charged_to_the_latency():
    """Peak-to-booked value, and ONLY where the peak came at or after the
    touch. A peak from three sessions earlier is a fact about the trade, not
    about the five minutes M12 is being judged on."""
    during = _latched(1, 'A', 300.0, -0.3, True, mfe_mid=15.0,
                      exit_debit=12.0, quantity=100,
                      mfe_mid_at='2026-08-28T09:25:25+05:30')
    before = _latched(2, 'B', 300.0, -0.3, True, mfe_mid=15.0,
                      exit_debit=12.0, quantity=100,
                      mfe_mid_at='2026-08-25T11:00:00+05:30')
    assert R._peak_giveback_rs(during) == 300.0
    assert R._peak_giveback_rs(before) is None
    s = R.tp_touch_stats([during, before])
    assert s['giveback_rs_total'] == 300.0
    body = _render([during, before]).split('## TP latch')[1]
    assert 'Rs 300' in body


def test_the_latency_reaches_the_flags_where_the_owner_reads_it():
    """The digest is skimmed for its flag list. Evidence for an open build
    decision that only appears in a table below the fold is evidence that
    will be read after the decision, not before it."""
    d = _digest([_latched(436, 'COFORGE', 296.0, -0.31, True)])
    assert any('touch→fill' in f for f in d['flags'])
    assert any('M12' in f for f in d['flags'])


# ── 3. absence reads as ABSENCE ─────────────────────────────────────────────

def test_no_touch_ever_recorded_reads_as_no_data_and_never_as_zero():
    """The live case on 2026-08-28: the latch shipped at 13:52 and the day's
    two exits booked at 09:25 and 10:05, so the whole book carries none of
    these fields. That is NOT "the same-day bound cost nothing"."""
    out = _render([_closed(436, 'COFORGE'), _closed(450, 'CROMPTON')])
    assert '## TP latch' in out, 'the section must be stated, never omitted'
    body = out.split('## TP latch')[1].split('## ')[0]
    assert 'no data' in body
    assert '0 of' not in body and 'Expired unbooked** 0' not in body, \
        'absence rendered as a measured zero'


def test_an_unarmed_latch_raises_no_flag_at_all():
    d = _digest([_closed(436, 'COFORGE')])
    assert not any('latch' in f.lower() or 'touch' in f.lower()
                   for f in d['flags'])


def test_an_armed_latch_that_never_lapsed_is_a_real_zero():
    """The other side of the same coin: once touches ARE being recorded, "none
    of them expired" is a finding and must be stated as one."""
    body = _render([_latched(436, 'COFORGE', 296.0, -0.31, True)]).split(
        '## TP latch')[1]
    assert 'Expired unbooked** 0 of 1' in body
    assert 'cost nothing yet' in body


def test_a_touch_with_no_booked_exit_yet_says_so_separately():
    """Armed, nothing measured: the two halves of the section have their own
    absence, because a touch on an open position answers question 1 and not
    question 2."""
    body = _render([_lapsed(441, 'TITAN')]).split('## TP latch')[1]
    assert 'Touch→fill** _no data' in body
    assert 'Expired unbooked** 1' in body


# ── 4. the expired latch is counted AND named ───────────────────────────────

def test_an_expired_latch_is_counted_and_the_trade_is_named():
    rows = [_lapsed(441, 'TITAN'), _latched(436, 'COFORGE', 296.0, -0.3, True)]
    d = _digest(rows)
    body = D.render(d).split('## TP latch')[1]
    assert 'Expired unbooked** 1 of 2' in body
    assert '#441 TITAN' in body
    assert '2026-08-27T15:29:10+05:30' in body and '1933.4' in body
    assert any('EXPIRED UNBOOKED' in f and '#441 TITAN' in f
               for f in d['flags']), \
        'the only price the same-day bound charges must earn a look'


def test_a_lapse_on_an_OPEN_position_still_counts():
    """The lapse lives on the record, not on the exit. A reader that walked
    only the closed trades would report the bound as costless while a position
    that lapsed on Tuesday and re-armed on Wednesday is still open."""
    exp = R.tp_latch_expiries([_lapsed(441, 'TITAN', status='entered')])
    assert len(exp) == 1 and exp[0]['status'] == 'entered'


def test_a_lapsed_then_rearmed_record_counts_two_touch_days_not_three():
    """`tp_touched_at` is OVERWRITTEN on a re-arm while the lapsed stamp is
    preserved in the list, so adding the two double-counts a latch that
    lapsed and was never re-armed."""
    rearmed = _lapsed(441, 'TITAN')
    rearmed[TS.TP_TOUCHED_AT] = '2026-08-28T09:20:00+05:30'
    assert R.tp_touch_days([rearmed]) == 2
    # never re-armed: the stale stamp and the lapse are ONE touch
    stale = _lapsed(442, 'INFY')
    stale[TS.TP_TOUCHED_AT] = stale[TS.TP_LATCH_EXPIRED][0]['touched_at']
    assert R.tp_touch_days([stale]) == 1


def test_corrupt_latch_evidence_cannot_take_the_digest_down():
    """The digest is the accountability record for a paper run; a malformed
    field must cost its own line, never the whole document."""
    junk = _closed(1, 'X', **{TS.TP_LATCH_EXPIRED: ['not-a-dict', None],
                              TS.TP_TOUCHED_AT: 'not-a-timestamp',
                              R.TP_TOUCH_SEC: 'soon'})
    out = _render([junk])
    assert '## TP latch' in out


# ── 5. the fifth copy of the exit vocabulary ────────────────────────────────

def test_a_bridged_stop_no_longer_prints_raw_in_the_closed_table():
    """`bcs/spread_monitor.py` writes `SL_SPREAD` for the trigger zebra calls
    `debit_sl`. The Closed table stripped `paper:` and printed the rest, so
    the bridged name reached the page — in the file that owns the arming
    gate, which is the most embarrassing place for it to survive."""
    out = _render([_closed(1, 'X', reason='SL_SPREAD')])
    table = out.split('## Closed')[1]
    assert outcomes.DEBIT_SL in table
    assert 'SL_SPREAD' not in table


def test_the_already_flat_qualifier_survives_in_the_digest_too():
    """`already_flat_tp` IS a take-profit, and it is NOT stop evidence: no
    close machinery ran and the price was recovered from order history rather
    than transacted. That is the fact `is_stop_exit` turns on, so the reader
    of the Closed table has to be able to see it."""
    table = _render([_closed(1, 'X', reason='ALREADY_FLAT_TP')]).split(
        '## Closed')[1]
    assert 'tp (already-flat)' in table


def test_the_digest_and_the_report_name_a_trigger_identically():
    """One presentation, not two. The point of repointing the fifth copy is
    that the two summaries a human reads cannot disagree about what fired."""
    for raw in ('paper:debit_sl', 'SL_SPREAD', 'ALREADY_FLAT_TP',
                'paper:tp', 'SL_GAMMA_PANIC', None):
        table = _render([_closed(1, 'X', reason=raw)]).split('## Closed')[1]
        assert f"| {R.reason_label(raw)} |" in table


def test_the_digest_no_longer_carries_its_own_copy_of_the_vocabulary():
    """Structural, and the one that keeps this fixed: a sixth copy added later
    would reintroduce the bug without failing anything above, because it would
    be somewhere none of these tests look."""
    src = inspect.getsource(D)
    assert "replace('paper:'" not in src and 'replace("paper:"' not in src, \
        'zebra/digest.py is re-deriving the exit vocabulary again — route it ' \
        'through zebra.report.reason_label / zebra.outcomes.classify instead'


# ── 6. the phone report, which shares the computation ───────────────────────

def _report(closed):
    return {'date': '2026-08-28', 'type': 'daily', 'closed': closed,
            'open': [], 'unrealized': {},
            'closed_summary': R._summarize_exits(closed)}


def test_the_eod_report_carries_the_latency_when_there_is_one():
    rep = _report([_latched(436, 'COFORGE', 296.0, -0.31, True,
                            long_strike=100, short_strike=110),
                   _latched(450, 'CROMPTON', 4.0, 0.01, False,
                            long_strike=100, short_strike=110)])
    text = R.format_text(rep)
    assert 'TP touch -> fill' in text
    assert 'median 150s' in text and 'worst 296s' in text
    assert 'TP touch' in R.format_telegram(rep)


def test_the_eod_report_omits_the_block_rather_than_printing_zeros():
    text = R.format_text(_report([_closed(1, 'X', long_strike=100,
                                          short_strike=110)]))
    assert 'TP touch' not in text


def test_the_text_report_stays_ascii_encodable():
    """`format_text` is PRINTED. A Windows console encodes stdout as cp1252,
    where one `→` raises UnicodeEncodeError and takes the whole EOD summary
    down with it — so the plain-text half uses `->` and the Telegram half,
    which goes out as UTF-8 over HTTP, keeps the real arrow."""
    text = R.format_text(_report([_latched(436, 'COFORGE', 296.0, -0.31, True,
                                           long_strike=100, short_strike=110)]))
    block = text.split('TP touch')[1].split('\n\n')[0]
    block.encode('cp1252')          # raises if a non-latin1 glyph crept in


# ── 7. purely additive ──────────────────────────────────────────────────────

def test_the_existing_sections_are_untouched_by_the_new_one():
    """The arming gate, the funnel, the vetting block, the cohort line and the
    engines section are the digest's job; the latch section is an addition to
    it, not a rewrite of it."""
    rows = [_closed(436, 'COFORGE'), _lapsed(441, 'TITAN')]
    d = _digest(rows)
    out = D.render(d)
    for section in ('# Zebra digest', '**Cycles**', '**Scan**', '**Vet**',
                    '## Closed', '**Cohort to date**', '## ⚑ Earns a look'):
        assert section in out
    assert any('ARMING GATE UNMET' in f for f in d['flags']), \
        'the gate still reports on a cohort of pure take-profits'


def test_flags_still_works_for_a_caller_that_knows_nothing_about_the_latch():
    """Same contract `eng` has: older call sites keep working."""
    rows = [_closed(436, 'COFORGE')]
    flags = D._flags(D._cycles([]), D._vetting([], '2026-08-28'),
                     D._trades(rows, '2026-08-28'), {}, D._cohort(rows), None)
    assert isinstance(flags, list)
