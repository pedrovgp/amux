"""A parked message has a LIFE: tracked, revived with its lane, or returned.

The incident behind this file (2026-11-26 → 28):

* the gateway answered ``send_parked … detail=queued (held in the input box;
  submitted automatically when this turn ends)`` and then forgot the message;
* amux's own composer watcher exits are WARN lines only —
  ``direct_draft_idle_submit_gave_up`` (30 min) and
  ``direct_draft_lane_stopped`` (seven times for `ocusage` on 11-25);
* the idle sweep stopped the lane anyway, because the message had already left
  `steering_queue` (acceptance deletes the row), so the queue probe could not
  see it, and `not-running` is never dead-lettered.

Contracts pinned here:

1. a park is RECORDED durably and a confirmed send CLEARS it;
2. a stopped lane that still owes a queue row is STARTED (the host can resolve
   `not-running`; a permanent wait is a defect);
3. a hold whose text died with its pane is RETURNED to the owner as an error
   and dropped — never silently "queued";
4. past the deadline the hold pages once and stops protecting the lane;
5. an unread queue decides NOTHING (AF-320);
6. `idle_stop` never stops a lane with a live hold.
"""
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.state import GatewayState

from test_topics import CHAT, FakeBot, make_cfg, run

SESSION = "mgr-x"
THREAD = 42
QUEUED_MSG = "queued (steering) — waiting for fresh boot readiness"
DIRECT_MSG = (
    "queued (held in the input box; submitted automatically when this turn ends)"
)


@pytest.fixture(autouse=True)
def _clean_module_dedupes():
    for name in ("_parked_warned", "_parked_hold_warned"):
        getattr(botmod, name).clear()
    botmod._parked_probe_failed.clear()
    yield
    for name in ("_parked_warned", "_parked_hold_warned"):
        getattr(botmod, name).clear()
    botmod._parked_probe_failed.clear()


class HoldAmux:
    """One transport covering every verb the parked path uses."""

    def __init__(self, rows=(), steering=(), measured=True, send=None, status=200):
        self.rows = [dict(row) for row in rows]
        self.steering = [dict(lane) for lane in steering]
        self.measured = measured
        self.status = status
        self.send = send or (lambda request: httpx.Response(200, json={"ok": True, "message": "sent"}))
        self.calls: list[tuple[str, str]] = []
        self.client = AmuxClient(
            "https://localhost:8824", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "/api/sessions":
            return httpx.Response(200, json=self.rows)
        if request.method == "GET" and path == "/api/debug/steering":
            return httpx.Response(
                self.status,
                json={"measured": self.measured, "lanes": self.steering},
            )
        if request.method == "POST" and path.endswith("/send"):
            return self.send(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "message": "ok"})
        return httpx.Response(404, json={"error": "no route"})

    def verbs(self) -> list[str]:
        return [
            path.rsplit("/", 1)[-1]
            for method, path in self.calls
            if method == "POST" and path != "/api/sessions"
        ]

    async def aclose(self):
        await self.client.aclose()


def queued_response(request):
    return httpx.Response(
        200, json={"ok": True, "message": QUEUED_MSG, "submission": "deferred"}
    )


def direct_response(request):
    return httpx.Response(200, json={"ok": True, "message": DIRECT_MSG, "submission": "deferred"})


def setup(tmp_path, fake, *, pinned=True, idle_s=1800.0, aged=10_000.0):
    cfg = make_cfg(
        tmp_path, idle_stop_s=idle_s, parked_sweep_s=60.0, parked_hold_max_s=1800.0
    )
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, THREAD, SESSION)
    state.set_turn_message(CHAT, SESSION, 5, thread=THREAD)
    if pinned:
        state.set_pinned(CHAT, SESSION, "ses_tg0001")
    state.touch_activity(CHAT, SESSION, "owner", at=time.time() - aged)
    app = SimpleNamespace(
        bot_data={"cfg": cfg, "client": fake.client, "state": state, "guard": None},
        bot=FakeBot(),
    )
    return cfg, state, app


# ---- state ---------------------------------------------------------------


def test_parked_hold_roundtrips_through_disk_and_is_bounded(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    hold = state.mark_parked(
        CHAT, SESSION, "please look at the migration", "queued", kind="steering", thread=THREAD
    )
    assert hold["kind"] == "steering" and hold["tries"] == 0
    assert state.parked(CHAT, SESSION)["preview"].startswith("please look")

    again = GatewayState(path)
    assert again.parked(CHAT, SESSION)["thread"] == THREAD
    assert again.parked_holds()[(CHAT, SESSION)]["reason"] == "queued"

    again.note_parked_try(CHAT, SESSION, at=123.0)
    third = GatewayState(path)
    assert third.parked(CHAT, SESSION)["tries"] == 1
    assert third.parked(CHAT, SESSION)["last_try"] == 123.0

    assert third.clear_parked(CHAT, SESSION) is True
    assert GatewayState(path).parked(CHAT, SESSION) is None
    assert GatewayState(path).clear_parked(CHAT, SESSION) is False

    for i in range(150):
        third.mark_parked(CHAT, f"s{i}", "x", "queued")
    raw = json.loads(path.read_text())
    assert len(raw["chats"][str(CHAT)]["parked"]) <= 100


def test_parked_kind_separates_a_composer_hold_from_a_queue_row():
    assert botmod.parked_kind(DIRECT_MSG) == "direct"
    assert botmod.parked_kind(QUEUED_MSG) == "steering"
    assert botmod.parked_kind("composer contains a draft; durable message remains queued") == "steering"
    assert botmod.parked_kind(None) == "steering"


# ---- recording and clearing ---------------------------------------------


def parked_ctx(tmp_path, fake):
    cfg, state, app = setup(tmp_path, fake)
    return SimpleNamespace(application=SimpleNamespace(bot=app.bot, bot_data=app.bot_data)), state


def test_a_park_is_recorded_and_a_confirmed_send_clears_it(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "idle", "running": True}],
        send=queued_response,
    )
    ctx, state = parked_ctx(tmp_path, fake)
    try:
        reason = run(botmod._deliver_prompt(ctx, CHAT, SESSION, "ping", "owner"))
        assert reason == "queued"
        hold = state.parked(CHAT, SESSION)
        assert hold is not None and hold["kind"] == "steering"
        assert "parked_tracked" in caplog.text and "kind=steering" in caplog.text
        # the owner is told WHEN they will hear back, in the same notice
        assert len(ctx.application.bot.sent) == 1
        assert "I will say so here" in ctx.application.bot.sent[0]["text"]

        fake.send = lambda request: httpx.Response(
            200, json={"ok": True, "message": "sent", "submission": "confirmed"}
        )
        assert run(botmod._deliver_prompt(ctx, CHAT, SESSION, "ok", "owner")) is None
        assert state.parked(CHAT, SESSION) is None, "a kept promise closes the hold"
        assert "parked_cleared" in caplog.text
    finally:
        run(fake.aclose())


# ---- the sweep -----------------------------------------------------------


def test_sweep_starts_a_stopped_lane_that_still_owes_a_queue_row(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "stopped", "running": False}],
        steering=[{"session": SESSION, "queued": 1, "oldest_age_s": 900}],
    )
    _cfg, state, app = setup(tmp_path, fake)
    state.mark_parked(CHAT, SESSION, "are you there?", "queued", kind="steering")
    try:
        run(botmod.parked_sweep_once(app))
        assert fake.verbs() == ["start"], "the host can resolve not-running"
        assert "parked_lane_started" in caplog.text
        assert state.parked(CHAT, SESSION) is not None, "the hold stays until delivered"
        assert app.bot.sent == [], "no page: a start is not an error"

        # bounded: a second tick inside the revive window does not echo `start`
        run(botmod.parked_sweep_once(app))
        assert fake.verbs() == ["start"]
    finally:
        run(fake.aclose())


def test_sweep_returns_a_message_that_died_with_its_lane(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "stopped", "running": False}],
        steering=[],  # accepted: the queue row was deleted with the composer paste
    )
    _cfg, state, app = setup(tmp_path, fake)
    state.mark_parked(CHAT, SESSION, "the reply you asked for", "queued", kind="direct")
    try:
        run(botmod.parked_sweep_once(app))
        assert fake.verbs() == [], "nothing durable holds it: no start"
        assert state.parked(CHAT, SESSION) is None, "the hold is dropped"
        assert "parked_failed" in caplog.text and "reason=lane-stopped" in caplog.text
        assert len(app.bot.sent) == 1
        notice = app.bot.sent[0]["text"]
        assert "Not delivered" in notice
        assert "the reply you asked for" in notice
        assert "Send it again" in notice
        assert app.bot.sent[0]["thread"] == THREAD
    finally:
        run(fake.aclose())


def test_sweep_expires_an_unconfirmed_hold_once_and_releases_the_lane(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "idle", "running": True}],
        steering=[],
    )
    cfg, state, app = setup(tmp_path, fake)
    state.mark_parked(
        CHAT, SESSION, "still waiting?", "queued", kind="direct", at=time.time() - 1900
    )
    try:
        run(botmod.parked_sweep_once(app))
        assert "parked_expired" in caplog.text
        assert state.parked(CHAT, SESSION) is None
        assert len(app.bot.sent) == 1 and "Not confirmed" in app.bot.sent[0]["text"]

        run(botmod.parked_sweep_once(app))  # the hold is gone: nothing left to page
        assert len(app.bot.sent) == 1
        assert cfg.parked_hold_max_s == 1800.0
    finally:
        run(fake.aclose())


def test_sweep_decides_nothing_when_the_queue_is_unmeasured(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "stopped", "running": False}],
        steering=[],
        measured=False,
    )
    _cfg, state, app = setup(tmp_path, fake)
    state.mark_parked(CHAT, SESSION, "hello", "queued", kind="direct")
    try:
        run(botmod.parked_sweep_once(app))
        assert fake.verbs() == [], "an unread queue is not an empty one"
        assert state.parked(CHAT, SESSION) is not None
        assert ("parked_sweep_unreadable" in caplog.text or "parked_sweep_unmeasured" in caplog.text)
        assert app.bot.sent == []
    finally:
        run(fake.aclose())


def test_a_transport_failure_also_decides_nothing(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    fake = HoldAmux(steering=[], status=503)
    _cfg, state, app = setup(tmp_path, fake)
    state.mark_parked(CHAT, SESSION, "hello", "queued", kind="direct")
    try:
        run(botmod.parked_sweep_once(app))
        assert state.parked(CHAT, SESSION) is not None
        assert "parked_sweep_unreadable" in caplog.text
    finally:
        run(fake.aclose())


# ---- idle_stop -----------------------------------------------------------


def test_idle_stop_never_freezes_a_lane_with_a_live_parked_hold(tmp_path, caplog):
    """The 2026-11-27 specimen: idle_stop 34 min after a parked message."""
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    fake = HoldAmux(
        rows=[{"name": SESSION, "activity": "x", "status": "idle", "running": True}],
        steering=[],  # the accepted hold is invisible to /api/debug/steering
    )
    _cfg, state, app = setup(tmp_path, fake, aged=10_000)
    state.mark_parked(CHAT, SESSION, "one more thing", "queued", kind="direct")
    try:
        run(botmod.idle_sweep_once(app))
        run(botmod.idle_sweep_once(app))
        assert fake.verbs() == [], "stopping would destroy the only copy"
        assert state.idle_stopped(CHAT, SESSION) is None
        assert caplog.text.count("idle_stop_skipped_parked") == 1

        # once the hold is closed (delivered, returned or expired) the lane is
        # stoppable again — a hold must never pin RAM forever
        state.clear_parked(CHAT, SESSION)
        run(botmod.idle_sweep_once(app))
        assert fake.verbs() == ["stop"]
    finally:
        run(fake.aclose())
