"""NIFTY non-regression gate: replay a recording through OLD and NEW code and diff (spec §12).

Usage:
  python replay_diff.py --recording harness/recordings/2026-10-07            # old vs new
  python replay_diff.py --recording ... --self-check                          # old vs recorder
  options: --old DIR (baseline modules, default harness/baseline)  --new DIR (default ETF_PI)
           --runs N (first N minutes only)  --new-csv (serve today's op_instruments.csv to NEW)

Contract (review-2 B3):
  * Kite call sequence: zero DIVERGENCE lines for OLD (harness self-consistency) and NEW.
  * JSON state (positions / state / levels): every key the OLD run wrote exists in NEW with the
    identical value; keys only NEW wrote must be listed in allowlist.json "json_new_keys".
  * Log: OLD's lines (time field stripped) must appear in NEW in the same order; NEW-only lines
    must match an allowlist.json "log_new_patterns" regex. NIFTY LIQ/EVENT gate lines are never
    allowed (the approved NIFTY freshness gate must not fire on a normal session).
  * Telegram bodies and logs/*.jsonl records: same projection rule as JSON state.
Exit code 0 = PASS, 1 = FAIL. A report is written to <work>/report.md.
"""
import argparse
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_common as hc  # noqa: E402

LOG_TIME = re.compile(r'^\d\d:\d\d:\d\d \| ')
FORBIDDEN_NIFTY = re.compile(r'LIQ_BLOCK|EVENT_BLOCK|EVENT_FLAT|UNVERIFIED')


def build_sandbox(work, variant, src, recording, new_csv):
    sb = os.path.join(work, variant)
    if os.path.isdir(sb):
        assert os.path.abspath(sb).startswith(os.path.abspath(work)), sb
        shutil.rmtree(sb)
    shutil.copytree(os.path.join(recording, 'pre_state'), sb)
    os.makedirs(os.path.join(sb, 'logs'), exist_ok=True)
    for fn in hc.BOT_MODULES:
        if os.path.exists(os.path.join(src, fn)):
            shutil.copy2(os.path.join(src, fn), os.path.join(sb, fn))
    if new_csv and variant == 'new':
        shutil.copy2(os.path.join(src, 'op_instruments.csv'), os.path.join(sb, 'op_instruments.csv'))
    return sb


def replay(sb, runs):
    errors = []
    for pkl in runs:
        r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'replay_one.py'), sb, pkl],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if r.returncode != 0:
            errors.append((os.path.basename(pkl), r.stderr.strip().splitlines()[-1:] or ['?']))
    return errors


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


_ALLOWED_PATTERNS = []   # regexes from allowlist.json "json_new_key_patterns" (dated keys)


def _allowed(k, allowed):
    return k in allowed or any(p.search(str(k)) for p in _ALLOWED_PATTERNS)


def projection_diff(old, new, allowed, path='$'):
    """OLD must be contained in NEW; NEW-only keys must be allow-listed. Returns problems."""
    out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k, v in old.items():
            if k not in new:
                out.append(f'{path}.{k}: missing in NEW')
            else:
                out += projection_diff(v, new[k], allowed, f'{path}.{k}')
        for k in new.keys() - old.keys():
            if not _allowed(k, allowed):
                out.append(f'{path}.{k}: new key not allow-listed')
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append(f'{path}: length {len(old)} vs {len(new)}')
        for i, (a, b) in enumerate(zip(old, new)):
            out += projection_diff(a, b, allowed, f'{path}[{i}]')
    elif old != new:
        out.append(f'{path}: {old!r} -> {new!r}')
    return out


def log_lines(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding='utf-8', errors='replace') as f:
        return [LOG_TIME.sub('', l.rstrip('\n')) for l in f]


def find_log(sb):
    logs = sorted(glob.glob(os.path.join(sb, 'logs', 'option_buy_*.log')))
    return logs[-1] if logs else os.path.join(sb, 'option_buy.log')


def compare(a_dir, b_dir, allow, a_log=None, b_log=None, check_calls=True):
    problems = {}
    if check_calls:
        for side, d in (('A', a_dir), ('B', b_dir)):
            div = [c for c in load_jsonl(os.path.join(d, 'calls.jsonl'))
                   if c['status'] not in ('ok', 'retry_of_recorded_failure')]
            if div:
                problems[f'kite call divergences ({side})'] = [json.dumps(c)[:200] for c in div[:20]] + [f'... total {len(div)}']
    names = sorted({os.path.basename(p) for d in (a_dir, b_dir) for p in
                    glob.glob(os.path.join(d, 'st_opt_*.json'))})
    for fn in names:
        p = projection_diff(load_json(os.path.join(a_dir, fn)), load_json(os.path.join(b_dir, fn)),
                            set(allow.get('json_new_keys', [])))
        if p:
            problems[fn] = p[:30] + ([f'... total {len(p)}'] if len(p) > 30 else [])
    for fn in sorted({os.path.basename(p) for d in (a_dir, b_dir) for p in
                      glob.glob(os.path.join(d, 'logs', '*.jsonl'))} | {'telegram.jsonl'}):
        sub = '' if fn == 'telegram.jsonl' else 'logs'
        a_recs, b_recs = load_jsonl(os.path.join(a_dir, sub, fn)), load_jsonl(os.path.join(b_dir, sub, fn))
        if fn == 'telegram.jsonl':   # NEW-only alerts allowed by pattern (review-4 R4-8)
            tpats = [re.compile(x) for x in allow.get('telegram_new_patterns', [])]
            b_recs = [r for r in b_recs if not any(p.search(r.get('body', '')) for p in tpats)]
        p = projection_diff(a_recs, b_recs, set(allow.get('json_new_keys', [])))
        if p:
            problems[fn] = p[:30] + ([f'... total {len(p)}'] if len(p) > 30 else [])
    la, lb = log_lines(a_log or find_log(a_dir)), log_lines(b_log or find_log(b_dir))
    pats = [re.compile(x) for x in allow.get('log_new_patterns', [])]
    bad = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            bad += [f'- {l}' for l in la[i1:i2]]
        if tag in ('replace', 'insert'):
            bad += [f'+ {l}' for l in lb[j1:j2] if FORBIDDEN_NIFTY.search(l) or not any(p.search(l) for p in pats)]
    if bad:
        problems['option_buy log'] = bad[:40] + ([f'... total {len(bad)}'] if len(bad) > 40 else [])
    return problems


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # Windows console is cp1252
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--recording', required=True)
    ap.add_argument('--old', default=os.path.join(HERE, 'baseline'))
    ap.add_argument('--new', default=os.path.dirname(HERE))
    ap.add_argument('--work', default=None)
    ap.add_argument('--runs', type=int, default=0)
    ap.add_argument('--self-check', action='store_true')
    ap.add_argument('--new-csv', action='store_true')
    a = ap.parse_args()
    rec = os.path.abspath(a.recording)
    work = a.work or os.path.join(HERE, 'replay_work', os.path.basename(rec))
    os.makedirs(work, exist_ok=True)
    runs = sorted(glob.glob(os.path.join(rec, 'runs', '*.pkl')))
    if a.runs:
        runs = runs[:a.runs]
    allow = load_json(os.path.join(HERE, 'allowlist.json')) or {}
    _ALLOWED_PATTERNS[:] = [re.compile(x) for x in allow.get('json_new_key_patterns', [])]

    sb_old = build_sandbox(work, 'old', a.old, rec, False)
    err_old = replay(sb_old, runs)
    if a.self_check:
        # Replay-old must reproduce what the recorder itself did, byte for byte (minus clock).
        problems = compare(os.path.join(rec, 'post'), sb_old, {}, a_log=os.path.join(rec, 'post', 'option_buy.log'),
                           check_calls=False)
        old_div = [c for c in load_jsonl(os.path.join(sb_old, 'calls.jsonl')) if c['status'] != 'ok']
        if old_div:
            problems['kite call divergences (replay-old)'] = [json.dumps(c)[:200] for c in old_div[:20]]
        label, errs = 'SELF-CHECK recorder vs replay-old', err_old
    else:
        sb_new = build_sandbox(work, 'new', a.new, rec, a.new_csv)
        err_new = replay(sb_new, runs)
        problems = compare(sb_old, sb_new, allow)
        label, errs = 'OLD vs NEW', err_old + err_new
    if errs:
        problems['replay process errors'] = [f'{r}: {e}' for r, e in errs[:20]]
    verdict = 'PASS' if not problems else 'FAIL'
    lines = [f'# {label} — {os.path.basename(rec)} — {len(runs)} runs — {verdict}', '']
    for k, v in problems.items():
        lines += [f'## {k}', *[f'    {x}' for x in v], '']
    with open(os.path.join(work, 'report.md'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print('\n'.join(lines[:60]))
    sys.exit(0 if verdict == 'PASS' else 1)


if __name__ == '__main__':
    main()
