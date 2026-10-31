"""The permanent menu (owner, 2026-10-31): one message, pinned, always shown.

Owner ask: *"Could I have a permanent menu always shown in the general
manager topic?"* The dispatcher answers in the forum group's General thread
(M11.5), and a Telegram bot's only "always shown" mechanism is a PINNED
message — so the gateway posts one menu message, remembers its id in
`state.json` (the rebind rule: only this process writes it), EDITs it on
later ensures instead of stacking copies, and pins it.

The contract under test:

1. the chat is the dispatcher's thread — a topic-bound dispatcher chat, else
   any forum chat, else a DM fallback (never a stray private DM outranking
   the group);
2. first ensure sends + records + pins; later ensures EDIT (no duplicates);
   "not modified" is a success, a deleted message re-sends fresh;
3. a refused pin is reported honestly (`unpinned` + the missing right), the
   menu message itself never disappears;
4. `/gmmenu` tells the owner exactly which state it reached; the startup
   task is wired and cancelled on shutdown — and never runs when no chat is
   known (the offline rule: no test may open a socket).
"""
import logging
from types import SimpleNamespace

from telegram.error import BadRequest, TelegramError

from telegram_gateway import bot as botmod
from telegram_gateway.state import GatewayState

from test_session_control import fake_command
from test_topics import CHAT, FakeBot, fake_context, make_cfg, run

GM = "general-mgr-1"
DM = 777  # a private chat that also has a dispatcher turn (must never win)


def forum_state(tmp_path, *, dm_first=False):
    """A state where the dispatcher answered in BOTH the group and a DM."""
    state = GatewayState(tmp_path / "state.json")
    if dm_first:
        state.set_turn_message(DM, GM, 3)  # private DM, no topics
    state.set_topic(CHAT, 42, "wk-x")  # the forum group (bound topics)
    state.set_turn_message(CHAT, GM, 100)  # its General turn
    if not dm_first:
        state.set_turn_message(DM, GM, 3)
    return state


def app_for(tmp_path, state, bot):
    cfg = make_cfg(tmp_path)  # allowed_user_ids = {1}
    return SimpleNamespace(bot=bot, bot_data={"cfg": cfg, "state": state})


# ---- 1. which chat ----------------------------------------------------------


def test_menu_chat_prefers_the_dispatcher_thread_over_a_dm(tmp_path):
    state = forum_state(tmp_path, dm_first=True)
    assert botmod.gm_menu_chat(state, {1}) == CHAT  # DM is found first, loses

    state = forum_state(tmp_path, dm_first=False)
    assert botmod.gm_menu_chat(state, {1}) == CHAT


def test_menu_chat_falls_back_to_the_forum_then_a_dm(tmp_path):
    # no dispatcher turn anywhere, but the group has topics → the group
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "wk-x")
    assert botmod.gm_menu_chat(state, {1}) == CHAT

    # only a DM has the dispatcher (no forum at all) → the DM, honestly
    state2 = GatewayState(tmp_path / "state2.json")
    state2.set_turn_message(DM, GM, 3)
    assert botmod.gm_menu_chat(state2, {1}) == DM

    # nothing at all → None (the ensure reports `skipped`, never guesses)
    assert botmod.gm_menu_chat(GatewayState(tmp_path / "s3.json"), {1}) is None


# ---- 2. first ensure: send, record, pin -------------------------------------


def test_first_ensure_posts_the_menu_records_it_and_pins(tmp_path, caplog):
    state = forum_state(tmp_path)
    bot = FakeBot()
    app = app_for(tmp_path, state, bot)

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))

    assert result["status"] == "pinned"
    (sent,) = bot.sent
    assert sent["chat_id"] == CHAT
    assert botmod.GM_MENU_TEXT.splitlines()[0] in sent["text"]
    # ONE menu, and it is the SAME keyboard everywhere (main_menu + 🧩) —
    # keyboards are recorded beside `sent`, never inside it
    (msg_id, markup) = bot.markups[0]
    keys = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert keys[-1] == "gw:prj"
    assert "gw:new" in keys
    # recorded so the NEXT ensure edits this message instead of stacking one
    assert state.menu_message(CHAT) == 901
    assert bot.pins == [(CHAT, 901)]
    assert any("gm_menu_pinned" in m for m in caplog.messages)


def test_a_second_ensure_edits_never_duplicates(tmp_path, caplog):
    state = forum_state(tmp_path)
    bot = FakeBot()
    app = app_for(tmp_path, state, bot)
    run(botmod.ensure_gm_menu(app))

    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))

    assert result["status"] == "pinned"
    assert len(bot.sent) == 1  # still ONE message — edited, not re-sent
    assert bot.edited == [(CHAT, 901, botmod.GM_MENU_TEXT)]
    assert bot.pins == [(CHAT, 901), (CHAT, 901)]  # the pin re-asserts
    assert state.menu_message(CHAT) == 901


def test_unmodified_content_is_a_success_not_a_failure(tmp_path):
    state = forum_state(tmp_path)
    bot = FakeBot()
    bot.edit_error = BadRequest("Message is not modified: specified new message")
    app = app_for(tmp_path, state, bot)
    run(botmod.ensure_gm_menu(app))  # first: sends (edit never ran yet)
    bot.edit_error = BadRequest("Message is not modified: specified new message")
    result = run(botmod.ensure_gm_menu(app))
    assert result["status"] == "pinned"
    assert len(bot.sent) == 1
    assert bot.pins[-1] == (CHAT, 901)


def test_a_deleted_menu_message_replaces_itself(tmp_path, caplog):
    """A dead reference must never wedge the menu: not-found → clear → fresh."""
    state = forum_state(tmp_path)
    bot = FakeBot()
    app = app_for(tmp_path, state, bot)
    run(botmod.ensure_gm_menu(app))

    bot.edit_error = BadRequest("Message to edit not found")
    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))

    assert result["status"] == "pinned"
    assert len(bot.sent) == 2  # a fresh message replaced the dead one
    assert state.menu_message(CHAT) == 902  # the new id is recorded
    assert bot.pins[-1] == (CHAT, 902)
    assert any("gm_menu_edit_gone" in m for m in caplog.messages)


def test_a_flaky_edit_keeps_the_message_and_the_pin_still_runs(tmp_path, caplog):
    """Network trouble on the edit must not duplicate the menu: keep the id,
    pin what exists, let the next ensure retry the content."""
    state = forum_state(tmp_path)
    bot = FakeBot()
    app = app_for(tmp_path, state, bot)
    run(botmod.ensure_gm_menu(app))

    bot.edit_error = TelegramError("read timed out")
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))

    assert result["status"] == "pinned"
    assert len(bot.sent) == 1  # NOT duplicated
    assert any("gm_menu_edit_failed" in m for m in caplog.messages)


# ---- 3. a refused pin reports honestly --------------------------------------


def test_a_refused_pin_keeps_the_menu_and_names_the_reason(tmp_path, caplog):
    state = forum_state(tmp_path)
    bot = FakeBot()
    bot.pin_error = TelegramError("Not enough rights to pin/unpin messages")
    app = app_for(tmp_path, state, bot)

    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))

    assert result["status"] == "unpinned"
    assert "rights" in result["detail"]
    assert len(bot.sent) == 1  # the menu message still exists
    assert state.menu_message(CHAT) == 901
    assert any("gm_menu_pin_failed" in m for m in caplog.messages)


def test_no_known_chat_is_a_skipped_not_a_crash(tmp_path, caplog):
    app = app_for(tmp_path, GatewayState(tmp_path / "state.json"), FakeBot())
    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        result = run(botmod.ensure_gm_menu(app))
    assert result == {"status": "skipped", "detail": "no chat has the dispatcher yet"}
    assert result["status"] == "skipped" and app.bot.sent == []
    assert any("gm_menu_skipped" in m for m in caplog.messages)


# ---- 4. /gmmenu speaks its state --------------------------------------------


def test_gmmenu_command_reports_pinned_and_unpinned(tmp_path):
    state = forum_state(tmp_path)
    bot = FakeBot()
    context = fake_context(make_cfg(tmp_path), None, state, bot)

    update = fake_command("/gmmenu")
    run(botmod.gmmenu_command(update, context))
    assert "📌 Menu pinned" in update.message.replies[0]["text"]

    (tmp_path / "b").mkdir()
    bot2 = FakeBot()
    bot2.pin_error = TelegramError("Not enough rights to pin/unpin messages")
    context2 = fake_context(make_cfg(tmp_path / "b"), None, state, bot2)
    update2 = fake_command("/gmmenu")
    run(botmod.gmmenu_command(update2, context2))
    text = update2.message.replies[0]["text"]
    assert "could NOT pin it" in text
    assert "Pin messages" in text and "@homuxBot" in text


def test_gmmenu_in_an_empty_state_asks_for_a_dispatcher_first(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    bot = FakeBot()
    context = fake_context(make_cfg(tmp_path), None, state, bot)
    update = fake_command("/gmmenu")

    run(botmod.gmmenu_command(update, context))

    assert "No chat has your dispatcher yet" in update.message.replies[0]["text"]


# ---- 5. wiring: startup task, cancelled on shutdown -------------------------


def test_post_init_spawns_the_menu_task_and_shutdown_closes_it(tmp_path, caplog):
    import httpx

    from telegram_gateway.amux import AmuxClient
    from telegram_gateway.bot import build_application, post_init, post_shutdown

    cfg = make_cfg(tmp_path, state_dir=tmp_path / "gw-state")  # ISOLATED state
    app = build_application(cfg)
    app.bot_data["client"] = AmuxClient(
        "https://localhost:1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    # Isolated + fresh state: no chat knows the dispatcher → the task must
    # SKIP, which is also what keeps this test offline (a real post would
    # open a socket — and must never read the LIVE gateway state either).
    # Everything runs in ONE loop: asyncio.run cancels foreign-loop tasks on
    # close, so spawn/await/shutdown must share a scenario.
    async def scenario():
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            await post_init(app)
            task = app.bot_data.get("gm_menu_task")
            assert task is not None
            await task  # completes: skipped (fresh state)
            await post_shutdown(app)
        return task

    task = run(scenario())

    assert any("gm_menu_skipped" in m for m in caplog.messages)
    assert task.done()  # spawned, completed, and closed by the shutdown
