from telegram_gateway.state import GatewayState


def test_selection_roundtrips_and_persists(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.select(111, "worker-a")
    state.select(222, "worker-b")
    assert state.selected(111) == "worker-a"
    assert state.selections() == {111: "worker-a", 222: "worker-b"}

    reloaded = GatewayState(path)
    assert reloaded.selected(111) == "worker-a"
    assert reloaded.selected(222) == "worker-b"


def test_clear_drops_selection_and_dedupe(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.select(1, "w")
    state.set_last_relayed(1, "w", "answer")
    state.clear(1)
    assert state.selected(1) is None
    assert state.last_relayed(1, "w") is None


def test_last_relayed_is_per_session(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.select(1, "a")
    state.set_last_relayed(1, "a", "answer a")
    state.set_last_relayed(1, "b", "answer b")
    assert state.last_relayed(1, "a") == "answer a"
    assert state.last_relayed(1, "b") == "answer b"


def test_corrupt_state_file_is_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    state = GatewayState(path)
    assert state.selections() == {}
    state.select(5, "w")
    assert GatewayState(path).selected(5) == "w"


def test_relayed_entries_are_bounded(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.select(1, "w")
    for i in range(20):
        state.set_last_relayed(1, f"s{i}", f"answer {i}")
    reloaded = GatewayState(tmp_path / "state.json")
    assert reloaded.last_relayed(1, "s19") == "answer 19"
    assert reloaded.last_relayed(1, "s0") is None


def test_chat_for_session_resolves_the_owning_chat(tmp_path):
    state = GatewayState(tmp_path / "state.json")
    state.set_topic(-100, 11, "mgr-a")          # forum group, topic bound
    state.set_turn_message(-100, "mgr-a", 5, thread=11)
    state.set_turn_message(823440998, "mgr-b", 6)  # private chat, turn only

    assert state.chat_for_session("mgr-a") == -100
    assert state.chat_for_session("mgr-b") == 823440998
    assert state.chat_for_session("mgr-never-seen") is None

    # a topic binding wins over a same-named session elsewhere
    state.set_turn_message(42, "mgr-a", 9)
    assert state.chat_for_session("mgr-a") == -100

    # survives a restart
    assert GatewayState(tmp_path / "state.json").chat_for_session("mgr-b") == 823440998


def test_topic_names_are_tracked_and_released(tmp_path):
    path = tmp_path / "state.json"
    state = GatewayState(path)
    state.set_topic(-100, 11, "mgr-a")                    # name defaults to session
    state.set_topic(-100, 12, "mgr-b", "Research task")   # custom display name

    assert state.thread_for_topic_name(-100, "mgr-a") == 11
    assert state.thread_for_topic_name(-100, "Research task") == 12
    assert state.thread_for_topic_name(-100, "nothing here") is None
    assert state.thread_for_topic_name(999, "mgr-a") is None  # other chat

    # a restart keeps the names
    reloaded = GatewayState(path)
    assert reloaded.thread_for_topic_name(-100, "Research task") == 12

    # releasing a session frees its name too, so it can be reused
    state.drop_session_topics(-100, "mgr-b")
    assert state.thread_for_topic_name(-100, "Research task") is None
    assert state.thread_for_topic_name(-100, "mgr-a") == 11


def test_legacy_topics_without_names_still_match_by_session(tmp_path):
    """State written before names were tracked must still answer."""
    path = tmp_path / "state.json"
    path.write_text(
        '{"chats": {"-100": {"topics": {"7": "mgr-old"}}}}', encoding="utf-8"
    )
    state = GatewayState(path)
    assert state.thread_for_topic_name(-100, "mgr-old") == 7
    assert state.thread_for_topic_name(-100, "mgr-new") is None
