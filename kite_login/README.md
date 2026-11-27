# kite_login — the one Kite login for every bot

Owner decision 2026-11-27. One package makes **both** Kite tokens every weekday
morning, plus the NSE holiday calendar. **No bot logs in to Kite on its own**:
`tests/test_login_policy.py` fails the commit if a Kite login endpoint appears in
any other tracked file. The only exceptions are the legacy files listed there,
and that list may only shrink.

It replaces:

| was | made | now |
|---|---|---|
| SNAIL 08:45 cron `main.py startup` | personal token `data/kite_access_token.json` | `kite_login` 08:30 |
| FIFTY daemon (08:50 slot inside `fifty-daemon.service`) | company token `FIFTY/data/kite_access_token.json` | `kite_login` 08:30 |
| SNAIL 08:30 cron `holiday_scraper --force` | `data/holiday_calendar.json` | `kite_login` 08:30 |
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
  alone and reported as `UNVERIFIED`. The 08:36 slot retries.
- Kite's `profile()` user is checked against the credentials **before** the file is
  written. The company token can never land in the shared market-data file (the
  company account has no quote permission, so every bot would go blind).
- The file is written atomically (temp file, then rename), so a bot never reads
  half a file.
- Secrets never reach a log or `login_log.jsonl`.

## Files on the Pi (never in git)

Credentials stay **where they always lived** (owner 2026-11-27, no copy).
SNAIL and FIFTY no longer *run*, but **these two files must not be deleted**:

| file | what |
|---|---|
| `BOTS/SNAIL/config/creds.env` | personal account: `ZERODHA_API_KEY`, `_API_SECRET`, `_USER_ID`, `_PASSWORD`, `_TOTP_SECRET` |
| `BOTS/FIFTY/data/kite_credentials.json` | company account: `{api_key, api_secret, user_id, password, totp_secret}` |
| `~/.kite_login/LOGIN_HOST` | this machine's hostname. No file = no login |
| `~/.kite_login/login.lock` | one run at a time (flock) |
| `~/.kite_login/login_log.jsonl` | every account's outcome (no secrets) |

The tokens go to the same files as before: `BOTS/data/kite_access_token.json`
(personal) and `BOTS/FIFTY/data/kite_access_token.json` (company).

## Cron (Pi, Mon–Fri)

```
30 8 * * 1-5 cd /home/trustit/Desktop/BOTS && CROCODILE/venv/bin/python -m kite_login morning >> Helper/logs/cron_kite_login_$(date +\%Y\%m\%d).log 2>&1
36 8 * * 1-5 cd /home/trustit/Desktop/BOTS && CROCODILE/venv/bin/python -m kite_login morning --final >> Helper/logs/cron_kite_login_$(date +\%Y\%m\%d).log 2>&1
```

08:30 on purpose (owner): if anything fails there is time to fix it before
the 09:15 open. 08:36 is a NOOP when 08:30 worked. `--final` sends one Telegram
alert (shared bot, `data/telegram_config.json`) if a token or the holiday
refresh is still not right. MOMENTUM's 08:35 morning waits for the token;
the 08:50 instrument jobs need it.

## Commands (on the Pi, from `~/Desktop/BOTS`)

```
CROCODILE/venv/bin/python -m kite_login status          # offline: which tokens are from this session
CROCODILE/venv/bin/python -m kite_login morning         # what the cron runs
CROCODILE/venv/bin/python -m kite_login login --account company
CROCODILE/venv/bin/python -m kite_login holidays --force
```

| you see | means | do |
|---|---|---|
| `HOST_REFUSED` | no or wrong `~/.kite_login/LOGIN_HOST` | expected on the laptop; on the Pi `hostname > ~/.kite_login/LOGIN_HOST` |
| `FAILED 'personal' credentials file missing` | `SNAIL/config/creds.env` gone | restore it (Drive backup / laptop copy) |
| `FAILED 'company' credentials file missing` | `FIFTY/data/kite_credentials.json` gone | restore it |
| `FAILED ... credentials missing: totp_secret` | a key is empty in that file | fill it in |
| `FAILED password: ...` / `totp: ...` | Zerodha refused the login | check the password / TOTP secret in that file; log in once in the Kite app |
| `FAILED ... another user ... NOT written` | that file holds another account's credentials | fix the file; nothing was overwritten |
| `UNVERIFIED` | today's token exists but Kite could not be reached | wait for the 08:36 retry; if it still fails, check the network |
| holidays `NOT overwritten` | Zerodha's page layout changed | yesterday's file is kept; fix `holidays.parse` |
| token fixed by hand AFTER 09:00 | the BCS monitor loaded the bad token at 09:00 and holds it all session | also `pkill -f "bcs.spread_monitor --cron"`; the next */5 tick reloads it. zebra, st_watch, BTST, MOMENTUM and ETF_PI re-read the file on their own |

## Cutover (done 2026-11-27, owner: no SNAIL/FIFTY fallback on Monday)

1. `git pull` on the Pi; `python -m pytest kite_login/tests -q` there (proves the real flock).
2. `hostname > ~/.kite_login/LOGIN_HOST`, `chmod 700 ~/.kite_login`; `morning` on a day
   whose tokens exist returns NOOP for both (proves credentials + live check, no login).
3. `crontab -l > ~/Desktop/BOTS/data/crontab_backup_YYYYMMDD_HHMM.txt`; add the two lines
   above; remove the FIFTY `watchdog.sh` line, every SNAIL line (08:45 startup, 08:30
   holiday scraper, commented SNAIL / Bouncer / Sniper / Iron-condor blocks) and every
   CROCODILE job (morning startup … daily report, its two log cleanups).
   **Keep `CROCODILE/venv`**: zebra, BTST, kotak_neo, GOLD, MOMENTUM and kite_login run on its python.
4. `sudo systemctl disable --now fifty-daemon snail-telegram`.
5. Rollback: `crontab <backup>` and `sudo systemctl enable --now fifty-daemon snail-telegram`.
