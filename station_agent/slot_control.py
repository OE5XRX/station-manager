# station_agent/slot_control.py
"""Concrete slot-control serial/shell transport — the device I/O layer.

This is the only station_agent module that talks to a slot's ``control`` device.
It writes one generic ``module <id> <op> <cap> [token]`` command and reads the
firmware's ``MODULE-RESULT`` reply. The broker above it is device-agnostic; all
serial/shell specifics (line discipline, byte cap, timeouts) live here. Mirrors
the proven read loop from ``slot_discovery`` and never raises into the caller.
"""

from __future__ import annotations

import logging
import os
import select
import termios
import time
import tty

from station_agent.descriptor import TOKEN_RE
from station_agent.slot_discovery import (
    _MAX_RESPONSE_BYTES,
    _MODULE_ID_RE,
    _extract_json,
)

logger = logging.getLogger(__name__)

_RESULT_PREFIX = "MODULE-RESULT "
_TIMEOUT_RESULT = {"ok": False, "error": "timeout"}


class SlotControl:
    def __init__(self, control_path: str, timeout: float = 3.0):
        self._path = control_path
        self._timeout = timeout

    def execute(self, module_id: str, op: str, cap: str, token: str | None = None) -> dict:
        """Send one command, return the parsed MODULE-RESULT (or a timeout error)."""
        # Defense-in-depth: never echo an unsafe id into the shell line, and fail
        # closed on a non-str module_id/cap so .match() can't raise TypeError.
        if not (isinstance(module_id, str) and _MODULE_ID_RE.match(module_id)) or not (
            isinstance(cap, str) and _MODULE_ID_RE.match(cap)
        ):
            return {"ok": False, "error": "bad_value"}
        # This transport is the device trust boundary: reject any op outside the
        # generic FW verb set before it reaches the command line, independent of
        # whatever the broker above happens to send.
        if op not in ("set", "get", "do"):
            return {"ok": False, "error": "bad_value"}
        if token is not None and (not isinstance(token, str) or not TOKEN_RE.match(token)):
            return {"ok": False, "error": "bad_value"}
        parts = ["module", module_id, op, cap]
        if token is not None:
            parts.append(token)
        cmd = (" ".join(parts) + "\r\n").encode()

        try:
            fd = os.open(self._path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            logger.debug("slot control: cannot open %s: %s", self._path, exc)
            return dict(_TIMEOUT_RESULT)

        saved = None
        try:
            try:
                saved = termios.tcgetattr(fd)
                tty.setraw(fd)
            except termios.error:
                pass
            return self._converse(fd, cmd)
        finally:
            if saved is not None:
                try:
                    termios.tcsetattr(fd, termios.TCSANOW, saved)
                except (termios.error, OSError):
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    def _converse(self, fd: int, cmd: bytes) -> dict:
        try:
            os.write(fd, cmd)
        except OSError:
            return dict(_TIMEOUT_RESULT)
        deadline = time.monotonic() + self._timeout
        buf = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(_TIMEOUT_RESULT)
            try:
                readable, _, _ = select.select([fd], [], [], remaining)
            except InterruptedError:
                continue
            except OSError:
                return dict(_TIMEOUT_RESULT)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return dict(_TIMEOUT_RESULT)
            if not chunk:
                return dict(_TIMEOUT_RESULT)
            buf += chunk
            if len(buf) > _MAX_RESPONSE_BYTES:
                return dict(_TIMEOUT_RESULT)
            parsed = _extract_json(buf, _RESULT_PREFIX)
            if parsed is not None:
                return parsed
