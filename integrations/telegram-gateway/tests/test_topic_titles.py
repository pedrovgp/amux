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


def app_of(cfg, state, bot):
    return SimpleNamespace(bot_data={"cfg": cfg, "state": state}, bot=bot)


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


def test_a_renamed_topic_is_not_touched_again(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    bound_topic(state, name="Fix the deploy pipeline")
    bot = TitleBot()
    calls = fake_titles(monkeypatch, {PIN: "A later retitle"})

    run(botmod.sync_topic_titles_once(app_of(cfg, state, bot)))

    assert bot.renames == []
    assert calls == [], "recorded name != session name -> one rename per topic"
    assert state.topic_name(CHAT, 42) == "Fix the deploy pipeline"


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
