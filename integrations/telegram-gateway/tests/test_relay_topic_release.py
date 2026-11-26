"""A relayed answer whose topic the owner deleted must heal itself.

Measured live 2026-11-25 12:09–12:13: the owner deleted the GM's topic 544;
every relayed answer answered ``Message thread not found``
(``relay_send_failed`` ×N) and the GM's reply sat in limbo until a human
called ``POST /message``, whose release path is the only one that freed the
binding. The control routes healed; the relay did not.

The contract proven here:

1. a send into a deleted thread RELEASES the (chat, session) binding and logs
   ``topic_released … reason=thread_missing source=relay`` (the sweep signal);
2. the SAME answer is then delivered ONCE into General (``thread=None``, no
   anchor — the anchor lived in the deleted topic), not retried forever;
3. the dedupe advances only on the successful delivery, so the next cycle
   sends nothing;
4. a NON-thread failure (rights) releases nothing and still retries — there is
   no regression in the failure path;
5. ``POST /topic`` / ``POST /message`` keep their own release behavior
   (covered by ``test_control.py``; the shared predicate is asserted here).
"""
import asyncio
import logging

import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.relay import SENTINEL, thread_missing
from telegram_gateway.state import GatewayState

from test_manager_mode import FakeBot
from test_relay_api import (
    PIN,
    FakeOcService,
    app_for,
    assistant,
    pane_client,
    run,
)

CHAT = -1003797327543  # the owner's group, from the live log line
SESSION = "general-mgr-823440998"
# The pane path resolves the session name against amux; `pane_client` serves
# this one.
PANE_SESSION = "mgr-x"
DEAD = 544  # the deleted topic from the incident
ANSWER = f"{SENTINEL}\nthe answer the owner was waiting for"


class DeadThreadBot(FakeBot):
    """Sends into ``dead`` threads answer the measured 'thread not found'."""

    def __init__(self, dead=(DEAD,), error="Message thread not found"):
        super().__init__()
        self.dead = set(dead)
        self.error = error

    async def send_message(self, chat_id, text, **kw):
        if kw.get("message_thread_id") in self.dead:
            from telegram.error import TelegramError

            raise TelegramError(self.error)
        return await super().send_message(chat_id, text, **kw)


@pytest.fixture(autouse=True)
def _clean_relay_state():
    for leaked in (
        botmod._no_boundary_warned,
        botmod._no_boundary_cycles,
        botmod._api_source,
        botmod._api_unavailable,
        botmod._api_window_missed,
        botmod._turn_notify,
        botmod._answer_msg_notified,
    ):
        leaked.clear()
    yield
    for leaked in (
        botmod._no_boundary_warned,
        botmod._no_boundary_cycles,
        botmod._api_source,
        botmod._api_unavailable,
        botmod._api_window_missed,
        botmod._turn_notify,
        botmod._answer_msg_notified,
    ):
        leaked.clear()


def relay_logs(caplog):
    return [r.getMessage() for r in caplog.records if r.name == "telegram_gateway"]


def test_the_shared_predicate_names_only_the_provable_death():
    """One detector for `/topic`, `/message` and the relay (they had drifted)."""
    assert thread_missing(Exception("Bad Request: Message thread not found"))
    assert not thread_missing(Exception("Not enough rights to send messages"))
    assert not thread_missing(Exception("Flood control exceeded"))


def test_api_relay_releases_a_deleted_topic_and_delivers_in_general(
    tmp_path, caplog
):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, DEAD, SESSION)
    state.set_turn_message(CHAT, SESSION, 55, thread=DEAD)  # anchor in the topic
    state.set_pinned(CHAT, SESSION, PIN)
    oc = FakeOcService([assistant("msg_1", ANSWER)])
    bot = DeadThreadBot()
    client, handler = pane_client("")

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    # (a) the binding is GONE, and the release is grepable
    assert state.session_topic(CHAT, SESSION) is None
    releases = [m for m in relay_logs(caplog) if "topic_released" in m]
    assert len(releases) == 1, releases
    assert "reason=thread_missing" in releases[0]
    assert "source=relay" in releases[0]
    assert "path=api" in releases[0]
    assert f"thread={DEAD}" in releases[0]

    # (b) the SAME answer landed ONCE, in General, with no stale anchor
    assert bot.sent == [
        {"chat_id": CHAT, "text": ANSWER, "reply_to": None, "thread": None}
    ]
    assert any("relay_sent_without_thread" in m for m in relay_logs(caplog))
    # the pane never ran: the API path healed
    assert not handler.requested

    # (c) the message id advanced, so the next cycle sends nothing twice
    assert state.oc_relayed_id(CHAT, SESSION) == "msg_1"
    assert len(bot.sent) == 1


def test_api_relay_still_fails_without_releasing_on_a_rights_error(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, DEAD, SESSION)
    state.set_turn_message(CHAT, SESSION, 55, thread=DEAD)
    state.set_pinned(CHAT, SESSION, PIN)
    oc = FakeOcService([assistant("msg_1", ANSWER)])
    bot = DeadThreadBot(error="Not enough rights to send messages")
    client, _ = pane_client("")

    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert state.session_topic(CHAT, SESSION) == DEAD  # nothing proven, nothing freed
    assert bot.sent == []
    assert any(
        "relay_send_failed" in m and "path=api" in m for m in relay_logs(caplog)
    )
    assert not any("topic_released" in m for m in relay_logs(caplog))
    assert state.oc_relayed_id(CHAT, SESSION) is None  # stays pending


def test_pane_relay_releases_a_deleted_topic_and_delivers_in_general(
    tmp_path, caplog
):
    """The pane path had the same defect (no pin/API: `relay_once` peeks)."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, DEAD, PANE_SESSION)
    state.set_turn_message(CHAT, PANE_SESSION, 55, thread=DEAD)
    oc = FakeOcService([])
    bot = DeadThreadBot()
    client, handler = pane_client(f"[from Telegram @owner] task\n{ANSWER}\n")

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert handler.requested, "the unpinned session must use the pane path"
    assert state.session_topic(CHAT, PANE_SESSION) is None
    releases = [m for m in relay_logs(caplog) if "topic_released" in m]
    assert len(releases) == 1 and "source=relay" in releases[0]
    assert "path=pane" in releases[0]
    assert bot.sent == [
        {"chat_id": CHAT, "text": "the answer the owner was waiting for",
         "reply_to": None, "thread": None}
    ]


def test_the_retry_is_tried_once_and_the_message_stays_pending(tmp_path, caplog):
    """If even General refuses, one WARN — and the answer is retried next cycle."""
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, DEAD, SESSION)
    state.set_turn_message(CHAT, SESSION, 55, thread=DEAD)
    state.set_pinned(CHAT, SESSION, PIN)
    oc = FakeOcService([assistant("msg_1", ANSWER)])

    class RefuseAll(DeadThreadBot):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def send_message(self, chat_id, text, **kw):
            self.calls += 1
            from telegram.error import TelegramError

            raise TelegramError("Message thread not found")

    bot = RefuseAll()
    client, _ = pane_client("")

    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert bot.calls == 2, "one attempt in the topic, ONE retry in General"
    assert state.session_topic(CHAT, SESSION) is None  # released before the retry
    warns = [m for m in relay_logs(caplog) if "relay_send_failed" in m]
    assert len(warns) == 1 and "retried_without_thread=true" in warns[0]
    assert state.oc_relayed_id(CHAT, SESSION) is None  # pending, not skipped
