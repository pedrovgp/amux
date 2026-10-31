"""General-manager workers (Milestone 11, owner ask 2026-10-30).

The owner's invariant: *"For each allowed user_id in the .env, there should
be a general-mgr-<userid> worker, with initial prompt, skills and notes
appropriate for this management tasks."*

The contract under test:

1. `ensure_general_managers` CREATES a missing dispatcher — pinned, from the
   root projects dir, briefed with the packaged `general_mgr_prompt.md`
   (saved as standing instructions) and STARTED — and never touches an
   existing one (create-only: re-briefing would disturb a live conversation);
2. the `dispatch` SKILL ships in the package and is installed content-
   idempotently into opencode's user-global skill dir (tests point it at tmp);
3. amux being down, or a refused create, WARNs `general_mgr_ensure_failed`
   with uid + status + detail and returns — provisioning must never take the
   gateway down, and the hourly tick retries.
"""
from types import SimpleNamespace

import httpx
import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.amux import AmuxClient
from telegram_gateway.manager import general_manager_name

from telegram_gateway.state import GatewayState

from test_fresh_session import ResetAmux
from test_session_control import CtlBot, lane
from test_topics import (
    CHAT,
    FakeMessage,
    fake_context,
    fake_update,
    make_cfg,
    run,
)


def gm_app(cfg, client):
    return SimpleNamespace(bot_data={"cfg": cfg, "client": client})


@pytest.fixture(autouse=True)
def _clean_gm_seen():
    botmod._general_mgr_seen.clear()
    yield
    botmod._general_mgr_seen.clear()


# ---- 1. the invariant ------------------------------------------------------


def test_a_missing_dispatcher_is_created_briefed_and_started(tmp_path, caplog):
    cfg = make_cfg(tmp_path)  # allowed_user_ids = {1}
    amux = ResetAmux()
    app = gm_app(cfg, amux.client)
    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            created = run(
                botmod.ensure_general_managers(app, skill_root=tmp_path / "skills")
            )
    finally:
        run(amux.aclose())

    assert created == ["general-mgr-1"]
    (body,) = amux.created_bodies
    assert body["name"] == "general-mgr-1"
    assert body["provider"] == "opencode"  # the fleet default manager provider
    assert body["dir"] == cfg.manager_cwd  # the root projects dir
    assert body["flags"].startswith("--session ses_tg")  # pinned: revive + identity
    assert body["start"] is False  # brief BEFORE the first start

    # the DISPATCHER prompt was saved as standing instructions…
    instructions = [
        c for c in amux.calls
        if c[0] == "POST" and c[1] == "/api/sessions/general-mgr-1/instructions"
    ]
    assert len(instructions) == 1
    text = instructions[0][2]["instructions"]
    assert "dispatcher" in text and "— answer ——" in text
    # …and it is the packaged file, not the per-conversation manager's
    packaged = cfg.general_mgr_instructions_file.read_text(encoding="utf-8")
    assert text == packaged.strip()

    # …then it was started
    assert any(
        c[0] == "POST" and c[1] == "/api/sessions/general-mgr-1/start"
        for c in amux.calls
    )
    assert any("general_mgr_ensured" in m for m in caplog.messages)


def test_an_existing_dispatcher_is_left_alone(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    amux = ResetAmux(rows=[lane("general-mgr-1")])
    app = gm_app(cfg, amux.client)
    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            assert run(botmod.ensure_general_managers(app, skill_root=tmp_path / "s")) == []
            assert run(botmod.ensure_general_managers(app, skill_root=tmp_path / "s")) == []
    finally:
        run(amux.aclose())
    assert amux.created_bodies == []
    # present is logged ONCE per process, not once per hourly tick
    assert len([m for m in caplog.messages if "general_mgr_present" in m]) == 1


def test_one_dispatcher_per_allowed_owner(tmp_path):
    cfg = make_cfg(tmp_path, allowed_user_ids=frozenset({1, 2}))
    amux = ResetAmux()
    try:
        created = run(
            botmod.ensure_general_managers(gm_app(cfg, amux.client),
                                           skill_root=tmp_path / "s")
        )
    finally:
        run(amux.aclose())
    assert created == ["general-mgr-1", "general-mgr-2"]
    assert general_manager_name(42) == "general-mgr-42"


# ---- 2. it must never take the gateway down --------------------------------


def test_amux_being_down_warns_and_returns(tmp_path, caplog):
    cfg = make_cfg(tmp_path)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = AmuxClient("https://localhost:8824", transport=httpx.MockTransport(refuse))
    try:
        with caplog.at_level("WARNING", logger="telegram_gateway"):
            assert run(
                botmod.ensure_general_managers(gm_app(cfg, client),
                                               skill_root=tmp_path / "s")
            ) == []
    finally:
        run(client.aclose())
    assert any("general_mgr_ensure_failed" in m and "status=0" in m
               for m in caplog.messages)


def test_a_refused_create_is_named_and_skipped(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    backing = ResetAmux()

    def failing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/sessions":
            return httpx.Response(500, json={"error": "boom"})
        return backing._handle(request)

    client = AmuxClient("https://localhost:8824", transport=httpx.MockTransport(failing))
    try:
        with caplog.at_level("WARNING", logger="telegram_gateway"):
            assert run(
                botmod.ensure_general_managers(gm_app(cfg, client),
                                               skill_root=tmp_path / "s")
            ) == []
    finally:
        run(client.aclose())
    warn = [m for m in caplog.messages if "general_mgr_ensure_failed" in m]
    assert warn and "status=500" in warn[0] and "general-mgr-1" in warn[0]


# ---- 3. the skill ships and installs content-idempotently ------------------


def test_the_dispatch_skill_installs_idempotently(tmp_path):
    source = tmp_path / "SKILL.md"
    source.write_text("---\nname: dispatch\ndescription: x\n---\nbody\n", encoding="utf-8")
    root = tmp_path / "opencode-skills"

    assert botmod.install_general_mgr_skill(source=source, dest_root=root) == "installed"
    dest = root / "dispatch" / "SKILL.md"
    assert dest.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    # identical content → nothing rewritten
    assert botmod.install_general_mgr_skill(source=source, dest_root=root) == "fresh"
    # drifted/edited on the machine → restored from the package
    dest.write_text("drifted", encoding="utf-8")
    assert botmod.install_general_mgr_skill(source=source, dest_root=root) == "installed"
    assert dest.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_the_packaged_prompt_and_skill_exist(tmp_path):
    """The artifacts are part of the package — a missing one is a build bug,
    caught here instead of a dispatcher arriving unbriefed or skillless."""
    source = botmod._general_mgr_skill_source()
    assert source.name == "SKILL.md" and source.exists()
    skill = source.read_text(encoding="utf-8")
    assert skill.startswith("---") and "name: dispatch" in skill

    prompt = make_cfg(tmp_path).general_mgr_instructions_file
    assert prompt.name == "general_mgr_prompt.md" and prompt.exists()
    text = prompt.read_text(encoding="utf-8")
    assert "general manager" in text and "— answer ——" in text
    assert "chat_id" in text and "@homuxBot" in text  # the group-creation ask
    assert "suggest" in text  # when to suggest deleting a topic


def test_config_default_points_at_the_packaged_prompt(tmp_path):
    cfg = make_cfg(tmp_path)
    assert cfg.general_mgr_instructions_file.name == "general_mgr_prompt.md"
    assert cfg.general_mgr_instructions_file.exists()


# ---- 11.5: forum General routes to the dispatcher --------------------------


def _dispatcher_row(**kw):
    return lane("general-mgr-1", flags="--session ses_gm1", **kw)


def test_forum_general_text_routes_to_the_dispatcher(tmp_path, caplog):
    """The owner's ruling: a forum-General message mints NOTHING — it goes to
    the sender's own dispatcher, anchored in General (`route=dispatcher`)."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = ResetAmux(rows=[_dispatcher_row()])
    bot = CtlBot()
    message = FakeMessage(message_id=100, text="dispatch this", thread=None)

    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(
                botmod.on_text(
                    fake_update(message),
                    fake_context(cfg, amux.client, state, bot),
                )
            )
    finally:
        run(amux.aclose())

    assert amux.created_bodies == []  # no lane minted
    assert bot.created == []  # no topic minted
    assert message.replies == []  # no ack: the answer is the reply
    assert (
        "POST",
        "/api/sessions/general-mgr-1/send",
        {"text": "[from Telegram @owner] dispatch this"},
    ) in amux.calls
    # a RUNNING dispatcher is never spuriously started (`is False` guard)
    assert not any(c[0] == "POST" and c[1].endswith("/start") for c in amux.calls)
    # anchored in General: thread None, on the dispatcher itself
    assert state.turn_message(CHAT, "general-mgr-1") == 100
    assert state.turn_thread(CHAT, "general-mgr-1") is None
    assert any(
        "prompt_text" in m and "route=dispatcher" in m for m in caplog.messages
    )


def test_forum_general_creates_the_dispatcher_on_first_message(tmp_path, caplog):
    """Create-on-first-use: the gateway restarted while amux was down, the
    hourly ensure has not run — the first message heals the ring itself."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = ResetAmux()  # no rows: the dispatcher does not exist yet
    bot = CtlBot()
    message = FakeMessage(message_id=100, text="hello", thread=None)

    try:
        with caplog.at_level("INFO", logger="telegram_gateway"):
            run(
                botmod.on_text(
                    fake_update(message),
                    fake_context(cfg, amux.client, state, bot),
                )
            )
    finally:
        run(amux.aclose())

    (body,) = amux.created_bodies
    assert body["name"] == "general-mgr-1"
    assert body["flags"].startswith("--session ses_tg")  # pinned
    assert any(
        c[0] == "POST" and c[1] == "/api/sessions/general-mgr-1/instructions"
        for c in amux.calls
    )  # briefed before its first turn
    assert any(
        "POST" in c and c[1] == "/api/sessions/general-mgr-1/send"
        for c in amux.calls
    )
    assert any(
        "general_mgr_ensured" in m and "source=first_message" in m
        for m in caplog.messages
    )
    assert bot.created == []  # still no topic


def test_private_chats_keep_manager_mode(tmp_path):
    """M11.5 is forum-scoped: a private chat has no dispatcher surface, so a
    plain message still mints its own manager (the M6 contract)."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    amux = ResetAmux()
    bot = CtlBot()
    message = FakeMessage(message_id=100, text="hello", thread=None)

    try:
        run(
            botmod.on_text(
                fake_update(message, forum=False),
                fake_context(cfg, amux.client, state, bot),
            )
        )
    finally:
        run(amux.aclose())

    assert amux.created_bodies and amux.created_bodies[0]["name"].startswith("mgr-")
    assert not any(
        "general-mgr" in str(c[1]) for c in amux.calls
    )  # the dispatcher is never touched
    assert message.replies  # the private "Starting … reply here" ack
