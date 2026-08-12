#!/usr/bin/env bash
#
# zebra Claude vetting layer — preflight, enable, verify
# ======================================================
#
#   ./vet_enable.sh              # CHECK ONLY — proves the layer WOULD work
#   ./vet_enable.sh --apply      # run the checks, then turn it on
#   ./vet_enable.sh --disable    # fast off switch (use this if it misbehaves)
#
# Run pi_setup.sh FIRST. This script assumes the engine is already deployed.
#
# Why the checks come before the switch: with vetting on, a signal waits for a
# verdict before it enters. If the CLI cannot spawn, the layer fails OPEN --
# signals enter UNVETTED rather than stalling, which is the right call for a
# trading system but means a broken layer looks EXACTLY like a working one
# from the outside. `claude -p` that lacks permission or a login does not hang
# and does not error: it exits 0 with the work undone. So the only way to know
# is to test the spawn before trusting it.
#
# Nothing here can open or close a position, and it never touches the
# live-money bcs.spread_monitor.
#
# Exit codes:  0 = ready / enabled   1 = blocking problem   2 = bad usage

set -uo pipefail

BOTS="/home/trustit/Desktop/BOTS"
HELPER="$BOTS/Helper"
PY="$BOTS/CROCODILE/venv/bin/python"
CFG="$HELPER/config/zebra_config.json"
SETTINGS="$HELPER/.claude/settings.json"
ACCOUNT="mail2raja18@gmail.com"

MODE=check
case "${1:-}" in
  --apply) MODE=apply ;;
  --disable) MODE=disable ;;
  ""|--check) MODE=check ;;
  -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
  *) echo "usage: $0 [--check|--apply|--disable]" >&2; exit 2 ;;
esac

PASS=0; WARN=0; FAIL=0
ok()   { printf '  \033[32m[ OK ]\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
warn() { printf '  \033[33m[WARN]\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
note() { printf '         %s\n' "$*"; }
head_(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

printf '\033[1mzebra vetting — %s MODE\033[0m\n' "$(echo "$MODE" | tr a-z A-Z)"
date '+%Y-%m-%d %H:%M:%S %Z'
cd "$HELPER" 2>/dev/null || { echo "cannot cd $HELPER"; exit 1; }

# ── DISABLE: do it first and get out ──────────────────────────────────────
if [ "$MODE" = disable ]; then
  head_ "Disabling"
  BK="$CFG.bak.$(date +%Y%m%d-%H%M%S)"; cp "$CFG" "$BK"; ok "backup $BK"
  "$PY" - "$CFG" <<'EOF'
import json, re, sys
p = sys.argv[1]; raw = open(p).read()
new = re.sub(r'("vet_enabled"\s*:\s*)true', r'\1false', raw)
json.loads(new)
open(p, 'w').write(new)
print('  \033[32m[ OK ]\033[0m vet_enabled set to false')
EOF
  "$PY" -c "from zebra import config as c; import sys; \
print('  VET_ENABLED now =', c.VET_ENABLED); sys.exit(0 if not c.VET_ENABLED else 1)"
  rc=$?
  [ $rc -eq 0 ] && printf '\n  \033[32mVETTING OFF\033[0m — next cycle runs unvetted, as before.\n' \
                || printf '\n  \033[31mSTILL ON\033[0m — check for a ZEBRA_VET_ENABLED env override.\n'
  exit $rc
fi

# ── 1. Engine must already be deployed ────────────────────────────────────
head_ "1. Engine"
[ -x "$PY" ] && ok "python $($PY -V 2>&1)" || bad "no venv python at $PY"
[ -f "$CFG" ] && ok "config $CFG" || bad "no config at $CFG"
if crontab -l 2>/dev/null | grep -q 'zebra run'; then
  ok "zebra cron installed (pi_setup.sh has run)"
else
  bad "no 'zebra run' cron — run pi_setup.sh --apply first"
fi
$PY -c "import sys; sys.path.insert(0,'$HELPER'); import zebra.vet, zebra.health" 2>/dev/null \
  && ok "zebra.vet + zebra.health import" || bad "vetting modules do not import"

# ── 2. The Claude CLI ─────────────────────────────────────────────────────
head_ "2. Claude CLI  (account: $ACCOUNT)"
CLI="$($PY - <<EOF 2>/dev/null
import sys; sys.path.insert(0, '$HELPER')
from zebra import vet
print(vet.resolve_cli() or '')
EOF
)"
if [ -n "$CLI" ]; then
  ok "resolve_cli() -> $CLI"
  note "(resolved to an ABSOLUTE path on purpose: cron's PATH is /usr/bin:/bin"
  note " and the CLI lives in ~/.local/bin — a bare 'claude' spawns fine by hand"
  note " and FileNotFoundErrors under cron, which is how a layer ships inert)"
else
  bad "resolve_cli() found no CLI — install it or set VET_CLI to an absolute path"
fi

if [ -n "$CLI" ]; then
  V="$("$CLI" --version 2>&1 | head -1)"; ok "version: $V"

  # THE test that matters: spawn it the way cron will — no HOME-derived PATH,
  # no interactive shell, minimal environment.
  echo "  -> spawning under a cron-like environment (this uses your account)..."
  OUT="$(env -i HOME="$HOME" PATH=/usr/bin:/bin "$CLI" -p 'reply with exactly: VETOK' 2>&1)"
  if printf '%s' "$OUT" | grep -q 'VETOK'; then
    ok "CLI answered under a minimal env — auth and PATH both good"
  else
    bad "CLI did NOT answer under a cron-like env. First 300 chars:"
    printf '%s\n' "$OUT" | head -c 300 | sed 's/^/           /'
    note ""
    note "If this mentions login/auth: run 'claude' interactively AS trustit,"
    note "then /login with $ACCOUNT. Do NOT enable vetting until this passes —"
    note "an unauthenticated spawn exits 0 with the work undone, so the layer"
    note "would look enabled and silently vet nothing."
  fi

  EXP="$($PY - <<EOF 2>/dev/null
import sys; sys.path.insert(0, '$HELPER')
from zebra import health
e = health.credential_expiry()
print(e.isoformat() if e else '')
EOF
)"
  [ -n "$EXP" ] && ok "refresh-token expiry: $EXP" \
                || note "no refresh-token expiry exposed (fine — the watchdog handles it)"
fi

# ── 3. Safety rails — these must hold BEFORE the switch ───────────────────
head_ "3. Safety rails"
if [ -f "$SETTINGS" ]; then
  N=$($PY -c "import json;print(len(json.load(open('$SETTINGS'))['permissions']['deny']))" 2>/dev/null || echo 0)
  [ "$N" -ge 5 ] && ok "$SETTINGS carries $N deny rules" \
                 || bad "deny rules missing/short in $SETTINGS"
else
  bad "no $SETTINGS — the interactive-agent backstop is absent"
fi
$PY - <<EOF
import sys; sys.path.insert(0, '$HELPER')
from zebra import config as c
need = ['close', 'enter', 'cancel', 'reset', 'trigger']
missing = [v for v in need if not any(v in d for d in c.VET_DENIED_TOOLS)]
if missing:
    print(f'  \033[31m[FAIL]\033[0m VET_DENIED_TOOLS does not cover: {missing}')
    sys.exit(1)
print(f'  \033[32m[ OK ]\033[0m VET_DENIED_TOOLS blocks all {len(need)} position verbs')
print(f'  \033[32m[ OK ]\033[0m model={c.VET_MODEL}  timeout={c.VET_TIMEOUT_SEC}s')
print(f'         allowed on argv: {c.VET_ALLOWED_TOOLS}')
print('         (a project settings.json ALLOW is IGNORED by claude -p —')
print('          grants MUST ride on argv; deny rules from the file DO apply)')
EOF
[ $? -eq 0 ] && PASS=$((PASS+1)) || FAIL=$((FAIL+1))

# Live proof the deny rule bites. `--help` is harmless even if it got through.
if [ -n "$CLI" ] && [ "$MODE" = apply ]; then
  echo "  -> live deny test (asks a real agent to run 'zebra close --help')..."
  DOUT="$("$CLI" -p "Run this exact command and report only whether it was permitted or denied: $PY -m zebra close --help" \
          --allowedTools "Bash($PY -m zebra:*)" \
          --disallowedTools "Bash(*zebra close*)" 2>&1 | tail -5)"
  if printf '%s' "$DOUT" | grep -qiE 'den(y|ied)|not permitted|blocked'; then
    ok "deny rule confirmed — the agent could not run 'zebra close'"
  else
    warn "deny test inconclusive; read it yourself:"
    printf '%s\n' "$DOUT" | sed 's/^/           /'
  fi
fi

# ── 4. The env-var trap ───────────────────────────────────────────────────
head_ "4. ZEBRA_VET_ENABLED override"
FOUND=0
[ -n "${ZEBRA_VET_ENABLED:-}" ] && { warn "set in THIS shell: '${ZEBRA_VET_ENABLED}'"; FOUND=1; }
for f in "$HOME/.bashrc" "$HOME/.profile" "$HOME/.bash_profile" /etc/environment; do
  [ -f "$f" ] && grep -q 'ZEBRA_VET_ENABLED' "$f" 2>/dev/null && \
    { warn "set in $f"; FOUND=1; }
done
crontab -l 2>/dev/null | grep -q 'ZEBRA_VET_ENABLED' && { warn "set in the crontab"; FOUND=1; }
if [ "$FOUND" -eq 0 ]; then
  ok "not set anywhere — the config FILE is the single source of truth"
else
  note "The env var OVERRIDES the file. Worse, cron does not inherit your"
  note "shell, so an export here would make hand-runs look vetted while every"
  note "cron cycle ran unvetted. Unset it and use the file."
fi

# ── 5. Current state, and the switch ──────────────────────────────────────
head_ "5. vet_enabled"
$PY -c "import sys;sys.path.insert(0,'$HELPER');from zebra import config as c;print('         config resolves VET_ENABLED =', c.VET_ENABLED)"
grep -q '"vet_enabled"' "$CFG" && note "key present in $CFG" \
                               || note "key ABSENT from $CFG (falling back to the code default: false)"

if [ "$MODE" = apply ]; then
  if [ "$FAIL" -gt 0 ]; then
    printf '\n  \033[31mREFUSING TO ENABLE\033[0m — %d check(s) failed above.\n' "$FAIL"
    printf '  A layer enabled on a broken CLI fails OPEN and vets nothing,\n'
    printf '  which is indistinguishable from working. Fix them first.\n'
    exit 1
  fi
  BK="$CFG.bak.$(date +%Y%m%d-%H%M%S)"; cp "$CFG" "$BK"; ok "backup $BK"
  "$PY" - "$CFG" <<'EOF'
import json, re, sys
p = sys.argv[1]
raw = open(p).read()
before = json.loads(raw)
if '"vet_enabled"' in raw:
    new = re.sub(r'("vet_enabled"\s*:\s*)(true|false)', r'\1true', raw)
else:
    # INSERT rather than rewrite. json.dump would reformat a file the owner
    # maintains by hand; this adds one line next to paper_mode and leaves
    # every other byte alone.
    new, n = re.subn(r'(\n(\s*)"paper_mode"\s*:\s*[^,\n]+,)',
                     r'\1\n\2"vet_enabled": true,', raw, count=1)
    if n != 1:
        sys.exit('could not find "paper_mode" to insert next to — edit by hand')
after = json.loads(new)          # must still be valid JSON
changed = {k for k in set(before) | set(after)
           if before.get(k) != after.get(k)}
if changed != {'vet_enabled'}:
    sys.exit(f'refusing to write: unexpected keys changed: {changed}')
open(p, 'w').write(new)
print('  \033[32m[ OK ]\033[0m vet_enabled: true written; no other key changed')
EOF
  if [ $? -ne 0 ]; then bad "config edit failed — restore: cp $BK $CFG"; else
    $PY -c "import sys;sys.path.insert(0,'$HELPER');from zebra import config as c;sys.exit(0 if c.VET_ENABLED else 1)" \
      && ok "read-back: VET_ENABLED is True" \
      || bad "read-back says still False — restore: cp $BK $CFG"
  fi
fi

# ── 6. Headroom (a Pi, running live money at the same time) ───────────────
head_ "6. Headroom"
note "RAM:  $(free -h 2>/dev/null | awk '/Mem:/{print $3" used / "$2" total, "$7" available"}')"
note "load: $(uptime | sed 's/.*load average: //')"
note "disk: $(df -h "$HELPER" | awk 'NR==2{print $4" free ("$5" used)"}')"
AVAIL=$(free -m 2>/dev/null | awk '/Mem:/{print $7}')
if [ -n "${AVAIL:-}" ] && [ "$AVAIL" -lt 400 ]; then
  warn "under 400 MB available — a spawned CLI alongside the live monitor may struggle"
else
  ok "memory headroom looks adequate"
fi

# ── Verdict ───────────────────────────────────────────────────────────────
head_ "Verdict"
printf '  %d passed, %d warning(s), %d failure(s)\n' "$PASS" "$WARN" "$FAIL"
[ "$FAIL" -gt 0 ] && { printf '\n  \033[31mNOT READY\033[0m — fix the [FAIL] items.\n'; exit 1; }
if [ "$MODE" = check ]; then
  printf '\n  \033[33mCHECK ONLY\033[0m — vetting was NOT enabled.\n'
  printf '  Re-run with --apply to turn it on.\n'
else
  printf '\n  \033[32mVETTING IS ON\033[0m — the next new signal will be vetted before it enters.\n'
fi
cat <<'TXT'

  What changes now:
    a new signal goes  watching -> triggered -> [waits for a verdict] -> entered
    ALLOW  -> enters (one tick of drift is the accepted cost)
    VETO   -> does not enter; a paper "shadow" tracks what it would have done,
              which is the only way the veto ever gets scored
    no verdict inside the timeout -> enters UNVETTED. Fail-open is deliberate:
              a vetting outage must never become a silent trading halt.

  Watch tomorrow:
    python -m zebra status                       # vet state per signal
    tail -f logs/vet_cli.log                     # the agent's own output
    grep -E 'VET|VETO|ALLOW|fail-open' logs/cron_zebra_$(date +%Y%m%d).log
    python -m zebra vet score                    # was the layer RIGHT? (needs a few days)

  If it misbehaves:
    ./vet_enable.sh --disable                    # off in one command
TXT
exit 0
