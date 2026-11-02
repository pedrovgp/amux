"""Only the final answer may buzz the phone (owner, 2026-11-02).

The failure being fixed: EVERY relayed segment — progress chatter ("I'm
investigating"), thinking, tool lines, the tails of a still-growing answer —
was sent with python-telegram-bot's default notification, so a dispatch
produced one push per relayed line (`relay_sent … chars=12, chars=12,
chars=1346` in the live log). The contract under test:

1. **progress is delivered, silently** — a segment with no `—— answer ——`
   sentinel goes out with `disable_notification` (arrives in the topic, no
   push), on BOTH paths (the OpenCode API read and the pane scrape);
2. **the answer notifies** — the sentinel-bearing answer's opening send is
   the one push of the turn, and the next turn may push again;
3. **one buzz per answer, not per chunk** — a long answer split by
   `chunk_text` notifies on its FIRST chunk only;
4. **the log says which it was** — `relay_sent … notify=True/False`.

Nothing here touches the network: the pane is a MockTransport, the API a
`FakeOcService`, and the bot a `FakeBot` recording `disable_notification`
beside `sent` (the exact-dict assertions pin `sent`'s shape).
"""
import logging

import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.relay import (
    CHUNK_LIMIT,
    carries_answer,
    pane_answer_start,
    pane_notify,
)
from telegram_gateway.state import GatewayState

from test_manager_mode import FakeBot
from test_relay_api import CHAT, PIN, FakeOcService, app_for, assistant, pane_client, run


@pytest.fixture(autouse=True)
def _clean_notify_gate():
    """Both notify scopes are module state; every test starts with no claim."""
    botmod._answer_notified.clear()
    botmod._answer_msg_notified.clear()
    yield
    botmod._answer_notified.clear()
    botmod._answer_msg_notified.clear()


def state_for(tmp_path) -> GatewayState:
    state = GatewayState(tmp_path / "state.json")
    state.set_turn_message(CHAT, "mgr-x", 55)
    return state


def relay(state, bot, oc, client):
    run(botmod.relay_once(app_for(state, bot, oc, client)))


# ---- the predicates -------------------------------------------------------


def test_carries_answer_is_the_sentinel_rule():
    assert carries_answer("—— answer ——\nthe answer")
    assert not carries_answer("I'm investigating")
    assert not carries_answer("")


def test_pane_answer_start_is_the_boundary_rule():
    # boundary = the sentinel line → the reply STARTS AT the answer
    assert pane_answer_start("[from Telegram @owner] task\n—— answer ——\ndone")
    # boundary = the prompt marker → everything after it is progress
    assert not pane_answer_start("[from Telegram @owner] task\nI'm reading this")


def test_pane_notify_separates_the_answer_from_its_progress_and_its_tails():
    grown = "[from Telegram @owner] task\n—— answer ——\nthe answer\nmore of it"
    # the answer's opening send → the one push of the turn
    assert pane_notify(grown, text="the answer", last=None, segment="the answer")
    # a tail of the still-growing answer → progress, muted
    assert not pane_notify(grown, text="the answer\nmore of it",
                           last="the answer", segment="more of it")
    # a reply with no sentinel yet → progress, muted until the answer lands
    progress = "[from Telegram @owner] task\nI'm investigating"
    assert not pane_notify(progress, text="I'm investigating",
                           last=None, segment="I'm investigating")


# ---- 1. progress: delivered, silent ---------------------------------------


def test_api_progress_messages_are_delivered_without_a_push(tmp_path):
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService([assistant("msg_1", "I'm investigating.")])
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    assert bot.sent == [
        {"chat_id": CHAT, "text": "I'm investigating.", "reply_to": 55, "thread": None}
    ]
    assert bot.notified == [False], "progress arrives, the phone stays quiet"


def test_a_pane_reply_without_the_sentinel_is_muted(tmp_path):
    state = state_for(tmp_path)  # unpinned: the pane scrape is the only path
    bot = FakeBot()
    client, _ = pane_client("[from Telegram @owner] task\nI'm investigating\n")

    try:
        relay(state, bot, None, client)
    finally:
        run(client.aclose())

    assert bot.sent[0]["text"] == "I'm investigating"
    assert bot.notified == [False]


# ---- 2. the answer: one push per turn -------------------------------------


def test_the_api_sentinel_answer_notifies(tmp_path):
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService([assistant("msg_1", "—— answer ——\nthe whole answer")])
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    assert bot.sent[0]["text"] == "—— answer ——\nthe whole answer"
    assert bot.notified == [True]


def test_a_batch_buzzes_once_on_the_newest_answer(tmp_path):
    """A stalled relay can carry two sentinel messages: the owner is waiting
    on the LAST one, so the older answer is delivered muted."""
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    state.set_oc_relayed_id(CHAT, "mgr-x", "msg_1")
    oc = FakeOcService(
        [
            assistant("msg_3", "—— answer ——\nthe current answer"),
            assistant("msg_2", "—— answer ——\na stale answer"),
            assistant("msg_1", "progress chatter"),
        ]
    )
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    assert [m["text"] for m in bot.sent] == [
        "—— answer ——\na stale answer",
        "—— answer ——\nthe current answer",
    ]
    assert bot.notified == [False, True]


def test_the_pane_answer_notifies_once_then_growth_is_muted(tmp_path):
    state = state_for(tmp_path)
    bot = FakeBot()
    first = "[from Telegram @owner] task\n—— answer ——\nthe answer"
    grown = first + "\nmore of it"
    client, _ = pane_client(first)
    client2, _ = pane_client(grown)

    try:
        relay(state, bot, None, client)
        relay(state, bot, None, client2)
    finally:
        run(client.aclose())
        run(client2.aclose())

    assert bot.notified == [True, False], "one push for the answer, none for its tail"


def test_a_growing_answer_stays_muted_without_a_turn_anchor(tmp_path):
    """The segment rule ALONE (no turn anchor to fall back on — a selected
    session, not a manager turn): the first body after the sentinel boundary
    pushes, every later tail is progress."""
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "mgr-x")  # relayable, but with NO turn anchor
    bot = FakeBot()
    first = "[from Telegram @owner] task\n—— answer ——\nthe answer"
    grown = first + "\nmore of it"
    client, _ = pane_client(first)
    client2, _ = pane_client(grown)

    try:
        relay(state, bot, None, client)
        relay(state, bot, None, client2)
    finally:
        run(client.aclose())
        run(client2.aclose())

    assert state.turn_message(CHAT, "mgr-x") is None  # the control: no anchor
    assert bot.notified == [True, False]


def test_the_next_turn_may_notify_again(tmp_path):
    state = state_for(tmp_path)
    bot = FakeBot()
    client, _ = pane_client("[from Telegram @owner] task\n—— answer ——\nfirst")

    try:
        relay(state, bot, None, client)
    finally:
        run(client.aclose())

    state.set_turn_message(CHAT, "mgr-x", 56)  # a new prompt, a new turn
    client2, _ = pane_client(
        "[from Telegram @owner] task two\n—— answer ——\nsecond answer"
    )
    try:
        relay(state, bot, None, client2)
    finally:
        run(client2.aclose())

    assert bot.notified == [True, True], "one push per ANSWER, not one per process"


# ---- 3. chunking: one buzz, not one per chunk -----------------------------


def test_a_chunked_answer_notifies_on_its_first_chunk_only():
    bot = FakeBot()
    text = "—— answer ——\n" + ("answer line\n" * 700)
    assert len(text) > CHUNK_LIMIT

    run(botmod._send_markdown(bot, CHAT, text, notify=True))

    assert len(bot.sent) > 1, "the answer really was split"
    assert bot.notified[0] is True
    assert all(notified is False for notified in bot.notified[1:])


def test_a_muted_send_mutes_every_chunk():
    bot = FakeBot()
    text = "progress chatter\n" * 500
    assert len(text) > CHUNK_LIMIT

    run(botmod._send_markdown(bot, CHAT, text, notify=False))

    assert len(bot.sent) > 1
    assert bot.notified == [False] * len(bot.sent)


def test_an_ordinary_send_still_notifies_by_default():
    """The default is unchanged: `/peek` output, the compact note, anything
    that is not a relayed progress segment still raises its push."""
    bot = FakeBot()

    run(botmod._send_markdown(bot, CHAT, "hello"))

    assert bot.notified == [True]


# ---- 4. the log carries the verdict ---------------------------------------


def test_relay_sent_logs_which_sends_notified(tmp_path, caplog):
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    state.set_oc_relayed_id(CHAT, "mgr-x", "msg_1")
    oc = FakeOcService(
        [
            assistant("msg_3", "—— answer ——\nthe answer"),
            assistant("msg_2", "I'm investigating."),
            assistant("msg_1", "progress chatter"),
        ]
    )
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "telegram_gateway" and "relay_sent" in r.getMessage()
    ]
    assert len(lines) == 2
    assert any("notify=False" in line for line in lines), lines
    assert any("notify=True" in line for line in lines), lines


# ---- 5. the dispatcher-forwarded turn (the owner's 2026-11-02 report) ------


def _user(msg_id, text="[from Telegram @owner] next task"):
    return {
        "id": msg_id,
        "type": "user",
        "time": {"created": 4, "streamed": 5, "completed": 6},
        "content": [{"type": "text", "text": text}],
    }


def test_api_turn_fresh_reads_the_conversation_not_the_anchor():
    """The API path's turn scope: a USER message newer than the last buzzed
    answer IS the next turn — whatever route the prompt took to get there."""
    chat, sess = 1, "s"
    # never buzzed → the first answer may buzz
    assert botmod._api_turn_fresh([assistant("a1")], chat, sess)
    botmod._answer_msg_notified[(chat, sess)] = "a1"
    # same turn: nothing user-authored after the buzzed answer → muted
    assert not botmod._api_turn_fresh(
        [assistant("a2"), assistant("a1"), _user("u1")], chat, sess
    )
    # a new prompt (even one the gateway never saw) → the next turn
    assert botmod._api_turn_fresh(
        [assistant("a2"), _user("u2"), assistant("a1"), _user("u1")], chat, sess
    )
    # the buzzed id slid out of the window → fail OPEN (a missed push is the
    # failure the owner feels; a rare double buzz is not)
    assert botmod._api_turn_fresh([assistant("a9")], chat, sess)


def test_a_dispatcher_forwarded_prompt_buzzes_even_with_a_stuck_anchor(tmp_path):
    """The live defect (2026-11-02): prompts the GM forwards straight to the
    lane (`amux send`) never advance the Telegram anchor, so the anchor-scoped
    gate muted EVERY answer after the first one — the owner saw silent
    replies and asked whether sending had stopped altogether. The conversation
    itself shows the new user message, so the second answer must buzz."""
    state = state_for(tmp_path)  # anchor 55 — and it STAYS 55 all test
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService(
        [assistant("msg_1", "—— answer ——\nfirst answer"), _user("msg_u1")]
    )
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        relay(state, bot, oc, client)  # cycle 1: the first answer buzzes
        # a dispatcher-forwarded prompt + its answer land in the conversation
        # without any gateway involvement:
        oc.history[0:0] = [
            assistant("msg_2", "—— answer ——\nsecond answer"),
            _user("msg_u2"),
        ]
        assert state.turn_message(CHAT, "mgr-x") == 55  # the anchor is STUCK
        relay(state, bot, oc, client)  # cycle 2
    finally:
        run(client.aclose())

    assert bot.notified == [True, True], "a new prompt must buzz its answer"
    assert botmod._answer_msg_notified[(CHAT, "mgr-x")] == "msg_2"


def test_an_answer_with_no_new_prompt_stays_muted(tmp_path):
    """The other half: a repeat/continuation with NO user message after the
    buzzed answer is the SAME turn — no second buzz."""
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    oc = FakeOcService(
        [assistant("msg_1", "—— answer ——\nfirst answer"), _user("msg_u1")]
    )
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        relay(state, bot, oc, client)
        oc.history[0:0] = [assistant("msg_2", "—— answer ——\nsame turn again")]
        relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    assert bot.notified == [True, False], "one push per PROMPT, not per send"


def test_a_repeat_answer_logs_the_quiet_verdict(tmp_path, caplog):
    """The two-fix log signal on the API path: an answer sent SILENT is in
    the log either way (`relay_answer_quiet`), so 'the owner reports no
    pushes' is always greppable."""
    state = state_for(tmp_path)
    state.set_pinned(CHAT, "mgr-x", PIN)
    botmod._answer_msg_notified[(CHAT, "mgr-x")] = "msg_1"  # already buzzed
    oc = FakeOcService([assistant("msg_1", "—— answer ——\nthe answer"), _user("u1")])
    bot = FakeBot()
    client, _ = pane_client("no boundary in this pane at all\n")

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            relay(state, bot, oc, client)
    finally:
        run(client.aclose())

    assert bot.notified == [False]
    assert any(
        "relay_answer_quiet" in m and "reason=already_notified" in m
        for m in caplog.messages
    )


def test_a_held_anchor_logs_the_quiet_answer(tmp_path, caplog):
    """The same signal on the PANE path: the pane says 'this is the answer'
    but the anchor gate refuses — the exact silent-answer signature."""
    state = state_for(tmp_path)  # anchor 55
    botmod._answer_notified[(CHAT, "mgr-x")] = 55  # this anchor already buzzed
    bot = FakeBot()
    client, _ = pane_client(
        "[from Telegram @owner] task\n—— answer ——\nthe answer"
    )

    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            relay(state, bot, None, client)
    finally:
        run(client.aclose())

    assert bot.notified == [False], "delivered, but silent"
    assert any(
        "relay_answer_quiet" in m and "reason=anchor_held" in m
        for m in caplog.messages
    )
