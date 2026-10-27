"""Telegram bot wiring: menu, prompt capture, voice notes and reply relay."""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .amux import AmuxClient, AmuxError
from .config import Config
from .control import ControlServer
from .guard import Guard, allow_only
from .manager import PROVIDERS, create_manager, new_manager_name
from .menu import (
    PREFIX,
    format_worker_line,
    main_menu,
    manager_keyboard,
    sessions_keyboard,
    status_keyboard,
    stop_confirm,
)
from .relay import chunk_text, delta_since, extract_reply, strip_ansi
from .state import GatewayState
from .voice import groq_transcriber, transcribe_voice

log = logging.getLogger("telegram_gateway")

PROMPT = 1

# A burst of identical messages in General (a client retry, a double tap, a
# forwarded copy) would open one topic per copy: each General message starts a
# fresh manager by design. An identical repeat inside DUP_WINDOW_S is merged
# into the session the first copy started instead.
DUP_WINDOW_S = 30.0
RECENT_STARTS = "recent_starts"
MAX_RECENT_STARTS = 100
# A session in one of these states cannot take the merged prompt.
DEAD_STATUSES = ("stopped", "stopping", "error")

WELCOME = (
    "🤖 *amux gateway*\n"
    "Pick an action below. Replies from your selected session arrive here "
    "automatically."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


def _client(context: ContextTypes.DEFAULT_TYPE) -> AmuxClient:
    return context.application.bot_data["client"]


def _state(context: ContextTypes.DEFAULT_TYPE) -> GatewayState:
    return context.application.bot_data["state"]


def _username(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "unknown"
    return user.username or f"user{user.id}"


async def _say(
    update: Update,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Answer through the callback's message when there is one, else reply.

    Markdown is best-effort: worker names and pane text can contain characters
    Telegram refuses to parse, so a parse failure falls back to plain text
    instead of losing the answer.
    """
    query = update.callback_query
    if query is not None:
        try:
            await query.edit_message_text(
                text, reply_markup=reply_markup, parse_mode=parse_mode
            )
            return
        except BadRequest:
            # "Message is not modified" or a non-editable message: reply anew.
            pass
    message = update.effective_message
    if message is None:
        return
    try:
        await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest:
        await message.reply_text(text, reply_markup=reply_markup)


async def _send_markdown(
    bot,
    chat_id: int,
    text: str,
    *,
    code_block: bool = False,
    reply_to: int | None = None,
    thread_id: int | None = None,
) -> list:
    """Send text as Markdown, falling back to plain text on a parse error.

    Returns the messages sent, so the caller can map their ids back to the
    session (that map is what a quote-reply is resolved against).
    ``thread_id`` posts into a forum topic; ``reply_to`` must be a message of
    that same thread — the relay enforces this, never across threads.
    """
    sent = []
    for chunk in chunk_text(text):
        payload = f"```\n{chunk}\n```" if code_block else chunk
        try:
            message = await bot.send_message(
                chat_id,
                payload,
                parse_mode=ParseMode.MARKDOWN,
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
            )
        except BadRequest:
            message = await bot.send_message(
                chat_id,
                chunk,
                reply_to_message_id=reply_to,
                message_thread_id=thread_id,
            )
        sent.append(message)
    return sent


def _session_from_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """The session a quote-reply belongs to, or ``None`` for a fresh message.

    Telegram carries ``reply_to_message.message_id``; the bot records the id of
    every message it sends for a session, so quoting any of them — the answer,
    the "started" ack — continues that session.
    """
    reply = getattr(update.message, "reply_to_message", None)
    if reply is None:
        return None
    message_id = getattr(reply, "message_id", None)
    if message_id is None:
        return None
    return _state(context).session_for_message(update.effective_chat.id, message_id)


def _msg_thread(update: Update) -> int | None:
    """The forum topic this message was posted in, else ``None``.

    ``None`` means General, or a chat without topics: Telegram only sets
    ``message_thread_id`` for messages inside a topic of a forum group. This
    is the topic rule's whole discriminator — General starts a session, a
    topic continues the one bound to it.
    """
    if not getattr(update.effective_chat, "is_forum", False):
        return None
    message = update.effective_message or update.message
    return getattr(message, "message_thread_id", None) or None


def _track(state: GatewayState, chat_id: int, session: str, message) -> None:
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        state.record_message(chat_id, message_id, session)


async def _deliver_prompt(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: str, text: str, username: str
) -> None:
    stamp = f"[from Telegram @{username}] {text}"
    client = _client(context)
    # A session can be mid-restart (live row, backend not spawned yet); the
    # bootstrap pass finishes in seconds, so retry once before surfacing it.
    for attempt in (1, 2):
        try:
            await client.send(session, stamp)
            return
        except AmuxError as exc:
            if exc.status in (409, 503) and attempt == 1:
                await asyncio.sleep(2.0)
                continue
            raise


async def _report_send_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: str,
    exc: AmuxError,
    source: str,
) -> None:
    """Log a refused send and recover from a session that no longer exists."""
    user_id = update.effective_user.id if update.effective_user else "-"
    log.warning(
        "send_failed session=%s user=%s source=%s status=%s detail=%s",
        session,
        user_id,
        source,
        exc.status,
        exc.detail,
    )
    message = update.message
    if message is None:
        return
    if exc.status == 404:
        rows = await _client(context).list_sessions()
        if not any(row.get("name") == session for row in rows):
            _state(context).clear(update.effective_chat.id)
            for thread in _state(context).drop_session_topics(
                update.effective_chat.id, session
            ):
                # A dead session's topic must become adoptable again, so the
                # owner's next message there starts a fresh manager.
                log.info(
                    "topic_released chat_id=%s thread=%s session=%s reason=session_gone",
                    update.effective_chat.id,
                    thread,
                    session,
                )
            await message.reply_text(
                f"⚠️ `{session}` no longer exists — pick another session.",
                reply_markup=sessions_keyboard(rows, "pick") if rows else main_menu(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await message.reply_text(
            f"⚠️ `{session}` is a worker row with no lane env file, so amux "
            "refused the send. Pick the lane it runs in, or start a fresh "
            "session with 🚀.",
            reply_markup=main_menu(),
        )
        return
    await message.reply_text(f"❌ amux refused: {exc.detail}")


async def _show_manager(update: Update, context: ContextTypes.DEFAULT_TYPE, provider: str | None = None) -> None:
    """Show (and let the owner change) which agent a fresh message starts."""
    cfg = _cfg(context)
    state = _state(context)
    chat_id = update.effective_chat.id
    choice = state.manager(
        chat_id, {"provider": cfg.manager_provider, "model": cfg.manager_model}
    )
    models: list[str] = []
    try:
        rows = await _client(context).models(provider or choice["provider"])
        for row in rows:
            model_id = str(row.get("id") or "")
            if model_id and model_id not in models:
                models.append(model_id)
        models = models[:8]
    except AmuxError as exc:
        log.debug("model_catalog_failed provider=%s detail=%s", provider, exc)
    await _say(
        update,
        "🧠 *Manager agent* — the agent a new message starts.\n"
        f"provider: `{choice['provider']}`\n"
        f"model: `{choice['model'] or 'server default'}`\n"
        f"cwd: `{cfg.manager_cwd}`\n\n"
        "In a topic group, every new message opens a new topic with its own "
        "manager; message inside a topic to continue that manager.",
        reply_markup=manager_keyboard(choice["provider"], choice["model"], models),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _require_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    session = _state(context).selected(update.effective_chat.id)
    if session:
        return session
    log.info(
        "session_required user=%s chat=%s",
        update.effective_user.id if update.effective_user else "-",
        update.effective_chat.id,
    )
    workers = await _client(context).list_sessions()
    await _say(
        update,
        "Pick a session first:",
        reply_markup=sessions_keyboard(workers, "pick") if workers else main_menu(),
    )
    return None


async def _create_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    name = f"tg-{time.strftime('%m%d-%H%M')}-{secrets.token_hex(2)}"
    client = _client(context)
    try:
        # A lane (env file + autostart), not a bare workers-table row: the
        # server refuses sends to a row because it has no lane env file.
        await client.create_session(
            name,
            cwd=cfg.default_workdir,
            provider=cfg.default_provider,
        )
        worker = await client.wait_until_started(name)
        slug = str(worker.get("status") or "")
        if slug == "stopped":
            # Autostart can be skipped when the box refuses a spawn; one
            # explicit start is the documented retry.
            await client.start(name)
            worker = await client.wait_until_started(name)
            slug = str(worker.get("status") or slug)
    except AmuxError as exc:
        log.warning(
            "session_create_failed name=%s provider=%s status=%s detail=%s",
            name,
            cfg.default_provider,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ Could not start a session: {exc.detail}")
        return

    if slug == "error":
        detail = worker.get("error") or worker.get("state") or "unknown error"
        log.warning("session_start_failed name=%s detail=%s", name, detail)
        await _say(
            update,
            f"❌ Session `{name}` was created but failed to start: {detail}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _state(context).select(update.effective_chat.id, name)
    provider = worker.get("provider") or cfg.default_provider or "server default"
    log.info("session_started name=%s provider=%s status=%s", name, provider, slug or "unknown")
    await _say(
        update,
        f"🚀 Started `{name}` ({provider}, {slug or 'running'}).\n"
        "Send a message to give it a task.",
        reply_markup=status_keyboard(name),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _peek(update: Update, context: ContextTypes.DEFAULT_TYPE, session: str) -> None:
    client = _client(context)
    raw = None
    for attempt in (1, 2):
        try:
            raw = await client.peek(session, lines=200)
            break
        except AmuxError as exc:
            if exc.status == 409 and attempt == 1:
                await asyncio.sleep(2.5)
                continue
            log.warning("peek_failed session=%s status=%s detail=%s", session, exc.status, exc.detail)
            if exc.status == 409:
                detail = (
                    f"`{session}` is being (re)started by amux right now — "
                    "try again in a few seconds."
                )
            else:
                detail = exc.detail
            await _say(update, f"❌ Peek failed: {detail}", parse_mode=ParseMode.MARKDOWN)
            return
    if raw is None:
        return
    text = strip_ansi(raw).strip()
    if not text:
        await _say(update, f"👁️ `{session}` has no output yet.", reply_markup=status_keyboard(session))
        return
    await _send_markdown(
        context.application.bot, update.effective_chat.id, text[-3500:], code_block=True
    )


def _status_text(workers: list[dict]) -> str:
    if not workers:
        return "No workers yet. Use 🚀 to start one."
    lines = [f"👁️ *{len(workers)} worker(s)*", ""]
    for worker in workers[:20]:
        lines.append("• " + format_worker_line(worker))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# commands and menu
# ---------------------------------------------------------------------------


@allow_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("command start user=%s chat=%s", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(WELCOME, reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


@allow_only
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("command menu user=%s chat=%s", update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text("Menu:", reply_markup=main_menu())


@allow_only
async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("command sessions user=%s chat=%s", update.effective_user.id, update.effective_chat.id)
    workers = await _client(context).list_sessions()
    await _say(
        update,
        _status_text(workers),
        reply_markup=sessions_keyboard(workers, "pick") if workers else main_menu(),
        parse_mode=ParseMode.MARKDOWN,
    )


@allow_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("command status user=%s chat=%s", update.effective_user.id, update.effective_chat.id)
    session = _state(context).selected(update.effective_chat.id)
    if session:
        await _peek(update, context, session)
        return
    workers = await _client(context).list_sessions()
    await _say(
        update,
        _status_text(workers),
        reply_markup=sessions_keyboard(workers, "peek") if workers else main_menu(),
        parse_mode=ParseMode.MARKDOWN,
    )


@allow_only
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        pass  # a stale query still gets its action handled below
    data = query.data or ""
    if not data.startswith(PREFIX):
        return
    action, _, raw = data[len(PREFIX):].partition(":")
    log.info(
        "callback action=%s target=%s user=%s chat=%s",
        action,
        raw or "-",
        update.effective_user.id,
        update.effective_chat.id,
    )

    if action in ("menu", "cancel"):
        await _say(update, WELCOME, reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)
    elif action == "new":
        await _create_session(update, context)
    elif action == "peek":
        if raw:
            await _peek(update, context, raw)
        else:
            workers = await _client(context).list_sessions()
            await _say(
                update,
                _status_text(workers),
                reply_markup=sessions_keyboard(workers, "peek") if workers else main_menu(),
                parse_mode=ParseMode.MARKDOWN,
            )
    elif action == "stop":
        if raw:
            cards = await _client(context).todo_cards(raw)
            warning = ""
            if cards:
                names = ", ".join(str(card["id"]) for card in cards[:5])
                warning = (
                    f"\n\n⚠️ {len(cards)} card(s) still assigned ({names}). "
                    "amux restarts a worker that owns a waiting Todo card, so the "
                    "stop will be undone until they are moved or closed."
                )
            await _say(
                update,
                f"⏹️ Stop `{raw}`?{warning}",
                reply_markup=stop_confirm(raw),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        workers = await _client(context).list_sessions()
        if not workers:
            await _say(update, "No workers to stop.", reply_markup=main_menu())
            return
        await _say(
            update,
            "Which session should I stop?",
            reply_markup=sessions_keyboard(workers, "stop"),
        )
    elif action == "pick":
        _state(context).select(update.effective_chat.id, raw)
        await _say(
            update,
            f"✅ Selected `{raw}`. Send a message or a voice note.",
            reply_markup=status_keyboard(raw),
            parse_mode=ParseMode.MARKDOWN,
        )
    elif action == "mgr":
        await _show_manager(update, context)
    elif action == "mgrp":
        _state(context).set_manager(update.effective_chat.id, raw, None)
        await _show_manager(update, context, provider=raw)
    elif action == "mgrm":
        provider, _, model = raw.partition(":")
        _state(context).set_manager(update.effective_chat.id, provider, model or None)
        await _show_manager(update, context, provider=provider)
    elif action == "stopok":
        chat_id = update.effective_chat.id
        try:
            await _client(context).stop(raw)
        except AmuxError as exc:
            log.warning(
                "session_stop_failed session=%s status=%s detail=%s",
                raw,
                exc.status,
                exc.detail,
            )
            await _say(update, f"❌ Stop failed: {exc.detail}")
            return
        # A stop answers "applied" before bootstrap considers restarting a
        # worker that still owns a waiting card. Check the truth, then report it.
        await asyncio.sleep(3)
        try:
            worker = await _client(context).find(raw)
        except AmuxError:
            worker = {}
        if worker.get("running"):
            cards = await _client(context).todo_cards(raw)
            if cards:
                names = ", ".join(str(card["id"]) for card in cards[:5])
                log.info("session_stop_undone session=%s cards=%s", raw, names)
                await _say(
                    update,
                    f"⏹️ Stop requested, but amux restarted `{raw}`: "
                    f"{len(cards)} card(s) are still assigned ({names}). "
                    "Move or close them first.",
                    reply_markup=main_menu(),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return
            log.info("session_stop_undone session=%s cards=none", raw)
            await _say(
                update,
                f"⚠️ `{raw}` is still running after the stop (it may be restarting).",
                reply_markup=main_menu(),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        state = _state(context)
        if state.selected(chat_id) == raw:
            state.clear(chat_id)
        # The topic outlives the session but must not stay bound to it: the
        # next message there starts a fresh manager instead of hitting a
        # stopped lane forever.
        _release_topics(state, chat_id, raw, reason="stopped")
        log.info("session_stopped session=%s", raw)
        await _say(update, f"⏹️ Stopped `{raw}`.", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


def _now() -> float:
    """Clock seam: the dedup window is asserted against this in tests."""
    return time.monotonic()


def _record_general_start(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, session: str
) -> None:
    """Remember the session a General text message just started.

    Recorded only once the lane is actually up: a start that failed must not
    swallow the next identical message (it falls through to a fresh attempt).
    """
    recent = context.application.bot_data.setdefault(RECENT_STARTS, {})
    now = _now()
    for key in [k for k, (_, ts) in recent.items() if now - ts > DUP_WINDOW_S]:
        recent.pop(key, None)
    recent[(chat_id, text)] = (session, now)
    while len(recent) > MAX_RECENT_STARTS:
        recent.pop(next(iter(recent)))


async def _duplicate_general_start(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str
) -> str | None:
    """The live session this exact text already started in this chat, if any.

    The record alone is not enough: within the window the lane can still have
    died (or been stopped from the menu), and a prompt handed to it would be
    lost. So the session is checked against amux before it is merged into.
    """
    recent = context.application.bot_data.get(RECENT_STARTS)
    if not recent:
        return None
    entry = recent.get((chat_id, text))
    if entry is None:
        return None
    session, ts = entry
    if _now() - ts > DUP_WINDOW_S:
        recent.pop((chat_id, text), None)
        return None
    try:
        row = await _client(context).find(session)
    except AmuxError as exc:
        if exc.status != 0:  # gone from the fleet: the record is stale
            recent.pop((chat_id, text), None)
        return None  # unreachable: fall through rather than guess
    if str(row.get("status") or "").lower() in DEAD_STATUSES:
        recent.pop((chat_id, text), None)
        return None
    return session


@allow_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    thread = _msg_thread(update)
    if thread is not None:
        # The topic identifies the session — no reply or quote needed.
        session = _state(context).topic_session(update.effective_chat.id, thread)
        if session:
            await _continue_session(update, context, session, text, "text")
            return
        # An unmapped topic (owner-made, or state loss) adopts a new manager.
        await _start_manager(update, context, text, source="text", topic=thread)
        return
    # General (or a private chat): a quote-reply continues, anything else
    # starts a fresh manager — in a forum group it gets its own topic.
    session = _session_from_reply(update, context)
    if session:
        await _continue_session(update, context, session, text, "text")
        return
    # The same sentence twice in a row is one intent, not two managers: the
    # burst goes to the session the first copy started (its topic already
    # exists). A dead or never-started session falls through to a fresh one.
    duplicate = await _duplicate_general_start(context, update.effective_chat.id, text)
    if duplicate:
        log.info(
            "duplicate_general_merged chat_id=%s session=%s msg=%s",
            update.effective_chat.id,
            duplicate,
            update.message.message_id,
        )
        await _continue_session(update, context, duplicate, text, "text")
        return
    await _start_manager(update, context, text, source="text")


async def _continue_session(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: str,
    text: str,
    source: str,
    *,
    anchor: int | None = None,
) -> None:
    """Deliver a quote-reply to the session it quotes (the same session rule).

    ``anchor`` is the message the answer should reply to. It differs from the
    owner's message for a voice note: the answer must attach to the readable
    TRANSCRIPTION message, never to the audio bubble the owner cannot read
    back. The transcription is itself a reply to the audio, so the thread
    still opens at the voice note.
    """
    chat_id = update.effective_chat.id
    state = _state(context)
    anchor_id = anchor or update.message.message_id
    # The anchor's thread is recorded with it: the relay may only reply to an
    # anchor inside the session's own topic, never across threads.
    state.set_turn_message(
        chat_id, session, anchor_id, thread=_msg_thread(update)
    )
    if anchor is not None:
        # Quoting the transcription must continue this session, same as
        # quoting any other bot message.
        state.record_message(chat_id, anchor, session)
    log.info(
        "prompt_%s user=%s session=%s chars=%s route=reply",
        source,
        update.effective_user.id,
        session,
        len(text),
    )
    try:
        await _deliver_prompt(context, chat_id, session, text, _username(update))
    except AmuxError as exc:
        await _report_send_error(update, context, session, exc, source)
        return
    # No ack here: the agent's answer arrives as a reply to THIS message, which
    # is the confirmation. One extra line per turn would double the traffic.


async def _create_forum_topic(
    update: Update, context: ContextTypes.DEFAULT_TYPE, name: str
) -> int | None:
    """Create the group topic a session will live in; ``None`` on failure.

    Failure is never fatal: the session falls back to the plain General
    behavior (today's pre-topic mode), and ``topic_create_failed`` is the
    sweep signal for the fallback ever being taken.
    """
    chat_id = update.effective_chat.id
    try:
        topic = await context.application.bot.create_forum_topic(chat_id, name=name)
    except TelegramError as exc:
        log.warning(
            "topic_create_failed chat_id=%s name=%s error=%s", chat_id, name, exc
        )
        return None
    thread = getattr(topic, "message_thread_id", None)
    if thread is None:
        log.warning(
            "topic_create_failed chat_id=%s name=%s detail=no thread id", chat_id, name
        )
        return None
    log.info("topic_created chat_id=%s thread=%s name=%s", chat_id, thread, name)
    return int(thread)


async def _start_manager(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    source: str,
    anchor: int | None = None,
    topic: int | None = None,
) -> None:
    """A fresh message starts a fresh manager agent and gets its first answer.

    ``anchor`` follows the same rule as in ``_continue_session``: a voice
    note's answer replies to the transcription message, not to the audio.

    In a forum group the session gets its own topic named after it: ``topic``
    is an existing topic to adopt (a message arrived inside one), ``None``
    means create one (the message arrived in General).
    """
    cfg = _cfg(context)
    state = _state(context)
    client = _client(context)
    chat_id = update.effective_chat.id
    choice = state.manager(
        chat_id, {"provider": cfg.manager_provider, "model": cfg.manager_model}
    )
    name = new_manager_name()
    anchor_id = anchor or update.message.message_id
    forum = bool(getattr(update.effective_chat, "is_forum", False))
    in_thread = _msg_thread(update)

    # The topic is bound BEFORE the lane exists: an owner message arriving
    # while the lane is still being created must already route somewhere.
    thread = topic if forum else None
    if forum and thread is None:
        thread = await _create_forum_topic(update, context, name)
    if forum and thread is not None:
        state.set_topic(chat_id, thread, name)

    if forum and thread is not None:
        if thread == in_thread:
            ack_text = (
                f"🧠 Starting `{name}` ({choice['provider']}) — send messages in "
                "this topic to continue it."
            )
        else:
            ack_text = (
                f"🧠 Starting `{name}` ({choice['provider']}) — continuing in "
                f"its own topic."
            )
    else:
        ack_text = f"🧠 Starting `{name}` ({choice['provider']}) — reply here to continue it."

    ack = await update.message.reply_text(ack_text, parse_mode=ParseMode.MARKDOWN)
    state.record_message(chat_id, ack.message_id, name)
    state.set_turn_message(chat_id, name, anchor_id, thread=in_thread)
    if anchor is not None:
        # Quoting the transcription must continue this manager session.
        state.record_message(chat_id, anchor, name)

    try:
        name, row = await create_manager(
            client, cfg, name=name, provider=choice["provider"], model=choice["model"]
        )
    except AmuxError as exc:
        log.warning(
            "manager_create_failed name=%s provider=%s status=%s detail=%s",
            name,
            choice["provider"],
            exc.status,
            exc.detail,
        )
        _release_topics(state, chat_id, name, reason="manager_create_failed")
        await _edit_or_send(ack, f"❌ Could not start a manager: {exc.detail}")
        return

    status = str(row.get("status") or "")
    if status == "error":
        detail = row.get("error") or "unknown error"
        log.warning("manager_start_failed name=%s detail=%s", name, detail)
        _release_topics(state, chat_id, name, reason="manager_start_failed")
        await _edit_or_send(ack, f"❌ `{name}` failed to start: {detail}")
        return

    log.info(
        "manager_ready name=%s provider=%s model=%s status=%s source=%s topic=%s",
        name,
        choice["provider"],
        choice["model"] or "(server default)",
        status,
        source,
        thread,
    )
    if source == "text" and in_thread is None:
        # A General text start: an identical repeat inside DUP_WINDOW_S merges
        # into this session instead of opening a second topic.
        _record_general_start(context, chat_id, text, name)
    try:
        await _deliver_prompt(context, chat_id, name, text, _username(update))
    except AmuxError as exc:
        await _report_send_error(update, context, name, exc, source)


def _release_topics(state: GatewayState, chat_id: int, session: str, *, reason: str) -> None:
    """Free a dead manager's topics so the next message can adopt them."""
    for thread in state.drop_session_topics(chat_id, session):
        log.info(
            "topic_released chat_id=%s thread=%s session=%s reason=%s",
            chat_id,
            thread,
            session,
            reason,
        )


async def _edit_or_send(message, text: str) -> None:
    """Edit an earlier ack, or send anew when Telegram will not let us edit."""
    try:
        await message.edit_text(text)
    except (BadRequest, TelegramError):
        await message.reply_text(text)


@allow_only
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    if not cfg.groq_api_key:
        await update.message.reply_text(
            "🎙️ Voice notes need GROQ_API_KEY in ~/.amux/server.env."
        )
        return
    log.info(
        "voice_note user=%s media_type=%s",
        update.effective_user.id,
        getattr(update.message.voice, "mime_type", "?"),
    )
    try:
        transcript = await transcribe_voice(
            update.message.voice, groq_transcriber(cfg.groq_api_key, cfg.groq_model)
        )
    except Exception as exc:  # transcription failures must be visible, not fatal
        log.warning("voice_transcription_failed user=%s error=%s", update.effective_user.id, exc)
        await update.message.reply_text(f"❌ Could not transcribe that voice note: {exc}")
        return
    if not transcript:
        await update.message.reply_text("🗣️ Heard nothing in that voice note.")
        return
    # The transcription is posted as a reply TO the audio (so the owner can
    # read what they said in the thread) and becomes the answer's anchor:
    # the relay replies to THIS message, never to the unreadable audio bubble.
    echo = await update.message.reply_text(f"🗣️ Transcribed: {transcript}")
    anchor = getattr(echo, "message_id", None)
    log.info(
        "voice_transcribed user=%s chars=%s anchor=%s",
        update.effective_user.id,
        len(transcript),
        anchor,
    )

    thread = _msg_thread(update)
    if thread is not None:
        # Same topic rule as text: the topic picks the session, no quote.
        session = _state(context).topic_session(update.effective_chat.id, thread)
        if session:
            await _continue_session(update, context, session, transcript, "voice", anchor=anchor)
            return
        await _start_manager(
            update, context, transcript, source="voice", anchor=anchor, topic=thread
        )
        return
    session = _session_from_reply(update, context)
    if session:
        await _continue_session(update, context, session, transcript, "voice", anchor=anchor)
        return
    await _start_manager(update, context, transcript, source="voice", anchor=anchor)


# ---------------------------------------------------------------------------
# send-prompt conversation
# ---------------------------------------------------------------------------


@allow_only
async def ask_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = await _require_session(update, context)
    if not session:
        return ConversationHandler.END
    await _say(update, f"✍️ Send the prompt for `{session}` (or /cancel).", parse_mode=ParseMode.MARKDOWN)
    return PROMPT


@allow_only
async def ask_prompt_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await ask_prompt(update, context)


@allow_only
async def receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id
    state = _state(context)
    session = state.selected(chat_id)
    if not session:
        await update.message.reply_text("No session selected — /menu")
        return ConversationHandler.END
    if text:
        log.info(
            "prompt_state user=%s session=%s chars=%s",
            update.effective_user.id,
            session,
            len(text),
        )
        state = _state(context)
        state.set_turn_message(
            chat_id,
            session,
            update.message.message_id,
            thread=_msg_thread(update),
        )
        try:
            await _deliver_prompt(context, chat_id, session, text, _username(update))
        except AmuxError as exc:
            await _report_send_error(update, context, session, exc, "prompt")
            return ConversationHandler.END
        ack = await update.message.reply_text(
            f"✅ Sent to `{session}` — reply here to continue it.",
            parse_mode=ParseMode.MARKDOWN,
        )
        _track(state, chat_id, session, ack)
    return ConversationHandler.END


@allow_only
async def receive_prompt_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """A voice note inside the ✍️ state goes to the SELECTED session.

    Deliberately not `on_voice`: that handler's no-reply branch starts a fresh
    manager, which would ignore the session the owner just picked.
    """
    cfg = _cfg(context)
    if not cfg.groq_api_key:
        await update.message.reply_text(
            "🎙️ Voice notes need GROQ_API_KEY in ~/.amux/server.env."
        )
        return ConversationHandler.END
    session = _state(context).selected(update.effective_chat.id)
    if not session:
        await update.message.reply_text("No session selected — /menu")
        return ConversationHandler.END
    try:
        transcript = await transcribe_voice(
            update.message.voice, groq_transcriber(cfg.groq_api_key, cfg.groq_model)
        )
    except Exception as exc:
        log.warning("voice_transcription_failed user=%s error=%s", update.effective_user.id, exc)
        await update.message.reply_text(f"❌ Could not transcribe that voice note: {exc}")
        return ConversationHandler.END
    if not transcript:
        await update.message.reply_text("🗣️ Heard nothing in that voice note.")
        return ConversationHandler.END
    # Same anchor rule as `on_voice`: the answer replies to the transcription
    # message, which is itself a reply to the audio.
    echo = await update.message.reply_text(f"🗣️ Transcribed: {transcript}")
    anchor = getattr(echo, "message_id", None)
    log.info(
        "voice_transcribed user=%s chars=%s anchor=%s route=selected",
        update.effective_user.id,
        len(transcript),
        anchor,
    )
    state = _state(context)
    state.set_turn_message(
        update.effective_chat.id,
        session,
        anchor or update.message.message_id,
        thread=_msg_thread(update),
    )
    if anchor is not None:
        state.record_message(update.effective_chat.id, anchor, session)
    try:
        await _deliver_prompt(
            context, update.effective_chat.id, session, transcript, _username(update)
        )
    except AmuxError as exc:
        await _report_send_error(update, context, session, exc, "voice")
        return ConversationHandler.END
    ack = await update.message.reply_text(
        f"✅ Sent to `{session}` — reply here to continue it.",
        parse_mode=ParseMode.MARKDOWN,
    )
    _track(state, update.effective_chat.id, session, ack)
    return ConversationHandler.END


@allow_only
async def cancel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# reply relay
# ---------------------------------------------------------------------------


# Sessions whose pane currently offers NO answer boundary. One WARN per
# stretch (reset by a successful extract): a miss repeats every relay cycle,
# and this exact silence hid the 2026-10-27 missing answers — the relay
# skipped every reply for hours without leaving a single line to sweep.
_no_boundary_warned: set[tuple[int, str]] = set()


async def relay_once(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    client: AmuxClient = app.bot_data["client"]
    state: GatewayState = app.bot_data["state"]
    for chat_id, sessions in state.sessions_to_relay().items():
        for session in sessions:
            try:
                raw = await client.peek(session, lines=300)
            except AmuxError as exc:
                log.debug("relay_peek_failed chat_id=%s session=%s error=%s", chat_id, session, exc)
                continue
            text = extract_reply(raw)
            if not text:
                if (chat_id, session) not in _no_boundary_warned:
                    _no_boundary_warned.add((chat_id, session))
                    log.warning(
                        "relay_no_boundary chat_id=%s session=%s pane_chars=%s "
                        "hint=no '—— answer ——' sentinel in the reply and the "
                        "[from Telegram @…] prompt marker scrolled out of the "
                        "viewport — nothing can be delivered until one is visible",
                        chat_id,
                        session,
                        len(raw),
                    )
                continue
            _no_boundary_warned.discard((chat_id, session))
            last = state.last_relayed(chat_id, session)
            if text == last:
                continue
            new_text = delta_since(last, text)
            if not new_text.strip():
                continue
            # Reply to the message that started this turn, so the answer lands
            # in the thread the owner can quote to keep talking to this session.
            # In a forum topic the answer goes INTO the topic; the anchor is
            # only reused when it was posted inside that very topic — a turn 1
            # anchor in General must never become a cross-thread reply.
            thread_id = state.session_topic(chat_id, session)
            reply_to = state.turn_message(chat_id, session)
            if thread_id is not None and state.turn_thread(chat_id, session) != thread_id:
                reply_to = None
            try:
                sent = await _send_markdown(
                    app.bot, chat_id, new_text, reply_to=reply_to, thread_id=thread_id
                )
            except TelegramError as exc:
                log.warning("relay_send_failed chat_id=%s session=%s error=%s", chat_id, session, exc)
                continue
            for message in sent:
                _track(state, chat_id, session, message)
            state.set_last_relayed(chat_id, session, text)
            log.info(
                "relay_sent chat_id=%s session=%s chars=%s total=%s reply_to=%s thread=%s",
                chat_id,
                session,
                len(new_text),
                len(text),
                reply_to,
                thread_id,
            )


async def relay_loop(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    while True:
        await asyncio.sleep(cfg.relay_interval_s)
        try:
            await relay_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad cycle kill the loop
            log.exception("relay_cycle_failed")


async def post_init(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    client: AmuxClient = app.bot_data["client"]
    try:
        health = await client.health()
        log.info("amux_health status=%s commit=%s", health.get("status"), health.get("commit"))
    except AmuxError as exc:
        log.warning("amux_unreachable url=%s error=%s", cfg.amux_url, exc)
    # The tracked topic path managers must use instead of raw createForumTopic.
    control = ControlServer(app, cfg.control_port)
    app.bot_data["control"] = control
    if cfg.control_port:
        await control.start()
    else:
        log.info("control_disabled reason=TELEGRAM_GATEWAY_CONTROL_PORT=0")
    app.bot_data["relay_task"] = asyncio.create_task(relay_loop(app))
    log.info(
        "gateway_started allowed_users=%d amux_url=%s voice=%s relay_interval_s=%s",
        len(cfg.allowed_user_ids),
        cfg.amux_url,
        "on" if cfg.groq_api_key else "off",
        cfg.relay_interval_s,
    )


async def post_shutdown(app: Application) -> None:
    control = app.bot_data.get("control")
    if control is not None:
        await control.stop()
    task = app.bot_data.get("relay_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    client: AmuxClient = app.bot_data["client"]
    await client.aclose()


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------


def build_application(cfg: Config) -> Application:
    app = Application.builder().token(cfg.bot_token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["guard"] = Guard(frozenset(cfg.allowed_user_ids), log)
    app.bot_data["client"] = AmuxClient(cfg.amux_url, verify=cfg.amux_verify)
    app.bot_data["state"] = GatewayState(cfg.state_path)

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_prompt, pattern=r"^gw:prompt$"),
            CommandHandler("prompt", ask_prompt_command),
        ],
        states={
            PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt),
                MessageHandler(filters.VOICE, receive_prompt_voice),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_prompt)],
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("sessions", sessions_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^gw:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app
