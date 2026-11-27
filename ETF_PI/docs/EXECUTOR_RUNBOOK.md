# ETF_PI executor — deploy, Monday 11-30 live test, go-live 12-01

Spec: `docs/EXECUTOR_SPEC.md`. Every Pi command below is a LIVE change: the owner says go first.
Pi paths: ETF_PI = `~/Desktop/Scripts/bots/ETF_PI` (cron, unchanged), shared NEO = `~/Desktop/BOTS/kotak_neo`.

## 1. Deploy (weekend, market closed)
```bash
# a) the shared package (kotak_neo: non-exclusive client, chase held_fn/price_fn, N2-N5)
cd ~/Desktop/BOTS && git pull -q --rebase && CROCODILE/venv/bin/python -m pytest kotak_neo/tests -q
# b) the ETF clone: realign the April sparse clone to git WITHOUT touching files
cd ~/Desktop/Scripts/bots && git fetch -q origin && git status --short | head   # expect the ~40 untracked files
git reset --mixed origin/main         # HEAD+index move; working tree untouched
git status --short                    # expect ONLY gitignored state; any M line = STOP and report
sha256sum -c ETF_PI/MANIFEST.sha256   # before the pull this fails ONLY for option_buy.py? -> see c)
git pull -q --rebase                  # brings executor/ + the D17 option_buy change
cd ETF_PI && sha256sum -c MANIFEST.sha256 && /usr/bin/python3 -m pytest executor/tests -q
# c) hide the stale copy in the main clone (owner: "remove outdated ETF_PI")
cd ~/Desktop/BOTS && git sparse-checkout set --no-cone '/*' '!/ETF_PI/' && ls ETF_PI 2>&1 | head -1
```
Order matters: (a) before (b) — the executor imports kotak_neo from `~/Desktop/BOTS`.

## 2. Install the daemon + watchdog
```bash
cd ~/Desktop/Scripts/bots/ETF_PI
cat > executor_local.yaml <<'EOF'
mode: paper            # Monday 08:50 -> live (step 4)
lots: 1
EOF
/usr/bin/python3 -m executor check
sudo cp executor/etf-executor.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now etf-executor && sleep 5 && python3 -m executor status
( crontab -l; echo '* 9-15 * * 1-5 cd /home/trustit/Desktop/Scripts/bots/ETF_PI && /usr/bin/python3 -m executor watchdog >> logs/executor_watchdog.log 2>&1' ) | crontab -
```
Dead-man: create a healthchecks.io check (period 2 min, grace 2 min, Telegram integration), then add
`"healthchecks": {"url": "https://hc-ping.com/<uuid>"}` to `credentials.json` (0600). The Pi reboots daily
08:00 (`rs.py`), so the service restarts fresh each morning and re-reads option_buy's constants (F20).

## 3. Controls (any time)
| want | command |
|---|---|
| no new entries | `python3 -m executor kill` |
| exit everything now | `python3 -m executor flatten` |
| undo both | `python3 -m executor resume` |
| what is it doing | `python3 -m executor status` · `tail -f logs/executor_$(date +%F).log` |
| stop the daemon | `sudo systemctl stop etf-executor` (positions stay; restart resumes from the journal) |

## 4. Monday 11-30 — 1-lot live test (owner at the app)
Before 09:10: `python3 -m kotak_neo status` (generation for today, no latch); `curl -4 ifconfig.me` = the whitelisted IP;
set `executor_local.yaml` to `mode: live`, `lots: 1`, `live_until: 2026-11-30` (live ends tonight unless extended);
`sudo systemctl restart etf-executor`; `executor status` shows `reconciled: true`, `ws_ok: true`.

Probe trades (cheap near-ATM current weekly, premium Rs 80-200): `python3 -m executor probe --symbol <KITE_SYM> --confirm-real-order`.

| # | test | how | pass mark |
|---|---|---|---|
| M0 | charges (11-27 contract note) | owner reads the note | brokerage 0 for intraday option round trips |
| M2 | lag after a fill | probe 1; journal ENTRY_FILL ts vs order time; positions MIS row | order book ≤ 1 s, positions ≤ 3 s |
| M3 | place → visible by tag | journal ENTRY_SEND vs ENTRY_FILL / book | ≤ 1 s on every place |
| M5 | no-modify step | probe 2, then `flatten` while the bid is moving | at most one ETF sell live at any read; no-order gap ≤ 1 s; exactly 65 sold |
| M6 | deep limit accepted | `flatten` probe 3 with `exit_steps_pct` reached (watch re-prices) | no RMS / price-band reject at −2% |
| M7 | coexistence | BTST stays rehearsal; check BTST logs at each ETF fill | no BTST kill / EXTRA LOT alert |
| M8 | restart mid-position | probe 4, then `sudo systemctl kill -s KILL etf-executor` | back ≤ 10 s, `reconciled`, position still managed, no 2nd buy/sell |
| M9 | EOD | a real signal or probe after 14:50 | flat by 15:16 (eod 15:15); no `etf-` order live at 15:20; no broker square-off line in the app |
| M11 | 401 on place | count `UNCLEAR` in the log | 0, or each settled within the grace |
| M12 | MIS on NEXT week's contract | probe on next week's ATM (12-01 is expiry: every 12-01 entry uses next week) | accepted and filled; if refused: owner decides NRML (+skip_foreign_strikes) for 12-01 |
| real | real 10M signals | they flow by themselves | each: BOUGHT/SOLD Telegram, ladder logs, journal CLOSED, P&L = fills |

## 5. Go / no-go for 12-01 (owner)
GO only if: M0 brokerage 0 · M2/M3/M5/M8/M9/M12 pass · zero orphan lots at 15:30 (`executor status`, app) ·
no unexplained `etf-` order. Then `live_until: 2026-12-31` (re-arm monthly) and the lot ceiling stays 1 lot.

## 6. Rollback
`python3 -m executor flatten` → wait flat → `sudo systemctl disable --now etf-executor` → remove the watchdog
cron line. option_buy keeps trading paper (its only live change is the intent file it writes).

## 7. Known limits (stage 1, stated)
- An `etf-` sell that is filled yet invisible in the order book for longer than `unclear_grace_sell_s` (10 s)
  could be re-sent (measured visibility: immediate). The account cap catches it on a single-bot strike.
- A carried MIS lot should not exist (broker square-off) — if one does, it is sold from 09:15:05 next session.
- `Tests/test_confirm_shadow.py` fails 2 checks on the baseline too (record-only shadow; pre-existing).
