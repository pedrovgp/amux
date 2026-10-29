"""Poll vitals: when ``getUpdates`` succeeds, fails, and recovers.

PTB hands a ``Conflict`` to the error handler as an exception and says NOTHING
about the polls that work — so "how long were we down?" and "did it resume?"
had no answer. The getUpdates conflicts of 2026-10-28/29 each arrived as an
unhandled traceback ("No error handlers are registered") with no duration and
no recovery line, which is exactly the silence the project rules forbid.

:class:`PollWatch` is the instrument, and it adds **no API call**: it is handed
to PTB as the bot's ``get_updates_request`` (``ApplicationBuilder``), so it
sees the HTTP status of the long-polls PTB already makes — measured in PTB
22.8, ``BaseRequest.do_request`` returns ``(status, body)`` *before*
``_request_wrapper`` raises ``Conflict`` on a 409, so the bookkeeping happens
ahead of the exception.

* ``200`` — a poll succeeded: ``last_success`` moves; if a conflict stretch is
  open it is closed with ``telegram_conflict_recovered … down_s=…``;
* ``409`` — another consumer holds this token: the stretch opens/advances, and
  the error handler (:func:`telegram_gateway.bot.on_poll_error`) logs
  ``telegram_conflict`` with the attempt count and how long we have been down.

``down_s`` is exact: first conflict → the first successful poll after it.
"""
from __future__ import annotations

import logging
import os
import time

from telegram.request import BaseRequest, HTTPXRequest, RequestData

log = logging.getLogger("telegram_gateway")

HTTP_OK = 200
HTTP_CONFLICT = 409


def default_watch() -> "PollWatch":
    """The production watch: a real ``HTTPXRequest`` underneath."""
    return PollWatch(HTTPXRequest())


class PollWatch(BaseRequest):
    """A ``BaseRequest`` delegate that records every getUpdates status.

    All four abstract members delegate to ``inner`` — PTB sees a normal
    request; we keep the vitals.
    """

    def __init__(self, inner: BaseRequest, *, clock=time.time):
        self.inner = inner
        self._clock = clock
        self.last_success: float | None = None
        self.conflict_since: float | None = None
        self.conflict_attempts = 0
        self.last_conflict_at: float | None = None

    # ---- BaseRequest surface (pure delegation) ---------------------------

    @property
    def read_timeout(self) -> float | None:
        return self.inner.read_timeout

    async def initialize(self) -> None:
        await self.inner.initialize()

    async def shutdown(self) -> None:
        await self.inner.shutdown()

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        *args,
        **kwargs,
    ) -> tuple[int, bytes]:
        # Faithful delegation: PTB passes its timeout sentinels through, and
        # re-writing them as `None` would change what the real request means.
        code, payload = await self.inner.do_request(
            url, method, request_data, *args, **kwargs
        )
        self.observe(code)
        return code, payload

    # ---- vitals ----------------------------------------------------------

    def observe(self, code: int) -> None:
        """Record one completed getUpdates status. Called in the poll loop."""
        now = self._clock()
        if code == HTTP_OK:
            self.last_success = now
            if self.conflict_since is not None:
                since = self.conflict_since
                attempts = self.conflict_attempts
                self.conflict_since = None
                self.conflict_attempts = 0
                log.info(
                    "telegram_conflict_recovered first=%.0f attempts=%s "
                    "down_s=%.1f pid=%s",
                    since,
                    attempts,
                    now - since,
                    _pid(),
                )
        elif code == HTTP_CONFLICT:
            self.conflict_attempts += 1
            self.last_conflict_at = now
            if self.conflict_since is None:
                self.conflict_since = now

    def note(self) -> dict:
        """A snapshot for the error handler's WARN line."""
        return {
            "attempt": max(1, self.conflict_attempts),
            "first": self.conflict_since,
            "down_s": (self._clock() - self.conflict_since)
            if self.conflict_since is not None
            else 0.0,
            "last_success": self.last_success,
        }

    def reset(self) -> None:
        """Forget every stretch (tests, and nothing else)."""
        self.last_success = None
        self.conflict_since = None
        self.conflict_attempts = 0
        self.last_conflict_at = None


def _pid() -> int:
    return os.getpid()
