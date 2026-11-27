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

## Live orders — the executor daemon (2026-11-27)
`option_buy.py` stays the signal generator + PAPER ledger. For each NIFTY 10M paper entry it
writes an intent file (`state/executor/intents/`); the `executor/` daemon (systemd
`etf-executor`) alone places live orders through the shared `BOTS/kotak_neo` session, keeps
its own journal-backed live book and runs the exit ladder on Kite ticks.
Spec `docs/EXECUTOR_SPEC.md` · ops `docs/EXECUTOR_RUNBOOK.md` · tests `executor/tests/`.
The old in-process `neo/` bridge is RETIRED (D17): importing `neo` raises, `trade_enabled` is
ignored, `neo_login.py` exits 2. ETF works by its `etf-` order tag only and trades MIS, so BTST
(NRML) on the same strike is never touched.

## Runs (Pi crontab, system `/usr/bin/python3`)
`option_buy.py` every minute (flock), `get_instruments.py` 08:50, `get_op_instruments.py`
08:52, `preflight.py --send` 08:56, `digest.py --send` 15:40, plus `tracker.py`,
`extract_data.py`, `rs.py`.
