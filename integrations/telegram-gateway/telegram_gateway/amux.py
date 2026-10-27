"""Thin async client for the amux HTTP API.

amux has two substrates behind the same names, and they answer different verbs:

- **lanes** — env-file + tmux fleet workers (``POST /api/sessions`` writes the
  env file); verbs live under ``/api/sessions/<name>``.
- **worker rows** — the newer ``workers`` table (``POST /api/workers``); verbs
  live under ``/api/workers/<name>``. They have no lane env file, so a send to
  one is refused by the server ("session not found").

``GET /api/sessions`` returns BOTH, merged; a lane carries the fleet fields
(``activity``, ``agent_state``, ``backend``) that a worker row does not. Every
verb resolves the name to the right base before calling, so the bot can list
whatever amux lists and still address it correctly.

Every method raises ``AmuxError`` carrying the server's own ``error`` text.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx

LANE_MARKER = "activity"  # present on fleet lanes, absent on worker rows


class AmuxError(RuntimeError):
    """A non-2xx answer or an unreachable server."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


def is_lane(row: dict) -> bool:
    return LANE_MARKER in row


class AmuxClient:
    def __init__(
        self,
        base_url: str,
        *,
        verify: bool = False,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            verify=verify,
            timeout=httpx.Timeout(timeout, connect=5.0),
            transport=transport,
        )

    async def __aenter__(self) -> "AmuxClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- plumbing -------------------------------------------------------

    async def _parsed(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        timeout: float | None = None,
    ):
        try:
            response = await self._client.request(
                method, path, params=params, json=body, timeout=timeout
            )
        except httpx.HTTPError as exc:
            raise AmuxError(0, f"amux unreachable at {self.base_url}: {exc}") from exc

        detail = response.text[:400]
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            detail = str(parsed.get("error") or parsed.get("detail") or detail)
        if response.status_code >= 400:
            raise AmuxError(response.status_code, detail)
        return parsed

    async def _json(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        parsed = await self._parsed(
            method, path, params=params, body=body, timeout=timeout
        )
        if not isinstance(parsed, dict):
            raise AmuxError(0, f"expected a JSON object, got: {str(parsed)[:200]}")
        return parsed

    @staticmethod
    def _key(key: str) -> str:
        return quote(str(key), safe="")

    # ---- discovery ------------------------------------------------------

    async def health(self) -> dict:
        return await self._json("GET", "/health", timeout=10.0)

    async def list_sessions(self) -> list[dict]:
        rows = await self._parsed("GET", "/api/sessions")
        return rows if isinstance(rows, list) else []

    async def find(self, name: str) -> dict:
        for row in await self.list_sessions():
            if isinstance(row, dict) and row.get("name") == name:
                return row
        raise AmuxError(404, f"session '{name}' not found")

    def _base(self, row: dict) -> str:
        return "/api/sessions" if is_lane(row) else "/api/workers"

    # ---- lifecycle ------------------------------------------------------

    async def create_session(
        self,
        name: str,
        *,
        cwd: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        flags: str | None = None,
        start: bool = True,
    ) -> dict:
        """Create a fleet lane (env file, autostart unless told otherwise).

        ``POST /api/workers`` would only write a table row with no lane env
        file, and the server refuses sends to those; the SPA's own create uses
        ``POST /api/sessions`` for exactly this reason.

        ``flags`` is stored verbatim as the lane's ``CC_FLAGS`` and rides on
        EVERY later start of that lane (the launch reads the env file), which
        is how a lane gets pinned to one OpenCode conversation: amux honours
        explicit flags over ``model`` (AMUX-3114), so a caller that passes
        ``flags`` must put the model in them itself when it wants one.
        """
        body: dict = {"name": name, "start": start}
        if cwd:
            body["dir"] = cwd
        if provider:
            body["provider"] = provider
        if model:
            body["model"] = model
        if flags:
            body["flags"] = flags
        return await self._json("POST", "/api/sessions", body=body, timeout=120.0)

    async def set_instructions(self, name: str, instructions: str) -> dict:
        """Save standing instructions for a lane.

        Deliberately WITHOUT ``apply``: ``start_session_as`` re-sends the saved
        instructions as the lane's first prompt when it starts, so briefing a
        stopped lane and then starting it is one clean turn.
        """
        return await self._json(
            "POST",
            f"/api/sessions/{self._key(name)}/instructions",
            body={"instructions": instructions},
        )

    async def models(self, provider: str | None = None) -> list[dict]:
        """Model catalog (``GET /api/models``), optionally one provider's."""
        data = await self._json("GET", "/api/models")
        rows = data.get("models")
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if provider and row.get("provider") != provider:
                continue
            out.append({"provider": row.get("provider"), "id": row.get("id")})
        return out

    async def start(self, name: str) -> dict:
        row = await self.find(name)
        return await self._json(
            "POST", f"{self._base(row)}/{self._key(name)}/start", timeout=120.0
        )

    async def stop(self, name: str) -> dict:
        row = await self.find(name)
        return await self._json(
            "POST", f"{self._base(row)}/{self._key(name)}/stop", timeout=60.0
        )

    async def delete(self, name: str) -> dict:
        row = await self.find(name)
        if is_lane(row):
            return await self._json(
                "POST", f"/api/sessions/{self._key(name)}/delete", timeout=60.0
            )
        return await self._json("DELETE", f"/api/workers/{self._key(name)}", timeout=60.0)

    async def wait_until_started(
        self, name: str, *, timeout: float = 25.0, interval: float = 1.5
    ) -> dict:
        """Poll until the spawn settles, so the bot reports the real outcome.

        Creation/start answers before the backend process answers, which is why
        the first peek can 409 ("live but backend not found"). Returns the last
        row read, even on timeout.
        """
        deadline = time.monotonic() + timeout
        row: dict = {}
        while True:
            try:
                row = await self.find(name)
            except AmuxError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(interval)
                continue
            status = str(row.get("status") or "")
            if status and status not in ("starting", "stopping"):
                return row
            if time.monotonic() >= deadline:
                return row
            await asyncio.sleep(interval)

    async def wait_until_running(
        self, name: str, *, timeout: float = 60.0, interval: float = 2.0
    ) -> dict:
        """Poll until the lane can take a task (or has genuinely failed).

        A lane created with ``start: false`` reads ``stopped``, and a just-started
        one reads ``starting`` — neither may be mistaken for "ready", which is
        why this waits for a live status rather than merely "not starting".
        """
        deadline = time.monotonic() + timeout
        row: dict = {}
        while True:
            try:
                row = await self.find(name)
            except AmuxError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(interval)
                continue
            status = str(row.get("status") or "").lower()
            if status in ("idle", "working", "active", "blocked", "busy", "error"):
                return row
            if time.monotonic() >= deadline:
                return row
            await asyncio.sleep(interval)

    # ---- interaction ----------------------------------------------------

    async def send(self, name: str, text: str) -> dict:
        # A send is answered when accepted, not when delivered; the relay's
        # pane read is the delivery signal.
        row = await self.find(name)
        return await self._json(
            "POST",
            f"{self._base(row)}/{self._key(name)}/send",
            body={"text": text},
            timeout=90.0,
        )

    async def peek(self, name: str, lines: int = 200) -> str:
        row = await self.find(name)
        data = await self._json(
            "GET",
            f"{self._base(row)}/{self._key(name)}/peek",
            params={"lines": lines},
        )
        output = data.get("output")
        return output if isinstance(output, str) else ""

    async def todo_cards(self, session: str) -> list[dict]:
        """Cards still assigned to a session that will restart it on stop.

        Bootstrap auto-starts any stopped worker that owns a waiting Todo card,
        so a stop that "fails" is usually this. A board read failure returns
        ``[]``: this decorates a stop message, it must not turn a real stop
        into an error.
        """
        try:
            rows = await self._parsed(
                "GET", "/api/board", params={"session": session, "done_limit": 0}
            )
        except AmuxError:
            return []
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            status = str(row.get("status") or "").lower()
            if status in ("todo", "doing", "backlog", "blocked"):
                out.append(
                    {
                        "id": row.get("id"),
                        "title": row.get("title"),
                        "status": status,
                    }
                )
        return out
