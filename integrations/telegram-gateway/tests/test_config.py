import pytest

from telegram_gateway.config import (
    ConfigError,
    endpoint_url,
    load_config,
    parse_env_file,
    parse_user_ids,
)


def test_parse_env_file_handles_comments_quotes_and_export(tmp_path):
    path = tmp_path / "server.env"
    path.write_text(
        "# comment\n"
        "\n"
        "PLAIN=value\n"
        "QUOTED='single value'\n"
        'DOUBLE="double value"\n'
        "export EXPORTED=yes\n"
        "NOT_A_PAIR\n"
    )
    env = parse_env_file(path)
    assert env == {
        "PLAIN": "value",
        "QUOTED": "single value",
        "DOUBLE": "double value",
        "EXPORTED": "yes",
    }


def test_parse_env_file_missing_is_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope.env") == {}


def test_parse_user_ids_accepts_commas_and_semicolons():
    assert parse_user_ids("1, 2;3") == [1, 2, 3]


def test_parse_user_ids_rejects_a_typo():
    with pytest.raises(ValueError, match="not-a-number"):
        parse_user_ids("123, not-a-number")


def test_endpoint_url_prefers_canonical_url(tmp_path):
    path = tmp_path / "endpoint.json"
    path.write_text('{"canonical_url": "https://localhost:8824", "legacy_port": 8822}')
    assert endpoint_url(path) == "https://localhost:8824"


def test_endpoint_url_none_when_missing(tmp_path):
    assert endpoint_url(tmp_path / "missing.json") is None


def test_load_config_requires_token_and_allowlist(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("")
    with pytest.raises(ConfigError) as excinfo:
        load_config(environ={}, env_file=env_file, endpoint_file=tmp_path / "e.json")
    joined = " ".join(excinfo.value.problems)
    assert "TELEGRAM_GATEWAY_BOT_TOKEN" in joined
    assert "ALLOWED_TELEGRAM_USER_IDS" in joined


def test_load_config_refuses_sharing_the_builtin_token(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "TELEGRAM_GATEWAY_BOT_TOKEN=1:shared\n"
        "TELEGRAM_BOT_TOKEN=1:shared\n"
        "ALLOWED_TELEGRAM_USER_IDS=42\n"
    )
    with pytest.raises(ConfigError, match="fight over getUpdates"):
        load_config(environ={}, env_file=env_file, endpoint_file=tmp_path / "e.json")


def test_load_config_happy_path_and_env_precedence(tmp_path):
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text('{"canonical_url": "https://localhost:9000"}')
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "TELEGRAM_GATEWAY_BOT_TOKEN=123:file-token\n"
        "ALLOWED_TELEGRAM_USER_IDS=7,8\n"
        "GROQ_API_KEY=file-key\n"
        "TELEGRAM_GATEWAY_RELAY_INTERVAL_S=30\n"
    )
    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_BOT_TOKEN": "123:process-token"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.bot_token == "123:process-token"  # process env wins
    assert cfg.allowed_user_ids == frozenset({7, 8})
    assert cfg.groq_api_key == "file-key"
    assert cfg.amux_url == "https://localhost:9000"
    assert cfg.amux_verify is False
    assert cfg.relay_interval_s == 30
    assert cfg.state_path.name == "state.json"


def test_load_config_defaults_endpoint_when_absent(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text(
        "TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n"
    )
    cfg = load_config(environ={}, env_file=env_file, endpoint_file=tmp_path / "none.json")
    assert cfg.amux_url == "https://localhost:8824"
    assert cfg.groq_api_key is None


def test_load_config_control_port_default_override_and_off(tmp_path):
    """The control endpoint port: default 8834, env override, 0 disables."""
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    endpoint = tmp_path / "none.json"

    cfg = load_config(environ={}, env_file=env_file, endpoint_file=endpoint)
    assert cfg.control_port == 8834

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_CONTROL_PORT": "9001"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.control_port == 9001

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_CONTROL_PORT": "0"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.control_port == 0  # 0 = endpoint disabled, not a typo


def test_load_config_control_port_rejects_garbage(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    with pytest.raises(ConfigError, match="TELEGRAM_GATEWAY_CONTROL_PORT"):
        load_config(
            environ={"TELEGRAM_GATEWAY_CONTROL_PORT": "banana"},
            env_file=env_file,
            endpoint_file=tmp_path / "none.json",
        )


def test_load_config_idle_stop_default_override_and_off(tmp_path):
    """TELEGRAM_GATEWAY_IDLE_STOP_S: 1800 default (owner: 30 min), 0 = off."""
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    endpoint = tmp_path / "none.json"

    cfg = load_config(environ={}, env_file=env_file, endpoint_file=endpoint)
    assert cfg.idle_stop_s == 1800.0

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_IDLE_STOP_S": "900"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.idle_stop_s == 900.0

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_IDLE_STOP_S": "0"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.idle_stop_s == 0.0  # 0 = feature off, not a typo


def test_load_config_idle_stop_rejects_garbage_and_negative(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    endpoint = tmp_path / "none.json"

    with pytest.raises(ConfigError, match="TELEGRAM_GATEWAY_IDLE_STOP_S"):
        load_config(
            environ={"TELEGRAM_GATEWAY_IDLE_STOP_S": "banana"},
            env_file=env_file,
            endpoint_file=endpoint,
        )
    with pytest.raises(ConfigError, match="TELEGRAM_GATEWAY_IDLE_STOP_S"):
        load_config(
            environ={"TELEGRAM_GATEWAY_IDLE_STOP_S": "-5"},
            env_file=env_file,
            endpoint_file=endpoint,
        )


def test_load_config_topic_sync_default_override_and_off(tmp_path):
    """TELEGRAM_GATEWAY_TOPIC_SYNC_S: 60 default, 0 = sync off."""
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    endpoint = tmp_path / "none.json"

    cfg = load_config(environ={}, env_file=env_file, endpoint_file=endpoint)
    assert cfg.topic_sync_s == 60.0

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_TOPIC_SYNC_S": "300"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.topic_sync_s == 300.0

    cfg = load_config(
        environ={"TELEGRAM_GATEWAY_TOPIC_SYNC_S": "0"},
        env_file=env_file,
        endpoint_file=endpoint,
    )
    assert cfg.topic_sync_s == 0.0  # 0 = off, not a typo


def test_load_config_topic_sync_rejects_garbage_and_negative(tmp_path):
    env_file = tmp_path / "server.env"
    env_file.write_text("TELEGRAM_GATEWAY_BOT_TOKEN=1:t\nALLOWED_TELEGRAM_USER_IDS=1\n")
    endpoint = tmp_path / "none.json"

    with pytest.raises(ConfigError, match="TELEGRAM_GATEWAY_TOPIC_SYNC_S"):
        load_config(
            environ={"TELEGRAM_GATEWAY_TOPIC_SYNC_S": "soon"},
            env_file=env_file,
            endpoint_file=endpoint,
        )
    with pytest.raises(ConfigError, match="TELEGRAM_GATEWAY_TOPIC_SYNC_S"):
        load_config(
            environ={"TELEGRAM_GATEWAY_TOPIC_SYNC_S": "-1"},
            env_file=env_file,
            endpoint_file=endpoint,
        )
