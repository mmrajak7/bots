# ETF_PI — option buying bot (runs on the Pi from `~/Desktop/Scripts/bots/ETF_PI`)

Status 2026-11-27. The **Pi's working copy is the FINAL code** (owner); this repo copy is
being synced to it (Monday 30-Nov). Go-live order: **ETF_PI first** (small capital, from
1 Dec), then NIFTY BTST, then zebra naked.

## Secrets — never in code
All tokens and logins are read through `etf_creds.py` from the UNTRACKED
`credentials.json` beside it (gitignored by `**/credentials.json`, mode 0600 on the Pi).
Sections: `telegram_main`, `telegram_error`, `extract_data_kite`, `extract_data_telegram`,
`tracker_kite`, `tracker_telegram`, `backtest_kite`. Also never committed: `secret.json`,
`neo_credentials.yaml`, `neo/data/session.json`, `logs/`.

## NEO (Kotak) — moving to the shared `kotak_neo` login
ETF_PI's own NEO login (`neo/session.py`, the 08:45 `neo_login.py` cron) is being retired:
the cron line is commented out on the Pi (2026-11-27) and ETF_PI will use the ONE shared
session published by `BOTS/kotak_neo` (see `kotak_neo/README.md`), importing it from
`~/Desktop/BOTS`. Until that lands, `trade_enabled: false` keeps the order path off.

## Runs (Pi crontab, system `/usr/bin/python3`)
`option_buy.py` every minute (flock), `get_instruments.py` 08:50, `get_op_instruments.py`
08:52, `preflight.py --send` 08:56, `digest.py --send` 15:40, plus `tracker.py`,
`extract_data.py`, `rs.py`.
