"""Replay ONE recorded cron minute against the bot code in a sandbox (spec §12, step 0).

Usage: python -I replay_one.py <sandbox_dir> <run.pkl>
Run in a fresh interpreter per minute (replay_diff.py does this): the bot keeps module-level
state exactly as a cron process does, so reusing one interpreter would not be a faithful replay.

Every Kite request is answered from the recording; requests are matched on their canonical
form (harness_common.norm_args, ltp == quote family). Anything that does not match the next
recorded call in order is written to <sandbox>/calls.jsonl as a DIVERGENCE — the "same ledger
but different API work" case review-2 B2 asked to detect. Unmatched requests raise, which the bot
treats exactly like a Kite failure (its helpers catch Exception).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness_common as hc  # noqa: E402


MAX_RETRIES = 2   # = option_buy.KITE_RETRIES; a refactor never retries more than this


class ReplayKite:
    def __init__(self, calls, log_path, run_id):
        self.calls = calls
        self.keys = [hc.norm_args(c['method'], c['args'], c['kwargs']) for c in calls]
        self.used = [False] * len(calls)
        self.ptr = 0
        self.last = None
        self.retries = 0
        self.log_path, self.run_id = log_path, run_id

    def _log(self, **rec):
        rec['run'] = self.run_id
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, default=str) + '\n')

    def _serve(self, method, args, kwargs):
        key = hc.norm_args(method, args, kwargs)
        in_order = (self.ptr < len(self.keys) and not self.used[self.ptr] and self.keys[self.ptr] == key)
        # A refactor may RETRY a call the recording saw fail (spec §13 retry policy). Serving the
        # same recorded error again reproduces the old outcome (the call fails) without counting
        # the retry as a divergence. Only when the NEXT recorded call is not this key (review-4
        # R4-1: the baseline itself makes back-to-back identical quotes — pre-shift then level
        # update — and the second must get its own recorded answer), and at most MAX_RETRIES times.
        if (not in_order and self.last is not None and self.keys[self.last] == key
                and self.calls[self.last]['exc'] and self.retries < MAX_RETRIES):
            self.retries += 1
            self._log(status='retry_of_recorded_failure', method=method, key=key, idx=self.last)
            raise Exception(self.calls[self.last]['exc'])
        idx = self.ptr if in_order else next(
            (i for i, k in enumerate(self.keys) if not self.used[i] and k == key), None)
        if idx is None:
            self._log(status='DIVERGENCE_UNRECORDED', method=method, key=key)
            raise Exception(f'REPLAY: no recorded response for {method} {key}')
        status = 'ok' if idx == self.ptr else 'DIVERGENCE_ORDER'
        self._log(status=status, method=method, key=key, idx=idx, expected=self.ptr)
        self.used[idx] = True
        self.last = idx
        self.retries = 0
        while self.ptr < len(self.used) and self.used[self.ptr]:
            self.ptr += 1
        c = self.calls[idx]
        if c['exc']:
            raise Exception(c['exc'])
        resp = c['resp']
        if method == 'ltp' and c['method'] in ('ltp', 'quote'):
            return hc.ltp_view(resp)
        return resp

    def ltp(self, keys):
        return self._serve('ltp', (keys,), {})

    def quote(self, keys):
        return self._serve('quote', (keys,), {})

    def historical_data(self, *args, **kwargs):
        return self._serve('historical_data', args, kwargs)

    def __getattr__(self, name):
        # Any other Kite method the bot (or a refactor) calls is served the same way.
        return lambda *a, **k: self._serve(name, a, k)

    def finish(self):
        for i, u in enumerate(self.used):
            if not u:
                self._log(status='DIVERGENCE_UNUSED', method=self.calls[i]['method'], key=self.keys[i], idx=i)


def main():
    sandbox, pkl = sys.argv[1], sys.argv[2]
    rec = hc.load_pickle(pkl)
    kite = ReplayKite(rec['calls'], os.path.join(sandbox, 'calls.jsonl'), rec['run_id'])
    sink = hc.TelegramSink(os.path.join(sandbox, 'telegram.jsonl'), rec['run_id'])
    ob = hc.import_bot(sandbox, rec['frozen'], kite, sink, sleep_noop=True)
    hc.run_main(ob)
    kite.finish()


if __name__ == '__main__':
    main()
