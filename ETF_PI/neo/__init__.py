"""
neo -- RETIRED 2026-11-27 (D17, owner decision). Importing it fails on purpose.

This in-process Kotak Neo bridge could not log in on the Pi (SDK v1, dead session
cache), could not map weekly NIFTY symbols, wrote the ledger before any order was
sent and never retried a failed exit. Live orders for ETF_PI are placed ONLY by the
executor daemon (`executor/`, docs/EXECUTOR_SPEC.md) through the shared
`BOTS/kotak_neo` package. The modules stay in the tree as history; nothing may
import them.
"""

raise ImportError('ETF_PI neo/ bridge is retired (D17, 2026-11-27): '
                  'live orders go through the executor daemon + BOTS/kotak_neo')
