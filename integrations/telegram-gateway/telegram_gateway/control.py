"""The gateway's own topic-creation path — the only one a manager may use.

A manager is an agent with a shell and, if it goes looking, the bot token.
The tempting shortcut is a raw Bot API ``createForumTopic`` call. That topic
would be invisible to the gateway: nothing binds it to a session in
``state.json``, so the owner's first message in it starts ANOTHER manager (the
adoption path), and the relay has no idea where the answers belong. The
2026-10-27 incident ended with five topics for one request for exactly this
reason.

This module is the sanctioned replacement. One localhost endpoint does the
three things in the right order:

1. create the topic (Bot API, same call the gateway makes for General),
2. bind it in the gateway's own ``GatewayState`` — the only writer of
   ``state.json`` while the gateway runs, because its in-memory copy clobbers
   any hand edit on the next save,
3. log ``topic_created … source=control``.

A second route, ``POST /message``, delivers a caller-authored message into
the topic BOUND to a session (owner, 2026-10-31): the dispatcher's report on
a dispatch lands in the worker's own topic, looked up from the gateway's
binding — the caller never names a thread.

``POST /topic`` also carries the owner's DEFAULT connect behavior
(2026-11-02): ``reconnect`` verifies an existing binding by landing the
context into it — a deleted topic is released and recreated — and ``intro``
opens the topic with the conversation's last messages, so "where we stopped"
is the first thing in it.

Transport: a tiny asyncio HTTP/1.1 server on ``127.0.0.1`` inside the running
gateway process — no framework, no second process, no shared file. It is
unauthenticated and loopback-only (single-owner machine); ``control_port=0``
disables it entirely. See ``memory-bank/progress/topic-dedup-and-tracked-creation.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging

from telegram.error import TelegramError

from . import opencode_api
from .amux import AmuxError, session_id_from_flags
from .menu import INTRO_MAX_CHARS, INTRO_MAX_MESSAGES
from .relay import assistant_texts, thread_missing

log = logging.getLogger("telegram_gateway")

MAX_BODY = 64 * 1024
READ_TIMEOUT = 5.0
REASONS = {
    200: "OK",
    201: "Created",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


async def _fetch_intro(app, pin: str, name: str) -> str | None:
    """The conversation's last completed answers, as a topic's opening context.

    The owner's default (2026-11-02): *"recreate the topic and fetch the
    latest message so I know where we stopped."* Reads the SHARED serve
    (``GET /api/session/<pin>/message``, newest-first) through the same
    client the relay uses, takes the last :data:`INTRO_MAX_MESSAGES`
    completed assistant texts oldest-first, bounded to
    :data:`INTRO_MAX_CHARS`. Every failure is a WARN and ``None``: an intro
    is a nicety, it never refuses a topic that must exist.
    """
    if not pin:
        return None
    cfg = app.bot_data.get("cfg")
    if cfg is None:  # a test app without config skips the nicety
        return None
    api = app.bot_data.get("oc_api") or opencode_api
    client = None
    try:
        client = await api.client_for_service(
            url=cfg.opencode_service_url,
            password=cfg.opencode_service_password,
            service_json=cfg.opencode_service_json,
            state_json=cfg.opencode_service_state,
        )
        messages = await client.messages(pin, max_pages=1)
    except AmuxError as exc:
        log.warning(
            "topic_intro_failed pin=%s status=%s detail=%s",
            pin,
            exc.status,
            exc.detail,
        )
        return None
    except Exception as exc:  # a surprise must degrade, never explode
        log.warning("topic_intro_failed pin=%s detail=%s", pin, exc)
        return None
    finally:
        if client is not None:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
    texts = assistant_texts(messages, limit=INTRO_MAX_MESSAGES)
    if not texts:
        return None
    body = "\n\n".join(texts)[:INTRO_MAX_CHARS]
    return f"📂 {name} — last messages before the pause:\n\n{body}"


# The verdicts of a ``reconnect`` attempt against an EXISTING binding.
LANDED = "landed"  # context posted into the live topic
DEAD = "dead"  # proven gone: release + recreate
FAILED = "failed"  # Telegram refused for another reason: keep the binding
SILENT = "silent"  # alive (or unproven), nothing to land


async def _land_context(
    app, chat_id: int, thread: int, session: str,
    prompt, intro: bool, pin: str | None, name: str,
) -> str:
    """Land the caller's context into an existing topic — and classify it.

    The SEND is the detector (there is no message-getter in the Bot API):
    a content send into a dead thread raises "Message thread not found" and
    delivered nothing, so the caller can release + recreate without any
    duplicate risk. With no content to send, a ``sendChatAction`` probe
    stands in (no message, no content) — if Telegram ever stops validating
    the thread there, the caller degrades to today's behavior, never worse.
    """
    parts = []
    if isinstance(prompt, str) and prompt.strip():
        parts.append(prompt.strip())
    if intro and pin:
        intro_text = await _fetch_intro(app, pin, name)
        if intro_text:
            parts.append(intro_text)
    content = "\n\n".join(parts) if parts else None
    if content:
        try:
            sent = await app.bot.send_message(
                chat_id, content, message_thread_id=thread
            )
        except TelegramError as exc:
            if thread_missing(exc):
                return DEAD
            log.warning(
                "topic_prompt_echo_failed session=%s thread=%s error=%s",
                session,
                thread,
                exc,
            )
            return FAILED
        message_id = getattr(sent, "message_id", None)
        if message_id is not None:
            # Recorded so quoting the context continues the session — but
            # deliberately NOT `set_turn_message`: a reconnect must never
            # hijack the anchor of a turn that is already in flight.
            state = app.bot_data["state"]
            state.record_message(chat_id, int(message_id), session)
        log.info(
            "topic_prompt_echoed session=%s thread=%s msg_id=%s chars=%s mode=reconnect",
            session,
            thread,
            message_id,
            len(content),
        )
        return LANDED
    try:
        await app.bot.send_chat_action(
            chat_id, action="typing", message_thread_id=thread
        )
    except TelegramError as exc:
        if thread_missing(exc):
            return DEAD
        log.debug(
            "topic_probe_inconclusive session=%s thread=%s error=%s",
            session,
            thread,
            exc,
        )
    return SILENT


async def create_topic(app, payload: dict) -> tuple[int, dict]:
    """Create a forum topic, bind it to ``session``, log it. Idempotent.

    Optional ``prompt`` (the dispatcher's spawn call): the task text is
    echoed plain into the NEW topic and becomes the session's turn anchor, so
    the owner can scroll up to what was asked and the first answer replies to
    it. The ``201`` body reports ``echoed``; an echo failure WARNs but never
    refuses the spawn. The topic is marked eligible for the title sync: born
    with the ``name`` you gave it, renamed ONCE to the session's OpenCode
    title when that title lands (owner, "I want", 2026-10-30) — and never
    again.

    Two opt-in flags (owner, 2026-11-02 — the DEFAULT connect behavior):

    - ``reconnect``: the session may already have a topic and that topic may
      be DELETED in Telegram. The existing binding is verified by landing
      the context into it (the send is the detector): alive → ``echoed:true``;
      dead → the stale binding is released (``topic_released …
      reason=thread_missing source=connect``) and a FRESH topic is created —
      ``recreated:true`` in the ``201`` body. Only "thread not found" proves
      death; any other error keeps the binding.
    - ``intro``: fetch the pin's last messages from the shared serve and
      open the topic with "where it stopped" — implied whenever a dead topic
      is recreated (the owner's default). A failed fetch degrades to the
      prompt alone (WARN), never a refusal.

    One behavioral flag (owner, 2026-11-14):

    - ``transcribe_only`` (boolean, optional): the topic becomes a
      TRANSCRIPTION-ONLY topic — voice notes answer with the
      ``🗣️ Transcribed: …`` echo and nothing else, text/photos get one
      visible refusal line, and no session is ever resolved or started there
      (``bot.on_voice``/``on_text``/``on_photo`` check it before any routing).
      ``session`` is still required by this endpoint's contract, so callers
      pass a PLACEHOLDER name (``transcribe-only``) that carries no pin: no
      idle stop, no revive, no title sync, no relay peek. The ``prompt`` is
      still echoed into the topic as its visible description, but it is NOT
      recorded as a turn anchor (a transcribe-only topic routes nothing, so
      it must never enter the relay's turn map). Re-POSTing with the flag on
      an existing binding (the ``reconnect``/``intro`` flows included)
      re-applies it without landing context.

    Returns ``(http status, json body)``; the body always says what happened
    so a manager can tell a real topic from a refused one without reading logs.
    """
    state = app.bot_data["state"]

    session = payload.get("session")
    if not isinstance(session, str) or not session.strip():
        return 400, {"error": "session is required"}
    session = session.strip()

    # A dispatch caller may pass the lane's OpenCode PIN (``ses_…``) as
    # ``session``. The binding's identity is the LANE NAME — a raw pin stored
    # as the name has no pin beside it, so the first prompt resolves to it,
    # 404s and RELEASES the topic (live 2026-11-14, thread 1548:
    # send_failed status=404 → topic_released reason=session_gone, and the
    # owner's answer landed in General). Resolve it to the lane that carries
    # the pin now; a pin no lane carries is refused, never bound.
    if session.startswith("ses_"):
        pin_as_session = session
        lane = None
        lookup = app.bot_data.get("client")
        if lookup is not None:
            try:
                rows = await lookup.list_sessions()
            except Exception:  # noqa: BLE001 — unreachable amux → clear 400 below
                rows = []
            for row in rows:
                if isinstance(row, dict) and session_id_from_flags(row.get("flags")) == pin_as_session:
                    lane = row.get("name")
                    break
        if not lane:
            return 400, {
                "error": "session is an OpenCode id and no lane carries that pin",
                "session": pin_as_session,
            }
        log.info(
            "topic_session_remapped pin=%s session=%s",
            pin_as_session,
            lane,
        )
        session = lane

    prompt = payload.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return 400, {"error": "prompt must be a string"}

    reconnect = bool(payload.get("reconnect"))
    intro = bool(payload.get("intro"))

    transcribe_only = payload.get("transcribe_only")
    if transcribe_only is not None and not isinstance(transcribe_only, bool):
        return 400, {"error": "transcribe_only must be a boolean"}
    transcribe_only = bool(transcribe_only)

    name = payload.get("name")
    if name is None or name == "":
        name = session
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name must be a non-empty string"}
    name = name.strip()

    chat_id = payload.get("chat_id")
    if chat_id is None:
        # A manager knows its own session name, not the group's chat id.
        chat_id = state.chat_for_session(session)
        if chat_id is None:
            return 400, {
                "error": "chat_id required: no chat has this session yet",
                "session": session,
            }
    else:
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return 400, {"error": "chat_id must be an integer"}

    # The lane's PIN, looked up ONCE, early: the intro needs it BEFORE the
    # echo, and the bind records it after the create (11.9a). A caller that
    # spawns the lane first (every shipped flow does) finds it here; a
    # name-only session stays exactly as bindable as before.
    pin = None
    client = app.bot_data.get("client")
    if client is not None:
        try:
            row = await client.find(session)
        except Exception:  # noqa: BLE001 — a name-only bind stays valid
            log.debug("topic_pin_unknown session=%s", session)
        else:
            pin = session_id_from_flags(row.get("flags"))

    # Idempotent: the session already has its topic — report it, create
    # nothing. With `reconnect`/`intro` the existing topic is VERIFIED (and
    # its context landed) before that report is trusted.
    recreated = False
    existing = state.session_topic(chat_id, session)
    if existing is not None and transcribe_only:
        # The flag belongs to the TOPIC: a reconnect/intro flow must not drop
        # it, and a topic flagged after the fact stops the title sync from
        # ever touching it (release spends the "not yet titled" flag).
        state.set_transcribe_only(chat_id, existing, True)
        state.release_topic_title(chat_id, existing)
        log.info(
            "topic_transcribe_only chat_id=%s thread=%s session=%s",
            chat_id,
            existing,
            session,
        )
    if existing is not None:
        if reconnect or intro:
            # A transcribe-only topic LANS nothing: with no prompt and no
            # intro the call degenerates to the sendChatAction probe — the
            # dead-thread detector still runs (and can still recreate), but
            # no context is ever dropped into a topic that routes nothing.
            verdict = await _land_context(
                app,
                chat_id,
                existing,
                session,
                None if transcribe_only else prompt,
                intro and not transcribe_only,
                pin,
                name,
            )
            if verdict == LANDED:
                return 200, {
                    "thread": existing,
                    "session": session,
                    "name": name,
                    "created": False,
                    "echoed": True,
                    "recreated": False,
                    "transcribe_only": transcribe_only,
                }
            if verdict == FAILED:
                return 200, {
                    "thread": existing,
                    "session": session,
                    "name": name,
                    "created": False,
                    "echoed": False,
                    "recreated": False,
                    "transcribe_only": transcribe_only,
                }
            if verdict == DEAD:
                for freed in state.drop_session_topics(chat_id, session):
                    log.info(
                        "topic_released chat_id=%s thread=%s session=%s "
                        "reason=thread_missing source=connect",
                        chat_id,
                        freed,
                        session,
                    )
                recreated = True  # fall through to a fresh create
        if not recreated:
            log.info(
                "topic_exists chat_id=%s thread=%s session=%s",
                chat_id,
                existing,
                session,
            )
            return 200, {
                "thread": existing,
                "session": session,
                "name": name,
                "created": False,
                "echoed": False,
                "recreated": False,
                "transcribe_only": transcribe_only,
            }

    # Telegram happily allows two topics with one name; we do not, because the
    # gateway's map is keyed by name and could not tell them apart.
    holder = state.thread_for_topic_name(chat_id, name)
    if holder is not None:
        return 409, {
            "error": "name_exists",
            "name": name,
            "thread": holder,
            "session": state.topic_session(chat_id, holder),
        }

    try:
        topic = await app.bot.create_forum_topic(chat_id, name=name)
    except TelegramError as exc:
        log.warning(
            "topic_create_failed chat_id=%s name=%s error=%s", chat_id, name, exc
        )
        return 502, {"error": str(exc)}

    thread = getattr(topic, "message_thread_id", None)
    if thread is None:
        log.warning(
            "topic_create_failed chat_id=%s name=%s detail=no thread id", chat_id, name
        )
        return 502, {"error": "telegram returned no thread id"}

    # Record the lane's PIN if it has one (11.9): `/topic` used to bind by
    # name only, which left `state.pinned` unset — and with it the API relay
    # blind (`_relay_pin` reads it), so a bound session's outputs relayed
    # only through the pane sentinel, never the structured channel that sees
    # turns driven from a dashboard or the CLI. `set_pinned` BEFORE
    # `set_topic`, so the binding writes `topic_pins` too. The pin was
    # resolved ONCE at the top (it also feeds the intro).
    if pin and not state.pinned(chat_id, session):
        state.set_pinned(chat_id, session, pin)
    # `created=True`: the OWNER's 11.8 ruling — this topic is ELIGIBLE for
    # the title sync. The task name is its BIRTH name; once OpenCode titles
    # the worker's conversation (seconds after the first turn), the topic is
    # renamed ONCE to that title (`topic_renamed … source=opencode_title`).
    # Hand-made (adopted) topics still pass False and are never touched.
    # A TRANSCRIBE-ONLY topic is never title-synced either: its placeholder
    # binding carries no pin, and `created=False` spends nothing on a rename
    # that must never happen (the owner named it himself).
    state.set_topic(
        chat_id,
        int(thread),
        session,
        name,
        created=not transcribe_only,
        transcribe_only=transcribe_only,
    )
    log.info(
        "topic_created chat_id=%s thread=%s name=%s source=control", chat_id, thread, name
    )
    if transcribe_only:
        log.info(
            "topic_transcribe_only chat_id=%s thread=%s session=%s",
            chat_id,
            thread,
            session,
        )
    if recreated:
        # The sweep signal for the owner's default (2026-11-02): a deleted
        # topic was found, released and replaced — grep-able as the moment a
        # dead binding healed itself.
        log.info(
            "topic_recreated chat_id=%s thread=%s session=%s reason=thread_missing",
            chat_id,
            thread,
            session,
        )
    echoed = False
    # The echo: the caller's prompt, plus the conversation's last messages
    # when the intro was asked for — or when a DEAD topic is being recreated
    # (the owner's default: the recreated topic opens with "where we stopped").
    parts = []
    if isinstance(prompt, str) and prompt.strip():
        parts.append(prompt.strip())
    if (intro or recreated) and pin and not transcribe_only:
        intro_text = await _fetch_intro(app, pin, name)
        if intro_text:
            parts.append(intro_text)
    body_text = "\n\n".join(parts)[:4000] if parts else None
    if body_text:
        # The context becomes the topic's FIRST message and the session's
        # turn anchor — scrolling up shows WHAT the worker was asked (and
        # where the conversation stood) and its first answer replies to that
        # (the contract the gateway's own minted topics had from the owner's
        # 2026-10-29 ask, now carried by the dispatcher's spawn call and the
        # connect flow). Plain, never parse_mode: the text goes verbatim.
        try:
            sent = await app.bot.send_message(
                chat_id, body_text, message_thread_id=int(thread)
            )
            message_id = getattr(sent, "message_id", None)
            if message_id is not None:
                if transcribe_only:
                    # Visible description ONLY: never an anchor and never a
                    # quote-map entry — a transcribe-only topic routes
                    # nothing, and `set_turn_message` here would put the
                    # placeholder into `sessions_to_relay`, where the relay
                    # would peek a session that does not exist every cycle.
                    log.info(
                        "topic_prompt_echoed session=%s thread=%s msg_id=%s "
                        "chars=%s anchor=none transcribe_only=1",
                        session,
                        thread,
                        message_id,
                        len(body_text),
                    )
                else:
                    state.record_message(chat_id, int(message_id), session)
                    state.set_turn_message(
                        chat_id, session, int(message_id), thread=int(thread)
                    )
                    log.info(
                        "topic_prompt_echoed session=%s thread=%s msg_id=%s chars=%s",
                        session,
                        thread,
                        message_id,
                        len(body_text),
                    )
                echoed = True
        except TelegramError as exc:
            # The topic exists and is bound either way: a failed echo is a
            # WARN, never a refused spawn.
            log.warning(
                "topic_prompt_echo_failed session=%s thread=%s error=%s",
                session,
                thread,
                exc,
            )
    return 201, {
        "thread": int(thread),
        "session": session,
        "name": name,
        "created": True,
        "echoed": echoed,
        "recreated": recreated,
        "transcribe_only": transcribe_only,
    }


async def post_message(app, payload: dict) -> tuple[int, dict]:
    """Deliver a caller-authored message into a session's BOUND topic.

    The dispatcher's half of the dispatch contract (owner, 2026-10-31): when
    it opens a worker's topic and reports on the dispatch, that report must
    land IN the topic — not only in the GM home thread. The dispatcher cannot
    touch the Bot API (that is the whole point of this endpoint), so it
    POSTs the report here and the gateway sends it into the topic bound to
    ``session``.

    The caller never names a thread: the topic is looked up from the
    gateway's own binding (``state.session_topic``), so a stale or foreign
    thread id cannot route a message into the wrong topic. The text goes
    plain (never ``parse_mode`` — the caller's own words, verbatim), the
    sent message id is recorded against the session (quoting the report
    continues that session, the same map every bind uses), and the outcome
    is logged ``topic_message_posted`` for sweeps.

    Returns ``(http status, json body)``; ``409 no_topic`` means the session
    has no topic yet (open one with ``POST /topic`` first) — an honest
    refusal, never a silent drop.
    """
    state = app.bot_data["state"]

    session = payload.get("session")
    if not isinstance(session, str) or not session.strip():
        return 400, {"error": "session is required"}
    session = session.strip()

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return 400, {"error": "text is required"}
    text = text.strip()

    chat_id = payload.get("chat_id")
    if chat_id is None:
        chat_id = state.chat_for_session(session)
        if chat_id is None:
            return 400, {
                "error": "chat_id required: no chat has this session yet",
                "session": session,
            }
    else:
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            return 400, {"error": "chat_id must be an integer"}

    thread = state.session_topic(chat_id, session)
    if thread is None:
        return 409, {
            "error": "no_topic",
            "session": session,
            "detail": "session has no topic bound yet — POST /topic first",
        }

    try:
        sent = await app.bot.send_message(chat_id, text, message_thread_id=thread)
    except TelegramError as exc:
        log.warning(
            "topic_message_failed session=%s thread=%s error=%s", session, thread, exc
        )
        released = False
        if thread_missing(exc):
            # The topic is GONE (measured live 2026-10-31: thread 191 had
            # been deleted, the send answered "Message thread not found").
            # Same lifecycle as the relay's 404-send path: release the
            # binding so the stale name/thread stop holding the map.
            for freed in state.drop_session_topics(chat_id, session):
                released = True
                log.info(
                    "topic_released chat_id=%s thread=%s session=%s "
                    "reason=thread_missing source=message",
                    chat_id,
                    freed,
                    session,
                )
        return 502, {"error": str(exc), **({"released": True} if released else {})}

    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        state.record_message(chat_id, int(message_id), session)
    log.info(
        "topic_message_posted session=%s thread=%s msg_id=%s chars=%s",
        session,
        thread,
        message_id,
        len(text),
    )
    return 201, {
        "thread": int(thread),
        "session": session,
        "msg_id": message_id,
        "chars": len(text),
    }


async def dispatch(app, method: str, path: str, body: bytes) -> tuple[int, dict]:
    """Route one request. Kept separate from the socket so tests can call it."""
    if path in ("/health", "/"):
        if method != "GET":
            return 405, {"error": "method not allowed"}
        return 200, {"status": "ok", "service": "telegram-gateway"}
    if path in ("/topic", "/message"):
        if method != "POST":
            return 405, {"error": f"method not allowed, use POST {path}"}
        if not body.strip():
            payload: object = {}
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return 400, {"error": "body is not valid JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}
        if path == "/topic":
            return await create_topic(app, payload)
        return await post_message(app, payload)
    return 404, {"error": f"no such route: {method} {path}"}


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------


class ControlServer:
    """HTTP/1.1 on loopback. ``port=0`` binds an ephemeral port (tests)."""

    def __init__(self, app, port: int):
        self.app = app
        self.port = port
        self._server: asyncio.AbstractServer | None = None

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", self.port
            )
        except OSError as exc:
            log.warning("control_start_failed port=%s error=%s", self.port, exc)
            return
        self.port = int(self._server.sockets[0].getsockname()[1])
        log.info(
            "control_started host=127.0.0.1 port=%s "
            "routes=POST /topic,POST /message,GET /health",
            self.port,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        log.info("control_stopped port=%s", self.port)

    async def _handle(self, reader, writer) -> None:
        status, payload = 500, {"error": "internal error"}
        method, path = "-", "-"
        try:
            request_line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                status, payload = 400, {"error": "malformed request line"}
            else:
                method, path = parts[0].upper(), parts[1]
                headers: dict[str, str] = {}
                while True:
                    line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT)
                    if not line or line in (b"\r\n", b"\n"):
                        break
                    key, _, value = line.decode("latin-1").partition(":")
                    if key:
                        headers[key.strip().lower()] = value.strip()
                try:
                    length = int(headers.get("content-length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY:
                    status, payload = 413, {"error": "bad content-length"}
                else:
                    body = (
                        await reader.readexactly(length) if length else b""
                    )
                    status, payload = await dispatch(self.app, method, path, body)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            status, payload = 400, {"error": "incomplete request"}
        except OSError:
            status, payload = 400, {"error": "connection lost"}
        except Exception:  # never let one request kill the listener
            log.exception("control_request_failed method=%s path=%s", method, path)
        finally:
            try:
                data = json.dumps(payload).encode("utf-8")
                head = (
                    f"HTTP/1.1 {status} {REASONS.get(status, 'OK')}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(data)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("latin-1")
                writer.write(head + data)
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        log.info(
            "control_request method=%s path=%s status=%s", method, path, status
        )
