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
from .guard import Guard, allow_only
from .menu import (
    PREFIX,
    format_worker_line,
    main_menu,
    sessions_keyboard,
    status_keyboard,
    stop_confirm,
)
from .relay import chunk_text, delta_since, extract_reply, strip_ansi
from .state import GatewayState
from .voice import groq_transcriber, transcribe_voice

log = logging.getLogger("telegram_gateway")

PROMPT = 1

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


async def _send_markdown(bot, chat_id: int, text: str, *, code_block: bool = False) -> None:
    """Send text as Markdown, falling back to plain text on a parse error."""
    for chunk in chunk_text(text):
        payload = f"```\n{chunk}\n```" if code_block else chunk
        try:
            await bot.send_message(chat_id, payload, parse_mode=ParseMode.MARKDOWN)
        except BadRequest:
            await bot.send_message(chat_id, chunk)


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
        log.info("session_stopped session=%s", raw)
        await _say(update, f"⏹️ Stopped `{raw}`.", reply_markup=main_menu(), parse_mode=ParseMode.MARKDOWN)


@allow_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if not text:
        return
    session = await _require_session(update, context)
    if not session:
        return
    log.info(
        "prompt_text user=%s session=%s chars=%s",
        update.effective_user.id,
        session,
        len(text),
    )
    try:
        await _deliver_prompt(context, update.effective_chat.id, session, text, _username(update))
    except AmuxError as exc:
        await _report_send_error(update, context, session, exc, "text")
        return
    await update.message.reply_text(f"✅ Sent to `{session}`.", parse_mode=ParseMode.MARKDOWN)


@allow_only
async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    if not cfg.groq_api_key:
        await update.message.reply_text(
            "🎙️ Voice notes need GROQ_API_KEY in ~/.amux/server.env."
        )
        return
    session = await _require_session(update, context)
    if not session:
        return
    log.info(
        "voice_note user=%s session=%s media_type=%s",
        update.effective_user.id,
        session,
        getattr(update.message.voice, "mime_type", "?"),
    )
    try:
        transcript = await transcribe_voice(
            update.message.voice, groq_transcriber(cfg.groq_api_key, cfg.groq_model)
        )
    except Exception as exc:  # transcription failures must be visible, not fatal
        log.warning("voice_transcription_failed user_id=%s error=%s", update.effective_user.id, exc)
        await update.message.reply_text(f"❌ Could not transcribe that voice note: {exc}")
        return
    if not transcript:
        await update.message.reply_text("🗣️ Heard nothing in that voice note.")
        return
    await update.message.reply_text(f"🗣️ Transcribed: {transcript}")
    try:
        await _deliver_prompt(
            context, update.effective_chat.id, session, transcript, _username(update)
        )
    except AmuxError as exc:
        await _report_send_error(update, context, session, exc, "voice")
        return
    await update.message.reply_text(f"✅ Sent to `{session}`.", parse_mode=ParseMode.MARKDOWN)


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
    session = _state(context).selected(update.effective_chat.id)
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
        try:
            await _deliver_prompt(
                context, update.effective_chat.id, session, text, _username(update)
            )
        except AmuxError as exc:
            await _report_send_error(update, context, session, exc, "prompt")
            return ConversationHandler.END
        await update.message.reply_text(f"✅ Sent to `{session}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


@allow_only
async def receive_prompt_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await on_voice(update, context)
    return ConversationHandler.END


@allow_only
async def cancel_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.", reply_markup=main_menu())
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# reply relay
# ---------------------------------------------------------------------------


async def relay_once(app: Application) -> None:
    cfg: Config = app.bot_data["cfg"]
    client: AmuxClient = app.bot_data["client"]
    state: GatewayState = app.bot_data["state"]
    for chat_id, session in state.selections().items():
        try:
            raw = await client.peek(session, lines=300)
        except AmuxError as exc:
            log.debug("relay_peek_failed chat_id=%s session=%s error=%s", chat_id, session, exc)
            continue
        text = extract_reply(raw)
        if not text:
            continue
        last = state.last_relayed(chat_id, session)
        if text == last:
            continue
        new_text = delta_since(last, text)
        if not new_text.strip():
            continue
        try:
            await _send_markdown(app.bot, chat_id, new_text)
        except TelegramError as exc:
            log.warning("relay_send_failed chat_id=%s session=%s error=%s", chat_id, session, exc)
            continue
        state.set_last_relayed(chat_id, session, text)
        log.info(
            "relay_sent chat_id=%s session=%s chars=%s total=%s",
            chat_id,
            session,
            len(new_text),
            len(text),
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
    app.bot_data["relay_task"] = asyncio.create_task(relay_loop(app))
    log.info(
        "gateway_started allowed_users=%d amux_url=%s voice=%s relay_interval_s=%s",
        len(cfg.allowed_user_ids),
        cfg.amux_url,
        "on" if cfg.groq_api_key else "off",
        cfg.relay_interval_s,
    )


async def post_shutdown(app: Application) -> None:
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
