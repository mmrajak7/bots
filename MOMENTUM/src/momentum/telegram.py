"""Telegram: the momentum book's OWN bot (owner 2026-11-25; FIFTY polls its own bot, so sharing one would steal updates).

Build spec §C12, §I. Secrets: secrets/telegram.json {"bot_token": "...", "chat_id": "..."} (git-ignored, mode 600).
- send(): retried tg_retries times, tg_retry_s apart; returns False when every try failed (automatic steps then do not
  start, D36).
- Buttons carry callback data "a:<8-char approval id>:<choice>" (<= 64 bytes, U18).
- poll_once(): reads updates, keeps only the owner's chat, writes each tap / command to state/inbox/ atomically.
  It NEVER acts on the book itself, with one exception: `kill` writes control/KILL at once (kill can only stop).
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import util

log = logging.getLogger("momentum.telegram")

CMD_RE = re.compile(r"^(kill|hold|release|resolve|filled|ack|status)(?:\s+(.*))?$", re.I)
CB_RE = re.compile(r"^a:([A-Za-z0-9]{8}):([a-z_]{1,16})$")


class TelegramConfigError(Exception):
    pass


def load_secrets(secrets_dir: Path) -> Tuple[str, str]:
    p = secrets_dir / "telegram.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        tok, chat = str(raw["bot_token"]), str(raw["chat_id"])
    except (FileNotFoundError, KeyError, json.JSONDecodeError) as e:
        raise TelegramConfigError(f"{p}: {type(e).__name__}") from e
    if not tok or not chat:
        raise TelegramConfigError(f"{p}: empty token or chat id")
    return tok, chat


class Bot:
    def __init__(self, token: str, chat_id: str, retries: int, retry_s: int, session: Any = None) -> None:
        import requests
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id, self.retries, self.retry_s = str(chat_id), retries, retry_s
        self.http = session or requests.Session()

    def _call(self, method: str, files: Any = None, **data: Any) -> Optional[Dict[str, Any]]:
        for k in range(self.retries):
            try:
                r = self.http.post(f"{self.base}/{method}", data=data, files=files, timeout=30)
                js = r.json()
                if js.get("ok"):
                    return js
                log.warning("telegram %s not ok: %s", method, str(js)[:200])
            except Exception as e:  # noqa: BLE001 - network; retried, then reported to the caller
                log.warning("telegram %s failed: %s", method, type(e).__name__)
            if k + 1 < self.retries:
                time.sleep(self.retry_s)
        return None

    def send(self, text: str, buttons: Optional[Sequence[Tuple[str, str]]] = None) -> bool:
        """buttons: [(label, callback_data)] on one row."""
        data: Dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
        if buttons:
            for _l, cb in buttons:
                if len(cb.encode()) > 64:
                    raise ValueError(f"callback data too long: {cb}")
            data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": l, "callback_data": cb}
                                                                    for l, cb in buttons]]})
        return self._call("sendMessage", **data) is not None

    def send_document(self, path: Path, caption: str) -> bool:
        with open(path, "rb") as f:
            return self._call("sendDocument", files={"document": (path.name, f)}, chat_id=self.chat_id,
                              caption=caption[:1000]) is not None

    def answer_callback(self, cb_id: str, text: str) -> None:
        self._call("answerCallbackQuery", callback_query_id=cb_id, text=text[:190])

    def updates(self, offset: int, timeout: int = 25) -> List[Dict[str, Any]]:
        js = self._call("getUpdates", offset=offset, timeout=timeout, allowed_updates=json.dumps(
            ["message", "callback_query"]))
        return list(js.get("result", [])) if js else []


def new_approval_id() -> str:
    import secrets
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


HELP = ("kill | hold <isin> | release <isin> | resolve <intent> found|absent | filled <intent> <qty> <avg_price> | "
        "ack | status")


def poll_once(bot: Bot, inbox: Path, offset_file: Path, kill_file: Path,
              status_fn: Optional[Callable[[], str]] = None) -> int:
    """One getUpdates round. -> number of inbox items written.
    `status` is read-only, so it is answered here at once (status_fn); every other command and every tap waits in
    the inbox for the order process. One inbox file per update id: an update Telegram hands out twice (crash before
    the offset was saved) is written once."""
    try:
        offset = int(offset_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        offset = 0
    n = 0
    for u in bot.updates(offset):
        offset = max(offset, int(u["update_id"]) + 1)
        item: Optional[Dict[str, Any]] = None
        if "callback_query" in u:
            q = u["callback_query"]
            chat = str(q.get("message", {}).get("chat", {}).get("id", ""))
            mt = CB_RE.match(str(q.get("data", "")))
            if chat != bot.chat_id or not mt:
                log.warning("ignored callback from chat %s", chat)
                continue
            item = {"kind": "tap", "approval_id": mt.group(1), "choice": mt.group(2), "update_id": u["update_id"]}
            bot.answer_callback(q["id"], "received")
        elif "message" in u:
            msg = u["message"]
            chat = str(msg.get("chat", {}).get("id", ""))
            text = str(msg.get("text", "")).strip()
            # Telegram clients send commands as "/status" (and "/start" when a chat is opened): accept the slash form
            # and a "@botname" suffix; /start and help only show the command list.
            text = re.sub(r"^/(\w+)(?:@\w+)?", r"\1", text)
            mt = CMD_RE.match(text)
            if chat != bot.chat_id:
                log.warning("ignored message from chat %s", chat)
                continue
            if text.lower() in ("start", "help"):
                bot.send(f"MOM commands: {HELP}")
                continue
            if not mt:
                bot.send(f"unknown command: {text[:60]} ({HELP})")
                continue
            cmd, args = mt.group(1).lower(), (mt.group(2) or "").strip()
            if cmd == "kill":
                util.write_atomic(kill_file, json.dumps({"by": "telegram", "chat_id": chat,
                                                         "at": util.now().isoformat()}) + "\n")
                bot.send("KILL set: no new momentum orders; open book orders will be cancelled by tag.")
            if cmd == "status" and status_fn is not None:
                bot.send("MOM " + status_fn())
                continue
            item = {"kind": "cmd", "cmd": cmd, "args": args, "update_id": u["update_id"]}
        if item:
            item["received_at"] = util.now().isoformat(timespec="seconds")
            name = f"{int(u['update_id']):015d}.json"      # sorts in arrival order; the same update = the same file
            if (inbox / name).exists() or (inbox / "done" / name).exists():
                continue
            util.write_json_atomic(inbox / name, item)
            n += 1
    util.write_atomic(offset_file, f"{offset}\n")
    return n
