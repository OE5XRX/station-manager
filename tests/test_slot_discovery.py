import json
import os
import re
import threading
import time

from station_agent import slot_discovery

FM_DESCRIBE = {
    "schema": 1,
    "module": "fm",
    "identity": {"type": "fm_transceiver", "model": "SA818-V", "version": "vhf"},
    "capabilities": [{"name": "frequency", "kind": "setting", "type": "float"}],
}
GPS_DESCRIBE = {
    "schema": 1,
    "module": "gps",
    "identity": {"type": "gnss", "model": "NEO-M9", "version": "1"},
    "capabilities": [],
}

_LIST_RE = re.compile(rb"module\s+list\s*$")
_DESCRIBE_RE = re.compile(rb"module\s+(\S+)\s+describe")


def _fake_firmware(master_fd, stop, modules, list_line=None):
    """Emulate the self-describing firmware shell on the pty master side.

    `modules`: ordered dict-like of {id: describe_dict|None}. Responds to
    `module list` with those ids (or the raw `list_line` override) and to
    `module <id> describe` with the matching MODULE-DESCRIBE, or a
    MODULE-RESULT unknown_module error when the id is absent / its value None.
    """
    os.set_blocking(master_fd, False)

    def w(s: str):
        try:
            os.write(master_fd, s.encode())
        except (BlockingIOError, OSError):
            pass

    # Note: no prompt is emitted before the first command — a leading "fm> " would be
    # echoed by the pty and merge (no newline) with the agent's command line. The real
    # firmware parses its own RX and never reads its echoes, so this is a fixture-only
    # concern; command matching below also tolerates an echoed prompt prefix.
    buf = b""
    while not stop.is_set():
        try:
            chunk = os.read(master_fd, 1024)
        except BlockingIOError:
            time.sleep(0.005)
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if _LIST_RE.search(line):
                if list_line is not None:
                    w(list_line + "\r\n")
                else:
                    w("MODULE-LIST " + json.dumps({"modules": list(modules)}) + "\r\n")
                w("fm> ")
                continue
            m = _DESCRIBE_RE.search(line)
            if m:
                mid = m.group(1).decode(errors="replace")
                spec = modules.get(mid)
                if spec is not None:
                    w("MODULE-DESCRIBE " + json.dumps(spec) + "\r\n")
                else:
                    w(
                        "MODULE-RESULT "
                        + json.dumps(
                            {
                                "ok": False,
                                "module": mid,
                                "cap": "",
                                "op": "describe",
                                "error": "unknown_module",
                            }
                        )
                        + "\r\n"
                    )
                w("fm> ")


def _pty_with_firmware(modules, list_line=None):
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()
    t = threading.Thread(
        target=_fake_firmware, args=(master_fd, stop, modules, list_line), daemon=True
    )
    t.start()
    return master_fd, slave_fd, stop, t


def _teardown(master_fd, slave_fd, stop, t):
    stop.set()
    os.close(master_fd)
    os.close(slave_fd)
    t.join(timeout=1)


# --- probe_slot -----------------------------------------------------------


def test_probe_slot_lists_and_describes(tmp_path):
    master_fd, slave_fd, stop, t = _pty_with_firmware({"fm": FM_DESCRIBE})
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=3.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    assert modules is not None
    assert len(modules) == 1
    assert modules[0]["id"] == "fm"
    assert modules[0]["identity"]["type"] == "fm_transceiver"
    assert modules[0]["capabilities"][0]["name"] == "frequency"


def test_probe_slot_enumerates_multiple_modules(tmp_path):
    # dict preserves insertion order (py3.7+), so list order is deterministic.
    master_fd, slave_fd, stop, t = _pty_with_firmware({"fm": FM_DESCRIBE, "gps": GPS_DESCRIBE})
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=3.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    ids = [m["id"] for m in modules]
    assert ids == ["fm", "gps"]
    assert modules[1]["identity"]["type"] == "gnss"


def test_probe_slot_skips_module_that_fails_describe(tmp_path):
    # "gps" is listed but returns unknown_module on describe → skipped; "fm" kept.
    master_fd, slave_fd, stop, t = _pty_with_firmware({"fm": FM_DESCRIBE, "gps": None})
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=1.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    assert [m["id"] for m in modules] == ["fm"]


def test_probe_slot_ignores_invalid_module_id(tmp_path):
    # A garbled id (spaces/control chars) must never be echoed back into a command.
    bad = 'MODULE-LIST {"modules":["fm","bad id;rm -rf"]}'
    master_fd, slave_fd, stop, t = _pty_with_firmware({"fm": FM_DESCRIBE}, list_line=bad)
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=1.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    assert [m["id"] for m in modules] == ["fm"]


def test_probe_slot_empty_module_list(tmp_path):
    master_fd, slave_fd, stop, t = _pty_with_firmware({})
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        modules = slot_discovery.probe_slot(str(link), timeout=1.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    assert modules == []


def test_probe_slot_timeout_returns_none(tmp_path):
    # Master stays open but idle (never answers `module list`) — deterministic timeout.
    master_fd, slave_fd = os.openpty()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        assert slot_discovery.probe_slot(str(link), timeout=0.5) is None
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_probe_slot_missing_modules_key_returns_none(tmp_path):
    # A MODULE-LIST object without the required `modules` key is malformed → fail closed.
    master_fd, slave_fd, stop, t = _pty_with_firmware({}, list_line="MODULE-LIST {}")
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        assert slot_discovery.probe_slot(str(link), timeout=0.5) is None
    finally:
        _teardown(master_fd, slave_fd, stop, t)


def test_probe_slot_missing_path_returns_none(tmp_path):
    assert slot_discovery.probe_slot(str(tmp_path / "nope"), timeout=0.5) is None


def test_probe_slot_broken_peer_returns_none(tmp_path):
    master_fd, slave_fd = os.openpty()
    link = tmp_path / "control"
    link.symlink_to(os.ttyname(slave_fd))
    os.close(master_fd)  # peer gone before probe
    try:
        assert slot_discovery.probe_slot(str(link), timeout=0.5) is None
    finally:
        os.close(slave_fd)


def test_probe_slot_non_dict_list_returns_none(tmp_path):
    # A syntactically valid but non-object MODULE-LIST payload must fail closed.
    master_fd, slave_fd, stop, t = _pty_with_firmware({}, list_line="MODULE-LIST null")
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        assert slot_discovery.probe_slot(str(link), timeout=0.5) is None
    finally:
        _teardown(master_fd, slave_fd, stop, t)


def test_probe_slot_caps_runaway_buffer(tmp_path):
    """A peer that floods bytes with no MODULE-LIST must fail closed via the byte cap."""
    master_fd, slave_fd = os.openpty()
    stop = threading.Event()

    def flood(fd, stop):
        os.set_blocking(fd, False)
        while not stop.is_set():
            try:
                os.write(fd, b"x" * 4096)
            except BlockingIOError:
                time.sleep(0.001)
            except OSError:
                break

    t = threading.Thread(target=flood, args=(master_fd, stop), daemon=True)
    t.start()
    try:
        link = tmp_path / "control"
        link.symlink_to(os.ttyname(slave_fd))
        result = slot_discovery.probe_slot(str(link), timeout=10.0)
    finally:
        stop.set()
        os.close(master_fd)
        os.close(slave_fd)
        t.join(timeout=1)
    assert result is None


# --- discover_slots -------------------------------------------------------


def test_discover_slots_missing_base_returns_empty(tmp_path):
    assert slot_discovery.discover_slots(str(tmp_path / "absent")) == []


def test_discover_slots_reports_slot_with_modules(tmp_path):
    master_fd, slave_fd, stop, t = _pty_with_firmware({"fm": FM_DESCRIBE})
    try:
        slot_dir = tmp_path / "slot1"
        slot_dir.mkdir()
        (slot_dir / "control").symlink_to(os.ttyname(slave_fd))
        slots = slot_discovery.discover_slots(str(tmp_path), timeout=3.0)
    finally:
        _teardown(master_fd, slave_fd, stop, t)
    assert len(slots) == 1
    assert slots[0]["slot"] == 1
    assert slots[0]["modules"][0]["id"] == "fm"
    assert slots[0]["modules"][0]["identity"]["type"] == "fm_transceiver"
