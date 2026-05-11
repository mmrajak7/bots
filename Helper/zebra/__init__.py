"""Zebra — synthetic long/short via Zero-Extrinsic Back Ratio.

Bullish (CE-Zebra): BUY 2x ITM CE + SELL 1x ATM CE, same expiry.
Bearish (PE-Zebra): BUY 2x ITM PE + SELL 1x ATM PE, same expiry.

Pipeline: Chartink scan → watchlist → trigger zone → strike analyzer →
ENTER alert with 2-3 candidate pairs → user enters manually → monitor for
TP / SPOT SL / DEBIT SL / TIME alerts → user exits manually.

Replaces: magnet, confidence_tracker, spot_tracker, flow (all silenced 2026-05-11).
Playbook: zebra/PLAYBOOK.md.
"""

from .trade_store import ZebraStore, get_store

__all__ = ['ZebraStore', 'get_store']
