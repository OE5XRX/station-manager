"""Slot discovery: scan the OE5XRX slot contract, enumerate + describe modules, report inventory.

The slot contract (`/dev/oe5xrx/slotN/control`) is filled identically by udev on real
hardware and by the sim-harness in simulation. This module only ever consumes that path;
it never touches USB topology. See the design spec:
docs/superpowers/specs/2026-07-04-module-simulation-layer-design.md

Per slot, the firmware is self-describing over its control serial:
  1. ``module list``            -> ``MODULE-LIST {"modules":["fm", ...]}``  (module ids)
  2. ``module <id> describe``   -> ``MODULE-DESCRIBE {schema, module, identity, capabilities}``
Module ids are NOT hardcoded — we enumerate whatever the firmware reports and describe each.
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

_LIST_CMD = b"module list\r\n"
_LIST_PREFIX = "MODULE-LIST "
_DESCRIBE_PREFIX = "MODULE-DESCRIBE "
_SLOT_RE = re.compile(r"slot(\d+)$")
# Module ids come from the device; only accept simple tokens before echoing one back
# in a command, so a garbled/hostile peer can't inject control bytes into the shell.
_MODULE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")

# A well-formed list/describe reply is well under this. Cap the retained buffer so a
# noisy or garbled peer cannot grow memory (or the per-chunk re-scan cost) without bound
# before the timeout fires. Well above any real reply; exceeding it fails closed.
_MAX_RESPONSE_BYTES = 65536


def probe_slot(control_path: str, timeout: float = 3.0) -> list[dict] | None:
    """Enumerate + describe every module reachable on a slot's control pty.

    Sends ``module list`` then ``module <id> describe`` for each reported id, over a
    single connection. Returns a list of ``{"id", "identity", "capabilities"}`` (possibly
    empty if the firmware lists no modules), or ``None`` if the slot cannot be opened or
    does not answer ``module list``. Never raises — discovery must not disrupt the heartbeat.
    """
    try:
        fd = os.open(control_path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        logger.debug("slot probe: cannot open %s: %s", control_path, exc)
        return None
    saved_attrs = None
    try:
        try:
            saved_attrs = termios.tcgetattr(fd)
            tty.setraw(fd)
        except termios.error:
            pass  # not a tty (e.g. plain file in a test) — proceed anyway

        deadline = time.monotonic() + timeout

        listing = _command(fd, _LIST_CMD, _LIST_PREFIX, deadline, control_path)
        if listing is None:
            logger.debug("slot probe: no MODULE-LIST from %s", control_path)
            return None
        ids = listing.get("modules", [])
        if not isinstance(ids, list):
            return None

        modules: list[dict] = []
        for mid in ids:
            if not isinstance(mid, str) or not _MODULE_ID_RE.match(mid):
                logger.debug("slot probe: skipping invalid module id %r on %s", mid, control_path)
                continue
            cmd = f"module {mid} describe\r\n".encode()
            described = _command(fd, cmd, _DESCRIBE_PREFIX, deadline, control_path)
            if described is None:
                logger.debug("slot probe: no describe for module %s on %s", mid, control_path)
                continue
            modules.append(
                {
                    "id": mid,
                    "identity": described.get("identity", {}),
                    "capabilities": described.get("capabilities", []),
                }
            )
        return modules
    finally:
        # Restore the original line discipline so we don't leave the slot control
        # device in raw mode for a subsequent reader (guarded — must never raise).
        if saved_attrs is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved_attrs)
            except (termios.error, OSError):
                pass
        try:
            os.close(fd)
        except OSError:
            pass  # close() can raise (e.g. EINTR) — must never raise into the heartbeat


def _command(fd: int, cmd: bytes, prefix: str, deadline: float, control_path: str) -> dict | None:
    """Write a shell command and read until a line carrying `prefix` parses as a JSON dict.

    Returns the parsed dict, or None on write error / timeout / EOF / read error / oversize.
    """
    try:
        os.write(fd, cmd)
    except OSError:
        logger.debug("slot probe: write failed on %s", control_path)
        return None
    buf = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            readable, _, _ = select.select([fd], [], [], remaining)
        except InterruptedError:
            continue
        except OSError:
            return None  # fail closed on any select error
        if not readable:
            continue
        try:
            chunk = os.read(fd, 4096)
        except (BlockingIOError, InterruptedError):
            continue
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        if len(buf) > _MAX_RESPONSE_BYTES:
            logger.debug(
                "slot probe: response exceeded %d bytes on %s", _MAX_RESPONSE_BYTES, control_path
            )
            return None
        parsed = _extract_json(buf, prefix)
        if parsed is not None:
            return parsed


def _extract_json(buf: bytes, prefix: str) -> dict | None:
    text = buf.decode("utf-8", errors="replace")
    for line in text.splitlines():
        idx = line.find(prefix)
        if idx == -1:
            continue
        payload = line[idx + len(prefix) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue  # line may be truncated; wait for more bytes
        if isinstance(parsed, dict):
            return parsed
        # non-dict (null/number/list) → keep scanning other lines
    return None


def discover_slots(base: str = "/dev/oe5xrx", timeout: float = 3.0) -> list[dict]:
    """Scan `base` for slotN/control, enumerate + describe modules, return inventory entries.

    Each entry is ``{"slot": N, "control": path, "modules": [{"id", "identity",
    "capabilities"}, ...]}``. Slots that do not answer ``module list`` are omitted.
    Returns ``[]`` if `base` does not exist. Never raises.
    """
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
            modules = probe_slot(control, timeout=timeout)
            if modules is None:
                continue
            entries.append(
                {
                    "slot": int(match.group(1)),
                    "control": control,
                    "modules": modules,
                }
            )
        except Exception:
            # One malformed slot must not discard the whole inventory.
            logger.debug("slot probe: skipping bad slot %s", control, exc_info=True)
            continue
    return entries
