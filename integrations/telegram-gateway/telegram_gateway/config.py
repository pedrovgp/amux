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
``TELEGRAM_GATEWAY_LOG_LEVEL`` default ``INFO``

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
    log_level: str = "INFO"
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

    state_dir = Path(
        (env.get("TELEGRAM_GATEWAY_STATE_DIR") or "").strip() or DEFAULT_STATE_DIR
    ).expanduser()

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
        log_level=(env.get("TELEGRAM_GATEWAY_LOG_LEVEL") or "INFO").strip().upper(),
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
