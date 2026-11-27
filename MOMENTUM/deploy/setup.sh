#!/usr/bin/env bash
# One-time / after-pull setup on the Pi. Safe to re-run. Does NOT arm anything: ARM is written by the owner by hand.
#   cd ~/Desktop/BOTS/MOMENTUM && bash deploy/setup.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
PY=/usr/bin/python3
[ "$($PY -c 'import sys;print(sys.version_info[:2]>=(3,11))')" = "True" ] || { echo "need Python >= 3.11"; exit 1; }
[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p state logs control secrets
chmod 700 secrets
[ -f secrets/telegram.json ] && chmod 600 secrets/telegram.json || echo "WARN: secrets/telegram.json missing (copy it with WinSCP)"
[ "$(timedatectl show -p Timezone --value)" = "Asia/Kolkata" ] || echo "WARN: timezone is not Asia/Kolkata"
# command wrappers
cat > momentum <<EOW
#!/usr/bin/env bash
export MOMENTUM_HOME="$HERE"
# -I (isolated: no PYTHONPATH, no user site, no cwd on sys.path) is kept; the package path is put on sys.path here.
exec "$HERE/.venv/bin/python" -I -c "import sys; sys.path.insert(0, '$HERE/src'); from momentum.cli import main; sys.exit(main())" "\$@"
EOW
chmod +x momentum
cat > momentum-poller-keepalive <<EOW
#!/usr/bin/env bash
pgrep -f "sys.exit\(main\(\)\) poller" >/dev/null || nohup "$HERE/momentum" poller >> "$HERE/logs/poller.out" 2>&1 &
EOW
chmod +x momentum-poller-keepalive
# crontab block (replaces only the MOMENTUM block)
BLOCK="$(sed -e "s#\$M-poller-keepalive#$HERE/momentum-poller-keepalive#" -e "s#\$M #$HERE/momentum #" deploy/crontab.txt | grep -v '^#')"
( crontab -l 2>/dev/null | sed '/# BEGIN MOMENTUM/,/# END MOMENTUM/d'; echo "# BEGIN MOMENTUM"; echo "$BLOCK"; echo "# END MOMENTUM" ) | crontab -
echo "crontab:"; crontab -l | sed -n '/# BEGIN MOMENTUM/,/# END MOMENTUM/p'
# checks (read-only)
./momentum status || true
echo "manifest check:"; .venv/bin/python -I -c "import sys;sys.path.insert(0,'src');from momentum import cli,config as CF;from pathlib import Path;h=Path('$HERE');print(cli.check_manifest(h,CF.load(h)) or 'OK')"
echo "setup done. NOT armed. Next: ./momentum isolation-test ; then the dry runs (docs/RUNBOOK.md)."
