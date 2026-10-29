"""One gateway per state dir — enforced by the kernel, not by hope.

Telegram reports a second consumer as
``Conflict: terminated by other getUpdates request; make sure that only one
bot instance is running`` — two pollers, one token, updates stolen from each
other. The failure mode on this box was a *stray manual start*: a
``bin/telegram-gateway`` run in a pane or a second terminal, invisible to
systemd (which only ever sees its own one process) and, before this guard, free
to long-poll the same token forever.

The lock is an ``flock`` on ``<state_dir>/gateway.pid``, taken BEFORE any
polling starts and released by the kernel when the holder dies — so a crashed
gateway never leaves a stale lock behind, and a pid left in the file is
diagnostic text, never an authority. The holder's pid is written in for the
error message and for the conflict WARN's ``pid=`` sweep.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import IO

__all__ = ["AlreadyRunning", "acquire"]


class AlreadyRunning(RuntimeError):
    """Another gateway process holds the lock for this state dir."""

    def __init__(self, path: Path | str, holder: str | None):
        self.path = Path(path)
        self.holder = holder
        super().__init__(
            f"another telegram-gateway already holds {self.path} "
            f"(pid {holder or 'unknown'})"
        )


def acquire(path: Path | str, *, pid: int | None = None) -> IO[str]:
    """Take the exclusive lock and return a handle that must stay open.

    Closing (or exiting) releases it. ``pid`` is only for tests: the pid that
    holds the lock is what the file records, nothing ever reads it back to
    decide anything.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = open(target, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = _holder(handle)
            raise AlreadyRunning(target, holder) from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid() if pid is None else pid))
        handle.flush()
        return handle
    except BaseException:
        handle.close()
        raise


def _holder(handle) -> str | None:
    """The pid the previous holder wrote, for the failure message only."""
    try:
        handle.seek(0)
        text = handle.read().strip()
    except OSError:
        return None
    return text or None
