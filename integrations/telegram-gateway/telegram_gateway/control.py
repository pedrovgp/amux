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
2. bind it in the gateway's **own** ``GatewayState`` — the only writer of
   ``state.json`` while the gateway runs, because its in-memory copy clobbers
   any hand edit on the next save,
3. log ``topic_created … source=control``.

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


async def create_topic(app, payload: dict) -> tuple[int, dict]:
    """Create a forum topic, bind it to ``session``, log it. Idempotent.

    Optional ``prompt`` (the dispatcher's spawn call): the task text is
    echoed plain into the NEW topic and becomes the session's turn anchor, so
    the owner can scroll up to what was asked and the first answer replies to
    it. The ``201`` body reports ``echoed``; an echo failure WARNs but never
    refuses the spawn.

    Returns ``(http status, json body)``; the body always says what happened
    so a manager can tell a real topic from a refused one without reading logs.
    """
    state = app.bot_data["state"]

    session = payload.get("session")
    if not isinstance(session, str) or not session.strip():
        return 400, {"error": "session is required"}
    session = session.strip()

    prompt = payload.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return 400, {"error": "prompt must be a string"}

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

    # Idempotent: the session already has its topic — report it, create nothing.
    existing = state.session_topic(chat_id, session)
    if existing is not None:
        log.info(
            "topic_exists chat_id=%s thread=%s session=%s", chat_id, existing, session
        )
        return 200, {
            "thread": existing,
            "session": session,
            "name": name,
            "created": False,
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

    state.set_topic(chat_id, int(thread), session, name)
    log.info(
        "topic_created chat_id=%s thread=%s name=%s source=control", chat_id, thread, name
    )
    echoed = False
    if isinstance(prompt, str) and prompt.strip():
        # The task that created the topic becomes its FIRST message and the
        # session's turn anchor — scrolling up shows WHAT the worker was asked
        # and its first answer replies to that (the contract the gateway's own
        # minted topics had from the owner's 2026-10-29 ask, now carried by
        # the dispatcher's spawn call). Plain, never parse_mode: the task text
        # goes verbatim, the caller's own words.
        body_text = prompt.strip()
        try:
            sent = await app.bot.send_message(
                chat_id, body_text, message_thread_id=int(thread)
            )
            message_id = getattr(sent, "message_id", None)
            if message_id is not None:
                state.record_message(chat_id, int(message_id), session)
                state.set_turn_message(chat_id, session, int(message_id), thread=int(thread))
                echoed = True
                log.info(
                    "topic_prompt_echoed session=%s thread=%s msg_id=%s chars=%s",
                    session,
                    thread,
                    message_id,
                    len(body_text),
                )
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
    }


async def dispatch(app, method: str, path: str, body: bytes) -> tuple[int, dict]:
    """Route one request. Kept separate from the socket so tests can call it."""
    if path in ("/health", "/"):
        if method != "GET":
            return 405, {"error": "method not allowed"}
        return 200, {"status": "ok", "service": "telegram-gateway"}
    if path == "/topic":
        if method != "POST":
            return 405, {"error": "method not allowed, use POST /topic"}
        if not body.strip():
            payload: object = {}
        else:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return 400, {"error": "body is not valid JSON"}
        if not isinstance(payload, dict):
            return 400, {"error": "body must be a JSON object"}
        return await create_topic(app, payload)
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
            "control_started host=127.0.0.1 port=%s routes=POST /topic,GET /health",
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
