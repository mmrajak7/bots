"""Configuration: tracked defaults + untracked overlay (overlay wins), and the folder layout.

Build spec §A. Every number used by the engine lives in config/momentum.defaults.json; nothing is hard-coded in code.
The overlay config/momentum.json (git-ignored) holds per-box settings such as mode=live.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(os.environ.get("MOMENTUM_HOME", str(Path(__file__).resolve().parents[2])))

REQUIRED_KEYS = (
    "mode", "account_user_id", "book_cash_open", "n_hold", "charges_reserve", "buy_ltp_mult", "buy_cap_mult",
    "ladder", "cancel_at", "last_place_at", "lock_tries", "vet_timeout_s", "kite_rps", "kite_retry_s",
    "token_poll_s", "token_first_poll", "token_deadline", "plan_final", "audit_ret", "audit_factor", "audit_kite_pp",
    "price_freeze", "tg_retries", "tg_retry_s", "min_free_gb", "rms_max", "defer_max", "token_file",
    "holiday_file", "nse_data", "claude_bin", "claude_version", "units_open", "nav_open",
    "step_plan_at", "place_at", "reconcile_at", "token_alert_at", "ladder_grace_min", "band_max",
    "arm_cap_rupees_default", "charges_rates", "srs_dir", "audit_top_n", "sell_rank", "backup_dir",
    "probe_reads", "probe_wait_s",
    "fo_scan_seed", "fo_rss_url", "fo_circular_base", "fo_scan_settle_days", "fo_scan_pause_s", "fo_vet_timeout_s",
    "fo_vet_max_circulars", "fo_vet_max_entrants", "fo_lookback_days", "fo_vet_give_up", "fo_vet_fail_action",
    "fo_min_stock_futures",
)
TIME_KEYS = ("step_plan_at", "place_at", "last_place_at", "cancel_at", "reconcile_at", "token_first_poll",
             "token_alert_at", "token_deadline", "plan_final")
CHARGE_KEYS = ("brokerage", "stt_buy", "exchange_txn", "sebi", "stamp_buy", "gst_on")
# Hard ceiling on the buy price multipliers: a config typo (1.10 for 1.010) must never become a 10% chase.
MAX_BUY_MULT = Decimal("1.03")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Paths:
    home: Path
    state: Path

    @property
    def control(self) -> Path:
        return self.home / "control"

    @property
    def arm_file(self) -> Path:
        return self.control / "ARM.json"

    @property
    def kill_file(self) -> Path:
        return self.control / "KILL"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets"

    @property
    def journal(self) -> Path:
        return self.state / "journal.jsonl"

    @property
    def ledger(self) -> Path:
        return self.state / "ledger.json"

    @property
    def inbox(self) -> Path:
        return self.state / "inbox"

    @property
    def decisions(self) -> Path:
        return self.state / "decisions"

    @property
    def bands(self) -> Path:
        return self.state / "bands"

    @property
    def audit(self) -> Path:
        return self.state / "audit"

    @property
    def vet(self) -> Path:
        return self.state / "vet"

    @property
    def locks(self) -> Path:
        return self.state / "locks"

    @property
    def reports(self) -> Path:
        return self.state / "reports"

    @property
    def snapshots(self) -> Path:
        return self.state / "snapshots"

    @property
    def sessions(self) -> Path:
        return self.state / "sessions.json"

    @property
    def refresh(self) -> Path:
        return self.state / "refresh"

    @property
    def derived(self) -> Path:
        """Rebuilt pipeline outputs (panel, store, band files) for the live path."""
        return self.state / "derived"

    @property
    def fo_vet(self) -> Path:
        """Write-once F&O vet results per (month, signal day) (frozen design D47)."""
        return self.state / "fo_vet"

    @property
    def fo_circulars(self) -> Path:
        """NSE F&O circular store + scan state. Shared by the dry and live books (public NSE files, a cache)."""
        return self.home / "cache" / "fo_circulars"

    def ensure(self) -> None:
        for d in (self.control, self.state, self.inbox, self.decisions, self.bands, self.audit, self.vet,
                  self.locks, self.reports, self.snapshots, self.refresh, self.derived, self.fo_vet,
                  self.fo_circulars):
            d.mkdir(parents=True, exist_ok=True)


def _read(p: Path) -> Dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: invalid JSON: {e}") from e


def load(home: Optional[Path] = None, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    h = home or HOME
    cfg = _read(h / "config" / "momentum.defaults.json")
    cfg.update(_read(h / "config" / "momentum.json"))
    if overrides:
        cfg.update(overrides)
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        raise ConfigError(f"config keys missing: {missing}")
    if cfg["mode"] not in ("dry", "live"):
        raise ConfigError(f"mode must be dry|live, got {cfg['mode']!r}")
    for k in ("book_cash_open", "n_hold", "lock_tries", "rms_max", "defer_max", "units_open"):
        if not isinstance(cfg[k], int) or cfg[k] <= 0:
            raise ConfigError(f"{k} must be a positive int")
    for k in ("band_max", "probe_reads", "probe_wait_s", "ladder_grace_min", "arm_cap_rupees_default", "vet_timeout_s", "kite_rps", "tg_retries",
              "audit_top_n"):
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] <= 0:
            raise ConfigError(f"{k} must be a positive int")
    for k in ("fo_scan_seed", "fo_scan_settle_days", "fo_vet_timeout_s", "fo_vet_max_circulars", "fo_vet_max_entrants",
              "fo_lookback_days", "fo_min_stock_futures"):
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] <= 0:
            raise ConfigError(f"{k} must be a positive int")
    if not _is_hhmm(cfg["fo_vet_give_up"]) or not cfg["fo_vet_give_up"] < cfg["plan_final"]:
        raise ConfigError("fo_vet_give_up must be HH:MM before plan_final")
    if cfg["fo_vet_fail_action"] not in ("final_alert", "approve"):
        raise ConfigError("fo_vet_fail_action must be final_alert|approve")
    ladder: List[str] = cfg["ladder"]
    if not isinstance(ladder, list) or not ladder:
        raise ConfigError("ladder times must be a non-empty ascending list")
    for k, v in [(k, cfg[k]) for k in TIME_KEYS] + [("ladder", x) for x in ladder]:
        if not _is_hhmm(v):
            raise ConfigError(f"{k}: {v!r} is not a HH:MM time")
    if ladder != sorted(ladder):
        raise ConfigError("ladder times must be a non-empty ascending list")
    # the session's order of events; equal times are allowed only where the engine defines them (place cut-off =
    # first ladder slot)
    if not (cfg["step_plan_at"] < cfg["place_at"] < cfg["last_place_at"] <= ladder[0]
            and ladder[-1] < cfg["cancel_at"] < cfg["reconcile_at"]):
        raise ConfigError("times must run step_plan_at < place_at < last_place_at <= ladder < cancel_at < "
                          "reconcile_at")
    try:
        ltp_m, cap_m = Decimal(str(cfg["buy_ltp_mult"])), Decimal(str(cfg["buy_cap_mult"]))
        reserve = Decimal(str(cfg["charges_reserve"]))
        rates = {k: Decimal(str(cfg["charges_rates"][k])) for k in CHARGE_KEYS}
    except (InvalidOperation, KeyError, TypeError) as e:
        raise ConfigError(f"buy multipliers / charges are not decimal numbers: {e!r}") from e
    if not (Decimal(1) <= ltp_m <= cap_m <= MAX_BUY_MULT):
        raise ConfigError(f"need 1 <= buy_ltp_mult <= buy_cap_mult <= {MAX_BUY_MULT}, got {ltp_m}, {cap_m}")
    if not (Decimal(0) <= reserve < Decimal("0.05")) or any(v < 0 or v >= 1 for v in rates.values()):
        raise ConfigError("charges_reserve / charges_rates out of range")
    return cfg


def _is_hhmm(v: Any) -> bool:
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":" or not (v[:2] + v[3:]).isdigit():
        return False
    return int(v[:2]) < 24 and int(v[3:]) < 60


def paths(home: Optional[Path] = None, state: Optional[Path] = None) -> Paths:
    h = home or HOME
    return Paths(home=h, state=state or (h / "state"))
