"""Integration tests: slot discovery folded into collect_inventory."""

from station_agent import inventory
from station_agent.config import AgentConfig


def _cfg(**kw):
    return AgentConfig(server_url="https://x.test", station_id=1, ed25519_key_path="/tmp/k", **kw)


def test_collect_inventory_includes_modules(monkeypatch):
    fake = [{"slot": 1, "control": "/dev/oe5xrx/slot1/control",
             "identity": {"type": "fm_transceiver"}, "capabilities": []}]
    monkeypatch.setattr("station_agent.inventory.discover_slots", lambda base, timeout=3.0: fake)
    data = inventory.collect_inventory(config=_cfg(slot_discovery_enabled=True))
    assert data["modules"] == fake


def test_collect_inventory_discovery_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr("station_agent.inventory.discover_slots",
                        lambda base, timeout=3.0: calls.append(base) or [])
    data = inventory.collect_inventory(config=_cfg(slot_discovery_enabled=False))
    assert data.get("modules", []) == []
    assert calls == []  # discovery must NOT have been called when disabled


def test_collect_inventory_discovery_failure_is_swallowed(monkeypatch):
    def boom(base, timeout=3.0):
        raise OSError("device gone")
    monkeypatch.setattr("station_agent.inventory.discover_slots", boom)
    data = inventory.collect_inventory(config=_cfg(slot_discovery_enabled=True))
    # Assert the key is present (not .get default): proves the INNER
    # _collect_modules guard returned [], not the outer except returning {}.
    assert data["modules"] == []  # did not raise
