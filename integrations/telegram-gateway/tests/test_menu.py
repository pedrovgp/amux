from telegram_gateway.menu import (
    PREFIX,
    format_worker_line,
    main_menu,
    manager_keyboard,
    sessions_keyboard,
    status_keyboard,
    stop_confirm,
)


def callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def test_main_menu_has_the_five_actions():
    menu = main_menu()
    assert callbacks(menu) == [
        f"{PREFIX}new",
        f"{PREFIX}peek",
        f"{PREFIX}prompt",
        f"{PREFIX}stop",
        f"{PREFIX}mgr",
    ]
    labels = [button.text for row in menu.inline_keyboard for button in row]
    assert any("Start" in label for label in labels)
    assert any("Peek" in label for label in labels)
    assert any("Send" in label for label in labels)
    assert any("Stop" in label for label in labels)
    assert any("Manager" in label for label in labels)


def test_manager_keyboard_offers_providers_then_models():
    markup = manager_keyboard("opencode", None, ["deepseek-v4.1-flash-free", "iceman-contributor-free"])
    data = callbacks(markup)
    assert f"{PREFIX}mgrp:opencode" in data
    assert f"{PREFIX}mgrp:claude" in data
    assert f"{PREFIX}mgrm:opencode:deepseek-v4.1-flash-free" in data
    assert f"{PREFIX}mgrm:opencode:" in data  # the server default arm
    assert data[-1] == f"{PREFIX}menu"
    assert all(len(d.encode()) <= 64 for d in data)


def test_manager_keyboard_marks_the_chosen_model():
    markup = manager_keyboard("opencode", "iceman", ["deepseek", "iceman"], )
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any(l.startswith("   ✅ iceman") for l in labels)


def test_sessions_keyboard_links_each_worker_and_a_menu():
    workers = [
        {"name": "alpha", "running": True, "status": "running"},
        {"name": "beta", "running": False, "status": {"state": "stopped"}},
    ]
    data = callbacks(sessions_keyboard(workers, "pick"))
    assert f"{PREFIX}pick:alpha" in data
    assert f"{PREFIX}pick:beta" in data
    assert data[-1] == f"{PREFIX}menu"


def test_sessions_keyboard_caps_long_lists():
    workers = [{"name": f"w{i}"} for i in range(30)]
    data = callbacks(sessions_keyboard(workers, "stop"))
    # 20 workers + the menu row
    assert len(data) == 21


def test_stop_confirm_and_status_buttons():
    assert callbacks(stop_confirm("alpha")) == [f"{PREFIX}stopok:alpha", f"{PREFIX}cancel"]
    status = callbacks(status_keyboard("alpha"))
    assert f"{PREFIX}peek:alpha" in status
    assert f"{PREFIX}prompt" in status


def test_format_worker_line_reads_the_status_shapes():
    assert format_worker_line(
        {"name": "a", "running": True, "status": "running", "model": "m"}
    ) == "a — running · m"
    assert format_worker_line(
        {"name": "b", "running": False, "status": {"state": "stopped"}}
    ) == "b — stopped"
