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


def load_instructions(cfg: Config) -> str:
    try:
        text = cfg.manager_instructions_file.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "manager_instructions_missing path=%s error=%s",
            cfg.manager_instructions_file,
            exc,
        )
        return FALLBACK_INSTRUCTIONS
    return text.strip() or FALLBACK_INSTRUCTIONS


async def create_manager(
    client: AmuxClient,
    cfg: Config,
    *,
    name: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[str, dict]:
    """Create the lane, brief it, start it, and wait until it can take a task."""
    name = name or new_manager_name()
    provider = provider or cfg.manager_provider
    model = cfg.manager_model if model is None else model

    await client.create_session(
        name, cwd=cfg.manager_cwd, provider=provider, model=model, start=False
    )
    await client.set_instructions(name, load_instructions(cfg))
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
