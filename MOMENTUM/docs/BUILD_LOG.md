# MOMENTUM build log (spec: SRS/MOMENTUM_BUILD_SPEC_DEC1.md; design: SRS/MOMENTUM_FROZEN_DESIGN.md)

## 2026-11-25
- Owner build go-ahead (D43): automatic 5L Dec 2026-Mar 2027, 50L from 1 Apr 2027. Deploy like FIFTY (push from PC, pull on Pi,
  bash setup script for cron); secrets git-ignored, copied by WinSCP. Repo private vs public: OPEN (asked).
- SRS portability: NSE_DATA env var in nonfo_srs.py:72, nse_ca.py:37, nse_cm_fetch.py:48, fetch_indices.py:23;
  joiner_profile SRS = __file__ dir. Defaults = PC paths. Backups in SRS/_pre_port_backup/. File-open trace of the decision
  path: no temp/scratch paths. November decision unchanged (sell TDPOWERSYS ASTRAMICRO, buy RAYMOND TFCILTD).
- Built: util, config(+defaults), journal (fsync, seq check, torn-line detect, flock), ledger (replay, paise), control
  (ARM/KILL/FIFTY kill sqlite bot_state/token strict), broker (journal-before-call, order-book tag guard, no write retry,
  REJECTED does not block the one corrected retry), fakebroker, pipeline (rehearsal route, members override, decide from cash).
- Tests: tests/test_core.py 15/15 OK. T2 known answer on rehearsal data (signal 2026-11-23): PASS (top 20 identical).
- FIFTY token generated_at format '2026-11-09T08:50:52.109270+05:30' (fromisoformat OK).
- F&O 'checked_on' stamp lives in control/fo_checked.json {month: date} (fo_announced.json keeps its tested list format).
- vet.py: Tier A via `claude -p --output-format json --tools "" --strict-mcp-config --mcp-config {} --setting-sources ""
  --no-session-persistence --permission-mode dontAsk --system-prompt .. --json-schema ..` in an empty temp cwd. On the PC
  (CLI 2.1.332): canary OK; T5 isolation OK (tools_available = [StructuredOutput] only; asked to list files -> NONE).
  MUST re-run T5 on the Pi (CLI 2.1.278; flags may differ).
- audit.py layers 1-2 written. Layer 1 on rehearsal data (2026-12, signal 11-23): pool 131, 3 flags (2 factor, 1 jump);
  touched: CUPID split 2026-03-09 raw ratio 0.2277 vs factor 0.2 (+13.9% on the ex-day; Kite check will tell).
  NOTE: the BLEND score window ends at the end of m-2, so November prices do not enter December's score.
- NEXT: plan.py (decide+audit+finalise, decision file, PLAN_FINAL), engine.py (14:45 plan+V2, 15:10 place, ladder, 15:28
  cancel, 15:35 reconcile), telegram.py (bot+poller+inbox), cli.py (cron commands), deploy/setup.sh, tests T3/T4.
- telegram.py (own bot, poller -> inbox, kill writes control/KILL), engine.py (afternoon stages PLAN/START/PLACE/LADDER/
  CANCEL/RECON, resumable; probe; ARM cap; qty from cap price; settle orphans right after placing). tests/test_engine.py:
  happy+rerun, vet down, telegram down, lost reply on probe (FOUND by tag, cancelled 15:28, no duplicate), probe failure,
  unfilled ladder/cancel/6-session window -> SKIPPED cash back, ladder cap, partial fill remainder, qty 0, kill by tag only
  (untracked momentum-looking tag never cancelled), one-shot taps. 26/26 OK.
- Bug found+fixed while testing: VET_DEFERRED lift reset the defer streak (2-in-a-row never reached OWNER_HOLD).
- evening.py (signal evening -> decision file write-once -> FINAL / owner approval; resume finalises, never rebuilds),
  cli.py (cron commands, preflight §E, manifest, isolation-test, E21 pending-signal re-run), deploy/setup.sh,
  deploy/crontab.txt, requirements.txt (numpy 2.1.0, kiteconnect 5.1.0 pinned), docs/RUNBOOK.md.
- E2E dry evening on real data (signal 2026-11-23, Kite faked from our panel): rebuild panel 136 s + band 93 s on the PC;
  no F&O stamp -> owner Approve asked once; re-run idempotent.
- BUG FOUND by the E2E: announced_additions took every fo_announced row with member_month <= m, so names that LEFT F&O
  (ZOMATO, DELTACORP, BSOFT, CYIENT, TATACOMM ...) were excluded: 273 members vs 218, 17 band names changed, 2 of the
  top 20 changed. Fixed: member_month == m only. Re-run pending.
- Re-run after the fix: members 218 == fo_membership[2026-11] (no diff); band b150_2026-12 bit-equal to the rehearsal;
  top 20 == T2. Audit: pool 131, 3 layer-1 flags, 1 touched (CUPID factor) -> AUDIT_HOLD + buttons. Stamp present ->
  FINAL: 20 positions x Rs 24,925, earmarked 4,98,500. Re-run -> "already final" (bug fixed: the order of checks).
- Pinned pipeline: the 22 research modules + SRS/data copied into MOMENTUM/pipeline/ (srs_dir = "pipeline"); T2 PASS
  on the pinned copy. Hand-kept inputs (fo_announced.json, CA overrides, exit events) now live in
  MOMENTUM/pipeline/data (Pi = master after deploy). manifests/code.sha256.json generated (momentum src + pipeline).
- Pi notes: cron PATH lacks ~/.npm-global/bin -> overlay "claude_bin": "/home/trustit/.npm-global/bin/claude".
  Pi CLI is 2.1.278 (PC 2.1.332): --tools / --json-schema may not exist there -> isolation-test decides; if it fails,
  update the CLI on the Pi (npm i -g @anthropic-ai/claude-code) and re-test.
- Independent check (advisor) 11-25 found gaps; fixed:
  1. kiteconnect lets requests' Timeout/ConnectionError through unwrapped (connect.py:914-916): a lost reply was
     journalled REJECTED_OTHER (terminal; order possibly live; owner Buy -> duplicate). Now only Kite's own error types
     are REJECTED; anything else = TIMEOUT -> settled by tag. _read retries on any non-auth error. Test added.
  2. Dry run now prices + records every leg (NOTE dry_order) with the default cap; no probe break.
  3. Telegram kill now cancels: the afternoon checks KILL after every wait (place, each ladder slot, cancel) and
     cancels open book orders by tag at once. Test: kill after the first placement -> all cancelled, no modify.
  4. Layer 2: a per-name Kite problem is a flag on that name; audit FAILS only if > half the names are unavailable.
     Decision bound to fo_announced sha: a changed file -> rebuilt as the next data_rev (band/members files per rev).
     Fetcher false-'holiday' lock-out: Kite sessions marked holiday/gap are unmarked before each fetch.
  5. SESSION_USED idempotent per (position, day) (crash between vet and STAGE no longer poisons the replay; test).
     Stale intents from an earlier day -> owner item: `filled <intent> <qty> <avg>` or `resolve <intent> absent`
     (test: power cut after placing -> next day blocks, owner fills, unblocks). Vet input now has signal close, gap %,
     CA rows ahead (+ quoted NSE purpose text); a CA ex-date on the order day defers that buy by code (OQ13; test).
  Tests 40/40.
- NOT BUILT (owner told): 16:00 three-way check (watch = heartbeat only), F&O entrant ALERT, T1/T3 replay tests
  (T1 = held-book decisions, January build; T3 buy-only replay not run), the 2-share TEST buy leg, Tier B.
- 16:00 three-way check (holdings vs opening + book fills, book qty, open book orders, cash) -> ALERT + blocks orders
  until the owner sends `ack`. Test. 41/41.
- E2E re-run on real data after all fixes: FINAL 20 x Rs 24,925, top 20 == T2, CUPID AUDIT_HOLD, re-run "already final",
  band files per rev (b150_2026-12_r0). Manifest regenerated (37 files).
- Owner 2026-11-25: repo = PUBLIC BOTS for now ("i will make it private once it is stabilised - just to make pull
  easy"); YES to all five: P0 ARM (automatic, 2026-11-30..2026-12-31, cap 5.5L/session); December gate = buy-path
  tests (full replay = January gate); BE/BZ buy -> OWNER_HOLD; first-order probe; 2-share TEST buy on Mon 30 Nov
  (to build: P0-T ARM, approve-each, tag M2611X01A1).
- Independent review 2026-11-25 (docs/REVIEW_2026-11-25.md): 36 fixes (tests/test_review.py, 46 tests); 87/87.
  Main ones: order-book REJECTED rows now classified (RMS x3 / band x1 / other -> OWNER_HOLD; the probe passes only
  on a first order settled OPEN / COMPLETE); cash guard read at 15:10; journal append safe with two processes + `repair-journal`;
  every non-dead order state is cancelled at 15:28 / on KILL; a crash-time order is found by tag before the ladder
  and the cancel; ledger.json check no longer blocks on a stale cache (journal_seq); approve_each ARM places nothing;
  three-way check tolerant of U6; startup failures alert; poller follows the mode; vet timeout kills the process group.
  New config keys: reconcile_at, ladder_grace_min, token_alert_at, band_max, probe_reads, probe_wait_s, backup_dir.
  Cron: afternoon also at 16:10 / 16:40. NOT yet run on the Pi (fcntl lock, killpg): run the unit suite there first.
- Fable independent review 11-25 (docs/REVIEW_2026-11-25.md): 46 findings, 36 fixed (1 Critical: approve_each ARM
  traded as automatic -> now places nothing; 7 High fixed), tests 41 -> 87. My line review of the order path:
  broker/place_all/probe/after_reject OK; found + fixed a double count in the reviewer's new stand-in fill (trades()
  lag -> stand-in at the order average, then the real trade booked again). Test added: 88/88. Manifest regenerated.
- Owner 2026-11-25: Dec 2026-Mar 2027 in the PERSONAL account YL6478 (token ../data/kite_access_token.json, ~08:45,
  naive generated_at read as IST - test), company QSK814 fresh at 50L from the 1 Apr 2027 rebalance (no scale-up /
  top-up then). Own kill only (FIFTY kill-switch check removed from preflight). Reason found: QSK814's API has NO
  market-data permission (quote() raises PermissionException, memory project_accounts) - the engine needs quote()
  at 14:45/15:10 and historical() in the evening. April needs market data on QSK or a two-token design. 89/89.
- Owner 2026-11-25 "go" (review answers all as recommended; D45): no 2-share TEST buy; band reject -> hold; manual
  repair-journal; probe 3 x 1 s; no circuit -> no order; backup by 18 Dec; vet inputs audit_flags + freshness before Jan.
  **April company switch: data reading ALWAYS from YL6478 (quote/historical/instruments/sessions/audit); QSK814 only for
  its own orders/order book/trades/holdings/margins (two tokens, two roles).**
- 2026-11-27 DRY-1 on the Pi stopped: "Kite sessions without an ok CM file: ['2026-11-08']" (Sunday muhurat). The
  research fetcher asks only weekdays + its frozen calendar + EXTRA_WEEKEND (to 2024). Fix 07fc823: configure() adds
  Kite's weekend sessions to the fetch list. DRY-1 re-run: FINAL 20 x Rs 9,970 (2L), CUPID + RATNAVEER AUDIT_HOLD.
- 2026-11-27/28 F&O entrant vet + day-1 backstop (frozen design D47; owner: "dont bring such works manual", "fully
  robust"; owner choice: vet failed -> FINAL + ALERT, no tap). Probes on the Pi: the isolated CLI with WebSearch /
  WebFetch only works (tools exactly those + StructuredOutput) but Anthropic's fetcher times out on NSE, so the Pi
  downloads the circulars itself (FAOP<n>.pdf over the global circular number range up to the RSS max; pdftotext)
  and the Tier A vet (no tools) reads the text. Kite NFO FUT names == F&O bhavcopy STF (218/218) + 6 index names.
  The code's own parser: 36/36 circulars cited by fo_announced.json (2020-2026, three table layouts) give the file's
  symbols and first-contract dates. Real vet call on the 17 Jul-Sep candidates: all classified right (incl. the
  BAJAJHLDNG, IEX/IREDA exclusions), 15 s. Found + fixed while building: withdrawal circulars quote the introduction
  phrase (code read them as introductions); old layouts (name before symbol, names in capitals); a 404 re-try on every
  30-min evening run would cost ~10 min (now once a day). New: fo_vet.py, `fo-scan` (cron 19:05), `fo-vet
  --as-of-signal`, engine.fo_day1_guard (14:45, fail closed), Paths.fo_vet / cache/fo_circulars, 12 config keys.
  tests/test_fo.py 31 tests; 122/122.
- 2026-11-28 review (advisor) fixes: undated circulars stay candidates; one unreadable PDF no longer stops the scan
  (recorded + reported); an off-menu vet answer keeps the code's own reading; a withdrawal cancels only earlier
  announcements; liveness = >= 8 F&O circulars in the look-back (a quarter without introductions is normal, none
  Jan 2022 - Nov 2024). Real-data positive test on the Pi (real vet CLI call, real F&O + CM files): entrants ==
  announced_additions - signal-day set for 2026-04 (8), 2025-08 (2), 2025-10 (1), 2026-01 (0). The membership check
  found the LEAVER gap (monthly expiry = signal day: 10 names in Jul 2025 still in the signal-day file): members now
  use futures expiring after the signal day (pipeline.fo_continuing) -> continuing + entrants == fo_membership.json[m]
  exactly in all four months. Seed scan done on the Pi: FAOP75371..77661, 55 circulars. 131/131.
