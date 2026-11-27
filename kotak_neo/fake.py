"""FakeNeo: a hostile fake of NEO's HTTP API, for every bot's tests.

It sits where `requests.Session` sits (`NeoClient(http=FakeNeo())`), so the
REAL client code -- envelopes, error classes, tags -- is what gets tested.
Reply SHAPES are the ones measured on 2026-11-27 (`errMsg`, the 5203 empty
report, cancel `{result, stat Ok, stCode 200}`, the 1018 text-in-stat 400).

Matching: a BUY limit at/above the ask fills at the ask; a SELL at/below the
bid fills at the bid; otherwise it rests until `set_quote` crosses it. An SL
sell rests as `trigger pending` until the bid falls to its trigger, then works
as a limit at its price -- so a GAP through the limit leaves it unfilled,
exactly the risk the monitored stop exists for.

Scriptable faults (each applies to the NEXT place unless noted):
  reject(code, msg)          definite refusal (HTTP 400, documented stCode)
  rms_reject(reason)         accepted with an order number, then `rejected`
  unclear(placed)            HTTP 401 on place; `placed` decides if it really went in
  timeout(placed)            requests.Timeout; same
  partial(n)                 only n shares fill now; the rest rests
  hide(reads)                the order is invisible for `reads` book reads
  dead_reads(n)              the next n READS answer 401 (session dead)
  slow_cancel(reads)         a cancel answers success but the order shows
                             `cancel pending` for `reads` book reads (and can
                             still FILL meanwhile if the book crosses)

It ALLOWS shorting on purpose: an over-sell must show up as a negative
position in a test, not be silently refused by the fake.
"""
from __future__ import annotations

import itertools
import json
from typing import Any, Dict, List, Optional


class _Resp:
    def __init__(self, code: int, body: Any):
        self.status_code = code
        self._b = body
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._b is None:
            raise ValueError('no json')
        return self._b


EMPTY = {'stat': 'Not_Ok', 'stCode': 5203, 'errMsg': 'No Data', 'desc': 'data not found'}


class FakeNeo:
    def __init__(self):
        self.quotes: Dict[str, Dict[str, float]] = {}
        self.orders: List[Dict[str, Any]] = []
        self.calls: List[tuple] = []
        self._no = itertools.count(261127000500000)
        self._next: List[tuple] = []
        self._dead_reads = 0
        self._slow_cancel = 0
        self.funds = 50000.0

    # -- scripting ---------------------------------------------------------------
    def set_quote(self, symbol: str, bid: float, ask: float) -> None:
        self.quotes[symbol] = {'bid': bid, 'ask': ask}
        for o in self.orders:
            self._match(o)

    def reject(self, code: int = 1004, msg: str = 'Insufficient balance.') -> None:
        self._next.append(('reject', code, msg))

    def rms_reject(self, reason: str = 'RMS:Blocked') -> None:
        self._next.append(('rms', reason))

    def unclear(self, placed: bool) -> None:
        self._next.append(('401', placed))

    def timeout(self, placed: bool) -> None:
        self._next.append(('timeout', placed))

    def partial(self, n: int) -> None:
        self._next.append(('partial', n))

    def hide(self, reads: int) -> None:
        self._next.append(('hide', reads))

    def dead_reads(self, n: int) -> None:
        self._dead_reads = n

    def slow_cancel(self, reads: int) -> None:
        self._slow_cancel = reads

    def net(self, symbol: str) -> int:
        return sum(int(o['fldQty']) * (1 if o['trnsTp'] == 'B' else -1)
                   for o in self.orders if o['trdSym'] == symbol)

    def placed(self, side: Optional[str] = None) -> List[Dict[str, Any]]:
        return [o for o in self.orders if side is None or o['trnsTp'] == side]

    # -- the exchange ------------------------------------------------------------
    def _match(self, o: Dict[str, Any]) -> None:
        if o['ordSt'] not in ('open', 'trigger pending', 'cancel pending'):
            return
        q = self.quotes.get(o['trdSym'])
        if not q:
            return
        if o['ordSt'] == 'trigger pending':
            if q['bid'] > float(o['trgPrc']):
                return
            o['ordSt'] = 'open'
        px, want = float(o['prc']), int(o['qty']) - int(o['fldQty'])
        cap = o.pop('_cap', None)
        n = want if cap is None else min(cap, want)
        if n <= 0:
            return
        if o['trnsTp'] == 'B' and px >= q['ask'] > 0:
            fill = q['ask']
        elif o['trnsTp'] == 'S' and 0 < px <= q['bid']:
            fill = q['bid']
        else:
            return
        done = int(o['fldQty'])
        o['avgPrc'] = '%.2f' % (((done * float(o['avgPrc'])) + n * fill) / (done + n))
        o['fldQty'] = str(done + n)
        if done + n >= int(o['qty']):
            o['ordSt'] = 'complete'

    # -- HTTP surface -------------------------------------------------------------
    def post(self, url: str, headers=None, params=None, data=None, timeout=None, **kw):
        self.calls.append(('POST', url, data))
        j = json.loads((data or {}).get('jData') or '{}') if isinstance(data, dict) else {}
        if url.endswith('/quick/order/rule/ms/place'):
            return self._place(j)
        if url.endswith('/quick/order/cancel'):
            return self._cancel(j)
        if url.endswith('/quick/user/limits'):
            if self._consume_dead():
                return _Resp(401, {'message': 'Invalid session'})
            return _Resp(200, {'stat': 'Ok', 'Net': '%.2f' % self.funds, 'stCode': 200})
        raise AssertionError('unscripted POST %s' % url)

    def get(self, url: str, headers=None, params=None, timeout=None, **kw):
        self.calls.append(('GET', url, None))
        if self._consume_dead():
            return _Resp(401, {'message': 'Invalid session'})
        if url.endswith('/quick/user/orders'):
            rows = []
            for o in self.orders:
                if o['ordSt'] == 'cancel pending':
                    o['_cp'] = o.get('_cp', 0) - 1
                    if o['_cp'] < 0:
                        o['ordSt'] = 'cancelled'
                if o.get('_hide', 0) > 0:
                    o['_hide'] -= 1
                    continue
                rows.append({k: v for k, v in o.items() if not k.startswith('_')})
            return _Resp(200, {'stat': 'Ok', 'data': rows}) if rows else _Resp(200, EMPTY)
        if url.endswith('/quick/user/trades'):
            # The live shape (measured 2026-11-27): one row per FILL, qty 0.
            rows = [{'nOrdNo': o['nOrdNo'], 'trdSym': o['trdSym'], 'trnsTp': o['trnsTp'],
                     'fldQty': int(o['fldQty']), 'qty': 0, 'avgPrc': o['avgPrc'],
                     'GuiOrdId': o['GuiOrdId'], 'rptTp': 'fill', 'prcTp': o['prcTp'],
                     'flDt': '27-Nov-2026', 'flTm': '13:45:19'}
                    for o in self.orders if int(o['fldQty']) > 0]
            return _Resp(200, {'stat': 'Ok', 'data': rows}) if rows else _Resp(200, EMPTY)
        if url.endswith('/quick/user/positions'):
            net: Dict[str, List[int]] = {}
            for o in self.orders:
                f = int(o['fldQty'])
                if f:
                    b = net.setdefault(o['trdSym'], [0, 0])
                    b[0 if o['trnsTp'] == 'B' else 1] += f
            rows = [{'trdSym': s, 'prod': 'NRML', 'flBuyQty': str(v[0]), 'flSellQty': str(v[1]),
                     'cfBuyQty': '0', 'cfSellQty': '0'} for s, v in net.items()]
            return _Resp(200, {'stat': 'Ok', 'data': rows}) if rows else _Resp(200, EMPTY)
        raise AssertionError('unscripted GET %s' % url)

    def _consume_dead(self) -> bool:
        if self._dead_reads > 0:
            self._dead_reads -= 1
            return True
        return False

    def _pop(self, kinds) -> Optional[tuple]:
        for i, f in enumerate(self._next):
            if f[0] in kinds:
                return self._next.pop(i)
        return None

    def _new_order(self, j: Dict[str, Any], rms: Optional[str] = None) -> Dict[str, Any]:
        o = {'nOrdNo': str(next(self._no)), 'ordSt': 'open' if j['pt'] == 'L' else 'trigger pending',
             'trdSym': j['ts'], 'trnsTp': j['tt'], 'qty': str(j['qt']), 'fldQty': '0',
             'avgPrc': '0.00', 'prc': j['pr'], 'trgPrc': j.get('tp', '0'), 'prcTp': j['pt'],
             'rejRsn': '--', 'GuiOrdId': j['ig'], 'ordDtTm': '27-Nov-2026 13:53:11'}
        h = self._pop(('hide',))
        if h:
            o['_hide'] = h[1]
        p = self._pop(('partial',))
        if p:
            o['_cap'] = p[1]
        self.orders.append(o)
        if rms is not None:
            o['ordSt'], o['rejRsn'] = 'rejected', rms      # RMS refuses AFTER accepting
        else:
            self._match(o)
        return o

    def _place(self, j: Dict[str, Any]):
        import requests
        f = self._pop(('reject', 'rms', '401', 'timeout'))
        if f and f[0] == 'reject':
            return _Resp(400, {'stCode': f[1], 'errMsg': f[2], 'stat': 'Not_Ok'})
        if f and f[0] in ('401', 'timeout'):
            if f[1]:
                self._new_order(j)
            if f[0] == 'timeout':
                raise requests.Timeout('read timed out')
            return _Resp(401, {'message': 'session rejected'})
        o = self._new_order(j, rms=f[1] if f and f[0] == 'rms' else None)
        return _Resp(200, {'nOrdNo': o['nOrdNo'], 'stat': 'Ok', 'stCode': 200})

    def _cancel(self, j: Dict[str, Any]):
        no = str(j.get('on'))
        o = next((x for x in self.orders if x['nOrdNo'] == no), None)
        if o is None:
            msg = 'please provide valid internal order number'
            return _Resp(400, {'stCode': 1018, 'errMsg': msg, 'stat': msg})
        if o['ordSt'] in ('complete', 'cancelled', 'rejected'):
            return _Resp(200, {'stat': 'Not_Ok', 'stCode': 1006, 'errMsg': 'already executed'})
        if self._slow_cancel:
            o['ordSt'], o['_cp'] = 'cancel pending', self._slow_cancel
        else:
            o['ordSt'] = 'cancelled'
        return _Resp(200, {'result': no, 'stat': 'Ok', 'stCode': 200})
