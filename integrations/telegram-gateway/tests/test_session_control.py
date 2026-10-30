"""Per-topic session controls (Milestone 8, owner ask 2026-10-28).

The contract under test:

1. `amux.config()` PATCHes the right substrate (lanes under `/api/sessions`,
   rows under `/api/workers`) and `amux.keys()` posts `{"keys": "Escape"}` to
   the workers route — the interrupt's whole transport;
2. a topic resolves through its PIN to the session's CURRENT name, so a
   rename — ours, or the dashboard's — cannot orphan it, while an unreachable
   amux falls back to the stored name instead of spawning a second manager;
3. the rename cascade rekeys EVERY gateway map and follows the Telegram topic
   title, and the title sync cannot flip that rename back;
4. the panel's buttons stay within Telegram's 64-byte callback cap, effort is
   claude-only (opencode's CLI would die on the flag), and a provider the
   catalog cannot name offers the custom-id entry instead of a dead page.
"""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from telegram.ext import ConversationHandler

from telegram_gateway import bot as botmod, opencode_api
from telegram_gateway.amux import AmuxClient, session_id_from_flags
from telegram_gateway.opencode_api import OcError
from telegram_gateway.state import GatewayState

from test_topics import (
    CHAT,
    FakeBot,
    FakeMessage,
    fake_context,
    fake_update,
    make_cfg,
    make_manager_amux,
    run,
)

PIN = "ses_tg10280000abcdef"


class NoOc:
    """The default API entry point in tests: no OpenCode server, ever.

    Without it a pinned opencode lane would run the REAL discovery (``ss`` +
    ``/proc``) from inside the suite — the offline rule for Milestone 9 is
    that discovery and transport are always something a test hands in.
    """

    def __init__(self, exc: Exception | None = None):
        self.exc = exc or OcError(0, "no opencode serve (offline test)")
        self.calls: list[tuple] = []

    async def client_for(self, lane=None, pin=None):
        self.calls.append(("client_for", lane, pin))
        raise self.exc

    async def client_for_service(self, **kwargs):
        self.calls.append(("client_for_service", kwargs))
        raise self.exc


# ---- fakes -----------------------------------------------------------------


class CtlBot(FakeBot):
    """FakeBot plus the title-edit surface the rename cascade drives."""

    def __init__(self):
        super().__init__()
        self.edited_topics = []  # (chat_id, thread, name)

    async def edit_forum_topic(self, chat_id, message_thread_id=None, name=None):
        self.edited_topics.append((chat_id, message_thread_id, name))
        return SimpleNamespace(message_thread_id=message_thread_id, name=name)


class FakeQuery:
    def __init__(self, data, *, thread=None, message_id=500):
        self.data = data
        self.message = SimpleNamespace(
            message_id=message_id, message_thread_id=thread, text=None
        )
        self.edits = []
        self.answered = False

    async def answer(self, *args, **kwargs):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append({"text": text, "markup": reply_markup, "parse_mode": parse_mode})
        return self.message


def fake_callback(data, *, thread=None, chat_id=CHAT):
    query = FakeQuery(data, thread=thread)
    chat = SimpleNamespace(id=chat_id)
    chat.is_forum = True
    return SimpleNamespace(
        callback_query=query,
        effective_message=query.message,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1, username="owner"),
        message=None,
    )


def fake_command(text, *, thread=None, chat_id=CHAT, args=(), forum=True):
    message = FakeMessage(message_id=900, text=text, thread=thread)
    chat = SimpleNamespace(id=chat_id)
    chat.is_forum = forum
    return SimpleNamespace(
        message=message,
        effective_message=message,
        effective_chat=chat,
        effective_user=SimpleNamespace(id=1, username="owner"),
        callback_query=None,
    )


def lane(name, **kw):
    row = {"name": name, "activity": "none", "status": "idle", "running": True,
           "provider": "opencode", "flags": ""}
    row.update(kw)
    return row


class FakeAmux:
    """One mock transport for every verb the control panel uses."""

    def __init__(self, rows=(), models=(), config_response=None, config_status=200,
                 keys_status=200):
        self.rows = [dict(row) for row in rows]
        self.models = [dict(m) for m in models]
        self.calls = []  # (method, path, body-dict)
        self.config_response = config_response  # override the default PATCH answer
        self.config_status = config_status
        self.keys_status = keys_status
        self.keys_response = {"ok": True, "accepted": True, "effect": "unverified",
                              "message": "sent"}
        self.client = AmuxClient(
            "https://localhost:8824", transport=httpx.MockTransport(self._handle)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        self.calls.append((request.method, path, body))
        if request.method == "GET" and path == "/api/sessions":
            return httpx.Response(200, json=self.rows)
        if request.method == "GET" and path == "/api/models":
            return httpx.Response(200, json={"models": self.models})
        if request.method == "PATCH" and path.endswith("/config"):
            if self.config_status >= 400:
                return httpx.Response(
                    self.config_status, json={"error": "refused"}
                )
            if "rename" in body and self.config_response is None:
                # Mirror the server: the lane really is renamed, so a later
                # find() sees the new name rather than a frozen fixture.
                new = str(body["rename"])
                old = path.split("/")[-2]
                for row in self.rows:
                    if row.get("name") == old:
                        row["name"] = new
                return httpx.Response(
                    200, json={"ok": True, "name": new, "message": f"renamed to {new}"}
                )
            payload = self.config_response or {"ok": True, "message": "applied"}
            return httpx.Response(self.config_status, json=payload)
        if request.method == "POST" and path.endswith("/keys"):
            if self.keys_status >= 400:
                return httpx.Response(self.keys_status, json={"error": "not running"})
            return httpx.Response(200, json=self.keys_response)
        if request.method == "POST" and path.endswith("/send"):
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET" and path.endswith("/peek"):
            return httpx.Response(200, json={"output": "the answer\n"})
        if request.method == "POST" and path == "/api/sessions":
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(200, json={})

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    async def aclose(self):
        await self.client.aclose()


def ctl_app(cfg, state, fake, bot=None, oc=None):
    """The shape `sync_topic_titles_once` / loops read: bot_data at the top."""
    return SimpleNamespace(
        bot=bot or CtlBot(),
        bot_data={
            "cfg": cfg,
            "client": fake.client,
            "state": state,
            "guard": None,
            "oc_api": oc or NoOc(),
        },
    )


def buttons(markup):
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _clean_warn_dedupe():
    botmod._cards_warned.clear()
    botmod._no_boundary_warned.clear()
    botmod._oc_fallback_warned.clear()
    opencode_api.reset_warn_dedupe()
    yield
    botmod._cards_warned.clear()
    botmod._no_boundary_warned.clear()
    botmod._oc_fallback_warned.clear()
    opencode_api.reset_warn_dedupe()


# ---- amux client verbs -----------------------------------------------------


def test_config_patch_uses_the_lane_route_for_a_lane(tmp_path):
    fake = FakeAmux(rows=[lane("mgr-x")])
    try:
        result = run(fake.client.config("mgr-x", model="claude-sonnet-5"))
    finally:
        run(fake.client.aclose())
    assert fake.patches() == [
        ("PATCH", "/api/sessions/mgr-x/config", {"model": "claude-sonnet-5"})
    ]
    assert result["ok"] is True


def test_config_patch_uses_the_worker_route_for_a_row(tmp_path):
    fake = FakeAmux(rows=[{"name": "row-x", "status": "idle"}])  # no `activity`
    try:
        run(fake.client.config("row-x", rename="row-y"))
    finally:
        run(fake.client.aclose())
    assert fake.patches() == [
        ("PATCH", "/api/workers/row-x/config", {"rename": "row-y"})
    ]


def test_keys_posts_escape_to_the_workers_route(tmp_path):
    fake = FakeAmux(rows=[lane("mgr-x")])
    try:
        result = run(fake.client.keys("mgr-x", "Escape"))
    finally:
        run(fake.client.aclose())
    assert fake.posts() == [
        ("POST", "/api/workers/mgr-x/keys", {"keys": "Escape"})
    ]
    assert result["effect"] == "unverified"


def test_session_id_from_flags_spellings():
    assert session_id_from_flags("--session ses_abc") == "ses_abc"
    assert session_id_from_flags("--session=ses_abc --auto") == "ses_abc"
    assert session_id_from_flags("--auto --session ses_abc") == "ses_abc"
    assert session_id_from_flags("--auto") is None
    assert session_id_from_flags("") is None
    assert session_id_from_flags(None) is None
    assert session_id_from_flags("--session --auto") is None  # value is a flag
    assert session_id_from_flags("--session noses_prefix") is None


# ---- state: pin-keyed identity + migration ---------------------------------


def test_set_pinned_promotes_an_existing_topic_binding(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    # The order the gateway actually writes in: the topic is bound before the
    # lane exists, and the pin only arrives once it does.
    state.set_topic(CHAT, 42, "mgr-x", created=True)
    assert state.topic_pin(CHAT, 42) is None
    state.set_pinned(CHAT, "mgr-x", PIN)
    assert state.topic_pin(CHAT, 42) == PIN
    assert state.topic_session(CHAT, 42) == "mgr-x"  # the fallback name stays
    # and a topic bound AFTER the pin carries it from the start
    state.set_topic(CHAT, 43, "mgr-x", created=True)
    assert state.topic_pin(CHAT, 43) == PIN
    # an unpinned session never gains a pin
    state.set_topic(CHAT, 44, "legacy-y")
    assert state.topic_pin(CHAT, 44) is None
    assert GatewayState(path).topic_pin(CHAT, 42) == PIN  # persisted


def test_load_migrates_name_bindings_to_pins(tmp_path):
    """state.json written BEFORE pins existed gets them on the next load."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "chats": {
                    str(CHAT): {
                        "topics": {"42": "mgr-old", "43": "legacy"},
                        "pinned": {"mgr-old": PIN},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    state = GatewayState(path)
    assert state.topic_pin(CHAT, 42) == PIN
    assert state.topic_pin(CHAT, 43) is None  # unpinned: stays name-bound
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["chats"][str(CHAT)]["topic_pins"] == {"42": PIN}


def test_session_topic_resolves_by_pin_before_name(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x")
    state.set_pinned(CHAT, "mgr-x", PIN)
    assert state.session_topic(CHAT, "mgr-x") == 42
    assert state.session_topic(CHAT, "nobody") is None
    # a topic bound by name only (legacy) still resolves by name
    state.set_topic(CHAT, 43, "legacy-y")
    assert state.session_topic(CHAT, "legacy-y") == 43


def test_rename_cascades_every_state_map(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    state.set_topic(CHAT, 43, "other")  # a second topic, untouched by the rename
    state.set_turn_message(CHAT, "mgr-old", 55, thread=42)
    state.touch_activity(CHAT, "mgr-old", "owner", at=1.0)
    state.mark_idle_stopped(CHAT, "mgr-old", at=2.0)
    state.record_message(CHAT, 55, "mgr-old")
    state.set_last_relayed(CHAT, "mgr-old", "block")
    state.select(CHAT, "mgr-old")

    assert state.rename_session(CHAT, "mgr-old", "tg-new") == [42]

    chat = json.loads(path.read_text(encoding="utf-8"))["chats"][str(CHAT)]
    assert chat["session"] == "tg-new"
    assert list(chat["pinned"]) == ["tg-new"]
    assert list(chat["activity"]) == ["tg-new"]
    assert list(chat["idle_stopped"]) == ["tg-new"]
    assert list(chat["turn_msg"]) == ["tg-new"]
    assert list(chat["last_relayed"]) == ["tg-new"]
    assert chat["msg_sessions"]["55"] == "tg-new"
    assert chat["topics"]["42"] == "tg-new"
    assert chat["topic_pins"]["42"] == PIN  # identity untouched by a rename
    # topic_names is the record of the Telegram TITLE, so the cascade leaves it
    # to the caller — which edits the title and only then records the new one.
    assert chat["topic_names"]["42"] == "mgr-old"
    assert chat["topics"]["43"] == "other"  # other sessions never move
    assert state.pinned(CHAT, "tg-new") == PIN
    assert state.pinned(CHAT, "mgr-old") is None
    assert state.session_topic(CHAT, "tg-new") == 42
    assert state.idle_stopped(CHAT, "tg-new") == 2.0
    assert GatewayState(path).selected(CHAT) == "tg-new"


def test_rebind_pin_catches_up_after_an_outside_rename(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    state.set_turn_message(CHAT, "mgr-old", 55, thread=42)

    assert state.rebind_pin(CHAT, PIN, "mgr-new") == ("mgr-old", [42])
    assert state.pinned(CHAT, "mgr-new") == PIN
    assert state.pinned(CHAT, "mgr-old") is None
    assert state.turn_message(CHAT, "mgr-new") == 55
    assert state.session_topic(CHAT, "mgr-new") == 42
    assert state.rebind_pin(CHAT, PIN, "mgr-new") is None  # already caught up


def test_drop_session_topics_matches_by_pin_when_the_name_is_stale(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-stale-name", created=True)
    state.set_pinned(CHAT, "mgr-stale-name", PIN)
    # the CURRENT name (the pin's key is current after the cascade)
    state.rename_session(CHAT, "mgr-stale-name", "mgr-now")
    assert state.drop_session_topics(CHAT, "mgr-now") == [42]
    assert state.topic_session(CHAT, 42) is None
    assert state.topic_pin(CHAT, 42) is None


def test_tracked_sessions_keeps_names_for_pin_bound_topics(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x")
    state.set_pinned(CHAT, "mgr-x", PIN)
    tracked = state.tracked_sessions()[CHAT]
    assert "mgr-x" in tracked  # NAMES, never pins: the sweep keys on names


# ---- the resolver: pin -> current name, name fallback ----------------------


def context_for(cfg, state, client, bot=None, args=(), oc=None):
    """The fake app context, in the (cfg, state, client) order this file uses.

    ``test_topics.fake_context`` takes (cfg, client, state); swapping here is
    cheaper than misreading an argument list in 25 tests. ``args`` mirrors what
    PTB puts on the context for a command with arguments. ``oc`` is the
    OpenCode API entry point (``NoOc`` by default — offline).
    """
    context = fake_context(cfg, client, state, bot or CtlBot())
    context.args = list(args)
    context.application.bot_data["oc_api"] = oc or NoOc()
    return context


def test_resolver_follows_the_pin_to_the_renamed_lane(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    state.set_turn_message(CHAT, "mgr-old", 55, thread=42)
    fake = FakeAmux(rows=[lane("mgr-new", flags=f"--session {PIN}")])
    bot = CtlBot()
    context = context_for(cfg, state, fake.client, bot)
    try:
        resolved = run(botmod._resolve_topic_session(context, CHAT, 42))
    finally:
        run(fake.aclose())
    assert resolved == "mgr-new"
    # and the maps were repaired behind it, so the relay keeps threading
    assert state.pinned(CHAT, "mgr-new") == PIN
    assert state.turn_message(CHAT, "mgr-new") == 55
    # the topic title followed the rename (and the auto-title sync let go)
    assert bot.edited_topics == [(CHAT, 42, "mgr-new")]
    assert state.topic_name(CHAT, 42) == "mgr-new"
    assert state.topic_created(CHAT, 42) is False


def test_resolver_falls_back_to_the_stored_name_when_amux_is_down(tmp_path, caplog):
    """An unreachable amux must never be read as "the session is gone" — that
    would adopt a SECOND manager into a live topic."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x", created=True)
    state.set_pinned(CHAT, "mgr-x", PIN)

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    fake = FakeAmux()
    fake.client = AmuxClient(
        "https://localhost:8824", transport=httpx.MockTransport(refuse)
    )
    context = context_for(cfg, state, fake.client)
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        resolved = run(botmod._resolve_topic_session(context, CHAT, 42))
    assert resolved == "mgr-x"  # the binding that exists
    assert any("topic_resolve_unreachable" in m for m in caplog.messages)


def test_resolver_keeps_the_stored_name_when_the_pin_has_no_live_lane(tmp_path):
    """The lane is gone: the name 404s into the existing release path, which is
    what frees the topic for adoption — the resolver never adopts itself."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-gone", created=True)
    state.set_pinned(CHAT, "mgr-gone", PIN)
    fake = FakeAmux(rows=[lane("somebody-else")])
    context = context_for(cfg, state, fake.client)
    try:
        resolved = run(botmod._resolve_topic_session(context, CHAT, 42))
    finally:
        run(fake.aclose())
    assert resolved == "mgr-gone"


def test_resolver_returns_none_for_an_unbound_topic(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[])
    context = context_for(cfg, state, fake.client)
    try:
        assert run(botmod._resolve_topic_session(context, CHAT, 777)) is None
    finally:
        run(fake.aclose())


def test_a_message_in_a_topic_still_continues_after_a_rename(tmp_path):
    """The end-to-end routing rule: topic → pin → the lane's CURRENT name."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    fake = FakeAmux(rows=[lane("mgr-new", flags=f"--session {PIN}")])
    bot = CtlBot()
    message = FakeMessage(message_id=200, text="and now what?", thread=42)
    update = fake_update(message)
    try:
        run(botmod.on_text(update, context_for(cfg, state, fake.client, bot)))
    finally:
        run(fake.aclose())
    sent = [c for c in fake.posts() if c[1].endswith("/send")]
    assert sent == [
        ("POST", "/api/sessions/mgr-new/send", {"text": "[from Telegram @owner] and now what?"})
    ]
    assert state.turn_message(CHAT, "mgr-new") == 200
    assert state.turn_thread(CHAT, "mgr-new") == 42


# ---- the panel -------------------------------------------------------------


def test_panel_renders_with_bounded_callbacks(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    # A session name at the panel's own cap (40 bytes): the RENDERED buttons
    # must still fit Telegram's 64 with their verb + second token.
    name = "mgr-" + "x" * 36
    fake = FakeAmux(rows=[lane(name, flags=f"--session {PIN}", running=False)])
    update = fake_callback(f"gw:ctl:{name}")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    markup = update.callback_query.edits[0]["markup"]
    data = buttons(markup)
    assert all(len(d.encode()) <= 64 for d in data), data
    assert "gw:ctlm:" in "".join(data)
    assert "gw:ctle:" in "".join(data)
    assert "gw:ctlr:" in "".join(data)
    assert "gw:ctli:" in "".join(data)
    assert "gw:stop:" in "".join(data)


def test_every_control_keyboard_fits_the_64_byte_cap():
    from telegram_gateway.menu import control_button, control_keyboard, effort_keyboard, model_keyboard

    long_name = "mgr-" + "x" * 60
    for markup in (
        control_button(long_name),
        control_keyboard(long_name),
        model_keyboard(long_name, ["a" * 45], "a" * 45),
        effort_keyboard(long_name, "xhigh"),
    ):
        for data in buttons(markup):
            assert len(data.encode()) <= 64, data


def test_model_pick_applies_the_indexed_model(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "claude-lane")
    fake = FakeAmux(
        rows=[lane("claude-lane", provider="claude", running=False)],
        models=[
            {"provider": "claude", "id": "claude-haiku-5"},
            {"provider": "claude", "id": "claude-opus-5"},
        ],
    )
    context = context_for(cfg, state, fake.client, CtlBot())
    try:
        listed = fake_callback("gw:ctlm:claude-lane")
        run(botmod.on_callback(listed, context))
        markup = listed.callback_query.edits[0]["markup"]
        data = buttons(markup)
        assert "gw:ctlp:claude-lane:1" in data
        assert any(d.startswith("gw:ctlc:") for d in data)  # the custom-id entry
        # the row marked current is the one the lane runs
        assert "○ claude-opus-5" in [b.text for row in markup.inline_keyboard for b in row]

        picked = fake_callback("gw:ctlp:claude-lane:1")
        run(botmod.on_callback(picked, context))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/claude-lane/config", {"model": "claude-opus-5"}) in fake.patches()
    # and the panel came back with the server's own confirmation
    assert "✅" in picked.callback_query.edits[0]["text"]


def test_model_page_of_an_uncatalogued_provider_offers_the_custom_entry(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("mgr-x", provider="opencode")], models=[])
    update = fake_callback("gw:ctlm:mgr-x")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    markup = update.callback_query.edits[0]["markup"]
    assert any(d.startswith("gw:ctlc:") for d in buttons(markup))
    assert "catalog has no" in update.callback_query.edits[0]["text"]


def test_custom_model_id_is_asked_for_and_applied(tmp_path):
    """The opencode path: the catalog cannot name the model, the owner can."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("mgr-x", provider="opencode")], models=[])
    bot = CtlBot()
    context = context_for(cfg, state, fake.client, bot)
    asked = fake_callback("gw:ctlc:mgr-x")
    assert run(botmod.ask_model(asked, context)) == botmod.ASK
    run(
        botmod.receive_ask(
            fake_update(FakeMessage(message_id=610, text="opencode-go/deepseek-v4-pro", thread=42)),
            context,
        )
    )
    run(fake.aclose())
    assert (
        "PATCH",
        "/api/sessions/mgr-x/config",
        {"model": "opencode-go/deepseek-v4-pro"},
    ) in fake.patches()


def test_server_default_resets_the_model(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(
        rows=[lane("claude-lane", provider="claude", running=False)],
        models=[{"provider": "claude", "id": "claude-haiku-5"}],
    )
    update = fake_callback("gw:ctlp:claude-lane:")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/claude-lane/config", {"model": ""}) in fake.patches()


def test_stale_model_index_re_renders_instead_of_guessing(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(
        rows=[lane("claude-lane", provider="claude", running=False)],
        models=[{"provider": "claude", "id": "claude-haiku-5"}],
    )
    update = fake_callback("gw:ctlp:claude-lane:9")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert fake.patches() == []  # nothing was applied
    assert update.callback_query.edits[0]["text"].startswith("🤖")


def test_effort_is_a_claude_dial_only(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("mgr-x", provider="opencode")])
    update = fake_callback("gw:ctlf:mgr-x:high")
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert fake.patches() == []  # opencode would die on `--effort`
    assert any("session_effort_unsupported" in m for m in caplog.messages)
    assert "claude" in update.callback_query.edits[0]["text"]


def test_effort_patch_for_a_claude_lane(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("claude-lane", provider="claude", running=False)])
    update = fake_callback("gw:ctlf:claude-lane:high")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/claude-lane/config", {"effort": "high"}) in fake.patches()


def test_effort_default_resets_the_flag(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("claude-lane", provider="claude", running=False)])
    update = fake_callback("gw:ctlf:claude-lane:")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/claude-lane/config", {"effort": ""}) in fake.patches()


def test_unknown_effort_level_is_refused_before_any_patch(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("claude-lane", provider="claude")])
    update = fake_callback("gw:ctlf:claude-lane:ludicrous")
    try:
        run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert fake.patches() == []
    assert "unknown effort" in update.callback_query.edits[0]["text"]


# ---- interrupt -------------------------------------------------------------


def test_interrupt_posts_escape_and_logs_it(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("mgr-x")])
    update = fake_callback("gw:ctli:mgr-x")
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert ("POST", "/api/workers/mgr-x/keys", {"keys": "Escape"}) in fake.posts()
    assert any("session_interrupted" in m for m in caplog.messages)


def test_interrupt_failure_is_a_warn_not_a_silent_tap(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[lane("mgr-x")])
    fake.keys_status = 409
    update = fake_callback("gw:ctli:mgr-x")
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.on_callback(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert any("session_interrupt_failed" in m for m in caplog.messages)
    assert "❌" in update.callback_query.edits[0]["text"]


# ---- rename cascade --------------------------------------------------------


def ask_then_answer(cfg, state, fake, bot, session, answer, *, thread=None):
    """Tap ✏️, then send the prompted name — the conversation's two halves."""
    asked = fake_callback(f"gw:ctlr:{session}", thread=thread)
    context = context_for(cfg, state, fake.client, bot)
    assert run(botmod.ask_rename(asked, context)) == botmod.ASK
    message = FakeMessage(message_id=601, text=answer, thread=thread)
    update = fake_update(message)
    assert run(botmod.receive_ask(update, context)) == ConversationHandler.END
    return asked, update


def test_rename_patches_migrates_and_follows_the_topic_title(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    state.set_turn_message(CHAT, "mgr-old", 55, thread=42)
    state.select(CHAT, "mgr-old")
    fake = FakeAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    bot = CtlBot()
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            asked, answered = ask_then_answer(
                cfg, state, fake, bot, "mgr-old", "tg-controls"
            )
    finally:
        run(fake.aclose())

    assert ("PATCH", "/api/sessions/mgr-old/config", {"rename": "tg-controls"}) in fake.patches()
    # every map moved with it…
    assert state.selected(CHAT) == "tg-controls"
    assert state.pinned(CHAT, "tg-controls") == PIN
    assert state.turn_message(CHAT, "tg-controls") == 55
    assert state.topic_session(CHAT, 42) == "tg-controls"
    assert state.topic_pin(CHAT, 42) == PIN
    # …the Telegram topic title followed, and the title sync let go of it…
    assert bot.edited_topics == [(CHAT, 42, "tg-controls")]
    assert state.topic_name(CHAT, 42) == "tg-controls"
    assert state.topic_created(CHAT, 42) is False
    assert any("session_renamed" in m and "source=rename_prompt" in m for m in caplog.messages)
    # …and the panel answered under the NEW name (the answer follows the
    # typed message, the same way every other answer does)
    assert "tg-controls" in answered.message.replies[-1]["text"]


def test_the_title_sync_cannot_flip_a_rename_back(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, topic_sync_s=60.0)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    fake = FakeAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    bot = CtlBot()
    try:
        ask_then_answer(cfg, state, fake, bot, "mgr-old", "tg-controls")
    finally:
        run(fake.aclose())

    app = ctl_app(cfg, state, fake, bot)
    bot.edited_topics.clear()

    async def titles(cwd):
        return {PIN: "OpenCode title"}  # the title the sync would have used

    monkeypatch.setattr(botmod, "read_titles", titles)
    run(botmod.sync_topic_titles_once(app))
    assert bot.edited_topics == []  # owner-named: the sync stays out of it


def test_rename_via_the_command_and_a_failed_rename(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)
    state.set_pinned(CHAT, "mgr-old", PIN)
    fake = FakeAmux(rows=[lane("mgr-old", flags=f"--session {PIN}")])
    bot = CtlBot()
    context = context_for(cfg, state, fake.client, bot, args=("tg-renamed",))
    update = fake_command("/name", thread=42, args=("tg-renamed",))
    try:
        run(botmod.name_command(update, context))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/mgr-old/config", {"rename": "tg-renamed"}) in fake.patches()
    assert state.topic_session(CHAT, 42) == "tg-renamed"

    # a refused rename changes NOTHING and says so
    fake2 = FakeAmux(rows=[lane("mgr-renamed")], config_status=500)
    state2 = GatewayState(tmp_path / "state2.json")
    state2.set_topic(CHAT, 42, "mgr-renamed", created=True)
    context2 = context_for(cfg, state2, fake2.client, CtlBot())
    asked = fake_callback("gw:ctlr:mgr-renamed")
    assert run(botmod.ask_rename(asked, context2)) == botmod.ASK
    with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
        run(
            botmod.receive_ask(
                fake_update(FakeMessage(message_id=602, text="nope", thread=42)),
                context2,
            )
        )
    run(fake2.aclose())
    assert state2.topic_session(CHAT, 42) == "mgr-renamed"  # untouched
    assert any("session_rename_failed" in m for m in caplog.messages)


def test_a_404_during_a_rename_does_not_release_the_topic(tmp_path, caplog):
    """The rename window: a message in flight must not free the topic."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-old", created=True)

    update = fake_update(FakeMessage(message_id=700, text="hi", thread=42))
    context = context_for(cfg, state, None, CtlBot())

    async def refused(name, text):
        raise botmod.AmuxError(404, "session not found")

    async def rows():
        return [lane("somebody-else")]

    context.application.bot_data["client"] = SimpleNamespace(
        send=refused, list_sessions=rows
    )
    context.application.bot_data["renaming"] = {(CHAT, "mgr-old"): "mgr-new"}
    with caplog.at_level(logging.INFO, logger="telegram_gateway"):
        run(botmod._report_send_error(update, context, "mgr-old", botmod.AmuxError(404, "x"), "text"))
    assert state.topic_session(CHAT, 42) == "mgr-old"  # NOT released
    assert any("send_deferred_rename" in m for m in caplog.messages)


# ---- slash commands --------------------------------------------------------


def test_ctl_command_targets_the_topic_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "mgr-x", created=True)
    fake = FakeAmux(rows=[lane("mgr-x")])
    update = fake_command("/ctl", thread=42)
    try:
        run(botmod.ctl_command(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert update.message.replies[0]["text"].startswith("🎛")


def test_ctl_command_falls_back_to_the_selection(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "chosen-one")
    fake = FakeAmux(rows=[lane("chosen-one")])
    update = fake_command("/ctl", forum=False)
    try:
        run(botmod.ctl_command(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert "chosen-one" in update.message.replies[0]["text"]


def test_ctl_command_without_any_session_says_so(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    fake = FakeAmux(rows=[])
    update = fake_command("/ctl", forum=False)
    try:
        run(botmod.ctl_command(update, context_for(cfg, state, fake.client, CtlBot())))
    finally:
        run(fake.aclose())
    assert "No session" in update.message.replies[0]["text"]


def test_model_command_with_an_id_applies_it_to_the_topic_session(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "claude-lane", created=True)
    fake = FakeAmux(rows=[lane("claude-lane", provider="claude", running=False)])
    update = fake_command("/model claude-opus-5", thread=42, args=("claude-opus-5",))
    context = context_for(cfg, state, fake.client, CtlBot(), args=("claude-opus-5",))
    try:
        run(botmod.model_command(update, context))
    finally:
        run(fake.aclose())
    assert ("PATCH", "/api/sessions/claude-lane/config", {"model": "claude-opus-5"}) in fake.patches()


def test_effort_command_validates_the_level(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.select(CHAT, "claude-lane")
    fake = FakeAmux(rows=[lane("claude-lane", provider="claude", running=False)])
    update = fake_command("/effort bogus", forum=False, args=("bogus",))
    context = context_for(cfg, state, fake.client, CtlBot(), args=("bogus",))
    try:
        run(botmod.effort_command(update, context))
    finally:
        run(fake.aclose())
    assert fake.patches() == []
    assert "unknown effort" in update.message.replies[0]["text"]


# ---- the ack button --------------------------------------------------------


def test_topic_ack_carries_the_control_button(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = CtlBot()
    message = FakeMessage(message_id=100, text="start something", thread=None)
    try:
        run(botmod.on_text(fake_update(message), context_for(cfg, state, client, bot)))
    finally:
        run(client.aclose())
    markup = message.replies[0]["reply_markup"]
    assert buttons(markup) == [f"gw:ctl:{created[0]}"]
    assert state.topic_session(CHAT, 1001) == created[0]


def test_message_in_an_unbound_topic_still_adopts_after_the_release(tmp_path):
    """The resolver never adopts; the released (unbound) topic still does."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    sent, created = [], []
    client = make_manager_amux(sent, created)
    bot = CtlBot()
    message = FakeMessage(message_id=210, text="make a manager", thread=777)
    try:
        run(botmod.on_text(fake_update(message), context_for(cfg, state, client, bot)))
    finally:
        run(client.aclose())
    assert created and state.topic_session(CHAT, 777) == created[0]
