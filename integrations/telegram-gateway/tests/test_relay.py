from telegram_gateway.relay import (
    CHUNK_LIMIT,
    _is_chrome,
    chunk_text,
    delta_since,
    extract_reply,
    strip_ansi,
)


def test_strip_ansi():
    assert strip_ansi("\x1b[38;5;231mhello\x1b[0m") == "hello"


def test_extract_reply_takes_text_after_the_last_marker():
    raw = (
        "\x1b[32mold output\x1b[0m\n"
        "[from Telegram @alice] first prompt\n"
        "first answer\n"
        "[from Telegram @alice] second prompt\n"
        "the second answer\n"
        "more of it\n"
    )
    assert extract_reply(raw) == "the second answer\nmore of it"


def test_extract_reply_drops_tui_chrome():
    raw = (
        "[from Telegram @alice] do the thing\n"
        "the answer\n"
        "╭──────────────────────────────╮\n"
        "│ bypass permissions on        │\n"
        "╰──────────────────────────────╯\n"
        "  shift+tab to cycle\n"
    )
    assert extract_reply(raw) == "the answer"


def test_extract_reply_none_without_marker():
    assert extract_reply("just ordinary pane output\n") is None


def test_extract_reply_none_when_nothing_follows():
    assert extract_reply("[from Telegram @alice] hi\n") is None


def test_chunk_text_short_is_one_chunk():
    assert chunk_text("short") == ["short"]


def test_chunk_text_splits_on_line_boundaries():
    text = ("x" * 100 + "\n") * 100
    chunks = chunk_text(text, limit=CHUNK_LIMIT)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_LIMIT for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_chunk_text_splits_a_pathological_line():
    text = "y" * (CHUNK_LIMIT * 2 + 10)
    chunks = chunk_text(text, limit=CHUNK_LIMIT)
    assert all(len(chunk) <= CHUNK_LIMIT for chunk in chunks)
    assert "".join(chunks) == text


def test_extract_reply_uses_the_agent_sentinel():
    # The live opencode pane: the prompt marker has scrolled away, and the
    # answer is followed by the agent's own footer and status bar.
    pane = (
        "some earlier turn\n"
        "—— answer ——\n"
        "the real answer\n"
        "Build · MiMo-V2.6-Flash · 9.3s · 1.3 tok/s\n"
        "╹▀▀▀▀▀▀▀▀▀▀▀▀\n"
        "  ~/Documents/pv/projetos   15.9K (2%) · $0.00  ctrl+p commands\n"
    )
    assert extract_reply(pane) == "the real answer"


def test_extract_reply_prefers_the_last_boundary():
    pane = (
        "[from Telegram @owner] first task\n"
        "first answer\n"
        "—— answer ——\n"
        "second answer\n"
        "Build · MiMo · 9.3s · 1.3 tok/s\n"
    )
    assert extract_reply(pane) == "second answer"


def test_chrome_rules_match_the_live_opencode_pane():
    chrome = (
        "15,861 tokens",
        "2% used",
        "$0.00 spent",
        "+ Thought · 2.4s",
        "Build · MiMo-V2.6-Flash · 9.3s · 1.3 tok/s",
        "╹▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀",
        "  ~/Documents/pv/projetos   15.9K (2%) · $0.00  ctrl+p commands    ~/Documents/pv/projetos",
        "  shift+tab to cycle",
    )
    for line in chrome:
        assert _is_chrome(line), f"should be chrome: {line!r}"
    for line in ("the answer is fine", "- a bullet of prose", "$5 is the cost"):
        assert not _is_chrome(line), f"should survive: {line!r}"


def test_delta_since_returns_only_new_text():
    assert delta_since("answer", "answer plus more") == " plus more"
    assert delta_since("answer", "answer\nplus more") == "plus more"
    assert delta_since("answer", "answer") == ""
    assert delta_since(None, "fresh") == "fresh"
    assert delta_since("old turn", "new turn") == "new turn"
