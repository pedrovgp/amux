"""Access gate for the Telegram gateway.

The gateway is an owner-control surface: every message, button press and voice
note must come from an explicitly allowed Telegram user ID.  A denial is never
silent — the caller gets an explicit refusal and the attempt lands in the log
with the user, chat and handler that tried.
"""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass

from telegram import Update
from telegram.ext import ContextTypes

DENIED_TEXT = "⛔ Access denied. This bot is private."


@dataclass(frozen=True)
class Guard:
    allowed_user_ids: frozenset[int]
    logger: logging.Logger

    def is_allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.allowed_user_ids

    def record_denial(
        self,
        *,
        user_id: int | None,
        username: str | None,
        chat_id: int | None,
        action: str,
    ) -> None:
        # One WARN line, greppable: security_denied is the sweep's signal.
        self.logger.warning(
            "security_denied user_id=%s username=%s chat_id=%s action=%s",
            user_id,
            username or "-",
            chat_id,
            action,
        )


async def _deny(update: Update) -> None:
    query = update.callback_query
    if query is not None:
        await query.answer(DENIED_TEXT, show_alert=True)
        return
    message = update.effective_message
    if message is not None:
        await message.reply_text(DENIED_TEXT)


def allow_only(handler):
    """Decorator: refuse any update whose sender is not on the allow-list.

    Applied to every handler (messages, callbacks, voice).  Returning ``None``
    on denial keeps ConversationHandler from advancing state for an intruder.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        guard: Guard = context.application.bot_data["guard"]
        user = update.effective_user
        chat = update.effective_chat
        user_id = user.id if user else None
        if guard.is_allowed(user_id):
            return await handler(update, context)
        guard.record_denial(
            user_id=user_id,
            username=user.username if user else None,
            chat_id=chat.id if chat else None,
            action=handler.__name__,
        )
        await _deny(update)
        return None

    return wrapper
