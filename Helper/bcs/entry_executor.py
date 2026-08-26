"""Open a bull call spread with real orders — Phase 3, and NOT armed.

Everything in this file is new order-placing code, which is the category
`feedback_live_automation_bar` exists for: this book has lost money to monitor
bugs twice, both at market open, and both on the EXIT side. Entries are the
side with no incidents yet, which is a reason for care rather than confidence.

Three rules shape the whole design, and they all follow from one asymmetry:
**a missed entry costs nothing and a bad one costs capital.**

1. **Entries are never urgent.** The close path has an `urgent` mode that will
   pay through an unreliable book on its final attempt, because a stop that
   does not execute is unbounded. Nothing here does that. An entry that cannot
   get a reliable book at a price it likes simply does not happen, and the
   signal is re-evaluated next cycle.

2. **One lot per order, and the spread is completed each round.** Not "all the
   longs, then all the shorts": that holds N naked longs in the middle, and a
   short leg that then cannot fill leaves a position nobody chose. Round `i`
   buys one long and sells one short, so stopping after round k leaves **k
   complete spreads** — smaller than intended, but exactly the shape the
   monitor knows how to manage. Free on Neo (no per-order brokerage), which is
   what makes the safe ordering also the cheap one.

3. **Long leg first, inside each round.** The house margin rule: buying the
   protective long before selling the short means the exchange never sees a
   naked short, so no margin spike. A round that fails between the two legs
   therefore leaves a LONG, which is capped-risk and can be held safely — the
   failure mode that direction produces is the survivable one.

What this file will NOT do
--------------------------
**It never unwinds.** If a round buys the long and cannot sell the short, the
orphan long is REPORTED and left alone. Placing a corrective order through a
book that has just failed to fill, using the same code that produced the
problem, is the amplification that turned a Feb-2026 stop into a four-fill
loss. A long call is capped risk and not urgent; the owner decides.

**It never retries the whole spread.** Partial means partial. The caller
records what actually filled.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable, Optional

from bcs import spread_monitor as sm

#: Ticks of price improvement offered to get filled. Entry pays the ask and
#: receives the bid, so the buffer crosses the touch rather than sitting at it.
#: Deliberately the same base the close path uses; it does NOT escalate,
#: because escalating on an entry is chasing.
ENTRY_SLIPPAGE_TICKS = sm.SLIPPAGE_TICKS_BASE

#: Attempts per leg. Lower than the close path's, on purpose: there, running
#: out of attempts leaves a live position unhedged, so it tries hard. Here,
#: running out means no trade.
ENTRY_MAX_ATTEMPTS = 2


def entries_allowed(log: Optional[Callable] = None) -> tuple:
    """(allowed, why_not). FAILS CLOSED — the opposite of the kill switch.

    `sm.trading_enabled()` fails OPEN by design, because it guards a path that
    only ever CLOSES positions: a config error there must not abandon the stops
    on a live book. That reasoning **inverts completely** for entries. A
    missing or malformed config must never be able to start placing orders, so
    this gate requires an explicit boolean `true` and treats everything else,
    including its own failure to read the config, as NO.

    Both conditions are required. The kill switch is still a stop button for
    entries — anyone who disarms trading expects it to stop *all* orders, and a
    switch that only half-works is worse than one that does not exist.
    """
    say = log or (lambda m: None)
    try:
        from zebra import config as zcfg
        auto = zcfg.AUTO_ENTRY
    except Exception as e:
        say(f"  ENTRY BLOCKED: could not read the auto-entry switch ({e}) — "
            f"failing CLOSED")
        return False, 'auto-entry config unreadable'
    if not auto:
        return False, 'auto_entry is off'
    if not sm.trading_enabled():
        return False, 'the kill switch is disarmed'
    return True, ''


def _leg_price(depth: dict, is_buy: bool, ticks: int) -> Optional[float]:
    """Limit price for one entry leg, or None if the book cannot support one.

    Buying pays ASK + buffer, selling receives BID - buffer. Both cross the
    touch by the same amount, so the debit this produces is the FILL-basis
    debit the gates were measured on plus a known, bounded slippage — not a
    mid-price fiction.
    """
    px = depth.get('ask') if is_buy else depth.get('bid')
    if not px or px <= 0:
        return None
    buf = ticks * sm.TICK_SIZE
    return sm.round_to_tick(px + buf if is_buy else px - buf)


def open_leg(kite, exchange: str, symbol: str, is_buy: bool, qty: int,
             dry_run: bool, context: Optional[dict] = None,
             log: Optional[Callable] = None) -> Optional[dict]:
    """Open ONE leg of ONE lot. Returns the fill dict, or None if it did not.

    Deliberately NOT `close_leg`. That function computes how much is left to do
    by reading the current position (`remaining = max(current_qty, 0)` for a
    sell), which is meaningful when unwinding something and meaningless when
    building from flat — a sell-to-open would compute zero remaining and report
    "already flat, nothing to close". Reusing it would have been a silent
    no-op, so this is a separate, much smaller function built from the same
    primitives.

    Returns None for every failure. No partial-fill bookkeeping: the order is
    one lot, so it fills or it does not.
    """
    say = log or sm.log
    for attempt in range(1, ENTRY_MAX_ATTEMPTS + 1):
        # Re-checked every attempt, like the close path. Entries get the
        # NORMAL cutoff and never the urgent one -- there is no entry worth
        # placing at 15:24.
        if datetime.now().time() > sm.LAST_ORDER_TIME:
            say(f"    ENTRY CUTOFF: past "
                f"{sm.LAST_ORDER_TIME.strftime('%H:%M')} — not opening {symbol}")
            return None

        try:
            depth = sm.get_option_depth(kite, exchange, symbol)
        except Exception as e:
            say(f"    {symbol}: quote failed ({e}) — attempt {attempt}")
            time.sleep(sm.POLL_INTERVAL_SEC)
            continue

        ok, why = sm.leg_quote_reliable(depth)
        if not ok:
            # No `urgent` escape hatch, unlike the close path. An unreliable
            # book is a reason not to open at all.
            say(f"    {symbol}: book not reliable ({why}) — not entering")
            time.sleep(sm.POLL_INTERVAL_SEC)
            continue

        price = _leg_price(depth, is_buy, ENTRY_SLIPPAGE_TICKS)
        if price is None or price <= 0:
            say(f"    {symbol}: no usable {'ask' if is_buy else 'bid'} — "
                f"not entering")
            return None

        txn = 'BUY' if is_buy else 'SELL'
        say(f"    Attempt {attempt}/{ENTRY_MAX_ATTEMPTS}: {txn} {symbol} x{qty} "
            f"@ {price} (bid {depth.get('bid')} / ask {depth.get('ask')})")
        order_id = sm.place_limit_order(kite, exchange, symbol, txn, qty,
                                        price, dry_run, context=context)
        if not order_id:
            return None

        result = sm.wait_for_fill(kite, order_id, dry_run)
        if result and result.get('status') == 'COMPLETE':
            say(f"    FILLED at {result.get('average_price')} | order {order_id}")
            return result

        # Timed out. The order is STILL LIVE -- `wait_for_fill` does not
        # cancel. Leaving it and placing another is exactly how the Feb-2026
        # short leg got bought four times.
        say(f"    {symbol}: not filled — cancelling {order_id} before retrying")
        sm.cancel_order_safe(kite, order_id, dry_run)
        final = sm._order_final_state(kite, order_id) if not dry_run else None
        if final and final.get('status') == 'COMPLETE':
            # Filled in the cancel race. It is a real position now.
            say(f"    {symbol}: filled during cancel at "
                f"{final.get('average_price')}")
            return final
    return None


def open_spread(kite, *, stock: str, long_symbol: str, short_symbol: str,
                exchange: str, lot_size: int, lots: int, dry_run: bool,
                trade_id=None, log: Optional[Callable] = None,
                telegram: Optional[Callable] = None) -> dict:
    """Open `lots` lots as `lots` complete one-lot spreads. Never raises.

    Returns:
      lots_filled  -- COMPLETE spreads established. This is the number to
                      record; anything else describes a position we do not
                      have.
      orphan       -- {'symbol', 'qty', 'fill'} when a round bought its long
                      and could not sell its short. Reported, never unwound.
      problems     -- human-readable, in order.

    `lots_filled == 0` means nothing was established and (absent an orphan)
    nothing is held.
    """
    say = log or sm.log
    tg = telegram or sm.send_telegram
    out = {'stock': stock, 'lots_requested': lots, 'lots_filled': 0,
           'long_fills': [], 'short_fills': [], 'orphan': None,
           'problems': []}

    allowed, why = entries_allowed(log=say)
    if not allowed and not dry_run:
        out['problems'].append('entry not allowed: %s' % why)
        say(f"  ENTRY REFUSED for {stock}: {why}")
        return out

    if lots <= 0 or lot_size <= 0:
        out['problems'].append('nothing to do (lots=%s lot_size=%s)'
                               % (lots, lot_size))
        return out

    say("")
    say("=" * 70)
    say(f"  OPENING BCS {stock}: {lots} lot(s) x {lot_size} as "
        f"{lots} one-lot spread(s){'  [DRY RUN]' if dry_run else ''}")
    say(f"  long  BUY  {long_symbol}")
    say(f"  short SELL {short_symbol}")
    say("=" * 70)

    def ctx(leg):
        return {'trade_id': trade_id, 'stock': stock, 'strategy': 'BCS',
                'reason': 'ENTRY', 'leg': leg, 'round': out['lots_filled'] + 1}

    for i in range(1, lots + 1):
        say(f"\n  Round {i}/{lots}")
        # LONG FIRST. The exchange must never see the short unhedged, and a
        # round that dies between the legs then leaves a capped-risk long
        # rather than a naked short.
        lfill = open_leg(kite, exchange, long_symbol, True, lot_size, dry_run,
                         context=ctx('long'), log=say)
        if not lfill:
            out['problems'].append(
                'round %d: long leg did not fill — stopping with %d complete '
                'spread(s), nothing extra held' % (i, out['lots_filled']))
            say(f"  Round {i}: long did not fill. Stopping.")
            break

        sfill = open_leg(kite, exchange, short_symbol, False, lot_size,
                         dry_run, context=ctx('short'), log=say)
        if not sfill:
            # The dangerous-looking case, and the safe one to be in. We hold
            # one more long than we wanted. NOT unwound -- see the module
            # docstring.
            out['orphan'] = {'symbol': long_symbol, 'qty': lot_size,
                             'fill': lfill.get('average_price')}
            out['problems'].append(
                'round %d: short leg did not fill after the long DID. '
                'Holding %d x %s that is NOT part of any recorded spread.'
                % (i, lot_size, long_symbol))
            say(f"  Round {i}: *** ORPHAN LONG *** {lot_size} x {long_symbol} "
                f"— not unwound, not recorded. Manual decision needed.")
            break

        out['long_fills'].append(lfill.get('average_price'))
        out['short_fills'].append(sfill.get('average_price'))
        out['lots_filled'] += 1
        say(f"  Round {i}: spread complete "
            f"(long {lfill.get('average_price')} / "
            f"short {sfill.get('average_price')})")

    _report(out, stock, lots, dry_run, say, tg)
    return out


def _report(out: dict, stock: str, lots: int, dry_run: bool, say, tg) -> None:
    """Say what actually happened, loudly when it is not what was asked for.

    A partial entry that only appears in a log line is the same failure as an
    unmonitored position: the record and the world disagree and nobody is
    told.
    """
    n = out['lots_filled']
    tag = '[DRY RUN] ' if dry_run else ''
    if n == lots and not out['orphan']:
        say(f"\n  {tag}ENTRY COMPLETE: {n}/{lots} lot(s) {stock}")
        return
    say(f"\n  *** {tag}ENTRY INCOMPLETE: {n}/{lots} lot(s) {stock} ***")
    for prob in out['problems']:
        say(f"    - {prob}")
    lines = [f"{tag}BCS ENTRY INCOMPLETE {stock}: {n}/{lots} lot(s) filled."]
    lines += ['- ' + p for p in out['problems']]
    if out['orphan']:
        lines.append("An UNHEDGED LONG is open and was deliberately not "
                     "unwound. Decide manually.")
    if n:
        lines.append(f"The {n} complete spread(s) ARE a valid position and "
                     f"will be recorded and monitored.")
    tg('\n'.join(lines))


def entry_debit(out: dict) -> Optional[float]:
    """Average per-share debit actually paid across the completed spreads.

    From the FILLS, never from the quote the decision was made on. The stops
    and the trail are all derived from the entry debit, so recording the
    intended price instead of the paid one puts every level slightly under
    the position — the same class of error as valuing an exit at mid.
    """
    if not out['lots_filled']:
        return None
    pairs = list(zip(out['long_fills'], out['short_fills']))
    if not pairs or any(a is None or b is None for a, b in pairs):
        return None
    return round(sum(a - b for a, b in pairs) / len(pairs), 2)
