"""Durable per-chat state: which session is selected, and the last block relayed.

Kept in ``~/.amux/telegram-gateway/state.json`` so a gateway restart does not
drop the chat's selected session or re-send the last reply.  Writes are
atomic (temp file + rename): a crash mid-write must not leave an unreadable
state file behind.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

MAX_RELAYED_PER_CHAT = 10


class GatewayState:
    def __init__(self, path: Path | str):
        self.path = Path(path).expanduser()
        self._data: dict = {"chats": {}}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {"chats": {}}
            return
        if isinstance(data, dict) and isinstance(data.get("chats"), dict):
            self._data = data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".state-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _chat(self, chat_id: int) -> dict:
        chats = self._data.setdefault("chats", {})
        return chats.setdefault(str(chat_id), {})

    # ---- selected session ----------------------------------------------

    def selected(self, chat_id: int) -> str | None:
        session = self._chat(chat_id).get("session")
        return session if isinstance(session, str) and session else None

    def select(self, chat_id: int, session: str) -> None:
        self._chat(chat_id)["session"] = session
        self._save()

    def clear(self, chat_id: int) -> None:
        chat = self._chat(chat_id)
        chat.pop("session", None)
        chat.pop("last_relayed", None)
        self._save()

    def selections(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for chat_id, chat in self._data.get("chats", {}).items():
            session = chat.get("session")
            if isinstance(session, str) and session:
                try:
                    out[int(chat_id)] = session
                except ValueError:
                    continue
        return out

    # ---- relay dedupe ---------------------------------------------------

    def last_relayed(self, chat_id: int, session: str) -> str | None:
        relayed = self._chat(chat_id).get("last_relayed")
        if isinstance(relayed, dict):
            value = relayed.get(session)
            return value if isinstance(value, str) else None
        return None

    def set_last_relayed(self, chat_id: int, session: str, text: str) -> None:
        chat = self._chat(chat_id)
        relayed = chat.get("last_relayed")
        if not isinstance(relayed, dict):
            relayed = {}
        relayed[session] = text
        # Bound the file: keep the most recently touched sessions only.
        if len(relayed) > MAX_RELAYED_PER_CHAT:
            for old in list(relayed)[: len(relayed) - MAX_RELAYED_PER_CHAT]:
                relayed.pop(old, None)
        chat["last_relayed"] = relayed
        self._save()
