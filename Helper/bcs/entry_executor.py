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

#: The most the FILL debit may exceed the debit the decision was gated on,
#: expressed as the slippage this file already intends to pay: two legs, each
#: crossing by ENTRY_SLIPPAGE_TICKS.
#:
#: Anything past that is the MARKET having moved, not our buffer. The signal
#: was vetted and gated (debit/width <= 45%, entry cost <= 15%) against a book
#: that no longer exists, and CLAUDE.md's pre-entry checklist is explicit that
#: alert pricing decays -- ASHOKLEY read 33% at signal and 40.5% the next
#: morning, a hard fail. Without this the executor pays whatever the new book
#: says and opens a trade the gates would have rejected.
DEBIT_SLIP_ALLOWANCE = 2 * ENTRY_SLIPPAGE_TICKS


def prospective_debit(kite, exchange: str, long_symbol: str,
                      short_symbol: str):
    """What this spread would cost RIGHT NOW, at the prices we would pay.

    ASK on the long plus the buffer, BID on the short minus it -- the same
    numbers `open_leg` is about to send. None when either book is unusable,
    which the caller must treat as "do not enter" rather than "no limit".
    """
    try:
        ld = sm.get_option_depth(kite, exchange, long_symbol)
        sd = sm.get_option_depth(kite, exchange, short_symbol)
    except Exception:
        return None
    lp = _leg_price(ld, True, ENTRY_SLIPPAGE_TICKS)
    sp = _leg_price(sd, False, ENTRY_SLIPPAGE_TICKS)
    if lp is None or sp is None:
        return None
    return round(lp - sp, 2)


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

    Returns the fill dict on a FULL fill, and `{'partial': True, ...}` when
    the broker filled some of it. Never None for a fill of any size.

    The earlier version accepted only `status == 'COMPLETE'` and returned None
    for everything else, on the stated reasoning that "the order is one lot, so
    it fills or it does not". That is FALSE: a lot is hundreds of shares and
    NSE fills partially. `wait_for_fill` documents returning a CANCELLED order
    carrying `filled_quantity > 0`, and those shares are held at the broker --
    so the caller was told "did not fill, nothing extra held" about a real
    position.
    """
    say = log or sm.log
    for attempt in range(1, ENTRY_MAX_ATTEMPTS + 1):
        # Re-checked every attempt, like the close path. Entries get the
        # NORMAL cutoff and never the urgent one -- there is no entry worth
        # placing at 15:24.
        # `sm.now_ist()`, not `datetime.now()`: LAST_ORDER_TIME is an IST
        # time-of-day, and comparing the BOX clock against it is the
        # `is_spread_settled` shape. On a UTC box, box time during Indian
        # market hours is 03:45-10:00, so this cutoff would NEVER fire and a
        # retry loop could place an entry order past 15:25 IST.
        if sm.now_ist().time() > sm.LAST_ORDER_TIME:
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
        part = _partial(result, qty)
        if part:
            # Some of it IS held. Stop here rather than retry: the remainder
            # would be a second order against a book that just proved it could
            # not absorb one, and the caller needs to hear about an odd-sized
            # position more than it needs the rest of the lot.
            say(f"    PARTIAL FILL {part['filled_quantity']}/{qty} "
                f"{symbol} @ {part.get('average_price')} — stopping")
            return part

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
        part = _partial(final, qty)
        if part:
            say(f"    PARTIAL FILL in the cancel race: "
                f"{part['filled_quantity']}/{qty} {symbol}")
            return part

        # THE ORDER MUST BE PROVEN DEAD BEFORE ANOTHER ONE IS PLACED.
        #
        # `cancel_order_safe` SWALLOWS a failed cancel (it logs and returns),
        # and `_order_final_state` returns None when the order book cannot be
        # read -- its own docstring says "callers must treat None as unknown,
        # never as did not fill". Until 2026-08-31 this loop did exactly that:
        # it fell through to the next attempt on None, and on a state that was
        # still OPEN, and placed a SECOND live order against the same leg.
        #
        # The reachable path is not exotic. Kite rate-limits the quote family
        # at 1/sec and this box has logged `Too many requests` for a whole
        # session (2026-08-27); a burst there refuses the cancel AND the
        # follow-up `orders()` read. The first order then fills minutes later,
        # next to the retry's fill: two lots short against one lot long, i.e.
        # a NET NAKED SHORT -- the precise exposure the long-first sequencing
        # exists to make impossible, and the Feb-2026 shape at entry instead
        # of exit.
        #
        # So: retry ONLY on a terminal status carrying no fill. Anything else
        # stops the run and is reported as UNKNOWN, which the caller must not
        # describe as "nothing extra held".
        status = str((final or {}).get('status', '')).upper()
        # A REJECTED ORDER IS RETRIED, AND THAT CONTRADICTS THE PROJECT'S
        # OWN ERROR TABLE. Flagged 2026-09-01, deliberately NOT changed here.
        #
        # `status in _TERMINAL_ORDER_STATUS and status != 'COMPLETE'` admits
        # REJECTED, so attempt 2 places a second order after an RMS,
        # price-band or margin refusal. CLAUDE.md's error table says "Order
        # rejected -> log error, do NOT retry automatically".
        #
        # BOTH readings are defensible and the choice is the owner's:
        #   * retry -- `open_leg` RE-QUOTES and re-prices on each attempt, so a
        #     price-band rejection at a stale limit is genuinely retryable, and
        #     one-lot-at-a-time re-pricing is this path's whole design;
        #   * refuse -- margin and RMS rejections are deterministic and the
        #     second order fails identically, which is what the table assumes.
        #
        # `test_a_confirmed_rejected_order_IS_retried` pins the retry, by name
        # and on purpose, so it is a decision already taken rather than an
        # oversight. Left standing; latent either way while `auto_entry` is
        # false. If it is ever changed, return None (nothing is held) -- a
        # dict would be TRUTHY to `_round` and read as a FILL.
        confirmed_dead = dry_run or (
            status in sm._TERMINAL_ORDER_STATUS and status != 'COMPLETE')
        if not confirmed_dead:
            say(f"    {symbol}: order {order_id} could NOT be confirmed dead "
                f"(status={status or 'unreadable'}). NOT placing another — "
                f"it may still be working at the broker.")
            return {'unknown': True, 'order_id': order_id, 'symbol': symbol,
                    'status': status or 'unreadable', 'filled_quantity': 0}
        say(f"    {symbol}: order {order_id} confirmed {status} with no fill "
            f"— safe to retry")
    return None


def _is_unknown(fill) -> bool:
    """Did the order path lose track of an order it placed?

    Distinct from None (nothing was placed, or it is proven dead with no
    fill) and from a partial (a known quantity is held). UNKNOWN means an
    order may be live at the broker RIGHT NOW, so the run must stop and the
    operator must look -- never "nothing extra held".
    """
    return bool(fill) and bool(fill.get('unknown'))


def _partial(result, qty: int):
    """The order dict when it filled SOME of `qty`, else None.

    A cancelled or rejected order can still carry a fill. Reading only
    `status` throws that away, and the shares are held either way.
    """
    if not result:
        return None
    try:
        filled = int(result.get('filled_quantity') or 0)
    except (TypeError, ValueError):
        return None
    if filled <= 0:
        return None
    out = dict(result)
    out['filled_quantity'] = filled
    # `>= qty` is a FULL fill wearing the wrong status -- an order can be
    # cancelled after it has completely filled. Returning None there (as the
    # first version of this fix did) throws away an entire real leg because a
    # string did not say COMPLETE.
    out['partial'] = filled < qty
    return out


def open_spread(kite, *, stock: str, long_symbol: str, short_symbol: str,
                exchange: str, lot_size: int, lots: int, dry_run: bool,
                trade_id=None, gated_debit: Optional[float] = None,
                log: Optional[Callable] = None,
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
           'partials': [], 'problems': [], 'unknown_orders': []}

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

    # EVERY round is wrapped. `place_limit_order` RE-RAISES on a broker
    # exception (deliberately, so the journal keeps a record), and nothing
    # below used to catch it -- so an exception on round 3 escaped with `out`
    # discarded, taking rounds 1 and 2 with it. Those are real spreads. The
    # caller then logged "executor raised, falling back to the ticket" and
    # recorded nothing: two live positions and an alert naming neither.
    #
    # Whatever happens, this function RETURNS what actually filled.
    for i in range(1, lots + 1):
        try:
            cont = _round(kite, out, i, lots, stock, long_symbol, short_symbol,
                          exchange, lot_size, dry_run, gated_debit, ctx, say)
            # The round RETURNED, so its outcome is known and already recorded
            # structurally (a complete spread, an orphan, a partial, or an
            # unknown order). Only an exception leaves a leg unaccounted for.
            out.pop('in_flight', None)
            if not cont:
                break
        except Exception as e:
            # THE LEGS, not just the prose. `_round` stamps `in_flight` before
            # each order and this is the only reader: whatever it still holds
            # is a leg the order path may have put at the broker without
            # anything recording it. Without this the entry residue was empty,
            # so `_entry_already_in_flight` passed next cycle and the same
            # signal bought ANOTHER long — every cycle, none of them in any
            # store. Quantity 0 means ask the broker.
            out['raised_legs'] = out.pop('in_flight', None) or {}
            out['problems'].append(
                'round %d: the order path raised (%s). Anything it filled '
                'before raising is at the broker and is NOT in this result -- '
                'check Kite.' % (i, e))
            say(f"  Round {i}: EXCEPTION in the order path ({e}). Stopping "
                f"with {out['lots_filled']} complete spread(s). Legs that may "
                f"be live: {', '.join(out['raised_legs']) or 'none identified'}")
            break

    _report(out, stock, lots, dry_run, say, tg)
    return out


def _round(kite, out, i, lots, stock, long_symbol, short_symbol, exchange,
           lot_size, dry_run, gated_debit, ctx, say) -> bool:
    """One round: buy one long, sell one short. False = stop the run."""
    say(f"\n  Round {i}/{lots}")

    # RE-READ the switch, every round. A multi-lot entry places orders over
    # minutes, and `trading_enabled`'s own docstring is that a switch consulted
    # once at startup cannot stop something already running. Reading it once
    # per ENTRY had exactly that defect at a smaller scale.
    if not dry_run:
        allowed, why = entries_allowed(log=say)
        if not allowed:
            out['problems'].append(
                'round %d: stopped mid-entry — %s. %d complete spread(s) are '
                'open and recorded.' % (i, why, out['lots_filled']))
            say(f"  Round {i}: entries disarmed mid-run ({why}). Stopping.")
            return False

    # PRICE, re-checked against what the decision was gated on. The signal
    # passed debit/width and entry-cost against a book that no longer exists.
    if gated_debit is not None:
        now = prospective_debit(kite, exchange, long_symbol, short_symbol)
        cap = gated_debit + DEBIT_SLIP_ALLOWANCE * sm.TICK_SIZE
        if now is None:
            out['problems'].append(
                'round %d: could not price the spread — not entering' % i)
            say(f"  Round {i}: no usable book to price the spread. Stopping.")
            return False
        if now > cap:
            out['problems'].append(
                'round %d: the book moved — %.2f to open vs %.2f gated '
                '(cap %.2f). The signal was vetted at a price that no longer '
                'exists.' % (i, now, gated_debit, cap))
            say(f"  Round {i}: DEBIT MOVED {now:.2f} > cap {cap:.2f} "
                f"(gated at {gated_debit:.2f}). Stopping.")
            return False

    # WHAT IS IN FLIGHT, so an EXCEPTION can still name the legs.
    #
    # THE GAP (found 2026-08-31). `place_limit_order` re-raises on a broker
    # exception (deliberately — the journal keeps the record), and every one of
    # the structured outputs below (`orphan`, `partials`, `unknown_orders`) is
    # assigned AFTER the call that raised. So a round that bought its long and
    # then hit a `TokenException` on the short reported PROSE only: `out` had
    # no `orphan`, `_entry_residue_legs` returned `{}`, `_record_entry_residue`
    # wrote nothing, and `_entry_already_in_flight` therefore found nothing
    # next cycle and sent the SAME signal back to the order path — buying
    # another long, every cycle, none of them in any store, invisible to
    # `capital.check` and to every sweep.
    #
    # That is the amplification the 2026-08-31 `_entry_already_in_flight`
    # guard was built to close, reopened by the one branch whose legs never
    # reached the residue record. Quantity 0 means ASK THE BROKER — the same
    # convention `_auto_enter_bcs`'s own raised-branch already uses.
    out['in_flight'] = {long_symbol: 0}

    # LONG FIRST. The exchange must never see the short unhedged, and a round
    # that dies between the legs then leaves a capped-risk long rather than a
    # naked short.
    lfill = open_leg(kite, exchange, long_symbol, True, lot_size, dry_run,
                     context=ctx('long'), log=say)
    if _is_unknown(lfill):
        # NOT "nothing extra held". The order may be working at the broker and
        # may fill after this function returns, so the run stops here and the
        # operator is pointed at Kite. Recording a spread we cannot prove we
        # hold is the one outcome worse than stopping short.
        out['unknown_orders'] = out.get('unknown_orders', []) + [
            {'round': i, 'leg': 'long', 'symbol': long_symbol,
             'order_id': lfill.get('order_id'), 'status': lfill.get('status')}]
        out['problems'].append(
            'round %d: the LONG order %s could not be confirmed dead or '
            'filled (status %s). It may be LIVE at the broker — check Kite '
            'before entering %s again. %d complete spread(s) are recorded.'
            % (i, lfill.get('order_id'), lfill.get('status'), long_symbol,
               out['lots_filled']))
        say(f"  Round {i}: LONG order state UNKNOWN. Stopping.")
        return False
    if lfill and lfill.get('partial'):
        _note_partial(out, i, long_symbol, lfill, lot_size)
        say(f"  Round {i}: partial LONG. Stopping.")
        return False
    if not lfill:
        out['problems'].append(
            'round %d: long leg did not fill — stopping with %d complete '
            'spread(s), nothing extra held' % (i, out['lots_filled']))
        say(f"  Round {i}: long did not fill. Stopping.")
        return False

    # The long is now HELD, at a known size. If the short raises, both facts
    # have to survive: the long we can prove, and the short we cannot.
    out['in_flight'] = {long_symbol: lot_size, short_symbol: 0}

    sfill = open_leg(kite, exchange, short_symbol, False, lot_size, dry_run,
                     context=ctx('short'), log=say)
    if _is_unknown(sfill):
        # The long IS held, and the short's fate is unknown, so this is at
        # least an orphan long and possibly a complete spread. Report BOTH
        # facts and stop; do not guess which, and never place the short again.
        out['orphan'] = {'symbol': long_symbol, 'qty': lot_size,
                         'fill': lfill.get('average_price')}
        out['unknown_orders'] = out.get('unknown_orders', []) + [
            {'round': i, 'leg': 'short', 'symbol': short_symbol,
             'order_id': sfill.get('order_id'), 'status': sfill.get('status')}]
        out['problems'].append(
            'round %d: the LONG filled and the SHORT order %s could not be '
            'confirmed dead or filled (status %s). Holding %d x %s, and the '
            'short MAY also be live — check Kite before doing anything else.'
            % (i, sfill.get('order_id'), sfill.get('status'), lot_size,
               long_symbol))
        say(f"  Round {i}: SHORT order state UNKNOWN against a filled long. "
            f"Stopping.")
        return False
    if sfill and sfill.get('partial'):
        # Worse than an orphan long: the round holds a FULL long and a PARTIAL
        # short, so it is neither a spread nor a clean single leg.
        _note_partial(out, i, short_symbol, sfill, lot_size)
        out['orphan'] = {'symbol': long_symbol, 'qty': lot_size,
                         'fill': lfill.get('average_price')}
        say(f"  Round {i}: partial SHORT against a full long. Stopping.")
        return False
    if not sfill:
        # The dangerous-looking case, and the safe one to be in. We hold one
        # more long than we wanted. NOT unwound -- see the module docstring.
        out['orphan'] = {'symbol': long_symbol, 'qty': lot_size,
                         'fill': lfill.get('average_price')}
        out['problems'].append(
            'round %d: short leg did not fill after the long DID. Holding '
            '%d x %s that is NOT part of any recorded spread.'
            % (i, lot_size, long_symbol))
        say(f"  Round {i}: *** ORPHAN LONG *** {lot_size} x {long_symbol} — "
            f"not unwound, not recorded. Manual decision needed.")
        return False

    # NOTHING IS IN FLIGHT ANY MORE — cleared HERE, not only in the caller.
    #
    # `open_spread` pops it after `_round` returns, which covers every normal
    # return. What it does not cover is a raise BETWEEN the round completing
    # and the return: the trailing `say(...)` below is a log call, and a
    # failing log handler would leave `in_flight` set on a round that has
    # already been counted in `lots_filled` and recorded by
    # `mark_entered_bcs`. The exception handler would then file those same
    # legs as residue — an incident naming legs the record already accounts
    # for, which can never self-resolve while the position is open, because
    # the sweep tests for symbol-flat at the broker.
    out.pop('in_flight', None)
    out['long_fills'].append(lfill.get('average_price'))
    out['short_fills'].append(sfill.get('average_price'))
    out['lots_filled'] += 1
    say(f"  Round {i}: spread complete (long {lfill.get('average_price')} / "
        f"short {sfill.get('average_price')})")
    return True


def _note_partial(out, i, symbol, fill, lot_size) -> None:
    """Record an odd-sized leg. It is HELD, whatever the status said."""
    n = fill.get('filled_quantity')
    out['partials'].append({'symbol': symbol, 'qty': n,
                            'fill': fill.get('average_price'), 'round': i})
    out['problems'].append(
        'round %d: PARTIAL fill %s x %s of a %s lot. Those shares are held '
        'and are NOT part of any recorded spread.' % (i, n, symbol, lot_size))


def _report(out: dict, stock: str, lots: int, dry_run: bool, say, tg) -> None:
    """Say what actually happened, loudly when it is not what was asked for.

    A partial entry that only appears in a log line is the same failure as an
    unmonitored position: the record and the world disagree and nobody is
    told.
    """
    n = out['lots_filled']
    tag = '[DRY RUN] ' if dry_run else ''
    unknown = out.get('unknown_orders') or []
    # No `not out['partials']` clause: a partial always stops the run, so it
    # can never coexist with a full lots_filled. A mutation proved the extra
    # test was unreachable, and an unobservable guard is decorative.
    #
    # `unknown` IS listed, because unlike a partial it does not imply an
    # incomplete count: an order whose fate we never learned can leave the
    # requested number of spreads filled AND an extra leg live at the broker.
    # A run in that state must never print COMPLETE.
    if n == lots and not out['orphan'] and not unknown:
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
    for u in unknown:
        lines.append(
            "ORDER STATE UNKNOWN: round %s %s leg %s, order %s (%s). It was "
            "NOT retried and may be LIVE at the broker. Check Kite before "
            "placing anything on this symbol."
            % (u.get('round'), u.get('leg'), u.get('symbol'),
               u.get('order_id'), u.get('status')))
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
