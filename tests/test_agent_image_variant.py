"""Tests for agent heartbeat image_variant reporting (Task 10)."""

from station_agent import heartbeat


def test_get_image_variant_reads_variant_id(tmp_path, monkeypatch):
    osr = tmp_path / "os-release"
    osr.write_text('PRETTY_NAME="OE5XRX Remote Station v1"\nVARIANT_ID="dev"\n')
    monkeypatch.setattr(heartbeat, "_OS_RELEASE_PATH", str(osr), raising=False)
    assert heartbeat.get_image_variant() == "dev"


def test_get_image_variant_missing_returns_empty(tmp_path, monkeypatch):
    osr = tmp_path / "os-release"
    osr.write_text('PRETTY_NAME="x"\n')
    monkeypatch.setattr(heartbeat, "_OS_RELEASE_PATH", str(osr), raising=False)
    assert heartbeat.get_image_variant() == ""


def test_get_image_variant_file_missing_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        heartbeat, "_OS_RELEASE_PATH", str(tmp_path / "nonexistent"), raising=False
    )
    assert heartbeat.get_image_variant() == ""


def test_payload_includes_image_variant(monkeypatch):
    monkeypatch.setattr(heartbeat, "get_image_variant", lambda: "release")
    payload = heartbeat.collect_system_info()
    assert payload["image_variant"] == "release"
