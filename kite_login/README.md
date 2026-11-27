# kite_login — the one Kite login for every bot

Owner decision 2026-11-27. One package makes **both** Kite tokens every weekday
morning, plus the NSE holiday calendar. **No bot logs in to Kite on its own**:
`tests/test_login_policy.py` fails the commit if a Kite login endpoint appears in
any other tracked file. The only exceptions are the legacy files listed there,
and that list may only shrink.

It replaces:

| was | made | now |
|---|---|---|
| SNAIL 08:45 cron `main.py startup` | personal token `data/kite_access_token.json` | `kite_login` 08:40 |
| FIFTY daemon (08:50 slot inside `fifty-daemon.service`) | company token `FIFTY/data/kite_access_token.json` | `kite_login` 08:40 |
| SNAIL 08:30 cron `holiday_scraper --force` | `data/holiday_calendar.json` | `kite_login` 08:40 |
| ETF_PI `tracker.py` / `extract_data.py` web login (a 3rd account, enctoken) | their own `token.txt` | read the personal token via `ETF_PI/shared_kite.py` |

**The token files did not move.** Paths, keys and `generated_at` style are
unchanged: the personal token keeps a naive local-time stamp (as SNAIL wrote it),
the company token keeps `+05:30` (as FIFTY wrote it). None of the ~120 readers
changes. The company file stays under `FIFTY/data/` until its few readers
(pyramid, portfolio tracker, `Helper/pull_pi.sh`) move with it. Do not delete
`FIFTY/data/` before then.

## Rules

- Logs in **only** on the machine named in `~/.kite_login/LOGIN_HOST` (the Pi).
  The laptop has no such file.
- A token made after 06:00 today that Kite still accepts is **never** replaced:
  the run is a NOOP.
- A token from today that Kite **cannot be asked about** (network down) is left
  alone and reported as `UNVERIFIED`. The 08:46 slot retries.
- Kite's `profile()` user is checked against the credentials **before** the file is
  written. The company token can never land in the shared market-data file (the
  company account has no quote permission, so every bot would go blind).
- The file is written atomically (temp file, then rename), so a bot never reads
  half a file.
- Secrets never reach a log or `login_log.jsonl`.

## Files on the Pi (never in git)

| file | what |
|---|---|
| `BOTS/data/kite_credentials.json` (0600) | `{"personal": {api_key, api_secret, user_id, password, totp_secret}, "company": {...}}` — gitignored by `**/kite_credentials.json` |
| `~/.kite_login/LOGIN_HOST` | this machine's hostname. No file = no login |
| `~/.kite_login/login.lock` | one run at a time (flock) |
| `~/.kite_login/login_log.jsonl` | every account's outcome (no secrets) |

## Cron (Pi, Mon–Fri)

```
40 8 * * 1-5 cd /home/trustit/Desktop/BOTS && CROCODILE/venv/bin/python -m kite_login morning >> Helper/logs/cron_kite_login_$(date +\%Y\%m\%d).log 2>&1
46 8 * * 1-5 cd /home/trustit/Desktop/BOTS && CROCODILE/venv/bin/python -m kite_login morning --final >> Helper/logs/cron_kite_login_$(date +\%Y\%m\%d).log 2>&1
```

08:46 is a NOOP when 08:40 worked. `--final` sends one Telegram alert (shared bot,
`data/telegram_config.json`) if a token or the holiday refresh is still not right.
Both runs finish before the 08:50 instrument jobs.

## Commands (on the Pi, from `~/Desktop/BOTS`)

```
CROCODILE/venv/bin/python -m kite_login status          # offline: which tokens are from this session
CROCODILE/venv/bin/python -m kite_login morning         # what the cron runs
CROCODILE/venv/bin/python -m kite_login login --account company
CROCODILE/venv/bin/python -m kite_login holidays --force
CROCODILE/venv/bin/python -m kite_login import-legacy   # ONE-TIME, see cutover
```

| you see | means | do |
|---|---|---|
| `HOST_REFUSED` | no or wrong `~/.kite_login/LOGIN_HOST` | expected on the laptop; on the Pi `hostname > ~/.kite_login/LOGIN_HOST` |
| `FAILED credentials file missing` | `data/kite_credentials.json` absent | `import-legacy` (cutover) or recreate it, `chmod 600` |
| `FAILED password: ...` / `totp: ...` | Zerodha refused the login | check the password / TOTP secret in the credentials file; log in once in the Kite app |
| `FAILED ... another user ... NOT written` | the credentials for that alias belong to another account | fix the credentials file; nothing was overwritten |
| `UNVERIFIED` | today's token exists but Kite could not be reached | wait for the 08:46 retry; if it still fails, check the network |
| holidays `NOT overwritten` | Zerodha's page layout changed | yesterday's file is kept; fix `holidays.parse` |

## Cutover (each step needs the owner's OK; it is a live change)

1. `git pull` on the Pi, then run `python -m pytest kite_login/tests -q` there (that run
   also proves the real flock).
2. `CROCODILE/venv/bin/python -m kite_login import-legacy`, then
   `hostname > ~/.kite_login/LOGIN_HOST` and `chmod 700 ~/.kite_login`.
3. `crontab -l > ~/Desktop/BOTS/data/crontab_backup_YYYYMMDD.txt`, then add the two lines
   above. **Keep the SNAIL 08:45 line and the FIFTY daemon for one morning** as the
   fallback. Both skip their login when today's token is valid, so the overlap is
   safe.
4. After one clean morning (`status` shows both tokens made this session, and the
   08:40 log shows `LOGGED_IN`/`NOOP`):
   - remove the FIFTY `watchdog.sh` line first, then
     `sudo systemctl disable --now fifty-daemon snail-telegram`;
   - remove the SNAIL lines (08:45 startup, 08:30 holiday scraper) and the commented
     SNAIL/Bouncer/Sniper/Iron-condor blocks;
   - remove the CROCODILE jobs (morning startup … daily report, and the two log
     cleanups). **Keep `CROCODILE/venv`**: zebra, BTST, kotak_neo, GOLD, MOMENTUM,
     FIFTY and kite_login all run on its python.
5. Rollback: restore the crontab backup and `sudo systemctl enable --now fifty-daemon snail-telegram`.
