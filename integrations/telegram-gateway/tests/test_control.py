"""The tracked topic path: managers never call the Bot API themselves.

A topic created outside the gateway is invisible to it — unbound in
``state.json``, so the owner's first message there would start yet another
manager. ``POST /topic`` is the one sanctioned path: create, bind, log.

Covers the route table, the create/bind/log contract, idempotency, the
duplicate-name refusal, failure signalling, and the real socket.
"""
import asyncio
import json
import logging
import socket
from types import SimpleNamespace

import httpx

from telegram.error import TelegramError

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.control import ControlServer, dispatch
from telegram_gateway.state import GatewayState

from test_topics import CHAT, FakeBot, make_cfg, run


def make_app(state, bot=None):
    return SimpleNamespace(bot=bot or FakeBot(), bot_data={"state": state})


def post(app, payload=None, method="POST", path="/topic", body=None):
    if body is None:
        body = json.dumps(payload).encode() if payload is not None else b""
    return run(dispatch(app, method, path, body))


def free_port() -> int:
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    holder.close()
    return port


# ---- route table ----------------------------------------------------------


def test_health_route_answers_ok(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    status, payload = post(app, method="GET", path="/health")
    assert (status, payload) == (200, {"status": "ok", "service": "telegram-gateway"})


def test_unknown_route_and_bad_bodies(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    assert post(app, method="GET", path="/topic")[0] == 405
    assert post(app, method="POST", path="/nope")[0] == 404
    assert post(app, body=b"{not json")[0] == 400
    assert post(app, body=b"[1,2]")[0] == 400


# ---- create + bind + log --------------------------------------------------


def test_topic_is_created_bound_and_logged(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 5)  # the session already lives here
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(app, {"session": "mgr-x"})

    assert status == 201
    assert payload == {
        "thread": 1001,
        "session": "mgr-x",
        "name": "mgr-x",
        "created": True,
    }
    # created in Telegram AND bound in the gateway's own state
    assert bot.created == [(CHAT, "mgr-x")]
    assert state.topic_session(CHAT, 1001) == "mgr-x"
    assert state.session_topic(CHAT, "mgr-x") == 1001
    # the sweep signal
    created = [
        r.getMessage() for r in caplog.records if "topic_created" in r.getMessage()
    ]
    assert created == [
        f"topic_created chat_id={CHAT} thread=1001 name=mgr-x source=control"
    ]


def test_explicit_chat_id_and_custom_name(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")  # no state for this session
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app, {"session": "mgr-y", "chat_id": CHAT, "name": "Research task"}
        )

    assert status == 201
    assert bot.created == [(CHAT, "Research task")]
    assert state.topic_session(CHAT, 1001) == "mgr-y"
    assert "source=control" in caplog.text


def test_session_without_a_chat_is_rejected(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    status, payload = post(app, {"session": "ghost"})
    assert status == 400
    assert "chat_id" in payload["error"]


def test_missing_or_bad_session_is_rejected(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    assert post(app, {})[0] == 400
    assert post(app, {"session": "   "})[0] == 400
    assert post(app, {"session": "mgr-a", "chat_id": "not-an-id"})[0] == 400
    assert post(app, {"session": "mgr-a", "chat_id": CHAT, "name": 7})[0] == 400


# ---- idempotency and name collisions -------------------------------------


def test_repeating_the_call_creates_nothing(tmp_path, caplog):
    """The owner asked for one topic; a retried call must not open a second."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 1001, "mgr-x")
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(app, {"session": "mgr-x"})

    assert status == 200
    assert payload["created"] is False
    assert payload["thread"] == 1001
    assert bot.created == []  # no Bot API call at all
    assert "topic_exists" in caplog.text
    assert "topic_created" not in caplog.text


def test_a_name_held_by_another_session_is_refused(tmp_path):
    """Telegram allows duplicate topic names; the gateway's map does not."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 1001, "mgr-x")
    bot = FakeBot()
    app = make_app(state, bot)

    status, payload = post(
        app, {"session": "mgr-y", "chat_id": CHAT, "name": "mgr-x"}
    )

    assert status == 409
    assert payload == {
        "error": "name_exists",
        "name": "mgr-x",
        "thread": 1001,
        "session": "mgr-x",
    }
    assert bot.created == []


def test_bot_api_failure_is_reported_not_swallowed(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()

    async def failing(chat_id, name=None):
        raise TelegramError("forum not available")

    bot.create_forum_topic = failing
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(app, {"session": "mgr-z", "chat_id": CHAT})

    assert status == 502
    assert "forum not available" in payload["error"]
    assert state.session_topic(CHAT, "mgr-z") is None  # nothing half-bound
    assert "topic_create_failed" in caplog.text


# ---- the real socket ------------------------------------------------------


def test_control_server_over_a_real_socket(tmp_path, caplog):
    """One event loop: the listener must be alive while the client asks."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-s", 7)
    bot = FakeBot()
    app = make_app(state, bot)
    server = ControlServer(app, 0)  # 0 = ephemeral port

    async def scenario():
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            await server.start()
            assert server.running
            base = f"http://127.0.0.1:{server.port}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                health = await client.get(f"{base}/health")
                answer = await client.post(f"{base}/topic", json={"session": "mgr-s"})
                again = await client.post(f"{base}/topic", json={"session": "mgr-s"})
            await server.stop()
        return health, answer, again

    health, answer, again = run(scenario())

    assert health.json()["status"] == "ok"
    assert answer.status_code == 201 and answer.json()["thread"] == 1001
    assert again.status_code == 200 and again.json()["created"] is False
    assert state.topic_session(CHAT, 1001) == "mgr-s"
    assert bot.created == [(CHAT, "mgr-s")]
    assert "control_started" in caplog.text
    assert "topic_created" in caplog.text and "source=control" in caplog.text
    assert "control_stopped" in caplog.text
    assert not server.running


def test_control_server_refuses_to_start_on_a_busy_port(tmp_path, caplog):
    """A taken port must not take the gateway down with it."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        server = ControlServer(make_app(GatewayState(tmp_path / "state.json")), port)
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(server.start())
        assert not server.running
        assert "control_start_failed" in caplog.text
    finally:
        holder.close()


# ---- wired into the running gateway ---------------------------------------


def _prepared_app(tmp_path, control_port):
    cfg = make_cfg(tmp_path, control_port=control_port)
    app = botmod.build_application(cfg)
    # never touch the live amux server from a unit test
    app.bot_data["client"] = AmuxClient(
        "https://localhost:1",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"status": "ok"})
        ),
    )
    return app


def test_post_init_starts_the_endpoint_and_shutdown_stops_it(tmp_path, caplog):
    app = _prepared_app(tmp_path, free_port())

    async def scenario():
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            await botmod.post_init(app)
            started = app.bot_data["control"].running
            port = app.bot_data["control"].port
            await botmod.post_shutdown(app)
            stopped = not app.bot_data["control"].running
        return started, port, stopped

    started, port, stopped = run(scenario())
    assert started and stopped
    assert port != 0
    assert "control_started" in caplog.text
    assert "control_stopped" in caplog.text
    assert "gateway_started" in caplog.text


def test_post_init_skips_the_endpoint_when_disabled(tmp_path, caplog):
    app = _prepared_app(tmp_path, 0)

    async def scenario():
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            await botmod.post_init(app)
            running = app.bot_data["control"].running
            await botmod.post_shutdown(app)
        return running

    assert run(scenario()) is False
    assert "control_disabled" in caplog.text
