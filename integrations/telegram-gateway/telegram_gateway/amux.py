"""Thin async client for the amux HTTP API.

Only the worker verbs this gateway needs.  Every method raises ``AmuxError``
carrying the server's own ``error`` text, so the bot can show a real reason
instead of a status code.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import httpx


class AmuxError(RuntimeError):
    """A non-2xx answer or an unreachable server."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"HTTP {status}: {detail}")


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

    # ---- verbs ----------------------------------------------------------

    async def health(self) -> dict:
        return await self._json("GET", "/health", timeout=10.0)

    async def list_workers(self, limit: int = 200) -> list[dict]:
        data = await self._json("GET", "/api/workers", params={"limit": limit})
        items = data.get("items")
        return items if isinstance(items, list) else []

    async def get_worker(self, key: str) -> dict:
        return await self._json("GET", f"/api/workers/{self._key(key)}")

    async def create_worker(
        self,
        display_name: str,
        *,
        cwd: str | None = None,
        provider: str | None = None,
        backend: str | None = None,
    ) -> dict:
        body: dict = {"display_name": display_name}
        if cwd:
            body["cwd"] = cwd
        if provider:
            body["provider"] = provider
        if backend:
            body["backend"] = backend
        return await self._json("POST", "/api/workers", body=body)

    async def start_worker(self, key: str) -> dict:
        # Starting can wait on host admission and a provider spawn.
        return await self._json("POST", f"/api/workers/{self._key(key)}/start", timeout=120.0)

    async def stop_worker(self, key: str) -> dict:
        return await self._json("POST", f"/api/workers/{self._key(key)}/stop", timeout=60.0)

    async def wait_until_started(
        self, key: str, *, timeout: float = 25.0, interval: float = 1.5
    ) -> dict:
        """Poll until the spawn settles, so the bot reports the real outcome.

        A start answers ``{"state": "starting"}`` before the backend process
        exists; answering the user from that would say "started" for a worker
        that later parks in ``error`` (the herdr spawn failure looked exactly
        like that).  Returns the last row read, even on timeout.
        """
        deadline = time.monotonic() + timeout
        row: dict = {}
        while True:
            try:
                row = await self.get_worker(key)
            except AmuxError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(interval)
                continue
            state = row.get("state")
            slug = state.get("state") if isinstance(state, dict) else row.get("status")
            if slug not in ("starting", "registered", "stopping"):
                return row
            if time.monotonic() >= deadline:
                return row
            await asyncio.sleep(interval)

    async def todo_cards(self, session: str) -> list[dict]:
        """Cards still assigned to a session that will restart it on stop.

        Bootstrap auto-starts any stopped worker that owns a waiting Todo card,
        so a stop that "fails" is usually this.  A board read failure returns
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

    async def send(self, key: str, text: str) -> dict:
        # A send is answered when accepted, not when delivered; the relay's
        # pane read is the delivery signal.
        return await self._json(
            "POST",
            f"/api/workers/{self._key(key)}/send",
            body={"text": text},
            timeout=90.0,
        )

    async def peek(self, key: str, lines: int = 200) -> str:
        data = await self._json(
            "GET", f"/api/workers/{self._key(key)}/peek", params={"lines": lines}
        )
        output = data.get("output")
        return output if isinstance(output, str) else ""
