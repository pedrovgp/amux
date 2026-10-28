"""OpenCode's own HTTP API: model, effort and interrupt for opencode lanes.

amux can PATCH a lane's config and press Escape in its pane (Milestone 8), but
both are indirect: a model change restarts the lane, and opencode has no
``--effort`` flag to PATCH at all — which is why effort was "claude-only".
What the owner asked for (2026-10-28): *"We have OpenCode server, so it
probably has a more stable API to control which model is selected, list the
available models, control which effort level is selected, and list the effort
levels."* It does. Every ``opencode --standalone`` lane spawns a child
``opencode serve --stdio --port 0`` that speaks a stable HTTP API on a RANDOM
localhost port — this module finds that port and speaks to it.

Discovery, measured live 2026-10-28:

===========================  ==========================================
``ss -tlnp``                ``127.0.0.1:<port>`` owned by pid P
``/proc/P/cmdline``         ``serve --stdio --port 0`` (the child, not the TUI)
``/proc/P/environ``         ``OPENCODE_PASSWORD`` + ``AMUX_SESSION`` (the lane)
``/proc/<ppid>/cmdline``    ``opencode --session ses_… --standalone`` (the pin)
===========================  ==========================================

Auth is HTTP Basic ``opencode:<OPENCODE_PASSWORD>``. The pin — the lane's
``CC_FLAGS --session ses_…`` — is both the API's session id and the stable
identity a rename never touches, so it is the preferred match; the lane name
(``AMUX_SESSION``) is the fallback (a renamed lane keeps its OLD env, so the
name match can go stale while the pin does not).

A headless ``opencode serve --service`` carries no password and no
``AMUX_SESSION``; it is skipped, and the caller falls back to Milestone 8's
behavior.

Every failure raises :class:`OcError` — an ``AmuxError`` **subclass**, so the
panel's existing ``except AmuxError`` sites cover this transport too — and
logs one WARN per (event, target) stretch: a dead serve must be sweepable
(``oc_api_*_failed``) without flooding the log on every tap.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .amux import AmuxError, session_id_from_flags

log = logging.getLogger("telegram_gateway")

OC_USER = "opencode"
DEFAULT_TIMEOUT_S = 10.0
SS_TIMEOUT_S = 5.0
# The pin lives on the TUI (one level above the serve child); walk a little
# further only to survive an extra wrapper, never into an unrelated process.
MAX_ANCESTORS = 3

_LISTEN_RE = re.compile(r"127\.0\.0\.1:(\d+)")
_OC_PID_RE = re.compile(r'users:\(\("opencode",pid=(\d+),')


class OcError(AmuxError):
    """A discovery failure, an auth rejection or a non-2xx serve answer.

    Subclassing ``AmuxError`` is deliberate: every panel call site already
    handles that shape (``status`` + ``detail``), so a second transport adds
    no second error vocabulary.
    """

    def __init__(self, status: int, detail: str):
        super().__init__(status, detail)


# ---------------------------------------------------------------------------
# one WARN per stretch (the sweep signal; never a flood)
# ---------------------------------------------------------------------------

_warned: set[tuple[str, str]] = set()


def _warn(event: str, target: str, exc: AmuxError) -> None:
    """Log ``event`` once per (event, target) until it succeeds again."""
    ident = (event, target)
    if ident in _warned:
        return
    _warned.add(ident)
    log.warning("%s target=%s status=%s detail=%s", event, target, exc.status, exc.detail)


def _ok(event: str, target: str) -> None:
    _warned.discard((event, target))


def reset_warn_dedupe() -> None:
    """Forget every logged stretch (tests, and nothing else)."""
    _warned.clear()


# ---------------------------------------------------------------------------
# discovery: ss + /proc -> (port, password, lane, pin)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OcServer:
    """One reachable ``opencode serve`` process."""

    pid: int
    port: int
    password: str
    lane: str | None = None  # AMUX_SESSION, the lane's name at spawn time
    pin: str | None = None  # --session ses_… on the TUI, stable across renames

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _run_ss() -> str:
    exe = shutil.which("ss") or "/usr/bin/ss"
    try:
        proc = subprocess.run(
            [exe, "-tlnp"], capture_output=True, text=True, timeout=SS_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OcError(0, f"ss failed: {exc}") from exc
    if proc.returncode != 0:
        raise OcError(
            0, f"ss exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
    return proc.stdout


def _ss_listeners(ss_output: str) -> list[tuple[int, int]]:
    """``[(port, pid)]`` for every loopback listener owned by an opencode."""
    out: list[tuple[int, int]] = []
    for line in (ss_output or "").splitlines():
        if not line.startswith("LISTEN"):
            continue
        port = _LISTEN_RE.search(line)
        pid = _OC_PID_RE.search(line)
        if port and pid:
            out.append((int(port.group(1)), int(pid.group(1))))
    return out


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _argv(proc_root: str, pid: int) -> list[str]:
    raw = _read_bytes(f"{proc_root}/{pid}/cmdline")
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _environ(proc_root: str, pid: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in _read_bytes(f"{proc_root}/{pid}/environ").split(b"\0"):
        if b"=" in item:
            key, _, value = item.partition(b"=")
            out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return out


def _ppid(proc_root: str, pid: int) -> int | None:
    text = _read_bytes(f"{proc_root}/{pid}/status").decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("PPid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _pin_of(proc_root: str, pid: int, argv: list[str]) -> str | None:
    """``--session ses_…`` on the process, else on its nearest ancestor."""
    pin = session_id_from_flags(" ".join(argv))
    if pin:
        return pin
    ancestor = _ppid(proc_root, pid)
    for _ in range(MAX_ANCESTORS):
        if not ancestor or ancestor <= 1:
            return None
        try:
            argv = _argv(proc_root, ancestor)
        except OSError:
            return None
        pin = session_id_from_flags(" ".join(argv))
        if pin:
            return pin
        ancestor = _ppid(proc_root, ancestor)
    return None


def discover_servers(
    *, proc_root: str = "/proc", ss_output: str | None = None
) -> list[OcServer]:
    """Every ``opencode serve`` on loopback whose auth we can read.

    ``proc_root``/``ss_output`` exist so tests run against a fixture tree and
    a fixture ``ss`` dump — no process, no socket, no network.
    """
    if ss_output is None:
        ss_output = _run_ss()
    servers: list[OcServer] = []
    for port, pid in _ss_listeners(ss_output):
        try:
            argv = _argv(proc_root, pid)
        except OSError:
            continue
        if "serve" not in argv:
            continue  # an opencode that is not the API server (the TUI itself)
        try:
            env = _environ(proc_root, pid)
        except OSError:
            continue
        password = env.get("OPENCODE_PASSWORD")
        if not password:
            continue  # headless `serve --service`: no auth material to use
        try:
            pin = _pin_of(proc_root, pid, argv)
        except OSError:
            pin = None
        servers.append(
            OcServer(
                pid=pid,
                port=port,
                password=password,
                lane=env.get("AMUX_SESSION") or None,
                pin=pin,
            )
        )
    return servers


def find_server(
    lane: str | None = None,
    pin: str | None = None,
    *,
    discover=discover_servers,
) -> OcServer:
    """The serve process for a lane (``AMUX_SESSION``) or a conversation pin.

    The pin wins when both are given: a rename rewrites the lane name (and a
    running process keeps the env it spawned with) while ``--session`` rides
    ``CC_FLAGS`` untouched, so the pin is the identity that survives.
    """
    if not lane and not pin:
        raise OcError(0, "discovery needs a lane name or a session pin")
    try:
        servers = list(discover())
    except OcError:
        raise
    except Exception as exc:  # a discovery bug must surface as a fallback, not a traceback
        err = OcError(0, f"discovery failed: {exc}")
        _warn("oc_api_discovery_failed", pin or lane or "-", err)
        raise err from exc
    if pin:
        for server in servers:
            if server.pin == pin:
                _ok("oc_api_discovery_failed", pin)
                if lane:
                    _ok("oc_api_discovery_failed", lane)
                return server
    if lane:
        for server in servers:
            if server.lane == lane:
                _ok("oc_api_discovery_failed", lane)
                if pin:
                    _ok("oc_api_discovery_failed", pin)
                return server
    seen = ", ".join(sorted({s.lane or f"pid:{s.pid}" for s in servers})) or "none"
    exc = OcError(
        0,
        f"no opencode serve for lane={lane or '-'} pin={pin or '-'} "
        f"(listening: {seen})",
    )
    _warn("oc_api_discovery_failed", pin or lane or "-", exc)
    raise exc


async def client_for(
    lane: str | None = None,
    pin: str | None = None,
    *,
    discover=None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> "OpencodeClient":
    """A client for the serve process that owns this lane/pin.

    ``discover`` is an injectable zero-arg callable returning ``[OcServer]``
    so tests never run ``ss`` or read ``/proc``.
    """
    run = discover if discover is not None else discover_servers
    try:
        server = await asyncio.to_thread(
            find_server, lane, pin, discover=run
        )
    except OcError:
        raise
    except Exception as exc:  # never let a discovery bug escape as a traceback
        err = OcError(0, f"discovery failed: {exc}")
        _warn("oc_api_discovery_failed", pin or lane or "-", err)
        raise err from exc
    return OpencodeClient(server.base_url, server.password, transport=transport)


# ---------------------------------------------------------------------------
# the client
# ---------------------------------------------------------------------------


def _provider_matches(provider_id: str, provider: str | None) -> bool:
    """Exact match, or the provider FAMILY (``opencode`` ⊃ ``opencode-go``).

    amux calls the lane ``opencode`` while OpenCode's own rows say ``opencode``
    and ``opencode-go`` — and a lane's current model is often the latter, so
    equality alone would hide a session's own model from its own list.
    """
    if not provider:
        return True
    return provider_id == provider or provider_id.startswith(provider + "-")


def _key(value: str) -> str:
    return quote(str(value), safe="")


class OpencodeClient:
    """Async Basic-auth client for one lane's ``opencode serve``."""

    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=httpx.BasicAuth(OC_USER, password),
            timeout=httpx.Timeout(timeout, connect=3.0),
            transport=transport,
        )

    async def __aenter__(self) -> "OpencodeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- plumbing -------------------------------------------------------

    async def _parsed(self, method: str, path: str, *, body: dict | None = None):
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise OcError(
                0, f"opencode serve unreachable at {self.base_url}: {exc}"
            ) from exc
        detail = response.text[:400]
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            detail = str(parsed.get("error") or parsed.get("detail") or detail)
        if response.status_code >= 400:
            raise OcError(response.status_code, detail)
        return parsed

    async def _json(self, method: str, path: str, *, body: dict | None = None) -> dict:
        parsed = await self._parsed(method, path, body=body)
        if not isinstance(parsed, dict):
            raise OcError(0, f"expected a JSON object, got: {str(parsed)[:200]}")
        return parsed

    # ---- verbs ----------------------------------------------------------

    async def list_models(self, provider: str | None = None) -> list[dict]:
        """The catalog filtered to one provider family, normalized.

        Returns ``[{id, providerID, variants: [level, …]}]`` — the SAME rows
        the panel renders and the tap re-reads, because an index that means
        one thing when drawn and another when pressed would apply a model the
        owner never saw.
        """
        event, target = "oc_api_model_list_failed", self.base_url
        try:
            parsed = await self._json("GET", "/api/model")
        except AmuxError as exc:
            _warn(event, target, exc)
            raise
        _ok(event, target)
        rows = parsed.get("data")
        out: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            provider_id = str(row.get("providerID") or "")
            if not _provider_matches(provider_id, provider):
                continue
            model_id = str(row.get("id") or "")
            if not model_id:
                continue
            variants = [
                str(variant.get("id"))
                for variant in row.get("variants") or []
                if isinstance(variant, dict) and variant.get("id")
            ]
            out.append(
                {"id": model_id, "providerID": provider_id, "variants": variants}
            )
        return out

    async def get_session(self, ses_id: str) -> dict:
        """``{model: {id, providerID, variant}, …}`` — model AND effort."""
        event, target = "oc_api_session_read_failed", ses_id
        try:
            parsed = await self._json("GET", f"/api/session/{_key(ses_id)}")
        except AmuxError as exc:
            _warn(event, target, exc)
            raise
        data = parsed.get("data")
        if not isinstance(data, dict):
            err = OcError(0, f"no session data for {ses_id}: {str(parsed)[:200]}")
            _warn(event, target, err)
            raise err
        _ok(event, target)
        return data

    async def set_model(
        self,
        ses_id: str,
        provider_id: str,
        model_id: str,
        variant: str | None = None,
    ) -> None:
        """Switch model AND effort in ONE POST (204 No Content).

        ``variant`` omitted means "no variant setting" — the server's own
        default for that model. This is the call that replaces amux's
        restart-the-lane config PATCH.
        """
        event, target = "oc_api_model_switch_failed", ses_id
        model: dict = {"providerID": provider_id, "id": model_id}
        if variant:
            model["variant"] = variant
        try:
            await self._parsed(
                "POST", f"/api/session/{_key(ses_id)}/model", body={"model": model}
            )
        except AmuxError as exc:
            _warn(event, target, exc)
            raise
        _ok(event, target)

    async def interrupt(self, ses_id: str) -> dict:
        """Stop the CURRENT turn without touching the lane (``interrupted``)."""
        event, target = "oc_api_interrupt_failed", ses_id
        try:
            parsed = await self._json(
                "POST", f"/api/session/{_key(ses_id)}/interrupt"
            )
        except AmuxError as exc:
            _warn(event, target, exc)
            raise
        _ok(event, target)
        return parsed
