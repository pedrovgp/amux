"""Configuration for the Telegram gateway.

Values are read from the process environment first, then from
``~/.amux/server.env`` (the same file the amux server loads at startup).  The
repo is public: this module never stores credentials, it only consumes them.

Required
--------
``TELEGRAM_GATEWAY_BOT_TOKEN``
    @BotFather token for the gateway's OWN bot.  It must not be the built-in
    connector's ``TELEGRAM_BOT_TOKEN``: Telegram delivers ``getUpdates`` to one
    consumer per token, so sharing it would make the two pollers steal each
    other's updates (HTTP 409).
``ALLOWED_TELEGRAM_USER_IDS``
    Comma-separated numeric Telegram user IDs.  An empty list is a hard error —
    the gateway refuses to start rather than run with no access gate.

Optional
--------
``GROQ_API_KEY``            enables voice-note transcription
``GROQ_TRANSCRIBE_MODEL``   default ``whisper-large-v3-turbo``
``AMUX_URL``                default: ``canonical_url`` from ``~/.amux/endpoint.json``
``AMUX_TLS_VERIFY``         ``1`` to verify TLS (the local server is self-signed)
``AMUX_DEFAULT_WORKDIR``    default workdir for new sessions (default ``~``)
``AMUX_DEFAULT_PROVIDER``   provider for new sessions (omitted -> server default)
``AMUX_DEFAULT_BACKEND``    backend for new sessions (default ``tmux``; the API's
                            own default is ``herdr``, which needs AMUX_HERDR_SESSION)
``TELEGRAM_GATEWAY_STATE_DIR`` default ``~/.amux/telegram-gateway``
``TELEGRAM_GATEWAY_RELAY_INTERVAL_S`` default ``20``
``TELEGRAM_GATEWAY_PEEK_LINES`` default ``2000`` — how far back the reply relay
    peeks a pane when it must fall back to the scrape. A pane viewport is ~50
    rows and opencode's history buffer is 2000 lines, so 300 lines lost long
    answers the moment the ``—— answer ——`` sentinel scrolled out of the
    window (the 2026-10-28 silence); the default now reaches the whole buffer.
``TELEGRAM_GATEWAY_LOG_LEVEL`` default ``INFO``
``AMUX_OPENCODE_SERVICE_URL``   the shared ``opencode serve --service`` base
    URL. Unset (the default) → the relay discovers the listening socket of a
    process running ``opencode serve --service`` each time it needs one, and
    falls back to the pane when there is none. Never a hardcoded port: that
    service is a child of a TUI and can die with it.
``AMUX_OPENCODE_SERVICE_PASSWORD`` overrides ``~/.config/opencode/service.json``
    (``password`` field) for that same service — the amux serve client's own
    contract (``crates/amux-server/src/opencode/serve.rs``).
``TELEGRAM_GATEWAY_CONTROL_PORT`` default ``8834`` — the gateway's own
    localhost control endpoint (``POST /topic``), the one path managers may use
    to open a topic; ``0`` disables it
``TELEGRAM_GATEWAY_IDLE_STOP_S`` default ``1800`` — a pinned manager lane with
    no owner message AND no new agent output for this long is stopped (RAM
    freed; its topic binding is kept, so the next message there starts the same
    lane back and OpenCode resumes the same conversation); ``0`` disables
``TELEGRAM_GATEWAY_TOPIC_SYNC_S`` default ``60`` — how often a gateway-made
    topic's name is checked against its OpenCode session title (the title is
    generated from the conversation a few seconds after the first turn, then
    the topic is renamed ONCE to match); ``0`` disables

Manager mode (a plain message starts a new agent)
-------------------------------------------------
``MANAGER_CWD``                default ``~/Documents/pv/projetos``
``MANAGER_PROVIDER``           default ``opencode`` (switchable from the menu)
``MANAGER_MODEL``              default: whatever the server picks for the provider
``MANAGER_INSTRUCTIONS_FILE``  default: ``manager_prompt.md`` next to this module
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ENDPOINT_FILE = "~/.amux/endpoint.json"
DEFAULT_ENV_FILE = "~/.amux/server.env"
DEFAULT_STATE_DIR = "~/.amux/telegram-gateway"
FALLBACK_AMUX_URL = "https://localhost:8824"
DEFAULT_GROQ_MODEL = "whisper-large-v3-turbo"
DEFAULT_CONTROL_PORT = 8834
# Owner, 2026-10-27: "30 minutes default." 0 disables the idle stop.
DEFAULT_IDLE_STOP_S = 1800.0
# Owner, 2026-10-27: "meaningful names for topics". 0 disables the sync.
DEFAULT_TOPIC_SYNC_S = 60.0
# Relay fallback peek depth (owner, 2026-10-28, "peek substantially further
# back … default well above 300"): tmux's own history limit is 2000 lines, so
# this reaches the entire buffer a long answer can still be hiding in.
DEFAULT_PEEK_LINES = 2000
MIN_PEEK_LINES = 50
# The shared opencode service's password file (amux's serve client contract).
DEFAULT_SERVICE_JSON = "~/.config/opencode/service.json"


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed.

    ``problems`` carries every issue found so ``--check`` can print them all in
    one pass instead of one restart per mistake.
    """

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Config:
    bot_token: str
    allowed_user_ids: frozenset[int]
    amux_url: str = FALLBACK_AMUX_URL
    amux_verify: bool = False
    default_workdir: str = field(default_factory=lambda: os.path.expanduser("~"))
    default_provider: str | None = None
    groq_api_key: str | None = None
    groq_model: str = DEFAULT_GROQ_MODEL
    state_dir: Path = field(default_factory=lambda: Path(DEFAULT_STATE_DIR).expanduser())
    relay_interval_s: float = 20.0
    # Pane depth for the relay's fallback scrape (see module docstring).
    peek_lines: int = DEFAULT_PEEK_LINES
    # The shared `opencode serve --service` the relay reads answers from.
    opencode_service_url: str | None = None
    opencode_service_password: str | None = None
    opencode_service_json: Path = field(
        default_factory=lambda: Path(DEFAULT_SERVICE_JSON).expanduser()
    )
    log_level: str = "INFO"
    # Gateway-owned control endpoint (127.0.0.1 only); 0 disables it.
    control_port: int = DEFAULT_CONTROL_PORT
    # Idle stop for PINNED manager lanes; 0 disables it (see module docstring).
    idle_stop_s: float = DEFAULT_IDLE_STOP_S
    # Topic-title sync (gateway-made topics follow OpenCode's session title);
    # the loop tick AND the on/off switch — 0 disables.
    topic_sync_s: float = DEFAULT_TOPIC_SYNC_S
    # Manager mode: a plain message (not a quote-reply) starts a new agent here.
    manager_cwd: str = field(
        default_factory=lambda: os.path.expanduser("~/Documents/pv/projetos")
    )
    manager_provider: str = "opencode"
    manager_model: str | None = None
    manager_instructions_file: Path = field(
        default_factory=lambda: Path(__file__).parent / "manager_prompt.md"
    )

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        """The single-instance lock (see ``telegram_gateway.single``).

        Beside ``state.json`` so every gateway started with this state dir —
        service or a manual run — contends for the same file.
        """
        return self.state_dir / "gateway.pid"


def parse_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env file.

    Mirrors the server's own loader: blank lines and ``#`` comments are
    ignored, optional single/double quotes around the value are stripped, and
    ``export`` prefixes are tolerated.
    """
    out: dict[str, str] = {}
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def parse_user_ids(raw: str) -> list[int]:
    """Parse ``ALLOWED_TELEGRAM_USER_IDS``.

    Raises ``ValueError`` naming the offending token: a typo silently dropped
    here would look like a working gate while locking the owner out (or, worse,
    a non-numeric entry silently ignored on a permissive parse).
    """
    ids: list[int] = []
    for token in (raw or "").replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError as exc:
            raise ValueError(f"not a numeric Telegram user id: {token!r}") from exc
    return ids


def endpoint_url(path: str | os.PathLike[str] = DEFAULT_ENDPOINT_FILE) -> str | None:
    """Read ``canonical_url`` from the endpoint file amux writes.

    The file carries ``{"canonical_url": "https://localhost:8824", ...}``.  A
    legacy ``url`` key is accepted as a fallback so an older file still works.
    """
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = data.get("canonical_url") or data.get("url")
    return url.strip() if isinstance(url, str) and url.strip() else None


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def load_config(
    environ: dict[str, str] | None = None,
    env_file: str | os.PathLike[str] = DEFAULT_ENV_FILE,
    endpoint_file: str | os.PathLike[str] = DEFAULT_ENDPOINT_FILE,
) -> Config:
    """Build a validated ``Config`` or raise ``ConfigError`` with every problem."""
    env = dict(parse_env_file(env_file))
    process_env = environ if environ is not None else os.environ
    env.update({k: v for k, v in process_env.items() if v is not None})

    problems: list[str] = []

    bot_token = (env.get("TELEGRAM_GATEWAY_BOT_TOKEN") or "").strip()
    if not bot_token:
        problems.append(
            "TELEGRAM_GATEWAY_BOT_TOKEN is not set (create a second bot with "
            "@BotFather; do NOT reuse the built-in connector's token)"
        )
    elif ":" not in bot_token:
        problems.append("TELEGRAM_GATEWAY_BOT_TOKEN does not look like a bot token (missing ':')")
    if bot_token and bot_token == (env.get("TELEGRAM_BOT_TOKEN") or "").strip():
        problems.append(
            "TELEGRAM_GATEWAY_BOT_TOKEN equals TELEGRAM_BOT_TOKEN; the built-in "
            "connector and this gateway would fight over getUpdates"
        )

    try:
        allowed = parse_user_ids(env.get("ALLOWED_TELEGRAM_USER_IDS") or "")
    except ValueError as exc:
        allowed = []
        problems.append(f"ALLOWED_TELEGRAM_USER_IDS: {exc}")
    if not allowed:
        problems.append(
            "ALLOWED_TELEGRAM_USER_IDS is empty — refusing to run without an "
            "access gate (put your numeric id, ask @userinfobot)"
        )

    amux_url = (env.get("AMUX_URL") or "").strip() or endpoint_url(endpoint_file) or FALLBACK_AMUX_URL

    default_workdir = os.path.expanduser(
        (env.get("AMUX_DEFAULT_WORKDIR") or "").strip() or "~"
    )

    try:
        relay_interval = float(env.get("TELEGRAM_GATEWAY_RELAY_INTERVAL_S") or 20)
    except ValueError:
        relay_interval = 20.0
        problems.append("TELEGRAM_GATEWAY_RELAY_INTERVAL_S is not a number; using 20")
    relay_interval = max(5.0, relay_interval)

    try:
        peek_lines = int(
            (env.get("TELEGRAM_GATEWAY_PEEK_LINES") or "").strip() or DEFAULT_PEEK_LINES
        )
    except ValueError:
        peek_lines = DEFAULT_PEEK_LINES
        problems.append(
            f"TELEGRAM_GATEWAY_PEEK_LINES is not a number; using {DEFAULT_PEEK_LINES}"
        )
    if peek_lines < MIN_PEEK_LINES:
        problems.append(
            f"TELEGRAM_GATEWAY_PEEK_LINES must be at least {MIN_PEEK_LINES}; "
            f"using {DEFAULT_PEEK_LINES}"
        )
        peek_lines = DEFAULT_PEEK_LINES

    state_dir = Path(
        (env.get("TELEGRAM_GATEWAY_STATE_DIR") or "").strip() or DEFAULT_STATE_DIR
    ).expanduser()

    try:
        control_port = int(
            (env.get("TELEGRAM_GATEWAY_CONTROL_PORT") or "").strip()
            or DEFAULT_CONTROL_PORT
        )
    except ValueError:
        control_port = DEFAULT_CONTROL_PORT
        problems.append(
            f"TELEGRAM_GATEWAY_CONTROL_PORT is not a number; using {DEFAULT_CONTROL_PORT}"
        )
    if not 0 <= control_port <= 65535:
        problems.append("TELEGRAM_GATEWAY_CONTROL_PORT must be 0 (disabled) or 1-65535")
        control_port = DEFAULT_CONTROL_PORT

    try:
        idle_stop_s = float(
            (env.get("TELEGRAM_GATEWAY_IDLE_STOP_S") or "").strip()
            or DEFAULT_IDLE_STOP_S
        )
    except ValueError:
        idle_stop_s = DEFAULT_IDLE_STOP_S
        problems.append(
            f"TELEGRAM_GATEWAY_IDLE_STOP_S is not a number; using {DEFAULT_IDLE_STOP_S:g}"
        )
    if idle_stop_s < 0:
        problems.append("TELEGRAM_GATEWAY_IDLE_STOP_S must be 0 (disabled) or positive")
        idle_stop_s = DEFAULT_IDLE_STOP_S

    try:
        topic_sync_s = float(
            (env.get("TELEGRAM_GATEWAY_TOPIC_SYNC_S") or "").strip()
            or DEFAULT_TOPIC_SYNC_S
        )
    except ValueError:
        topic_sync_s = DEFAULT_TOPIC_SYNC_S
        problems.append(
            f"TELEGRAM_GATEWAY_TOPIC_SYNC_S is not a number; using {DEFAULT_TOPIC_SYNC_S:g}"
        )
    if topic_sync_s < 0:
        problems.append("TELEGRAM_GATEWAY_TOPIC_SYNC_S must be 0 (disabled) or positive")
        topic_sync_s = DEFAULT_TOPIC_SYNC_S

    if problems:
        raise ConfigError(problems)

    return Config(
        bot_token=bot_token,
        allowed_user_ids=frozenset(allowed),
        amux_url=amux_url.rstrip("/"),
        amux_verify=_truthy(env.get("AMUX_TLS_VERIFY")),
        default_workdir=default_workdir,
        default_provider=(env.get("AMUX_DEFAULT_PROVIDER") or "").strip() or None,
        groq_api_key=(env.get("GROQ_API_KEY") or "").strip() or None,
        groq_model=(env.get("GROQ_TRANSCRIBE_MODEL") or "").strip() or DEFAULT_GROQ_MODEL,
        state_dir=state_dir,
        relay_interval_s=relay_interval,
        peek_lines=peek_lines,
        opencode_service_url=(env.get("AMUX_OPENCODE_SERVICE_URL") or "").strip() or None,
        opencode_service_password=(env.get("AMUX_OPENCODE_SERVICE_PASSWORD") or "").strip()
        or None,
        opencode_service_json=Path(DEFAULT_SERVICE_JSON).expanduser(),
        log_level=(env.get("TELEGRAM_GATEWAY_LOG_LEVEL") or "INFO").strip().upper(),
        control_port=control_port,
        idle_stop_s=idle_stop_s,
        topic_sync_s=topic_sync_s,
        manager_cwd=os.path.expanduser(
            (env.get("MANAGER_CWD") or "").strip() or "~/Documents/pv/projetos"
        ),
        manager_provider=(env.get("MANAGER_PROVIDER") or "").strip() or "opencode",
        manager_model=(env.get("MANAGER_MODEL") or "").strip() or None,
        manager_instructions_file=Path(
            (env.get("MANAGER_INSTRUCTIONS_FILE") or "").strip()
            or (Path(__file__).parent / "manager_prompt.md")
        ).expanduser(),
    )
