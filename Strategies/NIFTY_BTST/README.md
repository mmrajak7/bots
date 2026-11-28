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

## The pass mark for going live (owner, 2026-11-02; `compare.gate`)
Fixed in advance. Read it with `python -m nifty_btst compare FILE`. Do not tune it mid-window.

| Rule | Value |
|---|---|
| Population | the first **30** closed pairs in entry order from 2026-10-30. Pair = same entry date + side as Volrix. The 31st never counts. |
| Fills | mean extra cost **<= Rs 2.00/share**, round trip. Extra cost = (Volrix exit - entry) - (paper exit - entry). |
| Coverage | **no Volrix win >= Rs 10,000 (1 lot) without a paper twin.** One miss is an immediate FAIL, for review. |
| Never excluded | a pair on a different strike or entry time; it is reported, and it stays in. |
| Excluded | Volrix rows with no genuine exit (`End of backtest`, `Error fetching exit price`). |

**It tests implementation only:** whether we can get Volrix's trades at about Volrix's prices, not
whether the edge is real. A losing window does not fail it, and 30 trades cannot prove an edge.
No real order until it reads PASS, the NEO design is approved, and the owner says so.

**WAIVED by the owner on 2026-11-26** ("we cannot get 100% sync; the concept is clear and matches
the direction and P&L; what matters now is razor-sharp execution"). Go-live 2026-12-01 after the
Stage 3 real 1-lot test. The waiver is `neo.pass_mark_waived: "2026-11-26"` in the Pi's LIVE overlay
only; the tracked default `null` keeps the gate fail-closed. The arming line then reads
`PASS MARK: WAIVED by the owner on 2026-11-26 ...` and still prints the computed verdict, as a
report (a computed FAIL is waived too: the owner waived the comparison itself). ARM is still
required and still re-read every minute; the waiver is read once per `live-run` start (removing
it takes effect at the next start). An unreadable waiver value never stops the run: it reads as
NOT waived (entries blocked, exits run) and the arming line says so. What replaces the gate as the implementation check is the twin
report in `live-report`: every trade both books took, live fill against the paper twin's fill,
`extra cost/share = (paper exit - paper entry) - (live exit - live entry)`, split into its entry
and exit parts (`nifty_btst/twin.py`). Added risk, said to the owner: real fills are measured only
from the first live trades; the Rs 10 probe proves mechanics, not slippage on a Rs 150 option.

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

---

## Live path on Kotak NEO — Stage 1 BUILT 2026-11-02, reviews 1-4 fixed 2026-11-04, NOT yet run against NEO

The same rule with a broker where the paper fill used to be. Direct REST (`requests`), no SDK,
Python 3.8-compatible. **Default is paper.** Nothing here has been run against the real NEO API:
the probes in Stage 0 and Stage 3 are what verifies the request shapes. Design, flow review and
the remediation tables (Windows only): `docs/NEO_LIVE_DESIGN.md` sections 14-17, `docs/NEO_REVIEW_[1234].md`.

### Modes (`execution`, set in the LIVE overlay `btst_live_config.json`, never in the paper overlay)
| `execution` | Orders go to | State dir | Telegram prefix |
|---|---|---|---|
| `paper` (default) | nowhere — the paper trader, unchanged | `state/` | `NIFTY-BTST (paper)` |
| `rehearsal` | a fake broker persisted under the state dir (survives restarts and the overnight); login + scrip master are REAL, read-only | `state_rehearsal/` | `NIFTY-BTST (REHEARSAL)` |
| `live` | Kotak NEO, 1 lot, only when ARMED (below) | `state_live/` | `NIFTY-BTST (LIVE)` |

**Paper reads none of this.** `python -m nifty_btst run` ignores `execution` and the `neo` block,
never opens `btst_live_config.json`, and never executes the broker code (`live_cli` is not even
imported unless a live command is given, R2-27). A typo made while arming cannot stop the paper trader. The live
engine is a separate process with its own lock, store and journal.

### The rule that governs every doubt: absence is not proof
A book that does not show an order, or a position row that is missing, is never on its own a reason
to send again.
- A send with no clear answer stays **UNKNOWN** until the broker shows it (by tag, then by
  symbol/side/qty/time), or until `neo.unknown_grace_sec` (600 s) has passed with at least two
  positively valid reads AND positions agree nothing happened. Alerts every minute meanwhile.
  An unclear **SELL** (a stop in a falling market) waits its own `neo.exit_unknown_grace_sec`
  (90 s, never below `positions_lag_sec`) and needs THREE valid reads (review H1, 2026-11-28):
  NEO shows a placed order at once and echoes our tag (measured 11-27).
- NEO's trade book has one row per FILL; the broker layer sums them into one row per order
  (size-weighted price) before anything reads them (review M1). The `prod` field is read
  upper-cased and trimmed, and a row carrying OUR tag is in scope whatever its product says
  (review H2): the real order/trade-row spelling is measured on 30 Nov (B1c).
- An order we hold a NUMBER for is never dropped on its day. The next day it is resolved against
  positions and the trade book: a fill the book never showed is **adopted** (booked at the limit,
  price flagged, the leg's own day as entry date); a flat broker means the DAY order died.
- Every **BUY** and every **SELL** first reads positions and the order book: no other live order of
  that side on the symbol, and the broker holds exactly what the book expects (BUY) / at least the
  quantity sold (SELL). A read failure is a refusal.
- A **short** on any symbol, an **extra lot**, an own-tag order the book does not know, or a tag
  once written off that later appears: the kill trips, LOUD (every minute; an already tripped kill
  still records and alerts each new distinct reason, and the minute reminder leads with the
  newest, R6-03), and nothing is ever
  traded to repair it. The owner looks in the app.
- **A missing position row is evidence of nothing.** A holding or leg from an earlier day with no
  row is UNKNOWN: never sold, never adopted, never abandoned on absence. Positive sources only: a
  row that exists, a Holdings row (`neo.holdings_source_enabled`, unverified until Stage 3), or
  your own statement for today: `python -m nifty_btst live-confirm-held ID --yes` after checking
  the app (journalled, expires tonight). A confirmation, or a Holdings row, vouches only while
  every SELL sent since reads terminal with ZERO fill: a filled SELL, a live SELL or one whose
  state cannot be read voids it, so neither can sell twice — but a cancelled or rejected attempt
  does not end the chase. `live-confirm-held` also OPENS a pending record whose entry may have
  filled overnight (at the real fill when the trade book shows it, else at the entry limit marked
  as an estimate) so the stop and the 09:25 exit run on it; it refuses while the BUY still reads
  live at the broker, and needs a running engine to read it (R4-09/14).
- **Other bots on the account are invisible to this book (B1, 2026-11-27).** ETF_PI (MIS) and
  zebra trade the SAME NIFTY contracts on this NEO account. Every order-book and trade-book read
  goes through `broker.ScopedBroker`: a row that STATES another product, or carries another bot's
  tag (`broker.OTHER_BOT_TAG_RE`: `zbr-`/`etf-`, ETF_PI's `ENT_/TGT_/EXT_<ts>`), is dropped. Rows with
  no tag or an unknown tag stay (a human in the app: the owner's manual sale must still block a
  double sell). A new bot on the account adds its tag prefix there. An unclear send is never bound
  to a row carrying a different tag. Positions stay NRML-filtered: no other bot may hold NRML on a
  strike this book holds.
- **Every SELL with a fill on the symbol today counts, ours or not (R4-02/03)** — among in-scope rows. A filled SELL
  this book did not send blocks our SELL on every source, naming it; more sold today than this
  book held is a real short and a loud kill at once; a negative row is the row shape without
  carried quantities only when it equals exactly the SELLs filled on a lot this book held. That
  sum leaves out the Stage 3 probe's own orders and nets filled BUYs this book did not send (an
  owner buy-back or round trip), so a clean probe sale or a repaired short does not keep the kill
  tripped (R5-01). A BUY is netted only when its row states this book's product AND today's
  date; a row with no `prod` or no parsable `ordDtTm` is never netted (R6-04). A foreign SELL
  already accounted for (adopted as an earlier lot's close, tied to a `live-book-external` close,
  or netted by an owner BUY) does not block a later lot on the same strike; reconcile books an
  owner's sale only for today's unclaimed SELLs and never inside a fresh entry's positions lag
  (R5-02). `live-book-external` ties the close to the sale itself: the record's own quarantined
  SELL, else the newest unclaimed foreign SELLs filled today and placed before the booking, each
  claimed once (one that shows up late is tied at the next reconcile). With no such sale the
  close excuses NOTHING (no quantity allowance) and is not counted as a sale today (R6-02).
  While our SELL is working, a foreign SELL on the symbol, filled OR still working, makes the
  engine cancel ours at once, not at its chase timer (R5-06, R6-05). Sold it in the app? `python -m
  nifty_btst live-book-external ID --price X --yes` (a pending entry that never filled:
  `--not-filled`; a pending one you sold: `--price X` books the real round trip). While an order of
  that record is LIVE at the broker, book-external cancels it and completes itself once the order
  reads terminal with no fill — a live order is never written off. Until a source exists the exit
  is refused and you are told every minute.
- Owner requests reach the running loop as files STAMPED with the day, the id and the symbol; the
  engine refuses another day's file or a mismatch, moves an unreadable one aside once, and writes a
  `.result` the command echoes: `APPLIED: ...`, `NOT APPLIED: <why>`, or `the running loop has not
  read it` (with the heartbeat age) — never a bare "applied". While the loop cannot be serviced
  (credentials or config unusable) the minute alert says request files are NOT being read.
- A trigger taken while entries are blocked by ARM or the kill is **abandoned at once** (owner-
  reviewable default, R3-24): arming at 14:30 never buys a 10:00 trigger the paper twin took
  then. An exits-only run (a real record under a refusing configuration) keeps the trigger
  pending and reports it, as before.
- Quantities are read in `neo.position_qty_unit` (`shares` default; `lots` multiplies by the row's
  `lotSz`). A quantity that is not a multiple of the lot, or that reads as lots where shares are
  expected, trips the kill loud rather than being read as flat.

### What live does differently from paper
- **Opening settle (owner, 2026-11-26; `neo.open_settle_sec`, 30).** NIFTY options have no
  pre-open (NSE's F&O pre-open, from 2025-12-08, is futures only). The 09:15:00 book was wide or
  one-sided on all 11 overnight mornings logged and within 0.7% on all 11 by 09:16:00. For the
  first 30 s after 09:15:00 a live SELL waits for a SETTLED book: spread <= `open_settle_max_abs`
  (Rs 0.50) OR <= `open_settle_max_pct` (1%) of mid. Paper's entry gate (Rs 2 OR 10%) is too loose
  for this: it would have passed 5 of the 11 opening books. Inside the window a bid under the stop
  counts as a stop only on a settled book (an LTP under it always does). After the window: any
  bid, as below (NEO-14 / NEO-30), so a stop is never stranded. `0` turns it off; max 60 s (the opening-minute log covers the whole wait).
  Not covered: a single stray opening TRADE under the stop still triggers it (as paper and the
  backtest do); only the bid trigger is gated.
  Paper is NOT changed (it is the Volrix twin), so the rehearsal-vs-paper gap on 09:15 stops now
  measures the settle (still simulated: the fake broker fills at its own limit).
- Entry: LIMIT BUY at the ask (rounded UP to the tick), **capped at trigger +5%**
  (`neo.entry_cap_pct`); above the cap no order, keep watching until 15:25. Not filled in 5 s:
  cancel, read back, re-price (up to `max_entry_attempts`). A rejection ends the side for the day;
  two in a row trip the kill — except the gateway refusals in `neo.retry_stcodes` (100008 = wrong
  IP, HTTP 429 = throttled): definitely NOT placed, no streak, not an attempt, not counted by the
  order ceilings, and retried after a backoff of 3 s doubling to at most 60 s (code constants,
  reset by the first send that is not refused and at the first refusal of a new day, R6-07), with
  an alert (R5-03). Each send has its own tag,
  a refused one included (R5-08). A leg whose fill is unknown is never abandoned. Order-book quantities get the unit check
  too: a fill that is not a whole lot multiple, or larger than what was sent, is never booked and
  never becomes a SELL quantity — the leg is quarantined and the kill trips loud; the stop is
  still watched and alerted; the owner's way out is `live-book-external ID --price X --yes` or
  `--not-filled`, which clears the quarantine on his word (R4-05). On a quarantined EXIT (the
  record holds the lot) `--not-filled` is refused with nothing changed (R5-05). The broker has
  REPORTED a fill, so the way out is to sell in the app if the lot is still held, then `--price X`.
  `live-confirm-held ID --yes` clears an exit quarantine only when the order and the trade book read
  that SELL terminal with NO fill (a partial fill counts); otherwise it is refused, and the selling
  guard also refuses while a cleared SELL of the record shows a fill (R6-01). The trade book gets the same
  check. A definite gateway refusal (100008, 429, on any HTTP status) is not an attempt and never
  pushes an exit into slow mode (R4-10/20).
- Stop: **50% of the REAL fill**, watched by the bot only (no resting order, Q1). It fires when the
  **LTP** is at or below the level, OR a two-sided traded-today **bid** is (the owner's wording).
  Exit: LIMIT SELL at the bid (rounded DOWN), chased 10 times at 3 s, then once a minute with an
  alert. A wide book never blocks a due exit; only a missing bid defers it (alert every minute).
- Quote outage (Kite down or token dead): the engine keeps running on NEO; each holding is
  alerted every minute. The 09:25 exit is latched only on a day SHOWN trading: a polled option book
  that traded today, a completed mark, or NEO order/trade rows dated today. With none of those the
  alert says so instead of latching (an unlisted holiday looks exactly like this); the moment the
  day proves itself the 09:25 rule applies — from 09:25, never before.
- The SELL goes out BEFORE the EXIT DUE alert, and Telegram is posted from a worker thread through
  a bounded queue (200; the oldest is dropped and counted) so a blackholed Telegram can never hold
  an order. `flush` (before every exit of the process) waits for the post in flight, bounded; a
  failed post is retried 3 times; a dead worker is noticed and the alert is posted synchronously
  with a 3 s timeout, always after the order (R4-01/19). Live has its OWN switch,
  `neo.telegram_enabled`.
- The order ceiling (`max_orders_per_day`) never stops an exit: past it the kill trips (no new
  entries) and exits keep their minimum interval. Probe sends and definite gateway refusals do
  not count.
- Kill: **Rs 75,000 drawdown from peak equity, open marks included** — stops NEW entries only, exits
  always run. A non-finite equity (a NaN price in the book) trips it loud rather than disabling it;
  `live-book-external` refuses a price that is not finite, positive and within 1/50..20x of the
  entry. `live-resume --yes` writes a RESUME request that the running loop applies (peak resets
  to current equity); it is applied directly only when no loop is running.
- Reconcile at start and every 5 min, summed by symbol. **Nothing is ever cancelled on a restart and
  nothing is auto-flattened.** A shortfall, a surplus (extra lot, unexplained long of our family)
  and a short all need two reads outside `neo.positions_lag_sec` and after the open before they
  trip the kill (loud, every minute); the first read is reported and blocks entries. A row that
  shows only today's SELL of a lot this book sold today is the row shape without carried quantities
  (measurement 1), not a short. A lot the owner confirmed today is known to reconcile. A
  calendar-closed day with a holding is still watched.
- **A real record outranks every setting.** `live-run` reads `state_live/book.json` first: with an
  open or pending record it runs the live book exits-only whatever `execution`, `lots`, ARM or the
  gate say, and names the record in the alert. A broken paper overlay falls back to the tracked
  defaults. If the credentials or the live config cannot be read, it alerts every minute naming
  the position AND re-reads the file every minute, so a repaired file starts the engine without a
  restart. Every owner command (`live-status`, `live-report`, `live-resume`, `live-digest`,
  confirm/book) addresses the LIVE book whenever a live record exists, and the rehearsal book
  under `execution: rehearsal` otherwise, so Stage 2 can rehearse them (R4-15). Before the open
  the loop's heartbeat says `pre-open`, and a command then prints `QUEUED ... applies at 09:15`
  (R4-07). With no loop running a command that needs the broker (an order in flight) says
  `NO ENGINE IS RUNNING` and leaves the request for the next cron tick (R4-14).
- **Live's rule values come from the tracked defaults plus LIVE's own overlay** — never the paper
  overlay (R4-04). Where they differ from paper the arming line says `RULES DIFFER FROM PAPER`
  (pairing broken; information only). An open record keeps the exit time it was filled under
  (`exit_rule_time`) and its stop level; later edits cannot move them.
- The book is written only when it changes (content hash; a changed save costs ~10-20 ms with the
  directory fsync, an unchanged one ~0.01 ms). The loop logs a `TICK timing` line every minute:
  last/mean/max tick ms, saves vs skipped, Telegram pending/dropped — read it on the Pi.

### Commands
```bash
python -m nifty_btst live-run              # the live/rehearsal loop (cron target); --once, --force
python -m nifty_btst live-status           # arming line, heartbeat, kill, pending, open, unresolved intents
python -m nifty_btst live-report           # scorecard of the live book (never-filled entries listed separately)
python -m nifty_btst live-digest           # (re)send today's live EOD digest (refused while the loop runs)
python -m nifty_btst live-resume --yes     # OWNER: request a kill reset (stamped request file)
python -m nifty_btst live-confirm-held ID --yes          # OWNER: the lot is held today (checked in the app); opens a pending record too
python -m nifty_btst live-book-external ID --price X --yes   # OWNER: sold it in the app; --not-filled for a pending entry
# every owner command echoes APPLIED / NOT APPLIED <why> / not read by the loop; they address the LIVE book when a live record exists
python -m nifty_btst neo-probe [--symbol KITESYM]        # Stage 0, read-only (no order path exists in it)
python -m nifty_btst neo-order-probe --symbol KITESYM --confirm-real-order [--step all|hold|sell] [--max-premium 500] [--allow-missing-row]
```
Exit codes add **6 = refused** (arming, credentials, mode). Loop: NEO orders serviced every 1 s,
Kite quote + rule every `neo.poll_seconds`; a missing or dead Kite token never stops the process.

### Config
Tracked: the `neo` block of `btst_config.defaults.json`. Untracked LIVE overlay:
`btst_live_config.json` with `{"execution": ..., "neo": {...}}` (merged key by key; the paper
overlay `btst_config.json` is not read by live at all — rule keys such as `next_day_exit` or
`lots` placed in the LIVE overlay apply to live only). Keys: `credentials_file` (relative to `BOTS/`),
`poll_seconds` 3, `entry_cap_pct` 5, `entry_timeout_sec` 5, `max_entry_attempts` 5,
`exit_chase_sec` 3, `exit_chase_fast_max` 10, `exit_retry_slow_sec` 60, `unknown_recheck_sec` 3,
`unknown_grace_sec` 600, `exit_unknown_grace_sec` 90, `kill_drawdown_rs` 75000, `max_premium_rs` 15000,
`max_entry_orders_per_day` 10, `max_orders_per_day` 100, `funds_margin_pct` 10,
`limits_net_field` "Net", `reconcile_sec` 300, `order_poll_sec` 1, `expiry_backstop_time`
09:30:00, `sell_refused_alert_sec` 60, `exit_defer_alert_sec` 60, `brokerage_per_order` 0 (NEO API
plan), `fake_fill_delay_sec` 2 (rehearsal), `volrix_csv` (the pass-mark CSV; REQUIRED for live
entries), `arm_max_days` 35, `empty_report_stcode` 5203, `empty_report_emsg` "No Data",
`empty_ok_without_data` false, `empty_report_order_book|positions|trade_book|holdings` (per-report
`{"stcode": .., "emsg": ..}`, null = the default shape), `auth_stcodes` [1003],
`transient_stcodes` [1005], `rejected_stcodes` [1004, 1006, 1007, 1009, 100008] (any other
`Not_Ok` on a send is UNCLEAR), `retry_stcodes` [100008, 429] (definite refusals, retried after a
3 s doubling to 60 s backoff), `telegram_enabled` true (LIVE's own switch), `holdings_source_enabled` false,
`position_qty_unit` shares|lots, `positions_lag_sec` 60. In code, not config: `MAX_LIVE_LOTS = 1`,
order-probe premium ceiling Rs 1,000, the refusal backoff 3 s doubling to 60 s (R5-03; the cap is a
safety bound an overlay must not raise), product NRML. A key in the `neo` block that the code does not
know is named in the arming line (`UNKNOWN neo keys ignored`). The rule values live shares with
paper (`stop_frac`, `next_day_exit`, `trigger_mult`, `entry_start`/`entry_end`, `market_close`,
`lots`) are compared with PAPER's effective values at every start: a difference is printed as
`RULES DIFFER FROM PAPER` in the arming line, as information (it breaks the pairing); it blocks
nothing.

### Arming — three separate things
1. **Run:** `btst_live_config.json` says `"execution": "live"` and `BOTS/data/neo_credentials.json`
   exists. Without these the live process refuses to start (exit 6).
2. **Strategy entries** need BOTH: `state_live/ARM` holding a date `YYYY-MM-DD` (today <= date, at
   most 35 days ahead) AND the pass mark reading **PASS** (`neo.volrix_csv` set to the Volrix export,
   `compare.gate` against the paper book) or **WAIVED** (`neo.pass_mark_waived` set to a date, the
   owner's decision of 2026-11-26). No CSV, an unreadable CSV, IN PROGRESS or FAIL without a
   waiver = **NOT ARMED**. Both are re-read every minute: deleting ARM disarms a running process. Unarmed, the
   process still runs every exit, reconciles and alerts — it opens nothing new.
3. **The order probe** needs `state_live/PROBE_ARM` holding TODAY's date, `--confirm-real-order`,
   market hours, 1 lot, premium under the ceiling. The strategy ARM never arms the probe, and
   PROBE_ARM never arms strategy entries.

### Credentials (never in the repo)
`BOTS/data/neo_credentials.json`, git-ignored by `**/neo_credentials.json`:
```json
{"consumer_key": "", "mobile_number": "+91...", "ucc": "", "mpin": "", "totp_secret": ""}
```
TOTP is computed in the stdlib from the base32 secret; nothing else is installed. Tokens are never
logged; the UCC, account name and the `actId`/`usrId` keys of broker rows are redacted from probe
output and from every Telegram text. Local logs under `state_live/` and `logs/` are git-ignored and
may still carry broker text verbatim (RMS rejection strings): do not paste them anywhere public.

### Stage 0 — what the owner does at the box (read-only)
```bash
cd /home/trustit/Desktop/BOTS/Strategies/NIFTY_BTST
../../CROCODILE/venv/bin/python -V                      # 3.8 is fine; the code needs nothing newer
../../CROCODILE/venv/bin/python -m pytest -q -p no:asyncio tests   # expect ALL GREEN (tests/test_io.py needs kiteconnect: the venv has it)
curl -4 ifconfig.me                                      # must equal the IP whitelisted on the NEO dashboard
# write ../../data/neo_credentials.json (shape above), chmod 600
../../CROCODILE/venv/bin/python -m nifty_btst neo-probe --symbol <today's marked weekly from `status`>
```
Read the probe output for: `baseUrl`; the scrip-master row (`pTrdSymbol`, `pSymbol`, lot 65,
derived expiry = Kite's expiry); the positions / order book / trade book **envelopes** (set
`neo.empty_report_stcode` / `empty_report_emsg` to the empty shape you see, or
`empty_ok_without_data: true` if an empty book is `stat: Ok` with no `data`); the **limits keys**
(set `neo.limits_net_field` to the one holding available rupees — the funds check fails closed
without it). Then `execution: rehearsal` for a few sessions including an overnight
(`state_rehearsal/`), then Stage 2 review.

### Stage 3 — the order probe (REAL, 1 lot, cheapest far-OTM weekly, inside market hours)
The probe refuses to run while `state_live/run.lock` is held. (A second NEO login does NOT kill
the first -- measured 2026-11-27, see below -- but the refusal stays: two processes sending orders
on one account is still the wrong shape.) `execution` must read `live` while it runs: on a
rehearsal box set it for the run only and restore the file after, as was done on 2026-11-27.
```bash
echo 2026-MM-DD > state_live/PROBE_ARM                   # today's date; expires tonight
../../CROCODILE/venv/bin/python -m nifty_btst neo-order-probe --symbol <sym> --confirm-real-order
../../CROCODILE/venv/bin/python -m nifty_btst neo-order-probe --symbol <sym> --confirm-real-order --step hold
# next morning, PROBE_ARM re-dated. Run the sell AT 09:15:00 (owner, 2026-11-26): it is the one
# real measure of an opening fill; the fake broker fills at its own limit.
../../CROCODILE/venv/bin/python -m nifty_btst neo-order-probe --symbol <sym> --confirm-real-order --step sell
```
`all` measures: order visibility delay after a send, `GuiOrdId` echo, cancel latency. Any order the
probe cannot read as terminal STOPS the run for good (UNKNOWN; check the app); B is skipped when a
lot is already held; the premium ceiling is re-checked on the live quote before every BUY. `hold`
keeps one lot overnight on purpose and journals it. `sell` prints what positions, the order book, the
trade book and Holdings show for it at the open (**is there a carry-forward row? did yesterday's
orders leave the book? does Holdings list it?** — Holdings is read for the measurement whatever
`holdings_source_enabled` says), then sells it through the selling guard; with the row absent it
refuses unless BOTH `--allow-missing-row --owner-confirms-held` are given AND the journal shows the
hold filled with no FILLED probe SELL since (a placed-then-cancelled SELL is not a sale). A SELL
with a fill on the symbol today refuses every `sell` run, whatever the positions report says, a
live SELL refuses it whatever the flags say, and a second `all` on the symbol refuses before
buying (R4-06/22); rows are read by date. A probe SELL whose outcome was never read (an unclear
send, still UNRESOLVED in the journal) refuses every probe SELL on the symbol that day, and `all`
before it buys (R5-10). A positions read that fails prints `row UNKNOWN` and sends nothing. The NOT FLAT
report at the end means the reports may be lagging: do NOT re-run; check the app. Every step journals to
`state_live/intents.jsonl`, always reports what is held, and always sends a Telegram.

### Measured on the Pi, 2026-11-27 (Stage 0 + Stage 3, owner-approved, 1 lot, account flat after)

| fact | measured |
|---|---|
| static IP | the whitelisted primary IP is the home router's public IPv4 (Pi and laptop share it at home) |
| empty report (order book, trades, positions) | `{"stat":"Not_Ok","stCode":5203,"errMsg":"No Data","desc":"data not found"}` at HTTP 200 -- the message key is **`errMsg`**, not `emsg` (fixed in `neo_client` K0, commit `2e54431`; config default `No Data` now matches) |
| `GuiOrdId` | **echoes our tag** |
| resting BUY -> cancel | visible `open` at once; cancel confirmed in 0.1-0.2 s |
| cancel success reply | `{"result":"<order no>","stat":"Ok","stCode":200}` -- no `nOrdNo` (fixed: was read as UNCLEAR) |
| cancel of an unknown order | HTTP 400 `{"stCode":1018,"errMsg":..,"stat":"<same text>"}` |
| place with a bad symbol | HTTP 400 `stCode 1007` (a documented refusal) |
| BUY at ask / SELL at bid | filled at once (8.80 / 8.75, NIFTY 26450 CE weekly) |
| **SL-L** (`pt "SL"`, trigger + limit) | rests as `trigger pending` in 0.2 s; cancel 0.1 s |
| **SL-M** | **REJECTED by RMS for options** after the API returned an order number: `STOP LOSS MARKET ORDER NOT ALLOWED IN OPTIONS` |
| trade-book rows | one row per FILL: `rptTp "fill"`, `fldQty` = fill shares, `qty` 0 |
| positions | `flBuyQty`/`flSellQty` in SHARES (130/130 for two 65-lot round trips) |
| second login | **does NOT kill the first** (reads, 3 checks) |
| unexplained | one HTTP 401 on PLACE at 13:32:47, 5 s after a fresh login; nothing placed; place auth fine 10 min later |
| charges (contract note 11-27) | 4 fills debited **Rs 12.68**: brokerage Rs 0.01 (effectively 0 -- `brokerage_per_order` 0 confirmed), STT Rs 2.00, exchange Rs 0.78, GST Rs 0.14, SEBI/stamp ~0; price loss Rs 9.75 on top. Live size (~Rs 150 premium, 1 lot) ~Rs 23 statutory per round trip, as modelled. Funds fell Rs 52.20, so Rs 39.52 is NOT on the note (likely margin still blocked at the read) -- check the Kotak ledger 11-30 |

The shared `kotak_neo` package (BOTS root) now owns the NEO login for every bot (one session,
`~/.kotak_neo/`, crons 08:40/08:55). BTST still logs in itself until it migrates (plan K5); the
measurement above is why the two can coexist meanwhile.

**Measurements Stage 0/3 must produce** (the code's defaults are assumptions until then):
1. Positions report: does a carried lot have a row at 09:00 and at 09:15, and what does the row show
   after it is sold today? -> if no row, `live-confirm-held` becomes a required morning step (the
   coordinator raises it with the owner) or `holdings_source_enabled` if Holdings lists it.
2. Does Holdings list a carried NRML option? -> `neo.holdings_source_enabled`.
3. The empty envelope of each report separately -> `neo.empty_report_<report>`.
4. How long the order book and positions lag after a send and after a fill -> `unknown_grace_sec`
   (600) and `positions_lag_sec` (60).
5. ~~Whether a second login kills the first~~ -- MEASURED 2026-11-27: it does not (reads).
6. Whether `flBuyQty` and related fields are in shares or lots (`positions.md` says to divide F&O
   values by `lotSz`) -> `neo.position_qty_unit`. A wrong setting trips the kill loud, never reads flat.
7. Which `Not_Ok` codes a placement really returns -> `neo.rejected_stcodes` / `neo.retry_stcodes`.
8. Whether Holdings still lists a lot sold in the app EARLIER TODAY or YESTERDAY (R3-18). The
   no-counting-SELL rule catches a sale made today; a Holdings line stale by a DAY would not be
   caught by anything, which is why `holdings_source_enabled` stays false until this is measured.
9. The `TICK timing` line in the rehearsal log after a full session: tick ms, saves vs skipped.

### Cron (live), beside the paper line, its own lock and log
```
*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Strategies/NIFTY_BTST && flock -n /tmp/nifty_btst_live.lock ../../CROCODILE/venv/bin/python -m nifty_btst live-run >> logs/cron_btst_live_$(date +\%Y\%m\%d).log 2>&1
```
**Restart only outside market hours** (`pkill -f "nifty_btst live-run"` after 15:40 or before
09:00): the loop holds the code it started with, and a restart inside the session is a gap in exit
watching plus a reconcile that must find the book exactly as the broker has it. After a `git pull`
that touches this folder, both loops need the restart; the paper one is harmless any time.

### Logged changes to the paper configuration
- 2026-11-26: the 09:25 exit is no longer set due on a day the option has not traded (an
  unlisted closure, 2026-11-24, carried it to the next session's 09:15 open: trade #11 sold 09:15:03
  at 119.20, rehearsal 09:25 at 100.40; #11 is left as booked). New `open_log_sec` (60): every poll
  of the opening minute is logged as POLL (evidence only, no trading change).
- 2026-11-02: `fee_rates.stt_sell_pct` 0.10 -> 0.15 (STT on option premium sold is 0.15% since
  2026-04-01, per the NEO audit). Stored paper `net_pnl` / `net_ex_brokerage` change at the next
  paper restart; the pass mark (`compare.gate`) uses prices only, so it is unaffected.

### Not verified against NEO (until Stage 0/3)
Weekly `pTrdSymbol` spelling; the `pExpiryDate` epoch offset; `GuiOrdId` echo (the code falls back
to symbol+side+qty+time) -- GuiOrdId, the empty envelopes, the limits field `Net` and the second
login were MEASURED 2026-11-27 (table above); still open: session lifetime; the `sId` query parameter; the seven measurements above;
whether `trdSym` in positions equals `pTrdSymbol`; whether Holdings lists an NRML option at all; the
order-book status strings beyond the documented ones (`trigger pending`, `modified`,
`cancel pending` are treated as live; anything else is alerted and treated as live).

**Nothing in this section has been run on the Pi.** The `fcntl` lock branch, the directory fsync and
the worker threads have run on Windows and under WSL2 Ubuntu (python 3.12, all green without
`tests/test_io.py`, which needs kiteconnect; the lock is held across processes for the whole run and
released explicitly in `finally`, never left to the garbage collector; the last alert survives a
real process exit), not yet on the Pi.
