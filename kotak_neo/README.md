# kotak_neo — the one Kotak NEO package every bot uses

One NEO account, **one login place**, one published session. BTST, zebra, ETF_PI
and any future bot read that session and send orders through this package;
**no bot logs in on its own** (`tests/test_login_policy.py` fails the commit
otherwise). Owner decisions and the live measurements behind the design:
2026-11-27.

## What lives where (on the Pi only)

`~/.kotak_neo/` (mode 0700, outside every git clone, never committed, never
copied off the Pi):

| file | what |
|---|---|
| `session.json` | the published session (0600). Never logged. |
| `state.json` | generation counter, cooldown, logins today, latch |
| `refresh_log.jsonl` | every refresh request + outcome (no secrets) |
| `alerts.jsonl` | what needs a human |
| `owners.json` | which bot holds which contract |
| `LOGIN_HOST` | this machine's hostname. **Only the Pi has it.** No file = no login. |
| `scrip_nse_fo_YYYYMMDD.csv` | today's scrip master, shared by all bots |

Credentials: the untracked `BOTS/data/neo_credentials.json` (the laptop keeps a
copy as an offline backup only; it has no `LOGIN_HOST`, so it never logs in).

## Daily

Cron on the Pi (Mon–Fri):

```
40 8 * * 1-5  cd ~/Desktop/BOTS && CROCODILE/venv/bin/python -m kotak_neo login >> Helper/logs/cron_kotak_neo_$(date +\%Y\%m\%d).log 2>&1
55 8 * * 1-5  (same line)   # NOOP when 08:40 worked; a retry when it did not
```

Bots never log in. A bot whose READ finds the session dead calls
`session.refresh(failed_generation)`; if another bot already refreshed, it
adopts the newer session; if the session still verifies, nobody logs in.

## Operator commands (run on the Pi from `~/Desktop/BOTS`)

```
CROCODILE/venv/bin/python -m kotak_neo status            # generation, day, logins today, latch -- no secrets
CROCODILE/venv/bin/python -m kotak_neo login             # what the cron runs
CROCODILE/venv/bin/python -m kotak_neo refresh --reason "why"   # logs in ONLY if the session is dead
CROCODILE/venv/bin/python -m kotak_neo unblock           # clear a latch / cooldown / today's count
CROCODILE/venv/bin/python -m kotak_neo owners            # who holds which contract
CROCODILE/venv/bin/python -m kotak_neo owners --assign SYMBOL btst   # e.g. an overnight lot
CROCODILE/venv/bin/python -m kotak_neo measure-second-login          # after hours only
```

## When something is wrong

| you see | it means | do |
|---|---|---|
| `status` shows `latched: mpin ...` | NEO named the MPIN, or 3 refusals in a row | check the credentials file / NEO app, then `unblock` |
| `latched: credentials ...` | the credentials file is missing/bad | fix the file, `unblock` |
| alert `daily_cap` | 8 logins today | a login in the NEO **app** may be killing the bots' session; one login per 30 min continues automatically |
| `ERROR ... upgrade this clone` | a session written by a newer schema | `git pull` the clone that printed it; never delete the session |
| a bot says `needs_human` on an exit | the exit chase could not settle an order from the book | **exit in the NEO app**, then tell the bot |
| alert `no_session_today` | a morning cron (08:40 / 08:55) ended without today's session (NEO down, cooldown, login failed, latched...) | `status`; if 08:55 also alerts, `refresh --reason` once NEO answers, or trade manually. Refusal/failure streaks restart each day; a latch does not |
| `login_failed` / `refresh_log` says `clock` | the Pi's clock was not NTP-synchronised (after the 08:00 reboot), so no TOTP was sent; never counts toward the cap or the 3-refusal latch | `timedatectl` (wait for `System clock synchronized: yes`), then `refresh --reason clock`. Off switch: `require_clock_sync: false` in `~/.kotak_neo/config.json` |
| `HOST_REFUSED` | a login was asked for on a machine without `LOGIN_HOST` | expected on the laptop; on the Pi, recreate the file |
| alert `static_ip` | NEO refused an order, a read or the session verify for the IP (stCode 100008 / 1037 / "session ip"), or the morning check saw the public IPv4 change. Since 11-28 (K5/R1) two kinds: **whitelist** (100008 / "static ip" / "ip ... whitelist") is NEVER a dead session -- no login (it cannot whitelist an IP; refused logins latch the day), reads come back unavailable; **session ip** (1037 / "session ip doesnt match": the session was made from another IP, i.e. the Pi's public IP changed) IS a dead session -- the next read logs in again from the new IP through the normal brakes, and orders then work only if the new IP is whitelisted | **manage open trades MANUALLY in the NEO app**; update the whitelist on the NEO dashboard (one change per 7 days). Backup plan until a static IP exists. The expected IP lives only in the Pi's `~/.kotak_neo/config.json` (`expected_public_ipv4`). |
| alert `orders_auth` | 2+ orders/cancels (any bot) refused as "session dead" on one session generation within 30 min, while the package makes NO login on order evidence (K1, 11-28; at most one alert per 15 min) | **check open trades in the NEO app now**; `status`. If reads work but orders keep failing, the session is half-dead: trade manually and tell the bots' sessions. Counter: `~/.kotak_neo/orders_auth.json` |

## Orders (what every bot gets)

- Every order carries the bot's tag (`zbr-`, `btst-`, `etf-`) and claims its
  contract: **one bot per contract**.
- **SL-M is refused** (RMS rejects it for options); stops are SL (trigger + limit)
  or monitored by the bot.
- A refused order (401/403/timeout) is **unclear**: never re-sent, never a login;
  its outcome is read from the order book by tag.
- `orders.chase_exit` sells to flat one step per call: never two sells working,
  never more than the account holds, the tag saved before the send.

## Tests

```
python -m pytest kotak_neo/tests -q      # laptop and Pi (the Pi run proves the real flock)
```
