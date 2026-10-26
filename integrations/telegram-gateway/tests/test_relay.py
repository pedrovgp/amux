from telegram_gateway.relay import (
    CHUNK_LIMIT,
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


def test_delta_since_returns_only_new_text():
    assert delta_since("answer", "answer plus more") == " plus more"
    assert delta_since("answer", "answer\nplus more") == "plus more"
    assert delta_since("answer", "answer") == ""
    assert delta_since(None, "fresh") == "fresh"
    assert delta_since("old turn", "new turn") == "new turn"
