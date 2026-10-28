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

Session control (Milestone 8 — the topic's own panel):

    gw:ctl:<session>        the panel (also its "back")
    gw:ctlm:<session>       model list for the session's provider
    gw:ctlp:<session>:<i>   pick model index i from the list just shown
                            (empty i = server default)
    gw:ctlc:<session>       ask for a custom model id (conversation)
    gw:ctle:<session>       effort levels
    gw:ctlf:<session>:<lvl> pick an effort level (empty = provider default)
    gw:ctlr:<session>       ask for a new name (conversation)
    gw:ctli:<session>       interrupt the current turn (Escape in the pane)

Every payload is kept ≤ 64 bytes (Telegram's cap): ``gw:ctlp:`` (8) +
:data:`MAX_CTL_KEY` (40) + ``:`` + ``xhigh`` (5) = 54.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .manager import PROVIDERS

PREFIX = "gw:"
# Telegram caps callback_data at 64 bytes; leave room for the verb prefix.
MAX_KEY = 52
# The control payloads carry a session AND a second token (model index, effort
# level), so the session is capped tighter than MAX_KEY.
MAX_CTL_KEY = 40

# The server's own list (session_verbs::VALID_EFFORTS), in ascending order.
EFFORTS = ("low", "medium", "high", "xhigh", "max")
# amux applies `--effort` to CLAUDE only — the dashboard's rule
# (app.js `provider === 'claude'`) and the safe one: `opencode --effort high`
# prints usage and exits 1, so writing the flag into CC_FLAGS would kill the
# lane at its next launch instead of changing how it thinks.
EFFORT_PROVIDERS = ("claude",)


def _key(session: str) -> str:
    return session[:MAX_KEY]


def ctl_key(session: str) -> str:
    return session[:MAX_CTL_KEY]


def effort_supported(provider: str | None) -> bool:
    return (provider or "").lower() in EFFORT_PROVIDERS


def flag_value(flags, name: str) -> str | None:
    """``--name value`` or ``--name=value`` from a flag string, else ``None``."""
    if not isinstance(flags, str) or not flags:
        return None
    tokens = flags.split()
    equals = name + "="
    for i, token in enumerate(tokens):
        if token == name:
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if token.startswith(equals):
            return token[len(equals):] or None
    return None


def session_model(row: dict) -> str:
    """The model a row is configured for, where its provider keeps it.

    Same precedence the dashboard uses (app.js `sessionConfiguredModel`):
    the ``--model`` flag, then the live agent's own report, then the row's
    model field. Empty means the provider default — for opencode that is the
    honest answer: the model lives in the worker's own config, not in amux.
    """
    for value in (
        flag_value(row.get("flags"), "--model"),
        row.get("active_model"),
        row.get("model"),
    ):
        if isinstance(value, str) and value:
            return value
    return ""


def session_effort(row: dict) -> str:
    """The ``--effort`` the lane carries (claude-only; ``""`` = default)."""
    if not effort_supported(row.get("provider")):
        return ""
    return flag_value(row.get("flags"), "--effort") or ""


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


# ---------------------------------------------------------------------------
# session control (Milestone 8): the panel a topic opens
# ---------------------------------------------------------------------------


def control_button(session: str) -> InlineKeyboardMarkup:
    """The 🎛 button riding on a topic ack (and any other one-off message)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🎛 Session control", callback_data=f"{PREFIX}ctl:{ctl_key(session)}"
                )
            ]
        ]
    )


def control_keyboard(session: str) -> InlineKeyboardMarkup:
    """Panel root: model, effort, rename, interrupt, stop — one session."""
    key = ctl_key(session)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🤖 Model", callback_data=f"{PREFIX}ctlm:{key}"),
                InlineKeyboardButton("🧠 Effort", callback_data=f"{PREFIX}ctle:{key}"),
            ],
            [
                InlineKeyboardButton("✏️ Rename", callback_data=f"{PREFIX}ctlr:{key}"),
                InlineKeyboardButton(
                    "⚡ Interrupt", callback_data=f"{PREFIX}ctli:{key}"
                ),
            ],
            [
                # Stop reuses the existing confirm (card warning + buttons)
                # rather than growing a second one.
                InlineKeyboardButton("🔴 Stop", callback_data=f"{PREFIX}stop:{key}"),
            ],
            [InlineKeyboardButton("⬅️ Menu", callback_data=f"{PREFIX}menu")],
        ]
    )


def model_keyboard(session: str, models: list[str], current: str) -> InlineKeyboardMarkup:
    """The provider's catalog, current one marked, plus default and custom.

    An EMPTY list is not a reason to hide the page: `GET /api/models` carries
    no opencode rows at all (measured 2026-10-28), so that is exactly when the
    custom-id escape hatch matters — the same "Custom model ID…" the dashboard
    offers when the catalog cannot name a choice.
    """
    key = ctl_key(session)
    rows = []
    for i, model in enumerate(models[:8]):
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅' if model == current else '○'} {model[:44]}",
                    callback_data=f"{PREFIX}ctlp:{key}:{i}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                f"{'✅' if not current else '○'} server default",
                callback_data=f"{PREFIX}ctlp:{key}:",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "⌨️ Custom model id…", callback_data=f"{PREFIX}ctlc:{key}"
            )
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"{PREFIX}ctl:{key}")])
    return InlineKeyboardMarkup(rows)


def effort_keyboard(session: str, current: str) -> InlineKeyboardMarkup:
    key = ctl_key(session)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if level == current else '○'} {level}",
                callback_data=f"{PREFIX}ctlf:{key}:{level}",
            )
        ]
        for level in EFFORTS
    ]
    rows.append(
        [
            InlineKeyboardButton(
                f"{'✅' if not current else '○'} provider default",
                callback_data=f"{PREFIX}ctlf:{key}:",
            )
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"{PREFIX}ctl:{key}")])
    return InlineKeyboardMarkup(rows)
