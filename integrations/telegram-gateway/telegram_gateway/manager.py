"""Manager sessions: lanes briefed to answer and delegate for Telegram.

A manager is an ordinary fleet lane, created with ``start: false`` so its
standing instructions can be saved first — ``start_session_as`` re-sends those
instructions as the lane's first prompt on start, so the role is always the
first thing the agent reads.
"""
from __future__ import annotations

import logging
import secrets
import time

from .amux import AmuxClient
from .config import Config

log = logging.getLogger("telegram_gateway")

# The providers amux can drive. The catalog does not enumerate all of them, so
# this list is the menu's honest default and the server validates the choice.
PROVIDERS = ("opencode", "claude", "codex", "gemini", "ollama")

FALLBACK_INSTRUCTIONS = (
    "You are the owner's manager agent, reached through Telegram. Answer briefly; "
    "delegate work to other amux lanes with `amux board` or `amux send <name> "
    "--stdin`, or POST $AMUX_URL/api/board. Research is yours; building rarely is."
)


def new_manager_name(now: time.struct_time | None = None) -> str:
    t = now or time.localtime()
    return f"mgr-{time.strftime('%m%d-%H%M', t)}-{secrets.token_hex(2)}"


def new_opencode_session_id(now: time.struct_time | None = None) -> str:
    """The OpenCode conversation id a new opencode manager will be pinned to.

    OpenCode's ``--session`` takes an id it has never seen and CREATES that
    conversation (measured 2026-10-27: `opencode run --session
    ses_amuxprobe0001` came back with `sessionID: ses_amuxprobe0001`), so the
    id can be minted before the process exists. That is the whole basis of the
    idle-stop/revive feature: the id rides the lane's ``CC_FLAGS`` on every
    start, so stopping the lane only frees RAM and starting it again resumes
    the SAME conversation. Prefix + timestamp keeps ids readable in logs;
    entropy keeps two lanes created in the same second apart.
    """
    t = now or time.localtime()
    return f"ses_tg{time.strftime('%m%d%H%M', t)}{secrets.token_hex(3)}"


def general_manager_name(uid: int) -> str:
    """The dispatcher lane for ONE allowed Telegram owner.

    The owner's invariant (2026-10-30): *"for each allowed user_id in the
    .env, there should be a general-mgr-<userid> worker"* — one ring member
    per allowed owner, provisioned by the gateway (which owns the allow-list),
    never by hand.
    """
    return f"general-mgr-{uid}"


def load_instructions_file(path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("instructions_missing path=%s error=%s", path, exc)
        return FALLBACK_INSTRUCTIONS
    return text.strip() or FALLBACK_INSTRUCTIONS


def load_instructions(cfg: Config) -> str:
    return load_instructions_file(cfg.manager_instructions_file)


async def create_manager(
    client: AmuxClient,
    cfg: Config,
    *,
    name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    flags: str | None = None,
    instructions: str | None = None,
) -> tuple[str, dict]:
    """Create the lane, brief it, start it, and wait until it can take a task.

    ``instructions`` overrides the brief (the general-manager flow passes its
    own dispatcher prompt); ``None`` keeps the per-conversation manager's.
    """
    name = name or new_manager_name()
    provider = provider or cfg.manager_provider
    model = cfg.manager_model if model is None else model

    await client.create_session(
        name,
        cwd=cfg.manager_cwd,
        provider=provider,
        model=model,
        flags=flags,
        start=False,
    )
    await client.set_instructions(
        name, load_instructions(cfg) if instructions is None else instructions
    )
    await client.start(name)
    row = await client.wait_until_running(name)
    log.info(
        "manager_created name=%s provider=%s model=%s status=%s cwd=%s",
        name,
        provider,
        model or "(server default)",
        row.get("status"),
        cfg.manager_cwd,
    )
    return name, row
