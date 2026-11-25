"""Owner controls and safety switches: ARM, KILL, FIFTY's kill switch, FIFTY's token (read only), clock.

Build spec §C1, §C2; frozen design §1.3 (ARM), D36 (kill), §2.3 (token), E10 (clock).
Code never writes ARM. KILL is written only by the owner at the Pi or by the Telegram poller on the owner's `kill`.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import util

PHASES = ("P0", "P0-T", "P1", "P2", "P3")
ARM_MODES = ("approve_each", "automatic")
ARM_FIELDS = ("phase", "plan_months", "mode", "buy_cap_rupees_per_session", "valid_from", "valid_to")


@dataclass(frozen=True)
class Arm:
    phase: str
    plan_months: Tuple[str, ...]
    mode: str
    cap_rupees: int
    valid_from: dt.date
    valid_to: dt.date

    def covers(self, plan_month: str, day: dt.date) -> bool:
        return plan_month in self.plan_months and self.valid_from <= day <= self.valid_to


def read_arm(p: Path) -> Tuple[Optional[Arm], str]:
    """-> (arm, problem). Any problem means: no ARM (dry run + ALERT)."""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "no ARM file"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"ARM unparseable: {e}"
    if not isinstance(raw, dict) or set(raw) != set(ARM_FIELDS):
        return None, f"ARM must have exactly the fields {ARM_FIELDS}"
    try:
        months = tuple(raw["plan_months"])
        if not months or not all(isinstance(m, str) and len(m) == 7 and m[4] == "-" for m in months):
            raise ValueError("plan_months")
        if raw["phase"] not in PHASES or raw["mode"] not in ARM_MODES:
            raise ValueError("phase/mode")
        cap = raw["buy_cap_rupees_per_session"]
        if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
            raise ValueError("cap")
        vf, vt = dt.date.fromisoformat(raw["valid_from"]), dt.date.fromisoformat(raw["valid_to"])
        if vf > vt:
            raise ValueError("valid_from > valid_to")
    except (ValueError, TypeError) as e:
        return None, f"ARM invalid ({e})"
    return Arm(raw["phase"], months, raw["mode"], cap, vf, vt), ""


def killed(kill_file: Path) -> bool:
    return kill_file.exists()


def fifty_kill_switch(db_file: Path) -> Tuple[bool, str]:
    """FIFTY's kill switch lives in its sqlite bot_state table (FIFTY/src/models/database.py:387).
    -> (active, problem). Unreadable counts as ACTIVE (fail closed)."""
    if not db_file.exists():
        return True, f"FIFTY db missing: {db_file}"
    try:
        con = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT value FROM bot_state WHERE key = 'kill_switch'").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return True, f"FIFTY db unreadable: {e}"
    return (row is not None and row[0] == "active"), ""


@dataclass(frozen=True)
class Token:
    api_key: str
    access_token: str
    user_id: str
    generated_at: dt.datetime


def read_token(p: Path, user_id: str) -> Tuple[Optional[Token], str]:
    """Accept only user_id == configured AND generated_at strictly today after 06:00 IST (spec §C2).
    FIFTY writes the file non-atomically, so the caller re-polls on a parse error."""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "token file missing"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return None, f"token unparseable: {e}"
    try:
        api_key, access, uid, gen = raw["api_key"], raw["access_token"], raw["user_id"], raw["generated_at"]
        if not all(isinstance(x, str) and x for x in (api_key, access, uid, gen)):
            raise ValueError("empty field")
        g = dt.datetime.fromisoformat(gen)
    except (KeyError, ValueError, TypeError) as e:
        return None, f"token fields invalid ({e})"
    g = g.replace(tzinfo=util.IST) if g.tzinfo is None else g.astimezone(util.IST)
    if uid != user_id:
        return None, f"token user {uid} != {user_id}"
    now = util.now()
    floor = dt.datetime(now.year, now.month, now.day, 6, 0, tzinfo=util.IST)
    if not (floor <= g <= now):
        return None, f"token generated_at {g.isoformat()} is not today after 06:00"
    return Token(api_key, access, uid, g), ""


def clock_ok() -> Tuple[bool, str]:
    """System TZ must be Asia/Kolkata and NTP synchronised (E10). Only checked on the Pi (posix)."""
    import os
    if os.name != "posix":
        return True, "not posix (dev PC): clock check skipped"
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "Timezone", "-p", "NTPSynchronized"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"timedatectl failed: {e}"
    kv: Dict[str, Any] = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if kv.get("Timezone") != "Asia/Kolkata":
        return False, f"timezone {kv.get('Timezone')}"
    if kv.get("NTPSynchronized") != "yes":
        return False, "NTP not synchronised"
    return True, ""
