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
import time
from pathlib import Path

MAX_RELAYED_PER_CHAT = 10
MAX_MSG_SESSIONS = 300
MAX_TOPICS_PER_CHAT = 200
# Activity stamps, pins and idle markers are keyed by session name: bounded the
# same way the message map is, so a long-lived chat cannot grow state.json.
MAX_ACTIVITY_PER_CHAT = 300


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

    # ---- message -> session map (the thread rule) -----------------------

    def record_message(self, chat_id: int, message_id: int, session: str) -> None:
        """Remember that a message we sent belongs to ``session``.

        This is what makes a quote-reply land in the right session: Telegram
        hands back ``reply_to_message.message_id``, and that id is the only
        durable link between a chat message and the lane that produced it.
        """
        chat = self._chat(chat_id)
        mapping = chat.get("msg_sessions")
        if not isinstance(mapping, dict):
            mapping = {}
        mapping[str(message_id)] = session
        while len(mapping) > MAX_MSG_SESSIONS:
            mapping.pop(next(iter(mapping)))
        chat["msg_sessions"] = mapping
        self._save()

    def session_for_message(self, chat_id: int, message_id: int) -> str | None:
        mapping = self._chat(chat_id).get("msg_sessions")
        if not isinstance(mapping, dict):
            return None
        session = mapping.get(str(message_id))
        return session if isinstance(session, str) and session else None

    # ---- forum topics: thread_id -> session (the topic rule) -----------

    def set_topic(
        self, chat_id: int, thread_id: int, session: str, name: str | None = None
    ) -> None:
        """Bind a forum topic (``message_thread_id``) to ``session``.

        This is what lets the owner just *write in the topic* to continue a
        session: Telegram hands back ``message_thread_id`` on every message,
        and the topic — not a quote — identifies the manager being talked to.

        ``name`` is the topic's display name; it defaults to the session name
        (the gateway's own convention) and is remembered separately because
        Telegram lets two topics share a name while this map may not.
        """
        chat = self._chat(chat_id)
        topics = chat.get("topics")
        if not isinstance(topics, dict):
            topics = {}
        thread = str(int(thread_id))
        topics[thread] = session
        names = chat.get("topic_names")
        if not isinstance(names, dict):
            names = {}
        names[thread] = name if isinstance(name, str) and name else session
        # Bound the file: keep the most recently touched topics only.
        while len(topics) > MAX_TOPICS_PER_CHAT:
            old = next(iter(topics))
            topics.pop(old, None)
            names.pop(old, None)
        chat["topics"] = topics
        chat["topic_names"] = names
        self._save()

    def topic_session(self, chat_id: int, thread_id: int | None) -> str | None:
        if thread_id is None:
            return None
        topics = self._chat(chat_id).get("topics")
        if not isinstance(topics, dict):
            return None
        session = topics.get(str(int(thread_id)))
        return session if isinstance(session, str) and session else None

    def session_topic(self, chat_id: int, session: str) -> int | None:
        """The thread id ``session`` answers in, or ``None`` (no topic)."""
        topics = self._chat(chat_id).get("topics")
        if not isinstance(topics, dict):
            return None
        for thread, name in topics.items():
            if name == session:
                try:
                    return int(thread)
                except ValueError:
                    continue
        return None

    def chat_for_session(self, session: str) -> int | None:
        """The chat this session lives in, or ``None`` (no chat has it).

        The reverse of the topic/turn maps: it lets a caller that knows only a
        session name — a manager asking the control endpoint for a topic —
        address the right chat without having been told its id. A chat that
        holds the session in a topic wins over one that only holds a turn.
        """
        chats = self._data.get("chats", {})
        for pass_topics in (True, False):
            for chat_id, chat in chats.items():
                if not isinstance(chat, dict):
                    continue
                if pass_topics:
                    hit = chat.get("topics")
                    if not (isinstance(hit, dict) and session in hit.values()):
                        continue
                else:
                    hit = chat.get("turn_msg")
                    if not (isinstance(hit, dict) and session in hit):
                        continue
                try:
                    return int(chat_id)
                except ValueError:
                    continue
        return None

    def thread_for_topic_name(self, chat_id: int, name: str) -> int | None:
        """The thread of the topic displayed as ``name``, else ``None``.

        Used to refuse a second topic with a name the gateway already gave to
        another one. Entries written before names were tracked fall back to
        the session-name convention, which is what every gateway-created topic
        used.
        """
        chat = self._chat(chat_id)
        names = chat.get("topic_names")
        names = names if isinstance(names, dict) else {}
        for thread, value in names.items():
            if value == name:
                try:
                    return int(thread)
                except ValueError:
                    continue
        topics = chat.get("topics")
        if isinstance(topics, dict):
            for thread, value in topics.items():
                if thread in names:
                    continue  # known by its display name above, not by session
                if value == name:
                    try:
                        return int(thread)
                    except ValueError:
                        continue
        return None

    def drop_session_topics(self, chat_id: int, session: str) -> list[int]:
        """Release every topic bound to ``session``; returns the thread ids.

        Used when a session dies (create failure, 404 send, confirmed stop):
        the topic must become adoptable again, so the owner's next message in
        it starts a fresh manager instead of hitting a dead lane forever.
        """
        chat = self._chat(chat_id)
        topics = chat.get("topics")
        if not isinstance(topics, dict):
            return []
        names = chat.get("topic_names")
        names = names if isinstance(names, dict) else None
        dropped = []
        for thread in [t for t, name in topics.items() if name == session]:
            topics.pop(thread, None)
            if names is not None:
                names.pop(thread, None)
            try:
                dropped.append(int(thread))
            except ValueError:
                continue
        if dropped:
            chat["topics"] = topics
            if names is not None:
                chat["topic_names"] = names
            self._save()
        return dropped

    # ---- pin + idle lifecycle (idle stop, same-session revive) -----------

    def set_pinned(self, chat_id: int, session: str, oc_session: str) -> None:
        """Record the OpenCode conversation this lane is pinned to.

        Only an opencode manager gets a pin, and ONLY a pinned session is ever
        idle-stopped — the pin is what makes "stop then start" resume the same
        conversation instead of opening a new one, so an unpinned (legacy, or
        another provider) lane keeps today's behaviour of running forever.
        """
        chat = self._chat(chat_id)
        pins = chat.get("pinned")
        if not isinstance(pins, dict):
            pins = {}
        pins[session] = oc_session
        chat["pinned"] = pins
        self._save()

    def pinned(self, chat_id: int, session: str) -> str | None:
        pins = self._chat(chat_id).get("pinned")
        if not isinstance(pins, dict):
            return None
        value = pins.get(session)
        return value if isinstance(value, str) and value else None

    def touch_activity(
        self, chat_id: int, session: str, kind: str = "owner", at: float | None = None
    ) -> None:
        """Stamp the last time a session was ACTIVE: ``owner`` (a prompt we
        delivered) or ``output`` (new text the relay sent).

        Both count, per the owner's call on 2026-10-27 ("Yes, agent output
        counts as activity") — a lane still producing answers is never idle.
        """
        chat = self._chat(chat_id)
        activity = chat.get("activity")
        if not isinstance(activity, dict):
            activity = {}
        entry = activity.get(session)
        if not isinstance(entry, dict):
            entry = {}
        entry[kind if kind in ("owner", "output") else "owner"] = float(
            time.time() if at is None else at
        )
        activity[session] = entry
        while len(activity) > MAX_ACTIVITY_PER_CHAT:
            activity.pop(next(iter(activity)), None)
        chat["activity"] = activity
        self._save()

    def last_activity(self, chat_id: int, session: str) -> float | None:
        """The newest of the owner/output stamps, or ``None`` if never seen."""
        entry = self._chat(chat_id).get("activity")
        if not isinstance(entry, dict):
            return None
        value = entry.get(session)
        if not isinstance(value, dict) or not value:
            return None
        stamps = [float(v) for v in value.values() if isinstance(v, (int, float))]
        return max(stamps) if stamps else None

    def mark_idle_stopped(
        self, chat_id: int, session: str, at: float | None = None
    ) -> None:
        """The sweeper stopped this lane; the topic binding is NOT released.

        The marker is what lets the relay skip the dead pane and the next
        prompt start the same lane again instead of sending into a stopped one.
        """
        chat = self._chat(chat_id)
        stopped = chat.get("idle_stopped")
        if not isinstance(stopped, dict):
            stopped = {}
        stopped[session] = float(time.time() if at is None else at)
        while len(stopped) > MAX_ACTIVITY_PER_CHAT:
            stopped.pop(next(iter(stopped)), None)
        chat["idle_stopped"] = stopped
        self._save()

    def clear_idle_stopped(self, chat_id: int, session: str) -> None:
        chat = self._chat(chat_id)
        stopped = chat.get("idle_stopped")
        if isinstance(stopped, dict) and session in stopped:
            stopped.pop(session, None)
            chat["idle_stopped"] = stopped
            self._save()

    def idle_stopped(self, chat_id: int, session: str) -> float | None:
        stopped = self._chat(chat_id).get("idle_stopped")
        if not isinstance(stopped, dict):
            return None
        value = stopped.get(session)
        return float(value) if isinstance(value, (int, float)) else None

    def tracked_sessions(self) -> dict[int, set[str]]:
        """Every session the gateway knows about, for the idle sweep.

        Superset of :meth:`sessions_to_relay`: selections, thread anchors,
        topic bindings, pins, activity stamps and idle markers all count, so a
        session cannot fall out of the sweep just because its anchor was
        trimmed.
        """
        out: dict[int, set[str]] = {}

        def add(chat_id, session):
            if isinstance(session, str) and session:
                out.setdefault(int(chat_id), set()).add(session)

        for chat_id, session in self.selections().items():
            add(chat_id, session)
        for chat_id, chat in self._data.get("chats", {}).items():
            for key in ("turn_msg", "activity", "pinned", "idle_stopped"):
                values = chat.get(key)
                if isinstance(values, dict):
                    for session in values:
                        add(chat_id, session)
            topics = chat.get("topics")
            if isinstance(topics, dict):
                for session in topics.values():
                    add(chat_id, session)
        return out

    # ---- turn tracking (what the relay replies to) ----------------------

    def set_turn_message(
        self, chat_id: int, session: str, message_id: int, thread: int | None = None
    ) -> None:
        """Anchor of the current turn: its id AND the thread it was posted in.

        ``thread`` (forum topic id, ``None`` outside a topic) is what makes
        cross-thread replies impossible: the relay only reuses an anchor whose
        thread matches the session's topic, otherwise it posts the answer into
        the topic unanchored rather than replying into the wrong thread.
        """
        chat = self._chat(chat_id)
        turns = chat.get("turn_msg")
        if not isinstance(turns, dict):
            turns = {}
        turns[session] = {
            "id": int(message_id),
            "thread": int(thread) if thread is not None else None,
        }
        chat["turn_msg"] = turns
        self._save()

    def turn_message(self, chat_id: int, session: str) -> int | None:
        value = self._turn(chat_id, session)
        if isinstance(value, dict):
            value = value.get("id")
        return int(value) if isinstance(value, int) else None

    def turn_thread(self, chat_id: int, session: str) -> int | None:
        """The thread the current turn's anchor was posted in (``None`` if not
        in a topic, or a legacy entry written before threads were tracked)."""
        value = self._turn(chat_id, session)
        if not isinstance(value, dict):
            return None
        thread = value.get("thread")
        return int(thread) if isinstance(thread, int) else None

    def _turn(self, chat_id: int, session: str):
        turns = self._chat(chat_id).get("turn_msg")
        if not isinstance(turns, dict):
            return None
        return turns.get(session)

    def sessions_to_relay(self) -> dict[int, set[str]]:
        """Every session this chat should have relayed, both modes.

        Mode 1 keeps a *selected* session; manager mode keeps one session per
        conversation thread (the ``turn_msg`` map). A session appears here as
        soon as it exists in either, so an answer is never dropped because the
        chat selected something else later.
        """
        out: dict[int, set[str]] = {}
        for chat_id, session in self.selections().items():
            out.setdefault(chat_id, set()).add(session)
        for chat_id, chat in self._data.get("chats", {}).items():
            turns = chat.get("turn_msg")
            if not isinstance(turns, dict):
                continue
            try:
                cid = int(chat_id)
            except ValueError:
                continue
            for session in turns:
                if isinstance(session, str) and session:
                    out.setdefault(cid, set()).add(session)
        return out

    # ---- manager agent choice -------------------------------------------

    def set_manager(self, chat_id: int, provider: str, model: str | None = None) -> None:
        chat = self._chat(chat_id)
        chat["manager"] = {"provider": provider, "model": model}
        self._save()

    def manager(self, chat_id: int, defaults: dict | None = None) -> dict:
        value = self._chat(chat_id).get("manager")
        if isinstance(value, dict) and value.get("provider"):
            return {"provider": value["provider"], "model": value.get("model")}
        return dict(defaults or {})
