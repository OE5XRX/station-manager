"""Slot discovery: scan the OE5XRX slot contract, enumerate + describe modules, report inventory.

The slot contract (`/dev/oe5xrx/slotN/control`) is filled identically by udev on real
hardware and by the sim-harness in simulation. This module only ever consumes that path;
it never touches USB topology. See the design spec:
docs/superpowers/specs/2026-07-04-module-simulation-layer-design.md

Per slot, the firmware is self-describing over its control serial:
  1. ``module list``            -> ``MODULE-LIST {"modules":["fm", ...]}``  (module ids)
  2. ``module <id> describe``   -> ``MODULE-DESCRIBE {schema, module, identity, capabilities}``
Module ids are NOT hardcoded — we enumerate whatever the firmware reports and describe each.

The control endpoint is a USB-CDC ACM serial line. We drive it with pyserial (concrete line
settings + read/write timeouts) rather than raw ``os.open`` + ``termios``. Right after opening
the device may still be emitting bytes — a boot/status banner on a fresh connect, or a leftover
shell prompt/echo — so we drain the link until it goes quiet before sending, then read framed
replies. This is I/O hygiene: it keeps our command and its ``MODULE-*`` reply from interleaving
with that unsolicited output, so the line parser never trips over stale bytes.

To be clear about what this is NOT: draining is not a workaround for a device hang. A firmware
bug used to make ``module list``/``describe`` hard-fault the module (a command-buffer stack
overflow), wedging its USB until a physical power cycle — that was fixed in the firmware
(FW-RemoteStation), not here, and is unrelated to how fast the agent sends. Opening the port
does not reset this module either (toggling DTR/RTS has no observable effect on it); the drain
simply absorbs whatever the firmware happens to have already queued.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time

import serial

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

_SERIAL_BAUD = 115200
# CDC-ACM ignores the actual bit rate, but pyserial still needs concrete line settings;
# these also make the intent explicit and let a real UART peer match.
_READ_TIMEOUT = 0.2  # seconds per blocking read in the drain / reply loops
_WRITE_TIMEOUT = 2.0
# Consider whatever the device is emitting on open (boot banner / stale prompt) finished once
# the link has been silent for _BOOT_QUIET seconds — but never wait longer than _BOOT_MAX.
_BOOT_QUIET = 0.3
_BOOT_MAX = 2.5


def probe_slot(control_path: str, timeout: float = 3.0, trace: bool = False) -> list[dict] | None:
    """Enumerate + describe every module reachable on a slot's control serial.

    Sends ``module list`` then ``module <id> describe`` for each reported id, over a
    single connection. Returns a list of ``{"id", "identity", "capabilities"}`` (possibly
    empty if the firmware lists no modules), or ``None`` if the slot cannot be opened or
    does not answer ``module list``. Never raises — discovery must not disrupt the heartbeat.
    """
    try:
        ser = serial.Serial(
            port=control_path,
            baudrate=_SERIAL_BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=_READ_TIMEOUT,
            write_timeout=_WRITE_TIMEOUT,
        )
    except (serial.SerialException, ValueError, OSError) as exc:
        logger.debug("slot probe: cannot open %s: %s", control_path, exc)
        return None

    try:
        # Drain any unsolicited output already on the link (boot banner / stale prompt) until
        # it goes quiet, so it can't interleave with our command's reply (see module docstring).
        _drain_until_quiet(ser, _BOOT_QUIET, _BOOT_MAX)

        deadline = time.monotonic() + timeout

        listing = _command(ser, _LIST_CMD, _LIST_PREFIX, deadline, control_path, trace=trace)
        if listing is None:
            logger.debug("slot probe: no MODULE-LIST from %s", control_path)
            return None
        # Fail closed if `modules` is missing or not a list; a present empty list
        # legitimately means "firmware responded, no modules".
        ids = listing.get("modules")
        if not isinstance(ids, list):
            return None

        modules: list[dict] = []
        for mid in ids:
            if not isinstance(mid, str) or not _MODULE_ID_RE.match(mid):
                logger.debug("slot probe: skipping invalid module id %r on %s", mid, control_path)
                continue
            cmd = f"module {mid} describe\r\n".encode()
            described = _command(ser, cmd, _DESCRIBE_PREFIX, deadline, control_path, trace=trace)
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
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass  # close() must never raise into the heartbeat


def _drain_until_quiet(ser: serial.Serial, quiet: float, max_wait: float) -> None:
    """Read and discard whatever the device is already emitting (boot banner / stale prompt)
    until the link has been silent for `quiet` seconds, or `max_wait` elapses. Never raises."""
    end = time.monotonic() + max_wait
    last_data = time.monotonic()
    while time.monotonic() < end:
        try:
            chunk = ser.read(4096)  # blocks up to ser.timeout, then returns what it has
        except (serial.SerialException, OSError):
            return
        now = time.monotonic()
        if chunk:
            last_data = now
        elif now - last_data >= quiet:
            return


def _command(
    ser: serial.Serial,
    cmd: bytes,
    prefix: str,
    deadline: float,
    control_path: str,
    trace: bool = False,
) -> dict | None:
    """Write a shell command and read until a line carrying `prefix` parses as a JSON dict.

    Returns the parsed dict, or None on write error / timeout / read error / oversize.
    """
    from station_agent import serial_trace

    try:
        ser.reset_input_buffer()  # drop any stale bytes (prompt/echo) before this command
        written = ser.write(cmd)
    except (serial.SerialException, OSError):
        logger.debug("slot probe: write failed on %s", control_path)
        return None
    if written is not None and written < len(cmd):
        # A short write (e.g. write timeout) puts a truncated command on the wire;
        # fail closed instead of waiting for a reply that can never arrive — and
        # don't trace a full TX we didn't actually send.
        logger.debug("slot probe: short write %d/%d on %s", written, len(cmd), control_path)
        return None
    serial_trace.log_io(logger, "TX", cmd, trace)
    buf = b""
    while time.monotonic() < deadline:
        try:
            chunk = ser.read(4096)
        except (serial.SerialException, OSError):
            return None
        if not chunk:
            continue  # read timed out with nothing; loop re-checks the deadline
        serial_trace.log_io(logger, "RX", chunk, trace)
        buf += chunk
        if len(buf) > _MAX_RESPONSE_BYTES:
            logger.debug(
                "slot probe: response exceeded %d bytes on %s", _MAX_RESPONSE_BYTES, control_path
            )
            return None
        parsed = _extract_json(buf, prefix)
        if parsed is not None:
            return parsed
    return None


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


def discover_slots(
    base: str = "/dev/oe5xrx", timeout: float = 3.0, trace: bool = False
) -> list[dict]:
    """Scan `base` for slotN/control, enumerate + describe modules, return inventory entries.

    Each entry is ``{"slot": N, "control": path, "modules": [{"id", "identity",
    "capabilities"}, ...]}``. Slots that do not answer ``module list`` are omitted.
    Returns ``[]`` if `base` does not exist. Never raises.

    When `trace` is True, every TX/RX chunk on each slot's serial is hex-dumped at
    DEBUG level (enabled via the ``trace_serial`` config field) — this is the only way
    the production discovery path emits raw bytes for "module not found" debugging.
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
            modules = probe_slot(control, timeout=timeout, trace=trace)
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
