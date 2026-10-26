"""Voice notes: the answer anchors on the TRANSCRIPTION, never on the audio.

The owner cannot read an audio bubble back, so the gateway's contract is:

1. the transcription is posted as a reply TO the audio message;
2. the agent's answer replies to the TRANSCRIPTION message (readable);
3. quoting the transcription continues that same session.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.config import Config
from telegram_gateway.guard import Guard
from telegram_gateway.state import GatewayState

log = __import__("logging").getLogger("test")


def run(coro):
    return asyncio.run(coro)


# ---- fakes ---------------------------------------------------------------


class FakeSentMessage:
    def __init__(self, message_id, text):
        self.message_id = message_id
        self.text = text


class FakeMessage:
    """An inbound message whose replies are recorded in order."""

    def __init__(self, message_id, *, voice=True, text=None, reply_to=None, next_id=700):
        self.message_id = message_id
        self.voice = SimpleNamespace(mime_type="audio/ogg") if voice else None
        self.text = text
        self.reply_to_message = (
            SimpleNamespace(message_id=reply_to) if reply_to is not None else None
        )
        self._next_id = next_id
        self.replies = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        sent = FakeSentMessage(self._next_id, text)
        self._next_id += 1
        self.replies.append(
            {"message_id": sent.message_id, "text": text, "reply_to": self.message_id}
        )
        return sent


def fake_update(message):
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=SimpleNamespace(id=9),
        effective_user=SimpleNamespace(id=1, username="owner"),
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


def fake_transcriber(monkeypatch, text="fala de teste"):
    async def _fake(voice, transcriber, tmp_dir=None):
        return text

    monkeypatch.setattr(botmod, "transcribe_voice", _fake)


def make_amux(names: list[str], sent: list | None = None):
    """A client whose /api/sessions answers the given names (lanes)."""

    def handler(request):
        if request.method == "GET" and request.url.path == "/api/sessions":
            return httpx.Response(
                200,
                json=[{"name": n, "activity": "none", "status": "idle"} for n in names],
            )
        if request.method == "POST" and request.url.path.endswith("/send"):
            if sent is not None:
                sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


def make_manager_amux(sent: list | None = None):
    """A client that creates whatever manager lane the gateway asks for."""

    created = {}

    def handler(request):
        if request.method == "POST" and request.url.path == "/api/sessions":
            created["name"] = json.loads(request.content)["name"]
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and request.url.path == "/api/sessions":
            name = created.get("name")
            rows = [{"name": name, "activity": "none", "status": "idle"}] if name else []
            return httpx.Response(200, json=rows)
        if request.method == "POST" and request.url.path.endswith("/send"):
            if sent is not None:
                sent.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


def fake_context(cfg, client, state):
    guard = Guard(frozenset({1}), log)
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"cfg": cfg, "client": client, "state": state, "guard": guard}
        )
    )


# ---- manager mode: a fresh voice note ------------------------------------


def test_fresh_voice_note_anchors_the_answer_on_the_transcription(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent: list = []
    client = make_manager_amux(sent)
    fake_transcriber(monkeypatch)
    message = FakeMessage(message_id=100)
    update = fake_update(message)

    try:
        run(botmod.on_voice(update, fake_context(cfg, client, state)))
    finally:
        run(client.aclose())

    # 1. The transcription is a reply to the audio itself.
    assert message.replies[0]["text"] == "🗣️ Transcribed: fala de teste"
    assert message.replies[0]["reply_to"] == 100
    transcription_id = message.replies[0]["message_id"]

    # 2. The answer's anchor is the TRANSCRIPTION, not the audio message.
    session = state.selected(9)  # not used in manager mode; find it from the map
    assert session is None
    session = state.session_for_message(9, message.replies[1]["message_id"])
    assert session and session.startswith("mgr-")
    assert state.turn_message(9, session) == transcription_id
    assert state.turn_message(9, session) != 100  # never the unreadable audio

    # 3. Quoting the transcription continues that same session.
    assert state.session_for_message(9, transcription_id) == session

    # 4. The agent received the transcript as its prompt.
    assert sent and sent[0]["text"] == "[from Telegram @owner] fala de teste"


def test_voice_reply_continues_the_quoted_session_and_anchors_the_answer(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.record_message(9, 55, "mgr-abc")  # a previous answer, quotable
    sent: list = []
    client = make_amux(["mgr-abc"], sent)
    fake_transcriber(monkeypatch)
    message = FakeMessage(message_id=100, reply_to=55)
    update = fake_update(message)

    try:
        run(botmod.on_voice(update, fake_context(cfg, client, state)))
    finally:
        run(client.aclose())

    transcription_id = message.replies[0]["message_id"]
    assert message.replies[0]["text"] == "🗣️ Transcribed: fala de teste"
    # The answer goes to the QUOTED session...
    assert sent and sent[0]["text"] == "[from Telegram @owner] fala de teste"
    # ...and replies to the transcription, not to the audio.
    assert state.turn_message(9, "mgr-abc") == transcription_id
    # Quoting the transcription continues the same session.
    assert state.session_for_message(9, transcription_id) == "mgr-abc"


# ---- selected session: the ✍️ prompt state --------------------------------


def test_selected_session_voice_anchors_on_the_transcription(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(9, "lane-1")
    sent: list = []
    client = make_amux(["lane-1"], sent)
    fake_transcriber(monkeypatch)
    message = FakeMessage(message_id=100)
    update = fake_update(message)

    try:
        outcome = run(
            botmod.receive_prompt_voice(update, fake_context(cfg, client, state))
        )
    finally:
        run(client.aclose())

    assert outcome == botmod.ConversationHandler.END
    transcription_id = message.replies[0]["message_id"]
    assert message.replies[0]["text"] == "🗣️ Transcribed: fala de teste"
    assert state.turn_message(9, "lane-1") == transcription_id
    assert state.session_for_message(9, transcription_id) == "lane-1"
    assert sent and sent[0]["text"] == "[from Telegram @owner] fala de teste"


def test_text_turn_still_anchors_on_the_owner_message(tmp_path):
    """A plain text message keeps its old behavior: no transcription step."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.record_message(9, 55, "mgr-abc")
    sent: list = []
    client = make_amux(["mgr-abc"], sent)
    message = FakeMessage(message_id=100, voice=False, text="oi", reply_to=55)
    update = fake_update(message)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state)))
    finally:
        run(client.aclose())

    assert state.turn_message(9, "mgr-abc") == 100  # the owner's message itself
    assert sent and sent[0]["text"] == "[from Telegram @owner] oi"
