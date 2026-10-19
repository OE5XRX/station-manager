"""native_sim backend for module OTA: swap the harness binary + restart (linux-image contract).

``D = config.sim_dir`` (``/var/lib/oe5xrx/sim``). ``sim-harness.sh`` runs ``D/native-sim-fm``
if it is a root-owned, non-group/world-writable regular executable, else the image-baked one.
Flash (contract steps 1-5; 6 = ModuleOta's verify, 7 = ``after_verify(True)``):

1. copy the image to ``D/native-sim-fm.new`` (fsync, root:root, 0755, sha256 == the
   server-pinned ``marker.checksum_sha256``)
2. ``.prev`` := hardlink of the live override, or ``rm -f .prev`` if there is none
3. ``touch D/pending`` + fsync the dir
4. ``rename(.new, native-sim-fm)``
5. ``systemctl restart <sim_service>`` (synchronous: the old harness is gone before verify)

Order matters: ``.prev`` is refreshed before ``pending`` is set, so whenever ``pending`` exists
``.prev`` matches the pre-flash state. Any failure before step 3 is TRANSIENT with the live
override untouched; any failure after it runs :meth:`SimBackend.rollback` (R1-R3, idempotent,
``pending`` removed last) and returns ROLLED_BACK.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess

from .module_ota import FlashOutcome, Marker, read_slot_identity

logger = logging.getLogger(__name__)

__all__ = ["SimBackend"]

RESTART_TIMEOUT_S = 60
_SHA_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
LIVE = "native-sim-fm"
NEW = "native-sim-fm.new"
PREV = "native-sim-fm.prev"
RB = "native-sim-fm.rb"
PENDING = "pending"


def _chown_root(fd: int) -> None:
    """``chown root:root`` on an open fd — wrapped so tests (not root) can monkeypatch it."""
    os.fchown(fd, 0, 0)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rm_f(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class SimBackend:
    name = "sim"
    settle_s = 0.0
    confirm_s = 0.0

    def __init__(self, config, run=subprocess.run, read_identity=read_slot_identity):
        self._config = config
        self._run = run
        self._read_identity = read_identity

    def _p(self, name: str) -> str:
        return os.path.join(self._config.sim_dir, name)

    # --- Backend protocol ---------------------------------------------------------

    def flash(self, slot: int, path: str, marker: Marker) -> FlashOutcome:
        # Wrong-module guard: the module must still be in this slot right before the swap.
        identity = self._read_identity(self._config, slot, marker.module_uid)
        if identity is None or identity.get("uid") != marker.module_uid:
            logger.warning(
                "module_ota: module %s not in slot %s any more; not flashing",
                marker.module_uid,
                slot,
            )
            return FlashOutcome.TRANSIENT
        if os.path.lexists(self._p(PENDING)) and not self.rollback():
            # A previous rollback did not finish: step 2 would make the unverified live
            # binary the new rollback target. Finish that rollback first.
            logger.warning("module_ota: stale sim flash still pending; not flashing")
            return FlashOutcome.TRANSIENT

        try:
            if not self._stage(path, marker.checksum_sha256):  # step 1
                return FlashOutcome.TRANSIENT
            self._refresh_prev()  # step 2
            self._touch_pending()  # step 3
        except OSError as exc:
            logger.warning("module_ota: sim flash failed before install: %s", exc)
            self._cleanup_before_pending()
            return FlashOutcome.TRANSIENT

        try:
            os.replace(self._p(NEW), self._p(LIVE))  # step 4
            self._fsync_dir()
        except OSError as exc:
            logger.error("module_ota: installing sim binary failed: %s; rolling back", exc)
            self.rollback()
            return FlashOutcome.ROLLED_BACK
        if not self._restart():  # step 5
            logger.error("module_ota: sim harness restart failed; rolling back")
            self.rollback()
            return FlashOutcome.ROLLED_BACK
        return FlashOutcome.OK

    def after_verify(self, slot: int, success: bool) -> None:
        if not success:
            if not os.path.lexists(self._p(PENDING)):
                # No pending = this flash never reached step 3 (e.g. crash before it, then
                # marker recovery): the live override is the untouched, verified one and
                # .prev may be stale — rolling back would install the wrong binary.
                logger.warning("module_ota: sim verify failed without pending; not rolling back")
                return
            self.rollback()
            return
        try:  # step 7
            _rm_f(self._p(PENDING))
            self._fsync_dir()
        except OSError as exc:
            # pending stays, so per the contract the next startup/flash rolls back to .prev
            # — the PRE-flash binary — although this flash verified and reported success.
            logger.error(
                "module_ota: cannot clear sim pending marker: %s; the verified flash will be "
                "rolled back on the next agent start",
                exc,
            )

    def recover(self, marker: Marker) -> FlashOutcome | None:
        if not os.path.lexists(self._p(PENDING)):
            return None
        logger.warning("module_ota: unverified sim flash pending on startup; rolling back")
        self.rollback()
        return FlashOutcome.ROLLED_BACK

    def startup_recover(self) -> None:
        """Contract: ``pending`` on agent startup → roll back, even without a crash marker."""
        if os.path.lexists(self._p(PENDING)):
            logger.warning("module_ota: sim pending on startup without verify; rolling back")
            self.rollback()

    # --- rollback (R1-R3) ---------------------------------------------------------

    def rollback(self) -> bool:
        """Restore the pre-flash binary. Idempotent; True when it ran to completion.

        On False ``pending`` is kept, so the rollback is simply re-run from the top.
        """
        try:  # R1
            if os.path.lexists(self._p(PREV)):
                _rm_f(self._p(RB))
                os.link(self._p(PREV), self._p(RB), follow_symlinks=False)
                os.replace(self._p(RB), self._p(LIVE))
            else:
                _rm_f(self._p(LIVE))
            self._fsync_dir()
        except OSError as exc:
            logger.error("module_ota: sim rollback could not restore the binary: %s", exc)
            return False
        if not self._restart():  # R2 (always: the unverified binary may be running)
            logger.error("module_ota: sim rollback could not restart the harness")
            return False
        try:  # R3, pending last
            _rm_f(self._p(NEW))
            _rm_f(self._p(RB))
            _rm_f(self._p(PENDING))
            self._fsync_dir()
        except OSError as exc:
            logger.error("module_ota: sim rollback cleanup failed: %s", exc)
            return False
        logger.warning("module_ota: sim firmware rolled back")
        return True

    # --- steps ----------------------------------------------------------------------

    def _stage(self, path: str, expected_sha256: str) -> bool:
        """Step 1. False (``.new`` removed) unless the staged copy matches the
        server-pinned ``expected_sha256`` (the marker's checksum).

        The copy itself is hashed — not the source — so an image that changed after the
        download check (bit rot, stale partial, misconfigured ``module_fw_dir``) never gets
        installed and executed as root. An empty/malformed pin fails closed."""
        new = self._p(NEW)
        _rm_f(new)
        expected = expected_sha256.lower() if isinstance(expected_sha256, str) else ""
        if not _SHA_RE.match(expected):
            logger.error("module_ota: no valid pinned sha256 for %s; not staging", path)
            return False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(new, flags, 0o700)
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
            _chown_root(out.fileno())
            os.fchmod(out.fileno(), 0o755)
        if _sha256_file(new) != expected:
            logger.error(
                "module_ota: sim binary copy of %s does not match the pinned sha256", path
            )
            _rm_f(new)
            return False
        return True

    def _refresh_prev(self) -> None:
        """Step 2: ``ln -f native-sim-fm .prev`` or ``rm -f .prev``."""
        prev = self._p(PREV)
        _rm_f(prev)
        if os.path.lexists(self._p(LIVE)):
            os.link(self._p(LIVE), prev, follow_symlinks=False)

    def _touch_pending(self) -> None:
        """Step 3: ``touch pending`` + fsync the dir (also persists step 2)."""
        fd = os.open(self._p(PENDING), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_dir()

    def _cleanup_before_pending(self) -> None:
        for name in (NEW, PENDING):
            try:
                _rm_f(self._p(name))
            except OSError as exc:
                logger.warning("module_ota: cannot remove %s: %s", self._p(name), exc)

    def _fsync_dir(self) -> None:
        fd = os.open(self._config.sim_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _restart(self) -> bool:
        argv = ["systemctl", "restart", self._config.sim_service]
        try:
            proc = self._run(
                argv, check=False, timeout=RESTART_TIMEOUT_S, capture_output=True, text=True
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("module_ota: %s failed: %s", argv, exc)
            return False
        if proc.returncode != 0:
            logger.error(
                "module_ota: %s exited %s: %s",
                argv,
                proc.returncode,
                ((proc.stdout or "") + (proc.stderr or ""))[-500:],
            )
            return False
        return True
