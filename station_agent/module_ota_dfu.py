"""DFU backend for module OTA: ``dfu-util`` addressed by USB port path.

Every ``dfu-util`` call is addressed with ``-p <port>`` from ``slots.conf`` and NEVER with
``-d vid:pid``: two identical modules share ``2fe3:0012``, so a vid:pid filter could flash the
wrong slot. Flow (mirrors FW-HIL ``live_dfu.py``): detach (``-e``) under the slot's control
device lock -> ``DETACH_WAIT_S`` -> download (``-a 0 -D``) with a bounded retry budget for a
module that hangs in DFU mode. The budget (``marker.dfu_attempts``) is persisted before every
retry so an agent restart can never exceed ``1 + DFU_MAX_RETRIES`` downloads in total.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time

from . import devlock
from .module_ota import (
    CONFIRM_S,
    DETACH_WAIT_S,
    DFU_DOWNLOAD_TIMEOUT_S,
    DFU_MAX_RETRIES,
    SETTLE_S,
    FlashOutcome,
    Marker,
    control_path,
    read_slot_identity,
)
from .module_ota_marker import path_in_dir

logger = logging.getLogger(__name__)

__all__ = ["DfuBackend", "is_device_rejection", "parse_slots_conf"]

_PORT_RE = re.compile(r"^\d+(-\d+)(\.\d+)*$", re.ASCII)
_SLOT_LINE_RE = re.compile(r"^slot(\d+)\s*=\s*(\S+)$", re.ASCII)
_REJECTION_RE = re.compile(r"DFU state\(\d+\) = dfuERROR, status\((\d+)\)", re.ASCII)
_LOG_OUTPUT_MAX = 500
DETACH_TIMEOUT_S = 15
LIST_TIMEOUT_S = 15
DETACH_LOCK_TIMEOUT_S = 15.0


def is_device_rejection(output: str) -> bool:
    """True if dfu-util reports the device rejected the transfer.

    Targets dfu-util's get_status-after-block error line
    ``DFU state(10) = dfuERROR, status(10) = Device's firmware is corrupt...`` with a
    non-zero status. Deliberately ignores ``Determining device status: state = dfuERROR, ...``
    and ``dfuERROR, clearing status``: dfu-util prints those when the device is still in the
    error state left by a previous hung download, i.e. on exactly the retry/recover path,
    and they say nothing about the current image.
    """
    return any(int(m.group(1)) != 0 for m in _REJECTION_RE.finditer(output))


def _image_matches(path: str, expected_sha256: str) -> bool:
    """Whether *path*'s sha256 equals *expected_sha256*. Unknown checksum or read error
    -> False (fail closed); a non-str checksum (corrupt marker) never raises."""
    if not isinstance(expected_sha256, str) or not expected_sha256:
        return False
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("module_ota: cannot read %s for re-verification: %s", path, exc)
        return False
    return digest.hexdigest() == expected_sha256.lower()


def parse_slots_conf(path: str) -> dict[int, str]:
    """``slot<N>=<usb-port-path>`` lines -> ``{N: port}``. Malformed lines are logged + skipped."""
    ports: dict[int, str] = {}
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError as exc:
        logger.warning("module_ota: cannot read %s: %s", path, exc)
        return ports
    for lineno, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SLOT_LINE_RE.match(line)
        if match is None:
            logger.warning("module_ota: %s:%d: malformed line %r", path, lineno, line)
            continue
        slot, port = int(match.group(1)), match.group(2)
        if not _PORT_RE.match(port):
            logger.warning("module_ota: %s:%d: invalid USB port path %r", path, lineno, port)
            continue
        ports[slot] = port
    return ports


class DfuBackend:
    name = "dfu"
    settle_s = SETTLE_S
    confirm_s = CONFIRM_S

    def __init__(
        self, config, run=subprocess.run, sleep=time.sleep, read_identity=read_slot_identity
    ):
        self._config = config
        self._run = run
        self._sleep = sleep
        self._read_identity = read_identity

    # --- Backend protocol ---------------------------------------------------------

    def flash(self, slot: int, path: str, marker: Marker) -> FlashOutcome:
        port = self._port(slot)
        if port is None:
            return FlashOutcome.TRANSIENT
        # Wrong-module guard: the module must still be in this slot right before detach.
        # Read OUTSIDE the devlock — probe_slot takes that (non-reentrant) lock itself.
        identity = self._read_identity(self._config, slot, marker.module_uid)
        if identity is None or identity.get("uid") != marker.module_uid:
            logger.warning(
                "module_ota: module %s not in slot %s any more; not flashing",
                marker.module_uid,
                slot,
            )
            return FlashOutcome.TRANSIENT
        try:
            with devlock.control_device_lock(
                control_path(self._config, slot), timeout=DETACH_LOCK_TIMEOUT_S
            ):
                self._detach(port)
                self._sleep(DETACH_WAIT_S)
        except TimeoutError as exc:
            logger.warning("module_ota: slot %s control device busy, not detaching: %s", slot, exc)
            return FlashOutcome.TRANSIENT
        return self._download_loop(port, path, marker, first_is_retry=False)

    def after_verify(self, slot: int, success: bool) -> None:
        pass

    def recover(self, marker: Marker) -> FlashOutcome | None:
        port = self._port(marker.slot)
        if port is None or not self.in_dfu_mode(port):
            return None
        if not isinstance(marker.path, str) or not path_in_dir(
            marker.path, self._config.module_fw_dir
        ):
            # Defence in depth (Marker.load already discards such markers): never flash a
            # file outside module_fw_dir.
            logger.error(
                "module_ota: slot %s in DFU mode but image %r is outside %s; not flashing",
                marker.slot,
                marker.path,
                self._config.module_fw_dir,
            )
            return FlashOutcome.DFU_STUCK
        if not os.path.isfile(marker.path):
            # In DFU mode without an image to flash: only a power-cycle brings it back.
            logger.error(
                "module_ota: slot %s in DFU mode but image %s is gone", marker.slot, marker.path
            )
            return FlashOutcome.DFU_STUCK
        if not _image_matches(marker.path, marker.checksum_sha256):
            # The interrupted cycle's image is unverifiable (power loss can leave it
            # truncated or zero-filled): never flash it, treat it like a missing image.
            logger.error(
                "module_ota: slot %s in DFU mode but image %s fails sha256 re-verification",
                marker.slot,
                marker.path,
            )
            return FlashOutcome.DFU_STUCK
        outcome = self._download_loop(port, marker.path, marker, first_is_retry=True)
        return None if outcome is FlashOutcome.OK else outcome

    def is_stuck(self, slot: int) -> bool:
        """Whether *slot*'s module currently sits in DFU mode (needs a power-cycle when no
        flash is in progress)."""
        port = self._port(slot)
        return port is not None and self.in_dfu_mode(port)

    # --- dfu-util -----------------------------------------------------------------------

    def in_dfu_mode(self, port: str) -> bool:
        argv = [self._config.dfu_util, "-l", "-p", port]
        try:
            proc = self._run(
                argv, check=False, timeout=LIST_TIMEOUT_S, capture_output=True, text=True
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("module_ota: %s failed: %s", argv, exc)
            return False
        return "Found DFU" in (proc.stdout or "") + (proc.stderr or "")

    def _port(self, slot: int) -> str | None:
        port = parse_slots_conf(self._config.slots_conf).get(slot)
        if port is None:
            conf = self._config.slots_conf
            logger.warning("module_ota: slot %s has no USB port in %s", slot, conf)
        return port

    def _detach(self, port: str) -> None:
        argv = [self._config.dfu_util, "-e", "-p", port]
        try:
            self._run(argv, check=False, timeout=DETACH_TIMEOUT_S, capture_output=True, text=True)
        except (subprocess.TimeoutExpired, OSError) as exc:
            # Not fatal by itself: the download step decides (device not found -> TRANSIENT).
            logger.warning("module_ota: detach %s failed: %s", argv, exc)

    def _download(self, port: str, path: str) -> tuple[int, str]:
        argv = [self._config.dfu_util, "-a", "0", "-p", port, "-D", path]
        try:
            proc = self._run(
                argv, check=False, timeout=DFU_DOWNLOAD_TIMEOUT_S, capture_output=True, text=True
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("module_ota: download %s failed: %s", argv, exc)
            return -1, str(exc)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

    def _download_loop(
        self, port: str, path: str, marker: Marker, *, first_is_retry: bool
    ) -> FlashOutcome:
        retry = first_is_retry
        while True:
            if retry:
                if marker.dfu_attempts >= DFU_MAX_RETRIES:
                    logger.error("module_ota: slot %s stuck in DFU mode", marker.slot)
                    return FlashOutcome.DFU_STUCK
                marker.dfu_attempts += 1
                marker.save()  # persist the budget BEFORE the retry runs
                logger.warning(
                    "module_ota: DFU retry %d/%d on slot %s",
                    marker.dfu_attempts,
                    DFU_MAX_RETRIES,
                    marker.slot,
                )
            rc, output = self._download(port, path)
            if rc == 0:
                return FlashOutcome.OK
            if is_device_rejection(output):
                logger.error(
                    "module_ota: slot %s device rejected image: %s",
                    marker.slot,
                    output[-_LOG_OUTPUT_MAX:],
                )
                return FlashOutcome.REJECTED
            if not self.in_dfu_mode(port):
                # Never entered DFU: the module still runs its old firmware.
                logger.warning("module_ota: no DFU device on port %s (rc %s)", port, rc)
                return FlashOutcome.TRANSIENT
            retry = True
