"""What has to be true before a live process may run, may ENTER, or may PROBE.

Three different questions, deliberately separate (review NEO-07):

  RUN     `execution` is `live` or `rehearsal`, the credentials file exists,
          `lots` <= MAX_LIVE_LOTS. Without these the process does not start.
  ENTER   strategy entries need ALL of:
            * `state_live/ARM`: first line a date YYYY-MM-DD, today <= date,
              and the date at most `arm_max_days` ahead;
            * the pass mark: `compare.gate` reads PASS on `neo.volrix_csv`
              against the PAPER book. No CSV, an unreadable CSV, IN PROGRESS
              or FAIL all mean NOT ARMED (fail closed).
          The file is re-read every minute by the running engine, so removing
          it disarms a running process (NEO-18). Unarmed = exits, reconcile
          and alerts still run; nothing new opens.
  PROBE   the Stage-3 order probe needs `state_live/PROBE_ARM` holding TODAY's
          date exactly. It can never enable strategy entries, and the strategy
          ARM can never enable the probe.

Why dates and not flags: a flag left on is on forever; a date expires on its
own. Why the arm file is separate from the config: the config says WHAT the
process is (live), the arm file says the owner said GO, and the two cannot be
conflated by one overlay edit.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import compare
from .config import Config
from .live_config import MAX_LIVE_LOTS, NeoConfig, rule_diffs

ARM_FILE = 'ARM'
PROBE_ARM_FILE = 'PROBE_ARM'


@dataclass
class ArmReport:
    may_run: bool
    armed_until: Optional[date]
    gate_verdict: str
    problems: List[str] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    #: rule values where LIVE differs from the PAPER twin (R4-04): information,
    #: because it breaks pairing; it blocks nothing
    rule_drift: List[str] = field(default_factory=list)

    @property
    def armed(self) -> bool:
        return self.armed_until is not None

    def text(self) -> str:
        return '\n'.join(self.lines)


def _first_date(path: Path) -> Tuple[Optional[date], str]:
    if not path.exists():
        return None, 'missing'
    try:
        raw = path.read_text(encoding='utf-8').strip().splitlines()
        first = raw[0].strip() if raw else ''
        return date.fromisoformat(first[:10]), 'ok'
    except (OSError, ValueError, IndexError):
        return None, 'unreadable'


def read_arm(state_dir: Path, today: date, max_days: int = 35) -> Optional[date]:
    """The armed-through date, or None. Past, missing, garbage, or further
    than `max_days` ahead all read as NOT armed. Never raises."""
    until, _ = _first_date(Path(state_dir) / ARM_FILE)
    if until is None or today > until or until > today + timedelta(days=max_days):
        return None
    return until


def read_probe_arm(state_dir: Path, today: date) -> bool:
    """PROBE_ARM must name today exactly: a probe is a one-day decision."""
    d, _ = _first_date(Path(state_dir) / PROBE_ARM_FILE)
    return d == today


def gate_verdict(neo: NeoConfig, paper_positions: List[Dict[str, Any]]) -> Tuple[str, str]:
    """(verdict, line). Anything but a readable CSV with a PASS is not PASS."""
    if not neo.volrix_csv:
        return 'NOT EVALUATED', 'PASS MARK: not evaluated (neo.volrix_csv not set) -> entries blocked'
    if not neo.volrix_csv.exists():
        return 'NOT EVALUATED', 'PASS MARK: not evaluated (%s missing) -> entries blocked' % neo.volrix_csv
    try:
        g = compare.gate(paper_positions, compare.load_volrix(neo.volrix_csv))
    except Exception as exc:                        # noqa: BLE001 - any CSV defect fails closed
        return 'ERROR', 'PASS MARK: not evaluated (%s: %s) -> entries blocked' % (
            type(exc).__name__, exc)
    return str(g['verdict']), compare.render_gate(g).splitlines()[0]


def read_paper_positions(paper_book: Optional[Path]) -> List[Dict[str, Any]]:
    """The paper book's positions, read-only and optional: a paper problem
    must never stop the live process (NEO-09)."""
    if not paper_book or not paper_book.exists():
        return []
    try:
        with open(paper_book, encoding='utf-8') as f:
            data = json.load(f)
        pos = data.get('positions') if isinstance(data, dict) else None
        return [p for p in pos if isinstance(p, dict)] if isinstance(pos, list) else []
    except (OSError, ValueError):
        return []


def check(cfg: Config, execution: str, neo: NeoConfig, state_dir: Path, today: date,
          paper_positions: List[Dict[str, Any]], paper_cfg: Optional[Config] = None) -> ArmReport:
    rep = ArmReport(may_run=True, armed_until=None, gate_verdict='NOT EVALUATED')
    rep.lines.append('EXECUTION: %s' % execution.upper())
    if execution == 'paper':
        rep.problems.append('execution is "paper": the live runner refuses to start')
    if not neo.credentials_file.exists():
        rep.problems.append('credentials file missing: %s' % neo.credentials_file)
    if cfg.lots > MAX_LIVE_LOTS:
        rep.problems.append('lots=%d exceeds MAX_LIVE_LOTS=%d (code, not config)'
                            % (cfg.lots, MAX_LIVE_LOTS))
    rep.may_run = not rep.problems
    if paper_cfg is not None:
        try:
            rep.rule_drift = rule_diffs(cfg, paper_cfg)
        except Exception as exc:                      # noqa: BLE001 - never stop a run over this
            rep.rule_drift = ['rule check failed: %s' % exc]
        if rep.rule_drift:
            rep.lines.append('RULES DIFFER FROM PAPER (pairing broken, information only): %s'
                             % '; '.join(rep.rule_drift))
    if neo.unknown_keys:
        rep.lines.append('UNKNOWN neo keys ignored: %s' % ', '.join(neo.unknown_keys))
    verdict, gate_line = gate_verdict(neo, paper_positions)
    rep.gate_verdict = verdict
    rep.lines.append(gate_line)
    if execution == 'rehearsal':
        rep.armed_until = today          # entries go to the fake; nothing real
        rep.lines.append('ARMED: rehearsal — orders go to the in-process fake broker '
                         '(a pass-mark block above applies to real orders only)')
    else:
        until = read_arm(state_dir, today, neo.arm_max_days)
        if until and verdict == 'PASS':
            rep.armed_until = until
            rep.lines.append('ARMED for entries through %s (%s) and the pass mark reads PASS'
                             % (until, Path(state_dir) / ARM_FILE))
        elif until:
            rep.lines.append('NOT ARMED: ARM is set through %s but the pass mark reads %s, not '
                             'PASS. No strategy entries. Exits, reconcile and alerts still run.'
                             % (until, verdict))
        else:
            rep.lines.append('NOT ARMED: no valid ARM date (write YYYY-MM-DD, at most %d days '
                             'ahead, to %s). Exits, reconcile and alerts still run.'
                             % (neo.arm_max_days, Path(state_dir) / ARM_FILE))
        rep.lines.append('PROBE_ARM: %s' % ('set for today' if read_probe_arm(state_dir, today)
                                             else 'not set (the order probe refuses)'))
    rep.lines.append('rule: stop %.0f%% of fill, exit %s, trigger x%.2f, entries %s-%s, close %s | '
                     'limits: lots %d (max %d), premium <= Rs %.0f, kill Rs %.0f from peak, '
                     'entry cap +%.1f%%, orders/day %d (entries %d), unknown grace %.0fs, '
                     'qty unit %s, holdings source %s, live telegram %s'
                     % (cfg.stop_frac * 100, cfg.next_day_exit, cfg.trigger_mult, cfg.entry_start,
                        cfg.entry_end, cfg.market_close, cfg.lots, MAX_LIVE_LOTS,
                        neo.max_premium_rs, neo.kill_drawdown_rs, neo.entry_cap_pct,
                        neo.max_orders_per_day, neo.max_entry_orders_per_day,
                        neo.unknown_grace_sec, neo.position_qty_unit,
                        'on' if neo.holdings_source_enabled else 'off',
                        'on' if neo.telegram_enabled else 'OFF'))
    for pr in rep.problems:
        rep.lines.append('REFUSED: ' + pr)
    return rep


def make_arm_reader(neo: NeoConfig, state_dir: Path, paper_book: Optional[Path]
                    ) -> Callable[[date], Optional[date]]:
    """What the running engine calls (throttled to once a minute): the ARM
    date AND the pass mark, both re-read, so the owner can disarm by
    deleting the file and a window that stops reading PASS stops entries."""
    def read(today: date) -> Optional[date]:
        until = read_arm(state_dir, today, neo.arm_max_days)
        if until is None:
            return None
        verdict, _ = gate_verdict(neo, read_paper_positions(paper_book))
        return until if verdict == 'PASS' else None
    return read


