# Sniper - Clean Structure Explanation

**Why Sniper exists and what's inside.**

---

## 🎯 Purpose

**Problem:** Helper folder was congested with:
- Multiple scanner versions (old/new)
- Documentation files (*.md)
- Analysis tools
- Butterfly scripts
- Position checkers
- CSV files

**Solution:** Sniper folder with ONLY essential files for Linux production deployment.

---

## 📁 What's Inside Sniper

```
Sniper/
├── scanner.py              18 KB   Main scanner (production-ready)
├── setup_cron.sh           5 KB    Automated cron setup script
├── README.md               5 KB    Usage guide
├── DEPLOY.md               3 KB    Quick deployment steps
├── STRUCTURE.md            -       This file
│
├── data/cache/                     Runtime cache (created automatically)
│   ├── .gitkeep                    Keep dir in git
│   ├── instruments.pkl      500KB  (created at runtime)
│   ├── zones_db.pkl         50KB   (created at runtime)
│   └── alerts_tracker.pkl   5KB    (created at runtime)
│
└── logs/                           Daily logs (auto-rotated)
    ├── .gitkeep                    Keep dir in git
    ├── scanner_YYYYMMDD.log  2MB   (created at runtime)
    └── cron.log                    (created by cron)
```

**Total:** ~31 KB (excluding runtime files)

---

## 🚫 What's NOT in Sniper

**Excluded from deployment:**

```
❌ All .md files from Helper (documentation)
❌ opportunity_scanner.py (old version)
❌ opportunity_scanner_live.py (old version)
❌ sr_scanner.py (old version)
❌ butterfly_analyzer.py (different tool)
❌ position_checker.py (different tool)
❌ kite_nse_options.py (different tool)
❌ run_scanner.bat (Windows only)
❌ CSV files (data files)
❌ Analysis logs
❌ Config files (stored in Bouncer/)
❌ Token files (stored in BOTS/data/)
```

**Why excluded?**
- Documentation: Not needed for runtime
- Old versions: Replaced by scanner.py
- Other tools: Separate functionality
- Windows files: Linux deployment only
- Data files: Generated at runtime

---

## 🔗 External Dependencies

Sniper relies on config files elsewhere in BOTS:

```
BOTS/
├── Sniper/                    ← Scanner files
├── Bouncer/
│   └── config/
│       └── config.json        ← Telegram token (referenced by scanner)
└── data/
    └── kite_access_token.json ← Kite token (referenced by scanner)
```

**Why not inside Sniper?**
- Config files shared across multiple bots (SNAIL, CROCODILE, Bouncer)
- Single source of truth for tokens
- Easier to manage one config location

---

## 🔄 Path Resolution

Scanner automatically finds config files:

```python
# In scanner.py (lines 33-40)
SCRIPT_DIR = Path(__file__).parent.resolve()     # /home/user/BOTS/Sniper
SNIPER_DIR = SCRIPT_DIR                          # /home/user/BOTS/Sniper
BOTS_DIR = SNIPER_DIR.parent                     # /home/user/BOTS
DATA_DIR = BOTS_DIR / 'data'                     # /home/user/BOTS/data
CACHE_DIR = SNIPER_DIR / 'data' / 'cache'        # /home/user/BOTS/Sniper/data/cache
LOGS_DIR = SNIPER_DIR / 'logs'                   # /home/user/BOTS/Sniper/logs

BOUNCER_CONFIG = BOTS_DIR / 'Bouncer' / 'config' / 'config.json'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'
```

**Works on any system** as long as directory structure is:
```
BOTS/
├── Sniper/
├── Bouncer/config/config.json
└── data/kite_access_token.json
```

---

## 📦 Files Comparison

### Before (Helper folder)

```
Helper/
├── helper/
│   ├── scanner.py                    ← The one we need
│   ├── opportunity_scanner.py        ❌ Old
│   ├── opportunity_scanner_live.py   ❌ Old
│   ├── sr_scanner.py                 ❌ Old
│   ├── butterfly_analyzer.py         ❌ Different tool
│   ├── position_checker.py           ❌ Different tool
│   ├── kite_nse_options.py           ❌ Different tool
│   ├── run_scanner.bat               ❌ Windows only
│   └── data/cache/
│
├── logs/
├── CLAUDE.md                         ❌ Documentation
├── README.md                         ❌ Documentation
├── SCANNER_README.md                 ❌ Documentation
├── SCANNER_SETUP.md                  ❌ Documentation
├── SCANNER_CRON_SETUP.md             ❌ Documentation
├── SCANNER_STATUS.md                 ❌ Documentation
├── SCANNER_FLOW_EXPLAINED.md         ❌ Documentation
├── SCANNER_COMPLETE_GUIDE.md         ❌ Documentation
├── LINUX_DEPLOYMENT.md               ❌ Documentation
├── TELEGRAM_ALERTS.md                ❌ Documentation
├── TRADING_SYMBOLS_EXPLAINED.md      ❌ Documentation
├── TRADE_RULES.md                    ❌ Documentation
└── (analysis files, CSVs, logs...)   ❌ Data files
```

**Total:** ~150+ files, ~20+ MB

### After (Sniper folder)

```
Sniper/
├── scanner.py              ✅ Main script
├── setup_cron.sh           ✅ Setup helper
├── README.md               ✅ Minimal docs
├── DEPLOY.md               ✅ Quick start
├── STRUCTURE.md            ✅ This file
├── data/cache/.gitkeep     ✅ Directory placeholder
└── logs/.gitkeep           ✅ Directory placeholder
```

**Total:** 7 files, ~31 KB

**Reduction:** 95%+ smaller, only essentials

---

## 🚀 Deployment Comparison

### Before (Helper)

```bash
# Complex: Need to know which files to copy
scp Helper/helper/scanner.py server:/path/
# But also need to exclude old versions...
# And avoid copying .md files...
# And make sure paths work...
```

### After (Sniper)

```bash
# Simple: Copy entire directory
scp -r Sniper user@server:/home/user/BOTS/

# Run setup
ssh user@server
cd /home/user/BOTS/Sniper
./setup_cron.sh

# Done!
```

---

## 🎯 Benefits

### 1. **Clean Separation**
- Helper: Development, analysis, multiple tools
- Sniper: Production scanner only

### 2. **Easy Deployment**
- Copy one directory
- Run one setup script
- Done

### 3. **No Confusion**
- Only scanner.py (no old versions)
- Clear purpose
- Minimal files

### 4. **Git-Friendly**
- Cache and logs auto-created
- .gitkeep preserves directory structure
- No large files in repo

### 5. **Maintainable**
- Easy to update (replace scanner.py)
- Easy to debug (one script)
- Easy to backup (small size)

---

## 📊 File Purpose

| File | Purpose | Size | Needed? |
|------|---------|------|---------|
| scanner.py | Main scanner script | 18 KB | ✅ Essential |
| setup_cron.sh | Automated setup | 5 KB | ✅ Helpful |
| README.md | Usage guide | 5 KB | ✅ Reference |
| DEPLOY.md | Quick start | 3 KB | ✅ Helpful |
| STRUCTURE.md | This file | 3 KB | ℹ️ Info |
| data/cache/ | Runtime cache | - | ✅ Auto-created |
| logs/ | Daily logs | - | ✅ Auto-created |

---

## 🔄 Update Process

**To update scanner:**

```bash
# On Windows (after changes to Helper/helper/scanner.py)
cp Helper/helper/scanner.py Sniper/scanner.py

# Deploy to Linux
scp Sniper/scanner.py user@server:/home/user/BOTS/Sniper/

# No cron restart needed (picks up on next run)
```

---

## 🌳 Full BOTS Structure

```
BOTS/
├── Sniper/                     ← NEW: Clean scanner deployment
│   ├── scanner.py
│   ├── setup_cron.sh
│   ├── README.md
│   ├── DEPLOY.md
│   ├── STRUCTURE.md
│   ├── data/cache/
│   └── logs/
│
├── Helper/                     ← ORIGINAL: Development, multiple tools
│   ├── helper/
│   │   ├── scanner.py          (source of truth, copy to Sniper)
│   │   ├── butterfly_analyzer.py
│   │   ├── position_checker.py
│   │   └── (other tools...)
│   └── (documentation...)
│
├── Bouncer/                    ← Telegram bot
│   └── config/config.json      (shared by all bots)
│
├── data/
│   └── kite_access_token.json  (shared by all bots)
│
├── SNAIL/                      ← Strategy bot
├── CROCODILE/                  ← Another bot
└── (other bots...)
```

**Relationship:**
- Helper = Development + multiple tools
- Sniper = Production deployment of scanner only
- Both reference same config files in Bouncer/ and data/

---

## ✅ Summary

**Sniper = Clean, minimal, production-ready scanner deployment**

- ✅ Only essential files (95% size reduction)
- ✅ No documentation clutter
- ✅ No old versions
- ✅ Easy to deploy (3 steps)
- ✅ Automated setup (setup_cron.sh)
- ✅ Clear purpose (scanner only)
- ✅ Git-friendly (small, clean)

**Use Sniper for Linux deployment. Use Helper for development.**

---

**Clean, minimal, ready to deploy.** 🎯
