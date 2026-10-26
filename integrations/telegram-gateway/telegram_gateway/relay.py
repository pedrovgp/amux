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
# The manager agent stamps its own reply boundary. A pane viewport is ~50 rows,
# so the prompt echo (which carries MARKER) scrolls away after a turn; the
# agent's own first line stays. Measured live 2026-10-26: the marker was gone
# from the pane 30 seconds after the turn, while the answer text remained.
SENTINEL = "—— answer ——"
TELEGRAM_LIMIT = 4096
# Leave room for a code fence / safety margin.
CHUNK_LIMIT = 3800

_BOX_CHARS = "╭╮╰╯│─┃┏┓┗┛┣┫┳┻╋━┃┆┊╎╏╹▁▂▃▄▅▆▇█▀▔"
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Agent-TUI status furniture: counted, not prose. Mirrors the built-in Rust
# relay's `is_chrome_line` (sessions_legacy.rs) — the same pane, the same
# shapes — with opencode's own status lines added from a live capture.
_STATUS_SUBSTRINGS = (
    "ctrl+p commands",     # opencode's bottom bar
    "bypass permissions",  # Claude Code's status bar
    "shift+tab to cycle",
    "esc to interrupt",
    "plan mode",
)
_STATUS_PATTERNS = (
    re.compile(r"^[\d.,]+ tokens$"),
    re.compile(r"^\d+% used$"),
    re.compile(r"^\$[\d.,]+ spent$"),
    re.compile(r"^\+ ?Thought · [\d.]+m?s$"),
    re.compile(r"^\w+ · [\w./-]+ · [\d.]+m?s · [\d.]+ ?tok/s$"),
)


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _is_chrome(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False  # blank lines are keepers; cleaning collapses runs later
    if stripped[0] in _BOX_CHARS or stripped[0] in _SPINNER:
        return True
    low = stripped.lower()
    if any(needle in low for needle in _STATUS_SUBSTRINGS):
        return True
    if any(pattern.match(stripped) for pattern in _STATUS_PATTERNS):
        return True
    # The Rust relay's general rules: a line that is mostly punctuation/box
    # drawing, or one that repeats a single glyph, is furniture, not prose.
    # A short ALPHANUMERIC line is a real answer though ("42", "ok") — that
    # exception is the difference between relaying an answer and relaying
    # nothing, measured live against an opencode pane 2026-10-26.
    if stripped.isalnum():
        return False
    if len(stripped) <= 2:
        return True
    alnum = sum(1 for c in stripped if c.isalnum() or c == " ")
    if len(stripped) > 3 and alnum / len(stripped) < 0.3:
        return True
    if len({c for c in stripped if c != " "}) <= 2:
        return True
    return False


def extract_reply(raw: str) -> str | None:
    """Return the agent's answer, or ``None`` when no boundary is in the pane.

    The boundary is the last prompt marker (``[from Telegram @...]``) or the
    agent's own sentinel line — whichever appears last, so an answer that
    replaced the prompt in the viewport is still found.
    """
    lines = strip_ansi(raw).splitlines()
    boundary = None
    for index, line in enumerate(lines):
        if MARKER in line or line.strip() == SENTINEL:
            boundary = index
    if boundary is None:
        return None
    block = [line.rstrip() for line in lines[boundary + 1:]]
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
