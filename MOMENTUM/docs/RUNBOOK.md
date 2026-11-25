# MOMENTUM — Pi runbook (December 2026 slice: buys only, 5L)

Design: `SRS/MOMENTUM_FROZEN_DESIGN.md` · Build spec: `SRS/MOMENTUM_BUILD_SPEC_DEC1.md` · Log: `docs/BUILD_LOG.md`.

## 1. Install (owner at the Pi)
1. `git pull` (same as FIFTY), then copy with WinSCP:
   - `secrets/telegram.json` → `{"bot_token": "<BotFather token>", "chat_id": "<your chat id>"}`
   - the NSE data seed (one time) → the folder named by `nse_data` in config (default `../../NSE_DATA`)
2. `cd ~/Desktop/BOTS/MOMENTUM && bash deploy/setup.sh` → venv, wrappers, crontab block. Prints the manifest check.
3. `./momentum isolation-test` → must print `"pass": true` (the vet has no tools on this CLI version).
4. `./momentum canary` → Telegram silent = OK; an ALERT means the vet path is broken.
5. Send `status` to the new bot in Telegram → the book status comes back (poller works).

Nothing trades until BOTH: `config/momentum.json` has `"mode": "live"` AND `control/ARM.json` covers the month.

## 2. Dry runs (no orders; everything goes to `state_dryrun/`)
- DRY-1 evening: `./momentum evening --dry --as-of-signal 2026-11-27` → the plan, audit flags, Telegram "[DRY]".
- DRY-2 afternoon (Mon 30 Nov 14:40): `./momentum afternoon --dry` → real quotes, vet call, limits, quantities; no order.

## 3. Arm December (owner, by hand, after the dry runs pass)
`config/momentum.json`:
```json
{"mode": "live"}
```
`control/ARM.json`:
```json
{"phase": "P0", "plan_months": ["2026-12"], "mode": "automatic", "buy_cap_rupees_per_session": 550000,
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
`resolve <intent_id> found|absent`, `status`. Un-kill: delete `control/KILL` at the Pi.
