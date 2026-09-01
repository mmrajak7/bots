"""What an NFO tradingsymbol says about the contract.

One definition, because three stores and one monitor all need it and the last
time a piece of option arithmetic existed in only one of them the bear-put book
ran for months with no intrinsic floor at all (B21).

The symbol is the source of truth about the CONTRACT. A record's store, its
`_store_type` tag and its field names are facts about bookkeeping. When the two
disagree the bookkeeping is wrong, and the money path must not act on it — see
`check_leg_types`.
"""
from __future__ import annotations

import re
from typing import Optional

_SUFFIX = re.compile(r'(CE|PE)$')
#: ANCHORED ON THE EXPIRY CODE, not on "the digits before CE/PE".
#
# It was `(\d+(?:\.\d+)?)(CE|PE)$`, which on a digit-coded WEEKLY index symbol
# swallows the expiry as well as the strike: `NIFTY2560525000CE` returned
# 2560525000.0 -- a confidently wrong number where the docstring promises
# None, feeding intrinsic-floor arithmetic in the stores and the monitor.
#
# `\d{2}[A-Z]{3}` is the monthly expiry code (`26FEB`, `26AUG`), and it is
# what separates symbol from strike. A weekly's `25605` has no alpha month, so
# it no longer matches at all and the caller gets the None it was promised.
# Verified against all 436 distinct option symbols in the four books: every
# one is the monthly form, including the awkward `360ONE26AUG1140CE`.
_STRIKE = re.compile(r'\d{2}[A-Z]{3}(\d+(?:\.\d+)?)(?:CE|PE)$')


def option_type(symbol) -> Optional[str]:
    """'CE', 'PE', or None for anything that is not an option symbol."""
    m = _SUFFIX.search(str(symbol or '').upper())
    return m.group(1) if m else None


def strike(symbol) -> Optional[float]:
    """Strike embedded in the symbol, or None.

    The trade schemas store `spread_width` but not the individual strikes, so
    anything needing a payoff has to recover them from here. None on anything
    unexpected — callers treat that as "cannot price", which is the fail-open
    direction.
    """
    m = _STRIKE.search(str(symbol or '').upper())
    return float(m.group(1)) if m else None


def check_leg_types(trade: dict, expected: dict):
    """Do the record's symbols match what this book is supposed to hold?

    `expected` maps field name -> 'CE' or 'PE'. Returns a list of human-readable
    problems; empty means consistent.

    **Why this is not paranoia.** `bcs/spread_monitor.py` picks SL_SPOT and TP
    direction from which store a record came out of: BCS stops on a fall and
    takes profit on a rise, BPS and FH the other way round. Nothing validated
    that a record in the BCS book actually holds calls. A bear put spread saved
    there — one wrong `get_store()` in a capture flow driven by natural
    language — reads its upside stop as a downside one, so on the FIRST poll of
    a perfectly healthy position both `spot <= sl_spot` and `spot >= target`
    are true at once and the monitor closes it at whatever the book offers.

    A missing field is NOT reported here. Required-field validation is the
    store's job and reporting it twice would turn one error into two.
    """
    problems = []
    for field, want in expected.items():
        sym = trade.get(field)
        if sym is None:
            continue
        got = option_type(sym)
        if got is None:
            problems.append(
                f"{field}={sym!r} is not an option symbol (expected a {want})")
        elif got != want:
            problems.append(
                f"{field}={sym} is a {got}, but this book holds {want}")
    return problems
