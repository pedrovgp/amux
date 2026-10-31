"""Dedup + General → the dispatcher (Milestone 7 contract → 11.5).

Old contract (M7): each forum-General message started a fresh manager AND its
own topic by design, so a burst (the 2026-10-27 09:29 incident: 3 messages in
7 s) opened 3 topics for one intent — dedup merged a repeat into the session
the first copy started.

New contract (M11.5, owner 2026-10-30): forum General mints NOTHING. Every
copy routes to the sender's own `general-mgr-<uid>` — so the burst record's
surviving job is visibility: a repeat inside ``DUP_WINDOW_S`` logs
``duplicate_general_merged`` as a merge into the SAME dispatcher instead of a
second routing, and different text (or an expired window) simply routes
normally. A dispatcher found STOPPED is revived before its prompt is
delivered — spawn-on-demand — and is never replaced by a second session; a
dispatcher that cannot be created answers honestly instead of silently.
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
DISPATCHER = "general-mgr-1"  # fake_update's sender is user id 1


def amux(sent, created, statuses=None, starts=None):
    """A client that records ``(session, prompt)`` and can mark lanes dead.

    ``statuses`` maps session name -> status (a ``stopped`` status also flips
    the row's ``running`` to False — the fact a revive keys on), and
    ``starts`` records every ``…/start`` call by session name.
    """
    statuses = statuses if statuses is not None else {}
    starts = starts if starts is not None else []

    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/api/sessions":
            created.append(json.loads(request.content)["name"])
            return httpx.Response(201, json={"ok": True})
        if request.method == "GET" and path == "/api/sessions":
            rows = [
                {
                    "name": name,
                    "activity": "none",
                    "status": statuses.get(name, "idle"),
                    "running": statuses.get(name, "idle") != "stopped",
                }
                for name in created
            ]
            return httpx.Response(200, json=rows)
        if request.method == "POST" and path.endswith("/send"):
            sent.append((path.split("/")[-2], json.loads(request.content)["text"]))
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path.endswith("/start"):
            starts.append(path.split("/")[-2])
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={})

    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler))


def seeded_amux(sent, created, statuses=None, starts=None):
    """The fleet as a running gateway finds it: the dispatcher already exists."""
    created.append(DISPATCHER)
    return amux(sent, created, statuses, starts)


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


def test_identical_general_messages_all_reach_the_dispatcher(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = seeded_amux(sent, created)
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

    # General minted NOTHING: no lane, no topic, no ack — and both copies of
    # the burst went to the one dispatcher.
    assert created == [DISPATCHER]
    assert bot.created == []
    assert first.replies == [] and second.replies == []
    assert sent == [
        (DISPATCHER, f"[from Telegram @owner] {TEXT}"),
        (DISPATCHER, f"[from Telegram @owner] {TEXT}"),
    ]

    # the signal a sweep can catch, exactly once: a MERGE, not a routing
    merged = [
        r for r in caplog.records if r.getMessage().startswith("duplicate_general_merged")
    ]
    assert len(merged) == 1
    assert f"session={DISPATCHER}" in merged[0].getMessage()
    assert "msg=101" in merged[0].getMessage()
    assert f"chat_id={CHAT}" in merged[0].getMessage()

    # the merged turn re-anchors on the duplicate, so its answer replies there
    assert state.turn_message(CHAT, DISPATCHER) == 101


def test_different_text_goes_to_the_same_dispatcher_without_a_merge_log(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = seeded_amux(sent, created)
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

    # exact-match dedup: different text is two independent turns of ONE
    # dispatcher — never a second session or topic (M7's contract is gone)
    assert created == [DISPATCHER]
    assert bot.created == []
    assert [s for s, _ in sent] == [DISPATCHER, DISPATCHER]
    assert all(s == DISPATCHER for s, _ in sent)


def test_a_repeat_outside_the_window_is_not_logged_as_a_merge(
    tmp_path, monkeypatch, caplog
):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = seeded_amux(sent, created)
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

    assert created == [DISPATCHER]  # still nothing minted
    assert bot.created == []
    assert "duplicate_general_merged" not in caplog.text
    assert len(sent) == 2


def test_a_stopped_dispatcher_is_revived_not_replaced(tmp_path, caplog):
    """Spawn-on-demand: a dispatcher that is down gets STARTED again —
    its conversation is a session that never went away — and the fleet never
    grows a second session for the same owner."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created, statuses, starts = [], [], {}, []
    client = seeded_amux(sent, created, statuses, starts)
    bot = FakeBot()
    context = fake_context(cfg, client, state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(FakeMessage(100, text=TEXT, thread=None)), context))
            statuses[DISPATCHER] = "stopped"  # the dispatcher dies mid-day
            run(botmod.on_text(fake_update(FakeMessage(101, text=TEXT, thread=None)), context))
        finally:
            run(client.aclose())

    assert created == [DISPATCHER]  # NEVER replaced
    assert bot.created == []
    assert starts == [DISPATCHER]  # revived, not re-created
    assert any(
        r.getMessage().startswith("dispatcher_revived") for r in caplog.records
    )
    assert sent and sent[-1][0] == DISPATCHER  # the prompt landed after revival
    # the stale record was dropped (the session was dead), so no merge claim
    assert "duplicate_general_merged" not in caplog.text


def test_a_dispatcher_that_cannot_be_created_is_reported_not_silent(
    tmp_path, caplog
):
    """No dispatcher and no way to make one (amux refuses): the owner gets an
    honest line, and NOTHING is delivered as if it had been."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    client = failing_amux()
    context = fake_context(cfg, client, state, bot)
    first = FakeMessage(message_id=100, text=TEXT, thread=None)
    second = FakeMessage(message_id=101, text=TEXT, thread=None)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        try:
            run(botmod.on_text(fake_update(first), context))
            run(botmod.on_text(fake_update(second), context))
        finally:
            run(client.aclose())

    # no prompt was ever "delivered"
    assert not [
        r for r in caplog.records if r.getMessage().startswith("prompt_text")
    ]
    # the failure is named for a sweep, once per attempt …
    warns = [
        r
        for r in caplog.records
        if "general_mgr_ensure_failed" in r.getMessage()
        and "source=first_message" in r.getMessage()
    ]
    assert len(warns) == 2
    # … and BOTH attempts answered the owner instead of vanishing
    for message in (first, second):
        assert message.replies
        assert "unavailable right now" in message.replies[0]["text"]
