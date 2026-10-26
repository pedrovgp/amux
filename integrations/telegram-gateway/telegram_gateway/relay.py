"""Turn a raw amux pane capture into the reply a chat should receive.

The pane carries ANSI escapes and TUI chrome (box drawing, the status bar,
spinner lines).  Telegram renders none of it, so it is stripped before the
marker search and the reply extraction — the same shape the built-in Rust
relay uses (``runtime_jobs::telegram_relay``).

The reply boundary is the LAST ``[from Telegram @...]`` marker: everything
after it is the session's answer to the prompt that carried the marker.
"""
from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
MARKER = "[from Telegram @"
TELEGRAM_LIMIT = 4096
# Leave room for a code fence / safety margin.
CHUNK_LIMIT = 3800

_BOX_CHARS = "╭╮╰╯│─┃┏┓┗┛┣┫┳┻╋━┃┆┊╎╏"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_CHROME_SUBSTRINGS = (
    "bypass permissions",
    "shift+tab",
    "esc to interrupt",
    "ctrl+o",
    "ctrl+r",
    "tokens left",
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _is_chrome(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False  # blank lines are keepers; cleaning collapses runs later
    if stripped[0] in _BOX_CHARS:
        return True
    if stripped[0] in _SPINNER:
        return True
    low = stripped.lower()
    return any(needle in low for needle in _CHROME_SUBSTRINGS)


def extract_reply(raw: str) -> str | None:
    """Return the text after the last prompt marker, or ``None``."""
    lines = strip_ansi(raw).splitlines()
    marker_index = None
    for index, line in enumerate(lines):
        if MARKER in line:
            marker_index = index
    if marker_index is None:
        return None
    block = [line.rstrip() for line in lines[marker_index + 1:]]
    block = [line for line in block if not _is_chrome(line)]
    text = "\n".join(block).strip()
    return text or None


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks on line boundaries where possible."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single pathological line
            if current:
                chunks.append("".join(current).rstrip())
                current, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit and current:
            chunks.append("".join(current).rstrip())
            current, size = [], 0
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current).rstrip())
    return [c for c in chunks if c]


def delta_since(last: str | None, text: str) -> str:
    """Return only what is new, so a streaming answer is not re-sent in full.

    A pane answer grows monotonically during a turn, so when the current block
    starts with the last one relayed, the remainder is the new part.  If it does
    not (a new turn moved the marker, or the capture scrolled), the whole block
    is new and is sent as-is.
    """
    if last and text.startswith(last):
        return text[len(last):].lstrip("\n")
    return text
