"""Portfolio capital limits and position sizing — everything anchored to the
CAPITAL, so the book compounds instead of being re-tuned by hand.

Why this exists
---------------
Phase 2 of the go-live plan is capital allocation, and the honest description
of what was here before is: almost nothing. `max_open_trades = 8` is a COUNT,
not a rupee figure; `_required_capital` / `_funds_line` compute a real exchange
margin and then write it into a Telegram sentence that gates nothing. Nowhere
was there a limit on how much money could be at risk at once.

Measured on the cohort (2026-08-26, 10 entries over 12 sessions): per-position
capital ran Rs 4,515 - 16,316 (median Rs 11,025), and the book reached **7
concurrent positions holding Rs 81,291** on 2026-08-21 — one position under a
cap that would not have stopped it, and no rupee cap anywhere.

Owner, 2026-08-26: *"We shall load 2L as initial and then reserve some per
trade... as the capital grows to say 4L then it can auto go for 2 lots and so
on... so capital based risk and so on + compounding"*, with Rs 25,000 as the
per-trade ceiling and 1 lot for now.

Those three numbers are one coherent scheme, which is why they are stored as
RATIOS and not as three independent figures:

    Rs 2,00,000 capital  /  8 slots  =  Rs 25,000 each  =  12.5% of capital

Store the ratio and the whole thing scales together on one number. Store the
rupees and they drift apart the first time capital changes, silently — 8 slots
at a stale Rs 25,000 against Rs 4,00,000 is a book running at half size with
nothing announcing it.

What capital MEANS for this book
--------------------------------
The debit. A long vertical is paid in full at entry and the debit is also the
maximum loss, so it is simultaneously the cash cost, the risk and the honest
planning number. `basket_order_margins` can report LESS on a hedged pair, never
more — so sizing against the debit is conservative in the direction that
matters and needs no broker call.

Compounding is OFF until it is armed
------------------------------------
`compound` adds realised net P&L to the base capital, which is what makes size
grow on its own. It defaults FALSE and the digest reports what it WOULD be,
for the same reason every other control in this system shipped alert-only
first: a number that moves position size should be watched before it is
believed. Lots only step at whole multiples of `capital_per_lot`, so this is a
slow-moving quantity either way — which is a reason to be relaxed about the
arithmetic, not a reason to skip watching it.
"""
from __future__ import annotations

from typing import List, NamedTuple, Optional

from . import config as cfg


class Limits(NamedTuple):
    """The resolved numbers one decision is made against.

    Passed explicitly rather than read from `cfg` inside every check, so a
    decision can be replayed later against the limits that were actually in
    force -- and so tests state their scenario instead of monkeypatching a
    module.
    """
    capital: float
    basis: str              # 'base' or 'compounded (base + realised)'
    max_open: int
    max_per_stock: int
    max_lots: int
    max_trade: Optional[float]
    max_deployed: Optional[float]


def realised_pnl(trades: List[dict]) -> tuple:
    """(rupees, n_costed, n_uncosted) of NET realised P&L on closed positions.

    `pnl_net` only -- never `pnl`. Gross P&L overstates the account by the fee
    drag, which on this book is 0.64% proportional plus ~Rs 87/leg, and
    compounding on a number that ignores costs sizes up on money that was
    never there. Records without it are COUNTED, not assumed zero: 179 old
    rows can never be costed at all because no per-leg prices were stored.
    """
    total = 0.0
    costed = uncosted = 0
    for t in trades:
        if t.get('status') != 'exited':
            continue
        v = t.get('pnl_net')
        if v is None:
            uncosted += 1
            continue
        try:
            total += float(v)
            costed += 1
        except (TypeError, ValueError):
            uncosted += 1
    return total, costed, uncosted


def effective_capital(trades: List[dict]) -> tuple:
    """(rupees, basis). The base figure, plus realised P&L when compounding.

    Uncosted closed trades do NOT block compounding -- they are old back-ratio
    rows that predate fee stamping and will never gain it, so refusing on them
    would mean compounding never turns on. They are surfaced by `describe`
    instead, which is the honest treatment: the number is slightly stale, not
    wrong in an unknown direction.
    """
    base = float(cfg.CAPITAL_RUPEES or 0)
    if not cfg.COMPOUND:
        return base, 'base'
    realised, _costed, _uncosted = realised_pnl(trades)
    return base + realised, 'compounded (base + realised)'


def lots_for_capital(capital: float) -> int:
    """Lots per position at this capital. One per `capital_per_lot`.

    Owner's rule: 2L -> 1 lot, 4L -> 2, and so on. Floor, never round: rounding
    up would size a Rs 3.9L account as though it were Rs 4L.

    Floored at 1 and ceilinged at `max_lots_hard`. The floor because a book
    below one unit of capital should be refused by the RUPEE limits with a
    reason, not silently sized to zero lots by the arithmetic. The ceiling
    because a stray zero in `capital_rupees` must not be able to order 50 lots.
    """
    per = float(cfg.CAPITAL_PER_LOT or 0)
    if per <= 0:
        return 1
    return max(1, min(int(capital // per), int(cfg.MAX_LOTS_HARD)))


def limits(trades: List[dict]) -> Limits:
    """Resolve every limit against the capital in force right now."""
    cap, basis = effective_capital(trades)
    return Limits(
        capital=cap,
        basis=basis,
        max_open=cfg.MAX_OPEN_TRADES,
        max_per_stock=cfg.MAX_OPEN_PER_STOCK,
        max_lots=lots_for_capital(cap),
        # A BANKRUPT BOOK IS LIMITED TO ZERO, NOT TO UNLIMITED. `check()`
        # SKIPS any limit that is None, so resolving these to None on
        # `cap <= 0` removed both rupee caps at exactly the moment they should
        # bind hardest. Reachable with COMPOUND on once realised net losses
        # (fees included) reach the base. `lots_for_capital` floors at 1 and
        # its docstring assumes "a book below one unit of capital should be
        # refused by the RUPEE limits" -- the limits that had just vanished.
        max_trade=(cap * cfg.MAX_TRADE_PCT / 100.0) if cap > 0 else 0.0,
        max_deployed=(cap * cfg.MAX_DEPLOYED_PCT / 100.0) if cap > 0 else 0.0,
    )


#: Statuses that are still holding money. `closing` counts: the close lock is
#: taken but the position is not out of the market, and treating it as free
#: capital would let a replacement be sized against money still committed.
#:
#: `partial_close` counts for a STRONGER version of the same reason. It is the
#: state a close is frozen in when a leg failed after orders went out: the
#: position is live at the broker, nothing is monitoring it, and it is waiting
#: on a human. Reading its rupees as FREE was the worst of the three readings —
#: the capital is not merely committed, it is committed to a position whose
#: size nobody currently knows. `max_open_per_stock` counts from this same
#: tuple, so omitting it also stopped a stranded position from blocking a
#: replacement on the same stock.
HOLDING = ('entered', 'closing', 'partial_close')


def position_capital(trade: dict) -> Optional[float]:
    """Rupees this position ties up = debit x quantity.

    None when it cannot be computed, which callers must treat as UNKNOWN and
    never as zero. A record whose cost we cannot read is exactly the one that
    should not be silently added to a total and declared affordable.
    """
    try:
        debit = float(trade['debit'])
    except (KeyError, TypeError, ValueError):
        return None
    qty = trade.get('quantity')
    if qty is None:
        try:
            qty = int(trade['lot_size']) * int(trade.get('lots') or 1)
        except (KeyError, TypeError, ValueError):
            return None
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    return debit * qty


def deployed(trades: List[dict]) -> tuple:
    """(rupees, n_positions, n_unpriced) across everything still holding.

    `n_unpriced` is reported rather than swallowed. A book with unreadable
    records has an understated total, and a budget check that cannot say so is
    reporting a number it does not have.
    """
    total = 0.0
    n = unpriced = 0
    for t in trades:
        if t.get('status') not in HOLDING:
            continue
        n += 1
        c = position_capital(t)
        if c is None:
            unpriced += 1
        else:
            total += c
    return total, n, unpriced


def check(trades: List[dict], candidate: dict,
          lim: Optional[Limits] = None) -> tuple:
    """(ok, reason). False = this position would breach a limit.

    `candidate` needs `stock` and enough of `debit`/`quantity` (or
    `lot_size`+`lots`) to price itself. Pure: no store, no clock, no config
    writes — everything it decides on is an argument or a module constant, so
    it can be tested exhaustively without a book.

    An UNPRICEABLE candidate fails CLOSED. Every other refusal here is about a
    number being too big; this one is about not having the number at all, and
    `feedback_no_rush_to_enter` settles it — a missed entry costs nothing, an
    unqualified one costs capital.
    """
    lim = lim or limits(trades)
    want = position_capital(candidate)
    if want is None:
        return False, ('candidate capital is unknown (debit/quantity missing) '
                       '- refusing rather than sizing against a blank')

    if lim.max_trade is not None and want > lim.max_trade:
        return False, ('one position at Rs %.0f exceeds the per-trade cap '
                       'Rs %.0f (%.1f%% of Rs %.0f capital)'
                       % (want, lim.max_trade, cfg.MAX_TRADE_PCT,
                          lim.capital))

    held, n_open, unpriced = deployed(trades)

    if lim.max_open and n_open >= lim.max_open:
        return False, ('%d positions already open, cap is %d '
                       '(max_open_trades)' % (n_open, lim.max_open))

    if lim.max_per_stock:
        stock = candidate.get('stock')
        same = sum(1 for t in trades
                   if t.get('status') in HOLDING and t.get('stock') == stock)
        if same >= lim.max_per_stock:
            return False, ('%s already has %d open position(s), cap is %d '
                           '(max_open_per_stock)'
                           % (stock, same, lim.max_per_stock))

    if lim.max_deployed is not None:
        if unpriced:
            # The total is understated by an unknown amount, so "held + want
            # fits" is not a fact. Refuse rather than approve against a
            # number we know is incomplete.
            return False, ('%d open position(s) could not be priced, so the '
                           'deployed total Rs %.0f is understated - refusing '
                           'against an incomplete book' % (unpriced, held))
        if held + want > lim.max_deployed:
            return False, ('Rs %.0f deployed + Rs %.0f wanted exceeds '
                           'the book cap Rs %.0f (%.0f%% of Rs %.0f capital)'
                           % (held, want, lim.max_deployed,
                              cfg.MAX_DEPLOYED_PCT, lim.capital))

    return True, ''


def describe(trades: Optional[List[dict]] = None) -> str:
    """One line naming the capital, its basis, and every limit derived from it.

    Logged every cycle on purpose. Nothing about this file is visible from the
    outside until it refuses something, and this system has already shipped two
    controls that were wired in, looked deployed and could never fire. It also
    states whether compounding is ON and how many closed trades could not be
    costed, because a compounded figure standing on a partial P&L is a
    different number from one standing on a complete one.
    """
    trades = trades or []
    lim = limits(trades)
    extra = ''
    if cfg.COMPOUND:
        _r, _costed, uncosted = realised_pnl(trades)
        if uncosted:
            extra = (' | %d closed trade(s) have no pnl_net and are NOT in '
                     'the compounded figure' % uncosted)
    else:
        would, _c, _u = realised_pnl(trades)
        if would:
            extra = (' | compounding OFF: it would be Rs %.0f'
                     % (lim.capital + would))
    return ('CAPITAL Rs %.0f (%s) | %d lot(s)/position | max %s open, %s per '
            'stock | per-trade Rs %.0f (%.1f%%) | book Rs %.0f (%.0f%%)%s'
            % (lim.capital, lim.basis, lim.max_lots,
               lim.max_open or 'unlimited', lim.max_per_stock or 'unlimited',
               lim.max_trade or 0, cfg.MAX_TRADE_PCT,
               lim.max_deployed or 0, cfg.MAX_DEPLOYED_PCT, extra))


# ── Sizing ──────────────────────────────────────────────────────────────────
#
# Owner, 2026-08-26: "sometimes we may not have enough liquidity to trade 5
# lots, so recommend entry 1 lot at a time and ensure entries are correct
# after entry. Keep an eye on overall capital + per-trade capital. As we trade
# in Neo, placing multiple orders won't be a problem -> no brokerage per
# order."
#
# Two separate ideas, and conflating them is how a size becomes a fill nobody
# could get:
#
#   HOW MANY lots the position may be  -- budget AND liquidity, below.
#   HOW they reach the market          -- one lot per order, `SLICE_LOTS`.
#
# The second is free on Neo (no per-order brokerage), which is what makes it
# worth doing: N one-lot orders cost the same as one N-lot order, each fill is
# confirmed before the next goes out, and a book that turns out to be thinner
# than the top-of-book claimed stops the entry part-filled instead of paying
# through. On a per-order-brokerage broker this trade-off reverses.

#: Lots per ORDER, not per position. Always 1 -- see above.
SLICE_LOTS = 1


def liquidity_lots(depth: Optional[dict], lot_size: int) -> Optional[int]:
    """Lots the top of book can absorb, or None when depth is unknown.

    Entry BUYS the long (so it needs size on the ASK) and SELLS the short (so
    it needs size on the BID). The binding side is whichever is thinner.

    Top-of-book only, which is an OVER-estimate of what one order fills at the
    touch and an UNDER-estimate of the whole book. That is the right direction
    to be wrong in only because entry is sliced: each one-lot order re-reads
    the book, so a wrong total costs a part-fill and not a paid-through spread.
    """
    if not depth or not lot_size or lot_size <= 0:
        return None
    try:
        long_avail = int((depth.get('long') or {}).get('ask_qty') or 0)
        short_avail = int((depth.get('short') or {}).get('bid_qty') or 0)
    except (TypeError, ValueError):
        return None
    if long_avail <= 0 or short_avail <= 0:
        return 0
    return min(long_avail, short_avail) // int(lot_size)


def plan(trades: List[dict], candidate: dict,
         depth: Optional[dict] = None,
         lim: Optional[Limits] = None) -> dict:
    """How big this entry may be, and WHICH limit decided it.

    Returns {'lots', 'slice_lots', 'capital', 'bound', 'reason', 'bounds'}.
    `lots == 0` means do not enter; `reason` says why in one line.

    `bounds` carries every limit's own answer, not just the winner. A size is
    a decision, and "3 lots" with no record of what the other four limits said
    cannot be audited after the fact -- which is the same reason the exit path
    persists its book rather than just the price.
    """
    lim = lim or limits(trades)
    one = dict(candidate)
    one['lots'] = 1
    one.pop('quantity', None)
    per_lot = position_capital(one)

    ok, why = check(trades, one, lim)
    if not ok:
        return {'lots': 0, 'slice_lots': SLICE_LOTS, 'capital': 0.0,
                'bound': 'refused', 'reason': why, 'bounds': {}}

    held, _n, _unpriced = deployed(trades)
    bounds = {'max_lots': lim.max_lots}
    if lim.max_trade is not None:
        bounds['max_trade_rupees'] = int(lim.max_trade // per_lot)
    if lim.max_deployed is not None:
        bounds['max_deployed_rupees'] = int(
            max(0.0, lim.max_deployed - held) // per_lot)
    liq = liquidity_lots(depth, candidate.get('lot_size'))
    if liq is not None:
        bounds['liquidity'] = liq
    # Depth absent is NOT depth unlimited. It is the same class as an
    # unpriceable candidate, and the same answer: take the smallest size that
    # is certainly executable rather than assume the book is deep.
    #
    # `depth is None` is a DIFFERENT case, kept deliberately: a caller that
    # never had depth to give must not be capped at 1 lot by a limit it was
    # never measured against (`test_no_depth_argument_at_all_leaves_liquidity_
    # out_of_it`). The distinction only holds while callers honour it, which
    # is why `zebra/monitor.py` now passes `{}` — "I looked and the book said
    # nothing" — rather than None.
    elif depth is not None:
        bounds['liquidity_unknown'] = 1

    lots = min(v for v in bounds.values())
    bound = [k for k, v in bounds.items() if v == lots][0]
    if lots <= 0:
        return {'lots': 0, 'slice_lots': SLICE_LOTS, 'capital': 0.0,
                'bound': bound, 'reason': 'no lots fit (%s)' % bound,
                'bounds': bounds}
    return {'lots': int(lots), 'slice_lots': SLICE_LOTS,
            'capital': per_lot * lots, 'bound': bound,
            'reason': '%d lot(s), bound by %s' % (lots, bound),
            'bounds': bounds}


# ── After the fill ──────────────────────────────────────────────────────────

def verify_entry(broker_positions, trade: dict) -> dict:
    """Does the BROKER agree with the position we just recorded?

    Owner, 2026-08-26: "ensure entries are correct after entry."

    The exit path has had this since the Feb-2026 incident
    (`reconcile_after_close`), for a reason that applies just as hard here: the
    code that placed the orders is exactly the code that cannot be trusted to
    say what it placed. Entry had nothing — a fill short by a lot, a leg that
    never went through, or a quantity typo in `zebra enter` produced a record
    that looked perfect and a position that was not the one being monitored.
    Every stop level afterwards is computed from the RECORD.

    Reads the broker's own view and compares it against what was written.
    Returns {'ok', 'problems', 'checked'}; NEVER raises and NEVER places an
    order — a mismatch means the order-placing code needs a human, and putting
    more orders on top is the amplification that turned a stop into a
    four-fill loss.

    `checked` is False when the broker's view could not be read at all. That is
    not a pass: `ok` is False too, because "we could not look" and "we looked
    and it is fine" must never render the same.
    """
    out = {'ok': False, 'checked': False, 'problems': []}
    if broker_positions is None:
        out['problems'].append('could not read positions from the broker - '
                               'the entry is UNVERIFIED, not verified')
        return out

    net = {}
    for p in broker_positions:
        sym = p.get('tradingsymbol')
        if sym:
            net[sym] = net.get(sym, 0) + int(p.get('quantity') or 0)
    out['checked'] = True

    qty = int(trade.get('quantity') or 0)
    # A vertical is long one leg and short the other, in equal size. Both the
    # SIGN and the MAGNITUDE are checked: a leg that came back the right size
    # with the wrong sign is the Feb-2026 shape, and it reads as "present".
    for key, want in (('long_symbol', qty), ('short_symbol', -qty)):
        sym = trade.get(key)
        if not sym:
            out['problems'].append('%s missing from the record' % key)
            continue
        have = net.get(sym)
        if have is None:
            out['problems'].append('%s: broker shows NO position (expected '
                                   '%+d)' % (sym, want))
        elif have != want:
            out['problems'].append('%s: broker shows %+d, record says %+d'
                                   % (sym, have, want))

    out['ok'] = not out['problems']
    return out
