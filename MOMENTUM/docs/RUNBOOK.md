# MOMENTUM — Pi runbook (December 2026 slice: buys only, 5L)

**Account (owner 2026-11-25): the PERSONAL account YL6478, December 2026 to March 2027.** The book reads the token
the Helper bots refresh at ~08:45 (`BOTS/data/kite_access_token.json`), never logs in. Keep the 5L as FREE cash on
top of the option bots' margin: the 15:10 cash guard stops the buys otherwise. DDPI must be active on the personal
demat before the first sell (1 Jan). Only the book's own kill (`kill` / `control/KILL`) stops it. The company account
(QSK814, 50L) starts fresh from the April 2027 rebalance; it has no market-data permission, so that needs work first.

**Test size (owner 2026-11-27): 2L** (`book_cash_open: 200000` in the Pi overlay; ₹9,970 per name; ARM cap 2.2L).
Test rebalances 1 Dec, 1 Jan, 1 Feb, 1 Mar in YL6478; the 20 stocks STAY in YL6478 after the test (not sold).
YL6478 has no option trading on Kite any more (options moved to NEO), so the book's cash sits beside positional
holdings only. **Token dependency:** the YL6478 token is written at 08:45 by SNAIL's `main.py startup` cron line;
that line must stay, or the book fails closed every day (no token = no orders + ALERT).

Design: `SRS/MOMENTUM_FROZEN_DESIGN.md` · Build spec: `SRS/MOMENTUM_BUILD_SPEC_DEC1.md` · Log: `docs/BUILD_LOG.md`.

## 1. Install (owner at the Pi)
1. `git pull` (same as FIFTY), then copy with WinSCP:
   - `secrets/telegram.json` → `{"bot_token": "<BotFather token>", "chat_id": "<your chat id>"}`
   - the NSE data seed (one time) → the folder named by `nse_data` in config (default `../../NSE_DATA`)
2. `cd ~/Desktop/BOTS/MOMENTUM && bash deploy/setup.sh` → venv, wrappers, crontab block. Prints the manifest check.
3. `./momentum isolation-test` → must print `"pass": true` (the vet has no tools on this CLI version).
4. `./momentum canary` → Telegram silent = OK; an ALERT means the vet path is broken.
5. Send `status` to the new bot in Telegram → the book status comes back within ~30 s (the poller answers `status`
   itself; every other command and every button waits for the next order step: 08:35, 14:45, evening).

Nothing trades until BOTH: `config/momentum.json` has `"mode": "live"` AND `control/ARM.json` covers the month.

## 2. Dry runs (no orders; everything goes to `state_dryrun/`)
- DRY-1 evening: `./momentum evening --dry --as-of-signal 2026-11-27` → the plan, audit flags, Telegram "[DRY]".
- DRY-2 afternoon (Mon 30 Nov 14:40): `./momentum afternoon --dry` → real quotes, vet call, limits, quantities; no order.

## 3. Arm December (owner, by hand, after the dry runs pass)
After writing `"mode": "live"`: the poller restarts itself onto the live state within 5 minutes (or run
`./momentum-poller-keepalive` after `pkill -f "momentum.cli poller"`). Send `status`: the reply must NOT start with
`[DRY]`. Until then taps and commands would land in the dry-run inbox. Only `mode: automatic` ARMs trade in this
slice; an `approve_each` ARM places nothing (ALERT ARM_MODE).
`config/momentum.json`:
```json
{"mode": "live"}
```
`control/ARM.json`:
```json
{"phase": "P0", "plan_months": ["2026-12"], "mode": "automatic", "buy_cap_rupees_per_session": 220000,
 "valid_from": "2026-11-30", "valid_to": "2026-12-31"}
```
`control/fo_checked.json` (after reading the NSE F&O circulars for December additions; add any to
`SRS/data/fo_announced.json` first):
```json
{"2026-12": "2026-11-30"}
```

## 4. Daily
| IST | What you see in Telegram |
|---|---|
| 20:00-23:30 (signal day) | PLAN FINAL, AUDIT FLAG buttons (Real / Hold / Data error), or ALERT NO_PLAN |
| 08:35 | ALIVE |
| 14:45-15:35 | starting BUY step → step summary |
| 16:00 | HEARTBEAT |

Commands to the bot: `kill` (stops everything; cancels open book orders by tag), `hold <ISIN>`, `release <ISIN>`,
`resolve <intent_id> found|absent`, `filled <intent_id> <qty> <avg_price>`, `ack`, `status`. Un-kill: delete
`control/KILL` at the Pi.

## 5. Alerts that need you (review 2026-11-25)
| Alert | Meaning | What to do |
|---|---|---|
| `STARTUP ... torn write` | power cut while a journal line was being written; every command stops | at the Pi: `./momentum repair-journal` (moves the unfinished last line aside), then nothing else |
| `NO_PLAN` (08:35) | the ARM covers this month but no FINAL plan exists | `./momentum evening --as-of-signal <last session of last month>` |
| `PROBE_FAILED` | the first order was rejected / not seen in the order book; the other buys wait a session | read the reason in the alert before the next session |
| `HOLD <sym> ... rejected (REJECTED_RMS, 3 in a row)` | no cash for the book three sessions running | fund the account, tap Buy |
| `NO_CIRCUIT` | the quote had no upper circuit limit, so the limit could not be clamped | nothing; it retries next session (no lock try); if it hits every name, the Kite field changed |
| `THREE_WAY` (16:00) / `opening holdings below the book` (preflight) | Kite and the book disagree | check Kite, then send `ack` |
| `STALE_INTENT` / `ORPHAN` / `ORDER_MISSING` | an order was never reconciled | contract note → `filled ...` or `resolve ... absent` |
| `CANCEL_FAILED` | the 15:28 cancel was not confirmed | look at Kite's order book; DAY orders end at the close |
