"""Configuration: tracked defaults + untracked overlay (overlay wins), and the folder layout.

Build spec §A. Every number used by the engine lives in config/momentum.defaults.json; nothing is hard-coded in code.
The overlay config/momentum.json (git-ignored) holds per-box settings such as mode=live.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

HOME = Path(os.environ.get("MOMENTUM_HOME", str(Path(__file__).resolve().parents[2])))

REQUIRED_KEYS = (
    "mode", "account_user_id", "book_cash_open", "n_hold", "charges_reserve", "buy_ltp_mult", "buy_cap_mult",
    "ladder", "cancel_at", "last_place_at", "lock_tries", "vet_timeout_s", "kite_rps", "kite_retry_s",
    "token_poll_s", "token_first_poll", "token_deadline", "plan_final", "audit_ret", "audit_factor", "audit_kite_pp",
    "price_freeze", "tg_retries", "tg_retry_s", "min_free_gb", "rms_max", "defer_max", "fifty_token_file",
    "fifty_db_file", "holiday_file", "nse_data", "claude_bin", "claude_version", "units_open", "nav_open",
)


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

    def ensure(self) -> None:
        for d in (self.control, self.state, self.inbox, self.decisions, self.bands, self.audit, self.vet,
                  self.locks, self.reports, self.snapshots, self.refresh, self.derived):
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
    ladder: List[str] = cfg["ladder"]
    if ladder != sorted(ladder) or not ladder:
        raise ConfigError("ladder times must be a non-empty ascending list")
    return cfg


def paths(home: Optional[Path] = None, state: Optional[Path] = None) -> Paths:
    h = home or HOME
    return Paths(home=h, state=state or (h / "state"))
