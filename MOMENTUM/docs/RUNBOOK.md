# MOMENTUM — Pi runbook (December 2026 slice: buys only, 2L test)

**Account (owner 2026-11-25): the PERSONAL account YL6478, December 2026 to March 2027.** The book reads the token
kite_login writes at 08:30 (`BOTS/data/kite_access_token.json`), never logs in. Keep the 2.2L as FREE cash: the
15:10 cash guard is all-or-nothing (the 08:35 job says `CASH_AM` early when the account is short). DDPI must be active on the personal
demat before the first sell (1 Jan). Only the book's own kill (`kill` / `control/KILL`) stops it. The company account
(QSK814, 50L) starts fresh from the April 2027 rebalance; it has no market-data permission, so that needs work first.

**Test size (owner 2026-11-27): 2L** (`book_cash_open: 200000`, now also the tracked default; ₹9,970 per name; ARM cap 2.2L).
The live book opens only under an ARM whose session cap covers `book_cash_open` (review 11-28: a wrong overlay can
never open a bigger book). The opening cash is written once and is permanent.
Test rebalances 1 Dec, 1 Jan, 1 Feb, 1 Mar in YL6478; the 20 stocks STAY in YL6478 after the test (not sold).
YL6478 has no option trading on Kite any more (options moved to NEO), so the book's cash sits beside positional
holdings only. **Token dependency:** from 30 Nov 2026 both Kite tokens are made at 08:30 (retry + alert 08:36) by
`python -m kite_login morning` (BOTS root; see kite_login/README). The book only READS `BOTS/data/kite_access_token.json`
and never logs in. No token = no orders + ALERT (fail closed). It also needs `BOTS/data/holiday_calendar.json`,
which kite_login refreshes. No dependency on SNAIL, FIFTY or CROCODILE processes, cron lines or Telegram bots.

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
- DRY-2 afternoon (Mon 30 Nov 14:40): runs BY CRON (mode is still dry) → real quotes, vet call, limits, quantities;
  no order. Read the [DRY] Telegram messages and `state_dryrun/journal.jsonl` (dry_order notes). A hand-run while
  cron holds the lock just exits.

## 3. Arm December (after the dry runs pass) — DEADLINE Mon 30 Nov 19:45
The 30 Nov 20:00 evening builds the December plan in whatever mode is set at that moment: arm after DRY-2 (~15:40)
and before 19:45. Armed later, that evening plans into the dry book, 1 Dec says NO_PLAN, and the buys slip a day.
The morning catch-up (09:10-12:40) only re-runs a signal day that is still pending.
After writing `"mode": "live"`: the poller restarts itself onto the live state within 5 minutes (or run
`./momentum-poller-keepalive` after `pkill -f 'sys.exit\(main\(\)\) poller'`). Send `status`: the reply must NOT start with
`[DRY]`. Until then taps and commands would land in the dry-run inbox. Only `mode: automatic` ARMs trade in this
slice; an `approve_each` ARM places nothing (ALERT ARM_MODE).
`config/momentum.json` is the WHOLE overlay: add `mode`, keep every other key:
```json
{"claude_bin": "/home/trustit/.npm-global/bin/claude", "book_cash_open": 200000, "arm_cap_rupees_default": 220000,
 "mode": "live"}
```
Audit-flag buttons tapped in the dry chat do not carry over: the live evening sends its own (and the 08:35 job
re-sends any untapped hold button every morning; `release <ISIN>` clears any owner-clearable hold).
`control/ARM.json`:
```json
{"phase": "P0", "plan_months": ["2026-12"], "mode": "automatic", "buy_cap_rupees_per_session": 220000,
 "valid_from": "2026-11-30", "valid_to": "2026-12-31"}
```
**F&O check is automatic (D47, 2026-11-28): nothing to read or stamp by hand.** The evening downloads NSE's F&O
circulars (`fo-scan`, also daily at 19:05), a tool-less vet classifies them, code verifies every claim, and stocks whose
F&O contracts start on the plan month's first session are excluded before the band. At 14:45 on day 1 the backstop
checks Kite's real futures list: a planned buy with futures that day is not bought. `control/fo_checked.json` is only
read for a decision made before D47. Check by hand any time: `./momentum fo-vet --as-of-signal 2026-11-27`
(writes nothing to the book).

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
| `NO_PLAN` | no FINAL plan (sent once a day per cause) | nothing if it says it is retrying (evening 30-min, weekday mornings 09:10-12:40); otherwise tell Claude |
| `CASH_AM` (08:35) | Kite free cash is below today's book buys | add funds before 15:10 |
| `PROBE_MOVED` | the first order was rejected for a stock reason; that name waits, the next is the probe | nothing (tap Buy/Skip on its HOLD) |
| `REFUSED_BUT_PLACED` | Kite refused an order that then appeared in the book; adopted and booked | nothing |
| `CANARY FAILED ... config pins` | the Claude CLI changed version | tell Claude: isolation-test, then update claude_version |
| `HOLIDAYS` (from 15 Dec) | no next-year holiday list: every session next year would be blocked | tell Claude |
| `PROBE_FAILED` | the first order was rejected / not seen in the order book; the other buys wait a session | read the reason in the alert before the next session |
| `HOLD <sym> ... rejected (REJECTED_RMS, 3 in a row)` | no cash for the book three sessions running | fund the account, tap Buy |
| `NO_CIRCUIT` | the quote had no upper circuit limit, so the limit could not be clamped | nothing; it retries next session (no lock try); if it hits every name, the Kite field changed |
| `THREE_WAY` (16:00) / `opening holdings below the book` (preflight) | Kite and the book disagree | check Kite, then send `ack` |
| `STALE_INTENT` / `ORPHAN` / `ORDER_MISSING` | an order was never reconciled | contract note → `filled ...` or `resolve ... absent` |
| `CANCEL_FAILED` | the 15:28 cancel was not confirmed | look at Kite's order book; DAY orders end at the close |
| `FO_VET_FAILED` | the evening F&O vet failed until 21:30; the plan went FINAL without it (D47) | nothing; the day-1 check still guards. Tell Claude (scan / CLI / NSE format) |
| `F&O ENTRANTS` / `F&O INFO` | stocks starting F&O on day 1 were excluded / a circular worth knowing | nothing |
| `FO_ENTRANT` (14:45) | a planned buy has stock futures on day 1: not bought, earmark stays cash | nothing (research never buys an F&O member) |
| `FO_FALSE_EXCLUSION` | the band excluded a name that has no futures on day 1 (withdrawn circular) | nothing to undo; tell Claude |
| `PREFLIGHT ... F&O day-1 list unavailable` | Kite's NFO list / the day-1 F&O bhavcopy could not be read | nothing; no buys this session (no lock try), next session retries |
