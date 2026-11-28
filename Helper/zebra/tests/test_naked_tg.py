"""Telegram `/nresume` for the live naked kill, read by zebra's cycle.

`zebra/naked_tg.py`; design Q7 (owner chat only, one-time code, Telegram
can only clear a kill).

Run:  cd Helper && python -m pytest zebra/tests/test_naked_tg.py -v
"""
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import naked_halt as nh       # noqa: E402
from zebra import naked_tg as tg         # noqa: E402

T0 = datetime(2026, 12, 2, 11, 0, 0)
TOKEN = '123456:SECRET-TOKEN-abc'
OWNER = '987654'                          # str in the config, as on disk


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    p = tmp_path / 'telegram_config.json'
    p.write_text(json.dumps({'bot_token': TOKEN, 'chat_id': OWNER}), encoding='utf-8')
    monkeypatch.setattr(cfg, 'TELEGRAM_CONFIG', p)
    return tmp_path


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeHttp:
    """Holds a queue of updates; honours `offset` unless told not to."""

    def __init__(self, updates=(), honour_offset=True, raise_with=None, resp=None):
        self.updates = list(updates)
        self.honour = honour_offset
        self.raise_with = raise_with
        self.resp = resp
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({'url': url, 'params': dict(params or {}), 'timeout': timeout})
        if self.raise_with:
            raise self.raise_with
        if self.resp is not None:
            return self.resp
        off = (params or {}).get('offset')
        res = [u for u in self.updates
               if not self.honour or off is None or u['update_id'] >= off]
        return Resp({'ok': True, 'result': res})


_uid = iter(range(500, 10**6))


def upd(text, chat=int(OWNER), uid=None, key='message', sender=None):
    return {'update_id': uid if uid is not None else next(_uid),
            key: {'message_id': 1, 'chat': {'id': chat, 'type': 'private'},
                  'from': {'id': chat if sender is None else sender},
                  'text': text, 'date': 0}}


def kill(mode='live'):
    return nh.halt(mode, 'test kill', T0, 'test')['code']


def run(http, sent=None):
    sent = [] if sent is None else sent
    return tg.poll_resume(T0, sent.append, http=http), sent


# -- no kill, no network -----------------------------------------------------------

def test_no_network_when_nothing_is_killed():
    h = FakeHttp([upd('/nresume ABC')])
    out, sent = run(h)
    assert out == [] and sent == [] and h.calls == []
    assert not tg.offset_path().exists()


def test_unreadable_halt_file_is_never_resumed_and_no_network():
    nh.state_path('live').write_text('{not json', encoding='utf-8')
    h = FakeHttp([upd('/nresume live ABCDEF')])
    out, sent = run(h)
    assert out == [] and h.calls == []
    assert 'fails closed' in nh.entries_allowed('live', T0.date())[1]


def test_missing_state_with_journalled_kill_is_not_resumable_from_telegram():
    code = kill('live')
    nh.state_path('live').unlink()                # resume() could recover this
    rcode = kill('rehearsal')                     # so the poll does run
    h = FakeHttp([upd('/nresume live %s' % code)])
    out, sent = run(h)
    assert h.calls and out[0]['result'] == 'not_killed'
    assert not nh.state_path('live').exists()     # untouched
    with pytest.raises(nh.HaltUnreadable):
        nh.load('live')
    assert nh.load('rehearsal')['kill_code'] == rcode


# -- the command -------------------------------------------------------------------

def test_valid_code_from_owner_clears():
    code = kill()
    h = FakeHttp([upd('/nresume %s' % code.lower())])
    out, sent = run(h)
    assert out[0]['result'] == 'cleared' and out[0]['mode'] == 'live'
    s = nh.load('live')
    assert not s['killed'] and s['peak_reset_pending']
    assert s['history'][-1]['by'] == 'telegram:%s' % OWNER
    assert 'RESUMED' in sent[0] and '<' not in sent[0]
    p = h.calls[0]['params']
    assert p['timeout'] == 0 and h.calls[0]['timeout'] <= 5


def test_bot_suffix_and_explicit_mode():
    code = kill('rehearsal')
    out, _ = run(FakeHttp([upd('/nresume@ZebraBot Rehearsal %s' % code)]))
    assert out[0]['result'] == 'cleared' and not nh.load('rehearsal')['killed']


def test_wrong_code_refused_and_kill_stays_without_leaking_the_code():
    code = kill()
    out, sent = run(FakeHttp([upd('/nresume 000000')]))
    assert out[0]['result'] == 'wrong_code'
    s = nh.load('live')
    assert s['killed'] and s['kill_code'] == code
    assert s['history'][-1]['event'] == 'resume_refused'
    assert code not in sent[0] and 'wrong' in sent[0]


def test_other_chat_is_ignored(caplog):
    code = kill()
    with caplog.at_level(logging.INFO):
        out, sent = run(FakeHttp([upd('/nresume %s' % code, chat=111)]))
    assert out == [] and sent == [] and nh.load('live')['killed']
    mine = ' '.join(r.getMessage() for r in caplog.records if r.name == tg.__name__)
    assert '111' in mine and code not in mine


def test_plain_owner_text_ignored_and_malformed_gets_usage():
    kill()
    out, sent = run(FakeHttp([upd('hello'), upd('/nresume'),
                              upd('/nresume live a b'), upd('/nresume paper X')]))
    assert [o['result'] for o in out] == ['usage', 'usage', 'usage']
    assert len(sent) == 3 and all('<' not in m for m in sent)
    assert nh.load('live')['killed']


def test_edited_message_ignored():
    code = kill()
    out, sent = run(FakeHttp([upd('/nresume %s' % code, key='edited_message')]))
    assert out == [] and nh.load('live')['killed']


def test_both_killed_requires_the_mode_word():
    lc = kill('live')
    kill('rehearsal')
    out, sent = run(FakeHttp([upd('/nresume %s' % lc)]))
    assert out[0]['result'] == 'usage' and 'name the mode' in sent[0]
    assert nh.load('live')['killed'] and nh.load('rehearsal')['killed']
    out, _ = run(FakeHttp([upd('/nresume live %s' % lc)]))
    assert out[0]['result'] == 'cleared'
    assert not nh.load('live')['killed'] and nh.load('rehearsal')['killed']


def test_named_mode_that_is_not_killed():
    kill('live')
    out, sent = run(FakeHttp([upd('/nresume rehearsal ABCDEF')]))
    assert out[0]['result'] == 'not_killed' and nh.load('live')['killed']


# -- offset / once only ------------------------------------------------------------

def test_offset_persisted_and_update_never_handled_twice():
    """Reads a file written by the code under test.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    kill()
    u = upd('/nresume 000000')
    h = FakeHttp([u], honour_offset=False)        # server ignores offset
    out1, s1 = run(h)
    assert out1[0]['result'] == 'wrong_code'
    assert json.loads(tg.offset_path().read_text())['offset'] == u['update_id'] + 1
    out2, s2 = run(h)
    assert out2 == [] and s2 == []
    assert h.calls[1]['params']['offset'] == u['update_id'] + 1
    refused = [e for e in nh.load('live')['history'] if e['event'] == 'resume_refused']
    assert len(refused) == 1


def test_offset_save_failure_acts_on_nothing(monkeypatch):
    code = kill()

    def boom(*a, **k):
        raise OSError('disk full')
    monkeypatch.setattr(tg, '_save_offset', boom)
    out, sent = run(FakeHttp([upd('/nresume %s' % code)]))
    assert out == [] and nh.load('live')['killed']


def test_corrupt_offset_file_reads_from_start():
    code = kill()
    tg.offset_path().write_text('garbage', encoding='utf-8')
    h = FakeHttp([upd('/nresume %s' % code)])
    out, _ = run(h)
    assert 'offset' not in h.calls[0]['params'] and out[0]['result'] == 'cleared'


def test_rate_limit_per_cycle():
    kill()
    ups = [upd('/nresume %06d' % i) for i in range(6)]
    out, sent = run(FakeHttp(ups))
    res = [o['result'] for o in out]
    assert res.count('wrong_code') == tg.MAX_ATTEMPTS
    assert res.count('rate_limited') == 6 - tg.MAX_ATTEMPTS
    refused = [e for e in nh.load('live')['history'] if e['event'] == 'resume_refused']
    assert len(refused) == tg.MAX_ATTEMPTS
    assert sum('attempts' in m for m in sent) == 1


# -- failures never raise ----------------------------------------------------------

def test_network_error_never_raises_and_never_logs_the_token(caplog):
    kill()
    err = ConnectionError('HTTPSConnectionPool: Max retries for url '
                          '/bot%s/getUpdates' % TOKEN)
    with caplog.at_level(logging.DEBUG):
        out, sent = run(FakeHttp(raise_with=err))
    assert out == [] and sent == []
    assert TOKEN not in caplog.text and 'getUpdates failed' in caplog.text
    assert nh.load('live')['killed'] and not tg.offset_path().exists()


def test_non_200_and_bad_json_never_raise(caplog):
    kill()
    out, _ = run(FakeHttp(resp=Resp({'ok': False, 'description': 'Conflict: %s' % TOKEN},
                                    status=409)))
    assert out == [] and TOKEN not in caplog.text

    class Bad:
        status_code = 200

        def json(self):
            raise ValueError('not json')
    out, _ = run(FakeHttp(resp=Bad()))
    assert out == []
    out, _ = run(FakeHttp(resp=Resp({'ok': True, 'result': [{'x': 1}, 'junk']})))
    assert out == []


def test_missing_telegram_config_no_network(monkeypatch, tmp_path):
    kill()
    monkeypatch.setattr(cfg, 'TELEGRAM_CONFIG', tmp_path / 'nope.json')
    h = FakeHttp([upd('/nresume X')])
    out, _ = run(h)
    assert out == [] and h.calls == []


def test_send_failure_does_not_raise():
    code = kill()

    def bad_send(text):
        raise RuntimeError('telegram down')
    out = tg.poll_resume(T0, bad_send, http=FakeHttp([upd('/nresume %s' % code)]))
    assert out[0]['result'] == 'cleared' and not nh.load('live')['killed']


def test_resume_raising_unexpectedly_is_contained(monkeypatch):
    kill()

    def boom(*a, **k):
        raise RuntimeError('lock timeout')
    monkeypatch.setattr(nh, 'resume', boom)
    out, sent = run(FakeHttp([upd('/nresume ABCDEF')]))
    assert out[0]['result'] == 'error' and 'FAILED' in sent[0]


def test_stale_offset_is_dropped_so_a_reset_id_sequence_is_still_read():
    """Reads a file written by the code under test.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    # Telegram picks a RANDOM next update_id after a quiet week; an old high
    # offset must not hide a fresh, lower-numbered /nresume.
    code = kill()
    tg.offset_path().write_text(json.dumps(
        {'offset': 10**9, 'saved_at': '2026-11-20 11:00:00'}), encoding='utf-8')
    h = FakeHttp([upd('/nresume %s' % code, uid=42)])
    out, _ = run(h)
    assert 'offset' not in h.calls[0]['params'] and out[0]['result'] == 'cleared'
    assert json.loads(tg.offset_path().read_text())['offset'] == 43


def test_no_allowed_updates_param():
    kill()
    h = FakeHttp([])
    run(h)
    assert 'allowed_updates' not in h.calls[0]['params']


def test_a_group_member_who_is_not_the_owner_cannot_resume():
    """Review R10: the owner check is on the SENDER as well as the chat."""
    code = kill()
    out, sent = run(FakeHttp([upd('/nresume %s' % code, sender=999999)]))
    assert nh.load('live')['killed'] is True and out == []
