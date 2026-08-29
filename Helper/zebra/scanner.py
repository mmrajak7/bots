"""Zebra scanner — Chartink scan + ST validation + watchlist add.

Pipeline:
  1. Chartink scan (monthly + weekly): F&O stocks within ±8% of ST line
  2. For each candidate: compute ST(10,3) independently, get LTP
  3. Direction: routed by `_direction_for` below, the single source of truth
     — price vs the ST line ONLY (price < ST -> CE, price > ST -> PE, exactly
     equal -> SKIP, rare). ST direction/trend alignment plays no part in this
     routing: it is a conviction tag only (see `cfg.is_trend_aligned`) and
     must NEVER be used as an entry filter — it reads false on nearly every
     historical record, so gating entry on it would veto almost the entire
     strategy. (This is the exact rule a stale copy of this docstring got
     wrong on 2026-08-13, when a vetting agent quoted a "SKIP if trend not
     aligned" branch that this code has never implemented and killed a valid
     signal on it.)
  4. Freshness filter (same as magnet): skip if price was within 1% of ST in
     last N days (bounce, not fresh approach)
  5. Add to ZebraStore as WATCHING if gap <= watch_gap_max (5%)

Heavy lifting (Chartink scrape, ST compute, freshness) is delegated to
playbook.magnet.scanner — that code is proven, just imported.
"""

from __future__ import annotations

from collections import Counter
import logging
import time
from datetime import datetime
from typing import List

from . import config as cfg
from .trade_store import ZebraStore

logger = logging.getLogger(__name__)


# Reuse magnet's proven Chartink + ST + freshness helpers
from playbook.magnet.scanner import (
    scan_chartink,
    _normalize_symbol,
    _get_kite,
    get_ltp,
    get_ltp_ex,
    compute_st_for_stock,
    check_freshness,
)


def _direction_for(price: float, st_val: float, st_direction: str) -> str:
    """Map (price vs ST) → Zebra direction.

    MAGNET logic: ST acts as an attractor. Whichever side of ST price sits
    on, it tends to pull toward ST.

      price < ST  → CE-Zebra (expect rally UP to ST)
      price > ST  → PE-Zebra (expect drop  DOWN to ST)

    ST direction is NOT used as a filter — magnet works regardless of trend.
    Quality is enforced by the gap band (3-5%) and the freshness check
    (no ST touch in last N days), not by trend confirmation.
    """
    if price < st_val:
        return 'CE'
    if price > st_val:
        return 'PE'
    return 'SKIP'  # exactly on ST (rare)


def run_all_scanners() -> List[dict]:
    """Run all enabled Chartink scanners. Returns raw candidates."""
    all_signals = []
    for s in cfg.SCANNERS:
        try:
            symbols = scan_chartink(s['clause'])
            logger.info("Chartink [%s]: %d symbols", s['name'], len(symbols))
            for sym in symbols:
                all_signals.append({
                    'stock': _normalize_symbol(sym),
                    'timeframe': s['timeframe'],
                })
            time.sleep(0.5)  # be polite to Chartink
        except Exception as e:
            logger.error("Chartink scan %s failed: %s", s['name'], e)
    return all_signals


def validate_and_add(store: ZebraStore, kite=None,
                     dry_run: bool = False) -> List[dict]:
    """Run scanners, validate each candidate, add to store as WATCHING.

    Returns the list of newly-added signal dicts (empty list if dry_run).
    """
    if kite is None:
        kite = _get_kite()

    raw = run_all_scanners()
    if not raw:
        logger.info("No raw Chartink signals")
        return []

    # Capacity guard
    watching = store.get_watching()
    if len(watching) >= cfg.MAX_WATCHING_SIGNALS:
        logger.info("Watching capacity %d/%d — skipping new adds",
                    len(watching), cfg.MAX_WATCHING_SIGNALS)
        return []

    # Get LTPs in one batch
    stocks = list({r['stock'] for r in raw})
    ltps = get_ltp(kite, stocks)

    added = []
    skips = Counter()
    for r in raw:
        stock = r['stock']
        timeframe = r['timeframe']
        price = ltps.get(stock, 0)
        if price <= 0:
            skips['no_ltp'] += 1
            logger.debug("SKIP %s: no LTP", stock)
            continue

        # ST compute (cached if already done today for this stock+TF)
        st_info = compute_st_for_stock(kite, stock, timeframe)
        if not st_info:
            skips['st_failed'] += 1
            logger.debug("SKIP %s %s: ST compute failed", stock, timeframe)
            continue

        st_val = st_info['st']
        st_dir = st_info['direction']

        # Direction routing. `_direction_for` returns 'SKIP' in exactly ONE
        # case: price == st_val to the tick, so there is no distance to the
        # magnet to trade. Trend direction is not consulted here at all.
        #
        # M5, renamed 2026-08-29. This counter was called `trend_misaligned`
        # and its log line read "trend not aligned" — a name and a message
        # describing a filter this scanner has never implemented. That exact
        # fiction has already cost a trade: on 2026-08-13 a spawned vetting
        # agent read "SKIP, trend not aligned" out of the docs and vetoed a
        # valid signal with it. `trend_aligned` is a CONVICTION TAG here, never
        # a filter (381 of 383 records are counter-trend, and the 2 aligned
        # ones averaged -24.6%), so a counter implying otherwise is a standing
        # invitation to re-derive the wrong rule. Free to rename: grep found
        # this line to be the key's ONLY occurrence in the tree, which also
        # falsifies the old comment's claim that it was read elsewhere.
        direction = _direction_for(price, st_val, st_dir)
        if direction == 'SKIP':
            skips['price_exactly_on_st'] += 1
            logger.debug("SKIP %s %s: price is ON the ST line, nothing to "
                         "travel to (price=%.2f vs ST=%.2f, dir=%s)",
                         stock, timeframe, price, st_val, st_dir)
            continue
        if direction not in cfg.ENABLED_DIRECTIONS:
            skips['direction_disabled'] += 1
            logger.debug("SKIP %s %s: direction %s disabled",
                         stock, timeframe, direction)
            continue

        # Gap check: must be within watch band
        gap = abs(price - st_val) / st_val
        if gap > cfg.WATCH_GAP_MAX:
            skips['gap_too_wide'] += 1
            logger.debug("SKIP %s %s: gap %.2f%% > watch_max %.2f%%",
                         stock, timeframe, gap * 100, cfg.WATCH_GAP_MAX * 100)
            continue
        if gap < cfg.STALE_GAP_MIN:
            skips['gap_too_late'] += 1
            logger.debug("SKIP %s %s: gap %.2f%% < stale_min %.2f%% (too late)",
                         stock, timeframe, gap * 100, cfg.STALE_GAP_MIN * 100)
            continue

        # Freshness check (reuse magnet's logic, but with OUR thresholds —
        # they used to be magnet's, so zebra's live entry band was set by the
        # config file of a bot retired in May).
        is_fresh, freshness_reason = check_freshness(
            stock, st_val, timeframe,
            entry_gap=cfg.FRESH_ENTRY_GAP,
            entry_gap_min=cfg.STALE_GAP_MIN,
            freshness_days=cfg.FRESHNESS_DAYS)
        if not is_fresh:
            # Counted separately: a band clip and a genuine ST touch are
            # different facts, and lumping them made the 3-4% clip invisible —
            # it read as ordinary freshness filtering in the skip histogram.
            if 'missed approach' in freshness_reason or 'too late' in freshness_reason:
                skips['gap_below_entry_band'] += 1
            else:
                skips['not_fresh'] += 1
            logger.debug("SKIP %s %s: %s", stock, timeframe, freshness_reason)
            continue

        # Dedup against existing open signals (BCS shadows excluded — they
        # mirror zebra trades passively and must not block fresh signals)
        existing = next(
            (t for t in store.load_trades()
             if t.get('stock') == stock
             and t.get('timeframe') == timeframe
             and t.get('direction') == direction
             and t.get('shadow_of') is None
             and t.get('status') in ('watching', 'triggered', 'entered')),
            None
        )
        if existing:
            skips['already_open'] += 1
            logger.debug("SKIP %s %s %s: already open as #%d (%s)",
                         stock, timeframe, direction, existing['id'],
                         existing['status'])
            continue

        # Cross-direction dedup: same stock can't be both CE-Zebra and PE-Zebra
        opposite = next(
            (t for t in store.load_trades()
             if t.get('stock') == stock
             and t.get('direction') != direction
             and t.get('shadow_of') is None
             and t.get('status') in ('watching', 'triggered', 'entered')),
            None
        )
        if opposite:
            skips['opposite_open'] += 1
            logger.debug("SKIP %s %s: opposite direction open (#%d %s)",
                         stock, direction, opposite['id'], opposite['direction'])
            continue

        # Passed all gates — add as watching. Trend alignment (the validated
        # with-trend premium tier) is NOT stored or baked into notes — it is
        # derived on demand via cfg.is_trend_aligned wherever it's shown (alert
        # badge, reports, analyze), so there is a single source of truth that
        # can't drift.
        signal_data = {
            'stock': stock,
            'timeframe': timeframe,
            'direction': direction,
            'st_value': round(st_val, 2),
            'st_direction': st_dir,
            'signal_price': round(price, 2),
            'signal_gap_pct': round(gap * 100, 2),
            'paper': True,
            'notes': f"Chartink {timeframe} {direction}-Zebra, gap={gap*100:.2f}%",
        }
        if dry_run:
            print(f"  [DRY] WATCH {stock} {timeframe} {direction} "
                  f"spot={price:.2f} ST={st_val:.2f} gap={gap*100:.2f}%")
            added.append(signal_data)
        else:
            try:
                trade = store.add_signal(signal_data)
                added.append(trade)
            except ValueError as e:
                skips['store_rejected'] += 1
                logger.debug("add_signal skipped %s: %s", stock, e)

    # The REASONS, aggregated, on the summary line. Per-symbol skips stay at
    # DEBUG (900 of them a cycle at INFO would bury everything else), but
    # "0 added" with no explanation is unactionable — and 4,475 such lines in
    # the real log carried no reason at all, so "why did DABUR never signal on
    # Aug 7" had no answer anywhere.
    detail = ', '.join(f'{k}={v}' for k, v in sorted(skips.items(),
                                                     key=lambda kv: -kv[1]))
    logger.info("Scanner: %d raw → %d added%s", len(raw), len(added),
                f' | skipped: {detail}' if detail else '')
    return added
