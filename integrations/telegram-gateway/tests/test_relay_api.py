"""Task 1 (owner, 2026-10-28): the relay reads answers from OpenCode's API.

The failure being fixed: the pane scrape only peeked `lines=300`, a long answer
pushed the `—— answer ——` sentinel out of that window, and the relay went
quiet (`relay_no_boundary` fired 60 times). The contract under test:

1. **API path** — a PINNED opencode session's answer is read from
   `GET /api/session/<id>/message` (newest first, cursor-paged until the
   relayed id), keyed on OpenCode's own message id, and delivered with the
   same anchor rules as the pane (into the topic, never across threads);
2. **fallback** — any service failure logs `relay_api_unavailable` ONCE per
   stretch and runs today's pane scrape, so a dead service degrades instead of
   going silent; an UNPINNED session (legacy, non-opencode) never touches the
   service at all;
3. **pane path stays honest** — the widened configurable peek
   (`TELEGRAM_GATEWAY_PEEK_LINES`, default 2000) still finds a sentinel that
   sits far beyond the old 300-line window, and a repeat miss logs
   `relay_no_boundary cycles=N` with a recovery line when it ends.

Nothing here touches the network: discovery/auth are injected (`FakeOcService`
hands in a MockTransport-backed client) and the pane is a MockTransport too.
"""
import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.config import Config
from telegram_gateway.opencode_api import OcError, OpencodeClient
from telegram_gateway.relay import plan_opencode_relay
from telegram_gateway.state import GatewayState

from test_manager_mode import FakeBot

CHAT = 9
PIN = "ses_tg10280900abcdef"
PANE_ANSWER = "[from Telegram @owner] task\nthe answer from the pane\n"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_relay_dedupe():
    """The one-WARN-per-stretch sets are module state; every test starts bare."""
    for leaked in (
        botmod._no_boundary_warned,
        botmod._no_boundary_cycles,
        botmod._api_source,
        botmod._api_unavailable,
        botmod._api_window_missed,
    ):
        leaked.clear()
    yield
    for leaked in (
        botmod._no_boundary_warned,
        botmod._no_boundary_cycles,
        botmod._api_source,
        botmod._api_unavailable,
        botmod._api_window_missed,
    ):
        leaked.clear()


def cfg(**kw) -> Config:
    defaults = dict(
        bot_token="1:test-token",
        allowed_user_ids=frozenset({1}),
        manager_cwd="/w",
    )
    defaults.update(kw)
    return Config(**defaults)


def assistant(msg_id, text=None, *, completed=True, reasoning=None):
    """One assistant row shaped like the live serve API's message objects."""
    content = []
    if reasoning:
        content.append({"type": "reasoning", "text": reasoning})
    if text is not None:
        content.append({"type": "text", "text": text})
    times = {"created": 1, "streamed": 2}
    if completed:
        times["completed"] = 3
    return {"id": msg_id, "type": "assistant", "time": times, "content": content}


def history_handler(history, *, status=200, error=None):
    """`GET /api/session/<id>/message`: newest-first pages behind a cursor.

    ``cursor`` carries the index of the next OLDER page — the same
    newest-first / ``cursor.next`` contract the live service answers with.
    ``error`` is raised as a transport failure (a refused connection).
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if error is not None:
            raise error
        if status >= 400:
            return httpx.Response(status, json={"error": "nope"})
        assert request.url.path.endswith("/message"), request.url.path
        start = int(request.url.params.get("cursor") or 0)
        limit = int(request.url.params.get("limit") or 100)
        seen.append(start)
        page = history[start : start + limit]
        cursor = {"next": str(start + limit)} if start + limit < len(history) else {}
        return httpx.Response(200, json={"data": page, "cursor": cursor})

    handler.seen = seen
    return handler


class FakeOcService:
    """The injected ``bot_data["oc_api"]``: one MockTransport-backed reader.

    Three failure shapes, because the relay must degrade on ALL of them:
    ``discovery_error`` (nothing listening), ``status`` (401/404) and
    ``request_error`` (refused/timeout at the socket).
    """

    def __init__(self, history=(), *, status=200, discovery_error=None, request_error=None):
        self.history = list(history)
        self.calls = 0
        self.handler = history_handler(self.history, status=status, error=request_error)
        self.discovery_error = discovery_error

    async def client_for_service(self, *, url=None, password=None, service_json=None):
        self.calls += 1
        if self.discovery_error is not None:
            raise self.discovery_error
        return OpencodeClient(
            url or "http://127.0.0.1:46373",
            password or "pw",
            transport=httpx.MockTransport(self.handler),
        )


def pane_client(output: str | None = None, *, buffer=None):
    """An amux client whose peek serves ``output`` (or the last N of ``buffer``)."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sessions":
            return httpx.Response(
                200, json=[{"name": "mgr-x", "activity": "x", "status": "idle"}]
            )
        lines = int(request.url.params.get("lines") or 0)
        requested.append(lines)
        if buffer is not None:
            return httpx.Response(200, json={"output": "\n".join(buffer[-lines:])})
        return httpx.Response(200, json={"output": output or ""})

    handler.requested = requested
    return AmuxClient("https://localhost:8824", transport=httpx.MockTransport(handler)), handler


def app_for(state, bot, oc, client, config=None):
    return SimpleNamespace(
        bot=bot,
        bot_data={
            "cfg": config or cfg(),
            "client": client,
            "state": state,
            "oc_api": oc,
        },
    )


# ---- the plan: what a newest-first page still owes the chat ---------------


def test_plan_sends_newer_answers_oldest_first_and_skips_the_relayed_one():
    messages = [
        assistant("msg_3", "third"),
        assistant("msg_2", "second"),
        assistant("msg_1", "first"),
    ]
    plan = plan_opencode_relay(messages, since_id="msg_1")
    assert plan.items == [("msg_2", "second"), ("msg_3", "third")]
    assert plan.adopt_id is None and plan.reason is None


def test_plan_ignores_reasoning_and_unfinished_messages():
    messages = [
        assistant("msg_2", None, completed=False),      # still streaming
        assistant("msg_1", "the answer", reasoning="hmm"),
    ]
    plan = plan_opencode_relay(messages, since_id=None)
    assert plan.items == [("msg_1", "the answer")]  # reasoning never relayed
    # …and an unfinished newest message is not a boundary to sit on either:
    # the finished one before it is the answer to send.
    assert plan.items[0][0] == "msg_1"


def test_plan_bootstraps_the_newest_answer_when_nothing_was_relayed():
    messages = [assistant("msg_2", "new"), assistant("msg_1", "old history")]
    plan = plan_opencode_relay(messages, since_id=None)
    assert plan.items == [("msg_2", "new")]  # old history is baselined, not dumped
    assert plan.adopt_id is None


def test_plan_bootstraps_nothing_when_the_pane_already_relayed_it():
    answer = "—— answer ——\nthe answer\nBuild · MiMo · 9.3s"
    messages = [assistant("msg_9", answer)]
    plan = plan_opencode_relay(
        messages, since_id=None, pane_text="the answer\nBuild · MiMo · 9.3s"
    )
    assert plan.items == []
    assert plan.adopt_id == "msg_9"  # adopt the id, send nothing twice
    assert plan.reason == "already_relayed_by_pane"


def test_plan_sends_nothing_when_the_relayed_id_is_out_of_window():
    plan = plan_opencode_relay([assistant("msg_9", "x")], since_id="msg_1")
    assert plan.items == [] and plan.adopt_id is None
    assert plan.reason == "since_id_not_in_window"  # a flood is worse than a stall


def test_plan_on_an_empty_session_has_a_readable_reason():
    assert plan_opencode_relay([], since_id=None).reason == "no_messages"


# ---- the state: the message id is the dedupe, and it survives a rename ---


def test_oc_relayed_id_roundtrips_and_is_rekeyed_by_a_rename(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_oc_relayed_id(CHAT, "mgr-x", "msg_7")
    assert GatewayState(tmp_path / "state.json").oc_relayed_id(CHAT, "mgr-x") == "msg_7"
    assert state.oc_relayed_id(CHAT, "mgr-y") is None  # per session
    state.rename_session(CHAT, "mgr-x", "mgr-renamed")
    assert state.oc_relayed_id(CHAT, "mgr-renamed") == "msg_7"
    assert state.oc_relayed_id(CHAT, "mgr-x") is None


# ---- the API path through relay_once -------------------------------------


def test_relay_reads_the_answer_from_the_opencode_service(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService([assistant("msg_1", "—— answer ——\nthe whole long answer")])
    bot = FakeBot()
    client, handler = pane_client("no boundary in this pane at all\n")

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert oc.calls == 2
    assert oc.handler.seen == [0, 0]  # the second read stopped at since_id
    assert not handler.requested, "the pane must stay out of a cycle the API served"
    assert bot.sent == [
        {"chat_id": CHAT, "text": "—— answer ——\nthe whole long answer",
         "reply_to": 55, "thread": None}
    ]  # exactly once: the message id, not the text, is the dedupe now
    assert state.oc_relayed_id(CHAT, "mgr-x") == "msg_1"
    messages = [r.getMessage() for r in caplog.records if r.name == "telegram_gateway"]
    assert any("relay_source" in m and "source=api" in m for m in messages)
    assert not any("relay_api_unavailable" in m for m in messages)


def test_relay_sends_only_what_is_newer_than_the_relayed_id(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    history = [assistant("msg_1", "first answer")]
    oc = FakeOcService(history)
    bot = FakeBot()
    client, _ = pane_client("")

    try:
        run(botmod.relay_once(app_for(state, bot, oc, client)))  # sends msg_1
        oc.history.insert(0, assistant("msg_2", "second answer"))
        run(botmod.relay_once(app_for(state, bot, oc, client)))  # only msg_2
    finally:
        run(client.aclose())

    assert [m["text"] for m in bot.sent] == ["first answer", "second answer"]
    assert state.oc_relayed_id(CHAT, "mgr-x") == "msg_2"


def test_relay_falls_back_to_the_pane_when_the_service_is_unavailable(
    tmp_path, caplog
):
    """401/404/timeout/refused → one WARN and TODAY's pane scrape, never silence."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService([], status=401)
    bot = FakeBot()
    client, handler = pane_client(PANE_ANSWER)

    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert bot.sent == [
        {"chat_id": CHAT, "text": "the answer from the pane",
         "reply_to": 55, "thread": None}
    ]
    assert handler.requested, "the pane path must have been used"
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "telegram_gateway" and "relay_api_unavailable" in r.getMessage()
    ]
    assert len(warnings) == 1, f"once per stretch, got {len(warnings)}"
    assert "HTTP 401" in warnings[0] and "fallback=pane" in warnings[0]


def test_relay_falls_back_to_the_pane_when_the_service_refuses_connections(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService(
        [],
        request_error=httpx.ConnectError(
            "connection refused", request=httpx.Request("GET", "http://x")
        ),
    )
    bot = FakeBot()
    client, handler = pane_client(PANE_ANSWER)

    try:
        run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert bot.sent and handler.requested
    assert state.oc_relayed_id(CHAT, "mgr-x") is None  # nothing adopted from a failure


def test_relay_falls_back_to_the_pane_when_no_service_is_listening(tmp_path, caplog):
    """The common case: `opencode serve --service` died with its TUI."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService(
        [], discovery_error=OcError(0, "no `opencode serve --service` listening")
    )
    bot = FakeBot()
    client, handler = pane_client(PANE_ANSWER)

    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert bot.sent[0]["text"] == "the answer from the pane"
    assert handler.requested
    warns = [
        r.getMessage()
        for r in caplog.records
        if "relay_api_unavailable" in r.getMessage()
    ]
    assert len(warns) == 1 and "no `opencode serve --service` listening" in warns[0]


def test_relay_never_calls_the_service_for_an_unpinned_session(tmp_path):
    """Legacy/unpinned opencode lanes and non-opencode providers: pane only."""
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)  # no pin recorded: pane path only
    oc = FakeOcService([assistant("msg_1", "should never be read")])
    bot = FakeBot()
    client, handler = pane_client(PANE_ANSWER)

    try:
        run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert oc.calls == 0
    assert handler.requested, "the pane path must have been used"
    assert bot.sent[0]["text"] == "the answer from the pane"


def test_relay_says_so_when_the_relayed_id_is_out_of_the_fetched_window(tmp_path, caplog):
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    state.set_pinned(CHAT, "mgr-x", PIN)
    state.set_oc_relayed_id(CHAT, "mgr-x", "msg_gone")
    oc = FakeOcService([assistant("msg_9", "far newer history")])
    bot = FakeBot()
    client, _ = pane_client("")

    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.relay_once(app_for(state, bot, oc, client)))
            run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert bot.sent == []  # nothing is sent on purpose…
    assert state.oc_relayed_id(CHAT, "mgr-x") == "msg_gone"  # …and the id stands
    misses = [
        r.getMessage()
        for r in caplog.records
        if "relay_api_window_missed" in r.getMessage()
    ]
    assert len(misses) == 1 and "since=msg_gone" in misses[0]  # once per stretch


# ---- the pane fallback: the widened peek ---------------------------------


def test_relay_peeks_far_back_for_a_sentinel_out_of_the_old_window(tmp_path):
    """Regression for the 2026-10-28 silence: a sentinel sitting at line ~500
    was invisible to the old `lines=300` peek, so a long answer was dropped.
    The default `TELEGRAM_GATEWAY_PEEK_LINES` (2000) must reach it — and the
    handler here only ever returns the LAST `lines` lines, so a short peek
    physically cannot see the boundary.
    """
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    buffer = [f"filler line {i}" for i in range(500)]
    buffer += ["—— answer ——", "the answer after 500 lines of scroll"]
    buffer += [f"trailing {i}" for i in range(5)]
    oc = FakeOcService([])  # no pin: the service is not consulted at all
    bot = FakeBot()
    client, handler = pane_client(buffer=buffer)

    try:
        run(botmod.relay_once(app_for(state, bot, oc, client)))
    finally:
        run(client.aclose())

    assert oc.calls == 0
    assert handler.requested and handler.requested[0] >= 1000, handler.requested
    assert bot.sent, "the boundary far beyond the old 300-line window was found"
    assert bot.sent[0]["text"].startswith("the answer after 500 lines of scroll")
    assert bot.sent[0]["reply_to"] == 55


def test_peek_lines_is_configurable(tmp_path):
    config = cfg(peek_lines=1500)
    assert config.peek_lines == 1500
    assert cfg().peek_lines >= 1000  # "default well above 300" (owner)
