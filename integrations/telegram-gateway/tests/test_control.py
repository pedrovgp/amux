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
        "echoed": False,
        "recreated": False,
    }
    # created in Telegram AND bound in the gateway's own state — flagged for
    # the title sync (11.8: the birth name follows the session's OpenCode
    # title, once)
    assert bot.created == [(CHAT, "mgr-x")]
    assert state.topic_session(CHAT, 1001) == "mgr-x"
    assert state.session_topic(CHAT, "mgr-x") == 1001
    assert state.topic_created(CHAT, 1001) is True
    # the sweep signal
    created = [
        r.getMessage() for r in caplog.records if "topic_created" in r.getMessage()
    ]
    assert created == [
        f"topic_created chat_id={CHAT} thread=1001 name=mgr-x source=control"
    ]


def test_prompt_echo_becomes_the_topics_first_message_and_anchor(tmp_path, caplog):
    """The dispatcher's spawn call carries the task: it lands as the topic's
    FIRST message and becomes the turn anchor, so scrolling up shows what was
    asked and the worker's first answer replies to it — the owner's
    2026-10-29 ask, now carried through `POST /topic` (Milestone 11.6)."""
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    app = make_app(state, bot)
    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {"session": "wk-x", "name": "fix-tests", "chat_id": CHAT,
             "prompt": "Fix the red suite in django-k8s"},
        )
    assert status == 201 and payload["echoed"] is True
    assert bot.sent == [
        {"chat_id": CHAT, "text": "Fix the red suite in django-k8s",
         "reply_to": None, "thread": 1001}
    ]  # plain: no parse_mode, the task verbatim
    # anchored: the first answer replies to the task inside the topic, and
    # quoting the echo continues the worker (the same map every bind uses)
    assert state.turn_message(CHAT, "wk-x") == 901
    assert state.turn_thread(CHAT, "wk-x") == 1001
    assert state.session_for_message(CHAT, 901) == "wk-x"
    assert any("topic_prompt_echoed" in r.getMessage() for r in caplog.records)


def test_a_failed_echo_never_refuses_the_spawn(tmp_path, caplog):
    class DeafBot(FakeBot):
        async def send_message(self, *args, **kwargs):
            raise TelegramError("telegram is having a day")

    state = GatewayState(tmp_path / "state.json")
    app = make_app(state, DeafBot())
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        status, payload = post(app, {"session": "wk-y", "chat_id": CHAT, "prompt": "task"})
    assert status == 201 and payload["created"] is True
    assert payload["echoed"] is False  # honest, and the topic still exists
    assert state.topic_session(CHAT, 1001) == "wk-y"
    assert any("topic_prompt_echo_failed" in r.getMessage() for r in caplog.records)


def test_a_non_string_prompt_is_refused_and_prompt_stays_optional(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    assert post(app, {"session": "wk-z", "chat_id": CHAT, "prompt": 42})[0] == 400
    assert post(app, {"session": "wk-z", "chat_id": CHAT})[0] == 201


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
                report = await client.post(
                    f"{base}/message",
                    json={"session": "mgr-s", "text": "dispatch report"},
                )
            await server.stop()
        return health, answer, again, report

    health, answer, again, report = run(scenario())

    assert health.json()["status"] == "ok"
    assert answer.status_code == 201 and answer.json()["thread"] == 1001
    assert again.status_code == 200 and again.json()["created"] is False
    # the report rode the SAME socket into the SAME bound topic
    assert report.status_code == 201 and report.json()["thread"] == 1001
    assert state.topic_session(CHAT, 1001) == "mgr-s"
    assert bot.created == [(CHAT, "mgr-s")]
    assert bot.sent[-1]["text"] == "dispatch report"
    assert bot.sent[-1]["thread"] == 1001
    assert "topic_message_posted" in caplog.text
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
    # `state_dir` isolated on purpose: post_init spawns the pinned-menu task,
    # and against the LIVE state it would find the real group and POST to
    # Telegram with the test token (an InvalidToken network call from the
    # suite — the offline rule).
    cfg = make_cfg(tmp_path, control_port=control_port, state_dir=tmp_path / "gw")
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


def test_topic_bind_records_the_lanes_pin(tmp_path):
    """Milestone 11.9: /topic on a PINNED lane records its conversation id at
    BIND time. That pin is what switches the API relay on for the topic — the
    only channel that carries outputs of turns driven from anywhere (the
    owner's dashboard, the CLI, the dispatcher) — and it makes the binding
    rename-proof (resolution goes pin-first). Without it `set_topic` POPS
    topic_pins and the topic would relay through the pane sentinel only."""
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/sessions":
            return httpx.Response(200, json=[
                {"name": "wk-adopted", "activity": "none", "status": "idle",
                 "flags": "--session ses_adopted123"},
            ])
        return httpx.Response(200, json={})

    client = AmuxClient(
        "https://localhost:8824", transport=httpx.MockTransport(handler)
    )
    app = SimpleNamespace(bot=bot, bot_data={"state": state, "client": client})
    try:
        status, _payload = run(
            dispatch(app, "POST", "/topic",
                     json.dumps({"session": "wk-adopted", "chat_id": CHAT}).encode())
        )
    finally:
        run(client.aclose())

    assert status == 201
    assert state.pinned(CHAT, "wk-adopted") == "ses_adopted123"
    assert state.topic_pin(CHAT, 1001) == "ses_adopted123"  # the relay's key


def test_topic_bind_still_works_for_a_name_only_session(tmp_path):
    """A session amux cannot resolve (or no client at all) keeps the old
    name-only bind — never a refusal."""
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "session not found"})

    client = AmuxClient(
        "https://localhost:8824", transport=httpx.MockTransport(handler)
    )
    app = SimpleNamespace(bot=bot, bot_data={"state": state, "client": client})
    try:
        status, _payload = run(
            dispatch(app, "POST", "/topic",
                     json.dumps({"session": "ghost", "chat_id": CHAT}).encode())
        )
    finally:
        run(client.aclose())
    assert status == 201
    assert state.topic_session(CHAT, 1001) == "ghost"
    assert state.topic_pin(CHAT, 1001) is None


# ---- POST /message: a dispatch report lands IN its topic (owner, 2026-10-31) ---


def test_message_posts_into_the_sessions_bound_topic(tmp_path, caplog):
    """The dispatch report is delivered into the topic BOUND to the session —
    looked up by the gateway, never by a thread id the caller supplies."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-demo")
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {"session": "wk-demo",
             "text": "Dispatched: adoption demo → topic 191"},
            path="/message",
        )

    assert status == 201
    assert payload["thread"] == 191
    assert payload["msg_id"] == 901
    assert bot.sent == [
        {"chat_id": CHAT, "text": "Dispatched: adoption demo → topic 191",
         "reply_to": None, "thread": 191}
    ]  # plain: no parse_mode, the report verbatim
    # recorded against the session: quoting the report continues THAT session
    assert state.session_for_message(CHAT, 901) == "wk-demo"
    assert any("topic_message_posted" in r.getMessage() for r in caplog.records)


def test_message_without_a_topic_is_refused_honestly(tmp_path):
    """A session with no bound topic gets 409 — an honest refusal, never a
    message dropped silently or posted to General by accident."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "wk-loose", 5)  # lives in this chat, no topic
    bot = FakeBot()
    app = make_app(state, bot)

    status, payload = post(app, {"session": "wk-loose", "text": "hi"}, path="/message")

    assert status == 409
    assert payload["error"] == "no_topic"
    assert bot.sent == []


def test_message_resolves_the_chat_for_a_session_it_was_not_told(tmp_path):
    """The GM knows the session, not the chat id — the same contract as
    POST /topic's optional chat_id."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 77, "wk-x")  # binds chat AND thread
    bot = FakeBot()
    app = make_app(state, bot)

    status, payload = post(app, {"session": "wk-x", "text": "note"}, path="/message")
    assert (status, payload["thread"]) == (201, 77)

    # an unseen session without chat_id → 400, same wording as /topic
    status, payload = post(app, {"session": "ghost", "text": "n"}, path="/message")
    assert status == 400 and "chat_id" in payload["error"]


def test_message_validation_and_telegram_failure(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    state = app.bot_data["state"]
    state.set_topic(CHAT, 5, "wk-y")
    assert post(app, {"text": "x"}, path="/message")[0] == 400
    assert post(app, {"session": "wk-y"}, path="/message")[0] == 400
    assert post(app, {"session": "wk-y", "text": "   "}, path="/message")[0] == 400
    assert post(
        app, {"session": "wk-y", "text": "x", "chat_id": "nope"}, path="/message"
    )[0] == 400

    async def refuse(chat_id, text, message_thread_id=None):
        raise TelegramError("not enough rights")

    app.bot.send_message = refuse
    status, payload = post(
        app, {"session": "wk-y", "text": "x"}, path="/message"
    )
    assert status == 502 and "not enough rights" in payload["error"]
    assert "released" not in payload  # a rights failure is NOT a dead topic


def test_message_on_a_deleted_topic_releases_the_binding(tmp_path, caplog):
    """Measured live 2026-10-31: thread 191 had been deleted and the send
    answered "Message thread not found". Same lifecycle as the relay's
    404-send path — the stale binding is released, not kept forever."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-gone")
    bot = FakeBot()

    async def gone(chat_id, text, message_thread_id=None):
        raise TelegramError("Message thread not found")

    bot.send_message = gone
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app, {"session": "wk-gone", "text": "x"}, path="/message"
        )

    assert status == 502
    assert payload == {"error": "Message thread not found", "released": True}
    assert state.topic_session(CHAT, 191) is None  # the map is clean again
    assert state.session_topic(CHAT, "wk-gone") is None
    assert "topic_released" in caplog.text
    assert "reason=thread_missing" in caplog.text
    assert "source=message" in caplog.text


def test_message_route_table(tmp_path):
    app = make_app(GatewayState(tmp_path / "state.json"))
    assert post(app, method="GET", path="/message")[0] == 405
    assert post(app, body=b"{not json", path="/message")[0] == 400
    assert post(app, body=b"[1,2]", path="/message")[0] == 400


# ---- reconnect + intro: the owner's DEFAULT connect (2026-11-02) ------------


class IntroOc:
    """The injected `oc_api` for intro tests: a shared-serve reader stub."""

    def __init__(self, rows=(), fail=None):
        self.rows = [dict(r) for r in rows]
        self.fail = fail

    async def client_for_service(self, **kwargs):
        if self.fail is not None:
            raise self.fail
        return self

    async def messages(self, pin, **kwargs):
        return [dict(r) for r in self.rows]

    async def aclose(self):
        pass


def intro_rows(*texts):
    return [
        {
            "id": f"m{i}",
            "type": "assistant",
            "time": {"completed": 1},
            "content": [{"type": "text", "text": t}],
        }
        for i, t in enumerate(texts)
    ]


def intro_app(tmp_path, state, bot, *, rows=(), oc_fail=None,
              flags="--session ses_intro1"):
    """An app shaped like the running gateway's: state, cfg, oc_api, a lane."""
    cfg = make_cfg(tmp_path)  # isolated cfg (state_dir defaults aside)
    handler = lambda request: httpx.Response(  # noqa: E731 — the lane's pin
        200,
        json=[{"name": "wk-x", "activity": "none", "status": "idle",
               "flags": flags}] if flags else [],
    )
    client = AmuxClient(
        "https://localhost:8824", transport=httpx.MockTransport(handler)
    )
    return SimpleNamespace(
        bot=bot,
        bot_data={
            "state": state,
            "client": client,
            "cfg": cfg,
            "oc_api": IntroOc(rows, fail=oc_fail),
        },
    )


def test_reconnect_lands_the_context_in_the_live_topic(tmp_path, caplog):
    """Alive topic: the caller's orientation line lands there (echoed:true),
    nothing is created, and the turn anchor of an in-flight turn is NOT
    hijacked — record only."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-x")
    state.set_turn_message(CHAT, "wk-x", 55, thread=191)  # a live turn anchor
    bot = FakeBot()
    app = SimpleNamespace(
        bot=bot, bot_data={"state": state, "client": None, "cfg": make_cfg(tmp_path)}
    )

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {"session": "wk-x", "prompt": "Reconnecting — say hi",
             "reconnect": True},
        )

    assert status == 200
    assert payload == {"thread": 191, "session": "wk-x", "name": "wk-x",
                       "created": False, "echoed": True, "recreated": False}
    assert bot.created == []  # nothing new in Telegram
    assert bot.sent[0]["thread"] == 191
    assert bot.sent[0]["text"] == "Reconnecting — say hi"
    # the live turn keeps its anchor (a reconnect must not steal reply_to)
    assert state.turn_message(CHAT, "wk-x") == 55
    # quoting the landed context continues the session
    assert state.session_for_message(CHAT, 901) == "wk-x"


class DeadThreadBot(FakeBot):
    """A bot whose sends into GIVEN threads answer the measured
    'Message thread not found' (live, thread 191, 2026-10-31)."""

    def __init__(self, dead=()):
        super().__init__()
        self.dead = set(dead)

    async def send_message(self, chat_id, text, **kw):
        if kw.get("message_thread_id") in self.dead:
            raise TelegramError("Message thread not found")
        return await super().send_message(chat_id, text, **kw)


def test_reconnect_on_a_deleted_topic_recreates_it_with_the_intro(
    tmp_path, caplog
):
    """THE owner's default: the topic is gone → the stale binding is
    released, a fresh topic is created, and it OPENS with the conversation's
    last messages ("where we stopped")."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-x")  # bound to a thread Telegram deleted
    bot = DeadThreadBot(dead={191})
    app = intro_app(
        tmp_path, state, bot, rows=intro_rows("second answer", "first answer")
    )

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {"session": "wk-x", "chat_id": CHAT, "name": "The Task",
             "prompt": "Reconnecting", "reconnect": True, "intro": True},
        )

    assert status == 201
    assert payload["recreated"] is True and payload["echoed"] is True
    # the dead binding is gone, the fresh one is bound
    assert state.topic_session(CHAT, 191) is None
    assert state.topic_session(CHAT, 1001) == "wk-x"
    assert bot.created == [(CHAT, "The Task")]
    text = bot.sent[0]["text"]
    assert bot.sent[0]["thread"] == 1001
    assert text.startswith("Reconnecting")  # the caller's words first
    assert "📂 The Task — last messages before the pause:" in text
    assert "first answer" in text and "second answer" in text  # oldest first
    # the sweep signals
    assert any("topic_released" in m and "reason=thread_missing" in m
               and "source=connect" in m for m in caplog.messages)
    assert any("topic_recreated" in m for m in caplog.messages)


def test_reconnect_keeps_the_binding_when_telegram_refuses_differently(
    tmp_path, caplog
):
    """Only 'thread not found' proves death: a rights failure leaves the
    binding exactly as it was."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-x")
    bot = FakeBot()

    async def refuse(chat_id, text, **kw):
        raise TelegramError("Not enough rights to send messages")

    bot.send_message = refuse
    app = SimpleNamespace(
        bot=bot, bot_data={"state": state, "client": None, "cfg": make_cfg(tmp_path)}
    )

    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        status, payload = post(
            app, {"session": "wk-x", "prompt": "hi", "reconnect": True}
        )

    assert status == 200 and payload["echoed"] is False
    assert state.session_topic(CHAT, "wk-x") == 191  # KEPT
    assert bot.created == []
    assert any("topic_prompt_echo_failed" in m for m in caplog.messages)


def test_reconnect_without_content_probes_with_a_chat_action(tmp_path):
    """No prompt, no intro: a sendChatAction stands in as the liveness
    probe (no message, no content) — alive keeps the report honest."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-x")
    bot = FakeBot()
    app = SimpleNamespace(
        bot=bot, bot_data={"state": state, "client": None, "cfg": make_cfg(tmp_path)}
    )

    status, payload = post(app, {"session": "wk-x", "reconnect": True})

    assert status == 200 and payload["echoed"] is False
    assert bot.actions == [(CHAT, 191)]  # probed the bound thread
    assert bot.created == [] and bot.sent == []


def test_a_dead_thread_without_content_recreates_too(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, "wk-x")
    bot = FakeBot()
    bot.action_error = TelegramError("Message thread not found")
    app = SimpleNamespace(
        bot=bot, bot_data={"state": state, "client": None, "cfg": make_cfg(tmp_path)}
    )

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(app, {"session": "wk-x", "reconnect": True})

    assert status == 201 and payload["recreated"] is True
    assert state.session_topic(CHAT, "wk-x") == 1001  # rebound to the fresh topic
    assert any("topic_recreated" in m for m in caplog.messages)


def test_intro_on_a_fresh_create_carries_the_history(tmp_path):
    """`intro:true` without reconnect (the 🧩 browser's adopt): the new
    topic opens with the conversation's last messages."""
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    app = intro_app(tmp_path, state, bot, rows=intro_rows("the last answer"))

    status, payload = post(
        app, {"session": "wk-x", "chat_id": CHAT, "name": "Resumed",
              "intro": True}
    )

    assert status == 201 and payload["echoed"] is True
    text = bot.sent[0]["text"]
    assert text.startswith("📂 Resumed — last messages before the pause:")
    assert "the last answer" in text


def test_a_failed_intro_fetch_never_refuses_the_create(tmp_path, caplog):
    """The intro is a nicety: a dead OpenCode service degrades to a
    prompt-only echo (or none), never a refused topic."""
    from telegram_gateway.opencode_api import OcError

    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    app = intro_app(tmp_path, state, bot, oc_fail=OcError(0, "no serve (test)"))

    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        status, payload = post(
            app, {"session": "wk-x", "chat_id": CHAT, "intro": True,
                  "prompt": "task text"}
        )

    assert status == 201 and payload["created"] is True
    assert payload["echoed"] is True  # the prompt still landed
    assert bot.sent[0]["text"] == "task text"  # intro absent, no crash
    assert any("topic_intro_failed" in m for m in caplog.messages)
