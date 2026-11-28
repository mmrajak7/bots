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
git status --short ETF_PI             # EXPECT exactly: M common.py extract_data.py tracker.py
                                      # backtest_time_to_tp.py (the Pi copies still hold inline
                                      # secrets) + D/M of files changed since the baseline.
                                      # Anything else = STOP and report.
python3 -c "import json;print(sorted(json.load(open('ETF_PI/credentials.json'))))"
                                      # must include the 5 sections: telegram_main, telegram_error,
                                      # extract_data_telegram, tracker_telegram, backtest_kite
git checkout -- ETF_PI                # take the repo versions (secrets via credentials.json)
rm -f ETF_PI/kite_trade.py            # deleted in git (kite_login), untracked after the reset
cd ETF_PI && sha256sum -c MANIFEST.sha256 && /usr/bin/python3 -m executor selftest
# owner: remove the now-unused tracker_kite / extract_data_kite sections from credentials.json
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
bots_root: /home/trustit/Desktop/BOTS
EOF
/usr/bin/python3 -m executor check
sudo cp executor/etf-executor.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now etf-executor && sleep 5 && python3 -m executor status
( crontab -l; echo '* 9-15 * * 1-5 cd /home/trustit/Desktop/Scripts/bots/ETF_PI && /usr/bin/python3 -m executor watchdog >> logs/executor_watchdog.log 2>&1' ) | crontab -
```
No external dead-man (owner 11-27: overkill; a combined 09:00 all-bots status message comes later).
The Pi reboots daily 08:00 (`rs.py`), so the service restarts fresh each morning and re-reads
option_buy's constants (F20).

## 3. Controls (any time)
**Always run executor commands from the LIVE folder**: `cd ~/Desktop/Scripts/bots/ETF_PI && python3 -m executor …`.
The state folder follows the code, so a command run from another copy (e.g. `~/Desktop/BOTS/ETF_PI`, until §1c
hides it) "succeeds" and does nothing (review A7). Suggested alias: `alias etf='cd ~/Desktop/Scripts/bots/ETF_PI && python3 -m executor'`.

| want | command |
|---|---|
| no new entries | `etf kill` — open trades keep their exits (it does NOT stop a sell already chasing) |
| exit everything now | `etf flatten` — exits REAL trades too and blocks entries until `resume`; run `etf status` first |
| undo both | `etf resume` |
| what is it doing | `etf status` · `tail -f state/executor/logs/executor_$(date +%F).log` (journal: `state/executor/journal_<mode>.jsonl`, tape: `tape_YYYYMMDD.jsonl`) |
| stop the daemon | `etf stop` (STOPPED flag). It stays stopped through the watchdog AND the 08:00 reboot (review B2): the daemon idles, sends one "IDLE" Telegram, places nothing. Open lots are NOT managed while stopped — `flatten` first |
| start it again | `etf start` |
| NEVER | change `mode` or stop the daemon while a lot is open (live and paper keep separate journals; a live lot in paper mode is alerted every 5 min but NOT managed) |
| NEVER, 09:00–15:30 | `git pull` in EITHER clone (`~/Desktop/BOTS` holds the kotak_neo the daemon imports; other sessions pull it for other bots — review A5) |

**Taking over by hand (review T5)** — in THIS order, never "cancel in the app first":
1. `etf stop` (otherwise the bot re-places a sell you cancel in the app within a second);
2. in the NEO app cancel any open `etf-` order on the strike;
3. exit the lot in the app with product **MIS**;
4. leave the daemon stopped for the day; `etf start` only after checking the app is flat.

If the positions row and the bot disagree the bot now alerts `… NOT selling, NOT closing … Check the NEO app` every
minute and keeps the lot managed (review B1); it closes a lot as "outside the bot" only when the order book shows a
sell it did not place.

## 4. Monday 11-30 — 1-lot live test (owner at the app)
The daemon has never run through a market session (installed 11-27 night). So: **paper first, then live while flat.**

**08:45–09:10** (paper, unchanged `executor_local.yaml`):
`cd ~/Desktop/BOTS && CROCODILE/venv/bin/python -m kotak_neo status` (generation for today, no latch);
`curl -4 ifconfig.me` = the whitelisted IP; `etf status` shows a fresh heartbeat, `mode: paper`.

**09:15:30** `etf status`: `reconciled: true` (reconcile starts at 09:15, not before), `ws_ok: true`, and
`state/executor/tape_<today>.jsonl` growing. If `ws_ok` is still false at **09:17**: check the Kite token (08:30
kite_login) and `sudo systemctl restart etf-executor` (the feed rebuilds itself once a minute, review B3, but after
the 08:00 boot on yesterday's token KiteTicker may have spent its retries — fix review F2; prices then come from the
2 s REST poll only). Record what you saw: 12-01 has no cutover restart.

**09:15–~09:30 paper smoke**: `etf probe --symbol <KITE_SYM>` (paper: no `--confirm-real-order` needed), watch
BOUGHT, then `etf flatten` → SOLD, journal CLOSED; `etf resume`.

**~09:30–09:40, flat** (`etf status` → `active: []`): set `executor_local.yaml` to `mode: live`, `lots: 1`,
`live_until: 2026-11-30` (live ends tonight unless extended); `sudo systemctl restart etf-executor`; re-check
`etf status` (`mode: live`, `reconciled: true`, `ws_ok: true`). Finish the live probes before the 10:30 blackout.

Probe trades (cheap near-ATM current weekly, premium Rs 80-200):
`etf probe --symbol <KITE_SYM> --confirm-real-order [--rest-steps N]`.
`--rest-steps N` (0-5, review T7) makes the flatten's first N sells rest 3% ABOVE the bid, so cancel → confirm →
re-place really runs N times; without it the first sell fills at once and M5/M6/M13 test nothing.

**After EVERY `flatten`: `etf resume`, then `etf status` shows `flatten: false`** — while the FLATTEN file exists every
new intent (the next probe, M8, M9, M12 and real signals) is SKIPPED (fix review F12).
**If M6b fails** (the MIS row is missing, mislabelled, or not 65 within ~3 s): STOP the live probes, `etf flatten`
→ `etf resume`, and treat 12-01 as NO-GO — while the row reads low the bot sells nothing, a stop-loss included (B1:
never a naked short; fix review F3). Do not hand-trade a strike the bot is probing (fix review F1).

| # | test | how | pass mark |
|---|---|---|---|
| M0 | charges (11-27 contract note) | **CLOSED 11-28**: brokerage Rs0.01, statutory Rs12.68; Saturday funds 49,987.32 = 50,000 − 12.68 (the Friday Rs39.52 was a released intraday hold) | brokerage 0 ✔ |
| M2 | lag after a fill | probe 1; journal ENTRY_FILL ts vs order time; positions MIS row | order book ≤ 1 s, positions ≤ 3 s |
| M3 | place → visible by tag | journal ENTRY_SEND vs ENTRY_FILL / book | ≤ 1 s on every place |
| M6b | MIS position row | log / app after probe 1's fill, and again after its exit | product reads exactly `MIS`; net quantity **65** within ~3 s of the fill and **0** after the exit (B1/T5: every hand-exit and restart case depends on this row) |
| M5 | no-modify step | probe 2 with `--rest-steps 3`, then `flatten` | at most one ETF sell live at any read; 3 cancels then a fill; exactly 65 sold |
| M13 | cancel latency / re-price steps | journal EXIT_STEP notes during M5 | re-prices count 1, 2, 3; each cancel confirmed ≤ 2 s; no repeated cancel POSTs (B6) |
| M6 | deep limit accepted | probe 3 with `--rest-steps 0` while the bid drifts, or a real exit reaching −1%/−2% | no RMS / price-band reject (if one: the bot backs off 5 s and alerts the reason, X-K1) |
| M7 | coexistence | BTST stays rehearsal; check BTST logs at each ETF fill | no BTST kill / EXTRA LOT alert |
| M8 | restart mid-position | probe 4, then `sudo systemctl kill -s KILL etf-executor` | back ≤ 10 s, `reconciled`, position still managed, no 2nd buy/sell |
| M9 | EOD | a probe between **14:45 and 14:50** (entries are refused 14:15–14:45 and from 14:51) on **next week's** contract | flat by 15:16 (eod 15:15); no `etf-` order live after 15:19:30 (`stop_placing`, A12); no broker square-off line in the app |
| M11 | 401 on place | count `UNCLEAR` in the log; kotak_neo alert `orders_auth` fires on 2 on one session generation within 30 min (K1) | 0, or each settled within the grace; no `orders_auth` |
| M12 | MIS on NEXT week's contract | probe on next week's ATM (12-01 is expiry: every 12-01 entry uses next week) | accepted and filled; if refused: **NO-GO for 12-01** (ETF is MIS-only in code since A7'; NRML would be a code change + review) |
| lat | trigger → fill | journal EXIT_FILL `trigger_to_fill_s` on every exit | record it; > 10 s on a stop-type exit = finding for the T3 change (first stop sell at bid −1%) |
| real | real 10M signals | they flow by themselves | each: BOUGHT/SOLD Telegram, ladder logs, journal CLOSED, P&L = fills |

## 5. Go / no-go for 12-01 (owner)
GO only if: M0 brokerage 0 · M2/M3/M5/M6b/M8/M9/M12/M13 pass · zero orphan lots at 15:30 (`etf status`, app) ·
no unexplained `etf-` order · no NEO 429s that stalled an exit. Then `live_until: 2026-12-31` (re-arm monthly)
and the lot ceiling stays 1 lot. Judge the first weeks on slippage and latency, not P&L (review T2: the paper record
books TPs past the target; the live edge's sign is unknown).

## 6. Rollback
`python3 -m executor flatten` → wait flat → `sudo systemctl disable --now etf-executor` → remove the watchdog
cron line. option_buy keeps trading paper (its only live change is the intent file it writes).

## 7. Known limits (stage 1, stated)
- An `etf-` sell that is filled yet invisible in the order book for longer than `unclear_grace_sell_s` (10 s)
  could be re-sent (measured visibility: immediate). The account cap catches it on a single-bot strike.
- A carried MIS lot should not exist (broker square-off) — if one does, it is sold from 09:15:05 next session.
- `Tests/test_confirm_shadow.py` fails 2 checks on the baseline too (record-only shadow; pre-existing).
- A dead session found by a read (review K8, 11-28): ETF waits at most `neo_refresh_wait_s` (2 s) for ANOTHER bot's
  refresh, then the step is unreadable and the loop goes on. If ETF itself holds the refresh lock it does the
  verify + login inline (normally ~4-6 s: a 3 s confirm gap + two login calls; worst case with NEO hanging ~60 s,
  when no order could work anyway).
- An IP refusal (K5/R1): the `static_ip` alert. 100008 / "static ip" (this IP not whitelisted): NO login, reads
  unavailable, orders refused until the whitelist matches -- manage the open lot in the NEO app. 1037 / "session ip"
  (the session was made from another IP: the public IP changed): the next read logs in again from the new IP;
  orders then work only if that IP is whitelisted (kotak_neo README "you see" table).
- Package alerts raised inside the exit loop (static_ip, orders_auth) are sent on a background thread (R4), so a
  hung Telegram never stalls an exit; a confirmed DEAD verify is cached 15 s (R3), so a cooldown costs no 3 s per read.
