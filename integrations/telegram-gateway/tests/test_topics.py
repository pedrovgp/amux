"""Forum topics: one Telegram topic per manager session (the topic rule).

The owner runs a Topics-enabled group with the bot. The contract:

1. a message in **General** starts a fresh manager AND its own topic,
   named after the session;
2. a message inside a **mapped topic** continues that session — no reply or
   quote needed, the topic itself selects the session;
3. a message inside an **unmapped topic** starts a manager that adopts it;
4. answers post into the topic (`message_thread_id`) and never reply across
   threads — an anchor outside the topic is dropped, not misused;
5. a failed/dead session releases its topic so the next message can retry.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
from telegram.error import TelegramError

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.config import Config
from telegram_gateway.guard import Guard
from telegram_gateway.state import GatewayState

log = __import__("logging").getLogger("test")

CHAT = -100  # a supergroup id (the forum group)


def run(coro):
    return asyncio.run(coro)


# ---- state: the topic <-> session map -----------------------------------


def test_topic_map_roundtrip_and_restart(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_topic(CHAT, 42, "mgr-a")
    state.set_topic(CHAT, 43, "mgr-b")

    reloaded = GatewayState(path)
    assert reloaded.topic_session(CHAT, 42) == "mgr-a"
    assert reloaded.topic_session(CHAT, 43) == "mgr-b"
    assert reloaded.topic_session(CHAT, 44) is None
    assert reloaded.topic_session(CHAT, None) is None
    assert reloaded.topic_session(-999, 42) is None  # other chat
    assert reloaded.session_topic(CHAT, "mgr-b") == 43
    assert reloaded.session_topic(CHAT, "mgr-z") is None


def test_topic_map_is_bounded(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    for i in range(250):
        state.set_topic(CHAT, i, f"s{i}")
    raw = json.loads((tmp_path / "state.json").read_text())
    assert len(raw["chats"][str(CHAT)]["topics"]) <= 200


def test_drop_session_topics_releases_only_that_session(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_topic(CHAT, 42, "mgr-a")
    state.set_topic(CHAT, 43, "mgr-a")
    state.set_topic(CHAT, 44, "mgr-b")

    assert sorted(state.drop_session_topics(CHAT, "mgr-a")) == [42, 43]
    assert state.topic_session(CHAT, 42) is None
    assert state.topic_session(CHAT, 44) == "mgr-b"  # untouched
    assert GatewayState(path).topic_session(CHAT, 43) is None  # persisted


def test_turn_anchor_carries_its_thread(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_turn_message(CHAT, "mgr-a", 55, thread=42)
    assert state.turn_message(CHAT, "mgr-a") == 55
    assert state.turn_thread(CHAT, "mgr-a") == 42

    state.set_turn_message(CHAT, "mgr-a", 56)  # outside any topic
    assert state.turn_message(CHAT, "mgr-a") == 56
    assert state.turn_thread(CHAT, "mgr-a") is None

    reloaded = GatewayState(path)
    assert reloaded.turn_message(CHAT, "mgr-a") == 56
    assert reloaded.turn_thread(CHAT, "mgr-a") is None


def test_legacy_integer_turn_entries_still_read(tmp_path):
    """State written before threads were tracked must not crash the readers."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"chats": {"9": {"turn_msg": {"mgr-old": 700}}}}), encoding="utf-8"
    )
    state = GatewayState(path)
    assert state.turn_message(9, "mgr-old") == 700
    assert state.turn_thread(9, "mgr-old") is None


# ---- fakes ---------------------------------------------------------------


class FakeSentMessage:
    def __init__(self, message_id, text):
        self.message_id = message_id
        self.text = text
        self.edits = []

    async def edit_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append(text)
        self.text = text
        return self

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        return FakeSentMessage(self.message_id + 1, text)


class FakeMessage:
    def __init__(self, message_id, text=None, thread=None, reply_to=None, voice=False):
        self.message_id = message_id
        self.text = text
        self.message_thread_id = thread
        self.reply_to_message = (
            SimpleNamespace(message_id=reply_to) if reply_to is not None else None
        )
        self.voice = SimpleNamespace(mime_type="audio/ogg") if voice else None
        self.replies = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        sent = FakeSentMessage(700 + len(self.replies), text)
        self.replies.append(
            {
                "message_id": sent.message_id,
                "text": text,
                "reply_to": self.message_id,
                "reply_markup": reply_markup,
                "obj": sent,
            }
        )
        return sent


class FakeBot:
    def __init__(self):
        self.sent = []
        self.created = []  # (chat_id, name) per create_forum_topic call

    async def create_forum_topic(self, chat_id, name=None):
        self.created.append((chat_id, name))
        return SimpleNamespace(message_thread_id=1000 + len(self.created), name=name)

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
        return FakeSentMessage(900 + len(self.sent), text)


def fake_update(message, *, forum=True, chat_id=CHAT):
    chat = SimpleNamespace(id=chat_id)
    if forum:
        chat.is_forum = True
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1, username="owner"),
        callback_query=None,
    )


def make_cfg(tmp_path, **kw):
    instructions = tmp_path / "manager_prompt.md"
    instructions.write_text("You are the manager.", encoding="utf-8")
    defaults = dict(
        bot_token="1:test-token",
        allowed_user_ids=frozenset({1}),
        groq_api_key="gsk-test",
        manager_cwd="/w",
        manager_instructions_file=instructions,
    )
    defaults.update(kw)
    return Config(**defaults)


def fake_context(cfg, client, state, bot):
    guard = Guard(frozenset({1}), log)
    return SimpleNamespace(
        application=SimpleNamespace(
            bot=bot,
            bot_data={"cfg": cfg, "client": client, "state": state, "guard": guard},
        )
    )


def make_manager_amux(sent=None, created=None, status=201, names=()):
    """A client that creates whatever manager lane the gateway asks for.

    ``names`` pre-populates the session list (a session that already exists
    on the "server" from a previous turn, e.g. a mapped topic's manager).
    """
    holder = {}

    def handler(request):
        if request.method == "POST" and request.url.path == "/api/sessions":
            if status >= 400:
                return httpx.Response(status, json={"error": "boom"})
            name = json.loads(request.content)["name"]
            holder["name"] = name
            if created is not None:
                created.append(name)
            return httpx.Response(status, json={"ok": True})
        if request.method == "GET" and request.url.path == "/api/sessions":
            rows = [
                {"name": n, "activity": "none", "status": "idle"} for n in names
            ]
            if holder.get("name"):
                rows.append(
                    {"name": holder["name"], "activity": "none", "status": "idle"}
                )
            return httpx.Response(200, json=rows)
        if request.method == "POST" and request.url.path.endswith("/send"):
            if sent is not None:
                sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


def fake_transcriber(monkeypatch, text="assunto falado"):
    async def _fake(voice, transcriber, tmp_dir=None):
        return text

    monkeypatch.setattr(botmod, "transcribe_voice", _fake)


# ---- routing: General / mapped topic / unmapped topic ---------------------


def test_general_message_routes_to_the_dispatcher(tmp_path, caplog):
    """M11.5: forum General mints NOTHING — the sender's own general-mgr
    answers right here; its prompt is one send, anchored in General."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("general-mgr-1",))
    bot = FakeBot()
    message = FakeMessage(message_id=100, text="start a research task", thread=None)
    update = fake_update(message)

    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert created == []  # no manager lane minted
    assert bot.created == []  # no topic minted
    assert message.replies == []  # no ack: the answer IS the reply
    # `sent` carries the stamped prompt; the target is proven three ways:
    # nothing else was created, no error/ack reply appeared, and the anchor
    # (below) is on the dispatcher itself.
    assert sent == [{"text": "[from Telegram @owner] start a research task"}]
    # anchored in GENERAL: the dispatcher's answer replies to the owner's
    # message right here (thread None = General)
    assert state.turn_message(CHAT, "general-mgr-1") == 100
    assert state.turn_thread(CHAT, "general-mgr-1") is None
    assert any(
        "prompt_text" in r.getMessage() and "route=dispatcher" in r.getMessage()
        for r in caplog.records
    )


def test_message_in_a_mapped_topic_continues_that_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("mgr-x",))
    bot = FakeBot()
    message = FakeMessage(message_id=200, text="and now what?", thread=500)
    update = fake_update(message)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert bot.created == []  # no new topic
    assert created == []  # no new manager
    assert sent and sent[0]["text"] == "[from Telegram @owner] and now what?"
    # the anchor lives INSIDE the topic -> the answer may reply to it
    assert state.turn_message(CHAT, "mgr-x") == 200
    assert state.turn_thread(CHAT, "mgr-x") == 500
    # no ack: the agent's answer is the confirmation
    assert message.replies == []


def test_message_in_an_unmapped_topic_adopts_a_manager(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = FakeBot()
    message = FakeMessage(message_id=210, text="topic made by hand", thread=777)
    update = fake_update(message)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    # no topic was CREATED — the existing one is adopted by the new session
    assert bot.created == []
    assert created and created[0].startswith("mgr-")
    assert state.topic_session(CHAT, 777) == created[0]
    assert state.session_topic(CHAT, created[0]) == 777
    # ack landed inside that topic (a reply to the in-topic message)
    assert message.replies and "this topic" in message.replies[0]["text"]
    assert state.turn_thread(CHAT, created[0]) == 777
    assert bot.sent == []  # Task 3: adoption never echoes — the owner already
    # typed inside the topic he made
    assert sent and sent[0]["text"] == "[from Telegram @owner] topic made by hand"


def test_non_forum_chat_never_touches_topics(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = FakeBot()
    message = FakeMessage(message_id=300, text="hello", thread=None)
    update = fake_update(message, forum=False, chat_id=9)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert bot.created == []
    assert created and created[0].startswith("mgr-")
    assert state.session_topic(9, created[0]) is None
    # old contract kept: a private chat gets the reply-in-thread ack
    assert message.replies and "reply here" in message.replies[0]["text"]
    assert bot.sent == []  # Task 3: no topic, no echo — the ack is the only reply


def test_manager_create_failure_releases_the_adopted_topic(tmp_path):
    """An unmapped topic adopted the lane; creation failing must give the
    topic back (adoptable), with the failure edited into the ack."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    client = make_manager_amux(status=500)
    bot = FakeBot()
    message = FakeMessage(message_id=120, text="do it", thread=777)
    update = fake_update(message)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    # adoption never mints a topic …
    assert bot.created == []
    # … and the one it BOUND is released when the lane never came up
    assert state.topic_session(CHAT, 777) is None
    # the failure was edited into the ack, not swallowed
    ack = message.replies[0]["obj"]
    assert ack.edits and "Could not start a manager" in ack.edits[-1]


# ---- voice in the forum ---------------------------------------------------


def test_voice_in_general_goes_to_the_dispatcher(tmp_path, monkeypatch):
    """M11.5 for voice: the TRANSCRIPT (never the audio) goes to the sender's
    dispatcher, and its answer replies to the transcript here in General."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("general-mgr-1",))
    bot = FakeBot()
    fake_transcriber(monkeypatch)
    message = FakeMessage(message_id=400, thread=None, voice=True)
    update = fake_update(message)

    try:
        run(botmod.on_voice(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    # transcription echoed in General (readable where the voice was sent)...
    assert message.replies[0]["text"] == "🗣️ Transcribed: assunto falado"
    transcription_id = message.replies[0]["message_id"]
    # ...and delivered to the dispatcher: no topic, no manager, one send
    assert created == []
    assert bot.created == []
    assert sent == [{"text": "[from Telegram @owner] assunto falado"}]
    # the ANCHOR is the transcript (M4's voice rule), here in General
    assert state.turn_message(CHAT, "general-mgr-1") == transcription_id
    assert state.turn_thread(CHAT, "general-mgr-1") is None
    # quoting the General transcription still continues the dispatcher
    assert state.session_for_message(CHAT, transcription_id) == "general-mgr-1"


def test_voice_inside_a_mapped_topic_continues_it(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("mgr-x",))
    bot = FakeBot()
    fake_transcriber(monkeypatch)
    message = FakeMessage(message_id=410, thread=500, voice=True)
    update = fake_update(message)

    try:
        run(botmod.on_voice(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert bot.created == []
    assert created == []
    transcription_id = message.replies[0]["message_id"]
    assert sent and sent[0]["text"] == "[from Telegram @owner] assunto falado"
    # anchor is the transcription, INSIDE the topic -> answer may reply to it
    assert state.turn_message(CHAT, "mgr-x") == transcription_id
    assert state.turn_thread(CHAT, "mgr-x") == 500


# ---- relay: into the topic, never across threads --------------------------


def make_relay_app(state, bot):
    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-x", "activity": "none", "status": "idle"}]
            )
        return httpx.Response(
            200, json={"output": "[from Telegram @owner] task\nthe answer\n"}
        )

    client = AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))
    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "cfg": Config(bot_token="1:test-token", allowed_user_ids=frozenset({1})),
            "client": client,
            "state": state,
        },
    )
    return app, client


def test_relay_answers_inside_the_topic_it_belongs_to(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    state.set_turn_message(CHAT, "mgr-x", 55, thread=500)  # anchor in-topic
    bot = FakeBot()
    app, client = make_relay_app(state, bot)
    try:
        run(botmod.relay_once(app))
    finally:
        run(client.aclose())
    assert bot.sent == [
        {"chat_id": CHAT, "text": "the answer", "reply_to": 55, "thread": 500}
    ]


def test_relay_never_replies_across_threads(tmp_path):
    """Turn 1 of a new topic: anchor in General, session has a topic.

    The answer must land IN the topic as a plain message — replying to the
    General anchor from inside the topic is impossible in Telegram.
    """
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    state.set_turn_message(CHAT, "mgr-x", 55, thread=None)
    bot = FakeBot()
    app, client = make_relay_app(state, bot)
    try:
        run(botmod.relay_once(app))
    finally:
        run(client.aclose())
    assert bot.sent == [
        {"chat_id": CHAT, "text": "the answer", "reply_to": None, "thread": 500}
    ]


def test_relay_without_a_topic_keeps_the_reply_anchor(tmp_path):
    """A session with no topic (private chat) behaves exactly as before."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(9, "mgr-x", 55, thread=None)
    bot = FakeBot()
    app, client = make_relay_app(state, bot)
    try:
        run(botmod.relay_once(app))
    finally:
        run(client.aclose())
    assert bot.sent == [
        {"chat_id": 9, "text": "the answer", "reply_to": 55, "thread": None}
    ]
