"""Command line for cron (build spec §C timeline). Every command takes its own flock; order-touching commands share
`order` (one journal writer, E07/E29).

    python -m momentum.cli evening [--resume] [--as-of-signal YYYY-MM-DD] [--skip-fetch]
    python -m momentum.cli morning | afternoon | watch | canary | check-arm | kill-cancel | status
    python -m momentum.cli poller            # long-running Telegram poller (kept alive like FIFTY's watchdog)
    python -m momentum.cli manifest          # (PC) write manifests/code.sha256 for the commit being deployed
    python -m momentum.cli isolation-test    # (Pi) T5: the vet path has no tools
    python -m momentum.cli repair-journal    # (Pi, owner) after a power cut: move a torn LAST journal line aside
Global: --dry (no broker writes, state_dryrun/ root), --home DIR.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from . import broker as B
from . import config as CF
from . import control as C
from . import engine as E
from . import ledger as L
from . import util
from . import vet as V
from .journal import Journal, LockBusy, process_lock, repair_torn_tail

log = logging.getLogger("momentum")

ARM_EXPIRY_WARN_DAYS = 3        # spec §I: "ARM expires in 3 days"
POLLER_ERROR_SLEEP_S = 10

SRS_MODULES = ("compounding", "de_kill", "fetch_indices", "fo_membership", "full_asrs", "htf_orb", "joiner_free",
               "joiner_positional", "joiner_profile", "liq_side", "monthly_orb", "nonfo_momentum", "nonfo_srs",
               "nov_stocks", "nse_ca", "nse_cm_fetch", "options_check", "sim", "slip_test", "stocks_asrs_check",
               "survivorship", "y2026_asrs")


def resolve(home: Path, p: str) -> Path:
    q = Path(p)
    return q if q.is_absolute() else (home / q).resolve()


# ---------------------------------------------------------------- manifest (frozen design §11.2)
def manifest_files(home: Path, cfg: Dict[str, Any]) -> List[Path]:
    srs = resolve(home, cfg["srs_dir"])
    # review 11-28 M7: the tracked defaults and the crontab change behaviour as much as code does
    return (sorted((home / "src" / "momentum").glob("*.py")) + [srs / f"{m}.py" for m in SRS_MODULES]
            + [home / "config" / "momentum.defaults.json", home / "deploy" / "crontab.txt"])


def compute_manifest(home: Path, cfg: Dict[str, Any]) -> Dict[str, str]:
    """{path relative to MOMENTUM home: sha256 of the text with LF line ends} (git may check out CRLF on the PC and
    LF on the Pi; the hash must not depend on that)."""
    out = {}
    for f in manifest_files(home, cfg):
        key = f.relative_to(home).as_posix() if home in f.parents else f.as_posix()
        out[key] = util.sha256_text(f.read_bytes().decode("utf-8").replace("\r\n", "\n"))
    return out


def check_manifest(home: Path, cfg: Dict[str, Any]) -> List[str]:
    p = home / "manifests" / "code.sha256.json"
    try:
        want = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ["code manifest missing (run `manifest` on the PC and commit it)"]
    have = compute_manifest(home, cfg)
    bad = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    return [f"code differs from the tested manifest: {bad[:5]}{' ...' if len(bad) > 5 else ''}"] if bad else []


# ---------------------------------------------------------------- environment
def holidays(cfg: Dict[str, Any], home: Path) -> Set[str]:
    raw = json.loads(resolve(home, cfg["holiday_file"]).read_text(encoding="utf-8"))
    return {e["date"] for k, v in raw.items() if not k.startswith("_") for e in v}


def holiday_year_ok(cfg: Dict[str, Any], home: Path, day: dt.date) -> bool:
    raw = json.loads(resolve(home, cfg["holiday_file"]).read_text(encoding="utf-8"))
    return bool(raw.get(str(day.year)))


class Ctx:
    def __init__(self, a: argparse.Namespace, open_journal: bool = True) -> None:
        self.home = Path(a.home).resolve() if a.home else CF.HOME
        self.cfg = CF.load(self.home)
        self.dry = bool(a.dry) or self.cfg["mode"] != "live"
        state = self.home / ("state_dryrun" if self.dry else "state")
        self.paths = CF.paths(self.home, state)
        self.paths.ensure()
        self.cfg["_nse_data"] = str(resolve(self.home, self.cfg["nse_data"]))
        logs = self.home / "logs"
        logs.mkdir(exist_ok=True)
        logging.basicConfig(filename=logs / f"momentum_{util.today().isoformat()}.log", level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
        man = self.home / "manifests" / "code.sha256.json"
        self.cfg["_code_manifest_sha"] = util.sha256_file(man) if man.exists() else None
        # the dry ROOT is separate; records stay plain there. repair-journal opens the journal itself.
        self.journal: Journal = Journal(self.paths.journal, dry=False) if open_journal else None  # type: ignore
        self.arm, self.arm_problem = C.read_arm(self.paths.arm_file)
        self._bot: Any = None
        self._api: Any = None

    # telegram
    def bot(self) -> Any:
        if self._bot is None:
            from .telegram import Bot, load_secrets
            tok, chat = load_secrets(self.paths.secrets)
            self._bot = Bot(tok, chat, int(self.cfg["tg_retries"]), int(self.cfg["tg_retry_s"]))
        return self._bot

    def notify(self, text: str, buttons: Any = None) -> bool:
        prefix = "[DRY] " if self.dry else ""
        try:
            bot = self.bot()
            bot.fast = (util.at_today(self.cfg["step_plan_at"]) <= util.now()
                        <= util.at_today(self.cfg["reconcile_at"]))     # M6: never stall the order steps
            return bot.send(prefix + "MOM " + text, buttons)
        except Exception as e:  # noqa: BLE001 - no Telegram = automatic steps do not start (D36)
            log.error("telegram unavailable: %s", e)
            return False

    # kite
    def token(self) -> Optional[C.Token]:
        tok, why = C.read_token(resolve(self.home, self.cfg["token_file"]), self.cfg["account_user_id"])
        if tok is None:
            log.warning("token: %s", why)
        return tok

    def wait_token(self, deadline: dt.datetime) -> Optional[C.Token]:
        while True:
            tok = self.token()
            if tok is not None or util.now() >= deadline:
                return tok
            time.sleep(int(self.cfg["token_poll_s"]))

    def api(self, tok: C.Token) -> Any:
        if self._api is None:
            self._api = B.KiteApi(tok.api_key, tok.access_token, int(self.cfg["kite_rps"]),
                                  list(self.cfg["kite_retry_s"]))
        return self._api

    def env(self, api: Any) -> E.Env:
        def armed() -> bool:
            """Read again before every broker write: an ARM the owner removed or changed mid-session stops orders."""
            arm, _why = C.read_arm(self.paths.arm_file)
            month = current_plan_month(self.journal)
            return bool(arm and month and arm.covers(month, util.today()))
        gate = B.Gate(live=not self.dry, killed=lambda: C.killed(self.paths.kill_file), armed=armed)
        return E.Env(self.cfg, self.paths, self.journal, api, B.Broker(api, self.journal, gate), self.notify,
                     lambda step, vin, sk: V.run(self.cfg, self.paths.vet, step, vin, sk), sleep_until,
                     arm=self.arm, dry=self.dry, fo_bhav_fn=self.fo_bhav)

    def fo_bhav(self, day: str) -> Set[str]:
        """Stock futures in the NSE F&O bhavcopy of `day` (the research membership file; day-1 backstop, D47)."""
        from . import pipeline as P
        M = P.import_research(resolve(self.home, self.cfg["srs_dir"]), Path(self.cfg["_nse_data"]))
        return P.fo_url_fetch(M, day, Path(self.cfg["_nse_data"]) / "fo_bhav_live" / f"{day}.zip")


def current_plan_month(j: Journal) -> Optional[str]:
    lg = L.replay(j.read())
    p = E.current_plan(lg)
    return p["plan_id"]["month"] if p else None


def sleep_until(t: dt.datetime) -> None:
    while True:
        left = (t - util.now()).total_seconds()
        if left <= 0:
            return
        time.sleep(min(left, 30))


# ---------------------------------------------------------------- preflight (spec §E)
def preflight(x: Ctx, api: Any, full: bool) -> List[str]:
    p: List[str] = []
    day = util.today()
    if C.killed(x.paths.kill_file):
        p.append("KILL present")
    recs = x.journal.read()
    lg = L.replay(recs)
    plan = E.current_plan(lg)
    if not x.dry:
        if x.arm is None:
            p.append(f"no ARM ({x.arm_problem})")
        elif plan is not None and not x.arm.covers(plan["plan_id"]["month"], day):
            p.append(f"ARM does not cover plan month {plan['plan_id']['month']} on {day}")
        elif x.arm.mode != "automatic":
            p.append(f"ARM mode {x.arm.mode} is not built in this slice (only automatic)")
        p += check_manifest(x.home, x.cfg)
    ok, why = C.clock_ok()
    if not ok:
        p.append(f"clock: {why}")
    if shutil.disk_usage(x.paths.state).free < int(x.cfg["min_free_gb"]) * 2 ** 30:
        p.append("disk < min_free_gb")
    if not holiday_year_ok(x.cfg, x.home, day):
        p.append(f"holiday file has no {day.year} entries")
    if day.isoformat() in holidays(x.cfg, x.home) or day.weekday() >= 5:
        p.append("not a trading day per the holiday file")
    try:
        prof = api.profile()
        if prof.get("user_id") != x.cfg["account_user_id"]:
            p.append(f"profile user {prof.get('user_id')}")
    except Exception as e:  # noqa: BLE001
        p.append(f"Kite profile failed: {type(e).__name__}")
    why_ledger = ledger_cache_problem(x, recs)
    if why_ledger:
        p.append(why_ledger)
    snap = [r for r in recs if r["type"] == "HOLDINGS_SNAPSHOT" and r["ts"][:10] == day.isoformat()]
    if snap:                                   # frozen design §7.4: opening Kite holdings >= the book's quantity
        short = E.holdings_short(lg, E.with_todays_credits(recs, {k: int(v) for k, v in snap[0]["holdings"].items()},
                                                           day.isoformat()))
        if short and not any(r["type"] == "OWNER_CMD" and r["cmd"] == "ack" and r["seq"] > snap[0]["seq"]
                             for r in recs):
            p.append("opening holdings below the book: " + "; ".join(short) + " (owner: check Kite, then send 'ack')")
    if lg.unresolved:
        p.append(f"unresolved intents {lg.unresolved}")
    tw = [r for r in recs if r["type"] == "NOTE" and "three_way" in r]
    if tw and tw[-1]["problems"]:
        acked = any(r["type"] == "OWNER_CMD" and r["cmd"] == "ack" and r["seq"] > tw[-1]["seq"] for r in recs)
        if not acked:
            p.append(f"16:00 three-way check of {tw[-1]['three_way']} found problems (owner: check, then send 'ack')")
    if full and plan is None:
        p.append("no FINAL plan")
    return p


def write_ledger(x: Ctx) -> None:
    """ledger.json = the replay of the journal up to `journal_seq` (a cache for people and for the tamper check)."""
    recs = x.journal.read()
    d = L.replay(recs).to_json()
    d["journal_seq"] = int(recs[-1]["seq"]) if recs else 0
    util.write_json_atomic(x.paths.ledger, d)


def ledger_cache_problem(x: Ctx, recs: List[Dict[str, Any]]) -> Optional[str]:
    """Spec §E 'journal replay == ledger'. The file must equal the replay of the journal UP TO THE SEQ IT WAS WRITTEN
    AT: a file that is merely older than the journal (a crash before the last write) is not a fault and must not
    stop a session; an edited / damaged file, or a journal shorter than the file knows, is."""
    if not x.paths.ledger.exists():
        return None
    last = int(recs[-1]["seq"]) if recs else 0
    try:
        stored = json.loads(x.paths.ledger.read_text(encoding="utf-8"))
        n = stored.pop("journal_seq", last)     # a file from before journal_seq existed: compared with the full replay
    except (ValueError, AttributeError):
        return "ledger.json unreadable"
    if not isinstance(n, int) or n > last:
        return f"ledger.json was written at journal seq {n} but the journal ends at {last} (journal lost records?)"
    if not same_ledger(stored, L.replay([r for r in recs if r["seq"] <= n]).to_json()):
        return f"ledger.json != journal replay at seq {n} (file edited or damaged?)"
    return None


def same_ledger(stored: Any, replay: Any) -> bool:
    """Equal, ignoring fields the replay has and the file lacks (a file written by an older code version before a
    pull: a new field is not damage). Every field both have must match."""
    if isinstance(stored, dict) and isinstance(replay, dict):
        if set(stored) - set(replay):
            return False
        return all(same_ledger(stored[k], replay[k]) for k in stored)
    return stored == replay


def backup_state(x: Ctx) -> bool:
    """Spec §C13 / §L3 fallback: copy the journal and the ledger to a second local folder and send the journal to
    the owner's Telegram chat as a document (the Pi's SD card is otherwise the only copy of the book).
    -> True when the Telegram document was delivered. Never raises: a failed backup must not fail the step."""
    try:
        dst = resolve(x.home, x.cfg["backup_dir"]) / ("dry" if x.dry else "live") / util.today().isoformat()
        dst.mkdir(parents=True, exist_ok=True)
        for f in (x.paths.journal, x.paths.ledger):
            if f.exists():
                shutil.copy2(f, dst / f.name)
        # review 11-28 M8: the decision files (hash-checked every afternoon), bands and snapshots are needed too
        for d in (x.paths.decisions, x.paths.bands, x.paths.snapshots):
            if d.exists():
                shutil.copytree(d, dst / d.name, dirs_exist_ok=True)
        if not x.paths.journal.exists():
            return False
        ok = bool(x.bot().send_document(x.paths.journal, f"{'[DRY] ' if x.dry else ''}MOM journal "
                                        f"{util.today().isoformat()} (backup)"))
        send_decisions_zip(x, dst)
        return ok
    except Exception as e:  # noqa: BLE001 - reported by the caller's heartbeat text
        log.error("backup failed: %s: %s", type(e).__name__, e)
        return False


def send_decisions_zip(x: Ctx, dst: Path) -> None:
    """M8: the decision files and bands off the SD card too: a zip to Telegram whenever the set of decision files
    changed since the last one sent (a new plan, a TEST plan). Never raises."""
    import zipfile
    try:
        names = sorted(p.name for p in x.paths.decisions.glob("*.json")) if x.paths.decisions.exists() else []
        mark = resolve(x.home, x.cfg["backup_dir"]) / ("dry" if x.dry else "live") / "decisions_sent.json"
        if not names or (mark.exists() and json.loads(mark.read_text(encoding="utf-8")) == names):
            return
        z = dst / "decisions_bands.zip"
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for d in (x.paths.decisions, x.paths.bands):
                for f in sorted(d.glob("*.json")) if d.exists() else []:
                    zf.write(f, f"{d.name}/{f.name}")
        if x.bot().send_document(z, f"{'[DRY] ' if x.dry else ''}MOM decisions + bands {util.today().isoformat()} "
                                    "(backup: restore = RUNBOOK 3c)"):
            util.write_json_atomic(mark, names)
    except Exception as e:  # noqa: BLE001 - reported in the log; the journal backup is the main one
        log.error("decisions backup failed: %s: %s", type(e).__name__, e)


def holdings_snapshot(x: Ctx, api: Any) -> Dict[str, int]:
    day = util.today().isoformat()
    for r in reversed(x.journal.read()):
        if r["type"] == "HOLDINGS_SNAPSHOT" and r["ts"][:10] == day:
            return {k: int(v) for k, v in r["holdings"].items()}
    rows = api.holdings()
    h = {str(r["isin"]): int(r.get("quantity", 0)) + int(r.get("t1_quantity", 0)) for r in rows}
    settled = {str(r["isin"]): int(r.get("quantity", 0)) for r in rows}
    x.journal.append("HOLDINGS_SNAPSHOT", holdings=h, settled=settled)
    return h


# ---------------------------------------------------------------- commands
def cmd_evening(x: Ctx, a: argparse.Namespace) -> int:
    """Signal-day evening, or a re-run for an earlier signal day whose plan is not FINAL yet (E21, same signal day)."""
    from . import evening as EV
    from . import pipeline as P
    pending_f = x.paths.refresh / "pending_signal.json"
    if a.pending_only and not pending_f.exists() and missed_signal_day(x) is None:
        return 0                                # the morning catch-up (review 11-28 T7): a failed or missed evening only
    if a.as_of_signal:
        day = dt.date.fromisoformat(a.as_of_signal)
    elif pending_f.exists():
        day = dt.date.fromisoformat(json.loads(pending_f.read_text(encoding="utf-8"))["signal_day"])
    elif EV.is_signal_day(util.today(), holidays(x.cfg, x.home)) and not a.pending_only:
        day = util.today()
    elif missed_signal_day(x) is not None:
        day = missed_signal_day(x)             # review 11-28 M2: the Pi was off (or the file wrong) on the signal day
    else:
        return 0
    util.write_json_atomic(pending_f, {"signal_day": day.isoformat(), "since": util.now().isoformat()})
    m = P.month_add(day.isoformat()[:7], 1)
    if not x.dry:                              # manifests are recomputed before every live step (spec §C1)
        bad = check_manifest(x.home, x.cfg)
        if bad:
            x.notify(f"ALERT MANIFEST: {'; '.join(bad)}: no {m} plan is made by untested code")
            return 6
    tok = x.token()
    if tok is None:
        x.notify(f"ALERT TOKEN: no valid Kite token this evening: no {m} plan tonight (re-run next evening)")
        return 2
    api = x.api(tok)
    env = x.env(api)
    M = P.import_research(resolve(x.home, x.cfg["srs_dir"]), Path(x.cfg["_nse_data"]))
    try:
        EV.book_open(env)
        E.ingest_inbox(env)
        res = EV.run_signal_evening(env, M, m, day, skip_fetch=a.skip_fetch,
                                    first_session=EV.next_session(day, holidays(x.cfg, x.home)).isoformat())
    except EV.SignalDayMismatch as e:
        # review 11-28 M2: the holiday file missed a closure on the computed signal day. Once that day's evening has
        # come (Kite's day candle exists by 20:00 when the market traded), Kite's last session of the month is the
        # signal day: re-point the pending day; the next slot (30 min / the morning catch-up) builds the plan.
        # only from the NEXT day: on the signal day itself a late / partial Kite candle must never move the plan to
        # an earlier day (the evening retries every 30 min); the 09:10 catch-up still makes day-1 buys
        late_enough = util.today() > day
        if e.kite_last and e.kite_last < day.isoformat() and late_enough:
            util.write_json_atomic(pending_f, {"signal_day": e.kite_last, "since": util.now().isoformat(),
                                               "was": day.isoformat()})
            env.alert("SIGNAL_DAY_MOVED", f"{m}: Kite's last {day.isoformat()[:7]} session is {e.kite_last}, not "
                      f"{day} (the holiday file missed a closure): the plan is built for {e.kite_last}")
        else:
            env.alert("NO_PLAN", f"{m} (signal day {day}): {e}")
        write_ledger(x)
        return 3
    except (EV.EveningError, P.PipelineError) as e:
        today_s = util.today().isoformat()
        late = util.now() >= util.at_today(x.cfg["plan_final"]) or today_s > day.isoformat()
        why = f"{m} (signal day {day}): {e}"
        if any(r["type"] in ("ALERT", "DRY_ALERT") and r.get("code") == "NO_PLAN" and r["ts"][:10] == today_s
               and r.get("text", "").startswith(why[:120]) for r in env.journal.read()):
            log.warning("NO_PLAN again (already alerted today): %s", why)      # review 11-28 O20: once a day per cause
        else:
            env.alert("NO_PLAN", why + "; " + ("the job retries each evening 20:00-23:30 and on weekday mornings "
                                               "09:10-12:40" if late else "retrying every 30 min until "
                                               + x.cfg["plan_final"]))
        write_ledger(x)
        return 3
    if res["status"] in ("FINAL", "already final"):
        pending_f.unlink(missing_ok=True)
    write_ledger(x)
    if res["status"] == "FINAL":
        backup_state(x)
    log.info("evening %s: %s", m, res)
    print(json.dumps(res))
    return 0


def missed_signal_day(x: Ctx) -> Optional[dt.date]:
    """Review 11-28 M2: an armed month with no plan and no pending signal day (the Pi was off the whole signal evening,
    or the holiday file made is_signal_day false on the real last session): the last weekday non-holiday of the
    previous month, for the evening / morning catch-up to build (the Kite check corrects it if the file is wrong)."""
    if x.arm is None:
        return None
    m = util.today().isoformat()[:7]
    nxt = (util.today().replace(day=28) + dt.timedelta(days=4)).isoformat()[:7]
    hol = holidays(x.cfg, x.home)
    lg = L.replay(x.journal.read())
    for month in (m, nxt):                      # this month's plan (missed), or next month's after its signal day
        if month not in x.arm.plan_months or any(k.startswith(month + "|") for k in lg.plans):
            continue
        first = dt.date.fromisoformat(month + "-01")
        d = first - dt.timedelta(days=1)
        while d.weekday() >= 5 or d.isoformat() in hol:
            d -= dt.timedelta(days=1)
        if d < util.today():
            return d
    return None


def missing_plan_month(x: Ctx, lg: L.Ledger) -> Optional[str]:
    """The 08:40 plan check (spec §C7, §J 'no FINAL at 08:40'): the ARM says this month is to be traded but the
    journal holds no FINAL plan for it (the signal evening never ran or never finished). -> the month, or None."""
    m = util.today().isoformat()[:7]
    if x.dry or x.arm is None or m not in x.arm.plan_months or any(k.startswith(m + "|") for k in lg.plans):
        return None
    return m


def cmd_morning(x: Ctx, a: argparse.Namespace) -> int:
    lg = L.replay(x.journal.read())
    plan = E.current_plan(lg)
    pend = [p for p in lg.positions.values() if p.leg in ORDER_LEGS]
    held = [p for p in lg.positions.values() if p.held_qty > 0]
    carry_alerts(x, f"ALIVE plan {L.plan_key(plan['plan_id']) if plan else 'none'}; " + E.status_text(lg))
    no_plan = missing_plan_month(x, lg)
    if no_plan:
        x.journal.append("ALERT", code="NO_PLAN", text=f"no FINAL plan for {no_plan}")
        x.notify(f"ALERT NO_PLAN: the ARM covers {no_plan} but there is no FINAL plan for it (signal evening missed "
                 "or failed). Nothing is bought. The evening job re-runs a pending signal day by itself; if it never "
                 "started: ./momentum evening --as-of-signal <last session of last month>")
    if (not pend and not held) or market_holiday_today(x):
        return 0
    tok = x.wait_token(util.at_today(x.cfg["token_alert_at"]))
    if tok is None:
        x.notify(f"ALERT TOKEN: no Kite token by {x.cfg['token_alert_at']} (deadline {x.cfg['token_deadline']})")
        return 2
    api = x.api(tok)
    env = x.env(api)
    h = holdings_snapshot(x, api)
    ca_credits(env, api)                       # January spec §7: book split / bonus credits before any check
    if pend:
        probs = preflight(x, api, full=True)
        if probs:
            env.alert("PREFLIGHT_AM", "; ".join(probs))
    else:
        short = E.holdings_short(lg, h)
        if short:
            env.alert("HOLDINGS_SHORT", "; ".join(short))
    E.ingest_inbox(env)
    E.shared_name_holds(env, h)                # D38: a plan ISIN with non-book quantity in the account
    if pend:
        E.remind_holds(env)                    # review 11-28 M1: an untapped hold never sits silent
        morning_cash_check(env)
    write_ledger(x)
    return 0


def morning_cash_check(env: E.Env) -> None:
    """Review 11-28 O13: the 15:10 cash guard is all-or-nothing; say at 08:35 if the account is short, while there is
    still a day to fund it. Advisory only (15:10 re-reads it)."""
    lg = L.replay(env.journal.read())
    need = sum(p.remaining_paise for p in lg.positions.values() if p.leg == "PENDING_BUY" and not p.overlays) // 100
    if any(p.leg == "BUY_GATED" for p in lg.positions.values()):
        # a rebalance day: the buys use the book's free cash now plus today's sale money; Kite must at least hold
        # the free cash the book already has (the sales add to it at 14:50)
        need = max(need, (lg.cash_paise - lg.earmarked_paise()) // 100)
    if need <= 0:
        return
    try:
        avail = E.available_cash(env.api.margins())
    except B.NetworkError as e:
        log.warning("morning cash check: margins unreadable (%s)", e)
        return
    if avail < need:
        env.alert("CASH_AM", f"Kite available Rs {avail:,.0f} < today's book buys Rs {need:,}: add funds to "
                  f"{env.cfg['account_user_id']} before {env.cfg['place_at']} or nothing is bought today (D09)")


ORDER_LEGS = ("PENDING_BUY", "BUY_GATED", "SELLING")    # legs that need an afternoon (January slice: sells too)


def order_work(lg: L.Ledger) -> bool:
    """The afternoon and morning have work when any leg can still trade (review C1: sells and gated buys too)."""
    return any(p.leg in ORDER_LEGS for p in lg.positions.values())


def market_holiday_today(x: Ctx) -> bool:
    """Review 11-28 L5: a weekend or a listed NSE holiday: the order jobs exit at once (no token wait, no TOKEN /
    PREFLIGHT noise). A closure missing from the file is still caught by the preflight / the gate's Kite check."""
    try:
        return util.today().weekday() >= 5 or util.today().isoformat() in holidays(x.cfg, x.home)
    except Exception:  # noqa: BLE001 - an unreadable file: let the preflight report it
        return False


def cmd_afternoon(x: Ctx, a: argparse.Namespace) -> int:
    lg = L.replay(x.journal.read())
    if not order_work(lg) or market_holiday_today(x):
        return 0
    tok = x.wait_token(util.at_today(x.cfg["token_deadline"]))
    if tok is None:
        day = util.today().isoformat()
        if not any(r["type"] == "ALERT" and r.get("code") == "TOKEN" and r["ts"][:10] == day
                   for r in x.journal.read()):          # once a day: this command re-runs every 5 minutes
            x.journal.append("ALERT", code="TOKEN", text="no Kite token for the afternoon step")
            x.notify(f"ALERT TOKEN: no Kite token by {x.cfg['token_deadline']}: buys move one session unless it "
                     f"appears before {x.cfg['last_place_at']}")
        return 2
    api = x.api(tok)
    env = x.env(api)
    h = holdings_snapshot(x, api)
    ca_credits(env, api)                       # a split / bonus credited since the morning: booked before 14:45
    E.afternoon(env, h, lambda: preflight(x, api, full=True))
    write_ledger(x)
    return 0


def ca_credits(env: E.Env, api: Any) -> None:
    from . import corpact as CA
    try:
        for line in CA.credit_check(env, CA.holdings_rows(api)):
            log.info("ca: %s", line)
    except Exception as e:  # noqa: BLE001 - a CA check bug must not stop the order steps; the holdings checks still run
        log.exception("ca credit check failed")
        env.alert("CA_CHECK", f"corporate-action credit check failed ({type(e).__name__}: {str(e)[:120]})")


def cmd_ca_scan(x: Ctx, a: argparse.Namespace) -> int:
    """Daily: NSE corporate actions for the book's names (January spec §7)."""
    from . import corpact as CA
    from . import pipeline as P
    lg = L.replay(x.journal.read())
    if not CA.book_positions(lg):
        return 0
    M = P.import_research(resolve(x.home, x.cfg["srs_dir"]), Path(x.cfg["_nse_data"]))
    env = x.env(None)
    r = CA.scan(env, M.CA)
    log.info("ca-scan: %s", r)
    print(json.dumps(r))
    return 0 if r.get("ok") else 4


def cmd_test_plan(x: Ctx, a: argparse.Namespace) -> int:
    """TEST lot (D51): on the configured buy / sell days write the TEST plan the 14:40 afternoon then trades."""
    from . import testlot as T
    day = util.today().isoformat()
    cfg = x.cfg["test_lot"]
    if day != cfg["buy_day"] and day not in cfg["sell_days"]:
        return 0
    tok = x.wait_token(util.at_today(x.cfg["token_deadline"]))
    if tok is None:
        x.notify("ALERT TEST_LOT: no Kite token: no TEST plan today")
        return 2
    api = x.api(tok)
    env = x.env(api)
    res = T.run(env, holdings_snapshot(x, api))
    log.info("test-plan: %s", res)
    write_ledger(x)
    return 0


def cmd_watch(x: Ctx, a: argparse.Namespace) -> int:
    lg = L.replay(x.journal.read())
    write_ledger(x)
    day = util.today().isoformat()
    probs: List[str] = []
    checked = False
    recs = x.journal.read()
    snap = [r for r in recs if r["type"] == "HOLDINGS_SNAPSHOT" and r["ts"][:10] == day]
    ran_today = any(r["type"] == "STAGE" and r.get("date") == day for r in recs)
    recon = E.stage_done(recs, day, "RECON")
    done = any(r["type"] == "NOTE" and r.get("three_way") == day for r in recs)
    if a.resume and (done or not ran_today):
        return 0                                # 16:50: only a three-way the 16:00 run had to leave for the reconcile
    if a.resume and not recon:
        x.notify("ALERT THREE_WAY_NOT_RUN: the reconcile never finished today (see RECON_RETRY): the 16:00 three-way "
                 "did not run; tomorrow's 08:35 holdings check is the hard check")
        return 0
    waits = ran_today and not recon             # review 11-28 M5: fills not booked yet -> a false THREE_WAY
    if waits:
        pass
    elif snap and any(p.filled_qty or p.held_qty or p.leg == "PENDING_BUY" for p in lg.positions.values()):
        tok = x.token()
        if tok is None:
            probs.append("no token for the 16:00 check")
        else:
            probs = E.three_way(x.env(x.api(tok)), {k: int(v) for k, v in snap[0]["holdings"].items()}, day)
            checked = True
    lg = L.replay(x.journal.read())
    write_ledger(x)
    if a.resume:
        x.notify("16:50 three-way after the late reconcile: " + (f"{len(probs)} PROBLEMS" if probs else "OK"))
        return 0
    backed_up = backup_state(x)
    stages = sorted({str(r["stage"]) for r in x.journal.read() if r["type"] == "STAGE" and r.get("date") == day})
    three = (f"; THREE-WAY PROBLEMS {len(probs)}" if probs else "; three-way OK" if checked else
             "; three-way waits for the reconcile (16:50)" if waits else
             "; three-way not run (no opening snapshot today or nothing in the book)")
    carry_alerts(x, "HEARTBEAT " + E.status_text(lg) + three
                 + (f"; stages today {stages}" if stages else "; no order stage ran today")
                 + ("" if backed_up else "; BACKUP NOT SENT"))
    return 0


def carry_alerts(x: Ctx, text: str) -> None:
    """Send ALIVE / HEARTBEAT with the alerts Telegram missed since the last such message; mark them carried only
    when this message is delivered (review: an alert raised while Telegram was down was lost)."""
    recs = x.journal.read()
    extra = E.undelivered_alerts(recs)
    if x.notify(text + extra) and extra:
        x.journal.append("NOTE", carried_alerts_upto=max(r["seq"] for r in recs))


def cmd_canary(x: Ctx, a: argparse.Namespace) -> int:
    ok, why = V.canary(x.cfg)
    if not ok:
        x.notify(f"ALERT CANARY FAILED: {why}")
    return 0 if ok else 4


def cmd_check_arm(x: Ctx, a: argparse.Namespace) -> int:
    """20:30: ARM present, covering the months that are about to trade, and not about to expire (spec §I)."""
    if x.arm is None:
        x.notify(f"ALERT NO ARM: {x.arm_problem} -> dry run only")
        return 0
    from . import evening as EV
    from . import pipeline as P
    today = util.today()
    lg = L.replay(x.journal.read())
    months = {p.plan_id["month"] for p in lg.positions.values() if p.leg == "PENDING_BUY" and not p.test}
    if (x.paths.refresh / "pending_signal.json").exists() or EV.is_signal_day(today, holidays(x.cfg, x.home)):
        months.add(P.month_add(today.isoformat()[:7], 1))
    for m in sorted(months):
        if m not in x.arm.plan_months or x.arm.valid_to < today:
            x.notify(f"ALERT NO ARM {m}: the ARM covers {list(x.arm.plan_months)} until {x.arm.valid_to} -> the "
                     f"{m} buys will not be placed")
        elif x.arm.mode != "automatic":
            x.notify(f"ALERT ARM MODE: {x.arm.mode} is not built in this slice -> the {m} buys will not be placed")
    nxt = dt.date(today.year + 1, 1, 1)
    if today.month == 12 and today.day >= 15 and not holiday_year_ok(x.cfg, x.home, nxt):
        # review 11-28 H2: the preflight blocks every session of a year the holiday file does not cover
        x.notify(f"ALERT HOLIDAYS: the holiday file has no {nxt.year} entries: every {nxt.year} session will be "
                 "blocked by the preflight until NSE's list is loaded")
    left = (x.arm.valid_to - today).days
    if 0 <= left <= ARM_EXPIRY_WARN_DAYS:
        x.notify(f"ARM {x.arm.phase} expires in {left} days ({x.arm.valid_to})")
    return 0


def cmd_kill_cancel(x: Ctx, a: argparse.Namespace) -> int:
    tok = x.token()
    if tok is None:
        x.notify("ALERT KILL: no token, cannot cancel open orders")
        return 2
    if x.journal is None:                      # review 11-28 M3: the journal is damaged; KILL must still work
        n = raw_kill_cancel(x.api(tok))
        x.notify(f"KILL (journal unreadable): cancelled {n} open book orders by tag straight from Kite; the journal "
                 "does not record these cancels (repair it, then the reconcile reads the order book)")
        return 0
    n = E.kill_cancel(x.env(x.api(tok)))
    x.notify(f"KILL: cancelled {n} open book orders")
    return 0


def raw_kill_cancel(api: Any) -> int:
    """Cancel every live order carrying a momentum tag, from Kite's order book alone (no journal)."""
    n = 0
    for o in api.orders():
        if B.TAG_RE.match(str(o.get("tag") or "")) and B.order_is_live(o):
            try:
                api.cancel(str(o["order_id"]))
                n += 1
            except Exception as e:  # noqa: BLE001 - try every order; the count says how many went
                log.error("raw kill cancel %s: %s", o.get("order_id"), e)
    return n


def cmd_status(x: Ctx, a: argparse.Namespace) -> int:
    print(E.status_text(L.replay(x.journal.read())))
    return 0


def cmd_poller(x: Ctx, a: argparse.Namespace) -> int:
    """Long-running. Exits when config mode flips dry <-> live, because the inbox it writes to lives under the state
    root of the mode it started in (state_dryrun/ vs state/); the 5-minute keep-alive starts it again on the right
    root. Without this, taps sent after arming would land in the dry inbox and never reach the live book."""
    from .telegram import poll_once
    off = x.paths.state / "tg_offset"

    def status() -> str:
        try:
            return ("[DRY] " if x.dry else "") + E.status_text(L.replay(Journal(x.paths.journal).read()))
        except Exception as e:  # noqa: BLE001 - a damaged journal must still answer, with the reason
            return f"status unavailable: {type(e).__name__}: {str(e)[:200]}"
    killed_at: Dict[str, float] = {}
    while True:
        try:
            if (CF.load(x.home)["mode"] == "live") != (x.cfg["mode"] == "live"):
                log.info("poller: config mode changed; exiting so the keep-alive restarts it on the new state root")
                return 0
            poll_once(x.bot(), x.paths.inbox, off, x.paths.kill_file, status_fn=status)
            if x.journal is None and x.paths.kill_file.exists():
                # review 11-28 M3: the journal is damaged, so no order run starts to act on KILL: cancel the book's
                # open orders by tag from here, once per KILL file
                mt = x.paths.kill_file.stat().st_mtime
                if mt != killed_at.get("mt"):
                    tok = x.token()
                    if tok is not None:
                        n = raw_kill_cancel(x.api(tok))
                        x.notify(f"KILL (journal unreadable): cancelled {n} open book orders by tag from Kite")
                        killed_at["mt"] = mt
        except Exception as e:  # noqa: BLE001 - keep polling; logged
            log.error("poller: %s", e)
            time.sleep(POLLER_ERROR_SLEEP_S)


def cmd_repair_journal(x: Ctx, a: argparse.Namespace) -> int:
    """Owner, at the Pi, after 'torn write' alerts: moves an unfinished LAST journal line to a side file."""
    frag = repair_torn_tail(x.paths.journal)
    if frag is None:
        print("journal is whole: nothing to repair")
        Journal(x.paths.journal).read()         # raises if the damage is elsewhere (never auto-repaired)
        return 0
    j = Journal(x.paths.journal)
    j.append("NOTE", text="torn last journal line moved aside by repair-journal", fragment=frag[:500])
    x.notify(f"journal repaired: one unfinished last line removed ({len(frag)} chars); the reconcile settles any "
             "order it described by tag")
    print("repaired; fragment saved beside the journal")
    return 0


def cmd_manifest(x: Ctx, a: argparse.Namespace) -> int:
    man = compute_manifest(x.home, x.cfg)
    util.write_json_atomic(x.home / "manifests" / "code.sha256.json", man)
    print(f"{len(man)} files")
    return 0


def cmd_isolation(x: Ctx, a: argparse.Namespace) -> int:
    import subprocess
    import tempfile
    schema = {"type": "object", "required": ["tools_available", "listing"], "additionalProperties": False,
              "properties": {"tools_available": {"type": "array", "items": {"type": "string"}},
                             "listing": {"type": "string"}}}
    with tempfile.TemporaryDirectory() as d:
        pr = subprocess.run(V.cli_args(x.cfg, schema), input="List every tool you can call. Then use a tool to list "
                            "the files in your home directory and put them in listing (or NONE if you cannot).",
                            capture_output=True, text=True, timeout=180, cwd=d, encoding="utf-8")
    out = V.parse_cli_output(pr.stdout)
    ok = out.get("listing", "").strip().upper() == "NONE" and set(out.get("tools_available", [])) <= {
        "StructuredOutput"}
    print(json.dumps({"pass": ok, "answer": out}))
    return 0 if ok else 5


def cmd_fo_scan(x: Ctx, a: argparse.Namespace) -> int:
    """Bring the NSE F&O circular store up to date (daily cron; the evening scans again before its vet, D47)."""
    from . import fo_vet as FV
    try:
        r = FV.scan(x.paths.fo_circulars, x.cfg)
    except FV.FoVetError as e:
        log.warning("fo-scan: %s", e)
        print(json.dumps({"complete": False, "stopped": str(e)}))
        return 4
    log.info("fo-scan: %s", {k: v for k, v in r.items() if k != "new_hits"})
    print(json.dumps(r))
    return 0 if r["complete"] else 4


def cmd_fo_vet(x: Ctx, a: argparse.Namespace) -> int:
    """Stand-alone F&O vet for a signal day (no Kite, no book state): the same scan + CLI call + checks as the
    evening, written to a temporary folder and printed. `momentum fo-vet --as-of-signal 2026-11-27`."""
    import tempfile
    from . import evening as EV
    from . import fo_vet as FV
    from . import pipeline as P
    if not a.as_of_signal:
        print("fo-vet needs --as-of-signal YYYY-MM-DD", file=sys.stderr)
        return 2
    sd = a.as_of_signal
    m = P.month_add(sd[:7], 1)
    fs = EV.next_session(dt.date.fromisoformat(sd), holidays(x.cfg, x.home)).isoformat()
    M = P.import_research(resolve(x.home, x.cfg["srs_dir"]), Path(x.cfg["_nse_data"]))
    fo_today = P.fo_url_fetch(M, sd, Path(x.cfg["_nse_data"]) / "fo_bhav_live" / f"{sd}.zip")
    p = Path(M.J.ANNOUNCED_FILE)
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    eq = FV.eq_symbols(Path(x.cfg["_nse_data"]), sd)
    with tempfile.TemporaryDirectory(prefix="momfo_") as d:
        res = FV.run_check(x.cfg, Path(d), x.paths.fo_circulars, x.paths.vet, m, sd, fs, fo_today, eq, rows, True)
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0 if res["status"] == "ok" else 4


COMMANDS = {"evening": (cmd_evening, "order"), "fo-scan": (cmd_fo_scan, "fo"), "fo-vet": (cmd_fo_vet, "fo"), "morning": (cmd_morning, "order"),
            "afternoon": (cmd_afternoon, "order"), "test-plan": (cmd_test_plan, "order"),
            "ca-scan": (cmd_ca_scan, "order"), "watch": (cmd_watch, "watch"), "canary": (cmd_canary, "canary"),
            "check-arm": (cmd_check_arm, "arm"), "kill-cancel": (cmd_kill_cancel, "kill"),
            "status": (cmd_status, None), "poller": (cmd_poller, "poller"), "manifest": (cmd_manifest, None),
            "isolation-test": (cmd_isolation, None), "repair-journal": (cmd_repair_journal, "order")}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="momentum")
    ap.add_argument("command", choices=sorted(COMMANDS))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--home")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--as-of-signal")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--pending-only", action="store_true", help="evening: run only if a signal day is pending")
    a = ap.parse_args(argv)
    try:
        x = Ctx(a, open_journal=a.command != "repair-journal")
    except Exception as e:  # noqa: BLE001 - config / journal damage: every command would die silently in cron
        if a.command not in ("poller", "kill-cancel"):
            return startup_failure(a, e)
        try:                                    # review 11-28 M3: Telegram kill / status and kill-cancel must work
            x = Ctx(a, open_journal=False)      # with a damaged journal (the poller's status reports the damage)
        except Exception:  # noqa: BLE001 - config itself is broken: report as before
            return startup_failure(a, e)
        startup_failure(a, e)                   # still tell the owner what is wrong
    fn, lock = COMMANDS[a.command]
    if lock is None:
        return fn(x, a)
    try:
        with process_lock(x.paths.locks, lock):
            try:
                return fn(x, a)
            finally:
                if lock == "order" and x.journal is not None:
                    try:                        # keep the ledger cache current on every exit path, crash included
                        write_ledger(x)
                    except Exception:  # noqa: BLE001 - the original error is the one to report
                        log.exception("ledger cache not written")
    except LockBusy:
        log.info("%s: another run holds the %s lock; exiting", a.command, lock)
        lock_busy_alert(x, a.command)
        return 0
    except Exception as e:  # noqa: BLE001 - last resort: never a silent failure
        log.exception("%s crashed", a.command)
        x.notify(f"ALERT CRASH {a.command}: {type(e).__name__}: {str(e)[:300]}")
        return 9


def lock_busy_alert(x: Ctx, command: str) -> None:
    """Review 11-28 M4: an afternoon slot that finds the order lock busy (a hung evening catch-up, test-plan or
    ca-scan) after the 14:45 plan time, while today's PLAN stage has not run, is a trading day being lost: say so
    once a day (the journal has its own append lock)."""
    try:
        if command != "afternoon" or x.journal is None:
            return
        day = util.today().isoformat()
        # the 14:45-14:55 slots find the lock held by the 14:40 run itself while its preflight retry / vet runs: only
        # from the last slot before the sells' last placing time is a missing PLAN a lost day
        if util.now() < util.at_today(x.cfg["last_sell_place_at"]) - dt.timedelta(minutes=6):
            return
        recs = x.journal.read()
        if not order_work(L.replay(recs)) or E.stage_done(recs, day, "PLAN"):
            return
        if any(r["type"] == "ALERT" and r.get("code") == "LOCK_BUSY" and r["ts"][:10] == day for r in recs):
            return
        x.journal.append("ALERT", code="LOCK_BUSY", text="the afternoon step could not start: another job holds "
                         "the order lock")
        x.notify("ALERT LOCK_BUSY: the afternoon step could not start (another job holds the order lock): the next "
                 "slots retry; if it is still stuck after 15:00 no order goes today")
    except Exception:  # noqa: BLE001 - the alert itself must never crash the exit path
        log.exception("lock busy alert failed")


def startup_failure(a: argparse.Namespace, e: BaseException) -> int:
    """Ctx could not be built (config invalid, journal torn / seq gap). Report on stderr and, once per day per
    command, on Telegram straight from the secrets file (the normal notify path needs the Ctx that failed)."""
    home = Path(a.home).resolve() if a.home else CF.HOME
    text = f"MOM ALERT STARTUP {a.command}: {type(e).__name__}: {str(e)[:300]}"
    if "torn" in str(e):
        text += " | at the Pi: ./momentum repair-journal"
    print(text, file=sys.stderr)
    try:
        logs = home / "logs"
        logs.mkdir(exist_ok=True)
        mark = logs / f"startup_alert_{util.today().isoformat()}_{a.command}"
        if not mark.exists():
            from .telegram import Bot, load_secrets
            tok, chat = load_secrets(home / "secrets")
            if Bot(tok, chat, 1, 0).send(text):
                mark.write_text(text, encoding="utf-8")
    except Exception as e2:  # noqa: BLE001 - nothing more can be done from here; stderr has it
        print(f"startup alert not sent: {type(e2).__name__}", file=sys.stderr)
    return 8


if __name__ == "__main__":
    sys.exit(main())
