"""Inline keyboard menu for the gateway.

Callback data vocabulary (all under the ``gw:`` prefix so one handler owns it):

    gw:menu                 back to the main menu
    gw:new                  create + start a session
    gw:peek                 status, then peek a session
    gw:prompt               enter the send-prompt conversation
    gw:stop                 choose a session to stop
    gw:pick:<session>       select a session for this chat
    gw:peek:<session>       peek one session now
    gw:stop:<session>       ask for stop confirmation
    gw:stopok:<session>     stop it
    gw:cancel               dismiss
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .manager import PROVIDERS

PREFIX = "gw:"
# Telegram caps callback_data at 64 bytes; leave room for the verb prefix.
MAX_KEY = 52


def _key(session: str) -> str:
    return session[:MAX_KEY]


def _worker_status(worker: dict) -> str:
    status = worker.get("status")
    if isinstance(status, dict):
        status = status.get("state") or status.get("status") or status.get("label")
    return str(status or ("running" if worker.get("running") else "stopped"))


def format_worker_line(worker: dict) -> str:
    name = worker.get("name") or worker.get("display_name") or worker.get("id") or "?"
    state = _worker_status(worker)
    model = worker.get("model") or ""
    tail = f" · {model}" if model else ""
    return f"{name} — {state}{tail}"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🚀 Start agent / new session", callback_data=f"{PREFIX}new"),
            ],
            [
                InlineKeyboardButton("👁️ Peek output / status", callback_data=f"{PREFIX}peek"),
            ],
            [
                InlineKeyboardButton("✍️ Send prompt", callback_data=f"{PREFIX}prompt"),
            ],
            [
                InlineKeyboardButton("⏹️ Stop / kill session", callback_data=f"{PREFIX}stop"),
            ],
            [
                InlineKeyboardButton("🧠 Manager settings", callback_data=f"{PREFIX}mgr"),
            ],
        ]
    )


def manager_keyboard(
    provider: str,
    model: str | None = None,
    models: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """Pick the agent a fresh message starts: provider first, then its models."""
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if p == provider else '○'} {p}", callback_data=f"{PREFIX}mgrp:{p}"
            )
        ]
        for p in PROVIDERS
    ]
    if models:
        for m in models[:8]:
            rows.append(
                [
                    InlineKeyboardButton(
                        f"   {'✅' if m == model else '○'} {m[:40]}",
                        callback_data=f"{PREFIX}mgrm:{provider}:{m[:40]}",
                    )
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    f"   {'✅' if model is None else '○'} server default",
                    callback_data=f"{PREFIX}mgrm:{provider}:",
                )
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data=f"{PREFIX}menu")])
    return InlineKeyboardMarkup(rows)


def sessions_keyboard(workers: list[dict], action: str) -> InlineKeyboardMarkup:
    """One button per worker for ``pick``, ``peek`` or ``stop``, plus Menu."""
    rows = []
    for worker in workers[:20]:
        name = worker.get("name") or worker.get("display_name") or worker.get("id")
        if not name:
            continue
        label = f"{'🟢' if worker.get('running') else '⚪'} {name}"
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"{PREFIX}{action}:{_key(name)}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Menu", callback_data=f"{PREFIX}menu")])
    return InlineKeyboardMarkup(rows)


def stop_confirm(session: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏹️ Stop it", callback_data=f"{PREFIX}stopok:{_key(session)}"),
                InlineKeyboardButton("↩️ Cancel", callback_data=f"{PREFIX}cancel"),
            ]
        ]
    )


def status_keyboard(session: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👁️ Peek", callback_data=f"{PREFIX}peek:{_key(session)}"),
                InlineKeyboardButton("✍️ Send prompt", callback_data=f"{PREFIX}prompt"),
            ],
            [
                InlineKeyboardButton("🚀 New session", callback_data=f"{PREFIX}new"),
                InlineKeyboardButton("⬅️ Menu", callback_data=f"{PREFIX}menu"),
            ],
        ]
    )
