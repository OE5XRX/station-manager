"""Crash marker for an in-flight module firmware flash.

Persists "flash of convergence X on slot N runs since T" in ``state_dir`` so a restarted agent
can classify and report the outcome instead of leaving the server-side convergence hanging.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MARKER_FILE = "module_ota.json"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$", re.ASCII)
_STR_FIELDS = ("module_uid", "target_version", "old_version", "artifact", "path")


@dataclass
class Marker:
    convergence_id: int
    slot: int
    module_uid: str
    target_version: str
    old_version: str
    artifact: str
    path: str
    phase: str  # "downloaded" | "flashing"
    dfu_attempts: int = 0
    started_at: float = field(default_factory=time.time)
    # Last-known inventory entry of the slot, re-used to pause the slot during recovery
    # (the module may be in DFU mode / re-enumerating and not answer a probe).
    snapshot: dict | None = None
    # Expected sha256 of ``path``: crash recovery re-verifies the on-disk image before
    # re-flashing it (power loss can leave it truncated/zero-filled). "" = unknown.
    checksum_sha256: str = ""
    state_dir: str = field(default="", repr=False, compare=False)

    def _file(self) -> str:
        return os.path.join(self.state_dir, MARKER_FILE)

    def save(self) -> None:
        """Write atomically: temp file + fsync + rename (+ dir fsync)."""
        os.makedirs(self.state_dir, exist_ok=True)
        data = dataclasses.asdict(self)
        data.pop("state_dir")
        fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".module_ota.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._file())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def clear(self) -> None:
        clear(self.state_dir)

    @classmethod
    def load(cls, state_dir: str, module_fw_dir: str | None = None) -> Marker | None:
        """The persisted marker, or None. A corrupt marker is logged and deleted.

        Beyond JSON shape, every field is type-checked; ``checksum_sha256`` must be "" or
        a sha256 hex digest (normalised to lowercase), and with ``module_fw_dir`` given the
        image ``path`` must live directly in that directory (realpath) — recovery flashes
        it, so a modified marker must not point the flasher at an arbitrary file."""
        path = os.path.join(state_dir, MARKER_FILE)
        try:
            with open(path) as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.error("module_ota: corrupt marker %s (%s); discarding", path, exc)
            clear(state_dir)
            return None
        try:
            if not isinstance(data, dict):
                raise TypeError("marker is not an object")
            data.pop("state_dir", None)
            marker = cls(**data, state_dir=state_dir)
            if not isinstance(marker.convergence_id, int) or not isinstance(marker.slot, int):
                raise TypeError("convergence_id/slot must be int")
            if marker.phase not in ("downloaded", "flashing"):
                raise ValueError(f"unknown phase {marker.phase!r}")
            for name in _STR_FIELDS:
                if not isinstance(getattr(marker, name), str):
                    raise TypeError(f"{name} must be str")
            if not isinstance(marker.dfu_attempts, int) or isinstance(marker.dfu_attempts, bool):
                raise TypeError("dfu_attempts must be int")
            if marker.snapshot is not None and not isinstance(marker.snapshot, dict):
                raise TypeError("snapshot must be an object or null")
            if not isinstance(marker.checksum_sha256, str):
                raise TypeError("checksum_sha256 must be str")
            if marker.checksum_sha256 and not _SHA_RE.match(marker.checksum_sha256):
                raise ValueError("checksum_sha256 is not a sha256 hex digest")
            marker.checksum_sha256 = marker.checksum_sha256.lower()
            if module_fw_dir is not None and not path_in_dir(marker.path, module_fw_dir):
                raise ValueError(f"image path {marker.path!r} is outside {module_fw_dir!r}")
        except (TypeError, ValueError) as exc:
            logger.error("module_ota: invalid marker %s (%s); discarding", path, exc)
            clear(state_dir)
            return None
        return marker


def path_in_dir(path: str, directory: str) -> bool:
    """Whether *path* resolves (realpath) to a file directly inside *directory*."""
    if not path:
        return False
    return os.path.dirname(os.path.realpath(path)) == os.path.realpath(directory)


def clear(state_dir: str) -> None:
    try:
        os.remove(os.path.join(state_dir, MARKER_FILE))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("module_ota: cannot remove marker in %s: %s", state_dir, exc)
