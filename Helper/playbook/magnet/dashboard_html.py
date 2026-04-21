"""HTML renderer for the magnet dashboard (stock-level view).

One unified table: one row per stock, merged across scanner, confidence
tracker, and spot 15M tracker. Shows signal date, signal spot, signal
option, today's spot + option, spot move %, option move %, target, SL,
what's tracked, and a verdict on whether the opportunity is still live.
"""

from __future__ import annotations

import html as _html
from datetime import datetime
from typing import Optional

from . import config as cfg


_OUT_PATH = cfg.LOG_DIR / 'magnet_dashboard.html'


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _fmt(x, dp=2, dash='-'):
    if x is None:
        return dash
    try:
        return f"{float(x):,.{dp}f}"
    except Exception:
        return dash


def _pnl_td(pct):
    if pct is None:
        return '<td class="num" data-sort="-99999">-</td>'
    cls = 'pnl-pos' if pct > 0 else ('pnl-neg' if pct < 0 else 'pnl-zero')
    return f'<td class="num {cls}" data-sort="{pct:.4f}">{pct:+.1f}%</td>'


def _num_td(val, dp=2):
    sort_val = val if val is not None else -99999
    return f'<td class="num" data-sort="{sort_val}">{_fmt(val, dp)}</td>'




# ---------------------------------------------------------------------------
#  Regime
# ---------------------------------------------------------------------------

def _render_regime(r: dict) -> str:
    def _flag(on):
        if on is True:
            return '<span class="flag flag-on">ON</span>'
        if on is False:
            return '<span class="flag flag-off">OFF</span>'
        return '<span class="flag flag-unk">?</span>'

    nifty = _fmt(r.get('nifty_ltp'), 1)
    chg = r.get('nifty_change_pct')
    chg_cls = ''
    chg_str = '-'
    if chg is not None:
        chg_cls = 'pnl-pos' if chg > 0 else ('pnl-neg' if chg < 0 else 'pnl-zero')
        chg_str = f"{chg:+.2f}%"
    rsi_val = r.get('rsi')
    rsi_str = f"{rsi_val:.1f}" if rsi_val is not None else '?'
    dma_val = r.get('close_50dma_pct')
    dma_str = f"{dma_val:+.2f}%" if dma_val is not None else '?'
    br_val = r.get('breadth')
    br_str = f"{br_val:.1f}%" if br_val is not None else '?'

    signals_on = sum(1 for x in (r.get('rsi_on'), r.get('dma_on'), r.get('breadth_on')) if x)
    known = sum(1 for x in (r.get('rsi_on'), r.get('dma_on'), r.get('breadth_on')) if x is not None)
    if known < 3:
        verdict = f"PARTIAL ({signals_on}/{known} ON)"
        verdict_cls = 'verdict-partial'
    elif signals_on == 0:
        verdict = "PAUSED - Active entries BLOCKED"
        verdict_cls = 'verdict-paused'
    elif signals_on >= 2:
        verdict = f"{signals_on}/3 ON (check 7d sustained)"
        verdict_cls = 'verdict-ok'
    else:
        verdict = f"{signals_on}/3 ON - insufficient"
        verdict_cls = 'verdict-partial'

    return f'''
    <section class="regime {verdict_cls}">
      <span class="r-label">REGIME</span>
      <span class="r-verdict">{_html.escape(verdict)}</span>
      <span class="r-sep">|</span>
      <span class="r-metric">NIFTY <b>{nifty}</b> <span class="{chg_cls}">{chg_str}</span></span>
      <span class="r-metric">RSI <b>{rsi_str}</b> {_flag(r.get('rsi_on'))}</span>
      <span class="r-metric">50DMA <b>{dma_str}</b> {_flag(r.get('dma_on'))}</span>
      <span class="r-metric">Breadth <b>{br_str}</b> {_flag(r.get('breadth_on'))}</span>
    </section>
    '''


# ---------------------------------------------------------------------------
#  Unified stocks table
# ---------------------------------------------------------------------------


_VERDICT_NOTE = {
    'LIVE':      'Still en route to target. Entry/add may still be valid.',
    'PAST TGT':  'Spot already reached target zone. Upside largely played out.',
    'STOPPED':   'Stop-loss breached. Invalid.',
    'CHASED':    'Option already +100%+. Chase risk high for new entry.',
    'STALE':     'Old signal with little spot movement. Probably dead.',
    'TP HIT':    'Closed - target hit.',
    'SL HIT':    'Closed - stop-loss hit.',
    'EXPIRED':   'Closed - option expired.',
    'EXITED':    'Closed - other exit reason.',
    'CANCELLED': 'Cancelled before entry.',
    'NO DATA':   'Missing price or target data.',
}

_VERDICT_CLS = {
    'LIVE':      'v-live',
    'PAST TGT':  'v-past',
    'STOPPED':   'v-stop',
    'CHASED':    'v-chased',
    'STALE':     'v-stale',
    'TP HIT':    'v-tp',
    'SL HIT':    'v-sl',
    'EXPIRED':   'v-stale',
    'EXITED':    'v-past',
    'CANCELLED': 'v-stale',
    'NO DATA':   'v-unk',
}


def _verdict_td(verdict: str):
    cls = _VERDICT_CLS.get(verdict, 'v-unk')
    return f'<td class="verdict {cls}">{_html.escape(verdict)}</td>'


def _render_stocks(records: list) -> str:
    open_recs = [r for r in records if r.get('row_status') == 'open']
    closed_recs = [r for r in records if r.get('row_status') == 'closed']

    total = len(records)
    entered = sum(1 for r in open_recs if r.get('status_hint') == 'ENTERED')
    live = sum(1 for r in open_recs if r.get('verdict') == 'LIVE')
    past = sum(1 for r in open_recs if r.get('verdict') == 'PAST TGT')
    tp_hits = sum(1 for r in closed_recs if r.get('verdict') == 'TP HIT')
    sl_hits = sum(1 for r in closed_recs if r.get('verdict') == 'SL HIT')

    header = f'''
    <section class="block"><div class="block-head">
      <div>
        <h2>SIGNALS <span class="sub">weekly + monthly only &middot; one row per stock (open) or per trade (closed)</span></h2>
        <div class="summary">
          <span><strong>{len(open_recs)}</strong> open</span>
          <span class="pnl-pos">{entered} entered</span>
          <span class="pnl-pos">{live} live</span>
          <span class="pnl-zero">{past} past tgt</span>
          <span class="sep">|</span>
          <span><strong>{len(closed_recs)}</strong> closed</span>
          <span class="pnl-pos">{tp_hits} TP</span>
          <span class="pnl-neg">{sl_hits} SL</span>
        </div>
      </div>
      <div class="filter-group">
        <button class="filter-btn active" data-filter="open">Open</button>
        <button class="filter-btn" data-filter="closed">Closed</button>
        <button class="filter-btn" data-filter="all">All</button>
      </div>
    </div>
    <details class="helper"><summary>How to read this</summary>
    <ul>
      <li><b>Dir CE</b> = expecting stock UP toward target. <b>PE</b> = expecting stock DOWN toward target.</li>
      <li><b>State</b>: <i>ENTERED</i> = option position is open. <i>WATCH</i> = signal fired, not entered yet. <i>CLOSED</i> = trade done.</li>
      <li><b>Days</b> = days since the ST-touch signal first fired (not since entry).</li>
      <li><b>SPOT group</b>: <i>Sig</i> = spot on signal day, <i>Now</i> = live spot (for open) or exit spot (for closed), <i>Target</i> = ST line to aim for, <i>SL</i> = stop-loss level, <i>MOV%</i> = Sig&rarr;Now move.</li>
      <li><b>OPTION group</b>: <i>Sig</i> = premium paid at entry (blank for watch-only), <i>Now</i> = live / exit premium, <i>MOV%</i> = realised/unrealised option P&amp;L.</li>
      <li><b>Verdict</b>: <span class="v-live">LIVE</span> = spot still heading to target (may add). <span class="v-past">PAST TGT</span> = target already hit, upside played. <span class="v-chased">CHASED</span> = option already +100%+. <span class="v-tp">TP HIT</span> / <span class="v-sl">SL HIT</span> = closed outcomes.</li>
      <li>Columns sort on click; arrow shows direction.</li>
    </ul>
    </details>
    '''
    html = [header]

    if total == 0:
        html.append('<p class="empty">No signals found.</p></section>')
        return ''.join(html)

    html.append('<table class="sortable unified"><thead>'
                '<tr class="group-row">'
                '<th rowspan="2">Stock</th><th rowspan="2">Dir</th>'
                '<th rowspan="2">TF</th><th rowspan="2">State</th>'
                '<th rowspan="2">Signal date</th><th rowspan="2">Days</th>'
                '<th colspan="5" class="grp grp-spot">SPOT</th>'
                '<th colspan="3" class="grp grp-opt">OPTION</th>'
                '<th rowspan="2">Verdict</th>'
                '<th rowspan="2">Exit date</th><th rowspan="2">Exit reason</th>'
                '<th rowspan="2">Option symbol</th>'
                '<th rowspan="2">Tracked by (notes)</th>'
                '</tr>'
                '<tr>'
                '<th>Sig</th><th>Now/Exit</th><th>Target</th><th>SL</th><th>&Delta;%</th>'
                '<th>Sig</th><th>Now/Exit</th><th>&Delta;%</th>'
                '</tr>'
                '</thead><tbody>')

    for r in (open_recs + closed_recs):
        sym = r['symbol']
        dir_ = r.get('direction') or '-'
        tf = r.get('timeframe') or '-'
        state = r.get('status_hint') or ''
        sig_date = r.get('signal_date') or '-'
        days = r.get('days_since_signal')
        days_str = f"{days}" if days is not None else '-'
        verdict = r.get('verdict', '-')
        note = _VERDICT_NOTE.get(verdict, '')
        trackers = ' &middot; '.join(_html.escape(t) for t in r.get('trackers', []))
        opt_sym = r.get('option_symbol') or ''
        exit_date = r.get('exit_date') or ''
        exit_reason = r.get('exit_reason') or ''
        row_status = r.get('row_status', 'open')

        html.append(f'<tr data-row-status="{row_status}">')
        html.append(f'<td class="sym">{_html.escape(sym)}</td>')
        html.append(f'<td class="dir-{dir_}">{_html.escape(dir_)}</td>')
        html.append(f'<td>{_html.escape(tf)}</td>')
        html.append(f'<td class="state">{_html.escape(state)}</td>')
        html.append(f'<td class="date">{_html.escape(sig_date)}</td>')
        html.append(f'<td data-sort="{days or 0}">{days_str}</td>')
        # SPOT group: sig | now | target | sl | Δ%
        html.append(_num_td(r.get('signal_spot')))
        html.append(_num_td(r.get('spot_now')))
        html.append(_num_td(r.get('target')))
        html.append(_num_td(r.get('sl')))
        html.append(_pnl_td(r.get('spot_move_pct')))
        # OPTION group: sig | now | Δ%
        html.append(_num_td(r.get('option_entry')))
        html.append(_num_td(r.get('option_now')))
        html.append(_pnl_td(r.get('option_move_pct')))
        html.append(_verdict_td(verdict))
        html.append(f'<td class="date">{_html.escape(exit_date)}</td>')
        html.append(f'<td class="exit-reason">{_html.escape(exit_reason)}</td>')
        html.append(f'<td class="opt" title="{_html.escape(note)}">{_html.escape(opt_sym)}</td>')
        html.append(f'<td class="trackers">{trackers}</td>')
        html.append('</tr>')

    html.append('</tbody></table>')

    # Verdict legend
    html.append('<div class="legend"><strong>Verdict legend:</strong> '
                + ' &middot; '.join(f'<span class="{_VERDICT_CLS.get(v, "v-unk")}">{v}</span> {_html.escape(n)}'
                                    for v, n in _VERDICT_NOTE.items())
                + '</div>')
    html.append('</section>')
    return ''.join(html)


# ---------------------------------------------------------------------------
#  CSS + JS
# ---------------------------------------------------------------------------

_CSS = r"""
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  margin: 0; padding: 24px;
  background: #0f1419; color: #e6e9ef; font-size: 13px;
}
h1 { font-size: 24px; margin: 0; color: #ffffff; letter-spacing: 1px; }
h2 { font-size: 16px; margin: 20px 0 8px; color: #89d4ff; letter-spacing: 0.5px; }
h2 .sub { font-size: 12px; color: #8a95a5; font-weight: normal; margin-left: 12px; letter-spacing: normal; }

.topbar {
  display: flex; justify-content: space-between; align-items: center;
  border-bottom: 1px solid #2a3442; padding-bottom: 14px; margin-bottom: 18px;
}
.ts { color: #8a95a5; font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }

.regime {
  display: flex; flex-wrap: wrap; align-items: center; gap: 14px;
  border: 1px solid #2a3442; border-radius: 6px; padding: 8px 14px;
  margin-bottom: 14px; background: #161c24;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12px;
}
.regime.verdict-ok      { border-left: 3px solid #2dce77; }
.regime.verdict-paused  { border-left: 3px solid #ef4444; }
.regime.verdict-partial { border-left: 3px solid #f59e0b; }
.r-label { color: #89d4ff; font-weight: 700; letter-spacing: 0.5px; font-size: 11px; }
.r-verdict { color: #e6e9ef; font-weight: 600; }
.regime.verdict-ok .r-verdict      { color: #86efac; }
.regime.verdict-paused .r-verdict  { color: #fca5a5; }
.regime.verdict-partial .r-verdict { color: #fcd34d; }
.r-sep { color: #2a3442; }
.r-metric { color: #a0aec0; font-size: 12px; }
.r-metric b { color: #e6e9ef; font-weight: 600; margin: 0 2px; }

.flag {
  display: inline-block; padding: 1px 7px; border-radius: 3px;
  font-size: 10px; font-weight: 600; margin-left: 4px; letter-spacing: 0.5px;
}
.flag-on  { background: #14532d; color: #86efac; }
.flag-off { background: #450a0a; color: #fca5a5; }
.flag-unk { background: #374151; color: #9ca3af; }

.block-head {
  display: flex; justify-content: space-between; align-items: flex-end;
  margin-bottom: 10px;
}
.summary {
  display: flex; gap: 14px; align-items: center;
  color: #b0b8c4; font-size: 12px; margin-top: 4px;
}
.summary span strong { color: #e6e9ef; font-size: 14px; }
.summary .sep { color: #2a3442; }

.filter-group {
  display: inline-flex; background: #161c24; border: 1px solid #2a3442;
  border-radius: 6px; overflow: hidden;
}
.filter-btn {
  background: transparent; color: #8a95a5; border: none;
  padding: 6px 14px; font-size: 12px; font-weight: 600;
  cursor: pointer; font-family: inherit; letter-spacing: 0.3px;
  border-right: 1px solid #2a3442;
}
.filter-btn:last-child { border-right: none; }
.filter-btn:hover { background: #1d2530; color: #e6e9ef; }
.filter-btn.active { background: #223041; color: #89d4ff; }

.helper {
  margin: 0 0 10px; background: #161c24; border: 1px solid #2a3442;
  border-radius: 4px; padding: 8px 12px; font-size: 12px;
}
.helper summary { color: #89d4ff; cursor: pointer; user-select: none; font-weight: 600; }
.helper summary:hover { color: #e6e9ef; }
.helper ul { margin: 8px 0 4px; padding-left: 20px; color: #b0b8c4; line-height: 1.7; }
.helper li { margin-bottom: 3px; }
.helper b { color: #e6e9ef; }
.helper i { color: #a0aec0; font-style: normal; font-weight: 500; }

/* Filter visibility */
body[data-filter="open"] tr[data-row-status="closed"] { display: none; }
body[data-filter="closed"] tr[data-row-status="open"] { display: none; }
tr[data-row-status="closed"] { background: #141920; }
tr[data-row-status="closed"] td.sym { color: #8a95a5; }

/* Column-group headers */
th.grp {
  text-align: center; padding: 4px 8px; font-size: 11px;
  letter-spacing: 1px; cursor: default; font-weight: 700;
  border-bottom: 1px solid #2a3442;
}
th.grp:hover { background: inherit; }
th.grp-spot { background: #1f2a3a; color: #89d4ff; }
th.grp-opt  { background: #2f1f3a; color: #d8b4fe; }
/* Under each group's cells */
table.unified thead tr:nth-child(2) th { font-size: 10px; padding: 4px 8px; color: #a0aec0; }
/* Cells under SPOT group (columns 7-11) and OPTION group (columns 12-14) */
table.unified tbody td:nth-child(7),
table.unified tbody td:nth-child(8),
table.unified tbody td:nth-child(9),
table.unified tbody td:nth-child(10),
table.unified tbody td:nth-child(11) { background: rgba(31, 42, 58, 0.25); }
table.unified tbody td:nth-child(12),
table.unified tbody td:nth-child(13),
table.unified tbody td:nth-child(14) { background: rgba(47, 31, 58, 0.25); }
tr[data-row-status="closed"] td:nth-child(7),
tr[data-row-status="closed"] td:nth-child(8),
tr[data-row-status="closed"] td:nth-child(9),
tr[data-row-status="closed"] td:nth-child(10),
tr[data-row-status="closed"] td:nth-child(11) { background: rgba(31, 42, 58, 0.45); }
tr[data-row-status="closed"] td:nth-child(12),
tr[data-row-status="closed"] td:nth-child(13),
tr[data-row-status="closed"] td:nth-child(14) { background: rgba(47, 31, 58, 0.45); }

.block { margin-bottom: 18px; }

table {
  width: 100%; border-collapse: collapse; background: #161c24;
  border: 1px solid #2a3442; border-radius: 6px; overflow: hidden;
  font-family: 'SF Mono', Consolas, monospace; font-size: 12px;
}
thead { background: #1d2530; }
th {
  text-align: left; padding: 8px 10px; color: #89d4ff;
  font-weight: 600; font-size: 11px; letter-spacing: 0.5px;
  cursor: pointer; user-select: none; border-bottom: 1px solid #2a3442;
}
th:hover { background: #223041; }
th.sort-asc::after  { content: ' ▲'; font-size: 9px; color: #89d4ff; }
th.sort-desc::after { content: ' ▼'; font-size: 9px; color: #89d4ff; }

td { padding: 6px 10px; border-bottom: 1px solid #1d2530; white-space: nowrap; }
tbody tr:hover { background: #1a222e; }
tbody tr:last-child td { border-bottom: none; }

td.num { text-align: right; }
td.sym { font-weight: 600; color: #e6e9ef; }
td.date { color: #b0b8c4; font-size: 11px; }
td.opt { color: #a0aec0; font-size: 11px; }
td.trackers { color: #8a95a5; font-size: 11px; }

.pnl-pos { color: #2dce77; }
.pnl-neg { color: #ef4444; }
.pnl-zero { color: #8a95a5; }

.dir-CE { color: #2dce77; font-weight: 600; }
.dir-PE { color: #ef4444; font-weight: 600; }

.verdict { font-weight: 600; font-size: 11px; letter-spacing: 0.5px; text-align: center; }
.v-live    { color: #2dce77; }
.v-past    { color: #89d4ff; }
.v-stop    { color: #ef4444; }
.v-chased  { color: #f59e0b; }
.v-stale   { color: #8a95a5; }
.v-tp      { color: #2dce77; font-weight: 700; }
.v-sl      { color: #ef4444; font-weight: 700; }
.v-unk     { color: #8a95a5; }
td.exit-reason { color: #b0b8c4; font-size: 11px; }

.legend {
  margin-top: 10px; padding: 10px 14px; background: #161c24;
  border: 1px solid #2a3442; border-radius: 4px; font-size: 11px;
  color: #8a95a5; line-height: 1.8;
}
.legend strong { color: #e6e9ef; }
.legend span { font-weight: 600; margin-right: 4px; }

.empty { color: #8a95a5; font-style: italic; padding: 8px 0; }
"""

_JS = r"""
// Filter buttons (Open / Closed / All)
document.body.dataset.filter = 'open';
document.querySelectorAll('.filter-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.body.dataset.filter = btn.dataset.filter;
    document.querySelectorAll('.filter-btn').forEach(function(b) {
      b.classList.toggle('active', b === btn);
    });
  });
});

// Sortable tables
// Skip group-header ths (.grp). Map each LEAF th to its data column index.
document.querySelectorAll('table.sortable').forEach(function(table) {
  var allTh = table.querySelectorAll('thead th');
  var leafHeaders = Array.prototype.filter.call(allTh, function(h) {
    return !h.classList.contains('grp');
  });
  leafHeaders.forEach(function(th, colIdx) {
    th.style.cursor = 'pointer';
    th.addEventListener('click', function() {
      var tbody = table.tBodies[0];
      var rows = Array.prototype.slice.call(tbody.rows);
      var asc = !th.classList.contains('sort-asc');
      leafHeaders.forEach(function(h) { h.classList.remove('sort-asc', 'sort-desc'); });
      th.classList.add(asc ? 'sort-asc' : 'sort-desc');
      rows.sort(function(a, b) {
        var aCell = a.cells[colIdx];
        var bCell = b.cells[colIdx];
        if (!aCell || !bCell) return 0;
        var aVal = aCell.getAttribute('data-sort');
        var bVal = bCell.getAttribute('data-sort');
        if (aVal === null) aVal = aCell.textContent.trim();
        if (bVal === null) bVal = bCell.textContent.trim();
        var aNum = parseFloat(aVal);
        var bNum = parseFloat(bVal);
        if (!isNaN(aNum) && !isNaN(bNum)) {
          return asc ? aNum - bNum : bNum - aNum;
        }
        return asc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      });
      rows.forEach(function(row) { tbody.appendChild(row); });
    });
  });
});
"""


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------

def render_html(regime: dict, records: list,
                generated_at: Optional[datetime] = None) -> str:
    now = generated_at or datetime.now()
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Magnet Dashboard</title>
  <style>{_CSS}</style>
</head>
<body>
  <div class="topbar">
    <h1>MAGNET DASHBOARD</h1>
    <div class="ts">Generated {now.strftime('%Y-%m-%d %H:%M:%S')}</div>
  </div>
  {_render_regime(regime)}
  {_render_stocks(records)}
  <script>{_JS}</script>
</body>
</html>
'''


def write_html(regime: dict, records: list) -> str:
    """Write HTML file and return its absolute path."""
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    page = render_html(regime, records)
    _OUT_PATH.write_text(page, encoding='utf-8')
    return str(_OUT_PATH.resolve())
