"""Meaningful topic names: a gateway-made topic shows its OpenCode title.

Owner, 2026-10-27: "use meaningful names for topics — it should match the
session name in opencode". A topic is born `mgr-…` (the lane name) while
OpenCode titles the same conversation from its content; the topic follows.

What is under contract here:

1. the reader turns `opencode session list` into id -> title (pure, injectable);
2. only a PINNED session on a GATEWAY-MINTED topic whose recorded name is still
   the session name is renamed — your hand-made names are never touched;
3. a stub title ("Untitled session") or a missing one keeps waiting;
4. the rename is recorded (the only record Telegram gives us) and is therefore
   ONE-SHOT: a second run has nothing to do.
"""
import logging
from types import SimpleNamespace

import pytest
from telegram.error import TelegramError

from telegram_gateway import bot as botmod
from telegram_gateway.state import GatewayState
from telegram_gateway.topic_titles import (
    MAX_TOPIC_TITLE,
    clean_title,
    parse_session_list,
    read_titles,
)

from test_session_control import NoOc
from test_topics import CHAT, make_cfg, run

PIN = "ses_tg10270000abcdef"
SESSION = "mgr-1027-1536-8b4e"
LISTING = (
    "ses_tg10270000abcdef\tFix the deploy pipeline\t10/27/2026, 9:00:00 AM\n"
    "ses_other\tUntitled session\t10/27/2026, 9:05:00 AM\n"
    "not-a-session-row\tjunk\n"
    "\n"
)



@pytest.fixture(autouse=True)
def _clean_rename_dedupe():
    botmod._rename_warned.clear()
    yield
    botmod._rename_warned.clear()


class TitleBot:
    def __init__(self, fail=False):
        self.renames = []
        self.fail = fail

    async def edit_forum_topic(self, chat_id, message_thread_id=None, name=None, **kw):
        if self.fail:
            raise TelegramError("Bad Request: test refusal")
        self.renames.append((chat_id, message_thread_id, name))
        return True


def app_of(cfg, state, bot, oc=None):
    """The sync's app shape. `oc` defaults to NoOc — the title source must
    NEVER fall through to the real shared service in tests (M11.8: titles
    prefer `GET /api/session`, and NoOc makes that leg fail fast into the
    monkeypatched `read_titles` fallback)."""
    return SimpleNamespace(
        bot_data={"cfg": cfg, "state": state, "oc_api": oc or NoOc()}, bot=bot
    )


def fake_titles(monkeypatch, titles):
    calls = []

    async def _read(cwd):
        calls.append(cwd)
        return titles

    monkeypatch.setattr(botmod, "read_titles", _read)
    return calls


def bound_topic(state, *, created=True, name=None, pin=PIN):
    """A gateway-minted topic bound to a pinned session, as after turn 1."""
    state.set_topic(CHAT, 42, SESSION, created=created)
    state.set_pinned(CHAT, SESSION, pin)
    state.set_turn_message(CHAT, SESSION, 5, thread=42)
    if name is not None:
        state.set_topic_name(CHAT, 42, name)


# ---- the reader ----------------------------------------------------------


def test_parse_session_list_maps_ids_to_titles():
    parsed = parse_session_list(LISTING)
    assert parsed["ses_tg10270000abcdef"] == "Fix the deploy pipeline"
    assert parsed["ses_other"] == "Untitled session"
    assert "not-a-session-row" not in parsed  # non-tabbed lines are not rows


def test_parse_session_list_keeps_the_first_row_for_an_id():
    text = "ses_a\tFirst title\tday1\nses_a\tSecond title\tday2\n"
    assert parse_session_list(text)["ses_a"] == "First title"


def test_clean_title_flattens_clamps_and_rejects_stubs():
    assert clean_title(None) is None
    assert clean_title("") is None
    assert clean_title("Untitled session") is None
    assert clean_title("untitled session") is None  # case-insensitive
    assert clean_title("12345") is None  # a bare number is not a name
    assert clean_title("  spaced \n over\tlines ") == "spaced over lines"
    long = "x" * 300
    assert len(clean_title(long)) == MAX_TOPIC_TITLE


def test_read_titles_uses_the_injected_runner_and_survives_a_failure():
    async def ok(cwd):
        return LISTING

    async def boom(cwd):
        raise RuntimeError("opencode missing")

    assert run(read_titles("/w", runner=ok))["ses_tg10270000abcdef"] == (
        "Fix the deploy pipeline"
    )
    assert run(read_titles("/w", runner=boom)) == {}  # a nicety: never fatal


# ---- state: minted vs adopted -------------------------------------------


def test_created_flag_separates_a_minted_topic_from_an_adopted_one(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_topic(CHAT, 42, SESSION, created=True)      # General path
    state.set_topic(CHAT, 43, SESSION)                    # adopted / POST /topic
    assert state.topic_created(CHAT, 42) is True
    assert state.topic_created(CHAT, 43) is False
    assert state.topic_name(CHAT, 42) == SESSION          # falls back to session

    state.set_topic_name(CHAT, 42, "Fix the deploy pipeline")
    again = GatewayState(path)
    assert again.topic_name(CHAT, 42) == "Fix the deploy pipeline"
    assert again.topic_created(CHAT, 42) is True  # rename keeps the flag


def test_releasing_a_session_clears_its_created_flag(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, SESSION, created=True)
    state.drop_session_topics(CHAT, SESSION)
    assert state.topic_created(CHAT, 42) is False


# ---- the sync ------------------------------------------------------------


def test_a_minted_topic_is_renamed_to_its_opencode_title(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot()
    calls = fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == [(CHAT, 42, "Fix the deploy pipeline")]
    assert state.topic_name(CHAT, 42) == "Fix the deploy pipeline"
    assert state.topic_session(CHAT, 42) == SESSION  # binding untouched
    assert calls == [cfg.manager_cwd]
    assert "topic_renamed" in caplog.text and "source=opencode_title" in caplog.text


def test_a_topic_you_named_by_hand_is_never_touched(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state, created=False)  # adopted: recorded name falls back too
    bot = TitleBot()
    calls = fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == []
    assert calls == [], "no candidate means no process is spawned at all"
    assert "topic_renamed" not in caplog.text


def test_a_topic_is_renamed_once_then_never_tracked(tmp_path, monkeypatch):
    """ONE rename per topic (11.8): a successful sync SPENDS the flag, so a
    later retitle is not followed and no reader runs while nothing is pending.
    The old test's manual state (flag set + a foreign recorded name) is one
    the code no longer produces: that combination now means a dispatcher
    topic that has never been titled."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot()
    titles = {PIN: "First title"}
    calls = fake_titles(monkeypatch, titles)

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))
    assert bot.renames == [(CHAT, 42, "First title")]
    assert state.topic_created(CHAT, 42) is False  # flag spent by the rename

    # OpenCode re-titles the conversation later: the topic must NOT follow
    titles[PIN] = "A later retitle"
    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))
    assert bot.renames == [(CHAT, 42, "First title")]  # still just the one
    assert len(calls) == 1, "flag spent -> no candidate -> no reader at all"
    assert state.topic_name(CHAT, 42) == "First title"


def test_it_waits_for_a_real_title_instead_of_renaming_to_a_stub(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot()
    fake_titles(monkeypatch, {PIN: "Untitled session"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == []
    assert state.topic_name(CHAT, 42) == SESSION  # still eligible for later
    assert state.topic_created(CHAT, 42) is True


def test_a_title_longer_than_telegram_allows_is_clamped(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot()
    fake_titles(monkeypatch, {PIN: "y" * 300})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames and len(bot.renames[0][2]) == MAX_TOPIC_TITLE


def test_a_failed_rename_warns_once_and_stays_eligible(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot(fail=True)
    fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))
    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert caplog.text.count("topic_rename_failed") == 1  # one WARN per stretch
    assert state.topic_name(CHAT, 42) == SESSION  # untouched -> retried next tick


def test_the_sync_is_off_when_the_knob_is_zero(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, topic_sync_s=0.0)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state)
    bot = TitleBot()
    calls = fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert calls == [] and bot.renames == []


def test_an_unpinned_session_has_no_title_source_and_is_skipped(tmp_path, monkeypatch):
    """Legacy / non-opencode lanes: no pin, no title, never renamed."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, SESSION, created=True)
    state.set_turn_message(CHAT, SESSION, 5, thread=42)  # tracked, but unpinned
    bot = TitleBot()
    calls = fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == [] and calls == []


# ---- 11.8: dispatcher topics follow the worker's title ---------------------


def test_a_dispatcher_topic_is_born_with_the_task_then_titled_once(
    tmp_path, monkeypatch
):
    """The owner's "I want" (2026-10-30): a POST /topic topic is BORN with
    the dispatcher's task name and renamed ONCE to the worker's OpenCode
    title — the control path creates it with `created=True` for exactly
    this, and the rename spends the flag."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state, name="fix the red suite")  # birth name = the task
    bot = TitleBot()
    fake_titles(monkeypatch, {PIN: "Fixing the flaky e2e suite"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == [(CHAT, 42, "Fixing the flaky e2e suite")]
    assert state.topic_name(CHAT, 42) == "Fixing the flaky e2e suite"
    assert state.topic_created(CHAT, 42) is False  # one rename, spent
    assert state.topic_session(CHAT, 42) == SESSION  # binding untouched


def test_titles_come_from_the_shared_service_first(tmp_path, monkeypatch, caplog):
    """M11.8's title source: `GET /api/session` on the shared serve — the ONLY
    path that sees workers spawned in OTHER projects (measured50/15). The
    cwd reader must not even run when the service answers."""
    from test_opencode_api import FakeOc

    caplog.set_level(logging.INFO, logger="telegram_gateway")
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state, name="fix the red suite")
    bot = TitleBot()
    oc = FakeOc(sessions=[{"id": PIN, "title": "Fixing the flaky e2e suite"}])
    calls = fake_titles(monkeypatch, {PIN: "a cwd title must never win"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot, oc=oc)))

    assert bot.renames == [(CHAT, 42, "Fixing the flaky e2e suite")]
    assert calls == [], "the shared service answered — no subprocess spawned"
    assert ("sessions",) in oc.calls  # GET /api/session is what named it
    assert "via=api" in caplog.text


def test_a_birth_name_equal_to_the_title_releases_without_a_pointless_edit(
    tmp_path, monkeypatch
):
    """recorded == title IS the done state: release the flag (so the topic
    stops being read every tick) and never attempt an edit Telegram would
    reject as unmodified."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state, name="Fix the deploy pipeline")
    bot = TitleBot()
    fake_titles(monkeypatch, {PIN: "Fix the deploy pipeline"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == []  # nothing to do
    assert state.topic_created(CHAT, 42) is False  # released, not re-read
    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))  # and stays done
    assert bot.renames == []
