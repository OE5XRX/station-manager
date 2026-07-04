import json
import os
import threading
import time

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


def _fake_module_reply(master_fd, stop, reply_line):
    """Emit a fixed MODULE-DESCRIBE reply (e.g. a non-dict payload)."""
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
            os.write(master_fd, reply_line.encode())
            buf = b""


def _pty_with_reply(reply_line):
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(
        target=_fake_module_reply, args=(master_fd, stop, reply_line), daemon=True
    )
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


def test_describe_slot_broken_peer_returns_none(tmp_path):
    """Peer (firmware/native_sim) gone: describe must fail closed, never raise.

    Point 'control' at the slave, then close the master before calling
    describe_slot so the initial write hits a dead peer (EIO/BrokenPipe).
    """
    master_fd, slave_fd = os.openpty()
    link = tmp_path / "control"
    link.symlink_to(os.ttyname(slave_fd))
    os.close(master_fd)  # kill the peer before we describe
    try:
        result = slot_discovery.describe_slot(str(link), timeout=0.5)
    finally:
        os.close(slave_fd)
    assert result is None


def test_describe_slot_non_dict_payload_returns_none(tmp_path):
    """A syntactically-valid but non-object payload (null/number/list) must
    yield None, honoring the 'return dict or None' contract — never a scalar."""
    master_fd, slave_fd, stop, t = _pty_with_reply("MODULE-DESCRIBE null\r\n")
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        result = slot_discovery.describe_slot(str(link), timeout=0.5)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert result is None


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


def _fake_module_flood(master_fd, stop):
    """Continuously write bytes with no MODULE-DESCRIBE line, to exercise the
    buffer cap. The master is set non-blocking so that once describe_slot stops
    reading (after hitting the cap), a full slave buffer yields BlockingIOError
    instead of a stuck write — the thread stays responsive to `stop`."""
    os.set_blocking(master_fd, False)
    while not stop.is_set():
        try:
            os.write(master_fd, b"x" * 4096)
        except BlockingIOError:
            time.sleep(0.001)  # slave buffer full — back off briefly
        except OSError:
            break


def test_describe_slot_caps_runaway_buffer(tmp_path):
    """A peer that never sends MODULE-DESCRIBE must fail closed via the byte cap,
    returning None without unbounded memory growth (and before the timeout)."""
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(target=_fake_module_flood, args=(master_fd, stop), daemon=True)
    t.start()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        # Generous timeout: the cap (not the timeout) must be what returns None.
        result = slot_discovery.describe_slot(str(link), timeout=10.0)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert result is None
