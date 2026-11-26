"""Tail amux's server log for ``steering queue STALLED`` — the READ half.

amux writes that WARN where a developer greps (``~/.amux/logs/server-rs.log``);
the owner lives in a Telegram topic, and a stall means their messages are NOT
reaching the worker. This module only READS the file and hands the caller the
session each new stall names; ``telegram_gateway.bot.stall_watch_once`` is the
write half (posting into the session's bound topic).

The measured line (2026-11-07, the stranded-delivery incident):

```
2026-11-07T22:18:15.514793Z  WARN amux_server::api::session_verbs: steering queue
STALLED — messages are not reaching this lane session=server-migration queued=2
oldest_min=10 last_skip=not-at-turn-boundary (within max age)
blocked_now=busy-past-deadline
```

Three rules, each one a defect this file refuses to reintroduce:

- **EOF on first sight.** The offset starts at the file's current size, so a
  gateway restart never replays yesterday's stalls as though they were new —
  N restarts must not become N copies of one incident in the owner's topic.
- **Complete lines only.** A torn last line (the writer is mid-append) stays
  unread until its newline arrives; half a line must never be parsed for a
  session name.
- **A replaced or truncated file starts over.** A different inode at the same
  path (logrotate renamed the old file away) or ``size < offset`` (truncated
  in place) resets the offset to 0 rather than skipping the whole new log.

amux already dedupes its own WARN per (session, condition, hour bucket), so
each line this reader returns IS a distinct stretch — the caller pages once
per line and is honest about what "once per stretch" means.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("telegram_gateway")

STALL_MARKER = "steering queue STALLED"
# The field after the message text. Anchored to the marker's own position so an
# unrelated `session=` earlier in the line can never be mistaken for it.
_SESSION_RE = re.compile(r"\bsession=(\S+)")
# The REASON is the one fact the owner can act on. `last_skip` may itself
# contain spaces (`send-refused: composer`), so stop at the next `key=` field
# instead of grabbing a single token. Both searches start AFTER the marker.
_LAST_SKIP_RE = re.compile(r"\blast_skip=(.*?)(?=\s+\w+=|$)")
_BLOCKED_NOW_RE = re.compile(r"\bblocked_now=(\S+)")


@dataclass(frozen=True)
class Stall:
    """One new stall line: its session, its reason fields, and the line itself.

    The reason fields are optional: amux versions without them still produce a
    valid stall, and a field that cannot be parsed is left empty rather than
    guessed. An empty `last_skip` is honest — the WARN itself prints "unknown
    (no skip recorded since this process started)" when amux has no memory.
    """

    session: str
    line: str
    last_skip: str = ""
    blocked_now: str = ""


class StallWatch:
    """An offset-tracked tail of one log file.

    ``read_new()`` returns only lines appended since the previous call (or
    since construction, which is EOF). A missing/unreadable file is not an
    error: the gateway keeps running and says so once, rather than every tick.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self._missing_logged = False
        stat = self._stat()
        # (device, inode) of the file we are tailing: a logrotate that renames
        # the old file away and creates a new one is a NEW file at this path,
        # whatever its size happens to be.
        self._id = None if stat is None else (stat.st_dev, stat.st_ino)
        # First sight starts at EOF: history is not an event.
        self._pos = 0 if stat is None else stat.st_size

    def _stat(self):
        try:
            return self.path.stat()
        except OSError:
            return None

    def read_new(self) -> list[Stall]:
        stat = self._stat()
        if stat is None:
            if not self._missing_logged:
                self._missing_logged = True
                log.warning(
                    "stall_log_unavailable path=%s hint=amux's server log is not "
                    "readable — steering-stall notices are OFF until it appears",
                    self.path,
                )
            return []
        self._missing_logged = False
        identity = (stat.st_dev, stat.st_ino)
        if identity != self._id:
            # Replaced under us (rotation renamed the old file away): the new
            # file starts at 0, and nothing in it is history.
            self._id = identity
            self._pos = 0
        elif stat.st_size < self._pos:
            self._pos = 0  # truncated in place: the file starts over
        if stat.st_size == self._pos:
            return []
        try:
            with open(self.path, "rb") as handle:
                handle.seek(self._pos)
                chunk = handle.read(stat.st_size - self._pos)
        except OSError as exc:
            log.warning("stall_log_read_failed path=%s error=%s", self.path, exc)
            return []
        end = chunk.rfind(b"\n")
        if end < 0:
            return []  # no complete line yet: wait for the newline
        self._pos += end + 1
        out: list[Stall] = []
        for raw in chunk[:end].split(b"\n"):
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            at = line.find(STALL_MARKER)
            if at < 0:
                continue
            match = _SESSION_RE.search(line, at)
            if not match:
                # A stall with no session= has no topic to reach: counted, not
                # guessed (the sender field would be a fabrication).
                log.warning(
                    "stall_line_unattributed path=%s line=%s",
                    self.path,
                    line[:200],
                )
                continue
            tail = line[at:]
            skip = _LAST_SKIP_RE.search(tail)
            blocked = _BLOCKED_NOW_RE.search(tail)
            out.append(
                Stall(
                    session=match.group(1),
                    line=line,
                    last_skip=(skip.group(1).strip() if skip else ""),
                    blocked_now=(blocked.group(1).strip() if blocked else ""),
                )
            )
        return out
