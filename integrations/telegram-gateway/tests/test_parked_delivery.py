"""A PARKED prompt is loud (the 2026-11-07 stranded-delivery incident).

The contract under test, gateway side:

1. amux answers a send it could not SUBMIT with either a 409 body
   (``composer contains a draft; durable message remains queued``) or a 200
   body (``queued (steering) …`` / ``submission: deferred``). Both are holds,
   not refusals — and before this the gateway said NOTHING about either:
   - the 409 was retried once and then reported as ``❌ amux refused`` (a
     message amux was in fact keeping), and
   - the 200 was swallowed by ``_deliver_prompt``'s bare ``return``.
2. Now every park logs ``send_parked … session=… reason=…`` at WARN and posts
   ONE line into the session's topic — the owner's words for the draft case —
   once per stretch (the next CONFIRMED delivery closes it).
3. The caller never claims ``✅ Sent`` for text amux is still holding.

The draft SELF-HEAL itself lives in the amux repo
(``submit_own_steering_draft`` / ``clear_delivered_duplicate_draft``); this
file pins only what the gateway must say while the hold lasts.
"""
import logging

import httpx
import pytest
from telegram.error import TelegramError

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.state import GatewayState

from test_topics import CHAT, FakeBot, FakeMessage, fake_context, fake_update, make_cfg, run

SESSION = "mgr-x"
THREAD = 42


@pytest.fixture(autouse=True)
def _clean_parked_dedupe():
    """`_parked_warned` is module state; a leaked stretch hides the next test."""
    botmod._parked_warned.clear()
    yield
    botmod._parked_warned.clear()


class SendStub:
    """A transport whose /send answer the test swaps between calls."""

    def __init__(self, rows=(), send=None):
        self.rows = [dict(row) for row in rows]
        self.send = send or (lambda request: httpx.Response(200, json={"ok": True}))
        self.sent_bodies: list[str] = []
        self.client = AmuxClient(
            "https://localhost:8824", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/api/sessions":
            return httpx.Response(200, json=self.rows)
        if request.method == "POST" and path.endswith("/send"):
            self.sent_bodies.append(request.content.decode())
            return self.send(request)
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "message": "ok"})
        return httpx.Response(404, json={"error": "no route"})

    async def aclose(self):
        await self.client.aclose()


def draft_hold(request):
    """The measured 409: amux KEEPS the text and says so (no `error` key, so
    `AmuxError.detail` is the raw JSON body)."""
    return httpx.Response(
        409,
        json={
            "ok": False,
            "message": "composer contains a draft; durable message remains queued",
            "fix": "message remains queued; inspect and submit or deliberately "
            "discard the existing draft in the pane before delivery can resume",
        },
    )


def queued(request):
    """The measured 200: accepted onto the steering queue, NOT submitted."""
    return httpx.Response(
        200,
        json={
            "ok": True,
            "message": "queued (steering) — delivers to 'mgr-x' at its next turn "
            "boundary",
            "submission": "deferred",
            "queue_id": "q-1",
        },
    )


def confirmed(request):
    return httpx.Response(200, json={"ok": True, "message": "sent", "submission": "confirmed"})


def ctx_for(tmp_path, stub, *, topic=True):
    cfg = make_cfg(tmp_path, idle_stop_s=1800.0)
    state = GatewayState(tmp_path / "state.json")
    if topic:
        state.set_topic(CHAT, THREAD, SESSION)
    bot = FakeBot()
    return cfg, state, bot, fake_context(cfg, stub.client, state, bot)


def test_parked_reason_reads_amux_own_markers():
    assert botmod.parked_reason("composer contains a draft; durable message remains queued") == "draft"
    assert botmod.parked_reason("this message is already pasted in the composer") == "draft"
    assert botmod.parked_reason("worker is not ready; durable message remains queued") == "queued"
    assert botmod.parked_reason("queued (steering) — delivers to 'x'") == "queued"
    assert botmod.parked_reason("sent", "deferred") == "queued"
    # a real delivery is never parked, whatever else the body carries
    assert botmod.parked_reason("sent", "confirmed") is None
    assert botmod.parked_reason("session 'x' not found") is None
    assert botmod.parked_reason(None) is None


def test_a_draft_hold_warns_and_pages_the_topic_once_per_stretch(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    stub = SendStub(
        rows=[{"name": SESSION, "activity": "x", "status": "idle"}], send=draft_hold
    )
    _cfg, state, bot, ctx = ctx_for(tmp_path, stub)

    reason = run(botmod._deliver_prompt(ctx, CHAT, SESSION, "So, what did you find out?", "owner"))
    assert reason == "draft", "a hold must not be reported as a refusal"

    # the WARN names the session — the sweep's grep
    assert "send_parked" in caplog.text
    assert f"session={SESSION}" in caplog.text
    assert "reason=draft" in caplog.text
    # the owner hears it IN THE TOPIC, in the words they were given
    assert len(bot.sent) == 1
    assert bot.sent[0]["thread"] == THREAD
    assert bot.sent[0]["chat_id"] == CHAT
    assert "parked behind a draft in the worker's input" in bot.sent[0]["text"]
    assert "amux will submit it when idle" in bot.sent[0]["text"]
    assert "parked_notice_sent" in caplog.text

    # a second park inside the same stretch: WARN again, no second page
    run(botmod._deliver_prompt(ctx, CHAT, SESSION, "still there?", "owner"))
    assert caplog.text.count("send_parked") == 2
    assert len(bot.sent) == 1, "one line per stretch, not per attempt"

    # a CONFIRMED delivery closes the stretch: the next park pages again
    stub.send = confirmed
    assert run(botmod._deliver_prompt(ctx, CHAT, SESSION, "ok", "owner")) is None
    assert (CHAT, SESSION) not in botmod._parked_warned
    stub.send = draft_hold
    run(botmod._deliver_prompt(ctx, CHAT, SESSION, "and now?", "owner"))
    assert len(bot.sent) == 2, "a new stretch pages again"
    run(stub.aclose())


def test_a_queued_send_is_surfaced_instead_of_swallowed(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    stub = SendStub(
        rows=[{"name": SESSION, "activity": "x", "status": "idle"}], send=queued
    )
    _cfg, state, bot, ctx = ctx_for(tmp_path, stub)

    reason = run(botmod._deliver_prompt(ctx, CHAT, SESSION, "queued prompt", "owner"))

    assert reason == "queued"
    assert "send_parked" in caplog.text and "reason=queued" in caplog.text
    assert len(bot.sent) == 1 and bot.sent[0]["thread"] == THREAD
    assert "queued" in bot.sent[0]["text"]
    run(stub.aclose())


def test_a_failed_page_does_not_consume_the_stretch(tmp_path, caplog):
    """A notice that could not be sent must be retried, never silently dropped."""
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    stub = SendStub(
        rows=[{"name": SESSION, "activity": "x", "status": "idle"}], send=draft_hold
    )
    _cfg, state, bot, ctx = ctx_for(tmp_path, stub)

    async def refuse(*args, **kwargs):
        raise TelegramError("boom")

    bot.send_message = refuse
    run(botmod._deliver_prompt(ctx, CHAT, SESSION, "hello", "owner"))
    assert "parked_notice_failed" in caplog.text
    assert (CHAT, SESSION) not in botmod._parked_warned
    run(stub.aclose())


def test_receive_prompt_never_claims_sent_for_a_parked_message(tmp_path, caplog):
    """`✅ Sent` for text amux is still holding is the false belief the
    incident cost hours to correct — the caller must stay silent and let the
    topic notice speak."""
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    stub = SendStub(
        rows=[{"name": SESSION, "activity": "x", "status": "idle"}], send=draft_hold
    )
    cfg, state, bot, ctx = ctx_for(tmp_path, stub)
    state.select(CHAT, SESSION)
    message = FakeMessage(message_id=300, text="So, what did you find out?", thread=THREAD)
    update = fake_update(message)

    outcome = run(botmod.receive_prompt(update, ctx))

    assert outcome == botmod.ConversationHandler.END
    assert message.replies == [], f"no ack may claim delivery: {message.replies}"
    assert not any("✅ Sent" in str(reply) for reply in message.replies)
    assert len(bot.sent) == 1 and "parked behind a draft" in bot.sent[0]["text"]
    run(stub.aclose())


def test_a_confirmed_delivery_still_gets_the_sent_ack(tmp_path):
    """The control for the test above: a real send keeps its ack."""
    stub = SendStub(
        rows=[{"name": SESSION, "activity": "x", "status": "idle"}], send=confirmed
    )
    cfg, state, bot, ctx = ctx_for(tmp_path, stub)
    state.select(CHAT, SESSION)
    message = FakeMessage(message_id=301, text="go", thread=THREAD)
    update = fake_update(message)

    outcome = run(botmod.receive_prompt(update, ctx))

    assert outcome == botmod.ConversationHandler.END
    assert message.replies and "✅ Sent" in message.replies[0]["text"]
    assert bot.sent == [], "a delivered message needs no parked notice"
    run(stub.aclose())
