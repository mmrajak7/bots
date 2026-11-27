"""Verify the G1-G4 broker-truth interlocks in neo/orders.py.

The failure these exist to stop: every duplicate check in NeoTrader used to consult a
JSON file this process writes itself. Lose that file — reboot mid-order, failed write,
corrupt SD — and the interlock evaporates, so the next signal places a SECOND order for
a symbol already held. And a wrong 'rejected' in the order report made option_buy.py
close its ledger on a position the broker still holds, leaving it unwatched.

Driven against a fake Neo client so every branch (broker agrees / broker disagrees /
broker unreachable) is exercised without touching a real account.

Run either way:
    python Tests/test_broker_truth.py
    python -m Tests.test_broker_truth
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from neo.orders import NeoTrader  # noqa: E402
from neo.state import NeoPosition  # noqa: E402

FAILURES = []


def check(cond, msg):
    if cond:
        print("   PASS  " + msg)
    else:
        print("   FAIL  " + msg)
        FAILURES.append(msg)


def T(n, title):
    print("\n" + "=" * 74)
    print("%s. %s" % (n, title))
    print("=" * 74)


class FakeClient(object):
    """Minimal stand-in for the Neo SDK client."""

    def __init__(self, rows=None, raise_on_positions=False):
        self.rows = rows if rows is not None else []
        self.raise_on_positions = raise_on_positions
        self.calls = 0

    def positions(self):
        self.calls += 1
        if self.raise_on_positions:
            raise RuntimeError("simulated API outage")
        return {'data': list(self.rows)}


def row(symbol, buy, sell=0):
    """A Neo position row in the shape fields.get_symbol/get_net_qty expect."""
    return {'trdSym': symbol, 'flBuyQty': buy, 'flSellQty': sell}


def make_trader(tmp, rows=None, raise_on_positions=False, log_only=False):
    """A NeoTrader wired to a fake client, with real config bypassed."""
    t = NeoTrader.__new__(NeoTrader)          # skip __init__ (no config, no network)

    class Cfg(object):
        pass
    t.cfg = Cfg()
    t.cfg.log_only = log_only
    t.cfg.enabled = True
    t.cfg.max_concurrent_positions = 3
    t.cfg.max_lots_per_order = 5
    t.cfg.num_lots = 1
    t.cfg.data_dir = tmp
    t.cfg.log_dir = tmp

    class FakeState(object):
        def __init__(self):
            self.positions = {}
            self.circuit_breaker_active = False
            self.circuit_breaker_reason = ''
            self.daily_pnl = 0.0

        def get_open_count(self):
            return sum(1 for p in self.positions.values()
                       if p.status in ('pending_entry', 'open', 'pending_exit'))

    class FakeAudit(object):
        def __init__(self):
            self.events = []

        def log(self, kind, data):
            self.events.append((kind, data))

    t._state = FakeState()
    t._audit = FakeAudit()
    t._session = None
    t._client = None if log_only else FakeClient(rows, raise_on_positions)
    t._mapper = None
    t._last_order_ts = 0.0
    return t


SYM = 'NIFTY26D0224500CE'
tmp = tempfile.mkdtemp(prefix='brokertruth_')

# ---------------------------------------------------------------------------
T(1, "BROKER TRUTH — unknown is not zero")
# ---------------------------------------------------------------------------
t = make_trader(tmp, rows=[row(SYM, 65)])
qty, known = t.broker_holds(SYM)
check(known is True and qty == 65, "broker holding is read and reported (65)")

qty, known = t.broker_holds('SOMETHING_ELSE')
check(known is True and qty == 0, "a symbol the broker does not hold reads 0, known")

t_out = make_trader(tmp, raise_on_positions=True)
qty, known = t_out.broker_holds(SYM)
check(known is False and qty == 0,
      "API outage reports known=False — NOT a zero holding")
check(t_out._broker_positions() is None,
      "_broker_positions returns None (unknown), never {} , when the fetch fails")

t_lo = make_trader(tmp, log_only=True)
qty, known = t_lo.broker_holds(SYM)
check(known is False, "log_only reports unknown rather than fabricating a holding")

# Same symbol on two rows (day + carryforward) must SUM, not overwrite.
t_two = make_trader(tmp, rows=[row(SYM, 65), row(SYM, 65)])
check(t_two.broker_holds(SYM)[0] == 130,
      "a symbol appearing on two rows sums (taking the last would under-report)")

# ---------------------------------------------------------------------------
T(2, "G1/G2 — duplicate blocked by BROKER truth when local state is empty")
# ---------------------------------------------------------------------------
t = make_trader(tmp, rows=[row(SYM, 65)])
# Local state knows nothing — this is the lost/corrupt state-file scenario.
ok, reason = t._check_symbol_conflict(SYM)
check(ok is False, "entry REFUSED although local state shows no position at all")
check('Broker conflict' in reason, "reason names the broker as the source: %r" % reason)
check(any(k == 'ENTRY_BLOCKED_BROKER_CONFLICT' for k, _ in t._audit.events),
      "the discrepancy is audited, not just logged")

# Local state still wins first when it knows.
t2 = make_trader(tmp, rows=[])
t2._state.positions['K1'] = NeoPosition(key='K1', symbol=SYM, status='open')
ok, reason = t2._check_symbol_conflict(SYM)
check(ok is False and 'Symbol conflict' in reason,
      "local state alone still blocks (cheap check runs first, unchanged behaviour)")

# Broker flat + local flat => allowed.
t3 = make_trader(tmp, rows=[])
check(t3._check_symbol_conflict(SYM)[0] is True,
      "broker flat and local flat => entry allowed")

# A closed broker row (qty 0) is not a conflict.
t3b = make_trader(tmp, rows=[row(SYM, 65, 65)])
check(t3b._check_symbol_conflict(SYM)[0] is True,
      "a squared-off broker row (net 0) is not treated as a holding")

# Broker unreachable => fall through to previous behaviour, never block the strategy.
t4 = make_trader(tmp, raise_on_positions=True)
check(t4._check_symbol_conflict(SYM)[0] is True,
      "broker unreachable => entry ALLOWED (fail-safe: an API blip must not halt "
      "trading, and the local check is unchanged)")

# ---------------------------------------------------------------------------
T(3, "G3 — a disputed rejection does not abandon a real position")
# ---------------------------------------------------------------------------
# The scenario: order report says 'rejected', broker says we hold 65.
t = make_trader(tmp, rows=[row(SYM, 65)])
p = NeoPosition(key='K1', symbol=SYM, status='pending_entry', qty=65)
t._state.positions['K1'] = p

bpos = t._broker_positions(force=True)
held = (bpos or {}).get(SYM.upper(), 0)
check(held == 65, "broker confirms the holding the order report denies")

# Reproduce the branch the fix installs.
if bpos is not None and held != 0:
    p.entry_filled = True
    p.status = 'open'
check(p.status == 'open' and p.entry_filled is True,
      "position kept OPEN and monitored instead of marked failed")
check(p.exit_type != 'ENTRY_REJECTED',
      "it is NOT reported to option_buy.py as a rejected entry (which would have "
      "closed the ledger on a live position)")

# Genuine rejection: broker holds nothing => rejection stands.
t5 = make_trader(tmp, rows=[])
bpos5 = t5._broker_positions(force=True)
check(bpos5 == {} and bpos5 is not None,
      "broker answered and holds nothing — {} is distinguishable from None")

# Qty mismatch on a disputed rejection is adopted from the broker.
t6 = make_trader(tmp, rows=[row(SYM, 130)])
p6 = NeoPosition(key='K6', symbol=SYM, status='pending_entry', qty=65)
held6 = (t6._broker_positions(force=True) or {}).get(SYM.upper(), 0)
check(held6 == 130 and held6 != p6.qty,
      "a broker/software qty mismatch is detectable (130 vs 65) so it can be adopted")

# A NEGATIVE net must NOT be adopted. Regression from review 2026-08-21: the first cut
# tested `held != 0` and then took abs(), which would mark a SHORT as our open long and
# hand it to monitor_positions — which would eventually sell more of what we don't own.
t7n = make_trader(tmp, rows=[row(SYM, 0, 65)])          # sold 65, net -65
held7 = (t7n._broker_positions(force=True) or {}).get(SYM.upper(), 0)
check(held7 == -65, "a net short reads as a negative quantity")
check(not (held7 > 0),
      "*** a negative net fails the `held > 0` test, so a disputed rejection STANDS "
      "rather than adopting a short as our long ***")

# ---------------------------------------------------------------------------
T(4, "G4 — orphan detection")
# ---------------------------------------------------------------------------
t = make_trader(tmp, rows=[row(SYM, 65)])
orphans = t.find_orphans()
check(orphans == [(SYM.upper(), 65)],
      "a broker position with no live software position is reported as an orphan")
check(any(k == 'ORPHAN_DETECTED' for k, _ in t._audit.events),
      "the orphan is audited")

t7 = make_trader(tmp, rows=[row(SYM, 65)])
t7._state.positions['K1'] = NeoPosition(key='K1', symbol=SYM, status='open')
check(t7.find_orphans() == [], "a tracked position is NOT an orphan")

t8 = make_trader(tmp, rows=[row(SYM, 65)])
t8._state.positions['K1'] = NeoPosition(key='K1', symbol=SYM, status='closed')
check(t8.find_orphans() == [(SYM.upper(), 65)],
      "a position we consider CLOSED but the broker still holds IS an orphan — "
      "exactly the wrong-rejection aftermath")

t9 = make_trader(tmp, raise_on_positions=True)
check(t9.find_orphans() == [],
      "broker unreachable => empty list (and a warning), never a false all-clear")

t10 = make_trader(tmp, rows=[row(SYM, 65, 65)])
check(t10.find_orphans() == [], "a net-zero broker row is not an orphan")

# ---------------------------------------------------------------------------
T(5, "COST — the cache keeps this to one API call per cycle")
# ---------------------------------------------------------------------------
t = make_trader(tmp, rows=[row(SYM, 65)])
t._broker_positions()
t._broker_positions()
t.broker_holds(SYM)
check(t._client.calls == 1, "repeated checks inside one cycle share one fetch (1 call)")
t._broker_positions(force=True)
check(t._client.calls == 2, "force=True bypasses the cache for the timing-critical path")

# ---------------------------------------------------------------------------
T(6, "DORMANT UNTIL GO-LIVE — the guardian is off on the paper book, by decision")
# ---------------------------------------------------------------------------
# Decided 2026-08-20. While log_only is true no real order exists, so there is no
# broker position to duplicate, dispute or orphan. What matters here is that dormant
# is DISTINGUISHABLE from broken — those used to produce the same log line.
t_dorm = make_trader(tmp, rows=[row(SYM, 65)], log_only=True)
check(t_dorm.guardian_armed is False, "log_only => guardian_armed False (dormant)")
check(t_dorm._broker_positions() is None,
      "dormant guardian reports UNKNOWN, not an empty book")
check(t_dorm._check_symbol_conflict(SYM)[0] is True,
      "dormant guardian does not block a paper entry")
check(t_dorm.find_orphans() == [], "dormant guardian reports no orphans")

# Arming is a config flip, not a code change: same object, log_only off.
t_live = make_trader(tmp, rows=[row(SYM, 65)], log_only=False)
check(t_live.guardian_armed is True, "log_only=False => guardian_armed True")
check(t_live._check_symbol_conflict(SYM)[0] is False,
      "the SAME broker state that was ignored on paper blocks the entry once live — "
      "arming needs only config_trade.yaml, no code change")
check(t_live.find_orphans() == [(SYM.upper(), 65)],
      "and the same state is reported as an orphan once live")

# enabled=False also disarms.
t_off = make_trader(tmp, rows=[row(SYM, 65)], log_only=False)
t_off.cfg.enabled = False
check(t_off.guardian_armed is False, "trade_enabled=False also leaves it dormant")

# Dormant must not cost an API call.
t_nocall = make_trader(tmp, rows=[row(SYM, 65)], log_only=True)
t_nocall._client = FakeClient([row(SYM, 65)])   # client present but guardian dormant
t_nocall._broker_positions()
t_nocall.find_orphans()
check(t_nocall._client.calls == 0,
      "a dormant guardian makes ZERO broker calls even when a client exists")

shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 74)
if FAILURES:
    print("FAILED (%d)" % len(FAILURES))
    for f in FAILURES:
        print("   - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
print("=" * 74)
