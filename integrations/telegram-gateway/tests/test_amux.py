import asyncio
import json

import httpx
import pytest

from telegram_gateway.amux import AmuxClient, AmuxError


def run(coro):
    return asyncio.run(coro)


def make_client(handler, base_url="https://localhost:8824"):
    transport = httpx.MockTransport(handler)
    return AmuxClient(base_url, transport=transport)


def lane(name="w", status="idle"):
    # Fleet lanes carry the activity/agent_state fields; worker rows do not.
    return {"name": name, "activity": "none", "agent_state": status, "status": status, "running": True}


def worker_row(name="w", status="idle"):
    return {"name": name, "lifecycle": "active", "status": status, "running": True}


def test_list_sessions_parses_the_bare_array():
    def handler(request):
        assert request.url.path == "/api/sessions"
        return httpx.Response(200, json=[lane("a"), worker_row("b")])

    client = make_client(handler)
    try:
        rows = run(client.list_sessions())
    finally:
        run(client.aclose())
    assert [row["name"] for row in rows] == ["a", "b"]


def test_find_is_404_for_an_unknown_name():
    def handler(request):
        return httpx.Response(200, json=[lane("a")])

    client = make_client(handler)
    try:
        with pytest.raises(AmuxError) as excinfo:
            run(client.find("missing"))
    finally:
        run(client.aclose())
    assert excinfo.value.status == 404


def test_create_session_posts_the_lane_contract():
    captured = {}

    def handler(request):
        captured.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(201, json={"ok": True, "name": "tg-x", "starting": True})

    client = make_client(handler)
    try:
        run(client.create_session("tg-x", cwd="/tmp", provider="opencode"))
    finally:
        run(client.aclose())
    assert captured == {
        "path": "/api/sessions",
        "body": {"name": "tg-x", "dir": "/tmp", "provider": "opencode", "start": True},
    }


def test_send_routes_a_lane_to_sessions():
    captured = {}

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=[lane("w")])
        captured.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    try:
        run(client.send("w", "[from Telegram @x] hi"))
    finally:
        run(client.aclose())
    assert captured["path"] == "/api/sessions/w/send"
    assert captured["body"] == {"text": "[from Telegram @x] hi"}


def test_send_routes_a_worker_row_to_workers():
    captured = {}

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=[worker_row("w")])
        captured.update(path=request.url.path)
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    try:
        run(client.send("w", "hi"))
    finally:
        run(client.aclose())
    assert captured["path"] == "/api/workers/w/send"


def test_stop_and_start_follow_the_substrate():
    seen = []

    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=[lane("a"), worker_row("b")])
        seen.append(request.url.path)
        return httpx.Response(200, json={"applied": True})

    client = make_client(handler)
    try:
        run(client.start("a"))
        run(client.stop("b"))
    finally:
        run(client.aclose())
    assert seen == ["/api/sessions/a/start", "/api/workers/b/stop"]


def test_peek_returns_output():
    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=[lane("w")])
        assert request.url.params["lines"] == "50"
        return httpx.Response(200, json={"output": "hello\n"})

    client = make_client(handler)
    try:
        assert run(client.peek("w", lines=50)) == "hello\n"
    finally:
        run(client.aclose())


def test_error_body_text_becomes_the_detail():
    def handler(request):
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json=[lane("w")])
        return httpx.Response(404, json={"error": "session 'w' not found"})

    client = make_client(handler)
    try:
        with pytest.raises(AmuxError) as excinfo:
            run(client.send("w", "hi"))
    finally:
        run(client.aclose())
    assert excinfo.value.status == 404
    assert excinfo.value.detail == "session 'w' not found"


def test_unreachable_server_is_an_amux_error():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = make_client(handler)
    try:
        with pytest.raises(AmuxError) as excinfo:
            run(client.health())
    finally:
        run(client.aclose())
    assert excinfo.value.status == 0
    assert "unreachable" in excinfo.value.detail


def test_wait_until_started_polls_until_the_spawn_settles():
    states = iter(
        [
            [lane("w", status="starting")],
            [lane("w", status="starting")],
            [lane("w", status="idle")],
        ]
    )

    def handler(request):
        return httpx.Response(200, json=next(states))

    client = make_client(handler)
    try:
        row = run(client.wait_until_started("w", timeout=5, interval=0.01))
    finally:
        run(client.aclose())
    assert row["status"] == "idle"


def test_todo_cards_keeps_only_open_cards():
    def handler(request):
        assert request.url.params["session"] == "m2acp2"
        return httpx.Response(
            200,
            json=[
                {"id": "AMUX-1", "title": "open", "status": "todo"},
                {"id": "AMUX-2", "title": "active", "status": "doing"},
                {"id": "AMUX-3", "title": "done", "status": "done"},
            ],
        )

    client = make_client(handler)
    try:
        cards = run(client.todo_cards("m2acp2"))
    finally:
        run(client.aclose())
    assert [card["id"] for card in cards] == ["AMUX-1", "AMUX-2"]


def test_todo_cards_read_failure_is_not_an_error():
    def handler(request):
        return httpx.Response(500, json={"error": "store wedged"})

    client = make_client(handler)
    try:
        assert run(client.todo_cards("w")) == []
    finally:
        run(client.aclose())
