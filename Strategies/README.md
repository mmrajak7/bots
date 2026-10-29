# Strategies — research workspace

Created 2026-10-29. Everything we discuss about finding a strategy that works
goes here: one file per study, plus this index.

## Index
| File | What |
|---|---|
| `01_volrix_screen_2026_10_29.md` | Screen of all 81 Volrix open strategies (app.volrix.ai/strategies) |
| `02_nifty_btst_expiry_analysis_2026_10_29.md` | NIFTY BTST rules + all 1,145 Volrix trades split by DTE / expiry day / day before |
| `NIFTY_BTST/` | Paper trader for the NIFTY BTST Rs100 rule on Kite quotes (README inside: deploy + monitor) |

## What we already tested elsewhere (do not re-run without new data)
- ST-magnet naked long (zebra): 100-trade test in progress; replay of the rules alone loses.
- NIFTY condor / strangle / iron fly / calendar: about 0 before costs on a Black-Scholes-off-VIX model.
  **That model did not use the real option chain**, and Volrix uses real historical chains. Where the
  two disagree, the real chain is the better witness.
- SNAIL, FIFTY, Banana ORB, MCX ST magnet, factor indices: see auto-memory.
