"""Same-stock re-entries — TAGGED and MEASURED, never blocked.

Owner, 2026-09-11, after BHARATFORG closed on its trail at 12:45 and re-entered
the same afternoon: "many trades we have entered again in 2 weeks — there
should be some way to eliminate this". Measured on the cohort before anything
was built (Drive snapshot, `scored()` population, calendar days from the prior
position's exit to the new entry):

    re-entry within 14 days    8 entries   closed 4/0    net +Rs 15,384
    first entry               27 entries   closed 13/9   net -Rs 13,209

All eight re-entries were the SAME weekly ST line in the same direction, so a
"fresh signal only" rule and a 14-day block were the same rule on this book —
and it would have removed the cohort's best-performing group. Four closes is
no evidence the other way either (four more were still open, and early closes
flatter a book: `feedback_a_fast_exit_manufactures_a_win_rate`).

So the owner's decision is to TAG every entry with its prior same-stock
position and score the two populations forward (`python -m zebra reentry`),
and to show the entry vet the name's recent trades. Nothing in this module can
refuse a signal, and nothing should import it for that purpose until the
scorecard can carry a rule.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

#: Calendar days from the prior position's exit to a new entry that make the
#: entry a RE-ENTRY on the scorecard. The owner's "2 weeks". Reporting only.
WINDOW_DAYS = 14

#: The record key the entry stamp is written under.
FIELD = 'prior_position'

#: Two ST lines within this fraction of each other are the same line, i.e. the
#: same signal episode rather than a fresh one.
SAME_ST_TOL = 0.01

#: Carried into the entry-vet context beside the positions themselves, so the
#: agent is told how to read them where it reads them.
VET_NOTE = (
    "Information, not a veto ground. Owner decision 2026-09-11: same-stock "
    "re-entries are TAGGED and MEASURED, not blocked — measured on the cohort, "
    "early re-entries on the same ST line did better than first entries, on a "
    "sample far too small to act on either way. A recent trade or a recent "
    "stop-out on this name is not by itself a reason to veto. Use it to see "
    "concentration: what this name has already made or cost, and whether a "
    "position on it is still open.")


def _key(t: dict, which: str) -> tuple:
    return (str(t.get('%s_date' % which) or ''), str(t.get('%s_time' % which) or ''))


def _order(t: dict) -> tuple:
    """Entry order: date, time, then id. The store allocates ids in order, so
    two entries stamped in the same second still have a strict order."""
    tid = t.get('id')
    return _key(t, 'entry') + (tid if isinstance(tid, int) else 0,)


def _day(s) -> Optional[date]:
    try:
        return datetime.strptime(str(s)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _net(t: dict):
    v = t.get('pnl_net')
    return v if isinstance(v, (int, float)) else t.get('pnl')


def _same_st(a: dict, b: dict) -> Optional[bool]:
    try:
        x, y = float(a.get('st_value')), float(b.get('st_value'))
    except (TypeError, ValueError):
        return None
    return x > 0 and abs(x - y) / x <= SAME_ST_TOL


def prior_position(book, trade: dict) -> Optional[dict]:
    """The most recent OTHER position on `trade`'s stock, as it stood when
    `trade` entered. None for a first entry.

    AS OF THE ENTRY (`feedback_measure_as_of_the_decision_date`). A prior whose
    exit came after this entry is reported as still open at the time, with no
    result: its outcome was not knowable when the decision was taken. While the
    scanner holds one position per stock that cannot happen, and it is reported
    rather than assumed.

    Positions from the whole book count, not just the cohort's: a stock traded
    last week is "recently traded" whichever engine version traded it.
    """
    stock = trade.get('stock')
    entered = _key(trade, 'entry')
    if not stock or not entered[0]:
        return None
    candidates = [p for p in (book or [])
                  if p is not trade
                  and p.get('id') != trade.get('id')
                  and p.get('stock') == stock
                  and p.get('status') in ('entered', 'exited')
                  and p.get('entry_date')
                  and _order(p) < _order(trade)]
    if not candidates:
        return None
    p = max(candidates, key=_order)
    closed = bool(p.get('status') == 'exited' and p.get('exit_date')
                  and _key(p, 'exit') <= entered)
    ref = _day(p.get('exit_date')) if closed else _day(p.get('entry_date'))
    this_day = _day(trade.get('entry_date'))
    return {
        'id': p.get('id'),
        'direction': p.get('direction'),
        'timeframe': p.get('timeframe'),
        'entry_date': p.get('entry_date'),
        'closed_before_entry': closed,
        'exit_date': p.get('exit_date') if closed else None,
        'exit_reason': p.get('exit_reason') if closed else None,
        'pnl_net': _net(p) if closed else None,
        'pnl_net_pct': p.get('pnl_net_pct') if closed else None,
        # From the prior's EXIT when it had closed, else from its entry.
        'days_since': (this_day - ref).days if (ref and this_day) else None,
        'same_direction': p.get('direction') == trade.get('direction'),
        'same_timeframe': p.get('timeframe') == trade.get('timeframe'),
        'same_st': _same_st(p, trade),
    }


def prior_for(book, trade: dict) -> Optional[dict]:
    """The stamped prior when the record carries one, else computed as of its
    entry — the answer the stamp would have given, for records entered before
    the stamp existed."""
    if FIELD in trade:
        return trade[FIELD]
    return prior_position(book, trade)


def is_reentry(prior: Optional[dict], window: int = WINDOW_DAYS) -> bool:
    return bool(prior) and prior.get('days_since') is not None \
        and prior['days_since'] <= window


def summarise(trades) -> dict:
    """Entries, closes, wins/losses on NET, net rupees, still open."""
    trades = list(trades or [])
    closed = [t for t in trades if t.get('status') == 'exited']
    wins = [t for t in closed if (_net(t) or 0) > 0]
    return {'entries': len(trades), 'closed': len(closed), 'wins': len(wins),
            'losses': len(closed) - len(wins),
            'net': round(sum(_net(t) or 0 for t in closed)),
            'open': len(trades) - len(closed)}


def recent_on_stock(book, stock: str, today: Optional[date] = None,
                    exclude_id=None, window: int = WINDOW_DAYS) -> list:
    """Positions on `stock` still open, or closed within `window` days of
    `today`, newest first — the entry vet's view of the name."""
    today = today or date.today()
    out = []
    for p in book or []:
        if p.get('stock') != stock or p.get('id') == exclude_id:
            continue
        if p.get('status') == 'entered':
            pass
        elif p.get('status') == 'exited':
            ex = _day(p.get('exit_date'))
            if ex is None or (today - ex).days > window:
                continue
        else:
            continue
        out.append({
            'id': p.get('id'), 'status': p.get('status'),
            'direction': p.get('direction'), 'timeframe': p.get('timeframe'),
            'st_value': p.get('st_value'),
            'entry_date': p.get('entry_date'), 'exit_date': p.get('exit_date'),
            'exit_reason': p.get('exit_reason'),
            'pnl_net': _net(p) if p.get('status') == 'exited' else None,
            'pnl_net_pct': p.get('pnl_net_pct'),
        })
    out.sort(key=lambda r: (str(r.get('entry_date') or '')), reverse=True)
    return out
