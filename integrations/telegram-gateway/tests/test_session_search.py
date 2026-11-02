"""The session search (owner, 2026-11-02): fuzzy by name, filter by dates.

The owner's ask: *"search sessions either globally or within projects with
a fuzzy search by name … filter by created date or last updated date within
a timestamp range … final timestamp by default being now or a little bit in
the future … initial a few days before, like five days."*

The contract under test:

1. **the pure rules** — defaults (now−5d → now+1d), fuzzy ordering
   (substring > subsequence > ratio, title AND id), range parsing (the
   owner's shapes, honest `None` for anything unreadable), and the filter
   itself (scope, chosen field, inclusive range, no guessed dates);
2. **the conversation is one-shot** — ask → answer → END, so the
   dispatcher's thread is never hijacked; a bad range RETRIES with an
   example instead of guessing;
3. **every tap re-renders the STORED search** — field toggle, scope drop,
   paging; a stale tap says so; a result adopts through the existing flow
   (`gw:adopt:<ses_id>`).

Offline: rows are fixtures, the service a stub, the bot a FakeBot.
"""
from datetime import datetime
import time

from telegram.ext import ConversationHandler

from telegram_gateway import bot as botmod, menu, search
from telegram_gateway.state import GatewayState

from test_fresh_session import ResetAmux
from test_session_browser import BrowseOc, browser_context
from test_session_control import fake_callback, fake_command
from test_topics import FakeBot, make_cfg, run

DIR_A = "/home/pv/Documents/pv/projetos/alpha"
DIR_B = "/home/pv/Documents/pv/projetos/beta"

NOW = 1_793_600_000_000.0  # a fixed "now" for every pure test
DAY = 86400_000.0


def rows(base=None):
    """Three conversations with BOTH dates: two in alpha, one in beta.

    ``base`` pins the timestamps for the PURE tests (a fixed ``NOW``); the
    conversation tests leave it real, so the rows always sit inside the
    default five-day window the render uses.
    """
    base = float(base) if base is not None else time.time() * 1000.0
    return [
        {"id": "ses_adopt0011223344", "title": "Adoption demo report",
         "directory": DIR_A, "created": base - 2 * DAY, "updated": base - 60_000},
        {"id": "ses_tele00112233445", "title": "Telegram session controls",
         "directory": DIR_A, "created": base - 10 * DAY, "updated": base - 2 * DAY},
        {"id": "ses_light0011223344", "title": "lighthouse essay",
         "directory": DIR_B, "created": base - DAY, "updated": base - 3 * DAY},
    ]


def ctx_for(tmp_path, *, data=None, client=None):
    """A context with the search's stubs: service rows, amux lanes, state."""
    cfg = make_cfg(tmp_path)
    state = GatewayState(tmp_path / "state.json")
    oc = BrowseOc(sessions=data if data is not None else rows())
    bot = FakeBot()
    if client is None:
        client = ResetAmux().client
    context = browser_context(cfg, oc, client, state, bot)
    return context, bot


def reply_of(update):
    """The last command-path reply: ``{"text", "reply_markup", …}``."""
    return update.message.replies[-1]


def edit_of(update):
    """The last callback-path edit: ``{"text", "markup", …}``."""
    return update.callback_query.edits[-1]


def keys_of(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


# ---- 1. the pure rules ------------------------------------------------------


def test_default_range_is_five_days_back_and_a_day_forward():
    from_ms, to_ms = search.default_range(NOW)
    assert from_ms == NOW - 5 * DAY
    assert to_ms == NOW + DAY  # "a little bit in the future … get everything"


def test_fuzzy_score_orders_substring_then_subsequence_then_ratio():
    assert search.fuzzy_score("adopt", "Adoption demo report") == 1.0
    assert search.fuzzy_score("adpmr", "Adoption demo report") == 0.8  # in order
    loose = search.fuzzy_score("demo-x", "Adoption demo report")
    assert 0 < loose < 0.8  # difflib ratio: overlapping, not a subsequence
    assert search.fuzzy_score("", "anything") == 0.0
    assert search.fuzzy_score("term", "") == 0.0


def test_row_score_matches_a_pasted_id_too():
    row = {"id": "ses_adopt0011223344", "title": "unrelated words"}
    assert search.row_score("adopt001", row) == 1.0  # the id itself
    assert search.row_score("", row) == 1.0  # no term = browse, not filter
    assert search.row_score("zzz", {"id": "ses_x", "title": "abc"}) < search.MIN_SCORE


def test_parse_range_accepts_the_owner_shapes():
    # comma, whole days: from the START of the first day, to its END
    from_ms, to_ms = search.parse_range("2026-10-28, 2026-11-03", NOW)
    assert datetime.fromtimestamp(from_ms / 1000).strftime("%Y-%m-%d") == "2026-10-28"
    to_day = datetime.fromtimestamp(to_ms / 1000)
    assert to_day.strftime("%Y-%m-%d") == "2026-11-03"
    assert (to_day.hour, to_day.minute, to_day.second) == (23, 59, 59)
    # "to" between the endpoints, and plain whitespace
    assert search.parse_range("2026-10-28 to 2026-11-03", NOW) == (from_ms, to_ms)
    assert search.parse_range("2026-10-28 2026-11-03", NOW) == (from_ms, to_ms)
    # an exact time on an endpoint
    timed, _ = search.parse_range("2026-10-28 09:30, 2026-11-03", NOW)
    assert datetime.fromtimestamp(timed / 1000).strftime("%H:%M") == "09:30"
    # `now` as an endpoint, and an empty endpoint = the default for its edge
    assert search.parse_range("now, 2026-11-03", NOW)[0] == NOW
    empty_from, _ = search.parse_range(", 2026-11-03", NOW)
    assert empty_from == NOW - 5 * DAY
    _, empty_to = search.parse_range("2026-10-28,", NOW)
    assert empty_to == NOW + DAY


def test_parse_range_refuses_what_it_cannot_read():
    assert search.parse_range("yesterday, tomorrow", NOW) is None  # not a date
    assert search.parse_range("2026-10-28", NOW) is None  # one endpoint only
    assert search.parse_range("2026-11-03, 2026-10-28", NOW) is None  # reversed
    assert search.parse_range("", NOW) is None
    assert search.parse_range("2026-13-45, 2026-11-03", NOW) is None  # not a day


def test_format_date_is_empty_for_missing_and_short_for_midnight():
    assert search.format_date(None) == ""
    assert search.format_date(0) == ""
    assert search.format_date(NOW) == datetime.fromtimestamp(NOW / 1000).strftime(
        "%Y-%m-%d %H:%M"
    )
    midnight = datetime(2026, 11, 3).timestamp() * 1000
    assert search.format_date(midnight) == "2026-11-03"


def test_search_sessions_filters_by_scope_field_and_range():
    data = rows(NOW)
    # scope: only alpha
    alpha = search.search_sessions(data, t_from=NOW - 30 * DAY, t_to=NOW + DAY,
                                   directory=DIR_A)
    assert [r["id"] for r in alpha] == ["ses_adopt0011223344",
                                        "ses_tele00112233445"]
    # field: the SAME range on created vs updated picks different rows
    week = (NOW - 7 * DAY, NOW + DAY)
    by_updated = search.search_sessions(data, t_from=week[0], t_to=week[1])
    assert len(by_updated) == 3  # every updated value is inside the week
    by_created = search.search_sessions(data, field="created",
                                        t_from=week[0], t_to=week[1])
    # Telegram controls was created 10 days ago: OUT on created, IN on
    # updated — and the survivors sort NEWEST-created first
    assert [r["id"] for r in by_created] == ["ses_light0011223344",
                                             "ses_adopt0011223344"]
    # a row WITHOUT the chosen date cannot prove it is in range: excluded
    undated = [{"id": "ses_x", "title": "t", "directory": DIR_A}]
    assert search.search_sessions(undated, field="created",
                                  t_from=week[0], t_to=week[1]) == []


def test_search_sessions_sorts_best_first_and_browses_newest_first():
    data = rows(NOW)
    hits = search.search_sessions(data, term="telegram",
                                  t_from=NOW - 30 * DAY, t_to=NOW + DAY)
    assert [r["id"] for r in hits] == ["ses_tele00112233445"]
    browse = search.search_sessions(data, t_from=NOW - 30 * DAY,
                                    t_to=NOW + DAY)
    assert [r["id"] for r in browse] == ["ses_adopt0011223344",
                                         "ses_tele00112233445",
                                         "ses_light0011223344"]
    # below the fuzzy threshold = no hit, never a near-miss guess
    assert search.search_sessions(data, term="qqqq",
                                  t_from=NOW - 30 * DAY, t_to=NOW + DAY) == []


# ---- 2. the keyboards -------------------------------------------------------


def test_results_keyboard_carries_ids_filters_and_stays_within_the_cap():
    entries = [
        {"id": "ses_adopt0011223344", "title": "Adoption demo report",
         "created": NOW - 2 * DAY, "updated": NOW},
        {"id": "ses_tele00112233445", "title": "Telegram session controls",
         "created": NOW - 10 * DAY, "updated": NOW - DAY, "adopted_by": "lane-x"},
    ]
    markup = menu.search_results_keyboard(
        entries, page=0, pages=3, field=search.FIELD_CREATED,
        t_from=NOW - 5 * DAY, t_to=NOW + DAY, scoped=True,
    )
    keys = keys_of(markup)
    assert all(len(k.encode()) <= 64 for k in keys)
    # results adopt through the EXISTING flow, by id
    assert "gw:adopt:ses_adopt0011223344" in keys
    assert "gw:adopt:ses_tele00112233445" in keys
    # the field toggle marks the ACTIVE date; both buttons re-toggle
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(lbl.startswith("✅") and "Created" in lbl for lbl in labels)
    assert any(lbl.startswith("○") and "Updated" in lbl for lbl in labels)
    assert keys.count("gw:findf") == 2
    # range button + scope way-back + paging + out
    assert "gw:findd" in keys
    assert "gw:finds" in keys
    assert "gw:findpg:1" in keys
    assert "gw:prj" in keys


def test_the_project_list_offers_the_scoped_search():
    markup = menu.project_sessions_keyboard(
        3, [{"id": "ses_x", "title": "t", "updated": None}]
    )
    keys = keys_of(markup)
    assert "gw:findprj:3" in keys
    assert all(len(k.encode()) <= 64 for k in keys)


# ---- 3. the conversation ----------------------------------------------------


def test_find_asks_for_the_term_and_renders_global_results(tmp_path):
    context, bot = ctx_for(tmp_path)

    asked = fake_command("/find")
    assert run(botmod.ask_find(asked, context)) == botmod.FIND
    assert "Send what to find" in reply_of(asked)["text"]
    assert "(global)" in reply_of(asked)["text"]

    answered = fake_command("adopt")
    assert run(botmod.receive_find(answered, context)) == ConversationHandler.END

    st = context.application.bot_data[botmod.FIND_STATE]
    assert st["term"] == "adopt"
    assert st["directory"] is None  # global scope
    # the owner's default field + range are stored…
    assert st["field"] == search.FIELD_UPDATED
    assert st["t_to"] - st["t_from"] == 6 * DAY  # 5 days back + 1 forward
    # …and the results render with them in the header
    reply = reply_of(answered)
    assert "🔍 `adopt` — 1 match(es)" in reply["text"]
    assert "scope: 🌍 global" in reply["text"]
    assert "filter: updated" in reply["text"]
    keys = keys_of(reply["reply_markup"])
    assert "gw:adopt:ses_adopt0011223344" in keys
    assert "gw:findf" in keys and "gw:findd" in keys
    assert "gw:finds" not in keys  # global scope: nothing to drop


def test_the_dates_flow_retries_an_unreadable_range_then_renders(tmp_path):
    context, bot = ctx_for(tmp_path)
    run(botmod.ask_find(fake_command("/find"), context))
    run(botmod.receive_find(fake_command("e"), context))

    # tap ⏱ and send nonsense: honest example, still asking (RETRY)
    entered = fake_callback("gw:findd")
    assert run(botmod.ask_find_dates(entered, context)) == botmod.FIND
    assert "initial, final" in edit_of(entered)["text"]
    retry = fake_command("yesterday to someday")
    assert run(botmod.receive_find(retry, context)) == botmod.FIND
    assert "couldn't read that range" in reply_of(retry)["text"]

    # a shape from the owner's spec: accepted, range applied, rendered
    done = fake_command("2026-10-28, 2026-11-03")
    assert run(botmod.receive_find(done, context)) == ConversationHandler.END
    st = context.application.bot_data[botmod.FIND_STATE]
    assert datetime.fromtimestamp(st["t_from"] / 1000).strftime("%Y-%m-%d") == "2026-10-28"
    assert "2026-10-28" in reply_of(done)["text"]


def test_project_scope_entry_and_stale_index_fallback(tmp_path):
    context, bot = ctx_for(tmp_path)
    # the project list was rendered earlier (the 🧩 flow stores it)
    context.application.bot_data[botmod.BROWSE_KEY] = [
        {"directory": "", "sessions": []},
        {"directory": DIR_A, "sessions": []},
    ]

    scoped = fake_callback("gw:findprj:1")
    assert run(botmod.ask_find_project(scoped, context)) == botmod.FIND
    assert context.application.bot_data[botmod.FIND_STATE]["directory"] == DIR_A
    assert "in alpha" in edit_of(scoped)["text"]

    # a stale index (list moved): honest fallback to GLOBAL, never a wrong dir
    stale = fake_callback("gw:findprj:9")
    assert run(botmod.ask_find_project(stale, context)) == botmod.FIND
    assert context.application.bot_data[botmod.FIND_STATE]["directory"] is None


def test_filter_toggles_and_paging_re_render_the_stored_search(tmp_path):
    context, bot = ctx_for(tmp_path)
    run(botmod.ask_find(fake_command("/find"), context))
    run(botmod.receive_find(fake_command("e"), context))

    # 📅 toggle: updated → created (re-render edits the SAME message)
    toggle = fake_callback("gw:findf")
    run(botmod.on_callback(toggle, context))
    st = context.application.bot_data[botmod.FIND_STATE]
    assert st["field"] == search.FIELD_CREATED
    assert "filter: created" in edit_of(toggle)["text"]
    # toggle back
    back = fake_callback("gw:findf")
    run(botmod.on_callback(back, context))
    assert st["field"] == search.FIELD_UPDATED
    assert "filter: updated" in edit_of(back)["text"]

    # 🌍 drops the scope (set one first) — global results come back
    st["directory"] = DIR_A
    dropped = fake_callback("gw:finds")
    run(botmod.on_callback(dropped, context))
    assert st["directory"] is None
    assert "scope: 🌍 global" in edit_of(dropped)["text"]

    # paging re-renders from the stored state (no re-ask)
    paged = fake_callback("gw:findpg:0")
    run(botmod.on_callback(paged, context))
    assert st["page"] == 0


def test_a_stale_filter_tap_is_honest(tmp_path):
    context, bot = ctx_for(tmp_path)  # no search ever started
    tap = fake_callback("gw:findf")
    run(botmod.on_callback(tap, context))
    assert "No active search" in edit_of(tap)["text"]


def test_empty_results_say_how_to_widen(tmp_path):
    context, bot = ctx_for(tmp_path, data=[])
    run(botmod.ask_find(fake_command("/find"), context))
    answered = fake_command("anything")
    run(botmod.receive_find(answered, context))
    assert "Nothing matches" in reply_of(answered)["text"]
    assert "widen the range" in reply_of(answered)["text"]
