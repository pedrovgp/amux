"""Manager mode: the thread rule, the manager lane sequence, and reply-to."""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.config import Config
from telegram_gateway.manager import create_manager, new_manager_name
from telegram_gateway.state import GatewayState


def run(coro):
    return asyncio.run(coro)


def make_client(handler):
    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


# ---- state: the message -> session map ---------------------------------


def test_message_map_survives_a_restart(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.record_message(9, 101, "mgr-1")
    state.record_message(9, 102, "mgr-1")
    state.record_message(9, 103, "mgr-2")

    reloaded = GatewayState(path)
    assert reloaded.session_for_message(9, 101) == "mgr-1"
    assert reloaded.session_for_message(9, 103) == "mgr-2"
    assert reloaded.session_for_message(9, 999) is None
    assert reloaded.session_for_message(8, 101) is None  # other chat


def test_message_map_is_bounded(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    for i in range(400):
        state.record_message(9, i, f"s{i}")
    raw = json.loads((tmp_path / "state.json").read_text())
    mapping = raw["chats"]["9"]["msg_sessions"]
    assert len(mapping) <= 300
    assert "0" not in mapping  # oldest evicted
    assert "399" in mapping


def test_turn_message_and_manager_choice(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(9, "mgr-1", 500)
    assert state.turn_message(9, "mgr-1") == 500
    assert state.turn_message(9, "mgr-2") is None

    state.set_manager(9, "claude", "claude-haiku-4-5")
    assert state.manager(9, {"provider": "opencode", "model": None}) == {
        "provider": "claude",
        "model": "claude-haiku-4-5",
    }
    # A chat with no choice falls back to the configured defaults.
    assert state.manager(8, {"provider": "opencode", "model": None}) == {
        "provider": "opencode",
        "model": None,
    }


def test_sessions_to_relay_unions_both_modes(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.select(9, "selected-lane")           # mode 1
    state.set_turn_message(9, "mgr-1", 700)    # manager mode
    state.set_turn_message(9, "mgr-2", 701)
    assert state.sessions_to_relay() == {9: {"selected-lane", "mgr-1", "mgr-2"}}


# ---- amux client: briefing verbs ---------------------------------------


def test_create_session_can_hold_start_and_carry_a_model():
    captured = {}

    def handler(request):
        if request.url.path == "/api/sessions":
            captured.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={})

    client = make_client(handler)
    try:
        run(client.create_session("mgr-1", cwd="/w", provider="opencode", model="m", start=False))
    finally:
        run(client.aclose())
    assert captured["body"] == {
        "name": "mgr-1",
        "dir": "/w",
        "provider": "opencode",
        "model": "m",
        "start": False,
    }


def test_set_instructions_never_applies():
    captured = {}

    def handler(request):
        captured.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    try:
        run(client.set_instructions("mgr-1", "be a manager"))
    finally:
        run(client.aclose())
    assert captured == {
        "path": "/api/sessions/mgr-1/instructions",
        "body": {"instructions": "be a manager"},  # no apply: start re-sends them
    }


def test_wait_until_running_refuses_a_stopped_lane():
    states = iter(
        [
            [{"name": "mgr-1", "activity": "none", "status": "stopped"}],
            [{"name": "mgr-1", "activity": "none", "status": "starting"}],
            [{"name": "mgr-1", "activity": "none", "status": "idle"}],
        ]
    )

    def handler(request):
        return httpx.Response(200, json=next(states))

    client = make_client(handler)
    try:
        row = run(client.wait_until_running("mgr-1", timeout=5, interval=0.01))
    finally:
        run(client.aclose())
    assert row["status"] == "idle"


def test_models_filters_by_provider():
    def handler(request):
        assert request.url.path == "/api/models"
        return httpx.Response(
            200,
            json={
                "models": [
                    {"provider": "claude", "id": "claude-haiku-4-5"},
                    {"provider": "opencode", "id": "deepseek-v4.1-flash-free"},
                ]
            },
        )

    client = make_client(handler)
    try:
        rows = run(client.models("claude"))
    finally:
        run(client.aclose())
    assert rows == [{"provider": "claude", "id": "claude-haiku-4-5"}]


# ---- manager creation sequence -----------------------------------------


def test_create_manager_briefs_before_starting():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-1", "activity": "none", "status": "idle"}]
            )
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(201, json={"ok": True})
        if request.url.path.endswith("/instructions"):
            return httpx.Response(200, json={"ok": True})
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    client = make_client(handler)
    cfg = Config(bot_token="1:t", allowed_user_ids=frozenset({1}), manager_cwd="/w")
    try:
        name, row = run(
            create_manager(client, cfg, name="mgr-1", provider="opencode", model=None)
        )
    finally:
        run(client.aclose())
    assert name == "mgr-1"
    assert row["status"] == "idle"
    posts = [(m, p) for (m, p) in calls if m == "POST"]
    assert posts[:3] == [
        ("POST", "/api/sessions"),
        ("POST", "/api/sessions/mgr-1/instructions"),
        ("POST", "/api/sessions/mgr-1/start"),
    ]


def test_new_manager_name_is_unique_and_prefixed():
    names = {new_manager_name() for _ in range(5)}
    assert len(names) == 5
    assert all(n.startswith("mgr-") for n in names)


# ---- the thread rule in the bot ----------------------------------------


def fake_update(message_id=100, reply_to=None, chat_id=9, user_id=1):
    reply = SimpleNamespace(message_id=reply_to) if reply_to else None
    message = SimpleNamespace(message_id=message_id, reply_to_message=reply, text="hi")
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id, username="owner"),
    )


def fake_context(state):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}))


def test_a_quote_reply_resolves_to_the_session_it_quotes(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.record_message(9, 55, "mgr-abc")
    assert botmod._session_from_reply(fake_update(reply_to=55), fake_context(state)) == "mgr-abc"
    # A reply to something we did not send is NOT a continuation.
    assert botmod._session_from_reply(fake_update(reply_to=42), fake_context(state)) is None
    # A fresh message is not a continuation either.
    assert botmod._session_from_reply(fake_update(), fake_context(state)) is None


class FakeBot:
    def __init__(self):
        self.sent = []
        self.created = []  # (chat_id, name) per create_forum_topic call

    async def send_message(
        self,
        chat_id,
        text,
        parse_mode=None,
        reply_to_message_id=None,
        message_thread_id=None,
    ):
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to_message_id,
                "thread": message_thread_id,
            }
        )
        return SimpleNamespace(message_id=900 + len(self.sent), text=text)

    async def create_forum_topic(self, chat_id, name=None):
        self.created.append((chat_id, name))
        return SimpleNamespace(message_thread_id=1000 + len(self.created), name=name)


def test_relay_replies_to_the_turn_message_and_records_its_own(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(9, "mgr-abc", 55)  # the owner's message that started the turn

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-abc", "activity": "none", "status": "idle"}]
            )
        return httpx.Response(200, json={"output": "[from Telegram @owner] task\nthe answer\n"})

    client = make_client(handler)
    bot = FakeBot()
    app = SimpleNamespace(
        bot=bot,
        bot_data={"cfg": None, "client": client, "state": state},
    )
    try:
        run(botmod.relay_once(app))
    finally:
        run(client.aclose())

    assert bot.sent == [
        {"chat_id": 9, "text": "the answer", "reply_to": 55, "thread": None}
    ]
    # The bot's own message id is now a continuation handle for that session.
    assert state.session_for_message(9, 901) == "mgr-abc"


def test_relay_does_not_repeat_an_unchanged_answer(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(9, "mgr-abc", 55)

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-abc", "activity": "none", "status": "idle"}]
            )
        return httpx.Response(200, json={"output": "[from Telegram @owner] task\nthe answer\n"})

    client = make_client(handler)
    bot = FakeBot()
    app = SimpleNamespace(bot=bot, bot_data={"cfg": None, "client": client, "state": state})
    try:
        run(botmod.relay_once(app))
        run(botmod.relay_once(app))  # same pane: nothing new to say
    finally:
        run(client.aclose())
    assert len(bot.sent) == 1


def test_relay_warns_once_when_the_pane_has_no_boundary(tmp_path, caplog):
    """A missing boundary used to be a *silent* `continue` — no log line at
    all. That silence hid the 2026-10-27 missing answers for hours: the agent
    never stamped `—— answer ——`, the prompt marker had scrolled out of the
    viewport, and the relay kept quietly skipping every cycle. Now it warns
    once per stretch (reset by a successful extract), so a sweep sees
    `relay_no_boundary` instead of a healthy-looking quiet log.
    """
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(9, "mgr-abc", 55)

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-abc", "activity": "none", "status": "idle"}]
            )
        # pane output with neither the prompt marker nor the sentinel
        return httpx.Response(200, json={"output": "⠋ telegram topics for mana…\n"})

    client = make_client(handler)
    bot = FakeBot()
    app = SimpleNamespace(
        bot=bot, bot_data={"cfg": None, "client": client, "state": state}
    )
    botmod._no_boundary_warned.clear()
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app))  # first miss: warns
            run(botmod.relay_once(app))  # still missing: stays quiet
    finally:
        run(client.aclose())
        botmod._no_boundary_warned.clear()

    warns = [
        r
        for r in caplog.records
        if r.name == "telegram_gateway" and "relay_no_boundary" in r.getMessage()
    ]
    assert len(warns) == 1, f"expected exactly one WARN, got {len(warns)}"
    assert bot.sent == []  # and nothing was delivered, honestly
