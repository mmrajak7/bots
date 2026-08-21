"""Test telegram.suppress_non_critical — the mute must never reach a critical alert.

Owner's call 2026-08-21: CROCODILE is parked and not trading, but must keep
RUNNING. It just stops narrating. Everything non-critical goes quiet; anything
about money or an unprotected position still gets through.

SAFETY: requests.post is replaced BEFORE any client call, so a regression in
this file cannot Telegram the owner. Nothing here touches the network.

Run: python test_telegram_suppression.py
"""
import sys
sys.path.insert(0, '.')

import src.reporting.telegram_client as tc


# ── Rail: no message can leave this process ──────────────────────────────

SENT = []


class _FakeResponse:
    def raise_for_status(self):
        pass

    def json(self):
        return {'ok': True}


def _fake_post(url, json=None, timeout=None, **kwargs):
    SENT.append((json or {}).get('text', ''))
    return _FakeResponse()


class _FakeRequests:
    post = staticmethod(_fake_post)


tc.requests = _FakeRequests()


# ── The matrix ───────────────────────────────────────────────────────────

client = tc.TelegramClient()

# The three real safety warnings in order_monitor.py go out via send_message()
# and must carry critical=True, or the mute takes down the only alerts that
# say a live position has no stop-loss.
CASES = [
    # (label,                          call,                                 should_send_when_muted)
    ('ORDERS PLACED',                  lambda: client.send_alert('ORDERS PLACED', critical=False), False),
    ('Order Filled & Position Created', lambda: client.send_alert('Order Filled', critical=False), False),
    ('SL HIT - Position Closed',       lambda: client.send_alert('SL HIT', critical=False), False),
    ('daily report',                   lambda: client.send_formatted_report('report'), False),
    ('EOD GTT Update Complete',        lambda: client.send_alert('EOD GTT', critical=False), False),
    ('morning startup',                lambda: client.send_alert('Ready for trading', critical=False), False),
    ('ORDER PLACEMENT FAILED',         lambda: client.send_alert('ORDER FAILED', critical=True), True),
    ('UNPROTECTED POSITION',           lambda: client.send_message('UNPROTECTED', critical=True), True),
    ('GTT MISSING',                    lambda: client.send_message('GTT MISSING', critical=True), True),
    ('GTT NOT ACTIVE',                 lambda: client.send_message('GTT NOT ACTIVE', critical=True), True),
]

failures = []


def check(label, actual, expected, context):
    ok = actual == expected
    if not ok:
        failures.append(f"{context}: {label} sent={actual}, expected {expected}")
    print(f"  {'PASS' if ok else 'FAIL'}  {label:<34} sent={actual}")


print("=" * 68)
print("Telegram suppression matrix")
print("=" * 68)

print("\nsuppress_non_critical = True  (only critical survives)")
client.suppress_non_critical = True
for label, call, should_send in CASES:
    before = len(SENT)
    call()
    check(label, len(SENT) > before, should_send, 'muted')

print("\nsuppress_non_critical = False  (everything goes out)")
client.suppress_non_critical = False
for label, call, _ in CASES:
    before = len(SENT)
    call()
    check(label, len(SENT) > before, True, 'unmuted')

# A caller that forgets the flag is treated as noise, not as an alert. That is
# the safe default for a NEW caller under a mute the owner switched on --
# but it means any future SAFETY message must pass critical=True explicitly.
print("\nDefault for an unflagged send_message()")
client.suppress_non_critical = True
before = len(SENT)
client.send_message('legacy caller, no flag')
check('unflagged defaults to suppressed', len(SENT) > before, False, 'default')

print()
print("=" * 68)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"All checks passed. {len(SENT)} messages would have been sent, 0 left this process.")
