"""Stateful pty fake-firmware harness for broker end-to-end tests.

Speaks the same self-describing shell protocol as the real firmware on native_sim:
``module list`` -> MODULE-LIST, ``module <id> describe`` -> MODULE-DESCRIBE, and
``module <id> <op> <cap> [token]`` -> MODULE-RESULT. set/do mutate per-module state;
get reads it. This is the transport under broker E2E tests — no hardware, no server.
"""

from __future__ import annotations

import json
import os
import re
import termios
import threading
import time
import tty

_LIST_RE = re.compile(rb"module\s+list\s*$")
_DESCRIBE_RE = re.compile(rb"module\s+(\S+)\s+describe\s*$")
_CMD_RE = re.compile(rb"module\s+(\S+)\s+(set|get|do)\s+(\S+)(?:\s+(\S+))?\s*$")


class FakeFirmware:
    def __init__(self, modules: dict):
        self._modules = modules
        self.state: dict = {mid: {} for mid in modules}
        self._master_fd, self._slave_fd = os.openpty()
        # Real UARTs deliver raw bytes: no line discipline, no echo, no CR/LF
        # mapping. A bare openpty() slave is canonical+echoing, which hides
        # file-open bugs that a real UART would expose. Force raw on both ends.
        for fd in (self._master_fd, self._slave_fd):
            try:
                tty.setraw(fd)
            except termios.error:
                pass
        self.control_path = os.ttyname(self._slave_fd)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        try:
            os.close(self._slave_fd)
        except OSError:
            pass
        self._thread.join(timeout=1)

    def _w(self, s: str):
        try:
            os.write(self._master_fd, s.encode())
        except (BlockingIOError, OSError):
            pass

    def _result(self, mid, cap, op, ok, value=None, error=None):
        body = {"ok": ok, "module": mid, "cap": cap, "op": op}
        if ok:
            body["value"] = value
        else:
            body["error"] = error
        self._w("MODULE-RESULT " + json.dumps(body) + "\r\n")

    def _cap(self, mid, cap):
        for c in self._modules.get(mid, {}).get("capabilities", []):
            if c.get("name") == cap:
                return c
        return None

    def _handle_cmd(self, mid, op, cap, token):
        if mid not in self._modules:
            self._result(mid, cap, op, False, error="unknown_module")
            return
        c = self._cap(mid, cap)
        if c is None:
            self._result(mid, cap, op, False, error="unknown_capability")
            return
        if op == "get":
            # Telemetry returns a canned reading; settings echo last-set value.
            if c.get("kind") == "telemetry":
                val = 42 if c.get("type") == "int" else "vhf"
            else:
                val = self.state[mid].get(cap)
            self._result(mid, cap, op, True, value=val)
            return
        # set / do: store the raw token (tests assert on it) and echo it back.
        self.state[mid][cap] = token
        self._result(mid, cap, op, True, value=token)

    def _serve(self):
        os.set_blocking(self._master_fd, False)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = os.read(self._master_fd, 1024)
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
                    self._w("MODULE-LIST " + json.dumps({"modules": list(self._modules)}) + "\r\n")
                    continue
                m = _DESCRIBE_RE.search(line)
                if m:
                    mid = m.group(1).decode(errors="replace")
                    spec = self._modules.get(mid)
                    if spec is not None:
                        self._w("MODULE-DESCRIBE " + json.dumps(spec) + "\r\n")
                    else:
                        self._result(mid, "", "describe", False, error="unknown_module")
                    continue
                m = _CMD_RE.search(line)
                if m:
                    mid = m.group(1).decode(errors="replace")
                    op = m.group(2).decode()
                    cap = m.group(3).decode(errors="replace")
                    token = m.group(4).decode(errors="replace") if m.group(4) else None
                    self._handle_cmd(mid, op, cap, token)


def make_slot_tree(tmp_path, slots: dict) -> str:
    """Build a slotN/control symlink tree under tmp_path; return the base dir."""
    base = tmp_path / "oe5xrx"
    base.mkdir(exist_ok=True)
    for num, fw in slots.items():
        slot_dir = base / f"slot{num}"
        slot_dir.mkdir(exist_ok=True)
        (slot_dir / "control").symlink_to(fw.control_path)
    return str(base)
