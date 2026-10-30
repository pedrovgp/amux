"""Fresh sessions + the keeper page (Milestone 10, owner 2026-10-30).

The contract under test:

1. **reset = respawn** — 🔄 Fresh session / `/reset` mints a new OpenCode
   session id, spawns a new pinned lane from the same template, migrates
   EVERY gateway map to it (the Milestone-8 rename cascade), moves the topic
   identity to the new pin, re-arms the topic for the fresh conversation's
   title, and retires the old lane — because durable state lives on the
   board/notes, never in the conversation;
2. **the age sweep** compacts a pinned session older than
   `manager_reset_days` ONLY when it is idle-ish (agent idle, no owner
   activity for an hour), the service can confirm the age, and the lane owns
   no cards — otherwise it defers, logged;
3. **the keeper page** — the idle sweep's `reason=not_running` branch (a
   tracked pinned lane found stopped without a stamp) pages the owner ONCE
   per stretch through a direct Bot API send, the measured bypass that works
   while this gateway's own handlers are down.
"""
import json
import time as time_mod
from types import SimpleNamespace

import httpx

from telegram_gateway import bot as botmod
from telegram_gateway.opencode_api import OcError
from telegram_gateway.state import GatewayState

from test_opencode_api import FakeOc
from test_session_control import (
    PIN,
    CtlBot,
    FakeAmux,
    context_for,
    fake_callback,
    fake_command,
    lane,
)
from test_topics import CHAT, make_cfg, run


class ResetAmux(FakeAmux):
    """FakeAmux that actually CREATES the lane it is asked to.

    The reset waits for the new lane to be running (`create_manager` →
    `wait_until_running`), so `find` must see the lane the POST created —
    with the provider and the CC_FLAGS pin the request carried.
    """

    def __init__(self, *args, cards=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.created_bodies = []
        self.cards = list(cards)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            body = json.loads(request.content.decode())
            self.created_bodies.append(body)
            self.rows.append(
                {
                    "name": body["name"],
                    "activity": "none",
                    "status": "idle",
                    "running": True,
                    "provider": body.get("provider") or "opencode",
                    "flags": body.get("flags") or "",
                }
            )
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and request.url.path == "/api/board":
            return httpx.Response(200, json=self.cards)
        return super()._handle(request)


def bound_state(path, *, session="mgr-old", pin=PIN):
    state = GatewayState(path)
    state.set_topic(CHAT, 42, session, created=True)
    state.set_pinned(CHAT, session, pin)
    state.set_turn_message(CHAT, session, 55, thread=42)
    state.select(CHAT, session)
    return state


# ---- 1. reset = respawn ----------------------------------------------------


def test_fresh_session_respawns_repins_and_retires_the_old_lane(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = bound_state(tmp_path / "state.json")
    amux = ResetAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    update = fake_callback("gw:ctln:mgr-old")
    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(botmod.on_callback(update, context_for(cfg, state, amux.client, CtlBot())))
    finally:
        run(amux.aclose())

    # the new lane is a pinned opencode manager, with a NEW session id
    (created,) = amux.created_bodies
    assert created["provider"] == "opencode"
    assert created["flags"].startswith("--session ses_tg")
    new_pin = created["flags"].split()[1]
    assert new_pin != PIN
    new = created["name"]
    assert new != "mgr-old"

    # every map moved, and the topic's identity names the NEW conversation
    assert state.selected(CHAT) == new
    assert state.pinned(CHAT, new) == new_pin
    assert state.topic_session(CHAT, 42) == new
    assert state.topic_pin(CHAT, 42) == new_pin
    assert state.turn_message(CHAT, new) == 55
    # the topic is re-armed: the title sync will follow the FRESH title
    assert state.topic_created(CHAT, 42) is True
    assert state.topic_name(CHAT, 42) == new

    # the old lane is retired, and the log carries the handoff
    assert ("POST", "/api/sessions/mgr-old/stop", {}) in amux.posts()
    assert any(
        "session_compacted" in m and "source=reset_action" in m for m in caplog.messages
    )
    # and the owner lands on the NEW session's panel
    assert "fresh session started" in update.callback_query.edits[0]["text"]
    assert new in update.callback_query.edits[0]["text"]


def test_reset_refuses_anything_that_is_not_a_pinned_opencode_lane(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "claude-lane")
    amux = ResetAmux(
        rows=[lane("claude-lane", provider="claude", flags="", running=False)]
    )
    update = fake_callback("gw:ctln:claude-lane")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, amux.client, CtlBot())))
    finally:
        run(amux.aclose())
    assert amux.created_bodies == []
    assert not any(c[1].endswith("/stop") for c in amux.posts())
    assert "cannot start a fresh session" in update.callback_query.edits[0]["text"]


def test_the_reset_command_targets_the_topic_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = bound_state(tmp_path / "state.json", session="mgr-old")
    amux = ResetAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    update = fake_command("/reset", thread=42)
    try:
        run(botmod.reset_command(update, context_for(cfg, state, amux.client, CtlBot())))
    finally:
        run(amux.aclose())
    assert len(amux.created_bodies) == 1
    assert state.topic_session(CHAT, 42) != "mgr-old"


# ---- 2. the age sweep ------------------------------------------------------


def compaction_app(cfg, state, amux, oc, bot=None):
    return SimpleNamespace(
        bot=bot or CtlBot(),
        bot_data={
            "cfg": cfg,
            "client": amux.client,
            "state": state,
            "oc_api": oc,
        },
    )


def aged_oc(age_days):
    created_ms = (time_mod.time() - age_days * 86400) * 1000
    return FakeOc(
        session={
            "model": {"providerID": "opencode-go", "id": "mimo-v2.6-flash",
                      "variant": "default"},
            "time": {"created": created_ms},
        }
    )


def test_the_age_sweep_compacts_an_old_idle_session(tmp_path, caplog):
    cfg = make_cfg(tmp_path, manager_reset_days=1)
    state = bound_state(tmp_path / "state.json")
    state.touch_activity(CHAT, "mgr-old", "owner", at=time_mod.time() - 7200)
    amux = ResetAmux(
        rows=[lane("mgr-old", flags=f"--session {PIN}", agent_state="idle")]
    )
    bot = CtlBot()
    app = compaction_app(cfg, state, amux, aged_oc(2), bot)
    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(botmod.compaction_once(app))
    finally:
        run(amux.aclose())
    assert len(amux.created_bodies) == 1
    assert any(
        "manager_compacted" in m and "source=age" in m for m in caplog.messages
    )
    # the owner is told, inside the topic the fresh session now owns
    notes = [s for s in bot.sent if "aged out" in s["text"]]
    assert notes and notes[0]["thread"] == 42


def test_the_age_sweep_defers_while_in_use_and_on_cards(tmp_path, caplog):
    cfg = make_cfg(tmp_path, manager_reset_days=1)
    amux_cards = ResetAmux(
        rows=[lane("mgr-cards", flags="--session ses_cards", agent_state="idle")],
        cards=[{"id": "AMUX-99", "status": "doing", "title": "t"}],
    )
    # (a) an OWNER message an hour ago: never compact under a live exchange
    state = bound_state(tmp_path / "a.json")
    state.touch_activity(CHAT, "mgr-old", "owner", at=time_mod.time() - 60)
    app = compaction_app(cfg, state, amux_cards, aged_oc(2))
    try:
        run(botmod.compaction_once(app))
        assert amux_cards.created_bodies == []

        # (b) cards assigned to the lane's name: compaction would strand them
        state2 = bound_state(tmp_path / "b.json", session="mgr-cards", pin="ses_cards")
        state2.touch_activity(CHAT, "mgr-cards", "owner", at=time_mod.time() - 7200)
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(botmod.compaction_once(compaction_app(cfg, state2, amux_cards, aged_oc(2))))
    finally:
        run(amux_cards.aclose())
    assert amux_cards.created_bodies == []
    assert any("manager_compact_deferred" in m and "reason=cards" in m for m in caplog.messages)


def test_a_young_session_and_a_dead_service_are_never_compacted(tmp_path):
    cfg = make_cfg(tmp_path, manager_reset_days=14)
    state = bound_state(tmp_path / "state.json")
    state.touch_activity(CHAT, "mgr-old", "owner", at=time_mod.time() - 7200)
    amux = ResetAmux(rows=[lane("mgr-old", flags=f"--session {PIN}", agent_state="idle")])
    try:
        # younger than the threshold
        run(botmod.compaction_once(compaction_app(cfg, state, amux, aged_oc(2))))
        assert amux.created_bodies == []
        # the service unreachable: never compact blind
        run(
            botmod.compaction_once(
                compaction_app(cfg, state, amux, FakeOc(fail=OcError(0, "gone")))
            )
        )
        assert amux.created_bodies == []
    finally:
        run(amux.aclose())


def test_reset_days_zero_disables_the_tick(tmp_path):
    cfg = make_cfg(tmp_path, manager_reset_days=0)
    state = bound_state(tmp_path / "state.json")
    state.touch_activity(CHAT, "mgr-old", "owner", at=time_mod.time() - 7200)
    amux = ResetAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    try:
        run(botmod.compaction_once(compaction_app(cfg, state, amux, aged_oc(999))))
    finally:
        run(amux.aclose())
    assert amux.created_bodies == []


# ---- 3. the keeper page ----------------------------------------------------


def keeper_app(cfg, state, amux):
    return SimpleNamespace(
        bot_data={"cfg": cfg, "client": amux.client, "state": state}, bot=CtlBot()
    )


def test_a_manager_found_dead_is_paged_once_per_stretch(tmp_path, monkeypatch, caplog):
    cfg = make_cfg(tmp_path)
    state = bound_state(tmp_path / "state.json")
    amux = FakeAmux(rows=[lane("mgr-old", flags=f"--session {PIN}", running=False)])
    paged = []

    async def fake_page(cfg_arg, chat_id, text, **kwargs):
        paged.append((cfg_arg.bot_token, chat_id, text))
        return True

    monkeypatch.setattr(botmod, "page_owner", fake_page)
    app = keeper_app(cfg, state, amux)
    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(botmod.idle_sweep_once(app))  # marks not_running AND pages
            run(botmod.idle_sweep_once(app))  # already stamped: no second page
    finally:
        run(amux.aclose())
    assert len(paged) == 1
    token, chat_id, text = paged[0]
    assert token == cfg.bot_token and chat_id == CHAT
    assert "stopped unexpectedly" in text
    assert state.idle_stopped(CHAT, "mgr-old") is not None  # still revivable
    assert any("dispatcher_down" in m for m in caplog.messages)
    assert any("owner_paged" in m for m in caplog.messages)


def test_a_deliberately_swept_or_stopped_manager_is_never_paged(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    paged = []

    async def fake_page(cfg_arg, chat_id, text, **kwargs):
        paged.append(text)
        return True

    monkeypatch.setattr(botmod, "page_owner", fake_page)

    # (a) the idle sweep's own stamp: found dead BEFORE, never re-paged
    state = bound_state(tmp_path / "a.json")
    state.mark_idle_stopped(CHAT, "mgr-old")
    amux = FakeAmux(rows=[lane("mgr-old", flags=f"--session {PIN}", running=False)])
    try:
        run(botmod.idle_sweep_once(keeper_app(cfg, state, amux)))
    finally:
        run(amux.aclose())

    # (b) a RUNNING lane: nothing to say
    state2 = bound_state(tmp_path / "b.json")
    amux2 = FakeAmux(
        rows=[lane("mgr-old", flags=f"--session {PIN}", running=True, agent_state="idle")]
    )
    state2.touch_activity(CHAT, "mgr-old", "owner", at=time_mod.time() - 100)
    try:
        run(botmod.idle_sweep_once(keeper_app(cfg, state2, amux2)))
    finally:
        run(amux2.aclose())

    assert paged == []


def test_page_owner_posts_a_direct_bot_api_message(tmp_path):
    """The measured bypass (2026-10-27): a raw sendMessage — sending needs no
    getUpdates, so it reaches the owner while this gateway's relay is down."""
    cfg = make_cfg(tmp_path)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    ok = run(
        botmod.page_owner(cfg, CHAT, "ping", transport=httpx.MockTransport(handler))
    )
    assert ok is True
    assert "sendMessage" in seen["url"] and "1:test-token" in seen["url"]
    assert seen["body"] == {"chat_id": CHAT, "text": "ping"}
