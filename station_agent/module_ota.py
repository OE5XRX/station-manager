"""Module firmware OTA (DFU / sim backends) for slot modules.

One reconcile cycle (``ModuleOta.run_once``) asks the server for at most one instruction,
classifies it against the live module, waits for the slot to be idle, pauses the slot,
downloads + flashes through a backend, verifies the version that comes back and reports
the result. A crash marker in ``state_dir`` lets a restarted agent finish the report.
Runs on the agent's main thread only — one flash per station at a time.
"""

from __future__ import annotations

import enum
import logging
import os
import re
import shutil
import time
from typing import Protocol

from . import devlock, ota, slot_discovery, slot_pause
from .module_ota_marker import Marker

logger = logging.getLogger(__name__)

__all__ = ["FlashOutcome", "Marker", "ModuleOta", "detect_backends", "read_slot_identity"]

DETACH_WAIT_S = 3.0
SETTLE_S = 45.0  # dfu only; the sim backend settles in 0 s
# dfu only (sim: 0 s): after the first post-settle target read, the target must still be
# reported CONFIRM_S later before committing — a trial image that fails the module's
# health gate reverts in that window and is reported rolled_back, not committed.
CONFIRM_S = 40.0
VERIFY_TIMEOUT_S = 180.0
VERIFY_POLL_S = 5.0
# After the confirm window ends, unreadable probes (None: CDC hiccup, probe timeout) are
# retried for this long before giving up — only a trusted non-target read, or no trusted
# read at all within the grace, counts as rolled_back.
CONFIRM_GRACE_S = 3 * VERIFY_POLL_S
DFU_DOWNLOAD_TIMEOUT_S = 120
DFU_MAX_RETRIES = 2
NUDGE_MIN_INTERVAL_S = 10.0

DFU_STUCK_MSG = "Modul hängt im DFU-Modus — Power-Cycle nötig"

API_PREFIX = "/api/v1/module-firmware/"
CHECK_PATH = API_PREFIX + "reconcile/check/"
COMMIT_PATH = API_PREFIX + "reconcile/commit/"
_ARTIFACT_SUFFIX = {"dfu": "bin", "sim": "native_sim"}
_SHA_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class FlashOutcome(enum.Enum):
    OK = "ok"
    REJECTED = "rejected"
    TRANSIENT = "transient"
    ROLLED_BACK = "rolled_back"
    DFU_STUCK = "dfu_stuck"


# Non-OK flash outcome -> (status/result, default error_message)
_OUTCOME_STATUS = {
    FlashOutcome.REJECTED: ("rejected", "device rejected the firmware image"),
    FlashOutcome.TRANSIENT: ("failed", "flash failed (transient)"),
    FlashOutcome.ROLLED_BACK: ("rolled_back", "module rolled back the firmware"),
    FlashOutcome.DFU_STUCK: ("rolled_back", DFU_STUCK_MSG),
}


class Backend(Protocol):
    name: str
    settle_s: float
    confirm_s: float

    def flash(self, slot: int, path: str, marker: Marker) -> FlashOutcome: ...

    def after_verify(self, slot: int, success: bool) -> None: ...

    def recover(self, marker: Marker) -> FlashOutcome | None: ...


class _NoBackend:
    """Stand-in when recovering a marker whose backend is no longer registered."""

    settle_s = SETTLE_S
    confirm_s = CONFIRM_S

    def __init__(self, name: str):
        self.name = name

    def flash(self, slot, path, marker):  # never called: recovery does not flash
        return FlashOutcome.TRANSIENT

    def after_verify(self, slot, success):
        pass

    def recover(self, marker):
        return None


def detect_backends(config) -> list[str]:
    """Return the available module-OTA backends (subset of ``dfu``, ``sim``)."""
    if not config.module_ota_enabled:
        return []
    backends = []
    if shutil.which(config.dfu_util) and os.path.isfile(config.slots_conf):
        backends.append("dfu")
    if os.path.isdir(config.sim_dir):
        backends.append("sim")
    return backends


BUILDABLE = frozenset({"dfu", "sim"})


def _build_backend(name: str, config, read_identity=None) -> Backend | None:
    """Construct the concrete backend *name* (DfuBackend: A4, SimBackend: A5)."""
    if name == "dfu":
        from .module_ota_dfu import DfuBackend  # lazy: module_ota_dfu imports this module

        return DfuBackend(config, read_identity=read_identity or read_slot_identity)
    if name == "sim":
        from .module_ota_sim import SimBackend  # lazy: module_ota_sim imports this module

        return SimBackend(config, read_identity=read_identity or read_slot_identity)
    return None


def _default_backends(config, read_identity=None) -> dict[str, Backend]:
    """Backends for every currently detected kind."""
    backends: dict[str, Backend] = {}
    for name in detect_backends(config):
        backend = _build_backend(name, config, read_identity)
        if backend is not None:
            backends[name] = backend
    return backends


def control_path(config, slot: int) -> str:
    return f"{config.slot_dev_base}/slot{slot}/control"


def read_slot_identity(config, slot: int, module_uid: str, timeout: float = 3.0) -> dict | None:
    """Identity of the module with ``uid == module_uid`` on *slot*, else None.

    ``slot_discovery.probe_slot`` takes the control-device lock itself.
    """
    modules = slot_discovery.probe_slot(control_path(config, slot), timeout)
    for module in modules or []:
        identity = module.get("identity") or {}
        if identity.get("uid") == module_uid:
            return identity
    return None


def _parse_instruction(data) -> dict | None:
    """Validated instruction dict, or None (logged) if malformed."""
    try:
        if not isinstance(data, dict):
            raise ValueError("instruction is not an object")
        conv = data["convergence_id"]
        if not isinstance(conv, int) or isinstance(conv, bool):
            raise ValueError("convergence_id must be int")
        slot = int(str(data["slot"]))
        size = data["size_bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ValueError("size_bytes must be a positive int")
        instr = {
            "convergence_id": conv,
            "slot": slot,
            "module_uid": str(data["module_uid"]),
            "variant": str(data.get("variant") or ""),
            "target_version": str(data["target_version"]),
            "artifact": str(data["artifact"]),
            "download_url": str(data["download_url"]),
            "checksum_sha256": str(data["checksum_sha256"]).lower(),
            "size_bytes": size,
        }
        if not instr["module_uid"] or not instr["target_version"]:
            raise ValueError("module_uid/target_version empty")
        if not _SHA_RE.match(instr["checksum_sha256"]):
            raise ValueError("checksum_sha256 is not a sha256 hex digest")
        url = instr["download_url"]
        if not url.startswith(API_PREFIX) or ".." in url or any(c.isspace() for c in url):
            raise ValueError(f"download_url {url!r} is not a module-firmware API path")
    except (KeyError, TypeError, ValueError) as exc:
        logger.error("module_ota: dropping malformed instruction (%s): %r", exc, data)
        return None
    return instr


def _module_id(snapshot: dict | None, module_uid: str) -> str:
    for module in (snapshot or {}).get("modules", []):
        if (module.get("identity") or {}).get("uid") == module_uid:
            return str(module.get("id", ""))
    return ""


class ModuleOta:
    def __init__(
        self,
        config,
        http_client,
        control=None,
        audio=None,
        backends: dict[str, Backend] | None = None,
        read_identity=read_slot_identity,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self._config = config
        self._http = http_client
        self._control = control
        self._audio = audio
        self._read_identity = read_identity
        # Constructed backends, cached across detection flaps. With explicitly injected
        # backends (tests) the pool is fixed; otherwise backends detected later (sim_dir
        # appeared, dfu-util installed) are built lazily on the next sync.
        self._auto_backends = backends is None
        self._pool: dict[str, Backend] = dict(
            _default_backends(config, read_identity) if backends is None else backends
        )
        # Active view: pool ∩ current detection. Re-synced at the start of every
        # run_once / recover_on_startup (main thread only).
        self.backends: dict[str, Backend] = {}
        self._sync_backends()
        self._clock = clock
        self._sleep = sleep

    # --- backends -------------------------------------------------------------

    def _executable(self, detected) -> list[str]:
        """Detected backend kinds this instance can execute (pool or lazily buildable)."""
        return [n for n in detected if n in self._pool or (self._auto_backends and n in BUILDABLE)]

    def advertised_backends(self) -> list[str]:
        """Backends for the heartbeat ``capabilities``: exactly what run_once can execute.

        Read-only (safe from the heartbeat keepalive thread): the same detection the next
        ``_sync_backends`` uses, so the server never hands out an instruction this agent
        cannot execute because of a capability/backend mismatch.
        """
        return self._executable(detect_backends(self._config))

    def _sync_backends(self) -> set[str]:
        """Re-detect: build newly available backends, drop vanished ones. Main thread only."""
        active: dict[str, Backend] = {}
        for name in self._executable(detect_backends(self._config)):
            backend = self._pool.get(name)
            if backend is None:
                backend = _build_backend(name, self._config, self._read_identity)
                if backend is None:
                    continue
                self._pool[name] = backend
            active[name] = backend
        self.backends = active
        return set(active)

    # --- public ---------------------------------------------------------------

    def recover_on_startup(self) -> str | None:
        """Finish a flash interrupted by a restart, then run the backends' startup hooks.

        Marker recovery first; afterwards every backend with a ``startup_recover()`` hook
        runs it (sim: roll back a ``pending`` override even without a marker, per the
        linux-image contract). A failing hook is logged and does not stop the others.
        """
        self._sync_backends()
        try:
            marker = Marker.load(self._config.state_dir, self._config.module_fw_dir)
            return None if marker is None else self._finish_from_marker(marker)
        finally:
            for name, backend in self.backends.items():
                hook = getattr(backend, "startup_recover", None)
                if hook is None:
                    continue
                try:
                    hook()
                except Exception:  # noqa: BLE001 — one backend must not block the others
                    logger.exception("module_ota: %s startup recovery failed", name)

    def run_once(self) -> str | None:
        detected = self._sync_backends()
        if not detected:
            return None
        marker = Marker.load(self._config.state_dir, self._config.module_fw_dir)
        if marker is not None:
            return self._finish_from_marker(marker)

        instr = self._check()
        if instr is None:
            return None
        conv, slot, uid = instr["convergence_id"], instr["slot"], instr["module_uid"]
        target = instr["target_version"]

        backend = self.backends.get(instr["artifact"])
        if backend is None:
            # Agent-local (capability raced the heartbeat, backend vanished): not an
            # artifact/module mismatch, so never ``rejected`` (= instant quarantine).
            self._status(conv, "failed", f"artifact backend {instr['artifact']!r} unavailable")
            return "failed"

        identity = self._read_identity(self._config, slot, uid)
        if identity is None or identity.get("uid") != uid:
            if self._dfu_stuck(backend, slot):
                # No CDC because the module still hangs in DFU mode from an earlier
                # cycle: keep reporting DFU-stuck so the dashboard notice survives
                # (a "not found" would overwrite it). Reported as transient ``failed``,
                # not ``rolled_back``: nothing was flashed, so the re-report must not
                # spend the attempt budget (the first detection already counted once)
                # — the server's transient cap still ends the drain.
                snapshot = self._control.last_inventory_entry(slot) if self._control else None
                module_id = _module_id(snapshot, uid)
                if module_id:
                    # The debouncer drops a CDC-less slot after a while; an event with
                    # module "" could not be matched to a card, so only the status goes.
                    self._event(slot, module_id, "firmware_dfu_stuck", {"msg": DFU_STUCK_MSG})
                self._forget_slot(slot)
                self._status(conv, "failed", DFU_STUCK_MSG)
                return "failed"
            self._status(conv, "failed", f"module {uid} not found in slot {slot}")
            return "failed"
        live_variant = str(identity.get("variant") or "")
        if live_variant and instr["variant"] and live_variant != instr["variant"]:
            self._status(
                conv,
                "rejected",
                f"variant mismatch: slot has {live_variant!r}, image is for {instr['variant']!r}",
            )
            return "rejected"
        old_version = str(identity.get("version", ""))
        if old_version == target:
            return self._commit(conv, target)

        if self._busy(slot):
            return "busy"
        snapshot = self._control.last_inventory_entry(slot) if self._control else None
        module_id = _module_id(snapshot, uid)
        if not module_id:
            # No last-known inventory entry carrying this module (yet): pausing with a
            # made-up snapshot would report a ghost module id "" to the server. Retry
            # once the control plane has an inventory entry for the slot.
            logger.info("module_ota: no inventory entry for %s in slot %s; retry later", uid, slot)
            return "busy"
        suffix = _ARTIFACT_SUFFIX.get(instr["artifact"], "bin")
        safe_target = _UNSAFE_FILENAME_RE.sub("_", target)
        marker = Marker(
            convergence_id=conv,
            slot=slot,
            module_uid=uid,
            target_version=target,
            old_version=old_version,
            artifact=instr["artifact"],
            path=os.path.join(self._config.module_fw_dir, f"conv{conv}-{safe_target}.{suffix}"),
            phase="downloaded",
            snapshot=snapshot,
            checksum_sha256=instr["checksum_sha256"],
            state_dir=self._config.state_dir,
        )
        if not self._pause_when_idle(slot, snapshot):
            return "busy"
        # Paused from here on: every path ends in _finish (which resumes the slot).
        result = "failed"
        try:
            started = {"target_version": target, "convergence_id": conv}
            self._event(slot, module_id, "firmware_update_started", started)
            self._refresh()
            result = self._download_and_flash(instr, marker, backend, module_id)
        except Exception as exc:  # noqa: BLE001 — never leave the slot paused
            logger.exception("module_ota: flash cycle for convergence %s crashed", conv)
            self._status(conv, "failed", f"agent error: {type(exc).__name__}: {exc}")
            result = "failed"
        finally:
            self._finish(marker, module_id, result)
        return result

    # --- cycle steps --------------------------------------------------------------

    def _download_and_flash(self, instr, marker: Marker, backend, module_id: str) -> str:
        conv = marker.convergence_id
        self._status(conv, "downloading")
        os.makedirs(self._config.module_fw_dir, exist_ok=True)
        ok = ota.download_firmware_resumable(
            self._http,
            instr["download_url"],
            instr["checksum_sha256"],
            marker.path,
            expected_size=instr["size_bytes"],
        )
        if not ok:
            self._status(conv, "failed", "download or checksum verification failed")
            return "failed"
        marker.save()
        self._status(conv, "flashing")
        marker.phase = "flashing"
        marker.save()
        outcome = backend.flash(marker.slot, marker.path, marker)
        flashed_at = self._clock()
        if outcome is FlashOutcome.OK:
            self._status(conv, "verifying")
            return self._verify(marker, backend, flashed_at)
        return self._report_outcome(marker, outcome, module_id)

    def _verify(self, marker: Marker, backend, started: float) -> str:
        """Poll until the module reports a trusted, confirmed version or VERIFY_TIMEOUT_S passes.

        Reads before ``settle_s`` are not trusted. The first trusted target read starts a
        full ``confirm_s`` window; only a target read at/after its end commits. The deadline
        only bounds reaching that first target read — a late first read (slow
        re-enumeration) extends the poll to the window's end plus ``CONFIRM_GRACE_S``, so
        nothing commits without a complete confirm window and a single unreadable probe at
        the window's end does not fail a good flash. Any trusted non-target read — also
        inside the window (trial image reverted) — is rolled_back.
        """
        settle_until = started + backend.settle_s
        deadline = started + VERIFY_TIMEOUT_S
        confirm_s = getattr(backend, "confirm_s", 0.0)
        confirm_until = None
        target = marker.target_version
        while True:
            identity = self._read_identity(self._config, marker.slot, marker.module_uid)
            now = self._clock()
            if identity is not None and now >= settle_until:
                version = str(identity.get("version", ""))
                if version != target:
                    backend.after_verify(marker.slot, False)
                    self._status(
                        marker.convergence_id,
                        "rolled_back",
                        f"module came back with version {version!r} instead of {target!r}",
                    )
                    return "rolled_back"
                if confirm_until is None:
                    confirm_until = now + confirm_s
                if now >= confirm_until:
                    backend.after_verify(marker.slot, True)
                    return self._commit(marker.convergence_id, version)
            end = (
                deadline
                if confirm_until is None
                else max(deadline, confirm_until + CONFIRM_GRACE_S)
            )
            if now >= end:
                break
            wake = confirm_until if confirm_until is not None and now < confirm_until else end
            remaining = wake - now
            self._sleep(min(VERIFY_POLL_S, remaining) if remaining > 0 else VERIFY_POLL_S)
        backend.after_verify(marker.slot, False)
        if confirm_until is not None:
            message = f"module stopped reporting {target!r} before it was confirmed"
        else:
            message = f"module did not come back within {VERIFY_TIMEOUT_S:.0f} s"
        self._status(marker.convergence_id, "rolled_back", message)
        return "rolled_back"

    def _report_outcome(self, marker: Marker, outcome: FlashOutcome, module_id: str) -> str:
        status, message = _OUTCOME_STATUS[outcome]
        if outcome is FlashOutcome.DFU_STUCK:
            self._event(marker.slot, module_id, "firmware_dfu_stuck", {"msg": DFU_STUCK_MSG})
            # The module hangs in DFU (no CDC): drop the control plane's stale last-known
            # identity, else the debouncer re-emits it after resume and the server reads
            # it as "module back online" — clearing the DFU-stuck notice it just set.
            self._forget_slot(marker.slot)
        self._status(marker.convergence_id, status, message)
        return status

    def _finish_from_marker(self, marker: Marker) -> str:
        """Classify and report a flash interrupted by an agent restart."""
        conv, slot = marker.convergence_id, marker.slot
        logger.warning(
            "module_ota: recovering convergence %s on slot %s (%s)", conv, slot, marker.phase
        )
        backend = self.backends.get(marker.artifact) or _NoBackend(marker.artifact)
        module_id = _module_id(marker.snapshot, marker.module_uid)
        result = "failed"
        keep_marker = False
        try:
            slot_pause.pause(slot, marker.snapshot)
            try:
                self._barrier(slot)
            except TimeoutError:
                # The device stays busy: keep the marker (a module stuck in DFU would
                # otherwise lose its recovery) and retry on the next cycle. Report nothing.
                logger.warning(
                    "module_ota: slot %s control device busy during recovery of convergence"
                    " %s; keeping the marker, retry next cycle",
                    slot,
                    conv,
                )
                keep_marker = True
                result = "busy"
                return result
            if marker.phase == "downloaded":
                # Never flashed: the live version is not a flash result.
                identity = self._read_identity(self._config, slot, marker.module_uid)
                version = None if identity is None else str(identity.get("version", ""))
                if version == marker.target_version:
                    result = self._commit(conv, version)
                else:
                    self._status(conv, "failed", "agent restarted before flashing")
            else:
                outcome = backend.recover(marker)
                if outcome is None or outcome is FlashOutcome.OK:
                    result = self._verify(marker, backend, self._clock())
                else:
                    result = self._report_outcome(marker, outcome, module_id)
        except Exception as exc:  # noqa: BLE001 — never leave the slot paused
            logger.exception("module_ota: recovery of convergence %s crashed", conv)
            self._status(conv, "failed", f"agent error: {type(exc).__name__}: {exc}")
            result = "failed"
        finally:
            if keep_marker:
                slot_pause.resume(slot)
                self._refresh()
            else:
                self._finish(marker, module_id, result)
        return result

    def _finish(self, marker: Marker, module_id: str, result: str) -> None:
        slot_pause.resume(marker.slot)
        finished = {"result": result, "target_version": marker.target_version}
        self._event(marker.slot, module_id, "firmware_update_finished", finished)
        # Forced: a fast cycle (e.g. download fails in < 1 s) never changed the debounced
        # inventory, so an emit-if-changed refresh would send nothing after resume.
        self._refresh(force=True)
        self._remove_image(marker.path)
        marker.clear()

    # --- idle gate / pause --------------------------------------------------------

    @staticmethod
    def _dfu_stuck(backend, slot: int) -> bool:
        """Whether *backend* sees *slot*'s module hanging in DFU mode (dfu only)."""
        is_stuck = getattr(backend, "is_stuck", None)
        if is_stuck is None:
            return False
        try:
            return bool(is_stuck(slot))
        except Exception:  # noqa: BLE001 — a failing probe must not break the cycle
            logger.exception("module_ota: DFU-stuck probe for slot %s failed", slot)
            return False

    def _busy(self, slot: int) -> bool:
        if self._control is not None and self._control.slot_busy(slot):
            logger.info("module_ota: slot %s busy (PTT); retry next cycle", slot)
            return True
        if self._audio is not None and self._audio.slot_active(slot):
            logger.info("module_ota: slot %s has live TX/diag audio; retry next cycle", slot)
            return True
        return False

    def _barrier(self, slot: int) -> None:
        """Wait until an in-flight probe/command holding the control device has finished."""
        with devlock.control_device_lock(control_path(self._config, slot)):
            pass

    def _pause_when_idle(self, slot: int, snapshot: dict) -> bool:
        """Pause *slot*, drain in-flight device users, re-check idle. False = busy (resumed)."""
        idle = False
        slot_pause.pause(slot, snapshot)
        try:
            self._barrier(slot)
            idle = not self._busy(slot)
        except TimeoutError:
            logger.warning("module_ota: slot %s control device stays busy; retry later", slot)
        finally:
            if not idle:  # also on an unexpected exception: never leave the slot paused
                slot_pause.resume(slot)
        return idle

    # --- server I/O (best effort) -------------------------------------------------

    def _check(self) -> dict | None:
        resp = self._http.request("POST", CHECK_PATH, json_data={})
        if resp is None or resp.status_code == 204:
            return None
        if resp.status_code != 200:
            logger.warning("module_ota: reconcile check returned HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            logger.error("module_ota: reconcile check returned invalid JSON: %s", exc)
            return None
        return _parse_instruction(data)

    def _status(self, conv: int, status: str, error_message: str = "") -> None:
        path = f"{API_PREFIX}reconcile/{conv}/status/"
        body = {"status": status, "error_message": error_message}
        try:
            resp = self._http.request("POST", path, json_data=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("module_ota: status %s for %s failed: %s", status, conv, exc)
            return
        if resp is None or not 200 <= resp.status_code < 300:
            logger.warning(
                "module_ota: status %s for convergence %s not accepted (%s)",
                status,
                conv,
                "no response" if resp is None else f"HTTP {resp.status_code}",
            )

    def _commit(self, conv: int, version: str) -> str:
        body = {"convergence_id": conv, "version": version}
        try:
            resp = self._http.request("POST", COMMIT_PATH, json_data=body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("module_ota: commit of %s failed: %s", conv, exc)
            return "success"
        if resp is not None and resp.status_code == 409:
            logger.warning("module_ota: server refused commit of %s (409)", conv)
            return "rolled_back"
        if resp is None or not 200 <= resp.status_code < 300:
            # The module runs the target; the server derives it from the reported version.
            logger.warning("module_ota: commit of %s not acknowledged", conv)
        return "success"

    def _event(self, slot: int, module_id: str, event: str, detail: dict) -> None:
        if self._control is None:
            return
        try:
            self._control.send_event(slot, module_id, event, detail)
        except Exception:  # noqa: BLE001
            logger.exception("module_ota: event %s failed", event)

    def _forget_slot(self, slot: int) -> None:
        forget = getattr(self._control, "forget_slot", None)
        if forget is None:
            return
        try:
            forget(slot)
        except Exception:  # noqa: BLE001
            logger.exception("module_ota: forgetting slot %s inventory failed", slot)

    def _refresh(self, force: bool = False) -> None:
        if self._control is None:
            return
        try:
            if force:
                self._control.request_inventory_refresh(force=True)
            else:
                self._control.request_inventory_refresh()
        except Exception:  # noqa: BLE001
            logger.exception("module_ota: inventory refresh failed")

    def _remove_image(self, path: str) -> None:
        fw_dir = os.path.realpath(self._config.module_fw_dir)
        if os.path.dirname(os.path.realpath(path)) != fw_dir:
            logger.warning("module_ota: not removing %s (outside %s)", path, fw_dir)
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("module_ota: cannot remove %s: %s", path, exc)
