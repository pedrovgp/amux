"""Command-line entrypoint: ``run`` (default) and ``--check``."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from telegram import Update

from .amux import AmuxClient, AmuxError
from .config import Config, ConfigError, load_config
from . import single

log = logging.getLogger("telegram_gateway")


def _mask(secret: str | None) -> str:
    if not secret:
        return "(unset)"
    return f"{secret[:6]}…{secret[-4:]}" if len(secret) > 12 else "•••"


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # PTB is chatty at INFO; keep the gateway's own lines readable.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("groq").setLevel(logging.WARNING)


async def _check(cfg: Config) -> int:
    problems = 0
    print("telegram-gateway --check")
    print(f"  bot token           {_mask(cfg.bot_token)}")
    print(f"  allowed user ids   {sorted(cfg.allowed_user_ids)}")
    print(f"  amux url           {cfg.amux_url} (tls verify={cfg.amux_verify})")
    print(f"  default workdir    {cfg.default_workdir}")
    print(f"  default provider   {cfg.default_provider or '(server default)'}")
    print(f"  groq voice         {'on: ' + cfg.groq_model if cfg.groq_api_key else 'off (GROQ_API_KEY unset)'}")
    print(f"  state file         {cfg.state_path}")
    print(f"  relay interval     {cfg.relay_interval_s:g}s")
    print(
        f"  control endpoint   "
        + (f"127.0.0.1:{cfg.control_port} (POST /topic)" if cfg.control_port else "off")
    )
    print(
        "  idle stop          "
        + (f"{cfg.idle_stop_s:g}s (pinned managers only)" if cfg.idle_stop_s else "off")
    )
    print(
        "  topic title sync   "
        + (f"{cfg.topic_sync_s:g}s" if cfg.topic_sync_s else "off")
    )

    if not os.path.isdir(cfg.default_workdir):
        print(f"  ✗ default workdir does not exist: {cfg.default_workdir}")
        problems += 1

    client = AmuxClient(cfg.amux_url, verify=cfg.amux_verify)
    try:
        health = await client.health()
        print(
            "  ✓ amux health      status=%s commit=%s"
            % (health.get("status"), str(health.get("commit"))[:12])
        )
        # list_workers() was renamed to list_sessions() by f37654bb7 and this
        # caller was never updated: --check died with an AttributeError on
        # every run since. test_cli.py now exercises this exact call path.
        sessions = await client.list_sessions()
        print(f"  ✓ sessions visible  {len(sessions)}")
    except AmuxError as exc:
        print(f"  ✗ amux unreachable: {exc}")
        problems += 1
    finally:
        await client.aclose()

    print("OK" if problems == 0 else f"{problems} problem(s)")
    return 0 if problems == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="telegram-gateway",
        description="Owner-only Telegram bridge for amux workers.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and reachability, then exit",
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print("telegram-gateway: configuration error(s):", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    _configure_logging(cfg.log_level)

    if args.check:
        return asyncio.run(_check(cfg))

    return _run(cfg)


def _run(cfg: Config) -> int:
    """Start long-polling — only if this state dir has no other poller.

    The lock is taken BEFORE the Application exists, so a stray manual start
    (a pane, a second terminal) fails loudly with ``gateway_already_running``
    instead of quietly becoming the second ``getUpdates`` consumer Telegram
    later reports as ``Conflict: terminated by other getUpdates request``.
    systemd's stop/start cannot trip it: the old process releases the flock as
    it exits, before the new one starts.
    """
    try:
        lock = single.acquire(cfg.lock_path)
    except single.AlreadyRunning as exc:
        log.error(
            "gateway_already_running pid=%s path=%s detail=%s",
            exc.holder,
            exc.path,
            exc,
        )
        print(
            f"telegram-gateway: already running (pid {exc.holder or 'unknown'}) — "
            "a second getUpdates consumer would steal updates from the first; "
            f"refusing to start. Lock: {exc.path}",
            file=sys.stderr,
        )
        return 3

    # Imported late so --check and the tests never build an Application.
    from .bot import build_application

    try:
        app = build_application(cfg)
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
