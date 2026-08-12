#!/usr/bin/env bash
#
# zebra BCS engine — Raspberry Pi deploy + readiness check
# =========================================================
#
#   scp Helper/zebra/pi_setup.sh pi:~/
#   ssh pi
#   chmod +x ~/pi_setup.sh
#   ./pi_setup.sh              # CHECK ONLY — reads everything, changes nothing
#   ./pi_setup.sh --apply      # git pull + install the cron line, then re-check
#
# Check mode is the default ON PURPOSE. This box also runs the LIVE-MONEY
# bcs.spread_monitor, so every mutation here is opt-in, announced before it
# happens, and backed up. The script never touches that monitor, never enables
# the Claude vetting layer, never places an order and never sends Telegram.
#
# Exit codes:  0 = ready   1 = blocking problem found   2 = bad usage

set -uo pipefail

# ── Paths (the Pi's layout; see memory/project_structure.md) ───────────────
BOTS="/home/trustit/Desktop/BOTS"
HELPER="$BOTS/Helper"
PY="$BOTS/CROCODILE/venv/bin/python"
TOKEN="$BOTS/data/kite_access_token.json"
CREDS="$BOTS/data/secret.json"
LOCKFILE="/tmp/zebra_monitor.lock"

# The desired cron line. Note the ESCAPED %% — cron reads a bare % as a
# newline in the command field, so an unescaped $(date +%Y%m%d) silently
# truncates the entry and the redirect vanishes.
CRON_LINE='*/5 9-15 * * 1-5 cd /home/trustit/Desktop/BOTS/Helper && flock -n /tmp/zebra_monitor.lock ../CROCODILE/venv/bin/python -m zebra run >> logs/cron_zebra_$(date +\%Y\%m\%d).log 2>&1'
CRON_TAG='zebra run'          # how we find our own line in an existing crontab

APPLY=0
case "${1:-}" in
  --apply) APPLY=1 ;;
  ""|--check) APPLY=0 ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--check|--apply]" >&2; exit 2 ;;
esac

PASS=0; WARN=0; FAIL=0
ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '         %s\n' "$*"; }
head_() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

printf '\033[1mzebra BCS deploy — %s MODE\033[0m\n' \
       "$( [ "$APPLY" -eq 1 ] && echo APPLY || echo CHECK )"
date '+%Y-%m-%d %H:%M:%S %Z'

# ── 1. Environment ────────────────────────────────────────────────────────
head_ "1. Environment"
for p in "$BOTS" "$HELPER" "$HELPER/zebra"; do
  [ -d "$p" ] && ok "dir $p" || bad "missing dir $p"
done
if [ -x "$PY" ]; then
  ok "python $($PY -V 2>&1)"
else
  bad "venv python not executable at $PY"
fi
[ -f "$CREDS" ] && ok "Drive service-account creds present" \
                || warn "no $CREDS — the store will run local-only"

# ── 2. Code version ───────────────────────────────────────────────────────
head_ "2. Code version"
cd "$HELPER" || { bad "cannot cd $HELPER"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  && ok "git repo at $(git rev-parse --show-toplevel)" \
  || bad "not a git work tree"

if [ "$APPLY" -eq 1 ]; then
  DIRTY="$(git status --porcelain -- zebra/ playbook/ bcs/ | head -5)"
  if [ -n "$DIRTY" ]; then
    warn "local modifications present; pulling with --ff-only so nothing is clobbered"
    printf '%s\n' "$DIRTY" | sed 's/^/           /'
  fi
  echo "  -> git pull --ff-only"
  if git pull --ff-only 2>&1 | sed 's/^/           /'; then
    ok "pulled"
  else
    bad "pull failed — resolve by hand, do NOT force"
  fi
else
  git fetch --quiet 2>/dev/null || warn "git fetch failed (offline?)"
  BEHIND="$(git rev-list --count HEAD..@{u} 2>/dev/null || echo '?')"
  if [ "$BEHIND" = "0" ]; then
    ok "up to date with origin"
  else
    warn "$BEHIND commit(s) behind origin — run with --apply to pull"
  fi
fi
note "HEAD $(git log --oneline -1 2>/dev/null)"

# ── 3. Imports ────────────────────────────────────────────────────────────
head_ "3. Imports"
if IMP="$($PY - <<'EOF' 2>&1
import sys
sys.path.insert(0, '/home/trustit/Desktop/BOTS/Helper')
mods = ['zebra.config', 'zebra.monitor', 'zebra.strikes', 'zebra.trade_store',
        'zebra.history', 'zebra.mfe', 'zebra.outcomes', 'zebra.scanner',
        'zebra.health', 'zebra.events', 'zebra.vet', 'bcs.drive_store']
bad = []
for m in mods:
    try:
        __import__(m)
    except Exception as e:
        bad.append(f'{m}: {type(e).__name__}: {e}')
print('OK' if not bad else 'FAIL')
print('\n'.join(bad))
EOF
)"; [ "${IMP%%$'\n'*}" = "OK" ]; then
  ok "all zebra + bcs modules import"
else
  bad "import failure:"; printf '%s\n' "$IMP" | tail -n +2 | sed 's/^/           /'
fi

# ── 4. Config — the switches that decide whether this trades ──────────────
head_ "4. Config (paper mode, vetting, the new guards)"
$PY - <<'EOF'
import sys
sys.path.insert(0, '/home/trustit/Desktop/BOTS/Helper')
from zebra import config as c

# (label, actual, expected, hard)  hard=True -> a mismatch blocks the deploy
checks = [
    ('PAPER_MODE',              c.PAPER_MODE,              True,  True),
    ('ENTRY_STRUCTURE',         c.ENTRY_STRUCTURE,        'bcs',  True),
    ('VET_ENABLED',             c.VET_ENABLED,             False, True),
    ('SPOT_SL_ENABLED',         c.SPOT_SL_ENABLED,         False, True),
    ('SPOT_VETO_ENABLED',       c.SPOT_VETO_ENABLED,       True,  True),
    ('TRAIL_ENABLED',           c.TRAIL_ENABLED,           True,  False),
    ('SWING_TP_ENABLED',        c.SWING_TP_ENABLED,        True,  False),
    ('ATTRACTION_ENABLED',      c.ATTRACTION_ENABLED,      True,  False),
]
worst = 0
for name, actual, exp, hard in checks:
    if actual == exp:
        print(f'  \033[32m[ OK ]\033[0m {name} = {actual!r}')
    else:
        tag = '\033[31m[FAIL]\033[0m' if hard else '\033[33m[WARN]\033[0m'
        print(f'  {tag} {name} = {actual!r}, expected {exp!r}')
        worst = max(worst, 2 if hard else 1)

# Values that must simply EXIST — they ship as code defaults, so an old
# zebra_config.json does not need editing, but a missing attribute means the
# pull did not land.
print()
for name in ('VALUE_TRIGGER_OPEN_BUFFER_SEC', 'SPREAD_COLLAPSE_PCT',
             'SPOT_MOVE_MIN_PCT', 'CORROBORATION_STALE_SEC',
             'WATCH_MAX_AGE_DAYS', 'BCS_MAX_ENTRY_COST_PCT',
             'BCS_MAX_DEBIT_TO_WIDTH_PCT', 'MIN_LEG_OI', 'TIME_SL_DAYS'):
    if hasattr(c, name):
        print(f'  \033[32m[ OK ]\033[0m {name} = {getattr(c, name)!r}')
    else:
        print(f'  \033[31m[FAIL]\033[0m {name} MISSING — old code, pull did not land')
        worst = 2
sys.exit(worst)
EOF
rc=$?
[ $rc -eq 0 ] && PASS=$((PASS+1)) || { [ $rc -eq 2 ] && FAIL=$((FAIL+1)) || WARN=$((WARN+1)); }

# ── 5. Today's fixes are actually in the file ─────────────────────────────
head_ "5. Guards present in the deployed source"
$PY - <<'EOF'
import sys, inspect
sys.path.insert(0, '/home/trustit/Desktop/BOTS/Helper')
from zebra import monitor, trade_store, strikes
want = [
    ('value bounds on a booked exit', trade_store.ZebraStore._bound_exit_value, None),
    ('spot veto (second source)',     monitor._spot_corroborates,              None),
    ('open buffer on value triggers', monitor._value_triggers_live,            None),
    ('expiry settlement net',         monitor._settle_if_expired,              None),
    ('blind-feed alert',              monitor._alert_monitoring_blind,         None),
    ('watchlist age bound',           monitor._expire_if_ancient,              None),
    ('swing TP breakeven guard',      monitor._swing_clears_breakeven,         None),
    ('shared expiry nag',             monitor._time_nag,                       None),
]
missing = [n for n, f, _ in want if f is None]
for n, f, _ in want:
    print(f'  \033[32m[ OK ]\033[0m {n}')
# wiring: the guards must be REACHED, not merely defined
src = inspect.getsource(monitor.check_entered)
wired = [('spot veto wired',   '_spot_corroborates(' in src),
         ('open buffer wired', '_value_triggers_live(' in src),
         ('expiry net wired',  '_settle_if_expired(' in src),
         ('expiry nag wired',  '_time_nag(' in src),
         ('poll line wired',   'POLL #%d' in src)]
esrc = inspect.getsource(monitor._enter_as_bcs)
wired.append(('breakeven guard wired', '_swing_clears_breakeven(' in esrc))
wsrc = inspect.getsource(monitor.check_watching)
wired.append(('watchlist age wired', '_expire_if_ancient(' in wsrc))
rc = 0
for label, present in wired:
    if present:
        print(f'  \033[32m[ OK ]\033[0m {label}')
    else:
        print(f'  \033[31m[FAIL]\033[0m {label} — defined but never called')
        rc = 1
sys.exit(rc)
EOF
[ $? -eq 0 ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

# ── 6. Kite token ─────────────────────────────────────────────────────────
head_ "6. Kite token"
if [ -f "$TOKEN" ]; then
  $PY - <<'EOF'
import json, sys
from datetime import datetime
p = '/home/trustit/Desktop/BOTS/data/kite_access_token.json'
d = json.load(open(p))
gen = str(d.get('generated_at', ''))[:10]
today = datetime.now().strftime('%Y-%m-%d')
if gen == today:
    print(f'  \033[32m[ OK ]\033[0m token generated today ({gen}), user {d.get("user_id")}')
    sys.exit(0)
print(f'  \033[33m[WARN]\033[0m token generated {gen}, today is {today} — it '
      f'expires 06:00 IST daily.')
print('         The 08:45 refresh task regenerates it; zebra will be BLIND '
      'until then and')
print('         will now say so on Telegram instead of failing quietly.')
sys.exit(1)
EOF
  [ $? -eq 0 ] && PASS=$((PASS+1)) || WARN=$((WARN+1))
else
  bad "no token at $TOKEN"
fi

# ── 7. Trade store ────────────────────────────────────────────────────────
head_ "7. Trade store"
$PY - <<'EOF'
import json, sys, collections, os
p = '/home/trustit/Desktop/BOTS/Helper/logs/zebra_trades.json'
if not os.path.exists(p):
    print('  \033[33m[WARN]\033[0m no local store yet — first run will pull it from Drive')
    sys.exit(1)
d = json.load(open(p))
t = d if isinstance(d, list) else d.get('trades', [])
c = collections.Counter(x.get('status') for x in t)
print(f'  \033[32m[ OK ]\033[0m {len(t)} records: ' +
      ', '.join(f'{k}={v}' for k, v in c.most_common()))
open_ = [x for x in t if x.get('status') == 'entered']
if open_:
    exp = collections.Counter(x.get('expiry') for x in open_)
    print(f'         {len(open_)} OPEN, expiries: ' +
          ', '.join(f'{k} x{v}' for k, v in sorted(exp.items())))
    nomfe = [x['id'] for x in open_ if 'mfe_mid' not in x]
    if nomfe:
        print(f'  \033[33m[WARN]\033[0m {len(nomfe)} open position(s) carry no mfe_* yet '
              f'— THE TRAIL CANNOT ARM until a live cycle writes them.')
        print(f'         Re-run this script after the first market cycle and this '
              f'should clear.')
    nobasis = [x['id'] for x in open_ if not x.get('pricing_basis')]
    if nobasis:
        print(f'         {len(nobasis)} open position(s) predate fill pricing and are '
              f'valued on the MID basis (correct — basis is fixed at entry).')
sys.exit(0)
EOF
[ $? -eq 0 ] && PASS=$((PASS+1)) || WARN=$((WARN+1))

# ── 8. Drive ──────────────────────────────────────────────────────────────
head_ "8. Drive connectivity"
$PY - <<'EOF'
import sys
sys.path.insert(0, '/home/trustit/Desktop/BOTS/Helper')
try:
    from zebra.trade_store import ZebraStore
    s = ZebraStore(); s.initialize()
    print(f'  \033[32m[ OK ]\033[0m store initialised, drive='
          f'{"enabled" if s._drive_enabled else "LOCAL-ONLY"}')
    sys.exit(0 if s._drive_enabled else 1)
except Exception as e:
    print(f'  \033[33m[WARN]\033[0m store init failed: {type(e).__name__}: {e}')
    print('         Not fatal — the store falls back to local-only and keeps trading.')
    sys.exit(1)
EOF
[ $? -eq 0 ] && PASS=$((PASS+1)) || WARN=$((WARN+1))

# ── 9. Logs ───────────────────────────────────────────────────────────────
head_ "9. Logs"
mkdir -p "$HELPER/logs" 2>/dev/null
[ -w "$HELPER/logs" ] && ok "logs/ writable" || bad "logs/ not writable"
TODAY_LOG="$HELPER/logs/cron_zebra_$(date +%Y%m%d).log"
note "today's log will be: $TODAY_LOG"
OLD="$HELPER/logs/cron_zebra.log"
if [ -f "$OLD" ]; then
  warn "the pre-rotation $OLD exists ($(du -h "$OLD" | cut -f1)) — harmless, now frozen"
fi
COUNT=$(ls -1 "$HELPER"/logs/cron_zebra_*.log 2>/dev/null | wc -l)
note "$COUNT dated zebra log(s) present"
note "to trim later:  find $HELPER/logs -name 'cron_zebra_*.log' -mtime +90 -delete"

# ── 10. CRON ──────────────────────────────────────────────────────────────
head_ "10. Cron"
CURRENT="$(crontab -l 2>/dev/null)"
if [ -z "$CURRENT" ]; then
  warn "no crontab for $(whoami)"
fi

echo "  --- zebra + live-money lines currently installed ---"
printf '%s\n' "$CURRENT" | grep -nE 'zebra|spread_monitor' | sed 's/^/         /' \
  || note "(none)"

# The live-money monitor must survive untouched.
if printf '%s\n' "$CURRENT" | grep -q 'bcs.spread_monitor'; then
  ok "live-money bcs.spread_monitor cron present (this script will NOT touch it)"
else
  warn "no bcs.spread_monitor cron found — expected on this box; check by hand"
fi

EXISTING_ZEBRA="$(printf '%s\n' "$CURRENT" | grep -F "$CRON_TAG" || true)"
if [ "$EXISTING_ZEBRA" = "$CRON_LINE" ]; then
  ok "zebra cron line is already exactly right"
elif [ -n "$EXISTING_ZEBRA" ]; then
  warn "zebra cron line differs from desired:"
  note "  have: $EXISTING_ZEBRA"
  note "  want: $CRON_LINE"
  if printf '%s' "$EXISTING_ZEBRA" | grep -q 'cron_zebra\.log'; then
    note "  (the change is the DATED log redirect — cron owns the fd, so Python"
    note "   cannot rotate it; rotation has to live in the redirect itself)"
  fi
else
  warn "no zebra cron line installed"
  note "  want: $CRON_LINE"
fi

if [ "$APPLY" -eq 1 ]; then
  if [ "$EXISTING_ZEBRA" = "$CRON_LINE" ]; then
    ok "cron already correct — nothing to install"
  else
    BACKUP="$HOME/crontab.backup.$(date +%Y%m%d-%H%M%S)"
    printf '%s\n' "$CURRENT" > "$BACKUP"
    ok "crontab backed up to $BACKUP"
    NEW="$(printf '%s\n' "$CURRENT" | grep -vF "$CRON_TAG")"
    NEW="$(printf '%s\n%s\n' "$NEW" "$CRON_LINE" | sed '/^$/d')"
    if printf '%s\n' "$NEW" | crontab -; then
      ok "zebra cron line installed"
      note "restore with:  crontab $BACKUP"
    else
      bad "crontab install FAILED — restore with: crontab $BACKUP"
    fi
  fi
fi

# ── 11. Is a cycle running right now? ─────────────────────────────────────
head_ "11. Runtime"
if pgrep -f "zebra run" >/dev/null 2>&1; then
  ok "a zebra cycle is running now"
else
  note "no zebra cycle running (normal — it is a 5-min cron, not a daemon)"
fi
if pgrep -f "bcs.spread_monitor" >/dev/null 2>&1; then
  ok "live-money spread_monitor is RUNNING (untouched)"
else
  note "live-money spread_monitor not running (expected outside 09:15-15:30)"
fi
[ -f "$LOCKFILE" ] && note "lock $LOCKFILE present (flock releases on exit; stale is harmless)"

# ── Verdict ───────────────────────────────────────────────────────────────
head_ "Verdict"
printf '  %d passed, %d warning(s), %d failure(s)\n' "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
  printf '\n  \033[31mNOT READY\033[0m — fix the [FAIL] items above and re-run.\n'
  exit 1
fi
if [ "$APPLY" -eq 0 ]; then
  printf '\n  \033[33mCHECK ONLY\033[0m — nothing was changed.\n'
  printf '  Re-run with --apply to pull and install the cron line.\n'
else
  printf '\n  \033[32mREADY\033[0m — zebra will run every 5 min, 09:00-15:55 Mon-Fri.\n'
fi
cat <<'TXT'

  What is now live, and what is not:
    PAPER mode      — auto-enters and auto-closes, real money is NOT at risk
    Claude vetting  — OFF. Enable only via zebra_config.json, never the env var
    spot stop       — OFF as a trigger, by measurement (it cuts 40% of winners)
    spot veto       — ON, and it can only ever REFUSE an exit, never cause one
    value triggers  — dark for the first 15 min after the open

  First thing to check tomorrow:
    tail -f logs/cron_zebra_$(date +%Y%m%d).log
    grep -c 'POLL #'   logs/cron_zebra_$(date +%Y%m%d).log   # one per position per cycle
    grep 'CYCLE '      logs/cron_zebra_$(date +%Y%m%d).log | tail -4
    grep -E 'QUOTE REJECT|VALUE BOUND|SPOT VETO|OPEN BUFFER' logs/cron_zebra_*.log
    ./pi_setup.sh      # re-run: the mfe_* warning should clear once a cycle has run
TXT
exit 0
