# ETF_PI live executor — build spec (2026-11-27)

Design of record: `Analysis/ETF_PI/tasks/DESIGN_neo_golive_execution.md` (2026-11-26, Fable) + the owner's
answers recorded there. This file is the ADAPTATION to what exists on 2026-11-27 and the owner's evening
decisions. Where they differ, this file wins. Every rule below has an ID used by the code and the tests.

## 0. Owner decisions (2026-11-27 evening)
| ID | Decision |
|---|---|
| O1 | Build the full executor now; 1-lot LIVE test Mon 2026-11-30; live 2026-12-01 at 1 lot. |
| O2 | Entry AND exit fully automatic. No step anywhere needs a human ("needs_human" is an ALERT, never a stop). |
| O3 | Product **MIS** (owner, later 2026-11-27 via the BTST session: NEO position rows are per product, so MIS never collides with BTST's NRML). Kotak MIS F&O auto square-off 15:20/15:25 (+Rs50/order) → live EOD `eod_time` 15:15 (owner to confirm). Account cap reads ETF's product row only; no row = unknown. |
| O4 | NEO has no modify in `kotak_neo` → every re-price = cancel → confirm terminal → place remainder. |
| O5 | Old `neo/` bridge hard-disabled (D17). `trade_enabled` ignored forever. |
| O6 | Work by TAG only. BTST may trade the SAME strike in parallel. ETF never reads another bot's lots as its own, never sells them, never cancels an order it did not tag. |
| O7 | Live location stays `~/Desktop/Scripts/bots/ETF_PI` (cron paths unchanged). Code from git. |
| O8 | Dead-man = healthchecks.io ping URL (Pi `credentials.json` section `healthchecks.url`). |
| O9 | Arming = executor config `mode: live`, `lots`, `live_until` (date) + KILL file. No daily ARM file. |
| O10 | (2026-11-28) Live LAST ENTRY 14:50 (whole minute) = paper's 25-min runway before the live EOD 15:15. Executor also refuses NO_ENTRY_WINDOWS itself. Journal records which sub-rule fired (`exit_detail`), quote snapshots, NEO call ms, entry slip, charges_est/net_est; logs in `state/executor/logs/` (not the 30-day cleanup); digest shows executor vs paper per key. |

## 1. Processes
| Piece | Runs | Owns | Never |
|---|---|---|---|
| `option_buy.py` (cron/min) | system python3 | signals, PAPER ledger, levels; writes an INTENT file for each NIFTY 10M paper entry | orders, NEO |
| `executor` daemon | systemd `etf-executor.service`, Restart=always 5 s | NEO orders, live book (journal), Kite ticks, live exits | write the paper ledger |
| `executor watchdog` | cron every minute 09:00-15:59 | heartbeat age → restart + Telegram | orders |

`python3 -m executor run` from the ETF_PI dir. `kotak_neo` is path-imported from `bots_root`
(Pi `/home/trustit/Desktop/BOTS`). The daemon imports `option_buy` ONLY to call `_ladder` and read its
constants (one copy of the exit rules); it detaches option_buy's file handlers after import.

## 2. Intent (option_buy → executor)
- Written in `_open_pos` after the paper row is saved, only when `U.name == 'NIFTY'` and `tf == '10minute'`.
  Atomic (tmp + fsync + rename) to `state/executor/intents/<key>.json`. Never raises into option_buy.
- Fields: key, created_at (ISO IST), kite_symbol, exchange, strike, expiry, opt_type, signal_tf, signal_ltp,
  st_value, t2_value, t2_tf, lot_size, tick_size, entry_bid, entry_ask.
- I1 an intent older than `intent_max_age_s` (20) when first read → SKIP (EXPIRED).
- I2 each intent key is processed once (journal), the file then moves to `intents/done/`.

## 3. Position state machine
`ENTRY_PENDING → ENTRY_WORKING → OPEN → EXIT_WORKING → CLOSED`, side exits: `SKIPPED`, `UNFILLED`.
Every transition is a journal line carrying the full position dict (rebuild = last line per key).

## 4. Entry (adapted 3.6, no modify)
- E1 gates (all must pass, else SKIP + Telegram): mode live/paper; not KILL/FLATTEN; daily loss not hit (R1);
  fault streak < 2 (R2); open+working positions < `max_open` (2); no own position already in that symbol;
  clock ≤ last entry 15:00 and session open; Kite price fresh (≤ 5 s); lots ≤ ceiling floor(capital/50k);
  live only: today ≤ `live_until`, NEO session usable, funds ≥ ask×qty×1.10.
- E2 price cap = min(signal_ltp + max(3 ticks, 0.3%), ST×(1 − MIN_REWARD_PCT/100), ST − MIN_T1_DISTANCE),
  rounded DOWN to tick. Ask > cap → SKIP "price ran".
- E3 limit = depth-walked ask for qty (first ask level whose cumulative qty ≥ qty), clipped to cap.
- E4 tag persisted (journal fsync) BEFORE the send. Then poll the order by tag every 0.5 s.
- E5 not complete within `entry_reprice_s` (2 s): cancel → confirm terminal. Filled > 0 → OPEN with the filled
  qty (the rest is NOT chased). 0 filled → re-read ask, re-check cap, place again; max `entry_attempts` (3)
  places in all → UNFILLED.
- E6 Unclear place: resolve by tag only, NEVER re-sent. Absent after `unclear_grace_s` (60) with the book
  readable → UNFILLED, but the tag stays on the watch list all day: a late fill on it is ADOPTED (position
  opened at its fill price and immediately managed, with a Telegram).
- E7 OPEN re-bases the ladder on the FILL: entry_premium = avg fill, peak = avg fill, entry_time = fill time.
  T1 (st_value) and T2 are absolute and unchanged.

## 5. Exit triggers (design 3.5/3.6, owner Q2/Q3/Q5/Q15)
| ID | When | Price used |
|---|---|---|
| X1 minute sample | first tick at/after second 4 of each minute: peak = max(peak, LTP), then `_ladder(curr=LTP)` exactly as paper (TIME_SL, ADV_SL, T1 arm/lock, cost floor, trail, PREM_SL, TP) | LTP |
| X2 TP | every tick: target = T2 if T2>0 else T1; best bid ≥ target | bid |
| X3 floors | every tick: floor = max(cost/T1 floor if armed, trail if peak gain ≥ 8%); bid ≤ floor for 5 s, or bid ≤ floor×(1−0.5%) at once. Exit type = TRAIL if trail is the binding level else the floor's sl_reason | bid |
| X4 PREM_SL | every tick: bid ≤ entry×(1 − 35%) | bid |
| X5 ADV backstop | every tick: peak gain < 3% and bid ≤ entry×(1 − 12%) | bid |
| X6 T1 arm | every tick: not t1_hit, T2>0, LTP ≥ T1 → run `_ladder(curr=LTP)` (arms the give-back floor; any exit it returns is taken) | LTP |
| X7 EOD | 15:25 (U.eod_close of that day) | clock |
| X8 no price | no tick and no REST quote for 30 s → Telegram every 30 s; 120 s → FLATTEN that position from the last bid | clock |
| X9 KILL / FLATTEN | FLATTEN file → exit all now (implies no entries). KILL file → no new entries only | file |

## 6. Exit order (no modify)
- X10 one working SELL per position, at most. qty = min(position remaining, OWN tagged net (R3), account net).
- X11 price schedule from the trigger: step 0 = depth-walked bid for qty (TP: the bid); each step not complete
  after `exit_step_s` (2 s) → cancel → confirm terminal → remainder at bid×(1−0.3%), then −0.6%, −1%, then
  −2% every step thereafter; always LIMIT, tick-rounded DOWN, never below one tick.
- X12 the chase never stops while the position holds lots. Every `exit_alert_every_s` (60) of chasing → Telegram.
  Unreadable book/positions → nothing is sent that step; alert after 3 in a row (O2: alert, keep trying).
- X13 Unclear sell: never re-sent on time alone; only after `unclear_grace_s` AND no live own sell on the
  symbol AND own net still holds the lots (kotak_neo rule F1).
- X14 IP refusal (stCode 100008/1037): kotak_neo raises the static_ip alert; the chase keeps trying every step.

## 7. Risk
- R1 daily loss: realised P&L from OWN fills today ≤ −`daily_loss_per_lot`(3000)×lots → no new entries today.
- R2 fault streak: a definite reject / unclear place / chase trouble alert counts 1; a clean round trip resets.
  ≥ 2 → no new entries today (Telegram).
- R3 own net = Σ own-tagged fills in today's trade book (B − S) per symbol, plus carried qty from the journal for a
  position opened on an earlier day. NEVER the account position alone.
- R4 account net < own net in a symbol → alert (someone sold our lots); sell cap uses the smaller.
  Own net < 0 (we oversold, Q7) → auto BUY-to-cover the shortfall at the ask if our last sell was < 5 min ago,
  else alert only. Account short with own net ≥ 0 → alert only (not ours).
- R5 restart: rebuild from the journal, then before ANY order: read order book + trades, settle every working
  tag, recompute own net, adopt late fills; positions with lots → resume their exit rules.
- R6 a position still open after 15:30 → Telegram every 10 min until flat; next session it is exited at 09:15
  open (chase), never held for the ladder.

## 8. Prices
- P1 Kite WebSocket (KiteTicker, full mode = 5-level depth) in a thread; subscribe on intent and for every open
  position; token re-read from `kite_access_token.json` on auth error/reconnect.
- P2 stale (> 5 s since last tick for a symbol we manage) → REST quote every 2 s through option_buy's
  cross-process Kite limiter.
- P3 tick tape: `state/executor/tape_YYYYMMDD.jsonl` (time, symbol, ltp, bid, ask) for managed symbols.

## 9. Ops
- H1 heartbeat `state/executor/heartbeat.json` each loop; healthchecks ping every 30 s while running.
- H2 watchdog (cron/min, 09:00-15:59 trading days): heartbeat > 90 s → `systemctl restart etf-executor` + Telegram.
- H3 Telegram: entries, fills, exits, skips, every abnormal state (via `common.sendMsgTelegram`).
- H4 status file `state/executor/status.json` (mode, positions, realised, kill flags, faults) for humans/digest.

## 10. Paper mode
`mode: paper` = same code path with `SimBroker`: buys fill at the depth-walked ask, sells at the bid,
after 0.5 s. Used for the pre-open dry run and the replay test.

## 11. Not in stage 1
Disaster SL-L; modify; MIS; trail-80; 10 s entry scan; partial-entry chase; multi-lot slicing (10 lots = 650 <
freeze 3,510).

## 12. Known interference outside ETF (to fix before BTST goes live, not before 12-01)
BTST's live engine pulls its own exit when it sees a SELL on its symbol it did not send (`_foreign_sells`).
An `etf-` sell of ETF's own lots on the same strike would trigger that. BTST stays rehearsal (Q8); the
BTST side must learn other bots' tags before it is armed.

## 13. Fable review 2026-11-27 — adopted (these override §1-§12 where they differ)
| Fable | Adopted rule |
|---|---|
| F1/F2 | `kotak_neo.NeoClient(exclusive=False)`: no claim/owners, no release. `chase_exit` extended (not forked): `held_fn`, `price_fn`, `max_reprices=None`, `max_troubles=None`; tests `kotak_neo/tests/test_shared_strike.py`. |
| F3/F5/F6 | OWN net = ORDER-BOOK filled qty of `etf-` orders (B−S) (+ journal carried qty). Sell cap = own net; the account net lowers it only when it is below own net for longer than `positions_lag_s` after our last fill (then alert). Unclear-sell re-send needs: grace passed, book readable, no live `etf-` sell, own net still ≥ remaining. Residual risk stated: an `etf-` sell invisible in the order book yet filled for longer than the grace. |
| F4 | `unclear_grace_sell_s` 10 (sells), `unclear_grace_s` 60 (entries); M3 measures. |
| F8 | No new order until the previous tag is terminal in a readable book with its final fill applied; re-cancel each step (idempotent); place never re-sent. |
| F9/F10 | Every kotak_neo call wrapped (any NeoError = unreadable this step, alert); `NeoClient(timeout=3)`; `resolve(wait_s=0)` only; heartbeat from the main loop. Unit `StartLimitIntervalSec=0`. |
| F11 | `orders_tagged`/`own_orders` parse only the rows they select: a bad foreign row cannot blind ETF. |
| F12 | X1 minute `_ladder(LTP)`: ARMING kept (t1_hit, floors, lock, peak); its exit taken only for TIME_SL and ADV_SL. Floors/TRAIL/PREM_SL/TP fire only from the tick rules on the BID (X2-X4). X6 tick T1 arm: arming only. |
| F13 | bid ≤ 0, ask ≤ 0 or bid > ask = no price (no trigger, counts toward X8). |
| F14 | Floor timer starts at the first breaching bid; any bid above resets; fires at +5 s on the clock if the last bid (≤ 5 s old) still breaches. Trail level changes only at X1. |
| F15 | Stale-price flatten: cumulative decrement last bid × (1 − 2%·n). Definite reject → back off `reject_backoff_s` 5 s, alert on the first. |
| F16 | Stop placing at 15:29:50; no `etf-` order live → state CARRY; resume 09:15:05 from the journal qty; alert every 5 min on an expiry-day carry. |
| F17 | A watched (unclear) entry tag seen RESTING is cancelled at once; a late entry FILL is adopted and EXITED at once (no stale ladder), counts as a fault. |
| F18 | ScripMap loaded at start and at 09:00; mapping (from_kite, lot cross-check) BEFORE the gates; miss = SKIP + Telegram; neo_symbol/token journalled on the position. |
| F20/F21 | Assert `option_buy.U.name == 'NIFTY'`; thresholds read from option_buy constants; never `option_buy.EOD_CLOSE_TIME`; constants hash in status. Pi reboots daily 08:00 → fresh import each day. |
| F22 | HTTP 429 = transient (no fault). Fault = definite/RMS reject of an entry, an unclear entry place, an exit chasing > 60 s. |
| F23 | Torn final journal line ignored; corrupt middle line = no new entries + alert, exits continue from the last good line per key. |
| F24 | Fills must be whole lots, else alert and manage the floor-lot qty. Entry UNFILLED only after one more read 1 s after the terminal cancel. |
| F19 | BTST live-run cron is active (rehearsal) on the Pi: check before Monday that it cannot alert/kill on ETF lots (runbook M7). |
