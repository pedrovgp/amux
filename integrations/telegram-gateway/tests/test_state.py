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
