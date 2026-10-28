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
            # Migration: every topic whose session is pinned gets the id as its
            # stable identity, so a later rename cannot orphan the topic.
            if self._migrate_topic_pins():
                self._save()

    def _migrate_topic_pins(self) -> bool:
        """Write ``topic_pins[thread] = ses_id`` for pin-known bindings.

        Runs once on load (and at the two write sites — ``set_topic`` when a
        pin already exists, ``set_pinned`` when the pin arrives after the
        topic). Entries are added, never removed: the name in ``topics`` stays
        as the fallback binding for when amux is unreachable.
        """
        changed = False
        for chat in self._data.get("chats", {}).values():
            if not isinstance(chat, dict):
                continue
            topics = chat.get("topics")
            pinned = chat.get("pinned")
            if not isinstance(topics, dict) or not isinstance(pinned, dict):
                continue
            pins_map = chat.get("topic_pins")
            pins_map = dict(pins_map) if isinstance(pins_map, dict) else {}
            touched = False
            for thread, session in topics.items():
                pin = pinned.get(session)
                if isinstance(pin, str) and pin and pins_map.get(thread) != pin:
                    pins_map[thread] = pin
                    touched = True
            if touched:
                chat["topic_pins"] = pins_map
                changed = True
        return changed

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
        self,
        chat_id: int,
        thread_id: int,
        session: str,
        name: str | None = None,
        *,
        created: bool = False,
    ) -> None:
        """Bind a forum topic (``message_thread_id``) to ``session``.

        This is what lets the owner just *write in the topic* to continue a
        session: Telegram hands back ``message_thread_id`` on every message,
        and the topic — not a quote — identifies the manager being talked to.

        ``name`` is the topic's display name; it defaults to the session name
        (the gateway's own convention) and is remembered separately because
        Telegram lets two topics share a name while this map may not.

        ``created`` says the GATEWAY minted this topic (the General path), as
        opposed to adopting one the owner made by hand or a manager opened
        through ``POST /topic``. Only a created topic may later be renamed to
        match its OpenCode session title: an adopted topic's recorded name
        falls back to the session name, so without this flag "recorded name ==
        session name" would be true for the owner's own topics too and the
        sync would overwrite names he chose.

        When the session already has a pin, ``topic_pins`` records it beside
        the name — that id is what a later rename resolves through.
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
        made = chat.get("topic_created")
        if not isinstance(made, dict):
            made = {}
        if created:
            made[thread] = True
        else:
            made.pop(thread, None)
        # The stable identity, when the session already has one: the pin is
        # what survives a rename, so it is written alongside the name rather
        # than instead of it (the name stays the unreachable-amux fallback).
        pins_map = chat.get("topic_pins")
        pins_map = pins_map if isinstance(pins_map, dict) else {}
        pin = self.pinned(chat_id, session)
        if pin:
            pins_map[thread] = pin
        else:
            pins_map.pop(thread, None)
        # Bound the file: keep the most recently touched topics only.
        while len(topics) > MAX_TOPICS_PER_CHAT:
            old = next(iter(topics))
            topics.pop(old, None)
            names.pop(old, None)
            made.pop(old, None)
            pins_map.pop(old, None)
        chat["topics"] = topics
        chat["topic_names"] = names
        chat["topic_created"] = made
        chat["topic_pins"] = pins_map
        self._save()

    def topic_pin(self, chat_id: int, thread_id: int) -> str | None:
        """The opencode conversation id this topic is stably bound to.

        ``None`` for a topic bound before pins existed, or one whose session is
        unpinned (another provider, a legacy lane): those resolve by NAME.
        """
        pins_map = self._chat(chat_id).get("topic_pins")
        if not isinstance(pins_map, dict):
            return None
        value = pins_map.get(str(int(thread_id)))
        return value if isinstance(value, str) and value else None

    def topic_created(self, chat_id: int, thread_id: int) -> bool:
        """Did the GATEWAY mint this topic (rather than adopt/name it)?"""
        made = self._chat(chat_id).get("topic_created")
        if not isinstance(made, dict):
            return False
        return made.get(str(int(thread_id))) is True

    def topic_name(self, chat_id: int, thread_id: int) -> str | None:
        """The topic's recorded display name, else ``None`` (never tracked)."""
        names = self._chat(chat_id).get("topic_names")
        if not isinstance(names, dict):
            return None
        value = names.get(str(int(thread_id)))
        return value if isinstance(value, str) and value else None

    def set_topic_name(self, chat_id: int, thread_id: int, name: str) -> None:
        """Record a rename. The Bot API cannot READ a topic's title back, so
        this map is the only record of what a topic is called — and it doubles
        as the rename's dedupe (the sync only acts while the recorded name is
        still the session name)."""
        chat = self._chat(chat_id)
        names = chat.get("topic_names")
        if not isinstance(names, dict):
            names = {}
        names[str(int(thread_id))] = name
        chat["topic_names"] = names
        self._save()

    def release_topic_title(self, chat_id: int, thread_id: int) -> None:
        """Hand a topic's title over to whoever just named it (the owner).

        The OpenCode title sync only edits a topic it MINTED (`topic_created`),
        and dedupes on "recorded name == session name". A session rename
        satisfies that identity again — so without this release the sync would
        retitle the topic back to OpenCode's session title on its next tick and
        silently undo the owner's rename. Popping the flag says: this title now
        belongs to the human, stop following.
        """
        chat = self._chat(chat_id)
        made = chat.get("topic_created")
        if not isinstance(made, dict) or str(int(thread_id)) not in made:
            return
        made.pop(str(int(thread_id)), None)
        chat["topic_created"] = made
        self._save()

    def topic_session(self, chat_id: int, thread_id: int | None) -> str | None:
        """The NAME this topic is bound to (the fallback identity).

        Not the resolved live session: a pin-bound topic can name a lane that
        was renamed after the binding was written. The caller that must talk to
        the fleet resolves through ``topic_pin`` first — see the gateway's
        topic resolver.
        """
        if thread_id is None:
            return None
        topics = self._chat(chat_id).get("topics")
        if not isinstance(topics, dict):
            return None
        session = topics.get(str(int(thread_id)))
        return session if isinstance(session, str) and session else None

    def session_topic(self, chat_id: int, session: str) -> int | None:
        """The thread id ``session`` answers in, or ``None`` (no topic).

        The PIN is looked for first: after a rename the name in ``topics`` can
        be one step behind (a rename made outside the gateway, which has no
        cascade to run), while ``topic_pins`` still names the conversation this
        lane carries. The name match below is the fallback for unpinned and
        legacy sessions — and the safety net for a topic bound by name only.
        """
        chat = self._chat(chat_id)
        pin = self.pinned(chat_id, session)
        topics = chat.get("topics")
        topics = topics if isinstance(topics, dict) else {}
        if pin:
            pins_map = chat.get("topic_pins")
            if isinstance(pins_map, dict):
                for thread, value in pins_map.items():
                    if value == pin:
                        try:
                            return int(thread)
                        except ValueError:
                            continue
        for thread, name in topics.items():
            if name == session:
                try:
                    return int(thread)
                except ValueError:
                    continue
        return None

    def rename_session(self, chat_id: int, old: str, new: str) -> list[int]:
        """Rekey every session-keyed map in this chat from ``old`` to ``new``.

        The rename cascade the panel's ✏️ runs after ``PATCH config
        {"rename": …}`` succeeds — and the same repair when a rename is made
        OUTSIDE the gateway (dashboard/CLI): without it the topic survives the
        rename (it is pin-bound) while the turn anchor, activity stamps, pins,
        dedupe and quote map keep addressing a name the fleet no longer has.

        Returns the threads bound to the session (by the new name OR by pin),
        so the caller can follow the Telegram topic title along. ``topic_names``
        is deliberately NOT rewritten here: it is the only record of what a
        topic is actually called in Telegram, and the caller updates it when
        (and only when) the title edit lands.
        """
        if not old or not new or old == new:
            return []
        chat = self._chat(chat_id)
        changed = False
        if chat.get("session") == old:
            chat["session"] = new
            changed = True
        for key in ("pinned", "activity", "idle_stopped", "turn_msg", "last_relayed"):
            mapping = chat.get(key)
            if isinstance(mapping, dict) and old in mapping:
                mapping[new] = mapping.pop(old)
                changed = True
        topics = chat.get("topics")
        if isinstance(topics, dict):
            for thread, value in list(topics.items()):
                if value == old:
                    topics[thread] = new
                    changed = True
        mapping = chat.get("msg_sessions")
        if isinstance(mapping, dict):
            for message_id, value in list(mapping.items()):
                if value == old:
                    mapping[message_id] = new
                    changed = True
        if changed:
            self._save()

        pin = self.pinned(chat_id, new)
        threads: set[int] = set()
        if isinstance(topics, dict):
            for thread, value in topics.items():
                if value == new:
                    try:
                        threads.add(int(thread))
                    except ValueError:
                        continue
        if pin:
            pins_map = chat.get("topic_pins")
            if isinstance(pins_map, dict):
                for thread, value in pins_map.items():
                    if value == pin:
                        try:
                            threads.add(int(thread))
                        except ValueError:
                            continue
        return sorted(threads)

    def rebind_pin(
        self, chat_id: int, pin: str, live_name: str
    ) -> tuple[str, list[int]] | None:
        """Catch this chat's maps up with a lane renamed OUTSIDE the gateway.

        Returns ``(stale_name, threads)`` that were rewritten, or ``None``
        when nothing needed to move. Only runs when the stored name is NOT a
        live session — the guard that keeps a rename from stealing maps that
        belong to a different, live lane of the same name.
        """
        if not pin or not live_name:
            return None
        pins = self._chat(chat_id).get("pinned")
        if not isinstance(pins, dict):
            return None
        stale = next(
            (name for name, value in pins.items() if value == pin and name != live_name),
            None,
        )
        if not isinstance(stale, str) or not stale:
            return None
        threads = self.rename_session(chat_id, stale, live_name)
        return stale, threads

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
                    bound = isinstance(hit, dict) and session in hit.values()
                    if not bound:
                        # Pin-bound: the name in `topics` may already be stale,
                        # but the conversation id still points at this chat.
                        pinned = chat.get("pinned")
                        pin = (
                            pinned.get(session)
                            if isinstance(pinned, dict)
                            else None
                        )
                        pins_map = chat.get("topic_pins")
                        bound = bool(pin) and isinstance(pins_map, dict) and pin in pins_map.values()
                    if not bound:
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

        Matches by name AND by pin, because a pin-bound topic's name can be
        stale (rename made outside the gateway) and a topic left bound to a
        name nobody has would never be released.
        """
        chat = self._chat(chat_id)
        topics = chat.get("topics")
        if not isinstance(topics, dict):
            return []
        names = chat.get("topic_names")
        names = names if isinstance(names, dict) else None
        made = chat.get("topic_created")
        made = made if isinstance(made, dict) else None
        pins_map = chat.get("topic_pins")
        pins_map = pins_map if isinstance(pins_map, dict) else None
        pinned = chat.get("pinned")
        pin = pinned.get(session) if isinstance(pinned, dict) else None
        bound = {t for t, name in topics.items() if name == session}
        if pin and pins_map is not None:
            bound |= {t for t, value in pins_map.items() if value == pin}
        dropped = []
        for thread in sorted(bound):
            topics.pop(thread, None)
            if names is not None:
                names.pop(thread, None)
            if made is not None:
                made.pop(thread, None)
            if pins_map is not None:
                pins_map.pop(thread, None)
            try:
                dropped.append(int(thread))
            except ValueError:
                continue
        if dropped:
            chat["topics"] = topics
            if names is not None:
                chat["topic_names"] = names
            if made is not None:
                chat["topic_created"] = made
            if pins_map is not None:
                chat["topic_pins"] = pins_map
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
        # The topic bound to this session learns its stable identity. This is
        # the usual order — the topic is created BEFORE the lane exists, so the
        # binding was written as a name and only now becomes a pin.
        topics = chat.get("topics")
        pins_map = chat.get("topic_pins")
        pins_map = pins_map if isinstance(pins_map, dict) else {}
        touched = False
        if isinstance(topics, dict):
            for thread, value in topics.items():
                if value == session and pins_map.get(thread) != oc_session:
                    pins_map[thread] = oc_session
                    touched = True
        if touched:
            chat["topic_pins"] = pins_map
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
