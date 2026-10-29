"""Task 2 (owner, 2026-10-28): the getUpdates conflicts, instrumented.

Evidence being fixed: seven `Conflict: terminated by other getUpdates request`
incidents (2026-10-28 02:36:51, 05:48:11, 07:54:10, 08:04:19; 2026-10-29
01:44:00, 03:00:10, 04:02:34) each logged as an unhandled traceback —
*"No error handlers are registered"* — with no duration, no attempt count and
no line saying polling had resumed. The contract under test:

1. **watch** — the `get_updates_request` PTB polls with records 200s and 409s:
   a conflict opens a stretch (attempt counts up), the first 200 after it logs
   `telegram_conflict_recovered … down_s=…` with the EXACT downtime;
2. **handler** — `on_poll_error` turns a Conflict into `telegram_conflict`
   (attempt, down_s, since, pid) instead of a traceback, and keeps the full
   traceback for every other error (registering a handler must not make
   handler failures quieter);
3. **wiring** — a real `bot.get_updates()` flows through the watch (proven
   against a MockTransport: 200 marks success, 409 raises Conflict AND opens
   the stretch);
4. **lock** — a second gateway on the same state dir fails loudly with
   `gateway_already_running` and never reaches `build_application`.
"""
import asyncio
import logging
import os
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import Conflict, TelegramError
from telegram.request import HTTPXRequest

from telegram_gateway import bot as botmod, cli, single
from telegram_gateway.config import Config
from telegram_gateway.pollwatch import PollWatch

from test_topics import make_cfg, run


class Inner:
    """A stand-in for the real request: replays a scripted status list."""

    read_timeout = 10.0

    def __init__(self, *codes):
        self.codes = list(codes)
        self.calls = []

    async def initialize(self):
        self.calls.append("init")

    async def shutdown(self):
        self.calls.append("shutdown")

    async def do_request(self, url, method, request_data=None, **kw):
        code = self.codes.pop(0) if self.codes else 200
        self.calls.append((method, code))
        return code, b"{}"


class Clock:
    """A manual clock: `down_s` must be asserted, not eyeballed."""

    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def ctx(app, error):
    return SimpleNamespace(application=app, error=error)


# ---- the watch: status in, vitals out ------------------------------------


def test_watch_opens_a_stretch_and_closes_it_on_the_first_success(caplog):
    clock = Clock()
    watch = PollWatch(Inner(200, 409, 409, 200), clock=clock)

    run(watch.do_request("https://x/getUpdates", "GET"))
    assert watch.last_success == 1_000.0 and watch.conflict_since is None

    clock.now = 1_010.0
    run(watch.do_request("https://x/getUpdates", "GET"))  # 409 #1
    clock.now = 1_011.5
    run(watch.do_request("https://x/getUpdates", "GET"))  # 409 #2
    assert watch.conflict_since == 1_010.0 and watch.conflict_attempts == 2

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        clock.now = 1_025.0
        run(watch.do_request("https://x/getUpdates", "GET"))  # 200 again

    recovered = [r.getMessage() for r in caplog.records if "telegram_conflict_recovered" in r.getMessage()]
    assert len(recovered) == 1, recovered
    assert "attempts=2" in recovered[0]
    assert "down_s=15.0" in recovered[0], recovered[0]  # 1010 -> 1025, exact
    assert watch.conflict_since is None and watch.conflict_attempts == 0


def test_watch_note_carries_the_vitals_the_warn_needs():
    clock = Clock()
    watch = PollWatch(Inner(200, 409), clock=clock)
    run(watch.do_request("u", "GET"))
    clock.now = 1_050.0
    run(watch.do_request("u", "GET"))  # conflict
    note = watch.note()
    assert note["attempt"] == 1
    assert note["first"] == 1_050.0
    assert note["down_s"] == 0.0
    assert note["last_success"] == 1_000.0  # "when polling last worked"


# ---- the error handler ---------------------------------------------------


def test_a_conflict_is_a_warn_with_vitals_not_a_traceback(caplog):
    clock = Clock()
    watch = PollWatch(Inner(200, 409, 409), clock=clock)
    run(watch.do_request("u", "GET"))
    clock.now = 1_200.0
    run(watch.do_request("u", "GET"))  # conflict #1
    clock.now = 1_206.0
    run(watch.do_request("u", "GET"))  # conflict #2
    app = SimpleNamespace(bot_data={"poll_watch": watch})

    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        run(botmod.on_poll_error(None, ctx(app, Conflict("terminated by other getUpdates request"))))

    warnings = [r for r in caplog.records if "telegram_conflict " in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]
    message = warnings[0].getMessage()
    assert "attempt=2" in message and "down_s=6.0" in message
    assert f"pid={os.getpid()}" in message and "since=1200" in message
    assert warnings[0].exc_info is None  # a Conflict is a condition, not a crash


def test_every_other_error_keeps_its_traceback(caplog):
    app = SimpleNamespace(bot_data={})
    error = TelegramError("boom")
    with caplog.at_level(logging.ERROR, logger="telegram_gateway"):
        run(botmod.on_poll_error(None, ctx(app, error)))
    record = [r for r in caplog.records if "telegram_error" in r.getMessage()][0]
    assert "boom" in record.getMessage()
    assert record.exc_info is not None  # the traceback survives the handler


def test_the_wiring_actually_carries_getupdates(tmp_path):
    """A real `bot.get_updates()` must flow through the watch.

    The whole instrument is worthless if PTB does not route getUpdates through
    the request we hand it — so this drives the real Bot: a 200 marks the
    success, a 409 raises Conflict AND opens the stretch, and the next 200
    logs the recovery.
    """
    cfg = make_cfg(tmp_path, bot_token="123456:TESTTOKEN")

    def main_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/getMe")
        return httpx.Response(200, json={"ok": True, "result": {
            "id": 1, "is_bot": True, "first_name": "gw", "username": "gw_bot",
            "can_join_groups": True, "can_read_all_group_messages": True,
            "supports_inline_queries": False,
        }})

    codes = iter([200, 409, 200])
    seen = []

    def updates_handler(request: httpx.Request) -> httpx.Response:
        code = next(codes)
        seen.append(code)
        if code == 409:
            return httpx.Response(409, json={
                "ok": False, "error_code": 409,
                "description": "Conflict: terminated by other getUpdates request",
            })
        return httpx.Response(200, json={"ok": True, "result": []})

    watch = PollWatch(
        HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(updates_handler)})
    )
    app = botmod.build_application(
        cfg,
        request=HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(main_handler)}),
        get_updates_request=watch,
    )
    try:
        run(app.initialize())
        first = run(app.bot.get_updates())  # 200 -> recorded
        assert list(first) == []
        assert watch.last_success is not None
        with pytest.raises(Conflict):
            run(app.bot.get_updates())  # 409 -> raised AND recorded
        assert watch.conflict_since is not None and watch.conflict_attempts == 1
        run(app.bot.get_updates())  # 200 -> stretch closed (logged)
        assert watch.conflict_since is None
    finally:
        run(app.shutdown())
    assert seen == [200, 409, 200]


def test_build_application_registers_the_watch_and_the_handler(tmp_path):
    app = botmod.build_application(make_cfg(tmp_path))
    watch = app.bot_data.get("poll_watch")
    assert isinstance(watch, PollWatch)
    assert botmod.on_poll_error in app.error_handlers


# ---- the lock: one gateway per state dir ---------------------------------


def test_the_lock_refuses_a_second_holder_and_frees_itself(tmp_path):
    path = tmp_path / "gateway.pid"
    first = single.acquire(path)
    try:
        assert path.read_text().strip() == str(os.getpid())
        with pytest.raises(single.AlreadyRunning) as excinfo:
            single.acquire(path)
        assert excinfo.value.holder == str(os.getpid())
        assert "already holds" in str(excinfo.value)
    finally:
        first.close()
    # …and once the holder is gone the SAME file locks again: a crash never
    # leaves a stale lock behind (the pid inside is text, never an authority).
    second = single.acquire(path)
    second.close()


def test_a_stray_second_start_fails_loudly_and_never_builds(tmp_path, caplog, capsys):
    cfg = make_cfg(tmp_path, bot_token="123456:TESTTOKEN", state_dir=tmp_path)
    held = single.acquire(cfg.lock_path)

    def explode(*args, **kwargs):
        raise AssertionError("build_application must never be reached")

    original = botmod.build_application
    botmod.build_application = explode
    try:
        with caplog.at_level(logging.ERROR, logger="telegram_gateway"):
            code = cli._run(cfg)
    finally:
        botmod.build_application = original
        held.close()

    assert code == 3
    warn = [r.getMessage() for r in caplog.records if "gateway_already_running" in r.getMessage()]
    assert len(warn) == 1 and str(os.getpid()) in warn[0]
    stderr = capsys.readouterr().err
    assert "already running" in stderr and "getUpdates consumer" in stderr


def test_a_free_state_dir_reaches_the_application(tmp_path, monkeypatch):
    """The control: with no holder, `_run` builds and starts (polling stubbed)."""
    cfg = make_cfg(tmp_path, bot_token="123456:TESTTOKEN", state_dir=tmp_path)
    started = {}

    def fake_build(config):
        started["cfg"] = config
        return SimpleNamespace(run_polling=lambda **kw: started.setdefault("polling", kw))

    original = botmod.build_application
    botmod.build_application = fake_build
    try:
        code = cli._run(cfg)
    finally:
        botmod.build_application = original

    assert code == 0
    assert started["cfg"] is cfg and "polling" in started
    # the lock file exists with our pid — and no holder keeps it
    assert (tmp_path / "gateway.pid").exists()
