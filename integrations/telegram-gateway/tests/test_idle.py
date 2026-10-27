"""Idle stop + same-session revive (the owner's 30-minute call, 2026-10-27).

The contract under test:

1. a NEW opencode manager is created pinned to a session id we mint
   (`--session ses_tg…` in its `flags`), and the pin is recorded in state;
2. the sweep stops a PINNED lane only when BOTH the owner and the agent have
   been silent for `idle_stop_s`, never while it owns a waiting board card,
   and never for an unpinned (legacy / other-provider) lane;
3. an idle-stopped lane keeps its topic binding: the relay stops peeking it,
   and the next prompt STARTS the same lane again before sending, so OpenCode
   resumes the conversation its `CC_FLAGS` pin names.
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

from test_topics import CHAT, FakeBot, FakeMessage, fake_context, fake_update, make_cfg, run

OC_PIN = "ses_tg10270000abcdef"


@pytest.fixture(autouse=True)
def _clean_warn_dedupe():
    """The sweep/relay keep one-WARN-per-stretch sets; they are module state."""
    botmod._cards_warned.clear()
    botmod._no_boundary_warned.clear()
    yield
    botmod._cards_warned.clear()
    botmod._no_boundary_warned.clear()


def app_of(cfg, client, state, bot=None):
    """The shape `idle_sweep_once` / `relay_once` read: bot_data at the top."""
    return SimpleNamespace(
        bot_data={"cfg": cfg, "client": client, "state": state, "guard": None},
        bot=bot,
    )


class FakeAmux:
    """One mock transport covering every verb the gateway uses."""

    def __init__(self, rows=(), cards=(), peek=""):
        self.rows = [dict(row) for row in rows]
        self.cards = [dict(card) for card in cards]
        self.peek = peek
        self.calls: list[tuple[str, str, str]] = []
        self.created: dict | None = None
        self.client = AmuxClient(
            "https://localhost:8824", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw = request.content.decode() if request.content else ""
        self.calls.append((request.method, path, raw))
        if request.method == "GET" and path == "/api/sessions":
            return httpx.Response(200, json=self.rows)
        if request.method == "GET" and path.endswith("/peek"):
            return httpx.Response(200, json={"output": self.peek})
        if request.method == "GET" and path == "/api/board":
            return httpx.Response(200, json=self.cards)
        if request.method == "POST" and path == "/api/sessions":
            body = json.loads(raw)
            self.created = body
            self.rows.append(
                {"name": body["name"], "activity": "none", "status": "idle",
                 "running": False}
            )
            return httpx.Response(201, json={"ok": True, **body})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "message": "ok"})
        return httpx.Response(404, json={"error": "no route"})

    def verbs(self) -> list[str]:
        """``POST /api/sessions/<name>/<verb>`` in order (start/stop/send)."""
        out = []
        for method, path, _ in self.calls:
            if method != "POST" or path == "/api/sessions":
                continue
            out.append(path.rsplit("/", 1)[-1])
        return out

    def posted(self) -> dict:
        assert self.created is not None, "no POST /api/sessions"
        return self.created

    async def aclose(self):
        await self.client.aclose()


def swept(tmp_path, rows, *, idle_s=1800.0, last_idle=10_000.0, cards=(), pin=True):
    """cfg + state + fake client for one sweep, with the session pre-aged."""
    cfg = make_cfg(tmp_path, idle_stop_s=idle_s)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x")
    state.set_turn_message(CHAT, "mgr-x", 5, thread=42)
    if pin:
        state.set_pinned(CHAT, "mgr-x", OC_PIN)
    state.touch_activity(CHAT, "mgr-x", "owner", at=time.time() - last_idle)
    fake = FakeAmux(rows=rows, cards=cards)
    return cfg, state, fake, app_of(cfg, fake.client, state)


def test_pin_activity_and_idle_marker_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_pinned(CHAT, "mgr-a", OC_PIN)
    state.touch_activity(CHAT, "mgr-a", "owner", at=100.0)
    state.touch_activity(CHAT, "mgr-a", "output", at=250.0)
    state.mark_idle_stopped(CHAT, "mgr-a", at=300.0)

    again = GatewayState(path)
    assert again.pinned(CHAT, "mgr-a") == OC_PIN
    assert again.pinned(CHAT, "mgr-b") is None
    assert again.last_activity(CHAT, "mgr-a") == 250.0  # newest stamp wins
    assert again.idle_stopped(CHAT, "mgr-a") == 300.0
    again.clear_idle_stopped(CHAT, "mgr-a")
    assert again.idle_stopped(CHAT, "mgr-a") is None
    assert GatewayState(path).idle_stopped(CHAT, "mgr-a") is None  # persisted


def test_tracked_sessions_is_a_superset_of_the_relay_set(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-anchor", 5)      # relay set
    state.set_topic(CHAT, 42, "mgr-topic")             # topic binding
    state.set_pinned(CHAT, "mgr-pin", OC_PIN)          # pin only
    state.touch_activity(CHAT, "mgr-act", "owner")     # activity only
    state.mark_idle_stopped(CHAT, "mgr-idle")          # marker only

    tracked = state.tracked_sessions()[CHAT]
    assert {"mgr-anchor", "mgr-topic", "mgr-pin", "mgr-act", "mgr-idle"} <= tracked
    assert "mgr-anchor" in state.sessions_to_relay()[CHAT]
    assert "mgr-act" not in state.sessions_to_relay()[CHAT]  # hence the superset


def test_activity_and_pin_maps_are_bounded(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    for i in range(350):
        state.touch_activity(CHAT, f"s{i}", "owner")
    raw = json.loads((tmp_path / "state.json").read_text())
    assert len(raw["chats"][str(CHAT)]["activity"]) <= 300


# ---- creation: the pin rides the lane's flags ----------------------------


def test_opencode_manager_is_pinned_and_carries_the_session_flag(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_manager(CHAT, "opencode", "opencode-go/some-model")
    fake = FakeAmux()
    bot = FakeBot()
    message = FakeMessage(message_id=100, text="hello", thread=None)

    try:
        run(
            botmod.on_text(
                fake_update(message), fake_context(cfg, fake.client, state, bot)
            )
        )
    finally:
        run(fake.aclose())

    body = fake.posted()
    assert body["flags"].startswith("--session ses_")
    assert "model" not in body, "a --model in CC_FLAGS breaks the opencode TUI"
    assert "opencode_model_dropped" in caplog.text
    session = body["name"]
    assert state.pinned(CHAT, session), "the pin is what makes a restart resume"
    assert state.last_activity(CHAT, session) is not None  # owner stamp
    assert "manager_pinned" in caplog.text


def test_other_provider_is_not_pinned_and_keeps_its_model(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_manager(CHAT, "claude", "claude-sonnet-4-5")
    fake = FakeAmux()
    bot = FakeBot()
    message = FakeMessage(message_id=101, text="hello", thread=None)

    try:
        run(
            botmod.on_text(
                fake_update(message), fake_context(cfg, fake.client, state, bot)
            )
        )
    finally:
        run(fake.aclose())

    body = fake.posted()
    assert "flags" not in body
    assert body["model"] == "claude-sonnet-4-5"
    assert state.pinned(CHAT, body["name"]) is None  # never idle-stopped
    assert "opencode_model_dropped" not in caplog.text


# ---- the sweep -----------------------------------------------------------


def test_sweep_stops_an_idle_pinned_lane(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path, [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}]
    )
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert fake.verbs() == ["stop"]
    assert state.idle_stopped(CHAT, "mgr-x") is not None
    assert state.topic_session(CHAT, 42) == "mgr-x", "the topic binding survives"
    assert "idle_stop " in caplog.text and "idle_s=10000" in caplog.text


def test_sweep_leaves_a_fresh_lane_alone(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path,
        [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}],
        last_idle=30.0,  # answered 30s ago, threshold is 1800
    )
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert fake.verbs() == []
    assert state.idle_stopped(CHAT, "mgr-x") is None
    assert "idle_stop " not in caplog.text


def test_sweep_never_touches_an_unpinned_lane(tmp_path, caplog):
    """Owner decision 2026-10-27: managers that already exist stay as today."""
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path,
        [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}],
        pin=False,
    )
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert not any(
        method == "POST" for method, _, _ in fake.calls
    ), "an unpinned lane must cost nothing at all"
    assert fake.calls and fake.calls[0][1] == "/api/sessions"  # only the list read
    assert state.idle_stopped(CHAT, "mgr-x") is None
    assert "idle_stop" not in caplog.text


def test_sweep_skips_a_lane_that_owns_a_waiting_card(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path,
        [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}],
        cards=[{"id": "c1", "title": "fix the migration", "status": "todo"}],
    )
    try:
        run(botmod.idle_sweep_once(app))
        run(botmod.idle_sweep_once(app))  # a second tick must not re-WARN
        assert fake.verbs() == []  # amux would restart it anyway (option B: no)
        assert state.idle_stopped(CHAT, "mgr-x") is None
        assert caplog.text.count("idle_stop_skipped_cards") == 1
        assert "c1" in caplog.text
        # and the lane becomes stoppable once the card is closed
        fake.cards.clear()
        run(botmod.idle_sweep_once(app))
        assert fake.verbs() == ["stop"]
    finally:
        run(fake.aclose())


def test_sweep_marks_a_lane_that_is_already_stopped(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path,
        [{"name": "mgr-x", "activity": "x", "status": "stopped", "running": False}],
    )
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert fake.verbs() == []  # nothing to stop
    assert state.idle_stopped(CHAT, "mgr-x") is not None  # but relay must skip it
    assert "reason=not_running" in caplog.text


def test_sweep_clears_the_marker_when_the_lane_is_running_again(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg, state, fake, app = swept(
        tmp_path, [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}]
    )
    state.mark_idle_stopped(CHAT, "mgr-x")
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert state.idle_stopped(CHAT, "mgr-x") is None
    assert "idle_stop_undone" in caplog.text
    assert fake.verbs() == []  # it did not stop a live lane again


def test_sweep_is_off_when_the_knob_is_zero(tmp_path):
    cfg, state, fake, app = swept(
        tmp_path,
        [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}],
        idle_s=0.0,
    )
    try:
        run(botmod.idle_sweep_once(app))
    finally:
        run(fake.aclose())

    assert fake.calls == []
    assert state.idle_stopped(CHAT, "mgr-x") is None


# ---- relay: a stopped lane costs no peek ---------------------------------


def test_relay_skips_an_idle_stopped_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 5)
    state.mark_idle_stopped(CHAT, "mgr-x")
    pane = "—— answer ——\nthe answer\n"
    fake = FakeAmux(rows=[{"name": "mgr-x", "activity": "x", "status": "idle"}], peek=pane)

    try:
        run(botmod.relay_once(app_of(cfg, fake.client, state, FakeBot())))
    finally:
        run(fake.aclose())

    assert not any(path.endswith("/peek") for _, path, _ in fake.calls)


def test_relay_still_peeks_a_live_session(tmp_path):
    """The control for the test above: without the marker, the peek happens."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 5)
    pane = "—— answer ——\nthe answer\n"
    fake = FakeAmux(rows=[{"name": "mgr-x", "activity": "x", "status": "idle"}], peek=pane)

    try:
        run(botmod.relay_once(app_of(cfg, fake.client, state, FakeBot())))
    finally:
        run(fake.aclose())

    assert any(path.endswith("/peek") for _, path, _ in fake.calls)


def test_relay_stamps_output_as_activity(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 5)
    before = time.time()
    pane = "—— answer ——\nthe answer\n"
    fake = FakeAmux(rows=[{"name": "mgr-x", "activity": "x", "status": "idle"}], peek=pane)

    try:
        run(botmod.relay_once(app_of(cfg, fake.client, state, FakeBot())))
    finally:
        run(fake.aclose())

    assert state.last_activity(CHAT, "mgr-x") >= before  # output counts


# ---- revive: the next prompt starts the same lane ------------------------


def test_deliver_starts_an_idle_stopped_lane_then_sends(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.mark_idle_stopped(CHAT, "mgr-x")
    fake = FakeAmux(rows=[{"name": "mgr-x", "activity": "x", "status": "idle"}])
    ctx = fake_context(cfg, fake.client, state, FakeBot())

    run(botmod._deliver_prompt(ctx, CHAT, "mgr-x", "continue please", "owner"))

    verbs = fake.verbs()
    assert verbs[:2] == ["start", "send"], f"revive must precede the prompt: {verbs}"
    assert state.idle_stopped(CHAT, "mgr-x") is None, "marker cleared on revive"
    assert state.last_activity(CHAT, "mgr-x") is not None
    assert "revive chat_id=" in caplog.text
    sent = [body for method, path, body in fake.calls if path.endswith("/send")]
    assert sent and "[from Telegram @owner] continue please" in sent[0]
    run(fake.aclose())


def test_deliver_sends_without_starting_when_not_stopped(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[{"name": "mgr-x", "activity": "x", "status": "idle"}])
    ctx = fake_context(cfg, fake.client, state, FakeBot())

    run(botmod._deliver_prompt(ctx, CHAT, "mgr-x", "hi", "owner"))

    assert "start" not in fake.verbs()
    assert fake.verbs() == ["send"]
    run(fake.aclose())


def test_stop_and_start_roundtrip_keeps_the_topic_binding(tmp_path, caplog):
    """The feature in one test: stop frees the lane, revive keeps the session."""
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path, idle_stop_s=1800.0)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x")
    state.set_pinned(CHAT, "mgr-x", OC_PIN)
    state.set_turn_message(CHAT, "mgr-x", 5, thread=42)
    state.touch_activity(CHAT, "mgr-x", "owner", at=time.time() - 10_000)
    rows = [{"name": "mgr-x", "activity": "x", "status": "idle", "running": True}]
    fake = FakeAmux(rows=rows)
    app = app_of(cfg, fake.client, state)

    run(botmod.idle_sweep_once(app))            # 30 min quiet -> stop
    assert fake.verbs() == ["stop"]
    assert state.idle_stopped(CHAT, "mgr-x") is not None
    assert state.topic_session(CHAT, 42) == "mgr-x"
    assert state.pinned(CHAT, "mgr-x") == OC_PIN

    run(botmod.relay_once(app_of(cfg, fake.client, state, FakeBot())))
    assert not any(p.endswith("/peek") for _, p, _ in fake.calls)  # no dead peek

    rows[0].update(status="idle", running=False)  # the lane really is stopped
    ctx = fake_context(cfg, fake.client, state, FakeBot())
    run(botmod._deliver_prompt(ctx, CHAT, "mgr-x", "back again", "owner"))
    assert fake.verbs()[-2:] == ["start", "send"]  # after the sweep's stop
    assert state.idle_stopped(CHAT, "mgr-x") is None
    assert state.topic_session(CHAT, 42) == "mgr-x"  # same topic, same session
    run(fake.aclose())
