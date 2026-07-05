"""Unit tests for station_agent.config defaults and loading."""

from station_agent.config import AgentConfig


def test_slot_discovery_defaults():
    cfg = AgentConfig(
        server_url="https://example.test",
        station_id=1,
        ed25519_key_path="/tmp/k",
    )
    assert cfg.slot_discovery_enabled is True
    assert cfg.slot_dev_base == "/dev/oe5xrx"
