import json
import collections
import datetime
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

HISTORY_FILE = r"C:\Users\mail2\.claude\history.jsonl"

sessions = []
with open(HISTORY_FILE, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            sessions.append(obj)
        except Exception:
            pass

# More granular patterns - order matters (first match wins)
patterns = [
    ("Slash Command", r"^/\w"),
    ("Continue / Proceed", r"^(continue|proceed|go ahead|yes|ok|do it|yes fix|yes implement|yes pls|carry on)\s*$"),
    ("Push to Git", r"push to git|push to github|commit.*push|git push"),
    ("Git Operations", r"commit|merge|branch|rebase|cherry.pick|stash|git (status|diff|log|add|reset)"),
    ("Bug Fix / Debug", r"bug|fix\b|error|broken|crash|fail|issue|debug|not working|doesn.t work|wrong|traceback|exception|stack trace|typo|undefined|NoneType|KeyError|TypeError|AttributeError|IndexError|missing|incorrect"),
    ("Code Review / Audit", r"review|audit|check.*code|validate|inspect|comprehensive.*review|code.*quality|lint|mypy|ruff|flake"),
    ("SAP / ABAP Development", r"sap\b|abap|fiori|cds\b|bapi|bw\b|hana|smartform|alv|idoc|rfc|odata|zcl_|z_|se38|se80|tcode|transport|ewm|scwm|lsmw|boe|crystal|webi"),
    ("Refactor / Improve", r"refactor|clean|improve|optimize|simplify|reorganize|restructur|elegant|better way|performance|speed up"),
    ("Testing", r"\btest\b|unittest|pytest|mock|coverage|assert|spec|verify|backtest"),
    ("Deploy / Cron / Server", r"deploy|cron|server|production|schedule|systemd|service|ssh|remote|linux|flock|supervisor|pm2"),
    ("Trade Entry / Execution", r"enter.*trade|execute|place.*order|buy.*sell|filled|entry price|spread.*trade|fallen hero.*enter|bcs.*enter|position.*open|order.*place"),
    ("Trade Monitoring / Position Check", r"position|monitor|check.*p.l|spread.*value|current.*price|ltp|quote|how.*doing|status.*trade|close.*trade|exit.*trade|trailing|stop.?loss"),
    ("Strategy Design / Playbook", r"strategy|playbook|when.*enter|risk.*reward|edge|thesis|when to|plan.*trade|iron condor|butterfly|bull call|bear put|wheel|jade lizard|fallen hero.*design|monte carlo|simulation|backtest.*engine",),
    ("Data Analysis / Research", r"analy[sz]|research|compare|statistics|chart|plot|graph|data.*look|report|summary|breakdown|distribution"),
    ("New Feature / Build", r"build|create|add\b|implement|new feature|write a|make a|develop|scaffold|generate|setup.*script|new.*script|new.*module|new.*function"),
    ("Config / Setup", r"config|setting|setup|install|environment|credential|token|api key|authenticate|login|connect"),
    ("Explain / Understand Code", r"explain|what does|how does|understand|walk.*through|trace|flow|logic.*behind|why does|what is|tell me|describe"),
    ("File / Data Operations", r"read.*file|write.*file|csv|json.*file|parse|convert|format|rename|move.*file|copy.*file|download|upload|fetch.*data"),
    ("Documentation", r"document|readme|comment|docstring|changelog|instruction|claude\.md|memory"),
    ("Scraping / API Integration", r"scrape|crawl|api.*integrat|web.*request|http|endpoint|webhook|telegram.*bot|kite.*api"),
    ("UI / Display", r"display|gui|ui\b|window|layout|render|show|print|output|table|format.*output|color|highlight"),
    ("Short/Vague Prompt", r"^.{1,15}$"),
]

category_counts = collections.Counter()
category_samples = collections.defaultdict(list)
project_category = collections.defaultdict(lambda: collections.Counter())

for s in sessions:
    display = s.get("display", "").strip()
    proj = s.get("project", "unknown")
    proj = proj.replace("C:\\Users\\mail2\\Documents\\", "").replace("C:\\Users\\mail2\\", "~/")
    ts = s.get("timestamp", 0)

    matched = False
    for cat, pattern in patterns:
        if re.search(pattern, display, re.IGNORECASE):
            category_counts[cat] += 1
            project_category[proj][cat] += 1
            if len(category_samples[cat]) < 5:
                category_samples[cat].append((proj, display[:200]))
            matched = True
            break

    if not matched:
        category_counts["Uncategorized"] += 1
        project_category[proj]["Uncategorized"] += 1
        if len(category_samples["Uncategorized"]) < 20:
            category_samples["Uncategorized"].append((proj, display[:200]))

print("=== ACTIVITY DISTRIBUTION (refined) ===\n")
total = len(sessions)
for cat, count in category_counts.most_common():
    pct = count / total * 100
    bar = "#" * int(pct / 1.5)
    print(f"  {count:4d} ({pct:5.1f}%) {bar:30s} {cat}")

print(f"\n  Total: {total}")

# Domain breakdown
print("\n\n=== DOMAIN BREAKDOWN ===\n")
domain_map = {
    "Trading Bots (SNAIL/Scalper/FIFTY/Bouncer/CROCODILE/Striker)": r"BOTS[\\/](SNAIL|Scalper|FIFTY|Bouncer|CROCODILE|Striker|Watcher|ConvictBar|Sniper)",
    "Helper (Options/BCS/FH)": r"BOTS[\\/]Helper|BOTS[\\/]data",
    "BOTS (root)": r"BOTS$",
    "SAP/ABAP (DHL)": r"SAP[\\/]DHL",
    "SAP/ABAP (Derlin)": r"SAP[\\/]Derlin",
    "SAP/ABAP (MRS)": r"SAP[\\/]MRS",
    "TVS Python Apps": r"Python[\\/]TVS",
    "ETF Analysis": r"ETF_PI|Analysis",
    "Backtesting": r"backtest",
    "Investment": r"Investment",
    "Other": r".",
}
domain_counts = collections.OrderedDict()
for s in sessions:
    proj = s.get("project", "unknown")
    for domain, pat in domain_map.items():
        if re.search(pat, proj):
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            break

for domain, count in sorted(domain_counts.items(), key=lambda x: -x[1]):
    pct = count / total * 100
    print(f"  {count:4d} ({pct:5.1f}%)  {domain}")

# Now let's look at what "Uncategorized" actually contains
print("\n\n=== UNCATEGORIZED SAMPLES (to improve classification) ===\n")
for proj, display in category_samples["Uncategorized"][:20]:
    safe = display.encode('ascii', 'replace').decode('ascii')
    print(f"  [{proj[:30]:30s}] {safe[:140]}")

# Workflow patterns - sequences of actions
print("\n\n=== WORKFLOW PATTERNS (consecutive sessions in same project) ===\n")
workflow_transitions = collections.Counter()
prev_cat = None
prev_proj = None
for s in sessions:
    display = s.get("display", "").strip()
    proj = s.get("project", "unknown")

    curr_cat = "Uncategorized"
    for cat, pattern in patterns:
        if re.search(pattern, display, re.IGNORECASE):
            curr_cat = cat
            break

    if prev_proj == proj and prev_cat and prev_cat != curr_cat:
        workflow_transitions[(prev_cat, curr_cat)] += 1

    prev_cat = curr_cat
    prev_proj = proj

print("Common transitions (A -> B):")
for (a, b), count in workflow_transitions.most_common(20):
    if count >= 5:
        print(f"  {count:3d}x  {a:35s} -> {b}")

# Time-of-day analysis
print("\n\n=== TIME OF DAY ANALYSIS ===\n")
hour_counts = collections.Counter()
for s in sessions:
    ts = s.get("timestamp", 0)
    if ts:
        dt = datetime.datetime.fromtimestamp(ts / 1000)
        hour_counts[dt.hour] += 1

for hour in range(6, 24):
    count = hour_counts.get(hour, 0)
    bar = "#" * (count // 5)
    print(f"  {hour:02d}:00  {count:4d}  {bar}")

# Day of week
print("\n\n=== DAY OF WEEK ===\n")
dow_counts = collections.Counter()
dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
for s in sessions:
    ts = s.get("timestamp", 0)
    if ts:
        dt = datetime.datetime.fromtimestamp(ts / 1000)
        dow_counts[dt.weekday()] += 1

for dow in range(7):
    count = dow_counts.get(dow, 0)
    bar = "#" * (count // 10)
    print(f"  {dow_names[dow]}  {count:4d}  {bar}")

# Specific recurring tasks
print("\n\n=== SPECIFIC RECURRING TASKS (Automation Candidates) ===\n")

recurring_patterns = [
    ("Push to git/github", r"push to git"),
    ("Fix lint/type errors", r"fix.*(lint|type|mypy|ruff|flake)"),
    ("Run/check tests", r"run.*test|check.*test|pytest|test.*fail"),
    ("Read logs", r"read.*log|check.*log|tail.*log"),
    ("Check positions/P&L", r"(check|show|what).*(position|p.?l|pnl|profit|loss)"),
    ("Refresh data/CSV", r"refresh|fetch.*data|update.*csv|kite.*option|instrument"),
    ("Deploy to server", r"deploy|cron|server.*setup|push.*server"),
    ("Memory/context management", r"remember|memory|save.*context|update.*claude"),
    ("Code review request", r"review|audit|comprehensive.*check"),
    ("Explain/understand code", r"explain|what does|how does|walk.*through"),
    ("Iteration/retry prompts", r"do.*\d+.*times|iteration|try again|retry"),
    ("Continue/proceed", r"^(continue|proceed|go ahead|yes|ok)\s*$"),
    ("Format/display fix", r"format|display|table|output.*look|print.*wrong"),
    ("Config changes", r"config|setting|threshold|parameter|change.*value"),
]

for label, pat in recurring_patterns:
    count = 0
    for s in sessions:
        display = s.get("display", "").strip()
        if re.search(pat, display, re.IGNORECASE):
            count += 1
    if count > 0:
        print(f"  {count:4d}x  {label}")

# Duration between sessions (proxy for session length)
print("\n\n=== SESSION INTENSITY BY PROJECT (avg sessions/active day) ===\n")
project_days = collections.defaultdict(set)
for s in sessions:
    proj = s.get("project", "unknown")
    proj = proj.replace("C:\\Users\\mail2\\Documents\\", "").replace("C:\\Users\\mail2\\", "~/")
    ts = s.get("timestamp", 0)
    if ts:
        dt = datetime.datetime.fromtimestamp(ts / 1000)
        project_days[proj].add(dt.date())

for proj, count in sorted(collections.Counter({p: len(sessions) for p, sessions in project_days.items()}).items(), key=lambda x: -x[1])[:15]:
    total_sessions = sum(1 for s in sessions if s.get("project", "").replace("C:\\Users\\mail2\\Documents\\", "").replace("C:\\Users\\mail2\\", "~/") == proj)
    active_days = len(project_days[proj])
    intensity = total_sessions / max(active_days, 1)
    print(f"  {proj:45s}  {active_days:3d} days  {total_sessions:4d} sessions  {intensity:.1f}/day")
