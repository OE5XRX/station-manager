"""Unit tests for station_agent.config defaults and loading."""

import os

from station_agent.config import AgentConfig, load_config


def test_slot_discovery_defaults():
    cfg = AgentConfig(
        server_url="https://example.test",
        station_id=1,
        ed25519_key_path="/tmp/k",
    )
    assert cfg.slot_discovery_enabled is True
    assert cfg.slot_dev_base == "/dev/oe5xrx"


def _write(tmp_path, body):
    p = tmp_path / "config.yml"
    p.write_text(body)
    os.environ["STATION_AGENT_CONFIG"] = str(p)
    return p


def test_control_defaults_off(tmp_path, monkeypatch):
    monkeypatch.delenv("STATION_AGENT_CONFIG", raising=False)
    _write(tmp_path, "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n")
    cfg = load_config()
    assert cfg.control_enabled is False
    assert cfg.control_dead_man_timeout == 1.5
    assert cfg.telemetry_default_interval_ms == 1000
    assert cfg.telemetry_min_floor_ms == 200


def test_control_enabled_from_yaml(tmp_path, monkeypatch):
    monkeypatch.delenv("STATION_AGENT_CONFIG", raising=False)
    _write(
        tmp_path,
        (
            "server_url: http://x\nstation_id: 1\ned25519_key_path: /k\n"
            "control_enabled: true\ncontrol_dead_man_timeout: 2.0\n"
            "telemetry_default_interval_ms: 500\ntelemetry_min_floor_ms: 100\n"
        ),
    )
    cfg = load_config()
    assert cfg.control_enabled is True
    assert cfg.control_dead_man_timeout == 2.0
    assert cfg.telemetry_default_interval_ms == 500
    assert cfg.telemetry_min_floor_ms == 100
