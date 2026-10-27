"""OpenCode's session titles — the name a topic should show.

A gateway-made topic is born as the lane name (`mgr-1027-1536-8b4e`) while
OpenCode titles the same conversation from its content a few seconds after the
first turn ("Telegram manager agent setup"). The owner's call (2026-10-27):
"meaningful names for topics — it should match the session name in opencode".

The direction is fixed by what the tools allow. OpenCode's TUI exposes no title
flag (`opencode --help`: only `--standalone/--server/--auto/--continue/
--session/--prompt`) and its private `--stdio --port 0` server has no HTTP port
for `opencode api` to write one, while the Bot API can RENAME a topic but has
no call to READ one back. So: read the title OpenCode chose, rename the topic,
and write the new name into `state.topic_names` — the only record of what a
topic is called, and the rename's dedupe in one field.

Reading is `opencode session list` in `MANAGER_CWD`: every manager lane shares
that cwd, so one project holds them all and the session id pinned in state
selects the right row.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("telegram_gateway")

# Telegram's limit for a forum topic's title.
MAX_TOPIC_TITLE = 128
# OpenCode's own stub, seen before its title job lands; renaming to it would
# replace a meaningless name with another one.
PLACEHOLDER_TITLES = frozenset({"untitled session"})
READ_TIMEOUT_S = 20.0


def parse_session_list(text: str) -> dict[str, str]:
    """Session id -> title from `opencode session list` output.

    Rows are ``id \\t title \\t date``, newest first; the first row for an id
    wins. Anything that is not a session row is ignored, so a warning printed
    to stdout cannot be mistaken for a title.
    """
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sid, title = parts[0].strip(), parts[1].strip()
        if not sid.startswith("ses_"):
            continue
        if sid not in out:
            out[sid] = title
    return out


def clean_title(title: str | None) -> str | None:
    """A title fit to become a topic name, or ``None`` to keep waiting.

    Single line (Telegram renders a topic name inline), within the 128-char
    limit, and never a placeholder: the real title arrives seconds after the
    first turn, and a topic is renamed ONCE, so the first name must be worth it.
    """
    if not title:
        return None
    flat = " ".join(str(title).split())
    if not flat:
        return None
    if flat.lower() in PLACEHOLDER_TITLES:
        return None
    if flat.isdigit():
        return None
    return flat[:MAX_TOPIC_TITLE].rstrip() or None


async def _run_session_list(cwd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "opencode",
        "--standalone",
        "session",
        "list",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"opencode session list exited {proc.returncode}: "
            f"{err.decode('utf-8', 'replace').strip()[:200]}"
        )
    return out.decode("utf-8", "replace")


async def read_titles(cwd: str, runner=None) -> dict[str, str]:
    """Every OpenCode session title reachable from ``cwd``, by session id.

    ``runner`` is injectable so tests never spawn a process. A failure — the
    binary missing, the project unreadable, a timeout — is an empty dict: a
    title read is a nicety and must never take a relay or sweep tick with it.
    """
    run = runner if runner is not None else _run_session_list
    try:
        text = await asyncio.wait_for(run(cwd), timeout=READ_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - the tick must survive a bad read
        log.debug("opencode_titles_unavailable cwd=%s error=%s", cwd, exc)
        return {}
    return parse_session_list(text if isinstance(text, str) else "")
