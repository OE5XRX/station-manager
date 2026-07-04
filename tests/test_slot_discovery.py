import json
import os
import threading

from station_agent import slot_discovery

DESCRIBE_JSON = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
}


def _fake_module(master_fd, stop):
    """Emit a MODULE-DESCRIBE line when it reads 'module fm describe'.

    The fake module holds the master side of the pty pair.  The agent
    (describe_slot) opens the slave device exposed via the 'control' symlink.
    """
    buf = b""
    os.write(master_fd, b"fm> ")
    while not stop.is_set():
        try:
            chunk = os.read(master_fd, 1024)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if b"module fm describe" in buf:
            line = "MODULE-DESCRIBE " + json.dumps(DESCRIBE_JSON) + "\r\n"
            os.write(master_fd, line.encode())
            buf = b""


def _pty_with_module():
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(target=_fake_module, args=(master_fd, stop), daemon=True)
    t.start()
    return master_fd, slave_fd, stop, t


def test_describe_slot_parses_identity(tmp_path):
    master_fd, slave_fd, stop, t = _pty_with_module()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        result = slot_discovery.describe_slot(str(link), timeout=3.0)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert result is not None
    assert result["identity"]["type"] == "fm_transceiver"
    assert result["module"] == "fm"


def test_describe_slot_timeout_returns_none(tmp_path):
    master_fd, slave_fd = os.openpty()  # nobody answers
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        result = slot_discovery.describe_slot(str(link), timeout=0.5)
    finally:
        os.close(master_fd)
        os.close(slave_fd)
    assert result is None


def test_describe_slot_missing_path_returns_none(tmp_path):
    assert slot_discovery.describe_slot(str(tmp_path / "nope"), timeout=0.5) is None


def test_discover_slots_missing_base_returns_empty(tmp_path):
    assert slot_discovery.discover_slots(str(tmp_path / "absent")) == []


def test_discover_slots_reports_slot(tmp_path):
    master_fd, slave_fd, stop, t = _pty_with_module()
    try:
        slot_dir = tmp_path / "slot1"
        slot_dir.mkdir()
        (slot_dir / "control").symlink_to(os.ttyname(slave_fd))
        slots = slot_discovery.discover_slots(str(tmp_path), timeout=3.0)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert len(slots) == 1
    assert slots[0]["slot"] == 1
    assert slots[0]["identity"]["type"] == "fm_transceiver"
