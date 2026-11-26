"""ONE notice per undelivered steering row — not one per hour.

Owner, 2026-11-26, while working in his own terminal: *"no tópico … eu continuo
recebendo mensagem de que está stalled"*. Live state behind that report: two GM
boot-delivery rows from 2026-11-25 19:48 (-03) on lanes whose opencode process
was suspended in the pane, so `lane_block_reason=not-running` was measurably
true and `steer_dead_letter_verdict` deliberately never reaps `not-running`
(the 2026-08-19 counter-incident). amux therefore re-WARNs hourly and the
gateway re-paged hourly: ten notices each, for one message each.

The contract:

1. the ROW is the identity (`last_skip_id` from `/api/debug/steering`): the
   same row reported again is suppressed with `stall_notice_suppressed`, and a
   different row pages again;
2. an UNMEASURED queue never becomes hourly spam: the condition
   (`last_skip`+`blocked_now`) is the key, and a changed condition pages;
3. a drained queue logs `stall_notice_cleared` and forgets the key, so the next
   genuine stall is not suppressed;
4. a stopped lane's notice says so ("NOT RUNNING … waits until the lane
   starts"), because the generic "its terminal shows what it is blocked on" is
   false for a shell prompt and is what read as "something is strange".
"""
import logging
import time
from types import SimpleNamespace

from telegram_gateway import bot as botmod
from telegram_gateway.stallwatch import STALL_MARKER, StallWatch
from telegram_gateway.state import GatewayState

from test_topics import CHAT, FakeBot, make_cfg, run

SESSION = "dedupe-lane"
THREAD = 741
ROW = "boot-delivery:dedupe-lane:1795635401:Owner:abc"


def line(*, queued=1, oldest_min=11, last_skip="not-running", blocked="not-running",
         stamp="2026-11-26T12:00:00.000000Z"):
    tail = f"queued={queued} oldest_min={oldest_min}"
    if last_skip:
        tail += f" last_skip={last_skip}"
    if blocked:
        tail += f" blocked_now={blocked}"
    return (
        f"{stamp}  WARN amux_server::api::session_verbs: {STALL_MARKER} — "
        f"messages are not reaching this lane session={SESSION} {tail}\n"
    )


class Queue:
    """The injected amux client: answers `/api/debug/steering` as data."""

    def __init__(self, lanes=None):
        self.lanes = lanes  # None == measured:false
        self.calls = 0

    async def steering_lanes(self):
        self.calls += 1
        return self.lanes


def lanes(*, row=ROW, queued=1, blocked="not-running", oldest_age_s=None):
    lane = {"session": SESSION, "queued": queued, "blocked_reason": blocked}
    if row is not None:
        lane["last_skip_id"] = row
    if oldest_age_s is not None:
        lane["oldest_age_s"] = oldest_age_s
    return {SESSION: lane}


def app_for(tmp_path, state, bot, queue):
    log = tmp_path / "server-rs.log"
    log.write_text("", encoding="utf-8")
    cfg = make_cfg(tmp_path, amux_server_log=log, state_dir=tmp_path / "gw")
    app = SimpleNamespace(
        bot=bot,
        bot_data={
            "cfg": cfg,
            "state": state,
            "client": queue,
            "stall_watch": StallWatch(cfg.amux_server_log),
        },
    )
    return app, log


def append(log, text):
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(text)


def state_with_topic(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, THREAD, SESSION)
    return state


def clear_dedupe():
    botmod._stall_notified.clear()
    botmod._stall_lanes_failed.clear()
    botmod._stall_seeded = False
    botmod._stall_process_started = time.monotonic()


# ---- one row, one notice ---------------------------------------------------


def test_the_same_stuck_row_is_reported_once_however_many_hours_pass(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    clear_dedupe()
    state, bot, queue = state_with_topic(tmp_path), FakeBot(), Queue(lanes())
    app, log = app_for(tmp_path, state, bot, queue)

    append(log, line(oldest_min=11))
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 1

    # one hour later: SAME row, the age grew, amux warns again
    append(log, line(oldest_min=71, stamp="2026-11-26T13:00:00.000000Z"))
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 1, "the same undelivered message must not re-page"
    assert "stall_notice_suppressed" in caplog.text
    assert f"session={SESSION}" in caplog.text


def test_a_new_row_pages_again(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    clear_dedupe()
    state, bot = state_with_topic(tmp_path), FakeBot()
    queue = Queue(lanes())
    app, log = app_for(tmp_path, state, bot, queue)

    append(log, line())
    run(botmod.stall_watch_once(app))

    queue.lanes = lanes(row="boot-delivery:dedupe-lane:1795639999:Owner:def")
    append(log, line(oldest_min=1, stamp="2026-11-26T14:00:00.000000Z"))
    run(botmod.stall_watch_once(app))

    assert len(bot.sent) == 2, "a different stuck message is news"


def test_an_unmeasured_queue_pages_per_condition_not_per_hour(tmp_path, caplog):
    """No measured queue → the condition is the key (never an hourly repeat)."""
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    clear_dedupe()
    state, bot, queue = state_with_topic(tmp_path), FakeBot(), Queue(None)
    app, log = app_for(tmp_path, state, bot, queue)

    append(log, line())
    run(botmod.stall_watch_once(app))
    append(log, line(oldest_min=71, stamp="2026-11-26T13:00:00.000000Z"))
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 1

    # the lane's block CHANGED (a composer draft appeared): that is news
    append(log, line(last_skip="send-refused: composer", blocked="composer-draft",
                     stamp="2026-11-26T14:00:00.000000Z"))
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 2


# ---- the loop closes -------------------------------------------------------


def test_a_drained_queue_clears_the_key_so_the_next_stall_pages(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    clear_dedupe()
    state, bot = state_with_topic(tmp_path), FakeBot()
    queue = Queue(lanes())
    app, log = app_for(tmp_path, state, bot, queue)

    append(log, line())
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 1

    # the row delivered: the next tick must notice the recovery (log only)
    queue.lanes = {}
    run(botmod.stall_watch_once(app))
    assert "stall_notice_cleared" in caplog.text
    assert f"session={SESSION}" in caplog.text
    assert len(bot.sent) == 1, "a recovery that needs nothing from the owner is log-only"

    # and the SAME row appearing again is no longer suppressed
    queue.lanes = lanes()
    append(log, line(stamp="2026-11-26T15:00:00.000000Z"))
    run(botmod.stall_watch_once(app))
    assert len(bot.sent) == 2


def test_an_unreadable_queue_keeps_the_dedupe_and_warns_once(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="telegram_gateway")
    clear_dedupe()

    class Broken(Queue):
        async def steering_lanes(self):
            raise RuntimeError("connection refused")

    state, bot = state_with_topic(tmp_path), FakeBot()
    app, log = app_for(tmp_path, state, bot, Broken())

    append(log, line())
    run(botmod.stall_watch_once(app))
    append(log, line(oldest_min=71, stamp="2026-11-26T13:00:00.000000Z"))
    run(botmod.stall_watch_once(app))

    assert len(bot.sent) == 1, "a failed probe never becomes hourly spam"
    assert caplog.text.count("stall_queue_unreadable") == 1


# ---- the notice says which silence this is ---------------------------------


def test_a_stopped_lane_notice_says_the_message_waits_for_a_start(tmp_path):
    clear_dedupe()
    state, bot = state_with_topic(tmp_path), FakeBot()
    app, log = app_for(tmp_path, state, bot, Queue(lanes()))

    append(log, line())
    run(botmod.stall_watch_once(app))

    text = bot.sent[0]["text"]
    assert "NOT RUNNING" in text
    assert "waits until the lane starts" in text
    assert "terminal shows what it is blocked on" not in text


def test_a_non_stopped_reason_keeps_the_generic_advice(tmp_path):
    clear_dedupe()
    state, bot = state_with_topic(tmp_path), FakeBot()
    app, log = app_for(
        tmp_path, state, bot,
        Queue(lanes(blocked="composer-draft")),
    )

    append(log, line(last_skip="send-refused: composer", blocked="composer-draft"))
    run(botmod.stall_watch_once(app))

    text = bot.sent[0]["text"]
    assert "last_skip=send-refused: composer" in text
    assert "terminal shows what it is blocked on" in text
    assert "NOT RUNNING" not in text


# ---- a deploy must not re-page a row the owner already saw ------------------


def test_a_row_older_than_this_process_is_not_paged_again(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="telegram_gateway")
    clear_dedupe()
    app, log = app_for(
        tmp_path,
        state_with_topic(tmp_path),
        FakeBot(),
        Queue(lanes(oldest_age_s=3600)),
    )
    append(log, line())
    run(botmod.stall_watch_once(app))

    assert app.bot.sent == [], "the run this deploy replaced already paged it"
    assert "stall_notice_seeded" in caplog.text


def test_a_row_queued_under_this_process_still_pages(tmp_path):
    clear_dedupe()
    app, log = app_for(
        tmp_path,
        state_with_topic(tmp_path),
        FakeBot(),
        Queue(lanes(oldest_age_s=5)),
    )
    append(log, line(oldest_min=0))
    run(botmod.stall_watch_once(app))

    assert len(app.bot.sent) == 1, "a fresh stuck message is news"
