"""The session browser (owner, 2026-10-31): projects → sessions → adopt.

Two owner asks in one surface:

1. a button listing every project directory (grouped from the shared
   OpenCode server's `GET /api/session` by `location.directory`) with its
   conversation count;
2. tapping a project lists its conversations (title · last update · ✅ when
   a lane already adopts that `ses_…`), and tapping a conversation adopts it
   — a lane pinned to that exact id in the conversation's own project dir
   (REUSING a lane that already pins it, never a second one), its topic
   opened through the gateway's own `POST /topic` code path, and the
   conversation's last messages posted as the topic intro, so the owner
   picks up where it was left.

The contract under test:

- grouping/ages/keyboards are pure and every callback stays ≤ 64 bytes;
- a pick resolves the `ses_…` id itself (a resume never resolves against a
  list the owner did not see); a stale PROJECT index re-renders instead of
  guessing;
- the adopt flow makes exactly the calls the owner named, in order, and
  records the pin (the relay's key) plus the title (so the title sync never
  attempts a same-name edit);
- every failure is an honest message, never a silent tap.
"""
import logging

from telegram_gateway import bot as botmod, menu
from telegram_gateway.opencode_api import OcError
from telegram_gateway.relay import CHUNK_LIMIT, assistant_texts
from telegram_gateway.state import GatewayState

from test_fresh_session import ResetAmux
from test_session_control import buttons, fake_callback, fake_command, lane
from test_topics import CHAT, FakeBot, fake_context, make_cfg, run

TITLE = "Adoption demo report"
DIR_A = "/home/pv/Documents/pv/projetos/alpha"
DIR_B = "/home/pv/Documents/pv/projetos/beta"

SES_OLD = "ses_disp10311712a3f9c2"  # measured 22 chars — the cap case
SES_NEW = "ses_disp10311744ff00aa"

PROJECTS_ROWS = [
    {
        "id": SES_NEW,
        "title": TITLE,
        "directory": DIR_A,
        "updated": 1_793_483_352_271,  # ms epoch, measured shape
    },
    {
        "id": SES_OLD,
        "title": "Older conversation",
        "directory": DIR_A,
        "updated": 1_793_000_000_000,
    },
    {
        "id": "ses_lone001122334455",
        "title": "Beta work",
        "directory": DIR_B,
        "updated": None,
    },
    {"id": "ses_orphan0011223344", "title": "", "directory": ""},  # no location
]

NEWEST_FIRST_MESSAGES = [
    {  # newest first, as the API returns it
        "id": "m3",
        "type": "assistant",
        "time": {"completed": 1793483352271},
        "content": [{"type": "text", "text": "latest answer"}],
    },
    {
        "id": "m2",
        "type": "user",
        "time": {"completed": 1793483000000},
        "content": [{"type": "text", "text": "the owner's question"}],
    },
    {
        "id": "m1",
        "type": "assistant",
        "time": {"completed": 1793482000000},
        "content": [{"type": "text", "text": "middle answer"}],
    },
    {
        "id": "m0",
        "type": "assistant",
        "time": {},  # still streaming — never part of an intro
        "content": [{"type": "text", "text": "half an answer"}],
    },
]


# ---- fakes -----------------------------------------------------------------


class BrowseClient:
    def __init__(self, owner):
        self.owner = owner

    async def sessions(self):
        self.owner.calls.append(("sessions",))
        if self.owner.sessions_exc is not None:
            raise self.owner.sessions_exc
        return [dict(row) for row in self.owner.sessions_list]

    async def messages(self, ses_id, **kwargs):
        self.owner.calls.append(("messages", ses_id))
        if self.owner.messages_exc is not None:
            raise self.owner.messages_exc
        return [dict(m) for m in self.owner.messages_list]

    async def aclose(self):
        self.owner.calls.append(("aclose",))


class BrowseOc:
    """The injected `oc_api` entry point: no ss, no /proc, no sockets."""

    def __init__(self, sessions=(), messages=None, fail=None,
                 messages_fail=None):
        self.sessions_list = [dict(r) for r in sessions]
        self.messages_list = [dict(m) for m in (messages or [])]
        self.sessions_exc = fail  # raised at DISCOVERY (a dead service)
        self.messages_exc = messages_fail
        self.calls = []

    async def client_for_service(self, **kwargs):
        self.calls.append(("client_for_service", kwargs))
        if self.sessions_exc is not None:
            raise self.sessions_exc
        return BrowseClient(self)


def browser_context(cfg, oc, client, state, bot):
    """fake_context + the injected API entry point (the offline rule)."""
    context = fake_context(cfg, client, state, bot)
    context.application.bot_data["oc_api"] = oc
    return context


def labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


# ---- 1. pure: grouping, ages, caps -----------------------------------------


def test_projects_group_by_directory_sorted_and_counted():
    groups = menu.projects_from_rows(PROJECTS_ROWS)
    assert [g["directory"] for g in groups] == ["", DIR_A, DIR_B]
    assert [len(g["sessions"]) for g in groups] == [1, 2, 1]
    # newest-updated first inside a project
    assert [s["id"] for s in groups[1]["sessions"]] == [SES_NEW, SES_OLD]
    # a row without an id is skipped, never rendered
    assert menu.projects_from_rows([{"title": "ghost"}]) == []


def test_ago_renders_an_honest_age():
    now = 1_793_483_400_000 / 1000
    assert menu.ago(None, now=now) == ""
    assert menu.ago(0, now=now) == ""
    assert menu.ago((now - 30) * 1000, now=now) == "now"
    assert menu.ago((now - 300) * 1000, now=now) == "5m"
    assert menu.ago((now - 7200) * 1000, now=now) == "2h"
    assert menu.ago((now - 3 * 86400) * 1000, now=now) == "3d"
    assert menu.ago((now - 90 * 86400) * 1000, now=now) == "3mo"


def test_browser_keyboards_stay_within_the_callback_cap():
    long_id = "ses_" + "x" * 48  # far past any real id
    entries = [{"id": long_id, "title": "t" * 120, "updated": None}]
    sessions_markup = menu.project_sessions_keyboard(0, entries)
    projects_markup = menu.projects_keyboard(menu.projects_from_rows(PROJECTS_ROWS))
    assert all(len(c.encode()) <= 64 for c in buttons(sessions_markup))
    assert all(len(c.encode()) <= 64 for c in buttons(projects_markup))
    # a session row addresses its id (never an index); projects address an index
    assert f"gw:adopt:{long_id[:52]}" in buttons(sessions_markup)
    assert "gw:prj:0" in buttons(projects_markup)
    # labels are phone-sized too
    assert all(len(lbl) <= 64 for lbl in labels(sessions_markup))


def test_assistant_texts_are_the_last_answers_oldest_first():
    texts = assistant_texts(NEWEST_FIRST_MESSAGES, limit=3)
    assert texts == ["middle answer", "latest answer"]  # half an answer dropped
    assert assistant_texts([]) == []
    assert assistant_texts(NEWEST_FIRST_MESSAGES, limit=1) == ["latest answer"]


# ---- 2. the entry point: /browse and the relayed button ---------------------


def test_browse_command_lists_projects_with_counts(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS)
    bot = FakeBot()
    context = browser_context(cfg, oc, ResetAmux().client, state, bot)
    update = fake_command("/browse")

    run(botmod.browse_command(update, context))

    assert update.message.replies, "the project list answers the command"
    reply = update.message.replies[0]
    assert "4 conversations in 3 directories" in reply["text"]
    assert "alpha — 2" in reply["text"]
    assert "beta — 1" in reply["text"]
    assert "gw:prj:0" in buttons(reply["reply_markup"])


def test_the_browse_button_rides_relayed_dispatcher_answers_only():
    """The GM's surface: every relayed dispatcher answer carries the entry
    button; a worker's topic answer does not grow a second panel."""
    assert buttons(botmod._browse_markup("general-mgr-1")) == ["gw:prj"]
    assert botmod._browse_markup("wk-worker") is None
    assert botmod._browse_markup("general-mgr-1") is not None


def test_send_markdown_attaches_the_keyboard_to_the_last_chunk_only(tmp_path):
    bot = FakeBot()
    text = "answer line\n" * 700  # > CHUNK_LIMIT → several chunks
    assert len(text) > CHUNK_LIMIT
    run(botmod._send_markdown(bot, CHAT, text, reply_markup=botmod.browse_button()))
    assert len(bot.sent) > 1
    last_id = 900 + len(bot.sent)
    assert [mid for mid, _ in bot.markups] == [last_id]


# ---- 3. the project's sessions ----------------------------------------------


def test_tapping_a_project_lists_sessions_with_adopted_marker(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS)
    amux = ResetAmux(rows=[lane("wk-alpha", flags=f"--session {SES_NEW}")])
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    try:
        # render, then tap — the SAME context carries the rendered list
        rendered = fake_callback("gw:prj")
        run(botmod.on_callback(rendered, context))
        tapped = fake_callback("gw:prj:1")  # alpha (sorted: "" < a… < b…)
        run(botmod.on_callback(tapped, context))
    finally:
        run(amux.aclose())

    edit = tapped.callback_query.edits[-1]
    assert "alpha — 2 conversation(s)" in edit["text"]
    markup = edit["markup"]
    keys = buttons(markup)
    assert keys[:2] == [f"gw:adopt:{SES_NEW}", f"gw:adopt:{SES_OLD}"]
    labels_ = labels(markup)
    # adopted marked, ages shown (a missing `time.updated` loses only its age)
    assert labels_[0].startswith("✅ " + TITLE)
    assert labels_[1].startswith("○ Older conversation")
    assert " · " in labels_[1]
    # back to the projects list
    assert "gw:prj" in keys


def test_a_stale_project_index_renders_the_current_list(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS)
    bot = FakeBot()
    context = browser_context(cfg, oc, ResetAmux().client, state, bot)
    update = fake_callback("gw:prj:9")  # no such project (or none rendered)

    run(botmod.on_callback(update, context))

    edit = update.callback_query.edits[-1]
    assert "the list moved" in edit["text"]  # honest re-render, never a guess
    assert "gw:prj:0" in buttons(edit["markup"])


# ---- 4. the adopt flow -------------------------------------------------------


def test_adopt_creates_a_pinned_lane_topics_it_and_intros_it(tmp_path, caplog):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS, messages=NEWEST_FIRST_MESSAGES)
    amux = ResetAmux()
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        with caplog.at_level(logging.INFO, logger="telegram_gateway"):
            run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    # 1. the lane: pinned to that exact ses_, in the conversation's project dir
    (body,) = amux.created_bodies
    assert body["name"].startswith("adopt-") and len(body["name"]) <= 40
    assert body["provider"] == "opencode"
    assert body["dir"] == DIR_A
    assert body["flags"] == f"--session {SES_NEW}"
    assert body["start"] is True

    # 2. the topic: through the gateway's own path, titled, echo = the
    # orientation + the intro (now POST /topic's own job, one format)
    assert bot.created == [(CHAT, TITLE)]
    echo = bot.sent[0]  # the echo is this flow's only SEND: FakeBot ids
    # start at 901, and the confirmation EDITS the tapped message
    assert echo["thread"] == 1001
    assert echo["text"].startswith("Resumed from the session browser.")
    assert f"📂 {TITLE} — last messages before the pause:" in echo["text"]
    assert "middle answer" in echo["text"] and "latest answer" in echo["text"]
    assert "half an answer" not in echo["text"]  # never an unfinished message

    # 3. the bindings the relay and the title sync need
    assert state.topic_pin(CHAT, 1001) == SES_NEW  # the relay's key
    assert state.topic_name(CHAT, 1001) == TITLE  # no same-name edit later
    assert state.turn_message(CHAT, body["name"]) == 901
    assert state.turn_thread(CHAT, body["name"]) == 1001

    # 4. the owner's confirmation carries the panel for the adopted lane
    edit = update.callback_query.edits[-1]
    assert f"Adopted `{body['name']}` → topic 1001" in edit["text"]
    assert TITLE in edit["text"]

    assert any("browse_adopted" in m for m in caplog.messages)
    assert any("browse_topic_bound" in m for m in caplog.messages)


def test_adopt_reuses_a_lane_that_already_pins_the_session(tmp_path):
    """One conversation, one lane, ever — a second create would fork the
    same resume key into two workers."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS, messages=NEWEST_FIRST_MESSAGES)
    amux = ResetAmux(rows=[lane("wk-alpha", flags=f"--session {SES_NEW}")])
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert amux.created_bodies == []  # no lane minted…
    assert bot.created == [(CHAT, TITLE)]  # …the topic still opened
    assert state.topic_session(CHAT, 1001) == "wk-alpha"
    assert "Reused lane `wk-alpha`" in update.callback_query.edits[-1]["text"]


def test_adopt_reports_a_vanished_session_honestly(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=[r for r in PROJECTS_ROWS if r["id"] != SES_NEW])
    amux = ResetAmux()
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert amux.created_bodies == []  # nothing created on a ghost
    assert bot.created == []
    assert "gone from the service" in update.callback_query.edits[-1]["text"]


def test_adopt_reports_a_dead_service_without_touching_amux(tmp_path):
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(fail=OcError(0, "no opencode serve (offline test)"))
    amux = ResetAmux()
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert amux.created_bodies == []
    assert "OpenCode service is unreachable" in update.callback_query.edits[-1]["text"]


def test_a_title_collision_retries_once_with_the_session_id(tmp_path):
    """Two conversations can share a title; the gateway map cannot — the
    retry lands the topic under a name derived from the conversation id."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(CHAT, 42, "other-lane", name=TITLE)  # holds the title
    oc = BrowseOc(sessions=PROJECTS_ROWS, messages=NEWEST_FIRST_MESSAGES)
    amux = ResetAmux()
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert len(bot.created) == 1  # the 409 is answered BEFORE any create
    assert bot.created[0][1].startswith(TITLE + " (")  # the id tiebreak
    assert state.topic_session(CHAT, 1001) is not None


def test_the_intro_degrades_to_a_marker_when_history_cannot_be_read(tmp_path):
    """A history failure is NOT a failed adopt: the topic opens anyway with
    the orientation line (the intro fetch is POST /topic's, best-effort)."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(
        sessions=PROJECTS_ROWS,
        messages_fail=OcError(404, "session is gone"),
    )
    amux = ResetAmux()
    bot = FakeBot()
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert amux.created_bodies and bot.created
    assert bot.sent[0]["text"] == "Resumed from the session browser."


def test_a_failed_topic_keeps_the_lane_and_asks_for_a_retry(tmp_path, caplog):
    """The lane is the resume vehicle: when Telegram refuses the topic AFTER
    the lane exists, the tap reports a retryable step, never a lost adopt —
    a re-tap takes the reuse path and retries exactly the topic."""
    from telegram.error import TelegramError

    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=PROJECTS_ROWS, messages=NEWEST_FIRST_MESSAGES)
    amux = ResetAmux()
    bot = FakeBot()

    async def refuse(chat_id, name=None):
        raise TelegramError("not enough rights")

    bot.create_forum_topic = refuse
    context = browser_context(cfg, oc, amux.client, state, bot)
    update = fake_callback(f"gw:adopt:{SES_NEW}")
    try:
        with caplog.at_level(logging.WARNING, logger="telegram_gateway"):
            run(botmod.on_callback(update, context))
    finally:
        run(amux.aclose())

    assert len(amux.created_bodies) == 1  # the lane is KEPT
    assert bot.created == []  # no topic
    kept = amux.created_bodies[0]["name"]
    assert state.session_topic(CHAT, kept) is None  # nothing half-bound
    text = update.callback_query.edits[-1]["text"]
    assert "is ready, but the topic could not be opened" in text
    assert "Tap the conversation again to retry" in text
    assert any("browse_topic_failed" in m for m in caplog.messages)
