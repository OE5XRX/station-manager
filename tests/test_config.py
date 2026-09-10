"""Unit tests for station_agent.config defaults and loading."""

from station_agent.config import AgentConfig, load_config


def test_slot_discovery_defaults():
    cfg = AgentConfig(
        server_url="https://example.test",
        station_id=1,
        ed25519_key_path="/tmp/k",
    )
    assert cfg.slot_discovery_enabled is True
    assert cfg.slot_dev_base == "/dev/oe5xrx"


def _write(tmp_path, body, monkeypatch):
    p = tmp_path / "config.yml"
    p.write_text(body)
    monkeypatch.setenv("STATION_AGENT_CONFIG", str(p))
    return p


def test_control_defaults(tmp_path, monkeypatch):
    _write(tmp_path, "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n", monkeypatch)
    cfg = load_config()
    assert cfg.control_dead_man_timeout == 1.5
    assert cfg.telemetry_default_interval_ms == 1000
    assert cfg.telemetry_min_floor_ms == 200


def test_trace_serial_default_off(tmp_path, monkeypatch):
    _write(tmp_path, "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n", monkeypatch)
    assert load_config().trace_serial is False


def test_trace_serial_from_yaml(tmp_path, monkeypatch):
    _write(
        tmp_path,
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\ntrace_serial: true\n",
        monkeypatch,
    )
    assert load_config().trace_serial is True


def test_control_other_settings_from_yaml(tmp_path, monkeypatch):
    _write(
        tmp_path,
        (
            "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n"
            "control_dead_man_timeout: 2.0\n"
            "telemetry_default_interval_ms: 500\ntelemetry_min_floor_ms: 100\n"
        ),
        monkeypatch,
    )
    cfg = load_config()
    assert cfg.control_dead_man_timeout == 2.0
    assert cfg.telemetry_default_interval_ms == 500
    assert cfg.telemetry_min_floor_ms == 100


def test_legacy_control_enabled_key_is_ignored(tmp_path, monkeypatch):
    from station_agent.config import CONFIG_PATH_ENV, load_config

    p = tmp_path / "c.yml"
    p.write_text(
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k.pem\ncontrol_enabled: true\n"
    )
    monkeypatch.setenv(CONFIG_PATH_ENV, str(p))
    cfg = load_config()
    assert not hasattr(cfg, "control_enabled")


def test_control_rediscovery_interval_default_and_override(tmp_path, monkeypatch):
    from station_agent.config import CONFIG_PATH_ENV, load_config

    p = tmp_path / "c.yml"
    p.write_text(
        "server_url: http://x\nstation_id: 1\ned25519_key_path: /k.pem\n"
        "control_rediscovery_interval: 5.0\n"
    )
    monkeypatch.setenv(CONFIG_PATH_ENV, str(p))
    cfg = load_config()
    assert cfg.control_rediscovery_interval == 5.0
