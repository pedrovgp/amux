"""amux's `steering queue STALLED` WARN reaches the affected topic.

The 2026-11-07 incident: amux logged `steering queue STALLED — messages are
not reaching this lane session=server-migration` while the owner's 09:37 voice
message waited in the queue, and that WARN reached nobody — it lives in
`~/.amux/logs/server-rs.log`, the owner lives in Telegram, and the topic said
nothing.

The contract:

1. the reader (`stallwatch.StallWatch`) tails the log with an offset: the
   first sight of a file starts at its END (a gateway restart never replays
   history as news), only COMPLETE lines are parsed, and a truncated/rotated
   file restarts from the top;
2. each NEW stall line for a session bound to a topic posts ONE line into that
   topic (`stall_notice_sent`), and a session with no topic is logged and
   skipped rather than guessed (`stall_notice_skipped reason=no_topic`);
3. the watch is spawned by `post_init` and cancelled by `post_shutdown`.
"""
import logging
from types import SimpleNamespace

import pytest

from telegram_gateway import bot as botmod
from telegram_gateway.stallwatch import STALL_MARKER, StallWatch
from telegram_gateway.state import GatewayState

from test_topics import CHAT, FakeBot, make_cfg, run

SESSION = "server-migration"
THREAD = 738


@pytest.fixture(autouse=True)
def _clean_stall_dedupe():
    """The row-dedupe maps are module state; every test starts bare."""
    botmod._stall_notified.clear()
    botmod._stall_lanes_failed.clear()
    botmod._stall_seeded = False
    yield
    botmod._stall_notified.clear()
    botmod._stall_lanes_failed.clear()
    botmod._stall_seeded = False


def line(session, stamp="2026-11-07T22:18:15.514793Z"):
    return (
        f"{stamp}  WARN amux_server::api::session_verbs: {STALL_MARKER} — "
        f"messages are not reaching this lane session={session} queued=2 "
        "oldest_min=10 last_skip=not-at-turn-boundary (within max age) "
        "blocked_now=busy-past-deadline\n"
    )


def log_file(tmp_path, *lines):
    path = tmp_path / "server-rs.log"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def app_for(tmp_path, state, bot, *, lines=(), append=()):
    """cfg + app whose stall watch already saw `lines` (so they are HISTORY)."""
    path = log_file(tmp_path, *lines, *append)
    cfg = make_cfg(tmp_path, amux_server_log=path, state_dir=tmp_path / "gw")
    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "cfg": cfg,
            "state": state,
            "stall_watch": StallWatch(cfg.amux_server_log),
        },
    )
    return app, path


# ---- the reader -----------------------------------------------------------


def test_history_is_never_replayed(tmp_path):
    path = log_file(tmp_path, line(SESSION))
    watch = StallWatch(path)  # constructed AFTER the line: it is history
    assert watch.read_new() == []

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line(SESSION, stamp="2026-11-07T22:19:00.000000Z"))
    stalls = watch.read_new()
    assert [stall.session for stall in stalls] == [SESSION]
    assert watch.read_new() == [], "one line, one read"


def test_the_reason_fields_are_parsed_with_their_spaces(tmp_path):
    """`last_skip=send-refused: composer` has a SPACE in its value."""
    path = log_file(tmp_path)
    watch = StallWatch(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            "2026-11-25T23:00:03.440682Z  WARN amux_server::api::session_verbs: "
            f"{STALL_MARKER} — messages are not reaching this lane session=ocusage "
            "queued=1 oldest_min=11 last_skip=send-refused: composer "
            "blocked_now=composer-draft\n"
        )
    (stall,) = watch.read_new()
    assert stall.session == "ocusage"
    assert stall.last_skip == "send-refused: composer"
    assert stall.blocked_now == "composer-draft"


def test_a_stall_without_reason_fields_is_still_a_stall(tmp_path):
    path = log_file(tmp_path)
    watch = StallWatch(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            f"2026-11-07T22:20:00.000000Z  WARN x: {STALL_MARKER} "
            "session=plain queued=1\n"
        )
    (stall,) = watch.read_new()
    assert stall.last_skip == "" and stall.blocked_now == ""


def test_a_torn_line_is_not_parsed_until_its_newline_arrives(tmp_path):
    path = log_file(tmp_path)
    watch = StallWatch(path)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line(SESSION)[:-1])  # no newline yet
    assert watch.read_new() == [], "half a line has no trustworthy session="

    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n")
    assert [stall.session for stall in watch.read_new()] == [SESSION]


def test_a_rotated_log_starts_from_the_top(tmp_path):
    """logrotate renames the old file away: a NEW inode at this path is not
    history, whatever its size happens to be."""
    path = log_file(tmp_path, line("old", stamp="2026-11-07T01:00:00.000000Z"))
    watch = StallWatch(path)
    assert watch.read_new() == []

    path.unlink()  # the rename half of a rotation
    path.write_text(line(SESSION) * 3, encoding="utf-8")  # longer than before
    assert [stall.session for stall in watch.read_new()] == [SESSION, SESSION, SESSION]
    assert watch.read_new() == []


def test_a_truncated_log_restarts_from_the_top(tmp_path):
    """truncate in place (same inode, smaller size): read from 0, not past EOF."""
    path = log_file(tmp_path, line("old", stamp="2026-11-07T01:00:00.000000Z") * 5)
    watch = StallWatch(path)
    assert watch.read_new() == []

    path.write_text(line(SESSION), encoding="utf-8")  # shorter than the offset
    assert [stall.session for stall in watch.read_new()] == [SESSION]


def test_a_missing_log_is_reported_once_not_every_tick(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    watch = StallWatch(tmp_path / "not-there-yet.log")

    assert watch.read_new() == []
    assert watch.read_new() == []
    assert caplog.text.count("stall_log_unavailable") == 1

    # it appears later: reading resumes from the top of the fresh file, and the
    # unavailable warning stops (the `_missing_logged` reset)
    path = tmp_path / "not-there-yet.log"
    path.write_text(line(SESSION), encoding="utf-8")
    assert [stall.session for stall in watch.read_new()] == [SESSION]
    assert caplog.text.count("stall_log_unavailable") == 1


# ---- the write half: into the topic ---------------------------------------


def test_a_new_stall_reaches_the_topic_once(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, THREAD, SESSION)
    bot = FakeBot()
    # the 22:00 stall is HISTORY (the watch is constructed after it)
    app, path = app_for(tmp_path, state, bot, lines=[line(SESSION)])

    run(botmod.stall_watch_once(app))
    assert bot.sent == [], "history must never be paged as news"

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line(SESSION, stamp="2026-11-07T22:18:15.514793Z"))
    run(botmod.stall_watch_once(app))

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == CHAT
    assert bot.sent[0]["thread"] == THREAD
    assert SESSION in bot.sent[0]["text"]
    assert "STALLED" in bot.sent[0]["text"]
    assert "stall_notice_sent" in caplog.text

    run(botmod.stall_watch_once(app))  # nothing new
    assert len(bot.sent) == 1, "one line per NEW stall line"


def test_the_topic_notice_names_the_reason(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, THREAD, SESSION)
    bot = FakeBot()
    app, path = app_for(tmp_path, state, bot)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line(SESSION).replace(
            "last_skip=not-at-turn-boundary (within max age)",
            "last_skip=send-refused: composer",
        ).replace("blocked_now=busy-past-deadline", "blocked_now=not-running"))
    run(botmod.stall_watch_once(app))

    assert len(bot.sent) == 1
    text = bot.sent[0]["text"]
    assert "last_skip=send-refused: composer" in text
    assert "blocked_now=not-running" in text
    assert "reason=last_skip=send-refused: composer" in caplog.text

    # A line with no reason fields still posts, honestly labelled unknown.
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            f"2026-11-07T23:00:00.000000Z  WARN x: {STALL_MARKER} "
            f"session={SESSION} queued=1\n"
        )
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 2
    assert "Reason: unknown (not logged)" in bot.sent[1]["text"]


def test_a_stall_for_an_unbound_session_is_skipped_and_logged(tmp_path, caplog):
    """No topic binding means no destination — count it, never guess one."""
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    state = GatewayState(tmp_path / "state.json")  # knows nothing about "ghost"
    bot = FakeBot()
    app, path = app_for(tmp_path, state, bot)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line("ghost"))
    run(botmod.stall_watch_once(app))

    assert bot.sent == []
    assert "stall_notice_skipped" in caplog.text
    assert "session=ghost" in caplog.text
    assert "reason=no_topic" in caplog.text


def test_a_stall_line_without_a_session_is_counted_not_guessed(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    path = log_file(tmp_path)
    watch = StallWatch(path)

    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            "2026-11-07T22:20:00.000000Z  WARN amux_server::api::session_verbs: "
            f"{STALL_MARKER} — messages are not reaching this lane queued=1\n"
        )

    assert watch.read_new() == []
    assert "stall_line_unattributed" in caplog.text


# ---- wiring ----------------------------------------------------------------


def test_post_init_spawns_the_stall_task_and_shutdown_closes_it(tmp_path, caplog):
    import httpx

    from telegram_gateway.amux import AmuxClient
    from telegram_gateway.bot import build_application, post_init, post_shutdown

    log_path = log_file(tmp_path)  # empty, and ISOLATED from the live log
    cfg = make_cfg(
        tmp_path,
        amux_server_log=log_path,
        state_dir=tmp_path / "gw-state",
        control_port=0,
    )
    app = build_application(cfg)
    app.bot_data["client"] = AmuxClient(
        "https://localhost:1",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )

    async def scenario():
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            await post_init(app)
            task = app.bot_data.get("stall_task")
            watch = app.bot_data.get("stall_watch")
            await post_shutdown(app)
        return task, watch

    task, watch = run(scenario())

    assert task is not None and task.done(), "spawned, then cancelled on shutdown"
    assert isinstance(watch, StallWatch)
    assert watch.path == log_path
    assert "gateway_started" in caplog.text
    assert "stall_watch_s=60" in caplog.text
