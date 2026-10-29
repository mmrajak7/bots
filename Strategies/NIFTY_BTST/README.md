# NIFTY_BTST — paper trader for the NIFTY weekly BTST Rs100 rule

Paper only. Reads Kite quotes; **places no orders**. One lot per signal.

Background and the backtest it is paired with:
`Strategies/01_volrix_screen_2026_10_29.md`, `Strategies/02_nifty_btst_expiry_analysis_2026_10_29.md`
(Windows only — `*.md` is git-ignored; this README is the copy that reaches the Pi).

## The rule (as published by Volrix; traded unchanged so every trade has a backtest twin)
| When | What |
|---|---|
| 09:20 | Mark the nearest weekly NIFTY CE and PE whose LTP is closest to Rs 100. **On expiry day: the NEXT weekly.** Trigger = 1.5 x mark. |
| 09:21-15:25 | A marked option trading at or above its trigger is bought, 1 lot, once per side per day. |
| Stop | 50% of entry price, watched whenever the market is open (F&O trades to **15:40** since the cash closing auction), **including 09:15-09:25 next morning**. |
| Exit | 09:25 the next trading session. No target. |

## Paper fill rules (house rules)
- BUY at the ASK, SELL at the BID, and only on a reliable book: two-sided, not crossed, and no
  wider than both Rs 2 and 10% of mid. Otherwise the fill DEFERS to the next poll.
- A triggered exit stays **latched** until it can be booked, even if the price bounces back.
- **Only a book that traded TODAY is acted on.** Kite keeps a two-sided book and a fresh quote
  timestamp long after the close and on holidays, so an option's `last_trade_time` is the test.
  A 09:25 exit due on a book that has not traded yet this morning waits (latched) for one that has.
- **Holidays are read from the market, not trusted from the calendar.** Every weekday runs. At the
  mark, no option traded today plus a calendar holiday = the day is closed quietly. No option
  traded but the calendar says session = MARK FAILED alert. Options trading on a calendar holiday
  = traded, with an alert that the calendar is wrong.
- Late start (after 09:21): the mark is rebuilt from the 09:20 one-minute candles. A side whose
  option already traded through its trigger before we were watching is **MISSED** and is never
  back-filled.
- Restart mid-session after the mark: a side whose option crossed its trigger (1-minute high)
  since the previous process's last heartbeat is MISSED too.
- Missed the 09:25 exit (box down): exits at the first poll, reason `next_day_late`.
  Still open after expiry: settled at intrinsic off the NIFTY close, reason `expired_settled`.
  Both are flagged in `report`.
- Each trade is stamped `cls` = `expiry_day` / `day_before` / `other` plus DTE (sessions),
  so the expiry split can be re-measured forward.
- P&L is stored three ways: gross, `net_pnl` (all charges incl. Rs 20/order brokerage), and
  `net_ex_brokerage` (Volrix's basis, for pairing).

## Layout
```
NIFTY_BTST/
  btst_config.defaults.json   tracked defaults
  btst_config.json            optional untracked overlay (wins key by key)
  nifty_btst/                 package (python -m nifty_btst ...)
  tests/                      pytest, no network
  state/  (git-ignored)       book.json, events_YYYYMMDD.jsonl, heartbeat.json, instrument cache
  logs/   (git-ignored)       cron output
```
Shared inputs (never in this folder): `BOTS/data/kite_access_token.json`,
`BOTS/data/telegram_config.json`, `BOTS/data/holiday_calendar.json`.

## Commands
```bash
python -m nifty_btst run            # session loop; exits after 15:40 and sends the EOD digest
python -m nifty_btst run --force    # special session on a calendar holiday/weekend (Budget, Muhurat)
python -m nifty_btst status         # heartbeat age, today's marks, open positions
python -m nifty_btst report         # scorecard: net, Volrix basis, by class / side / exit / DTE, slippage
python -m nifty_btst compare FILE   # pair with a Volrix "Download CSV" export (owner downloads it)
python -m nifty_btst digest         # re-send today's EOD digest
python -m pytest -q -p no:asyncio tests
```
Exit codes: 0 ok, 2 token problem, 3 corrupt book, 4 already running, 5 config unusable.

## Telegram
Plain text, prefixed `NIFTY-BTST (paper)`: MARK (09:20), ENTRY, EXIT, EOD digest; plus
QUOTES FAILING (10 polls in a row), MARK FAILED, STEP ERRORS, token stale, BOOK CORRUPT,
calendar wrong, config unusable. Problem alerts are sent once a day, not on every cron tick.

## Deploy on the Pi (run at the box)
There is no bare `python` on the Pi; use the shared venv.
```bash
cd /home/trustit/Desktop/BOTS && git pull --rebase
cd Strategies/NIFTY_BTST && mkdir -p logs state
../../CROCODILE/venv/bin/python -m pytest -q -p no:asyncio tests
../../CROCODILE/venv/bin/python -m nifty_btst status
```
Crontab line (`crontab -e`). The `\%` escaping is required:
```
*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Strategies/NIFTY_BTST && flock -n /tmp/nifty_btst.lock ../../CROCODILE/venv/bin/python -m nifty_btst run >> logs/cron_btst_$(date +\%Y\%m\%d).log 2>&1
```
The 09:00 tick starts the long-lived loop, which waits for 09:15 and exits after 15:40. Later ticks see the lock and
exit. A crashed loop restarts within 5 minutes. Ticks after 15:40 only send the digest if it was
not sent.

**After every `git pull` that touches this folder, restart the loop** (it holds the code it
started with). The next cron tick restarts it:
```bash
pkill -f "nifty_btst run"
```

## Monitor
```bash
tail -f logs/cron_btst_$(date +%Y%m%d).log
../../CROCODILE/venv/bin/python -m nifty_btst status
```

## Known limits
- Polls every 3 s with one `quote()` call. The Kite API key is shared with zebra and the BCS
  monitor (~1 quote req/s per key), so a 429 is retried with backoff.
  Volrix works on 1-minute bars, so our trigger can fire inside a minute Volrix would score at
  its close. The pairing measures exactly that difference.
- Special sessions on a weekend are not in the Mon-Fri cron; run `run --force` by hand.
  2026-02-01 (Budget Sunday) was the backtest's 3rd-best trade.
- The profit is concentrated: Volrix's top 20 of 1,145 trades are 75% of its P&L.
  **A missed day matters more than a few rupees of slippage.** Watch the heartbeat.
