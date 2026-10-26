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


def test_list_workers_parses_items():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"items": [{"name": "a"}], "total": 1})

    client = make_client(handler)
    try:
        assert run(client.list_workers()) == [{"name": "a"}]
    finally:
        run(client.aclose())
    assert (seen["method"], seen["path"]) == ("GET", "/api/workers")


def test_create_worker_sends_the_body():
    captured = {}

    def handler(request):
        captured.update(
            method=request.method,
            path=request.url.path,
            body=json.loads(request.content),
        )
        return httpx.Response(201, json={"id": "wrk_1", "name": "tg-x"})

    client = make_client(handler)
    try:
        row = run(client.create_worker("tg-x", cwd="/tmp", provider="claude"))
    finally:
        run(client.aclose())
    assert row["id"] == "wrk_1"
    assert captured == {
        "method": "POST",
        "path": "/api/workers",
        "body": {"display_name": "tg-x", "cwd": "/tmp", "provider": "claude"},
    }


def test_send_posts_text_to_the_worker_route():
    captured = {}

    def handler(request):
        captured.update(
            raw_path=request.url.raw_path.decode(), body=json.loads(request.content)
        )
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    try:
        assert run(client.send("my worker", "[from Telegram @x] hi")) == {"ok": True}
    finally:
        run(client.aclose())
    assert captured["raw_path"] == "/api/workers/my%20worker/send"
    assert captured["body"] == {"text": "[from Telegram @x] hi"}


def test_peek_returns_output():
    def handler(request):
        assert request.url.params["lines"] == "50"
        return httpx.Response(200, json={"output": "hello\n"})

    client = make_client(handler)
    try:
        assert run(client.peek("w", lines=50)) == "hello\n"
    finally:
        run(client.aclose())


def test_error_body_text_becomes_the_detail():
    def handler(request):
        return httpx.Response(404, json={"error": "session 'x' not found"})

    client = make_client(handler)
    try:
        with pytest.raises(AmuxError) as excinfo:
            run(client.send("x", "hi"))
    finally:
        run(client.aclose())
    assert excinfo.value.status == 404
    assert excinfo.value.detail == "session 'x' not found"


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


def test_start_and_stop_hit_the_right_routes():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"applied": True})

    client = make_client(handler)
    try:
        run(client.start_worker("w"))
        run(client.stop_worker("w"))
    finally:
        run(client.aclose())
    assert paths == ["/api/workers/w/start", "/api/workers/w/stop"]
