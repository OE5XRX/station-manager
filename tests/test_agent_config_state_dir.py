import os
import textwrap

from station_agent.config import AgentConfig, load_config


def test_state_dir_defaults():
    cfg = AgentConfig()
    assert cfg.state_dir == "/var/lib/station-agent"


def test_state_dir_loaded_from_yaml(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(textwrap.dedent("""
        server_url: https://example.test
        station_id: 1
        ed25519_key_path: /tmp/key.pem
        state_dir: /data/agent-state
    """))
    monkeypatch.setenv("STATION_AGENT_CONFIG", str(cfg_file))
    cfg = load_config()
    assert cfg.state_dir == "/data/agent-state"


def test_state_dir_not_required_for_validate():
    cfg = AgentConfig(
        server_url="https://x", station_id=1, ed25519_key_path="/k.pem", state_dir=""
    )
    cfg.validate()  # must not raise
