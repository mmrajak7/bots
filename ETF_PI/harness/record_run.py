"""Record one cron minute of the UNMODIFIED baseline option_buy.py (spec §12, step 0).

Cron (Pi), offset 25 s from the live bot so the two never burst Kite together (review-2 B11):
  * 9-15 * * 1-5 sleep 25; /usr/bin/flock -n /tmp/ob_record.lock /usr/bin/python3 \
      /home/trustit/Desktop/Scripts/bots/ETF_PI/harness/record_run.py >> \
      /home/trustit/Desktop/Scripts/bots/ETF_PI/harness/record.log 2>&1

Each invocation:
  1. first run of a day: (re)build the sandbox from the live dir, refusing to run unless
     option_buy.py matches the baseline hash; seed state ONCE (sandbox creation); copy today's
     op_instruments.csv + config; snapshot the sandbox as recordings/<date>/pre_state/
  2. import the sandbox bot with the clock frozen at run start, Kite wrapped by RecordingKite,
     Telegram captured — then run main() exactly like cron does
  3. write recordings/<date>/runs/<HHMMSS>.pkl (every Kite call with its response) and mirror
     the sandbox outputs into recordings/<date>/post/ (end-of-day self-check target)

The recorder trades nothing and sends nothing: Neo stays off (config has trade_enabled: false,
and the sandbox bridge is never initialised with orders), Telegram goes to a file.
"""
import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_common as hc  # noqa: E402

BASELINE_SHA256 = '2b897a4cb1f9ced43c9473b79ea4d0005d8880b965034326371c6c443db194cc'
TOKEN_PATH = (r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
              if platform.system() == 'Windows'
              else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')


class RecordingKite:
    """Pass-through to the real KiteConnect that keeps every call and its result.

    ltp() is answered from quote() (same keys) and recorded as 'ltp': the replay can then serve
    either the old code's ltp() or a refactor's quote() from the same recorded response, and the
    recording carries bid/ask/last_trade_time the live bot never stored.
    """
    def __init__(self):
        from kiteconnect import KiteConnect
        with open(TOKEN_PATH) as f:
            td = json.load(f)
        self._k = KiteConnect(api_key=td['api_key'])
        self._k.set_access_token(td['access_token'])
        self.calls = []

    def _rec(self, method, args, kwargs, fn):
        entry = {'method': method, 'args': args, 'kwargs': kwargs, 'resp': None, 'exc': None,
                 'exc_type': None}
        try:
            entry['resp'] = fn()
            return entry['resp']
        except Exception as e:
            # str(e) exactly as the bot logs it ("LTP failed: <str(e)>"), so a replay that
            # re-raises it reproduces the recorded log line; the type is kept separately.
            entry['exc'] = str(e) or type(e).__name__
            entry['exc_type'] = type(e).__name__
            raise
        finally:
            self.calls.append(entry)

    def ltp(self, keys):
        resp = self._rec('ltp', (keys,), {}, lambda: self._k.quote(keys))
        return hc.ltp_view(resp)

    def quote(self, keys):
        return self._rec('quote', (keys,), {}, lambda: self._k.quote(keys))

    def historical_data(self, *args, **kwargs):
        return self._rec('historical_data', args, kwargs,
                         lambda: self._k.historical_data(*args, **kwargs))

    def __getattr__(self, name):
        attr = getattr(self._k, name)
        if not callable(attr):
            return attr
        return lambda *a, **k: self._rec(name, a, k, lambda: attr(*a, **k))


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


def snapshot(src, dst):
    """Copy every non-code file of the sandbox (state, levels, jsonl, csv, config)."""
    os.makedirs(dst, exist_ok=True)
    for root, _dirs, files in os.walk(src):
        if '__pycache__' in root:
            continue
        for fn in files:
            if fn.endswith(('.py', '.pyc', '.lock')) or (root.endswith('logs') and fn.endswith('.log')):
                continue
            rel = os.path.relpath(os.path.join(root, fn), src)
            os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), os.path.join(dst, rel))


def force_paper(cfg_path):
    """The sandbox must never reach Neo, even if the live config is switched to trading."""
    import yaml
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
    if cfg.get('trade_enabled'):
        cfg['trade_enabled'] = False
        cfg['log_only'] = True
        with open(cfg_path, 'w') as f:
            yaml.safe_dump(cfg, f)


def prepare_day(live, sandbox, day_dir):
    """First run of the day: verify baseline, (create +) refresh the sandbox, snapshot it."""
    if sha256(os.path.join(live, 'option_buy.py')) != BASELINE_SHA256:
        raise SystemExit('REFUSING: live option_buy.py is not the baseline (sha256 mismatch). '
                         'The recording must be made with the unmodified base.')
    created = not os.path.exists(os.path.join(sandbox, 'option_buy.py'))
    os.makedirs(os.path.join(sandbox, 'logs'), exist_ok=True)
    for fn in hc.BOT_MODULES + hc.BOT_DATA + ['op_instruments.csv']:
        if os.path.exists(os.path.join(live, fn)):
            shutil.copy2(os.path.join(live, fn), os.path.join(sandbox, fn))
    force_paper(os.path.join(sandbox, 'config_trade.yaml'))
    if created:   # seed carried state once; afterwards the sandbox evolves on its own decisions
        for fn in hc.STATE_FILES:
            if os.path.exists(os.path.join(live, fn)):
                shutil.copy2(os.path.join(live, fn), os.path.join(sandbox, fn))
        for fn in hc.LOG_STATE_FILES:
            if os.path.exists(os.path.join(live, 'logs', fn)):
                shutil.copy2(os.path.join(live, 'logs', fn), os.path.join(sandbox, 'logs', fn))
    snapshot(sandbox, os.path.join(day_dir, 'pre_state'))
    # Review-2 D3: a position still open from an earlier day makes _close_stale run on this
    # day's first minute and changes the quote sequence for old vs new. Record it loudly.
    stale_open = []
    try:
        pos = json.load(open(os.path.join(sandbox, 'st_opt_positions.json')))
        today = datetime.datetime.now().strftime('%Y%m%d')
        stale_open = [k for k, p in pos.items() if isinstance(p, dict) and p.get('status') == 'open'
                      and p.get('entry_date', today) < today]
    except Exception:
        pass
    if stale_open:
        print(f'WARNING: recording day starts with stale open positions {stale_open} — '
              f'this day cannot serve as a clean NIFTY proof', flush=True)
    meta = {'created_sandbox': created, 'baseline_sha256': BASELINE_SHA256, 'stale_open_at_start': stale_open,
            'python': sys.version, 'prepared_at': datetime.datetime.now().isoformat()}
    with open(os.path.join(day_dir, 'day_meta.json'), 'w') as f:
        json.dump(meta, f, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', default=os.path.dirname(HERE))
    ap.add_argument('--sandbox', default=os.path.join(HERE, 'sandbox_rec'))
    ap.add_argument('--out', default=os.path.join(HERE, 'recordings'))
    ap.add_argument('--frozen', default=None,
                    help='TEST ONLY: pin the clock to YYYY-MM-DDTHH:MM (harness self-test after hours)')
    a = ap.parse_args()

    import pytz
    ist = pytz.timezone('Asia/Kolkata')
    frozen = (ist.localize(datetime.datetime.strptime(a.frozen, '%Y-%m-%dT%H:%M')) if a.frozen
              else datetime.datetime.now(ist).replace(microsecond=0))
    if frozen.weekday() >= 5:
        return
    day_dir = os.path.join(a.out, frozen.strftime('%Y-%m-%d'))
    os.makedirs(os.path.join(day_dir, 'runs'), exist_ok=True)
    if not os.path.exists(os.path.join(day_dir, 'day_meta.json')):
        prepare_day(a.live, a.sandbox, day_dir)

    run_id = frozen.strftime('%H%M%S')
    kite = RecordingKite()
    sink = hc.TelegramSink(os.path.join(a.sandbox, 'telegram.jsonl'), run_id)
    error = None
    try:
        ob = hc.import_bot(a.sandbox, frozen, kite, sink, sleep_noop=False)
        hc.run_main(ob)
    except Exception:
        error = traceback.format_exc()
        print(f'{run_id} recorder error:\n{error}', flush=True)
    hc.save_pickle({'frozen': frozen, 'run_id': run_id, 'calls': kite.calls, 'error': error},
                   os.path.join(day_dir, 'runs', f'{run_id}.pkl'))
    snapshot(a.sandbox, os.path.join(day_dir, 'post'))
    log = os.path.join(a.sandbox, 'logs', f"option_buy_{frozen.strftime('%Y-%m-%d')}.log")
    if os.path.exists(log):
        shutil.copy2(log, os.path.join(day_dir, 'post', 'option_buy.log'))
    print(f'{run_id} ok calls={len(kite.calls)}', flush=True)


if __name__ == '__main__':
    main()
