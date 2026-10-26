from telegram_gateway.menu import (
    PREFIX,
    format_worker_line,
    main_menu,
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


def test_main_menu_has_the_four_actions():
    menu = main_menu()
    assert callbacks(menu) == [
        f"{PREFIX}new",
        f"{PREFIX}peek",
        f"{PREFIX}prompt",
        f"{PREFIX}stop",
    ]
    labels = [button.text for row in menu.inline_keyboard for button in row]
    assert any("Start" in label for label in labels)
    assert any("Peek" in label for label in labels)
    assert any("Send" in label for label in labels)
    assert any("Stop" in label for label in labels)


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
