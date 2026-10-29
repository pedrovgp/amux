"""Telegram bot wiring: menu, prompt capture, voice notes and reply relay."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import opencode_api
from .amux import AmuxClient, AmuxError, session_id_from_flags
from .config import Config
from .control import ControlServer
from .guard import Guard, allow_only
from .manager import PROVIDERS, create_manager, new_manager_name, new_opencode_session_id
from .menu import (
    EFFORTS,
    PREFIX,
    control_button,
    control_keyboard,
    ctl_key,
    effort_keyboard,
    effort_supported,
    format_worker_line,
    main_menu,
    manager_keyboard,
    model_keyboard,
    session_effort,
    session_model,
    sessions_keyboard,
    status_keyboard,
    stop_confirm,
)
from .pollwatch import PollWatch, default_watch
from .relay import (
    chunk_text,
    delta_since,
    extract_reply,
    plan_opencode_relay,
    strip_ansi,
)
from .state import GatewayState
from .topic_titles import clean_title, read_titles
from .voice import groq_transcriber, transcribe_voice

log = logging.getLogger("telegram_gateway")

PROMPT = 1
ASK = 2  # the ✏️ rename / ⌨️ custom-model prompt conversation

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


# ---------------------------------------------------------------------------
# the OpenCode serve API (Milestone 9) — model, effort, interrupt
# ---------------------------------------------------------------------------


def _oc_api(context: ContextTypes.DEFAULT_TYPE):
    """The OpenCode API entry point: the real module by default.

    Tests inject a fake at ``bot_data["oc_api"]`` — the panel's offline rule:
    no test may run ``ss``, read ``/proc`` or open a socket, so discovery is
    always something a test can hand in.
    """
    return context.application.bot_data.get("oc_api") or opencode_api


# One WARN per (session) stretch: a dead serve must be sweepable
# (`oc_api_fallback`) without flooding the log on every tap.
_oc_fallback_warned: set[str] = set()


def _oc_fallback_warn(session: str, op: str, exc) -> None:
    if session in _oc_fallback_warned:
        return
    _oc_fallback_warned.add(session)
    log.warning(
        "oc_api_fallback op=%s session=%s detail=%s",
        op,
        session,
        getattr(exc, "detail", None) or "OpenCode server unreachable",
    )


def _oc_fallback_ok(session: str) -> None:
    _oc_fallback_warned.discard(session)


async def _oc_client(
    context: ContextTypes.DEFAULT_TYPE, row: dict, op: str
) -> tuple | None:
    """``(client, pin)`` when this row is an opencode lane with a live serve.

    ``None`` means "run today's path" — not an opencode lane, no pin (an
    unpinned lane has no conversation id to address), or discovery/auth
    failed (WARNed here and, for the reason, by ``opencode_api``).
    """
    if str(row.get("provider") or "").lower() != "opencode":
        return None
    pin = session_id_from_flags(row.get("flags"))
    if not pin:
        return None
    name = str(row.get("name") or "?")
    try:
        client = await _oc_api(context).client_for(lane=name, pin=pin)
    except AmuxError as exc:
        _oc_fallback_warn(name, op, exc)
        return None
    if client is None:
        return None  # an entry point that answered "nothing" is not a client
    return client, pin


def _oc_expected(row: dict) -> bool:
    """True when this row SHOULD reach its own OpenCode server.

    Used to tell "there is no dial here" (an unpinned or non-opencode lane,
    which gets Milestone 8's honest refusal) apart from "the dial exists but
    its server did not answer" — the owner deserves the second reason, not
    the first.
    """
    return str(row.get("provider") or "").lower() == "opencode" and bool(
        session_id_from_flags(row.get("flags"))
    )


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


# ---------------------------------------------------------------------------
# session identity: topic -> pin -> CURRENT lane name
# ---------------------------------------------------------------------------

RENAMING = "renaming"  # bot_data: {(chat_id, old_name): new_name} while a PATCH runs


def _renaming(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault(RENAMING, {})


async def _resolve_topic_session(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, thread: int
) -> str | None:
    """The live session a topic addresses, or ``None`` (a fresh binding).

    The **pin** is the identity: ``topic_pins`` holds the opencode conversation
    id from the lane's ``CC_FLAGS --session ses_…``, which a rename never
    touches, so the topic keeps addressing its session through any number of
    renames — here or in the dashboard. Resolution goes

        topic -> pin -> CURRENT lane name (``GET /api/sessions``, flags match)

    and the name recorded at bind time is the fallback for three cases: an
    unpinned/legacy session (never had a pin), amux being unreachable (a WARN
    and the stored name — an API blip must NOT be read as "the session is
    gone"), and a lane that vanished from the list (the stored name then 404s
    into the existing release path, which is what frees the topic).

    ``None`` means genuinely unbound — only THAT adopts a new manager.
    """
    state = _state(context)
    client = _client(context)
    stored = state.topic_session(chat_id, thread)
    pin = state.topic_pin(chat_id, thread)
    if not stored and not pin:
        return None
    pending = _renaming(context).get((chat_id, stored))
    if pending:
        # Mid-rename: the lane is between two names, neither of which may be
        # listed right now. Address where it is going rather than releasing a
        # topic whose session still exists.
        return pending
    if not pin:
        return stored
    try:
        rows = await client.list_sessions()
    except AmuxError as exc:
        log.warning(
            "topic_resolve_unreachable chat_id=%s thread=%s pin=%s stored=%s "
            "status=%s detail=%s",
            chat_id,
            thread,
            pin,
            stored,
            exc.status,
            exc.detail,
        )
        return stored
    live = {
        str(row.get("name")): row
        for row in rows
        if isinstance(row, dict) and row.get("name")
    }
    for name, row in live.items():
        if session_id_from_flags(row.get("flags")) != pin:
            continue
        if stored and stored != name and stored not in live:
            # A rename this gateway did not perform (dashboard, CLI): repair
            # every map that names the old lane, then let the topic title
            # follow. The `stored not in live` guard stops the repair from
            # stealing maps that belong to a live lane of the old name.
            rebound = state.rebind_pin(chat_id, pin, name)
            if rebound:
                stale, threads = rebound
                log.info(
                    "session_renamed old=%s new=%s source=pin_resolve chat_id=%s",
                    stale,
                    name,
                    chat_id,
                )
                await _follow_topic_titles(
                    context, chat_id, threads, name, source="pin_resolve"
                )
        return name
    return stored


async def _follow_topic_titles(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    threads: list[int],
    new_name: str,
    *,
    source: str,
) -> None:
    """Make every topic of a renamed session show the new name.

    The edit is the visible half of a rename; releasing ``topic_created`` is
    the other half — without it the OpenCode title sync would see
    "recorded name == session name" again on its next tick and rename the
    topic straight back, undoing the owner's choice. A failed edit is a WARN
    (the recorded title then still matches what Telegram shows).
    """
    state = _state(context)
    bot = context.application.bot
    for thread in threads:
        previous = state.topic_name(chat_id, thread)
        try:
            await bot.edit_forum_topic(
                chat_id, message_thread_id=thread, name=new_name
            )
        except TelegramError as exc:
            log.warning(
                "topic_title_update_failed chat_id=%s thread=%s session=%s "
                "title=%s error=%s",
                chat_id,
                thread,
                new_name,
                new_name,
                exc,
            )
            continue
        state.set_topic_name(chat_id, thread, new_name)
        state.release_topic_title(chat_id, thread)
        log.info(
            "topic_renamed chat_id=%s thread=%s session=%s from=%s to=%s source=%s",
            chat_id,
            thread,
            new_name,
            previous or "(unknown)",
            new_name,
            source,
        )


async def _deliver_prompt(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, session: str, text: str, username: str
) -> None:
    stamp = f"[from Telegram @{username}] {text}"
    client = _client(context)
    state = _state(context)
    if state.idle_stopped(chat_id, session) is not None:
        # The sweeper stopped this lane; the topic binding survived precisely
        # so this moment resumes it. The lane's CC_FLAGS carry its pinned
        # `--session <id>`, so the relaunch opens the SAME OpenCode
        # conversation — stop freed RAM, nothing about the session was lost.
        try:
            await client.start(session)
            await client.wait_until_started(session)
        except AmuxError as exc:
            log.warning(
                "revive_failed chat_id=%s session=%s status=%s detail=%s",
                chat_id,
                session,
                exc.status,
                exc.detail,
            )
            raise
        state.clear_idle_stopped(chat_id, session)
        log.info("revive chat_id=%s session=%s", chat_id, session)
    state.touch_activity(chat_id, session, "owner")
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
        chat_id = update.effective_chat.id
        pending = _renaming(context)
        # A 404 while THIS gateway is renaming the same session is the rename
        # window, not a dead lane: releasing the topic here would orphan it and
        # the next message would open a SECOND manager beside the renamed one.
        moving_to = pending.get((chat_id, session)) or next(
            (
                new
                for (cid, _old), new in pending.items()
                if cid == chat_id and new == session
            ),
            None,
        )
        if moving_to:
            log.info(
                "send_deferred_rename chat_id=%s session=%s moving_to=%s",
                chat_id,
                session,
                moving_to,
            )
            await message.reply_text(
                f"⌛ `{session}` is being renamed — give it a second and send again.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
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


# ---------------------------------------------------------------------------
# session control: the panel a topic (or /ctl) opens — Milestone 8
# ---------------------------------------------------------------------------


def _back_keyboard(session: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Back", callback_data=f"{PREFIX}ctl:{ctl_key(session)}"
                )
            ]
        ]
    )


async def _target_session(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> str | None:
    """The session a slash command addresses.

    In a forum topic that is the TOPIC's session (pin-resolved, so it survives
    renames); in General or a private chat it is the chat's selected session —
    the same split plain messages and commands already follow.
    """
    thread = _msg_thread(update)
    if thread is not None:
        return await _resolve_topic_session(context, update.effective_chat.id, thread)
    return _state(context).selected(update.effective_chat.id)


async def _show_control(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: str,
    *,
    note: str | None = None,
) -> None:
    """The panel itself: identity, state, and the six things you can do."""
    try:
        row = await _client(context).find(session)
    except AmuxError as exc:
        log.warning(
            "control_session_failed session=%s status=%s detail=%s",
            session,
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ `{session}`: {exc.detail}",
            reply_markup=main_menu(),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    provider = str(row.get("provider") or "?")
    model = session_model(row)
    effort = session_effort(row)
    show_effort = effort_supported(provider)
    # An opencode lane reports its own model AND its own effort (a variant of
    # that model) from its serve API; the row can only ever show the flags.
    oc = await _oc_client(context, row, "control")
    if oc:
        try:
            sess = await oc[0].get_session(oc[1])
        except AmuxError as exc:
            _oc_fallback_warn(session, "control", exc)
        else:
            oc_model = sess.get("model") or {}
            model = str(oc_model.get("id") or model)
            effort = str(oc_model.get("variant") or "")
            show_effort = True
            _oc_fallback_ok(session)
    state_line = "🟢 running" if row.get("running") else "⚪ stopped"
    lines = [f"🎛 *Session control* — `{session}`"]
    if note:
        lines.append(note)
    lines += [
        f"provider: `{provider}` · {state_line}",
        f"model: `{model or 'server default'}`",
    ]
    if show_effort:
        lines.append(f"effort: `{effort or 'default'}`")
    await _say(
        update,
        "\n".join(lines),
        reply_markup=control_keyboard(session),
        parse_mode=ParseMode.MARKDOWN,
    )


def _model_ids(models: list[dict]) -> list[str]:
    """De-duplicated ids, in catalog order — the indexes the buttons carry.

    Shared by the render and the tap: an index that means one thing when drawn
    and another when pressed would apply an id the owner never saw.
    """
    out: list[str] = []
    for entry in models:
        model_id = str(entry.get("id") or "")
        if model_id and model_id not in out:
            out.append(model_id)
    return out


def _oc_refs(models: list[dict]) -> list[dict]:
    """OpenCode's rows de-duped by ``(providerID, id)``, catalog order.

    The same id exists under both ``opencode`` and ``opencode-go`` (measured:
    ``claude-haiku-5-5``, ``gemini-4-argon``), so id-only dedupe would drop a
    model and a provider-blind index would apply the wrong one. The render and
    the tap both build this list, so position always means the same row.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in models:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("providerID") or ""), str(row.get("id") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def _oc_entry(refs: list[dict], model: dict) -> dict | None:
    """The catalog row for a session's ``model`` block, or None."""
    model_id = str(model.get("id") or "")
    provider_id = str(model.get("providerID") or "")
    return next(
        (
            row
            for row in refs
            if row.get("id") == model_id and row.get("providerID") == provider_id
        ),
        None,
    )


def _resolve_oc_model(refs: list[dict], model: str, current: dict) -> dict | None:
    """The catalog row a requested id names, or None (not in this catalog).

    ``provider/id`` (what ``gw:ctlo`` resolves to) matches exactly; a bare id
    — typed at ``/model`` — matches only if OpenCode's catalog has it, and
    when both providers carry it the session's own provider wins, so a
    switch never silently changes provider. An id the catalog cannot name is
    NOT an error: that is the custom-id escape hatch, and it keeps today's
    amux PATCH.
    """
    wanted = str(model or "")
    if not wanted:
        return None
    if "/" in wanted:
        provider_id, _, model_id = wanted.partition("/")
        return next(
            (
                ref
                for ref in refs
                if ref.get("providerID") == provider_id and ref.get("id") == model_id
            ),
            None,
        )
    matches = [ref for ref in refs if ref.get("id") == wanted]
    if not matches:
        return None
    own = str(current.get("providerID") or "")
    return next((ref for ref in matches if ref.get("providerID") == own), matches[0])


async def _provider_models(client: AmuxClient, provider: str) -> list[dict]:
    """The catalog for ONE provider; an unknown provider gets nothing.

    ``models(None)`` is the whole mixed catalog, which must not be offered as
    if it were this provider's list.
    """
    if not provider:
        return []
    return await client.models(provider)


async def _show_models(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: str,
    page: int | str = 0,
) -> None:
    """The provider's catalog, current one marked.

    An opencode lane with a pin reads ITS OWN server (``GET /api/model``:
    this is where the model list and the effort variants both live); every
    other case — and any API failure — keeps amux's ``GET /api/models``,
    whose opencode section is empty by construction.
    """
    client = _client(context)
    try:
        row = await client.find(session)
    except AmuxError as exc:
        log.warning(
            "control_models_failed session=%s status=%s detail=%s",
            session,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ model list failed: {exc.detail}")
        return
    provider = str(row.get("provider") or "")
    oc = await _oc_client(context, row, "models")
    if oc:
        try:
            models = await oc[0].list_models(provider)
            sess = await oc[0].get_session(oc[1])
        except AmuxError as exc:
            _oc_fallback_warn(session, "models", exc)
        else:
            _oc_fallback_ok(session)
            refs = _oc_refs(models)
            model = sess.get("model") or {}
            current = str(model.get("id") or "")
            current_index = next(
                (
                    i
                    for i, ref in enumerate(refs)
                    if ref.get("id") == model.get("id")
                    and ref.get("providerID") == model.get("providerID")
                ),
                None,
            )
            lines = [
                f"🤖 *Model* — `{session}`",
                f"current: `{current or 'server default'}`",
            ]
            if not refs:
                lines += [
                    "",
                    f"OpenCode's server lists no `{provider or '?'}` models. "
                    "Type the full id with ⌨️ Custom model id… — the same "
                    "shape `provider/model` that `opencode models` prints.",
                ]
            await _say(
                update,
                "\n".join(lines),
                reply_markup=model_keyboard(
                    session,
                    [str(ref.get("id") or "") for ref in refs],
                    current,
                    page,
                    current_index=current_index,
                    verb="ctlo",
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    try:
        models = await _provider_models(client, provider)
    except AmuxError as exc:
        log.warning(
            "control_models_failed session=%s status=%s detail=%s",
            session,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ model list failed: {exc.detail}")
        return
    ids = _model_ids(models)
    current = session_model(row)
    lines = [f"🤖 *Model* — `{session}`", f"current: `{current or 'server default'}`"]
    if _oc_expected(row):
        # The OpenCode catalog exists but its server did not answer — say so,
        # so an empty list is not read as "there are no models".
        lines += [
            "",
            "⚠️ this lane's OpenCode server is unreachable right now — "
            "showing amux's catalog instead.",
        ]
    if not ids:
        # No catalog rows for this provider — opencode has none (its models
        # live in the worker's own config), so the id has to be typed.
        lines += [
            "",
            f"amux's catalog has no `{provider or '?'}` models. Type the full "
            "id with ⌨️ Custom model id… — the same shape "
            "`provider/model` that `opencode models` prints.",
        ]
    await _say(
        update,
        "\n".join(lines),
        reply_markup=model_keyboard(session, ids, current, page),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _pick_model(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str, index: str
) -> None:
    """Apply the model the tapped row named (an index into the list shown)."""
    if not index:
        await _apply_model(update, context, session, "")
        return
    client = _client(context)
    try:
        row = await client.find(session)
        models = await _provider_models(client, str(row.get("provider") or ""))
    except AmuxError as exc:
        log.warning(
            "model_pick_failed session=%s index=%s status=%s detail=%s",
            session,
            index,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ model pick failed: {exc.detail}")
        return
    ids = _model_ids(models)
    try:
        model = ids[int(index)]
    except (ValueError, IndexError):
        # The catalog moved between the render and the tap: re-render instead
        # of applying an id the owner never saw.
        log.warning(
            "model_pick_stale session=%s index=%s available=%s",
            session,
            index,
            len(ids),
        )
        await _show_models(update, context, session)
        return
    await _apply_model(update, context, session, model)


async def _pick_oc_model(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str, index: str
) -> None:
    """Apply index i from the OPENCODE list on screen (``gw:ctlo``).

    The index is resolved ONLY against OpenCode's own catalog — amux's has
    different rows, so resolving it there could apply a model the owner never
    saw. Unresolvable (API gone, index stale) says so and re-renders instead
    of guessing; only a successfully resolved ``provider/id`` reaches
    ``_apply_model``, which applies it and falls back to today's PATCH if the
    API refuses mid-flight.
    """
    if not index:
        await _apply_model(update, context, session, "")
        return
    client = _client(context)
    try:
        row = await client.find(session)
    except AmuxError as exc:
        log.warning(
            "model_pick_failed session=%s index=%s status=%s detail=%s",
            session,
            index,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ model pick failed: {exc.detail}")
        return
    oc = await _oc_client(context, row, "pick")
    if oc is None:
        # The list rendered came from the API, so this index has no meaning
        # in amux's catalog. The owner's tap deserves an explanation, not a
        # model from a list they were never shown.
        await _say(
            update,
            "❌ this list came from the OpenCode server, which is unreachable "
            "right now — tap 🤖 Model to reload it.",
            reply_markup=_back_keyboard(session),
        )
        return
    try:
        models = await oc[0].list_models(str(row.get("provider") or ""))
    except AmuxError as exc:
        _oc_fallback_warn(session, "pick", exc)
        await _say(
            update,
            f"❌ model pick failed: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    refs = _oc_refs(models)
    try:
        ref = refs[int(index)]
    except (ValueError, IndexError):
        log.warning(
            "model_pick_stale session=%s index=%s available=%s",
            session,
            index,
            len(refs),
        )
        await _show_models(update, context, session)
        return
    await _apply_model(
        update,
        context,
        session,
        f"{ref.get('providerID')}/{ref.get('id')}",
    )


async def _oc_apply_model(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: str,
    row: dict,
    oc: tuple,
    model: str,
) -> bool:
    """Hot-switch model AND effort through the lane's own server.

    Returns True when the request is fully handled; False means "fall back to
    today's amux PATCH" — either the id is not in this server's catalog (the
    owner typed a custom id, which the PATCH applies verbatim) or the API
    failed part-way. In both cases ``model`` is exactly what was asked for,
    so the fallback applies the RIGHT id, never an index against another list.
    """
    oc_client, pin = oc
    provider = str(row.get("provider") or "")
    try:
        catalog = await oc_client.list_models(provider)
        sess = await oc_client.get_session(pin)
    except AmuxError as exc:
        _oc_fallback_warn(session, "model", exc)
        return False
    current = sess.get("model") or {}
    target = _resolve_oc_model(_oc_refs(catalog), model, current)
    if target is None:
        return False  # not catalogued here: the typed-id PATCH owns it
    variants = [str(v) for v in target.get("variants") or []]
    current_variant = str(current.get("variant") or "")
    # Carry the effort over only when the new model HAS that dial; otherwise
    # the server's own default for the new model is the honest answer.
    variant = current_variant if current_variant in variants else None
    try:
        await oc_client.set_model(
            pin,
            str(target.get("providerID") or ""),
            str(target.get("id") or ""),
            variant,
        )
    except AmuxError as exc:
        _oc_fallback_warn(session, "model", exc)
        return False
    _oc_fallback_ok(session)
    log.info(
        "session_model_set session=%s model=%s provider=%s variant=%s path=oc_api",
        session,
        target.get("id"),
        target.get("providerID"),
        variant or "(default)",
    )
    note = f"✅ model set to `{target.get('id')}`"
    if variant:
        note += f" · effort `{variant}`"
    await _show_control(update, context, session, note=note)
    return True


async def _apply_model(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str, model: str
) -> None:
    """Apply ``model`` (``""`` = server default) to the session.

    An opencode lane whose serve API answers gets a HOT switch — model and
    effort in one POST, no restart. Everything else, and every API failure,
    keeps amux's config PATCH (today's behavior: a non-claude change restarts
    the lane and the conversation resumes).
    """
    client = _client(context)
    try:
        row = await client.find(session)
    except AmuxError as exc:
        log.warning(
            "session_model_failed session=%s model=%s status=%s detail=%s",
            session,
            model or "(default)",
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ model change refused: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    if model:
        oc = await _oc_client(context, row, "model")
        if oc and await _oc_apply_model(update, context, session, row, oc, model):
            return
    try:
        result = await client.config(session, model=model)
    except AmuxError as exc:
        log.warning(
            "session_model_failed session=%s model=%s status=%s detail=%s",
            session,
            model or "(default)",
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ model change refused: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    log.info(
        "session_model_set session=%s model=%s mode=%s applied=%s path=amux",
        session,
        model or "(default)",
        result.get("mode"),
        result.get("applied"),
    )
    message = str(result.get("message") or f"model set to {model or 'server default'}")
    await _show_control(update, context, session, note=f"✅ {message}")


async def _show_effort(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str
) -> None:
    try:
        row = await _client(context).find(session)
    except AmuxError as exc:
        log.warning(
            "control_effort_failed session=%s status=%s detail=%s",
            session,
            exc.status,
            exc.detail,
        )
        await _say(update, f"❌ effort list failed: {exc.detail}")
        return
    provider = str(row.get("provider") or "")
    # An opencode lane's effort levels are the current model's `variants`
    # (GET /api/model on its own server) — Milestone 9's whole point: the
    # dial exists, it was only never reachable from here.
    oc = await _oc_client(context, row, "effort")
    if oc:
        try:
            sess = await oc[0].get_session(oc[1])
            catalog = await oc[0].list_models(provider)
        except AmuxError as exc:
            _oc_fallback_warn(session, "effort", exc)
            await _say(
                update,
                f"❌ effort list failed: {exc.detail}",
                reply_markup=_back_keyboard(session),
            )
            return
        _oc_fallback_ok(session)
        model = sess.get("model") or {}
        entry = _oc_entry(_oc_refs(catalog), model)
        variants = [str(v) for v in (entry or {}).get("variants") or []]
        current = str(model.get("variant") or "")
        if current not in variants:
            current = ""  # "default"/unknown reads as the provider default
        model_id = str(model.get("id") or "?")
        if not variants:
            await _say(
                update,
                f"🧠 `{model_id}` reports no effort levels — OpenCode gives "
                "this model no dial, so there is nothing to set.",
                reply_markup=_back_keyboard(session),
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        await _say(
            update,
            f"🧠 *Effort* — `{session}`\nmodel: `{model_id}`\n"
            f"current: `{current or 'provider default'}`",
            reply_markup=effort_keyboard(session, current, levels=variants),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if _oc_expected(row):
        # The dial exists (its levels live on the OpenCode server) but that
        # server did not answer: report the truth rather than the stale
        # "opencode has no effort switch" reason.
        await _say(
            update,
            "❌ effort list unavailable — this lane's OpenCode server is "
            "unreachable right now.",
            reply_markup=_back_keyboard(session),
        )
        return
    if not effort_supported(provider):
        # Say WHY instead of writing a flag the lane's CLI cannot parse.
        log.warning(
            "session_effort_unsupported session=%s provider=%s",
            session,
            provider or "(unknown)",
        )
        await _say(
            update,
            f"🧠 `{provider or '?'}` has no effort switch amux can drive. "
            "amux applies `--effort` to claude lanes only (the dashboard's own "
            "rule); other providers take their reasoning level from the model, "
            "and opencode's CLI would reject the flag outright.",
            reply_markup=_back_keyboard(session),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    current = session_effort(row)
    await _say(
        update,
        f"🧠 *Effort* — `{session}`\ncurrent: `{current or 'provider default'}`",
        reply_markup=effort_keyboard(session, current),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _apply_effort(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str, level: str
) -> None:
    """Set the effort: a variant of the current model on opencode, the
    ``--effort`` config PATCH on claude — the two paths Milestone 8 and 9 own
    respectively. Never an effort PATCH on an opencode lane: the flag would
    ride ``CC_FLAGS`` to the next launch and kill it there.
    """
    client = _client(context)
    try:
        row = await client.find(session)
    except AmuxError as exc:
        log.warning(
            "session_effort_failed session=%s level=%s status=%s detail=%s",
            session,
            level or "(default)",
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ effort change refused: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    provider = str(row.get("provider") or "")
    oc = await _oc_client(context, row, "effort")
    if oc:
        oc_client, pin = oc
        try:
            sess = await oc_client.get_session(pin)
            catalog = await oc_client.list_models(provider)
        except AmuxError as exc:
            _oc_fallback_warn(session, "effort", exc)
            await _say(
                update,
                f"❌ effort change refused: {exc.detail}",
                reply_markup=_back_keyboard(session),
            )
            return
        model = sess.get("model") or {}
        entry = _oc_entry(_oc_refs(catalog), model)
        variants = [str(v) for v in (entry or {}).get("variants") or []]
        if level and level not in variants:
            # The level must exist on THIS model: a stale tap (the model
            # changed underneath) is refused before any POST.
            log.warning("session_effort_invalid session=%s level=%s", session, level)
            await _say(
                update,
                f"❌ unknown effort `{level}` — "
                f"one of: {', '.join(variants) or '(this model has none)'}",
                reply_markup=_back_keyboard(session),
            )
            return
        try:
            await oc_client.set_model(
                pin,
                str(model.get("providerID") or ""),
                str(model.get("id") or ""),
                level or None,
            )
        except AmuxError as exc:
            _oc_fallback_warn(session, "effort", exc)
            await _say(
                update,
                f"❌ effort change refused: {exc.detail}",
                reply_markup=_back_keyboard(session),
            )
            return
        _oc_fallback_ok(session)
        log.info(
            "session_effort_set session=%s effort=%s path=oc_api",
            session,
            level or "(default)",
        )
        await _show_control(
            update, context, session, note=f"✅ effort set to `{level or 'default'}`"
        )
        return
    if _oc_expected(row):
        # Pinned opencode lane whose serve API did not answer: say THAT
        # instead of the stale "opencode has no effort" reason, and never
        # fall through to a PATCH that would kill the lane at its next start.
        await _say(
            update,
            "❌ effort unavailable — this lane's OpenCode server is "
            "unreachable right now.",
            reply_markup=_back_keyboard(session),
        )
        return
    if not effort_supported(provider):
        log.warning(
            "session_effort_unsupported session=%s provider=%s",
            session,
            provider,
        )
        await _show_effort(update, context, session)
        return
    if level and level not in EFFORTS:
        log.warning("session_effort_invalid session=%s level=%s", session, level)
        await _say(
            update,
            f"❌ unknown effort `{level}` — one of: {', '.join(EFFORTS)}",
            reply_markup=_back_keyboard(session),
        )
        return
    try:
        result = await client.config(session, effort=level)
    except AmuxError as exc:
        log.warning(
            "session_effort_failed session=%s level=%s status=%s detail=%s",
            session,
            level or "(default)",
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ effort change refused: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    log.info(
        "session_effort_set session=%s effort=%s mode=%s applied=%s path=amux",
        session,
        level or "(default)",
        result.get("mode"),
        result.get("applied"),
    )
    message = str(result.get("message") or f"effort set to {level or 'default'}")
    await _show_control(update, context, session, note=f"✅ {message}")


async def _interrupt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: str
) -> None:
    """Stop the CURRENT turn, never the lane.

    Deliberately a different verb from ⏹️ stop: the owner's "make it stop
    thinking" must not tear down the session, its topic binding or its
    conversation. The preferred transport is OpenCode's own
    ``POST /api/session/<id>/interrupt`` (the TUI's own interrupt); every
    other lane, and any API failure, keeps pressing Escape into the pane —
    both end in ``session_interrupted``, with ``path=`` naming which ran.
    """
    client = _client(context)
    try:
        row = await client.find(session)
    except AmuxError:
        row = None  # the keys path below reports the failure it actually hits
    if row is not None:
        oc = await _oc_client(context, row, "interrupt")
        if oc:
            try:
                result = await oc[0].interrupt(oc[1])
            except AmuxError as exc:
                _oc_fallback_warn(session, "interrupt", exc)
            else:
                _oc_fallback_ok(session)
                log.info(
                    "session_interrupted session=%s path=oc_api interrupted=%s",
                    session,
                    result.get("interrupted"),
                )
                await _show_control(
                    update,
                    context,
                    session,
                    note="⚡ current turn interrupted (OpenCode API)",
                )
                return
    try:
        result = await client.keys(session, "Escape")
    except AmuxError as exc:
        log.warning(
            "session_interrupt_failed session=%s status=%s detail=%s",
            session,
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ interrupt failed: {exc.detail}",
            reply_markup=_back_keyboard(session),
        )
        return
    effect = str(result.get("effect") or ("interrupted" if result.get("ok") else "unknown"))
    log.info(
        "session_interrupted session=%s effect=%s path=keys detail=%s",
        session,
        effect,
        result.get("message"),
    )
    await _show_control(
        update, context, session, note=f"⚡ current turn interrupted ({effect})"
    )


async def _apply_rename(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    old: str,
    new: str,
    *,
    source: str,
) -> None:
    """``PATCH config {"rename": …}``, then carry the name through everything.

    The server's rename cascade moves the lane (env file, tmux session,
    worktree, board); the GATEWAY's own maps are keyed by the old name, so
    without this second cascade the topic would keep its pin (fine) while the
    turn anchor, activity stamps, pins, dedupe and quote map all pointed at a
    name the fleet no longer has. Both halves are required — that is the point
    of the feature.
    """
    new = " ".join((new or "").split())
    state = _state(context)
    chat_id = update.effective_chat.id
    if not new:
        await _say(update, "❌ a name cannot be empty.")
        return
    if new == old:
        await _say(update, f"ℹ️ `{old}` already has that name.", parse_mode=ParseMode.MARKDOWN)
        return
    # The rename window: an in-flight message must not read a 404 mid-PATCH as
    # a dead session and release the topic (see _report_send_error).
    pending = _renaming(context)
    pending[(chat_id, old)] = new
    client = _client(context)
    try:
        result = await client.config(old, rename=new)
    except AmuxError as exc:
        log.warning(
            "session_rename_failed old=%s new=%s status=%s detail=%s",
            old,
            new,
            exc.status,
            exc.detail,
        )
        await _say(
            update,
            f"❌ rename refused: {exc.detail}",
            reply_markup=_back_keyboard(old),
        )
        return
    finally:
        pending.pop((chat_id, old), None)
    new = str(result.get("name") or new)

    threads = state.rename_session(chat_id, old, new)
    # In-memory maps the cascade cannot reach (bot_data, one-WARN-per-stretch).
    recent = context.application.bot_data.get(RECENT_STARTS)
    if isinstance(recent, dict):
        for key in [k for k, (value, _ts) in recent.items() if value == old]:
            recent.pop(key, None)
    _no_boundary_warned.discard((chat_id, old))
    _cards_warned.discard((chat_id, old))
    await _follow_topic_titles(context, chat_id, threads, new, source=source)
    log.info(
        "session_renamed old=%s new=%s source=%s topics=%s",
        old,
        new,
        source,
        len(threads),
    )
    await _show_control(update, context, new, note=f"✅ renamed to `{new}`")


# ---- the ✏️ rename / ⌨️ custom-model prompt conversation --------------------

PENDING_ASK = "pending_ask"


def _pending_ask(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.application.bot_data.setdefault(PENDING_ASK, {})


def _ask_session(data: str, verb: str) -> str:
    return (data or "").removeprefix(PREFIX + verb)


async def _ask(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, session: str, prompt: str
) -> int:
    query = update.callback_query
    if query is not None:
        try:
            await query.answer()
        except TelegramError:
            pass
    _pending_ask(context)[(update.effective_chat.id, update.effective_user.id)] = {
        "action": action,
        "session": session,
    }
    await _say(update, prompt, parse_mode=ParseMode.MARKDOWN)
    return ASK


@allow_only
async def ask_rename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = _ask_session(update.callback_query.data, "ctlr:")
    return await _ask(
        update,
        context,
        "rename",
        session,
        f"✏️ Send the new name for `{session}` (or /cancel).",
    )


@allow_only
async def ask_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    session = _ask_session(update.callback_query.data, "ctlc:")
    return await _ask(
        update,
        context,
        "model",
        session,
        f"⌨️ Send the model id for `{session}` (or /cancel).",
    )


@allow_only
async def receive_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = (update.effective_chat.id, update.effective_user.id)
    pending = _pending_ask(context).pop(key, None)
    if not pending:
        await update.message.reply_text("Nothing was being asked — /menu")
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    log.info(
        "ask_answer user=%s session=%s action=%s chars=%s",
        update.effective_user.id,
        pending["session"],
        pending["action"],
        len(text),
    )
    if pending["action"] == "rename":
        await _apply_rename(
            update, context, pending["session"], text, source="rename_prompt"
        )
    else:
        await _apply_model(update, context, pending["session"], text)
    return ConversationHandler.END


@allow_only
async def cancel_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    key = (update.effective_chat.id, update.effective_user.id)
    dropped = _pending_ask(context).pop(key, None)
    log.info(
        "ask_cancelled user=%s action=%s",
        update.effective_user.id,
        dropped["action"] if dropped else "-",
    )
    await update.message.reply_text("Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END


# ---- slash commands (shortcuts over the same panel) -----------------------


@allow_only
async def ctl_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info(
        "command ctl user=%s chat=%s",
        update.effective_user.id,
        update.effective_chat.id,
    )
    session = await _target_session(update, context)
    if not session:
        log.info(
            "control_no_session user=%s chat=%s",
            update.effective_user.id,
            update.effective_chat.id,
        )
        await _say(
            update,
            "No session here yet — send a message in a topic, or /sessions to pick one.",
        )
        return
    await _show_control(update, context, session)


@allow_only
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info(
        "command model user=%s chat=%s args=%s",
        update.effective_user.id,
        update.effective_chat.id,
        context.args,
    )
    session = await _target_session(update, context)
    if not session:
        await _say(update, "No session selected — /sessions")
        return
    if context.args:
        await _apply_model(update, context, session, context.args[0])
        return
    await _show_models(update, context, session)


@allow_only
async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info(
        "command effort user=%s chat=%s args=%s",
        update.effective_user.id,
        update.effective_chat.id,
        context.args,
    )
    session = await _target_session(update, context)
    if not session:
        await _say(update, "No session selected — /sessions")
        return
    if context.args:
        await _apply_effort(update, context, session, context.args[0].lower())
        return
    await _show_effort(update, context, session)


@allow_only
async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """``/name <new>`` now, ``/name`` alone → ask for it (entry point)."""
    log.info(
        "command name user=%s chat=%s args=%s",
        update.effective_user.id,
        update.effective_chat.id,
        context.args,
    )
    session = await _target_session(update, context)
    if not session:
        await _say(update, "No session selected — /sessions")
        return ConversationHandler.END
    if context.args:
        await _apply_rename(
            update, context, session, " ".join(context.args), source="command"
        )
        return ConversationHandler.END
    return await _ask(
        update,
        context,
        "rename",
        session,
        f"✏️ Send the new name for `{session}` (or /cancel).",
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
    elif action == "ctl":
        await _show_control(update, context, raw)
    elif action == "ctlm":
        # `raw` is the session, optionally `:<page>` — the page only says
        # "render again": the source (amux vs OpenCode) is re-derived at
        # render time, never read back from a tap.
        session, _, page = raw.partition(":")
        await _show_models(update, context, session, page)
    elif action == "ctlp":
        session, _, index = raw.partition(":")
        await _pick_model(update, context, session, index)
    elif action == "ctlo":
        session, _, index = raw.partition(":")
        await _pick_oc_model(update, context, session, index)
    elif action == "ctle":
        await _show_effort(update, context, raw)
    elif action == "ctlf":
        session, _, level = raw.partition(":")
        await _apply_effort(update, context, session, level)
    elif action == "ctli":
        await _interrupt(update, context, raw)
    elif action in ("ctlr", "ctlc"):
        # The prompt conversation owns both as entry points, registered before
        # this handler: reaching here means that wiring broke. Log it (a button
        # that does nothing must be impossible to mistake for silence) and fall
        # back to the panel rather than leaving the tap unanswered.
        log.warning(
            "ask_entry_missed action=%s target=%s chat=%s",
            action,
            raw,
            update.effective_chat.id,
        )
        await _show_control(update, context, raw)
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
        # The topic identifies the session — no reply or quote needed. The
        # binding resolves through its pin, so a rename (ours or the
        # dashboard's) cannot orphan it.
        session = await _resolve_topic_session(context, update.effective_chat.id, thread)
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

    # Pin an opencode manager to a conversation id we mint: it rides the lane's
    # CC_FLAGS on EVERY start (amux replays explicit flags verbatim), so an
    # idle stop only frees RAM and the next message in the topic resumes this
    # same conversation. Only opencode has a create-or-resume `--session`
    # flag, and the sweep only ever stops a PINNED session — so other
    # providers, and every manager created before this change, keep running
    # exactly as they do today (owner decision 2026-10-27: "Leave them").
    oc_session = new_opencode_session_id() if choice["provider"] == "opencode" else None
    flags = f"--session {oc_session}" if oc_session else None
    if oc_session and choice["model"]:
        # `--model` in CC_FLAGS reaches the opencode TUI, which rejects the
        # flag (measured 2026-10-27: prints usage, EXIT=1) and the lane never
        # starts. amux lets explicit flags win over `model`, so sending BOTH
        # would still reach the TUI as a bare `--model` — the model is
        # therefore dropped for opencode until the real fix lands in amux
        # (owner: "Fix it later, after we make sure the core works").
        log.warning(
            "opencode_model_dropped model=%s session=%s",
            choice["model"],
            name,
        )
        choice = dict(choice, model=None)

    # The topic is bound BEFORE the lane exists: an owner message arriving
    # while the lane is still being created must already route somewhere.
    thread = topic if forum else None
    if forum and thread is None:
        thread = await _create_forum_topic(update, context, name)
    if forum and thread is not None:
        # `created` is what may later be renamed to OpenCode's session title:
        # only a topic THIS gateway minted (General path), never one adopted
        # from the owner's hand or opened through POST /topic.
        state.set_topic(chat_id, thread, name, created=topic is None)

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

    ack = await update.message.reply_text(
        ack_text,
        parse_mode=ParseMode.MARKDOWN,
        # The topic's control panel, one tap away from the message that binds
        # it. It survives the failure edit below (Telegram keeps the markup
        # when edit_text carries none) — tapping it then reports the missing
        # session, which is the honest answer anyway.
        reply_markup=control_button(name),
    )
    state.record_message(chat_id, ack.message_id, name)
    state.set_turn_message(chat_id, name, anchor_id, thread=in_thread)
    if anchor is not None:
        # Quoting the transcription must continue this manager session.
        state.record_message(chat_id, anchor, name)

    try:
        name, row = await create_manager(
            client,
            cfg,
            name=name,
            provider=choice["provider"],
            model=choice["model"],
            flags=flags,
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

    if oc_session:
        # Only now that the lane exists: a pin on a lane that failed to start
        # would make the sweep stop something that never ran.
        state.set_pinned(chat_id, name, oc_session)
        log.info("manager_pinned name=%s oc_session=%s", name, oc_session)

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
        session = await _resolve_topic_session(context, update.effective_chat.id, thread)
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


# /cancel for BOTH conversations is `cancel_ask` — one handler, registered as
# the conversation's single fallback, so a tap of /cancel can never clear one
# state and leave the other's pending entry behind.


# ---------------------------------------------------------------------------
# reply relay
# ---------------------------------------------------------------------------


# Sessions whose pane currently offers NO answer boundary. One WARN per
# stretch (reset by a successful extract): a miss repeats every relay cycle,
# and this exact silence hid the 2026-10-27 missing answers — the relay
# skipped every reply for hours without leaving a single line to sweep. Every
# REPEAT now also logs its cycle count (`relay_no_boundary cycles=N`), so a
# miss that never ends reads as a growing number instead of one stale line.
_no_boundary_warned: set[tuple[int, str]] = set()
_no_boundary_cycles: dict[tuple[int, str], int] = {}

# The API path's own one-WARN-per-stretch bookkeeping: `relay_source=api`
# while the OpenCode service answers, `relay_api_unavailable` when it does not
# (and the pane scrape takes over). Once per stretch each — the relay ticks
# every 20s, so per-cycle logging would bury everything else in the file.
_api_source: set[tuple[int, str]] = set()
_api_unavailable: set[tuple[int, str]] = set()
# `since_id` fell outside the fetched window: nothing is sent on purpose (a
# flood is worse than a stall), and this says so instead of looking like a
# quiet, healthy relay.
_api_window_missed: set[tuple[int, str]] = set()


def _relay_pin(state: GatewayState, chat_id: int, session: str) -> str | None:
    """The OpenCode conversation id to read for this session, else ``None``.

    Only a PINNED opencode manager has one — the ``--session ses_…`` minted at
    create, which is both the lane's resume key and the API's session id — so
    legacy managers and every non-opencode provider keep the pane scrape
    untouched (owner, 2026-10-28: "Legacy or unpinned opencode lanes, and any
    non-opencode provider … keep the pane path unchanged"). The topic's pin is
    the fallback for a session whose name no longer matches its map entry.
    """
    pin = state.pinned(chat_id, session)
    if pin:
        return pin
    thread = state.session_topic(chat_id, session)
    if thread is not None:
        return state.topic_pin(chat_id, thread)
    return None


def _note_api_source(chat_id: int, session: str, pin: str) -> None:
    """The OpenCode service delivered: log it once, close any pane miss."""
    key = (chat_id, session)
    if key not in _api_source:
        _api_source.add(key)
        log.info(
            "relay_source chat_id=%s session=%s source=api pin=%s",
            chat_id,
            session,
            pin,
        )
    _api_unavailable.discard(key)
    if key in _no_boundary_warned:
        # Delivery resumed through the API: the pane's miss stretch is over.
        _no_boundary_warned.discard(key)
        log.info(
            "relay_boundary_recovered chat_id=%s session=%s cycles=%s source=api",
            chat_id,
            session,
            _no_boundary_cycles.pop(key, 0),
        )


def _note_api_unavailable(chat_id: int, session: str, exc) -> None:
    """Failure is a FALLBACK, not silence: the pane path runs right after this."""
    key = (chat_id, session)
    _api_source.discard(key)
    if key in _api_unavailable:
        return
    _api_unavailable.add(key)
    status = getattr(exc, "status", 0)
    detail = getattr(exc, "detail", None) or str(exc)
    log.warning(
        "relay_api_unavailable chat_id=%s session=%s error=%s fallback=pane",
        chat_id,
        session,
        f"HTTP {status}: {detail}" if status else detail,
    )


def _note_api_window(chat_id: int, session: str, since_id: str, fetched: int) -> None:
    key = (chat_id, session)
    if key in _api_window_missed:
        return
    _api_window_missed.add(key)
    log.warning(
        "relay_api_window_missed chat_id=%s session=%s since=%s fetched=%s "
        "hint=the relayed message id is not in the fetched history — nothing "
        "sent on purpose; reset the session's relay state to re-bootstrap",
        chat_id,
        session,
        since_id,
        fetched,
    )


async def _relay_api(app, oc, cfg, state, chat_id, session, pin: str) -> bool:
    """Relay an answer from the OpenCode service; ``False`` = use the pane.

    This is Task 1's replacement channel (owner, 2026-10-28): the service
    returns the conversation as JSON — no viewport, no ANSI, no chrome, no
    sentinel scrolling out of a 50-row pane — so a 1,994-char answer arrives
    whole. EVERY failure (no service, auth, 404, timeout, unreachable) returns
    ``False`` after one WARN, and the caller then runs today's pane scrape:
    failure must never become silence.
    """
    try:
        client = await oc.client_for_service(
            url=cfg.opencode_service_url,
            password=cfg.opencode_service_password,
            service_json=cfg.opencode_service_json,
        )
    except AmuxError as exc:
        _note_api_unavailable(chat_id, session, exc)
        return False
    since = state.oc_relayed_id(chat_id, session)
    try:
        messages = await client.messages(pin, since_id=since)
    except AmuxError as exc:
        _note_api_unavailable(chat_id, session, exc)
        return False
    finally:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
    _note_api_source(chat_id, session, pin)
    plan = plan_opencode_relay(
        messages, since_id=since, pane_text=state.last_relayed(chat_id, session)
    )
    # Same anchor rule as the pane path: INTO the topic, and only a reply to
    # an anchor that lives in that very topic (never across threads).
    thread_id = state.session_topic(chat_id, session)
    reply_to = state.turn_message(chat_id, session)
    if thread_id is not None and state.turn_thread(chat_id, session) != thread_id:
        reply_to = None
    stored = since
    for message_id, text in plan.items:
        try:
            sent = await _send_markdown(
                app.bot, chat_id, text, reply_to=reply_to, thread_id=thread_id
            )
        except TelegramError as exc:
            log.warning(
                "relay_send_failed chat_id=%s session=%s error=%s", chat_id, session, exc
            )
            # `stored` stops at the last message that DID go out, so the rest
            # is retried on the next cycle instead of skipped forever.
            break
        for message in sent:
            _track(state, chat_id, session, message)
        stored = message_id
        state.touch_activity(chat_id, session, "output")
        log.info(
            "relay_sent chat_id=%s session=%s chars=%s total=%s reply_to=%s "
            "thread=%s source=api",
            chat_id,
            session,
            len(text),
            len(text),
            reply_to,
            thread_id,
        )
    if plan.reason == "since_id_not_in_window":
        _note_api_window(chat_id, session, since or "-", len(messages))
    else:
        # Progress (or a healthy read): the window stretch is over.
        _api_window_missed.discard((chat_id, session))
    if stored != since:
        if stored:
            state.set_oc_relayed_id(chat_id, session, stored)
        if plan.adopt_id and not plan.items:
            log.info(
                "relay_api_baseline chat_id=%s session=%s msg_id=%s reason=%s",
                chat_id,
                session,
                plan.adopt_id,
                plan.reason,
            )
    return True


async def relay_once(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    client: AmuxClient = app.bot_data["client"]
    state: GatewayState = app.bot_data["state"]
    # Same injection rule as the panel's `_oc_api`: tests hand in a fake, the
    # production bot runs the real module (no test may run `ss` or read /proc).
    oc = app.bot_data.get("oc_api") or opencode_api
    for chat_id, sessions in state.sessions_to_relay().items():
        for session in sessions:
            if state.idle_stopped(chat_id, session) is not None:
                # The sweeper stopped this lane: its pane is gone, so there is
                # nothing to peek and no answer to relay. The marker is cleared
                # by the next prompt (which starts the lane again).
                continue
            pin = _relay_pin(state, chat_id, session)
            if pin and await _relay_api(app, oc, cfg, state, chat_id, session, pin):
                continue  # the service answered; the pane stays out of this cycle
            try:
                raw = await client.peek(session, lines=cfg.peek_lines)
            except AmuxError as exc:
                log.debug("relay_peek_failed chat_id=%s session=%s error=%s", chat_id, session, exc)
                continue
            text = extract_reply(raw)
            key = (chat_id, session)
            if not text:
                cycles = _no_boundary_cycles.get(key, 0) + 1
                _no_boundary_cycles[key] = cycles
                _no_boundary_warned.add(key)
                # Every repeat is logged with its cycle count (owner,
                # 2026-10-28): a miss that never ends must read as a GROWING
                # number a sweep can catch, not as one stale line.
                log.warning(
                    "relay_no_boundary cycles=%s chat_id=%s session=%s "
                    "pane_chars=%s peek_lines=%s hint=no '—— answer ——' sentinel "
                    "in the reply and the [from Telegram @…] prompt marker "
                    "scrolled out of the viewport — nothing can be delivered "
                    "until one is visible",
                    cycles,
                    chat_id,
                    session,
                    len(raw),
                    cfg.peek_lines,
                )
                continue
            if key in _no_boundary_warned:
                # The boundary is back after a miss: say so, with how many
                # cycles the silence lasted, so the log shows it ENDED.
                _no_boundary_warned.discard(key)
                log.info(
                    "relay_boundary_recovered chat_id=%s session=%s cycles=%s "
                    "source=pane",
                    chat_id,
                    session,
                    _no_boundary_cycles.pop(key, 0),
                )
            else:
                _no_boundary_cycles.pop(key, None)
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
            # New agent output IS activity (owner, 2026-10-27): a session still
            # producing answers is never swept, however long the turn runs.
            state.touch_activity(chat_id, session, "output")
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


# ---------------------------------------------------------------------------
# idle sweep: stop a pinned manager that has been quiet, keep its topic
# ---------------------------------------------------------------------------

IDLE_SWEEP_TICK_S = 60.0
# A lane that owns board cards is skipped, and would be skipped every tick:
# one WARN per stretch (the shape `_no_boundary_warned` uses), so a sweep over
# the log still shows WHY nothing was stopped.
_cards_warned: set[tuple[int, str]] = set()


async def idle_sweep_once(app: Application) -> None:
    """Stop pinned manager lanes idle for ``cfg.idle_stop_s`` (owner: 30 min).

    The contract, and the reason each guard exists:

    - **PINNED only.** The pin (`--session <id>` in the lane's CC_FLAGS) is
      what makes a later start resume the SAME OpenCode conversation, so a
      session without one (created before this change, or another provider)
      is never stopped: owner decision 2026-10-27, "Leave them as today".
    - **Owner silence AND no new output.** Either stamp resets the clock, so a
      long-running turn is never cut off mid-task.
    - **Cards first.** amux's bootstrap auto-restarts a stopped worker that
      owns a waiting Todo/Doing card; stopping one anyway would be undone
      within a sweep (option B, declined). We skip and say so.
    - **The topic binding is never touched.** That is the whole feature: the
      marker recorded here is what the next prompt finds, to start the lane
      again and resume its conversation.
    """
    cfg: Config = app.bot_data["cfg"]
    client: AmuxClient = app.bot_data["client"]
    state: GatewayState = app.bot_data["state"]
    if not cfg.idle_stop_s:
        return
    now = time.time()
    try:
        rows = await client.list_sessions()
    except AmuxError as exc:
        log.debug("idle_sweep_unreachable error=%s", exc)
        return
    live = {
        str(row.get("name")): row
        for row in rows
        if isinstance(row, dict) and row.get("name")
    }
    for chat_id, sessions in state.tracked_sessions().items():
        for session in sessions:
            if not state.pinned(chat_id, session):
                continue
            marked = state.idle_stopped(chat_id, session)
            row = live.get(session)
            if marked is not None:
                if row is not None and row.get("running"):
                    # Something brought it back (amux's own bootstrap): track
                    # it again instead of fighting it over and over.
                    state.clear_idle_stopped(chat_id, session)
                    log.info(
                        "idle_stop_undone chat_id=%s session=%s reason=running_again",
                        chat_id,
                        session,
                    )
                continue
            is_running = None if row is None else row.get("running")
            if is_running is None and row is not None:
                continue  # a row that will not say: leave it alone
            if row is None or is_running is False:
                # Gone or already stopped (the ⏹️ button, a reboot): mark it
                # so the relay stops peeking a dead pane and the next prompt
                # revives the lane instead of sending into a corpse.
                state.mark_idle_stopped(chat_id, session)
                log.info(
                    "idle_stop chat_id=%s session=%s reason=not_running",
                    chat_id,
                    session,
                )
                continue
            last = state.last_activity(chat_id, session)
            if last is None:
                continue  # no activity recorded: not ours to judge
            idle = now - last
            if idle < cfg.idle_stop_s:
                continue
            cards = await client.todo_cards(session)
            if cards:
                if (chat_id, session) not in _cards_warned:
                    _cards_warned.add((chat_id, session))
                    log.warning(
                        "idle_stop_skipped_cards chat_id=%s session=%s cards=%s "
                        "hint=amux restarts a stopped worker that owns a waiting card",
                        chat_id,
                        session,
                        ",".join(str(card.get("id")) for card in cards),
                    )
                continue
            _cards_warned.discard((chat_id, session))
            try:
                await client.stop(session)
            except AmuxError as exc:
                log.warning(
                    "idle_stop_failed chat_id=%s session=%s status=%s detail=%s",
                    chat_id,
                    session,
                    exc.status,
                    exc.detail,
                )
                continue
            state.mark_idle_stopped(chat_id, session)
            log.info(
                "idle_stop chat_id=%s session=%s idle_s=%.0f threshold_s=%.0f cards=0",
                chat_id,
                session,
                idle,
                cfg.idle_stop_s,
            )


async def idle_sweep_loop(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    while True:
        await asyncio.sleep(IDLE_SWEEP_TICK_S)
        try:
            await idle_sweep_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad cycle kill the loop
            log.exception("idle_sweep_failed")



# ---------------------------------------------------------------------------
# topic titles: a gateway-made topic shows its OpenCode session's name
# ---------------------------------------------------------------------------

# One WARN per rename that fails (retried next tick while it stays eligible).
_rename_warned: set[tuple[int, int]] = set()


async def sync_topic_titles_once(app: Application) -> None:
    """Rename a gateway-made topic to its OpenCode session's title.

    Owner, 2026-10-27: "use meaningful names for topics — it should match the
    session name in opencode". A topic is born `mgr-…` (the lane name) while
    OpenCode titles the same conversation from its content seconds after the
    first turn; the topic follows that title.

    Eligibility is deliberately narrow, and each guard has a reason:

    - **pinned session** — only an opencode lane has the id that selects its
      title (legacy/other-provider lanes are never touched).
    - **topic the GATEWAY minted** — a topic you made by hand, or one a manager
      opened through ``POST /topic``, keeps the name it was given.
    - **recorded name still == the session name** — a renamed topic drops out
      of the sweep here, which is what makes this ONE rename per topic (no
      churn, no process spawned when nothing is pending) rather than a follower
      of every later retitle.

    The Bot API cannot read a topic's title back, so `state.topic_names` is the
    only record of what a topic is called: the rename writes it, and the same
    field is the dedupe.
    """
    cfg: Config = app.bot_data["cfg"]
    state: GatewayState = app.bot_data["state"]
    if not cfg.topic_sync_s:
        return
    candidates: list[tuple[int, int, str, str]] = []
    for chat_id, sessions in state.tracked_sessions().items():
        for session in sessions:
            thread = state.session_topic(chat_id, session)
            if thread is None:
                continue
            pin = state.pinned(chat_id, session)
            if not pin:
                continue
            if not state.topic_created(chat_id, thread):
                continue
            if (state.topic_name(chat_id, thread) or session) != session:
                continue  # already renamed, or named deliberately elsewhere
            candidates.append((chat_id, thread, session, pin))
    if not candidates:
        return
    titles = await read_titles(cfg.manager_cwd)
    bot = app.bot
    for chat_id, thread, session, pin in candidates:
        title = clean_title(titles.get(pin))
        if title is None:
            # No title yet (OpenCode's title job still running) or a stub:
            # wait for the real one — a topic is renamed only once.
            continue
        try:
            await bot.edit_forum_topic(chat_id, message_thread_id=thread, name=title)
        except TelegramError as exc:
            if (chat_id, thread) not in _rename_warned:
                _rename_warned.add((chat_id, thread))
                log.warning(
                    "topic_rename_failed chat_id=%s thread=%s session=%s "
                    "title=%s error=%s",
                    chat_id,
                    thread,
                    session,
                    title,
                    exc,
                )
            continue
        _rename_warned.discard((chat_id, thread))
        previous = state.topic_name(chat_id, thread) or session
        state.set_topic_name(chat_id, thread, title)
        log.info(
            "topic_renamed chat_id=%s thread=%s session=%s from=%s to=%s "
            "source=opencode_title",
            chat_id,
            thread,
            session,
            previous,
            title,
        )


async def sync_topic_titles_loop(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    while True:
        await asyncio.sleep(cfg.topic_sync_s)
        try:
            await sync_topic_titles_once(app)
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad cycle kill the loop
            log.exception("topic_sync_failed")


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
    app.bot_data["idle_task"] = asyncio.create_task(idle_sweep_loop(app))
    app.bot_data["title_task"] = asyncio.create_task(sync_topic_titles_loop(app))
    log.info(
        "gateway_started allowed_users=%d amux_url=%s voice=%s relay_interval_s=%s "
        "idle_stop_s=%s topic_sync_s=%s",
        len(cfg.allowed_user_ids),
        cfg.amux_url,
        "on" if cfg.groq_api_key else "off",
        cfg.relay_interval_s,
        f"{cfg.idle_stop_s:g}" if cfg.idle_stop_s else "off",
        f"{cfg.topic_sync_s:g}" if cfg.topic_sync_s else "off",
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
    idle_task = app.bot_data.get("idle_task")
    if idle_task is not None:
        idle_task.cancel()
        try:
            await idle_task
        except asyncio.CancelledError:
            pass
    title_task = app.bot_data.get("title_task")
    if title_task is not None:
        title_task.cancel()
        try:
            await title_task
        except asyncio.CancelledError:
            pass
    client: AmuxClient = app.bot_data["client"]
    await client.aclose()


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------


async def on_poll_error(update, context) -> None:
    """PTB's error handler: a conflict gets vitals, everything else its traceback.

    Before this, PTB logged *"No error handlers are registered, logging
    exception"* and dumped a raw traceback (measured 7× on 2026-10-28/29):
    no duration, no attempt count, no way to see that polling resumed. A
    ``Conflict`` now logs ``telegram_conflict`` with the watch's vitals —
    ``attempt`` (PTB retries the long-poll on its own, so a competitor that
    HOLDS the token shows a growing count), ``down_s`` (time since the first
    conflict of this stretch) and ``pid`` (which instance is being crowded out,
    so a stray second start is visible in the same line).

    Any other error keeps a full traceback: registering a handler must make
    conflicts *readable*, never make handler failures quieter than they were.
    """
    app = getattr(context, "application", None)
    watch = getattr(app, "bot_data", {}).get("poll_watch") if app else None
    error = getattr(context, "error", None)
    if isinstance(error, Conflict):
        note = watch.note() if watch is not None else {}
        log.warning(
            "telegram_conflict attempt=%s down_s=%.1f since=%s last_ok=%s "
            "pid=%s detail=%s",
            note.get("attempt", 1),
            note.get("down_s", 0.0),
            f"{note['first']:.0f}" if note.get("first") else "-",
            f"{note['last_success']:.0f}" if note.get("last_success") else "-",
            os.getpid(),
            error,
        )
        return
    log.error(
        "telegram_error update=%s error=%s",
        getattr(update, "update_id", None),
        error,
        exc_info=error if isinstance(error, BaseException) else None,
    )


def build_application(
    cfg: Config,
    *,
    request=None,
    get_updates_request: PollWatch | None = None,
) -> Application:
    """Build the bot — and, with it, the two instruments Task 2 owes.

    ``get_updates_request`` is a :class:`~telegram_gateway.pollwatch.PollWatch`
    wrapping the request PTB uses for ``getUpdates`` ONLY, so the conflict
    WARN/recovery pair is measured from the real poll statuses; the seams
    (``request``, ``get_updates_request``) exist so tests can point both at
    ``httpx.MockTransport`` without a socket.
    """
    watch = get_updates_request or default_watch()
    builder = Application.builder().token(cfg.bot_token).get_updates_request(watch)
    if request is not None:
        builder = builder.request(request)
    app = builder.build()
    app.add_error_handler(on_poll_error)
    app.bot_data["poll_watch"] = watch
    app.bot_data["cfg"] = cfg
    app.bot_data["guard"] = Guard(frozenset(cfg.allowed_user_ids), log)
    app.bot_data["client"] = AmuxClient(cfg.amux_url, verify=cfg.amux_verify)
    app.bot_data["state"] = GatewayState(cfg.state_path)

    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(ask_prompt, pattern=r"^gw:prompt$"),
            CommandHandler("prompt", ask_prompt_command),
            # ✏️ rename / ⌨️ custom model: ask, then apply on the next message.
            CallbackQueryHandler(ask_rename, pattern=r"^gw:ctlr:"),
            CallbackQueryHandler(ask_model, pattern=r"^gw:ctlc:"),
            CommandHandler("name", name_command),
        ],
        states={
            PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_prompt),
                MessageHandler(filters.VOICE, receive_prompt_voice),
            ],
            ASK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ask),
            ],
        },
        fallbacks=[
            # ONE cancel for both states: while a state is active the fallbacks
            # are checked in order, so a second CommandHandler("cancel") would
            # shadow the pending-ask cleanup and leave a stale prompt behind.
            CommandHandler("cancel", cancel_ask),
        ],
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("sessions", sessions_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("ctl", ctl_command))
    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("effort", effort_command))
    app.add_handler(conversation)
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^gw:"))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app
