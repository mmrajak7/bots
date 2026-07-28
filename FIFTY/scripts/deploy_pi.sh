#!/usr/bin/env bash
#
# FIFTY - Raspberry Pi deployment script
# =====================================================================
# Deploys the breadth/regime + tick-size fixes (commit 4fafcae) and heals
# the breadth gap left by the regime pipeline never having run.
#
# Run manually on the Pi:
#     cd /home/trustit/Desktop/BOTS/FIFTY
#     bash scripts/deploy_pi.sh            # interactive, asks before writes
#     bash scripts/deploy_pi.sh --dry-run  # show everything, change nothing
#     bash scripts/deploy_pi.sh --yes      # no prompts
#
# Options:
#   --dry-run        inspect + report only; no pull, no DB write, no restart
#   --yes            assume yes for every confirmation
#   --skip-backfill  don't run the one-shot breadth backfill
#   --force          allow running during market hours (09:15-15:30 IST)
#
# SAFE TO RE-RUN. Every step is idempotent or explicitly guarded.
#
# WHY THE ORDER MATTERS:
#   the watchdog cron restarts the service within 5 min of it stopping, so it
#   is suspended FIRST and restored by an EXIT trap even if this script dies;
#   the backfill runs while the service is DOWN so nothing else is writing
#   history.json / regime_state.json at the same time.
# =====================================================================

set -euo pipefail

BOT_DIR="/home/trustit/Desktop/BOTS/FIFTY"
VENV_PY="/home/trustit/Desktop/BOTS/CROCODILE/venv/bin/python"
SERVICE="fifty-daemon"
EXPECTED_COMMIT="4fafcae"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BOT_DIR/data/backups/deploy_$TS"

DRY_RUN=0; ASSUME_YES=0; SKIP_BACKFILL=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)       DRY_RUN=1 ;;
    --yes|-y)        ASSUME_YES=1 ;;
    --skip-backfill) SKIP_BACKFILL=1 ;;
    --force)         FORCE=1 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

WATCHDOG_SUSPENDED=0
CRON_BACKUP="/tmp/fifty_crontab_$TS.bak"

step()  { echo; echo "=============================================================="; \
          echo "  $*"; echo "=============================================================="; }
info()  { echo "    $*"; }
ok()    { echo "  [OK]   $*"; }
warn()  { echo "  [WARN] $*"; }
die()   { echo "  [FAIL] $*" >&2; exit 1; }

confirm() {
  [ "$ASSUME_YES" = "1" ] && return 0
  [ "$DRY_RUN" = "1" ] && { info "(dry-run: would ask) $1"; return 1; }
  read -r -p "  >>> $1 [y/N] " reply
  [[ "$reply" =~ ^[Yy]$ ]]
}

# --- watchdog restore runs on ANY exit, including failure/Ctrl-C -------------
restore_watchdog() {
  if [ "$WATCHDOG_SUSPENDED" = "1" ]; then
    if crontab "$CRON_BACKUP" 2>/dev/null; then
      echo; ok "watchdog cron restored"
    else
      echo; warn "COULD NOT restore watchdog cron automatically!"
      warn "run this yourself:  crontab $CRON_BACKUP"
    fi
  fi
}
trap restore_watchdog EXIT

# =====================================================================
step "STEP 0  Preflight"
# =====================================================================
[ -d "$BOT_DIR" ]  || die "bot dir not found: $BOT_DIR (are you on the Pi?)"
[ -x "$VENV_PY" ]  || die "venv python not found: $VENV_PY"
cd "$BOT_DIR"
git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repo: $BOT_DIR"

info "dir      : $BOT_DIR"
info "python   : $($VENV_PY --version 2>&1)"
info "commit   : $(git log --oneline -1)"
info "service  : $(systemctl is-active $SERVICE 2>/dev/null || echo inactive)"
[ "$DRY_RUN" = "1" ] && warn "DRY RUN - nothing will be changed"

# Deploying mid-session stops signal processing and order monitoring.
# 10# forces base 10: "0915" would otherwise be read as octal and blow up.
DOW=$(date +%u); NOW=$((10#$(date +%H%M)))
if [ "$DOW" -le 5 ] && [ "$NOW" -ge 915 ] && [ "$NOW" -le 1530 ]; then
  warn "MARKET HOURS - the bot will be down for several minutes"
  if [ "$FORCE" != "1" ] && [ "$DRY_RUN" != "1" ]; then
    confirm "deploy anyway?" || die "aborted (re-run after 15:30, or pass --force)"
  fi
fi

# The backfill needs a live Kite token; the daemon mints one at 08:50 daily.
TOKEN_OK=0
if [ -f data/kite_access_token.json ]; then
  TOKEN_AGE_H=$(( ( $(date +%s) - $(stat -c %Y data/kite_access_token.json) ) / 3600 ))
  if [ "$TOKEN_AGE_H" -lt 20 ]; then
    TOKEN_OK=1; ok "kite token ${TOKEN_AGE_H}h old - usable for backfill"
  else
    warn "kite token is ${TOKEN_AGE_H}h old - backfill will be skipped"
  fi
else
  warn "no kite token file - backfill will be skipped"
fi

# =====================================================================
step "STEP 1  Backup"
# =====================================================================
if [ "$DRY_RUN" = "1" ]; then
  info "would back up trading.db, regime_state.json, breadth/ -> $BACKUP_DIR"
else
  mkdir -p "$BACKUP_DIR"
  for f in data/trading.db data/regime_state.json data/breadth/history.json \
           data/breadth/breadth_daily.json config/config.yaml; do
    [ -f "$f" ] && cp -p "$f" "$BACKUP_DIR/$(basename "$f")" && info "saved $(basename "$f")"
  done
  ok "backup -> $BACKUP_DIR"
fi

# =====================================================================
step "STEP 2  Suspend watchdog"
# =====================================================================
# watchdog.sh runs every 5 min and will restart the service the moment we stop
# it - which would run OLD code and, worse, hold the process lock while the
# backfill writes history.json. It also Telegram-alerts on every restart.
if crontab -l 2>/dev/null | grep -q "watchdog.sh"; then
  if [ "$DRY_RUN" = "1" ]; then
    info "would suspend the watchdog cron line for the duration of this run"
  else
    crontab -l > "$CRON_BACKUP"
    crontab -l | sed '/watchdog\.sh/s/^/#DEPLOY /' | crontab -
    WATCHDOG_SUSPENDED=1
    ok "watchdog suspended (auto-restored on exit; backup $CRON_BACKUP)"
  fi
else
  info "no watchdog cron entry found - nothing to suspend"
fi

# =====================================================================
step "STEP 3  Stop the service"
# =====================================================================
if [ "$DRY_RUN" = "1" ]; then
  info "would: sudo systemctl stop $SERVICE"
else
  sudo systemctl stop "$SERVICE" || warn "stop returned non-zero (already stopped?)"
  for _ in $(seq 1 30); do
    systemctl is-active --quiet "$SERVICE" || break
    sleep 1
  done
  systemctl is-active --quiet "$SERVICE" && die "service still active after 30s"
  ok "service stopped"

  # The daemon holds a process lock; a hard kill can leave it behind and the
  # next start would exit(2) with "Another instance is running".
  if pgrep -f "main.py --daemon" >/dev/null 2>&1; then
    warn "a main.py --daemon process is still alive - killing it"
    pkill -f "main.py --daemon" || true
    sleep 3
    pgrep -f "main.py --daemon" >/dev/null 2>&1 && die "could not kill daemon process"
  fi
  ok "no daemon process remains"
  if [ -f data/.fifty_lock ]; then
    rm -f data/.fifty_lock && ok "removed stale lock file"
  fi
fi

# =====================================================================
step "STEP 4  Pull the new code"
# =====================================================================
BEFORE="$(git rev-parse --short HEAD)"
info "before: $BEFORE"
if [ "$DRY_RUN" = "1" ]; then
  git fetch origin main >/dev/null 2>&1 || warn "fetch failed"
  info "would fast-forward to: $(git rev-parse --short origin/main)"
else
  # config.yaml.template is tracked and has previously conflicted on the Pi.
  if ! git diff --quiet -- config/config.yaml.template 2>/dev/null; then
    warn "local edits to config.yaml.template - discarding (it is a template)"
    git checkout -- config/config.yaml.template
  fi
  git pull origin main || die "git pull FAILED - resolve manually, then re-run"
  AFTER="$(git rev-parse --short HEAD)"
  ok "now at: $AFTER"
  [ "$BEFORE" = "$AFTER" ] && info "(already up to date)"
  git merge-base --is-ancestor "$EXPECTED_COMMIT" HEAD 2>/dev/null \
    && ok "expected commit $EXPECTED_COMMIT present" \
    || die "expected commit $EXPECTED_COMMIT NOT in history - wrong branch?"
fi

# =====================================================================
step "STEP 5  Verify the code before starting anything"
# =====================================================================
$VENV_PY -m py_compile main.py src/core/regime.py src/core/order_manager.py \
    src/core/exit_manager.py src/api/dual_kite_client.py src/utils/price_rounder.py \
  && ok "all modified modules compile" || die "compile FAILED - do not start the service"

# The whole point of this release: the regime task must be in the DAEMON's
# scheduler, not only in the dead cron path.
grep -q '_safe_run("regime"' main.py \
  && ok "regime task is wired into the daemon scheduler" \
  || die "regime task NOT wired into main.py - wrong code deployed"

for t in regime_smoke_test tick_size_smoke_test; do
  if [ -f "_research/filter_stretch/$t.py" ]; then
    if PYTHONIOENCODING=utf-8 $VENV_PY "_research/filter_stretch/$t.py" >/tmp/${t}_$TS.log 2>&1; then
      ok "$t: $(grep -oE '[0-9]+ passed, [0-9]+ failed' /tmp/${t}_$TS.log | tail -1)"
    else
      tail -20 /tmp/${t}_$TS.log
      die "$t FAILED - see /tmp/${t}_$TS.log"
    fi
  else
    warn "$t not found (skipped)"
  fi
done

# =====================================================================
step "STEP 6  Inspect + clean the trading DB"
# =====================================================================
# Finds rows the bot can no longer resolve on its own and verifies each against
# the BROKER before touching it. Nothing is retired that holds real exposure.
CLEAN_MODE="report"
if [ "$DRY_RUN" != "1" ]; then
  if confirm "retire stale rows if the broker confirms they hold no position?"; then
    CLEAN_MODE="apply"
  else
    info "report only - no DB writes"
  fi
fi

# `if !` wrapper: a DB-inspection failure must not abort the run and leave the
# service stopped. We still need to reach STEP 8.
if ! CLEAN_MODE="$CLEAN_MODE" $VENV_PY - <<'PYEOF'
import os, sqlite3, sys, json
from datetime import datetime, timedelta

MODE = os.environ.get("CLEAN_MODE", "report")
db = sqlite3.connect("data/trading.db")
db.row_factory = sqlite3.Row

print("  --- current state ---")
for tbl in ("signal_queue", "open_orders", "open_positions"):
    counts = ", ".join(f"{r[0]}={r[1]}" for r in
                       db.execute(f"select status,count(*) from {tbl} group by status"))
    print(f"    {tbl:<16} {counts}")

# Broker truth. If this fails we must NOT guess - abort the cleanup.
try:
    sys.path.insert(0, ".")
    from src.api.dual_kite_client import get_kite_client
    kite = get_kite_client()
    gtt_ids = {str(g.get("id")) for g in kite.get_gtt_orders()}
    holdings = {h["tradingsymbol"] for h in kite.get_holdings() if h.get("quantity")}
    print(f"    broker: {len(gtt_ids)} GTTs, {len(holdings)} holdings")
except Exception as e:
    print(f"  [WARN] broker check failed ({e}) - cleanup SKIPPED (never guess)")
    raise SystemExit(0)

actions = []

# (a) entry orders still PENDING whose GTT no longer exists at the broker
for r in db.execute("select id,script,gtt_id,placed_at,last_error from open_orders "
                    "where status='PENDING' and position_created=0"):
    gid = str(r["gtt_id"])
    if gid in gtt_ids:
        continue
    if r["script"] in holdings:
        print(f"    [SKIP] order #{r['id']} {r['script']}: GTT gone but SHARES HELD "
              f"- may have filled, reconcile manually")
        continue
    actions.append(("open_orders", r["id"], "EXPIRED",
                    f"order #{r['id']} {r['script']} GTT {gid} absent at broker, "
                    f"no holding (placed {r['placed_at']})"))

# (b) signals stuck APPROVED with no live order and no position
for r in db.execute("select id,script,signal_date,signal_month from signal_queue "
                    "where status='APPROVED'"):
    live = db.execute(
        "select count(*) from open_orders where signal_id=? and status in "
        "('PENDING','TRIGGERED','FILLED','PLACING')", (r["id"],)).fetchone()[0]
    held = db.execute(
        "select count(*) from open_positions where signal_id=? and status='OPEN'",
        (r["id"],)).fetchone()[0]
    if live or held or r["script"] in holdings:
        print(f"    [SKIP] signal #{r['id']} {r['script']}: has live order/position")
        continue
    actions.append(("signal_queue", r["id"], "EXPIRED",
                    f"signal #{r['id']} {r['script']} APPROVED but never entered "
                    f"(signal {r['signal_date']})"))

print("  --- stale rows ---")
if not actions:
    print("    none - nothing to clean")
else:
    for _, _, _, desc in actions:
        print(f"    - {desc}")

if actions and MODE == "apply":
    for table, rid, new_status, desc in actions:
        db.execute(f"update {table} set status=? where id=?", (new_status, rid))
    db.commit()
    print(f"  [OK]   retired {len(actions)} row(s) -> EXPIRED")
    for table, rid, _, _ in actions:
        got = db.execute(f"select status from {table} where id=?", (rid,)).fetchone()[0]
        print(f"    verified {table} #{rid} = {got}")
elif actions:
    print("    (report only - re-run and confirm, or pass --yes, to apply)")
db.close()
PYEOF
then
  warn "DB cleanup step failed - continuing so the service still restarts"
fi

# =====================================================================
step "STEP 7  Heal the breadth gap (one-shot backfill)"
# =====================================================================
# Runs with the service DOWN so nothing else writes history.json concurrently.
# If skipped, the daemon heals it itself over ~9 cycles once it is running -
# this step only makes it immediate.
if [ "$SKIP_BACKFILL" = "1" ]; then
  info "skipped (--skip-backfill)"
elif [ "$TOKEN_OK" != "1" ]; then
  warn "skipped - kite token stale/missing."
  warn "the daemon will heal the gap itself after its 08:50 token refresh."
elif [ "$DRY_RUN" = "1" ]; then
  $VENV_PY - <<'PYEOF'
import sys; sys.path.insert(0, ".")
from src.core.regime import RegimeManager
from src.api.dual_kite_client import get_kite_client
rm = RegimeManager(get_kite_client())
hist_last = rm._last_session_in_history()
n = rm._nifty_daily_signals()
missing = [s for s in (n or {}).get("sessions", []) if s > (hist_last or "")]
print(f"    history last session : {hist_last}")
print(f"    NIFTY last session   : {(n or {}).get('session')}")
print(f"    MISSING              : {missing}")
print(f"    would backfill       : {'yes' if len(missing) > 1 else 'no (<=1 missing -> daily capture handles it)'}")
PYEOF
else
  # non-fatal for the same reason as STEP 6: the daemon can heal the gap itself.
  if ! $VENV_PY - <<'PYEOF'
import sys, json, time; sys.path.insert(0, ".")
from pathlib import Path
from src.core.regime import RegimeManager
from src.api.dual_kite_client import get_kite_client
from src.utils.config_manager import config

rm = RegimeManager(get_kite_client())
before = rm._last_session_in_history()
print(f"    history last session BEFORE: {before}")

# One-shot: give it a large per-cycle budget so the sweep finishes in this run
# instead of over ~9 daemon cycles. The daemon keeps its own 60s budget.
config._config.setdefault("regime", {})["backfill_seconds_per_cycle"] = 1800

t0 = time.time()
for i in range(25):
    rm.maintenance()
    bf = (rm._load_state().get("backfill") or {})
    if not bf:
        print("    no backfill was needed")
        break
    print(f"    pass {i+1}: cursor={bf.get('cursor')} filled={bf.get('filled')} "
          f"done={bf.get('done')} complete={bf.get('complete')}")
    if bf.get("done"):
        break

after = rm._last_session_in_history()
print(f"    history last session AFTER : {after}  ({time.time()-t0:.0f}s)")

daily = Path("data/breadth/breadth_daily.json")
if daily.exists():
    readings = json.load(open(daily))
    tail = {d: readings[d] for d in sorted(readings)[-5:]}
    print(f"    breadth readings ({len(readings)} total), latest: {tail}")
else:
    print("    [WARN] breadth_daily.json still absent")

res = rm.evaluate()
print(f"    regime now: {res}")
PYEOF
  then
    warn "backfill did not finish cleanly - the daemon will retry it on its own"
  fi
fi

# =====================================================================
step "STEP 8  Start the service"
# =====================================================================
if [ "$DRY_RUN" = "1" ]; then
  info "would: sudo systemctl start $SERVICE"
else
  sudo systemctl start "$SERVICE"
  sleep 8
  systemctl is-active --quiet "$SERVICE" || {
    sudo journalctl -u "$SERVICE" -n 40 --no-pager
    die "service failed to start - see journal above"
  }
  ok "service active"
  info "$(systemctl status $SERVICE --no-pager | sed -n '3p')"

  # Heartbeat proves the main loop is turning, not just that the unit started.
  sleep 20
  if [ -f data/.heartbeat ]; then
    AGE=$(( $(date +%s) - $(stat -c %Y data/.heartbeat) ))
    [ "$AGE" -lt 120 ] && ok "heartbeat fresh (${AGE}s)" \
                       || warn "heartbeat is ${AGE}s old - watch it"
  else
    warn "no heartbeat file yet"
  fi

  echo; info "--- last 25 log lines ---"
  tail -n 25 "logs/fifty_$(date +%Y-%m-%d).log" 2>/dev/null || info "(no log yet)"
fi

# =====================================================================
step "DONE"
# =====================================================================
cat <<EOF

  Backup            : $BACKUP_DIR
  Commit            : $(git rev-parse --short HEAD)
  Service           : $(systemctl is-active $SERVICE 2>/dev/null || echo inactive)

  WHAT TO CHECK ON THE NEXT MARKET DAY
  ------------------------------------
  Breadth must now produce log lines. Watch for:

    grep -iE "breadth|regime" logs/fifty_\$(date +%F).log

  Expected, in order:
    * if a gap remains  -> "Breadth: backfilling sessions ... (resumable)"
                           then "backfill progress N/905 ... COMPLETE"
    * every morning     -> "Breadth: capturing <date> official closes for 905 symbols"
                           then "Breadth: captured ~900 closes for <date>"

  RED FLAGS
    * an INSTANT "COMPLETE" with a low filled count -> API was down, data is
      not real. The script now warns on this explicitly.
    * "Breadth coverage too low"  -> repository did not fill properly
    * NO breadth lines at all     -> the regime task is still not running

  Regime state:  cat data/regime_state.json
  Roll back   :  git checkout $BEFORE && sudo systemctl restart $SERVICE
                 (and restore the DB from $BACKUP_DIR if the cleanup ran)

EOF
