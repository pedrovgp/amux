"""Dedup: a burst of identical General messages is ONE session, not N.

The owner's client can hand the gateway the same sentence twice within a few
seconds (the 2026-10-27 09:29 incident: 3 messages in 7 s). Each General
message starts a fresh manager and its own topic by design, so an unguarded
burst opened 3 topics for one intent.

The contract asserted here:

1. identical text inside ``DUP_WINDOW_S`` (30 s) -> the second message is
   delivered to the session the first one started, and logs
   ``duplicate_general_merged`` — no second topic, no second lane;
2. different text -> still a new session/topic (dedup is exact-match);
3. outside the window -> new session/topic;
4. a session that failed to start or has been stopped -> falls through to the
   normal new-session path instead of vanishing into a dead lane.
"""
import json
import logging

import httpx

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.state import GatewayState

from test_topics import (
    CHAT,
    FakeBot,
    FakeMessage,
    fake_context,
    fake_update,
    make_cfg,
    run,
)

TEXT = "Create a topic and answer me there any answer will do"


def amux(sent, created, statuses=None):
    """A client that records ``(session, prompt)`` and can mark lanes dead.

    ``statuses`` maps session name -> status, so a test can stop the session
    the first message started before the duplicate arrives.
    """
    statuses = statuses if statuses is not None else {}

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/api/sessions":
            if request.url.params.get("force") == "fail":
                return httpx.Response(500, json={"error": "boom"})
            created.append(json.loads(request.content)["name"])
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and path == "/api/sessions":
            rows = [
                {"name": name, "activity": "none", "status": statuses.get(name, "idle")}
                for name in created
            ]
            return httpx.Response(200, json=rows)
        if request.method == "POST" and path.endswith("/send"):
            sent.append((path.split("/")[-2], json.loads(request.content)["text"]))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


def failing_amux():
    """A client whose lane creation always fails (create -> HTTP 500)."""

    def handler(request):
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(500, json={"error": "boom"})
        if request.method == "GET" and request.url.path == "/api/sessions":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"ok": True})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


# ---- the four contracts ---------------------------------------------------


def test_identical_general_messages_open_one_topic_and_one_session(
    tmp_path, caplog
):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = amux(sent, created)
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    first = FakeMessage(message_id=100, text=TEXT, thread=None)
    second = FakeMessage(message_id=101, text=TEXT, thread=None)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(first), context))
            run(botmod.on_text(fake_update(second), context))
        finally:
            run(client.aclose())

    # one topic, one lane, two prompts — both to the same session
    assert len(bot.created) == 1
    assert len(created) == 1
    session = created[0]
    assert sent == [
        (session, f"[from Telegram @owner] {TEXT}"),
        (session, f"[from Telegram @owner] {TEXT}"),
    ]
    assert state.topic_session(CHAT, 1001) == session

    # the signal a sweep can catch, exactly once
    merged = [
        r for r in caplog.records if r.getMessage().startswith("duplicate_general_merged")
    ]
    assert len(merged) == 1
    assert f"session={session}" in merged[0].getMessage()
    assert "msg=101" in merged[0].getMessage()
    assert f"chat_id={CHAT}" in merged[0].getMessage()

    # only the first message got the "Starting" ack; the merged one is silent
    assert len(first.replies) == 1
    assert second.replies == []

    # the merged turn anchors on the duplicate, so its answer is relayed too
    assert state.turn_message(CHAT, session) == 101


def test_different_text_still_starts_a_new_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = amux(sent, created)
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    try:
        run(botmod.on_text(fake_update(FakeMessage(100, text=TEXT, thread=None)), context))
        run(
            botmod.on_text(
                fake_update(FakeMessage(101, text="something else entirely", thread=None)),
                context,
            )
        )
    finally:
        run(client.aclose())

    assert len(created) == 2
    assert len(bot.created) == 2  # a topic each
    assert [s for s, _ in sent] == created


def test_identical_message_past_the_window_starts_a_new_session(
    tmp_path, monkeypatch, caplog
):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = amux(sent, created)
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    clock = {"now": 1_000.0}
    monkeypatch.setattr(botmod, "_now", lambda: clock["now"])

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(FakeMessage(100, text=TEXT, thread=None)), context))
            clock["now"] += botmod.DUP_WINDOW_S + 1
            run(botmod.on_text(fake_update(FakeMessage(101, text=TEXT, thread=None)), context))
        finally:
            run(client.aclose())

    assert len(created) == 2
    assert len(bot.created) == 2
    assert "duplicate_general_merged" not in caplog.text


def test_stopped_session_falls_through_to_a_new_one(tmp_path, caplog):
    """A dead lane must not swallow the repeat: the burst gets a fresh session."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created, statuses = [], [], {}
    client = amux(sent, created, statuses)
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(FakeMessage(100, text=TEXT, thread=None)), context))
            statuses[created[0]] = "stopped"  # the first session dies
            run(botmod.on_text(fake_update(FakeMessage(101, text=TEXT, thread=None)), context))
        finally:
            run(client.aclose())

    assert len(created) == 2
    assert len(bot.created) == 2
    assert "duplicate_general_merged" not in caplog.text
    # the second prompt went to the NEW session, not the stopped one
    assert sent[-1][0] == created[1]


def test_failed_start_is_not_recorded_as_a_merge_target(tmp_path, caplog):
    """A manager that never came up cannot be a merge target."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    client = failing_amux()
    context = fake_context(cfg, client, state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(FakeMessage(100, text=TEXT, thread=None)), context))
            run(botmod.on_text(fake_update(FakeMessage(101, text=TEXT, thread=None)), context))
        finally:
            run(client.aclose())

    assert len(bot.created) == 2  # both attempts opened (and released) a topic
    assert "duplicate_general_merged" not in caplog.text
    assert "manager_create_failed" in caplog.text
