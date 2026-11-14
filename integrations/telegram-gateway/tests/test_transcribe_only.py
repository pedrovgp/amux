"""A TRANSCRIBE-ONLY topic: audio in, transcript out, nothing else woken.

The owner's ask (2026-11-14): one topic he keeps open where he only sends
voice notes and gets back ONLY the transcription — no agent may be woken,
nothing else happens. Before this feature every voice note echoed and then
ROUTED (bound topic → `_continue_session` wakes the agent; unbound topic →
`_start_manager` spawns one), and text/photos routed the same way.

Contract covered here:

1. voice in a flagged topic → the existing `🗣️ Transcribed:` echo, then
   RETURN: no amux send, no manager, no turn anchor, and the distinct
   `topic=transcribe_only=1` log line;
2. text (and photo) in a flagged topic → ONE visible line, routed nowhere;
3. topics WITHOUT the flag behave exactly as before (suite-green proof);
4. `POST /topic` accepts `"transcribe_only": true` (bool-checked), persists
   the flag in `state.json`, binds the placeholder WITHOUT a title-sync flag
   and WITHOUT a turn anchor, and re-applies it on the reconnect flow.
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from telegram_gateway import bot as botmod
from telegram_gateway.control import dispatch
from telegram_gateway.state import GatewayState

from test_topics import (
    CHAT,
    FakeBot,
    FakeMessage,
    fake_context,
    fake_transcriber,
    fake_update,
    make_cfg,
    make_manager_amux,
    run,
)

PLACEHOLDER = "transcribe-only"


def flagged_state(tmp_path, thread=700, name="Transcrição de áudios"):
    """State with one topic bound to the placeholder AND flagged."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, thread, PLACEHOLDER, name)
    state.set_transcribe_only(CHAT, thread, True)
    return state


def post(app, payload):
    body = json.dumps(payload).encode() if payload is not None else b""
    return run(dispatch(app, "POST", "/topic", body))


def make_app(state, bot=None):
    return SimpleNamespace(bot=bot or FakeBot(), bot_data={"state": state})


class FakePhotoSize:
    file_size = 10

    async def get_file(self):
        raise AssertionError("a flagged topic must never download a photo")


# ---- branch 1: voice ------------------------------------------------------


def test_voice_in_a_transcribe_only_topic_echoes_and_routes_nowhere(
    tmp_path, monkeypatch, caplog
):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = flagged_state(tmp_path)
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = FakeBot()
    fake_transcriber(monkeypatch, text="bom dia chefe")
    message = FakeMessage(message_id=600, thread=700, voice=True)
    update = fake_update(message)

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_voice(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    # the echo — the whole answer — is posted as a reply to the audio
    assert message.replies[0]["text"] == "🗣️ Transcribed: bom dia chefe"
    assert message.replies[0]["reply_to"] == 600
    # ...and NOTHING else: no prompt delivered, no manager, no topic minted
    assert sent == []
    assert created == []
    assert bot.created == []
    assert bot.sent == []
    # no turn anchor, no quote-map entry: the placeholder stays out of every
    # relay/sweep map (`sessions_to_relay` is selections + turn_msg)
    assert state.turn_message(CHAT, PLACEHOLDER) is None
    assert state.session_for_message(CHAT, message.replies[0]["message_id"]) is None
    # the two-fix log signal: one distinct, greppable line
    tagged = [
        r.getMessage()
        for r in caplog.records
        if "voice_transcribed" in r.getMessage() and "topic=transcribe_only=1" in r.getMessage()
    ]
    assert tagged, "the transcribe-only voice branch must announce itself"


# ---- branch 2: text / photo ----------------------------------------------


def test_text_in_a_transcribe_only_topic_gets_one_line_and_routes_nowhere(
    tmp_path, caplog
):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = flagged_state(tmp_path)
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = FakeBot()
    message = FakeMessage(message_id=601, thread=700, text="o que você faz?")
    update = fake_update(message)

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert [r["text"] for r in message.replies] == [
        "🎙️ Este tópico só transcreve áudios."
    ]
    assert message.replies[0]["reply_to"] == 601
    assert sent == []  # no session resolved, nothing delivered
    assert created == []  # no manager spawned
    assert bot.created == []
    assert state.turn_message(CHAT, PLACEHOLDER) is None
    assert any(
        "transcribe_only_blocked" in r.getMessage() and "source=text" in r.getMessage()
        for r in caplog.records
    )


def test_photo_in_a_transcribe_only_topic_gets_one_line_and_saves_nothing(
    tmp_path, caplog
):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = flagged_state(tmp_path)
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = FakeBot()
    message = FakeMessage(message_id=602, thread=700, photo=[FakePhotoSize()])
    update = fake_update(message)

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_photo(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert [r["text"] for r in message.replies] == [
        "🎙️ Este tópico só transcreve áudios."
    ]
    assert sent == []
    assert created == []
    assert not (tmp_path / "state" / "photos").exists()  # never even saved
    assert any(
        "transcribe_only_blocked" in r.getMessage() and "source=photo" in r.getMessage()
        for r in caplog.records
    )


# ---- branch 3: normal topics unchanged -----------------------------------


def test_voice_in_a_normal_topic_still_continues_its_session(tmp_path, monkeypatch):
    """The flag is the ONLY difference: an ordinary topic keeps today's
    behavior (echo → deliver to the bound session → anchor on the transcript)."""
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("mgr-x",))
    bot = FakeBot()
    fake_transcriber(monkeypatch, text="assunto falado")
    message = FakeMessage(message_id=610, thread=500, voice=True)
    update = fake_update(message)

    try:
        run(botmod.on_voice(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert message.replies[0]["text"] == "🗣️ Transcribed: assunto falado"
    assert sent == [{"text": "[from Telegram @owner] assunto falado"}]
    assert state.turn_message(CHAT, "mgr-x") == message.replies[0]["message_id"]
    assert state.turn_thread(CHAT, "mgr-x") == 500


def test_text_in_a_normal_topic_still_continues_its_session(tmp_path):
    cfg = make_cfg(tmp_path, state_dir=tmp_path / "state")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 500, "mgr-x")
    sent, created = [], []
    client = make_manager_amux(sent, created, names=("mgr-x",))
    bot = FakeBot()
    message = FakeMessage(message_id=611, thread=500, text="e agora?")
    update = fake_update(message)

    try:
        run(botmod.on_text(update, fake_context(cfg, client, state, bot)))
    finally:
        run(client.aclose())

    assert message.replies == []  # no refusal line: it routed
    assert sent == [{"text": "[from Telegram @owner] e agora?"}]


# ---- POST /topic: the payload --------------------------------------------


def test_post_topic_transcribe_only_creates_a_flagged_topic(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {
                "session": PLACEHOLDER,
                "chat_id": CHAT,
                "name": "Transcrição de áudios",
                "transcribe_only": True,
                "prompt": "Áudios falados viram texto aqui; nada mais acontece.",
            },
        )

    assert status == 201
    assert payload["transcribe_only"] is True
    thread = payload["thread"]
    assert state.transcribe_only(CHAT, thread) is True
    assert state.topic_session(CHAT, thread) == PLACEHOLDER
    # never title-synced (no `topic_created` flag) and never anchored:
    assert state.topic_created(CHAT, thread) is False
    assert state.turn_message(CHAT, PLACEHOLDER) is None
    assert state.session_for_message(CHAT, 900) is None  # the echo's msg id
    # the prompt IS visible in the topic (it describes what the topic is for)
    assert bot.sent == [
        {
            "chat_id": CHAT,
            "text": "Áudios falados viram texto aqui; nada mais acontece.",
            "reply_to": None,
            "thread": thread,
        }
    ]
    assert any("topic_transcribe_only" in m for m in caplog.messages)
    # persisted across a restart (this is what the gateway re-reads)
    assert GatewayState(tmp_path / "state.json").transcribe_only(CHAT, thread) is True


def test_post_topic_rejects_a_non_boolean_transcribe_only(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    app = make_app(state)

    status, payload = post(
        app, {"session": PLACEHOLDER, "chat_id": CHAT, "transcribe_only": "yes"}
    )

    assert status == 400
    assert payload == {"error": "transcribe_only must be a boolean"}
    assert state.transcribe_only(CHAT, 1001) is False


def test_transcribe_only_is_reapplied_on_reconnect_without_landing(
    tmp_path, caplog
):
    """The reconnect/intro flow must not drop the flag — and must not land
    context (prompt or intro) into a topic that routes nothing. The probe
    still runs, so a deleted topic is still detected and recreated."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 191, PLACEHOLDER, "Transcrição de áudios")
    bot = FakeBot()
    app = make_app(state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        status, payload = post(
            app,
            {
                "session": PLACEHOLDER,
                "chat_id": CHAT,
                "reconnect": True,
                "intro": True,
                "prompt": "do not land me",
                "transcribe_only": True,
            },
        )

    assert status == 200
    assert payload["transcribe_only"] is True
    assert state.transcribe_only(CHAT, 191) is True
    assert state.topic_created(CHAT, 191) is False  # sync flag spent
    assert bot.sent == []  # nothing landed
    assert bot.actions == [(CHAT, 191)]  # the dead-thread probe still ran
    assert state.turn_message(CHAT, PLACEHOLDER) is None
    assert any("topic_transcribe_only" in m for m in caplog.messages)
