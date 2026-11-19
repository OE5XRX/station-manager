import textwrap

import pytest

from station_agent.config import AgentConfig, load_config


def _load(tmp_path, monkeypatch, extra=""):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        textwrap.dedent("""
        server_url: https://example.test
        station_id: 1
        ed25519_key_path: /tmp/key.pem
    """)
        + extra
    )
    monkeypatch.setenv("STATION_AGENT_CONFIG", str(cfg_file))
    return load_config()


def test_stall_limit_defaults_to_hours(tmp_path, monkeypatch):
    assert AgentConfig().ota_download_stall_limit == 6 * 3600
    assert _load(tmp_path, monkeypatch).ota_download_stall_limit == 6 * 3600


def test_stall_limit_loaded_from_yaml(tmp_path, monkeypatch):
    cfg = _load(tmp_path, monkeypatch, "ota_download_stall_limit: 600\n")
    assert cfg.ota_download_stall_limit == 600


def test_negative_stall_limit_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="ota_download_stall_limit"):
        _load(tmp_path, monkeypatch, "ota_download_stall_limit: -1\n")
