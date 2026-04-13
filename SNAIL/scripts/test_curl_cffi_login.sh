#!/bin/bash
# =============================================================================
# Test curl_cffi auto-login + Scan ALL Telegram tokens across BOTS
# Run on server: bash scripts/test_curl_cffi_login.sh
# =============================================================================

set -e

BOTS_DIR="/home/trustit/Desktop/BOTS"
VENV_PYTHON="${BOTS_DIR}/CROCODILE/venv/bin/python"
ETF_PI_DIR="/home/trustit/Desktop/Scripts/bots/ETF_PI"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo ""
echo "============================================================"
echo "  BOTS Health Check — $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

# -----------------------------------------------------------------
# PART 1: Test curl_cffi Kite auto-login
# -----------------------------------------------------------------
echo ""
echo -e "${CYAN}[1/2] Testing curl_cffi Kite auto-login...${NC}"
echo "------------------------------------------------------------"

cd "${BOTS_DIR}/SNAIL"

${VENV_PYTHON} -c "
import sys
sys.path.insert(0, 'src')
from dotenv import load_dotenv
load_dotenv('config/creds.env')
import os

from api.kite_auth import KiteAuthenticator

config = {
    'api_key': os.getenv('ZERODHA_API_KEY'),
    'api_secret': os.getenv('ZERODHA_API_SECRET'),
    'user_id': os.getenv('ZERODHA_USER_ID'),
    'password': os.getenv('ZERODHA_PASSWORD'),
    'totp_secret': os.getenv('ZERODHA_TOTP_SECRET'),
    'token_file': '../data/kite_access_token.json'
}

auth = KiteAuthenticator(config)
print(f'Session: {type(auth.session).__module__}')

try:
    token = auth.auto_login()
    print(f'LOGIN OK — token: {token[:20]}...')
except Exception as e:
    print(f'LOGIN FAILED — {e}')
    sys.exit(1)
" 2>&1 | grep -v "^$" | while read -r line; do
    if echo "$line" | grep -qi "LOGIN OK"; then
        echo -e "  ${GREEN}${line}${NC}"
    elif echo "$line" | grep -qi "FAILED\|ERROR"; then
        echo -e "  ${RED}${line}${NC}"
    else
        echo "  $line"
    fi
done

# -----------------------------------------------------------------
# PART 2: Scan & validate ALL Telegram bot tokens
# -----------------------------------------------------------------
echo ""
echo -e "${CYAN}[2/2] Scanning Telegram bot tokens...${NC}"
echo "------------------------------------------------------------"

${VENV_PYTHON} << 'PYEOF'
import json, yaml, os, sys
try:
    from curl_cffi import requests as http
    def get(url):
        return http.get(url, timeout=10, impersonate="chrome120")
except ImportError:
    import requests as http
    def get(url):
        return http.get(url, timeout=10)

BOTS_DIR = "/home/trustit/Desktop/BOTS"
ETF_PI_DIR = "/home/trustit/Desktop/Scripts/bots/ETF_PI"

# Collect all tokens: (name, token, source_file)
tokens = []

def add(name, token, source):
    if token and isinstance(token, str) and ":" in token and len(token) > 20:
        # Skip env var placeholders like ${TELEGRAM_BOT_TOKEN}
        if token.startswith("${") or token.startswith("$"):
            return
        tokens.append((name, token, source))

# --- SNAIL: creds.env ---
creds_env = os.path.join(BOTS_DIR, "SNAIL", "config", "creds.env")
if os.path.exists(creds_env):
    with open(creds_env) as f:
        for line in f:
            line = line.strip()
            if "TELEGRAM_BOT_TOKEN" in line and "=" in line and not line.startswith("#"):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                add("SNAIL", val, creds_env)

# --- CROCODILE: config.yaml ---
croc_cfg = os.path.join(BOTS_DIR, "CROCODILE", "config", "config.yaml")
if os.path.exists(croc_cfg):
    with open(croc_cfg) as f:
        cfg = yaml.safe_load(f)
    tg = cfg.get("telegram", {}) if cfg else {}
    add("CROCODILE", tg.get("bot_token", ""), croc_cfg)

# --- FIFTY: config.yaml ---
fifty_cfg = os.path.join(BOTS_DIR, "FIFTY", "config", "config.yaml")
if os.path.exists(fifty_cfg):
    with open(fifty_cfg) as f:
        cfg = yaml.safe_load(f)
    tg = cfg.get("telegram", {}) if cfg else {}
    add("FIFTY", tg.get("bot_token", ""), fifty_cfg)

# --- Scalper: credentials.yaml ---
scalper_cfg = os.path.join(BOTS_DIR, "Scalper", "config", "credentials.yaml")
if os.path.exists(scalper_cfg):
    with open(scalper_cfg) as f:
        cfg = yaml.safe_load(f)
    tg = cfg.get("telegram", {}) if cfg else {}
    add("Scalper", tg.get("bot_token", ""), scalper_cfg)

# --- Helper/Magnet: magnet_config.json ---
magnet_cfg = os.path.join(BOTS_DIR, "Helper", "config", "magnet_config.json")
if os.path.exists(magnet_cfg):
    with open(magnet_cfg) as f:
        cfg = json.load(f)
    for key in ["telegram_watching", "telegram_entry", "telegram"]:
        tg = cfg.get(key, {})
        if tg.get("bot_token"):
            add(f"Magnet ({key})", tg["bot_token"], magnet_cfg)
            break

# --- Helper/ST Watch: st_watch_config.json ---
stw_cfg = os.path.join(BOTS_DIR, "Helper", "config", "st_watch_config.json")
if os.path.exists(stw_cfg):
    with open(stw_cfg) as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {})
    add("ST Watch", tg.get("bot_token", ""), stw_cfg)

# --- Helper/BCS: bcs_config.json ---
bcs_cfg = os.path.join(BOTS_DIR, "Helper", "config", "bcs_config.json")
if os.path.exists(bcs_cfg):
    with open(bcs_cfg) as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {})
    if tg.get("bot_token"):
        add("BCS Monitor", tg["bot_token"], bcs_cfg)

# --- Helper/Scanner: scanner_config.json ---
scan_cfg = os.path.join(BOTS_DIR, "Helper", "config", "scanner_config.json")
if os.path.exists(scan_cfg):
    with open(scan_cfg) as f:
        cfg = json.load(f)
    tg = cfg.get("telegram", {})
    if tg.get("bot_token"):
        add("Scanner", tg["bot_token"], scan_cfg)

# --- ETF_PI: scan common config patterns ---
if os.path.isdir(ETF_PI_DIR):
    for root, dirs, files in os.walk(ETF_PI_DIR):
        # Skip venv, __pycache__, .git
        dirs[:] = [d for d in dirs if d not in ("venv", "__pycache__", ".git", "node_modules")]
        for fname in files:
            fpath = os.path.join(root, fname)
            if fname.endswith((".yaml", ".yml")):
                try:
                    with open(fpath) as f:
                        cfg = yaml.safe_load(f)
                    if isinstance(cfg, dict):
                        tg = cfg.get("telegram", {})
                        if isinstance(tg, dict) and tg.get("bot_token"):
                            add(f"ETF_PI/{fname}", tg["bot_token"], fpath)
                except:
                    pass
            elif fname.endswith(".json"):
                try:
                    with open(fpath) as f:
                        cfg = json.load(f)
                    if isinstance(cfg, dict):
                        tg = cfg.get("telegram", {})
                        if isinstance(tg, dict) and tg.get("bot_token"):
                            add(f"ETF_PI/{fname}", tg["bot_token"], fpath)
                except:
                    pass
            elif fname.endswith(".env"):
                try:
                    with open(fpath) as f:
                        for line in f:
                            line = line.strip()
                            if "TELEGRAM" in line and "TOKEN" in line and "=" in line and not line.startswith("#"):
                                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                                add(f"ETF_PI/{fname}", val, fpath)
                except:
                    pass

# --- Deduplicate by token value ---
seen = {}
unique = []
for name, token, source in tokens:
    if token not in seen:
        seen[token] = name
        unique.append((name, token, source))
    else:
        # Append name to existing entry
        for i, (n, t, s) in enumerate(unique):
            if t == token:
                unique[i] = (f"{n}, {name}", t, s)
                break

# --- Validate each token ---
print(f"\n  Found {len(unique)} unique Telegram bot tokens:\n")

all_ok = True
for name, token, source in unique:
    bot_id = token.split(":")[0]
    masked = f"{bot_id}:{'*' * 12}"
    try:
        r = get(f"https://api.telegram.org/bot{token}/getMe")
        data = r.json()
        if data.get("ok"):
            bot_name = data["result"].get("username", "?")
            print(f"  \033[0;32m[OK]\033[0m     {name:20s}  {masked}  @{bot_name}")
        else:
            desc = data.get("description", "Unknown error")
            print(f"  \033[0;31m[REVOKED]\033[0m {name:20s}  {masked}  {desc}")
            all_ok = False
    except Exception as e:
        print(f"  \033[1;33m[ERROR]\033[0m  {name:20s}  {masked}  {e}")
        all_ok = False

print()
if not all_ok:
    print("  ⚠ Some tokens are revoked/invalid — create new bots via @BotFather")
else:
    print("  All tokens valid.")

print()
PYEOF

echo "============================================================"
echo "  Done."
echo "============================================================"
