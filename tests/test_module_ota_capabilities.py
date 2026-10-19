from station_agent import heartbeat, module_ota
from station_agent.config import AgentConfig


def _cfg(tmp_path, **kw):
    kw.setdefault("slots_conf", str(tmp_path / "slots.conf"))
    kw.setdefault("sim_dir", str(tmp_path / "sim"))
    return AgentConfig(server_url="https://x", station_id=1, ed25519_key_path="/tmp/k", **kw)


def test_config_defaults():
    c = AgentConfig()
    assert c.module_ota_enabled is True
    assert c.module_fw_dir == "/var/lib/station-agent/module-fw"
    assert c.slots_conf == "/etc/oe5xrx/slots.conf"
    assert c.sim_dir == "/var/lib/oe5xrx/sim"
    assert c.sim_service == "oe5xrx-sim-harness"
    assert c.dfu_util == "dfu-util"


def test_load_config_maps_fields(tmp_path, monkeypatch):
    f = tmp_path / "c.yml"
    f.write_text(
        "server_url: https://x\nstation_id: 1\ned25519_key_path: /k\n"
        "module_ota_enabled: false\nmodule_fw_dir: /a\nslots_conf: /b\n"
        "sim_dir: /c\nsim_service: svc\ndfu_util: /usr/bin/d\n"
    )
    monkeypatch.setenv("STATION_AGENT_CONFIG", str(f))
    from station_agent.config import load_config

    c = load_config()
    assert (c.module_ota_enabled, c.module_fw_dir, c.slots_conf) == (False, "/a", "/b")
    assert (c.sim_dir, c.sim_service, c.dfu_util) == ("/c", "svc", "/usr/bin/d")


def test_detect_backends_dfu(tmp_path, monkeypatch):
    (tmp_path / "slots.conf").write_text("")
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: "/usr/bin/dfu-util")
    assert module_ota.detect_backends(_cfg(tmp_path)) == ["dfu"]


def test_detect_backends_dfu_needs_both(tmp_path, monkeypatch):
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: "/usr/bin/dfu-util")
    assert module_ota.detect_backends(_cfg(tmp_path)) == []
    (tmp_path / "slots.conf").write_text("")
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: None)
    assert module_ota.detect_backends(_cfg(tmp_path)) == []


def test_detect_backends_sim(tmp_path, monkeypatch):
    (tmp_path / "sim").mkdir()
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: None)
    assert module_ota.detect_backends(_cfg(tmp_path)) == ["sim"]


def test_detect_backends_both(tmp_path, monkeypatch):
    (tmp_path / "sim").mkdir()
    (tmp_path / "slots.conf").write_text("")
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: "/x")
    assert module_ota.detect_backends(_cfg(tmp_path)) == ["dfu", "sim"]


def test_detect_backends_none(tmp_path, monkeypatch):
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: None)
    assert module_ota.detect_backends(_cfg(tmp_path)) == []


def test_detect_backends_disabled(tmp_path, monkeypatch):
    (tmp_path / "sim").mkdir()
    (tmp_path / "slots.conf").write_text("")
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: "/x")
    assert module_ota.detect_backends(_cfg(tmp_path, module_ota_enabled=False)) == []


def _patch_heartbeat(monkeypatch):
    monkeypatch.setattr(heartbeat, "collect_inventory", lambda config=None: {})
    monkeypatch.setattr(heartbeat, "_collect_telemetry_safe", lambda c: {})


def test_heartbeat_includes_capabilities(tmp_path, monkeypatch):
    _patch_heartbeat(monkeypatch)
    (tmp_path / "sim").mkdir()
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: None)
    info = heartbeat.collect_system_info(config=_cfg(tmp_path))
    assert info["capabilities"] == {"module_ota": {"backends": ["sim"]}}


def test_heartbeat_capabilities_empty_without_backend(tmp_path, monkeypatch):
    _patch_heartbeat(monkeypatch)
    monkeypatch.setattr(module_ota.shutil, "which", lambda n: None)
    assert heartbeat.collect_system_info(config=_cfg(tmp_path))["capabilities"] == {}
    assert heartbeat.collect_system_info(config=None)["capabilities"] == {}


def test_heartbeat_advertises_explicit_backends(tmp_path, monkeypatch):
    """The agent passes the backends its ModuleOta can execute; heartbeat uses exactly those."""
    _patch_heartbeat(monkeypatch)
    info = heartbeat.collect_system_info(config=_cfg(tmp_path), backends=["sim"])
    assert info["capabilities"] == {"module_ota": {"backends": ["sim"]}}
    assert heartbeat.collect_system_info(config=_cfg(tmp_path), backends=[])["capabilities"] == {}
