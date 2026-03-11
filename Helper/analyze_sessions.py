import json
import collections
import datetime
import re

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

print(f"Total sessions: {len(sessions)}")
print()

# Group by project
projects = collections.Counter()
for s in sessions:
    proj = s.get("project", "unknown")
    proj = proj.replace("C:\\Users\\mail2\\Documents\\", "").replace("C:\\Users\\mail2\\", "~/")
    projects[proj] += 1

print("=== SESSIONS BY PROJECT ===")
for proj, count in projects.most_common(50):
    print(f"  {count:4d}  {proj}")

# Date range
timestamps = [s.get("timestamp", 0) for s in sessions if s.get("timestamp")]
if timestamps:
    earliest = datetime.datetime.fromtimestamp(min(timestamps) / 1000)
    latest = datetime.datetime.fromtimestamp(max(timestamps) / 1000)
    print(f"\nDate range: {earliest.strftime('%Y-%m-%d')} to {latest.strftime('%Y-%m-%d')}")
    print(f"Total days span: {(latest - earliest).days}")
    print(f"Avg sessions/day: {len(sessions) / max((latest - earliest).days, 1):.1f}")

# Now analyze the content of prompts
print("\n\n=== ACTIVITY CATEGORIZATION ===\n")

categories = {
    "Bug Fix / Debug": [],
    "New Feature / Build": [],
    "Refactor / Improve": [],
    "Code Review / Audit": [],
    "Deploy / Cron / Server": [],
    "Data Analysis / Research": [],
    "Trade Entry / Execution": [],
    "Trade Monitoring / Position Check": [],
    "Strategy Design / Playbook": [],
    "SAP / ABAP Development": [],
    "Config / Setup": [],
    "Git Operations": [],
    "Explain / Understand Code": [],
    "File / Data Operations": [],
    "Testing": [],
    "Documentation": [],
    "Scraping / API Integration": [],
    "Other": [],
}

# Keywords for categorization
patterns = {
    "Bug Fix / Debug": r"bug|fix|error|broken|crash|fail|issue|debug|not working|doesn.t work|wrong|traceback|exception|stack trace",
    "New Feature / Build": r"build|create|add|implement|new feature|write a|make a|develop|scaffold|generate|setup.*script",
    "Refactor / Improve": r"refactor|clean|improve|optimize|simplify|reorganize|restructur|elegant|better way",
    "Code Review / Audit": r"review|audit|check.*code|validate|inspect|look at|examine|security",
    "Deploy / Cron / Server": r"deploy|cron|server|production|schedule|systemd|service|linux|ssh|remote",
    "Data Analysis / Research": r"analys|research|backtest|compare|statistics|chart|plot|graph|data.*look|monte carlo|simulation",
    "Trade Entry / Execution": r"enter.*trade|execute|place.*order|buy.*sell|filled|entry|spread.*trade|fallen hero.*enter|bcs.*enter",
    "Trade Monitoring / Position Check": r"position|monitor|check.*p.l|spread.*value|current.*price|ltp|quote|how.*doing|status.*trade",
    "Strategy Design / Playbook": r"strategy|playbook|when.*enter|risk.*reward|edge|thesis|when to|plan.*trade|iron condor|butterfly|bull call|bear put|wheel|jade lizard|fallen hero.*design",
    "SAP / ABAP Development": r"sap|abap|fiori|cds|bapi|bw|hana|smartform|alv|idoc|rfc|odata|zcl_|z_|se38|se80|tcode|transport",
    "Config / Setup": r"config|setting|setup|install|environment|credential|token|api key|authenticate",
    "Git Operations": r"commit|push|pull|merge|branch|git |rebase|cherry.pick|stash",
    "Explain / Understand Code": r"explain|what does|how does|understand|walk.*through|trace|flow|logic.*behind",
    "File / Data Operations": r"read.*file|write.*file|csv|json.*file|parse|convert|format|rename|move.*file|copy.*file|scrape",
    "Testing": r"test|unittest|pytest|mock|coverage|assert|spec",
    "Documentation": r"document|readme|comment|docstring|changelog|instruction",
    "Scraping / API Integration": r"scrape|crawl|api.*integrat|fetch.*data|web.*request|http|endpoint|webhook|telegram.*bot",
}

category_counts = collections.Counter()
project_category = collections.defaultdict(lambda: collections.Counter())

for s in sessions:
    display = s.get("display", "").lower()
    proj = s.get("project", "unknown")
    proj = proj.replace("C:\\Users\\mail2\\Documents\\", "").replace("C:\\Users\\mail2\\", "~/")

    matched = False
    for cat, pattern in patterns.items():
        if re.search(pattern, display, re.IGNORECASE):
            category_counts[cat] += 1
            project_category[proj][cat] += 1
            matched = True
            break  # first match wins

    if not matched:
        category_counts["Other"] += 1
        project_category[proj]["Other"] += 1

print("Overall activity distribution:")
for cat, count in category_counts.most_common():
    pct = count / len(sessions) * 100
    bar = "#" * int(pct)
    print(f"  {count:4d} ({pct:5.1f}%) {bar} {cat}")

print("\n\n=== TOP PROJECTS WITH ACTIVITY BREAKDOWN ===\n")
for proj, count in projects.most_common(15):
    print(f"\n--- {proj} ({count} sessions) ---")
    proj_cats = project_category[proj]
    for cat, cnt in proj_cats.most_common(5):
        print(f"    {cnt:3d}  {cat}")

# Extract common action verbs / request patterns
print("\n\n=== COMMON REQUEST PATTERNS (First 10 words of prompts) ===\n")
first_words = collections.Counter()
for s in sessions:
    display = s.get("display", "").strip()
    if display:
        words = display.split()[:6]
        phrase = " ".join(words).lower()
        # Normalize
        phrase = re.sub(r"[^a-z0-9 /]", "", phrase)
        if len(phrase) > 5:
            first_words[phrase] += 1

print("Most common prompt starts:")
for phrase, count in first_words.most_common(40):
    if count >= 2:
        print(f"  {count:3d}x  {phrase}")

# Slash commands used
print("\n\n=== SLASH COMMANDS USED ===\n")
slash_cmds = collections.Counter()
for s in sessions:
    display = s.get("display", "").strip()
    if display.startswith("/"):
        cmd = display.split()[0]
        slash_cmds[cmd] += 1

for cmd, count in slash_cmds.most_common():
    print(f"  {count:3d}x  {cmd}")

# Repetitive patterns - things done 3+ times
print("\n\n=== REPETITIVE TASKS (done 3+ times, candidates for automation) ===\n")
# Look for very similar prompts
normalized_prompts = collections.Counter()
for s in sessions:
    display = s.get("display", "").strip().lower()
    # Normalize: remove specific names/numbers
    norm = re.sub(r"\b[A-Z]{3,}[0-9]*\b", "STOCK", display, flags=re.IGNORECASE)
    norm = re.sub(r"\b\d+\.?\d*\b", "NUM", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    if 10 < len(norm) < 200:
        normalized_prompts[norm] += 1

print("Repeated prompt patterns (normalized):")
for prompt, count in normalized_prompts.most_common(30):
    if count >= 3:
        print(f"  {count:3d}x  {prompt[:120]}")

# Print full prompts for manual review (sample)
print("\n\n=== SAMPLE PROMPTS BY CATEGORY (for manual review) ===\n")
for cat in ["Trade Entry / Execution", "Trade Monitoring / Position Check", "Strategy Design / Playbook", "Deploy / Cron / Server"]:
    print(f"\n--- {cat} ---")
    count = 0
    for s in sessions:
        display = s.get("display", "").strip()
        proj = s.get("project", "")
        if re.search(patterns.get(cat, "NOMATCH"), display, re.IGNORECASE):
            ts = s.get("timestamp", 0)
            dt = datetime.datetime.fromtimestamp(ts/1000).strftime("%m-%d") if ts else "?"
            print(f"  [{dt}] {display[:150]}")
            count += 1
            if count >= 8:
                break
