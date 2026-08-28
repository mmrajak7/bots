"""Which alerts reach the phone — the policy, and the rails on it.

Owner, 2026-08-28, after a morning of reconciling three Telegrams about two
positions:

    "i dont need unwanted messages like these [DRY RUN] BPS WOULD CLOSE:
     CROMPTON / Reason: TP / Entry: 2.10 | Exit: not priced (no fill) / P&L:
     not computed... need only important alerts in telegram"

The dangerous half of that request is the half these tests exist for. The
message he wants gone and the message he must never lose read almost
identically — both say a close happened and no fill priced it — and they need
opposite reactions:

  * DRY RUN over a PAPER record   -> rehearsal. zebra books it and sends the
                                     real P&L minutes later. Noise.
  * ARMED, no fill found          -> a real close could not be priced. A
                                     human must look.

So the tests below are mostly negative: they assert that the important
messages still arrive, and that no future edit can sweep them up with the
rehearsals. Two of them fail if `SILENT` grows a member it should not have.

Run:  cd Helper && python -m pytest bcs/tests/test_alert_policy.py -v
"""
import importlib
import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import alert_policy as ap                                # noqa: E402
from bcs import spread_monitor as sm                              # noqa: E402
from bcs.tests.fakes import FakeBroker, FakeClock, MemoryStore, TelegramSpy  # noqa: E402

LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY, DEBIT = 700, 13.55
BOOKS = {
    'NFO:%s' % LONG:  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                       'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    'NFO:%s' % SHORT: {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                       'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}


def _trade(paper=False):
    t = {'id': 450, 'stock': 'CROMPTON', 'status': 'open',
         'long_symbol': LONG, 'short_symbol': SHORT, 'quantity': QTY,
         'exchange': 'NFO', 'net_debit': DEBIT, 'spot_symbol': 'NSE:CROMPTON'}
    if paper:
        t['paper'] = True
    return t


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_trade()])


def _close(store, trade, dry_run):
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    return sm._close_spread_inner(kite, store, trade, spot=235.95,
                                  reason='TP', dry_run=dry_run, label='BPS')


# ── the rails: a safety alert can never become suppressible ─────────────────

def test_safety_and_booking_can_never_be_silenced():
    assert not (ap.SILENT & ap.NEVER_SILENT)
    assert ap.SAFETY in ap.NEVER_SILENT
    assert ap.BOOKING in ap.NEVER_SILENT
    assert ap.should_send(ap.SAFETY)[0] is True
    assert ap.should_send(ap.BOOKING)[0] is True


def test_adding_a_safety_class_to_SILENT_breaks_the_import(monkeypatch, tmp_path):
    """The rail with teeth. Editing SILENT to include SAFETY must not merely
    be wrong — the module must refuse to load, so the mistake cannot ship as a
    quiet behaviour change nobody notices until an alert does not arrive."""
    src = Path(ap.__file__).read_text(encoding='utf-8')
    broken = src.replace("SILENT = frozenset({SHADOW})",
                         "SILENT = frozenset({SHADOW, SAFETY})")
    assert broken != src, 'the SILENT definition moved; update this test'
    path = tmp_path / 'broken_policy.py'
    path.write_text(broken, encoding='utf-8')
    sys.path.insert(0, str(tmp_path))
    try:
        with pytest.raises(RuntimeError) as ei:
            importlib.import_module('broken_policy')
        assert 'never be suppressed' in str(ei.value)
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop('broken_policy', None)


def test_an_unclassified_alert_is_sent():
    """Default is SEND. The opposite default fails silently: an alert nobody
    knew had stopped arriving."""
    assert ap.should_send(None)[0] is True
    assert ap.should_send('a-class-nobody-defined')[0] is True


def test_only_the_shadow_class_is_silent():
    """Pins the whole policy in one line, so growing it is a visible diff."""
    assert ap.SILENT == frozenset({ap.SHADOW})


# ── the noise the owner asked to lose ───────────────────────────────────────

def test_a_dry_run_close_of_a_paper_record_is_not_telegrammed(env):
    spy, store = env
    _close(store, _trade(paper=True), dry_run=True)
    assert spy.sent == [], 'a rehearsal reached the phone: %r' % spy.sent
    assert spy.suppressed, 'nothing was even offered to the policy'


def test_the_duplicate_trigger_announcement_is_suppressed_too(env):
    """The owner got the monitor's COFORGE TP at 09:18 AND zebra's at 09:25.
    The trigger alert is the first of the two, so silencing only the close
    alert would halve the problem and call it fixed."""
    spy, store = env
    _close(store, _trade(paper=True), dry_run=True)
    assert not [m for m in spy.sent if 'TRIGGERED' in m]
    assert [m for _c, m in spy.suppressed if 'TRIGGERED' in m]


def test_a_suppressed_message_is_still_written_to_the_log(capsys):
    """Telegram is not the evidence channel. Suppressing a message must cost
    nothing the dry-run evidence week depends on, so `send_telegram` logs the
    whole body plus the policy's reason and only then returns."""
    body = 'SHADOW CLOSE: CROMPTON' + chr(10) + 'Booked by: zebra (paper)'
    sm.send_telegram(body, alert_class=ap.SHADOW)
    out = capsys.readouterr().out
    assert 'telegram suppressed' in out
    assert 'SHADOW CLOSE: CROMPTON' in out
    assert 'Booked by: zebra (paper)' in out, 'only the first line was kept'


def test_the_shadow_names_the_engine_that_actually_books(env):
    """Task 3. Whether it goes to the phone or only to the log, the message
    must say WHICH engine books this position — that is the fact the owner
    was missing when he had two numbers for one trade."""
    spy, store = env
    _close(store, _trade(paper=True), dry_run=True)
    body = '\n'.join(m for _c, m in spy.suppressed)
    assert 'zebra (paper)' in body


# ── the messages that must never be lost ────────────────────────────────────

def test_a_live_unpriced_close_refusal_still_alarms(monkeypatch):
    """THE distinction. Armed, both legs flat, no fill this system placed can
    price the exit — a human must look. It must be sent, and it must be
    readable as different from a rehearsal."""
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    sm._unpriced_close_alerted.clear()
    legs = [('short', SHORT, 'BUY', {'price': None, 'untagged': 0,
                                     'error': None}),
            ('long', LONG, 'SELL', {'price': None, 'untagged': 2,
                                    'error': None})]
    out = sm._refuse_unpriced_close(_trade(), 'BPS', 'TP', 235.95,
                                    dry_run=False, legs=legs)
    assert out is False
    assert len(spy.sent) == 1, 'the live refusal was suppressed: %r' % spy.sent
    msg = spy.sent[0]
    assert 'CANNOT BE PRICED' in msg
    assert 'NOT A DRY-RUN REHEARSAL' in msg
    assert 'ACTION NEEDED' in msg
    # ...and it must not be mistakable for the rehearsal, which says the
    # opposite in almost the same words.
    assert 'REHEARSAL — this is expected' not in msg


def test_a_real_record_in_dry_run_is_safety_not_shadow(env):
    """The trap. `dry_run` alone is NOT what makes an alert noise — having
    another engine that books is. A live position whose stop fires while the
    only engine that could close it is disarmed needs a human, and there is
    no zebra message coming."""
    assert ap.close_alert_class(dry_run=True, is_paper_record=False) == ap.SAFETY
    assert ap.close_alert_class(dry_run=True, is_paper_record=True) == ap.SHADOW
    assert ap.close_alert_class(dry_run=False, is_paper_record=False) == ap.BOOKING

    spy, store = env
    _close(store, _trade(paper=False), dry_run=True)
    body = '\n'.join(spy.sent)
    assert 'NO ORDER WAS PLACED' in body
    assert 'STILL OPEN at the broker' in body
    assert 'ACTION NEEDED' in body


def test_a_real_live_close_still_reports_its_pnl(env):
    spy, store = env
    _close(store, _trade(paper=False), dry_run=False)
    assert [m for m in spy.sent if 'P&L: Rs' in m], spy.sent


def _telegram_calls(src):
    """(text) of every `send_telegram(...)` call in the module source."""
    out, i = [], 0
    while True:
        i = src.find('send_telegram(', i)
        if i == -1:
            return out
        depth, end = 0, None
        for j in range(i, len(src)):
            if src[j] == '(':
                depth += 1
            elif src[j] == ')':
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            return out
        out.append(src[i:end + 1])
        i = end + 1


@pytest.mark.parametrize('needle', [
    'Manual intervention needed',
    'MONITOR FATAL: Kite token expired',
    'STALE KITE TOKEN',
    'MONITOR DISARMED',
    'CANNOT BE PRICED',
    'NOT FLAT AFTER CLOSE',
    'FLIPPED POSITION',
    'PARTIAL CLOSE',
    'Close manually in Kite',
    'BLIND NEAR SL',
    'SPOT UNAVAILABLE',
    'reached the ORDER PATH for',
    'consecutive errors',
    'Cohort store unavailable',
    'Naked risk',
])
def test_every_manual_attention_alert_is_classified_safety(needle):
    """Source-level, and deliberately so: driving fifteen failure paths would
    test the harness. What must hold is that EVERY `send_telegram` call whose
    body carries one of these phrases passes `alert_class=alert_policy.SAFETY`
    — unsuppressible by construction, not by the current value of `SILENT`."""
    calls = [c for c in _telegram_calls(inspect.getsource(sm)) if needle in c]
    assert calls, 'alert text moved, or the alert is gone: %r' % needle
    for c in calls:
        assert 'alert_class=alert_policy.SAFETY' in c, (
            'this alert can be silenced by a policy edit: %r -- %s'
            % (needle, c[:300]))


def test_the_vet_and_zebra_senders_are_untouched_by_this_policy():
    """The owner named the Claude-vet alerts (usage limit, login needed) as
    ones he wants. They are sent by `zebra/vet.py` and `zebra/monitor.py`
    through their OWN sender, which this policy does not wrap — asserted here
    so a later "unify the senders" refactor has to think about it."""
    import zebra.monitor as zmon
    assert 'alert_policy' not in inspect.getsource(zmon._send_telegram)
