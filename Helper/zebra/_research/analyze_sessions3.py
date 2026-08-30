import json, collections, re, sys
sys.stdout.reconfigure(encoding='utf-8')

sessions = []
with open(r"C:\Users\mail2\.claude\history.jsonl", "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            sessions.append(json.loads(line))
        except:
            pass

# Longer uncategorized prompts
print("=== UNCATEGORIZED PROMPTS (longer ones, 50 samples) ===\n")
count = 0
known = r"bug|fix|error|broken|crash|fail|debug|review|audit|test|deploy|cron|server|monitor|position|check.*p|trade|entry|execute|refactor|config|setup|explain|what does|read.*file|csv|document|readme|sap|abap|cds|bapi|push.*git|commit|merge|display|gui|ui|format|scrape|api.*integrat|create|build|add|implement|write a|make a|strategy|playbook|analys|research|compare"
for s in sessions:
    d = s.get("display", "").strip()
    proj = s.get("project", "").replace("C:\\Users\\mail2\\Documents\\", "")
    if len(d) > 30 and not d.startswith("/"):
        if not re.search(known, d, re.IGNORECASE):
            safe = d.encode("ascii", "replace").decode("ascii")[:180]
            print(f"  [{proj[:25]:25s}] {safe}")
            count += 1
            if count >= 50:
                break

# SNAIL deep dive
print("\n\n=== SNAIL PROJECT DEEP DIVE ===\n")
snail_themes = collections.Counter()
for s in sessions:
    if "SNAIL" in s.get("project", ""):
        d = s.get("display", "").strip().lower()
        if len(d) > 15:
            if "telegram" in d: snail_themes["Telegram integration"] += 1
            elif "websocket" in d or "tick" in d: snail_themes["WebSocket/tick"] += 1
            elif "order" in d or "place" in d or "fill" in d: snail_themes["Order management"] += 1
            elif "signal" in d or "scan" in d or "filter" in d: snail_themes["Signal/scanning"] += 1
            elif "margin" in d or "capital" in d: snail_themes["Margin/capital"] += 1
            elif "pnl" in d or "profit" in d or "loss" in d: snail_themes["P&L tracking"] += 1
            elif "expiry" in d or "rollover" in d: snail_themes["Expiry/rollover"] += 1
            elif "log" in d: snail_themes["Logging"] += 1
            elif "database" in d or "sqlite" in d: snail_themes["Database"] += 1
            elif "iron" in d or "condor" in d or "butterfly" in d: snail_themes["Strategy logic"] += 1
            elif "startup" in d or "init" in d or "session" in d: snail_themes["Session/startup"] += 1
            elif "sl" in d or "stop" in d or "trail" in d: snail_themes["SL/trailing"] += 1
            elif "gui" in d or "window" in d or "table" in d: snail_themes["GUI/display"] += 1
            else: snail_themes["General development"] += 1

for theme, count in snail_themes.most_common(20):
    print(f"  {count:4d}  {theme}")

# Scalper deep dive
print("\n=== SCALPER PROJECT DEEP DIVE ===\n")
scalper_themes = collections.Counter()
for s in sessions:
    if "Scalper" in s.get("project", ""):
        d = s.get("display", "").strip().lower()
        if len(d) > 15:
            if "websocket" in d or "tick" in d or "feed" in d: scalper_themes["WebSocket/feed"] += 1
            elif "order" in d or "fill" in d or "oco" in d: scalper_themes["Order/OCO"] += 1
            elif "gui" in d or "window" in d or "ui" in d: scalper_themes["GUI"] += 1
            elif "sound" in d or "alert" in d: scalper_themes["Alerts"] += 1
            elif "trail" in d or "sl" in d or "stop" in d: scalper_themes["SL/trailing"] += 1
            elif "pnl" in d or "charge" in d or "brokerage" in d: scalper_themes["P&L/charges"] += 1
            elif "supertrend" in d or "indicator" in d: scalper_themes["Indicators"] += 1
            elif "position" in d or "trade" in d: scalper_themes["Position tracking"] += 1
            else: scalper_themes["General"] += 1

for theme, count in scalper_themes.most_common():
    print(f"  {count:4d}  {theme}")

# SAP deep dive
print("\n=== SAP PROJECTS DEEP DIVE ===\n")
sap_themes = collections.Counter()
for s in sessions:
    proj = s.get("project", "")
    if "SAP" in proj or "Derlin" in proj:
        d = s.get("display", "").strip().lower()
        if len(d) > 10:
            if "report" in d or "alv" in d: sap_themes["Report/ALV"] += 1
            elif "form" in d or "smartform" in d or "pdf" in d: sap_themes["Forms/PDF"] += 1
            elif "cds" in d or "view" in d: sap_themes["CDS Views"] += 1
            elif "bw" in d or "query" in d or "cube" in d: sap_themes["BW/Analytics"] += 1
            elif "fiori" in d or "ui5" in d or "odata" in d: sap_themes["Fiori/UI5"] += 1
            elif "transfer" in d or "material" in d: sap_themes["Stock/Transfer"] += 1
            elif "scrap" in d or "reject" in d: sap_themes["Scrap/Reject"] += 1
            elif "boe" in d or "crystal" in d or "webi" in d: sap_themes["BOE/Crystal"] += 1
            elif "timesheet" in d or "cats" in d: sap_themes["Timesheets"] += 1
            elif "table" in d or "field" in d or "structure" in d: sap_themes["Data model"] += 1
            elif "dms" in d or "document" in d: sap_themes["DMS"] += 1
            else: sap_themes["General SAP dev"] += 1

for theme, count in sap_themes.most_common():
    print(f"  {count:4d}  {theme}")

# Agent/expert prompts
print("\n=== AGENT/EXPERT PATTERNS USED ===\n")
agent_samples = []
for s in sessions:
    d = s.get("display", "").strip()
    if re.search(r"(expert|agent|role|persona|act as|you are a)", d, re.IGNORECASE):
        if len(agent_samples) < 15:
            safe = d.encode("ascii", "replace").decode("ascii")[:200]
            proj = s.get("project", "").replace("C:\\Users\\mail2\\Documents\\", "")
            agent_samples.append(f"[{proj[:20]}] {safe}")

print(f"Total: {len(agent_samples)}+ sessions")
for sample in agent_samples:
    print(f"  {sample}")

# Helper specific: what does user do here?
print("\n=== HELPER PROJECT DEEP DIVE ===\n")
helper_themes = collections.Counter()
for s in sessions:
    proj = s.get("project", "")
    if "Helper" in proj:
        d = s.get("display", "").strip().lower()
        if len(d) > 10:
            if "butterfly" in d: helper_themes["Butterfly analysis"] += 1
            elif "bcs" in d or "bull call" in d or "spread" in d: helper_themes["BCS/Bull Call Spread"] += 1
            elif "fallen hero" in d or "jade lizard" in d: helper_themes["Fallen Hero"] += 1
            elif "watchlist" in d or "alert" in d or "price" in d: helper_themes["Watchlist/alerts"] += 1
            elif "position" in d or "pnl" in d or "check" in d: helper_themes["Position check"] += 1
            elif "option" in d or "chain" in d or "csv" in d: helper_themes["Options data/CSV"] += 1
            elif "drive" in d or "google" in d or "sync" in d: helper_themes["Drive sync"] += 1
            elif "monitor" in d or "cron" in d: helper_themes["Monitor/cron"] += 1
            elif "telegram" in d: helper_themes["Telegram"] += 1
            elif "scanner" in d or "stock" in d: helper_themes["Scanner/stock"] += 1
            elif "playbook" in d or "strategy" in d: helper_themes["Playbook/strategy"] += 1
            elif "trade" in d or "enter" in d or "execute" in d: helper_themes["Trade entry"] += 1
            else: helper_themes["General"] += 1

for theme, count in helper_themes.most_common():
    print(f"  {count:4d}  {theme}")

# FIFTY deep dive
print("\n=== FIFTY PROJECT DEEP DIVE ===\n")
fifty_themes = collections.Counter()
for s in sessions:
    if "FIFTY" in s.get("project", ""):
        d = s.get("display", "").strip().lower()
        if len(d) > 10:
            if "report" in d or "daily" in d or "summary" in d: fifty_themes["Reports/daily summary"] += 1
            elif "telegram" in d: fifty_themes["Telegram"] += 1
            elif "scan" in d or "filter" in d or "signal" in d: fifty_themes["Scanning/signals"] += 1
            elif "cron" in d or "schedule" in d or "server" in d: fifty_themes["Cron/deploy"] += 1
            elif "oi" in d or "open interest" in d: fifty_themes["Open Interest"] += 1
            elif "nifty" in d or "index" in d: fifty_themes["NIFTY/index"] += 1
            else: fifty_themes["General"] += 1

for theme, count in fifty_themes.most_common():
    print(f"  {count:4d}  {theme}")

# TVS/Accounts deep dive
print("\n=== TVS PYTHON APPS DEEP DIVE ===\n")
tvs_themes = collections.Counter()
for s in sessions:
    if "TVS" in s.get("project", ""):
        d = s.get("display", "").strip().lower()
        if len(d) > 10:
            if "inward" in d: tvs_themes["Inward processing"] += 1
            elif "invoice" in d or "bill" in d: tvs_themes["Invoicing"] += 1
            elif "report" in d or "excel" in d: tvs_themes["Reports/Excel"] += 1
            elif "pl" in d or "profit" in d or "loss" in d: tvs_themes["P&L"] += 1
            elif "accounts" in d or "account" in d: tvs_themes["Accounts"] += 1
            elif "migration" in d: tvs_themes["Migration"] += 1
            elif "mail" in d or "email" in d: tvs_themes["Email"] += 1
            else: tvs_themes["General"] += 1

for theme, count in tvs_themes.most_common():
    print(f"  {count:4d}  {theme}")
