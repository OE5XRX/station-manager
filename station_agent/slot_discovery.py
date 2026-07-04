"""Slot discovery: scan the OE5XRX slot contract, describe smart modules, report inventory.

The slot contract (`/dev/oe5xrx/slotN/control`) is filled identically by udev on real
hardware and by the sim-harness in simulation. This module only ever consumes that path;
it never touches USB topology. See the design spec:
docs/superpowers/specs/2026-07-04-module-simulation-layer-design.md
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import select
import termios
import time
import tty

logger = logging.getLogger(__name__)

_DESCRIBE_CMD = b"module fm describe\r\n"
_DESCRIBE_PREFIX = "MODULE-DESCRIBE "
_SLOT_RE = re.compile(r"slot(\d+)$")

# A well-formed describe reply is ~1.5 KB. Cap the retained buffer so a noisy or
# garbled peer cannot grow memory (or the per-chunk re-scan cost) without bound
# before the timeout fires. Well above any real reply; exceeding it fails closed.
_MAX_DESCRIBE_BYTES = 65536


def describe_slot(control_path: str, timeout: float = 3.0) -> dict | None:
    """Open a slot control pty, send `describe`, return the parsed JSON or None."""
    try:
        fd = os.open(control_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        logger.debug("slot describe: cannot open %s: %s", control_path, exc)
        return None
    saved_attrs = None
    try:
        try:
            saved_attrs = termios.tcgetattr(fd)
            tty.setraw(fd)
        except termios.error:
            pass  # not a tty (e.g. plain file in a test) — proceed anyway
        try:
            os.write(fd, _DESCRIBE_CMD)
        except OSError:
            logger.debug("slot describe: write failed on %s", control_path)
            return None
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([fd], [], [], remaining)
            if not readable:
                continue
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_DESCRIBE_BYTES:
                logger.debug(
                    "slot describe: response exceeded %d bytes on %s",
                    _MAX_DESCRIBE_BYTES,
                    control_path,
                )
                return None
            parsed = _extract_describe(buf)
            if parsed is not None:
                return parsed
        logger.debug("slot describe: timeout on %s", control_path)
        return None
    finally:
        # Restore the original line discipline so we don't leave the slot control
        # device in raw mode for a subsequent reader (guarded — must never raise).
        if saved_attrs is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved_attrs)
            except (termios.error, OSError):
                pass
        os.close(fd)


def _extract_describe(buf: bytes) -> dict | None:
    text = buf.decode("utf-8", errors="replace")
    for line in text.splitlines():
        idx = line.find(_DESCRIBE_PREFIX)
        if idx == -1:
            continue
        payload = line[idx + len(_DESCRIBE_PREFIX) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue  # line may be truncated; wait for more bytes
        if isinstance(parsed, dict):
            return parsed
        # non-dict (null/number/list) → keep scanning other lines
    return None


def discover_slots(base: str = "/dev/oe5xrx", timeout: float = 3.0) -> list[dict]:
    """Scan `base` for slotN/control, describe each, return inventory entries."""
    if not os.path.isdir(base):
        return []

    def _slot_num(control: str) -> int:
        # Sort by numeric slot index so slot10 follows slot9 (not slot1).
        match = _SLOT_RE.match(os.path.basename(os.path.dirname(control)))
        return int(match.group(1)) if match else -1

    entries: list[dict] = []
    for control in sorted(glob.glob(os.path.join(base, "slot*", "control")), key=_slot_num):
        slot_dir = os.path.basename(os.path.dirname(control))
        match = _SLOT_RE.match(slot_dir)
        if not match:
            continue
        try:
            described = describe_slot(control, timeout=timeout)
            if described is None:
                continue
            entries.append(
                {
                    "slot": int(match.group(1)),
                    "control": control,
                    "identity": described.get("identity", {}),
                    "capabilities": described.get("capabilities", []),
                }
            )
        except Exception:
            # One malformed slot must not discard the whole inventory.
            logger.debug("slot describe: skipping bad slot %s", control, exc_info=True)
            continue
    return entries
