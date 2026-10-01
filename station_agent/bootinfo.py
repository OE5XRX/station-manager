# station_agent/bootinfo.py
"""Boot / reboot-reason detection — agent-owned per telemetry contract."""

import json
import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)


def compute_reboot_reason(evidence: dict) -> str:
    """Classify why the previous boot ended, from collected evidence.

    Priority: crash > ota_rollback > watchdog > undervoltage > clean > unknown.
    A clean systemd shutdown overrides a lingering 'undervoltage occurred' bit.
    """
    if evidence.get("pstore_crash"):
        return "crash"
    if evidence.get("ota_rollback"):
        return "ota_rollback"
    if evidence.get("watchdog"):
        return "watchdog"
    if evidence.get("clean_marker"):
        return "clean"
    if evidence.get("undervoltage_occurred"):
        return "undervoltage"
    return "unknown"


BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
_STATE_FILE = "boot_state.json"
_CLEAN_MARKER = "clean_shutdown.marker"
_PENDING_UPGRADE_MARKER = "pending_upgrade.marker"
_LAST_OTA_RESULT_FILE = "last_ota_result.json"
_PSTORE_DIR = "/sys/fs/pstore"


def read_boot_id() -> str | None:
    try:
        with open(BOOT_ID_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _state_path(state_dir: str) -> str:
    return os.path.join(state_dir, _STATE_FILE)


def _load_state(state_dir: str) -> dict:
    with open(_state_path(state_dir)) as f:
        return json.load(f)


def _save_state(state_dir: str, state: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    tmp = _state_path(state_dir) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, _state_path(state_dir))


def mark_clean_shutdown(state_dir: str) -> None:
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, _CLEAN_MARKER), "w") as f:
            f.write("1")
    except OSError as exc:
        logger.debug("could not write clean-shutdown marker: %s", exc)


def clean_marker_present(state_dir: str) -> bool:
    return os.path.exists(os.path.join(state_dir, _CLEAN_MARKER))


def _clear_clean_marker(state_dir: str) -> None:
    try:
        os.remove(os.path.join(state_dir, _CLEAN_MARKER))
    except OSError:
        pass


# --- Pending-upgrade marker (H2) -----------------------------------------


def mark_pending_upgrade(state_dir: str, target_slot: str) -> None:
    """Write a marker so detect_boot knows a trial boot was deliberately armed.

    Called by ota.apply_update immediately after set_upgrade_pending succeeds.
    If state_dir is unwritable the marker is never written and _bootloader_rollback
    degrades to never firing (acceptable — DeploymentResult alerting still covers it).
    """
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, _PENDING_UPGRADE_MARKER)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"target_slot": target_slot}, f)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("could not write pending-upgrade marker: %s", exc)


def _pending_upgrade_marker_present(state_dir: str) -> bool:
    return os.path.exists(os.path.join(state_dir, _PENDING_UPGRADE_MARKER))


def _consume_pending_upgrade_marker(state_dir: str) -> None:
    try:
        os.remove(os.path.join(state_dir, _PENDING_UPGRADE_MARKER))
    except OSError:
        pass


# --- Last OTA result (M13) ------------------------------------------------


def write_last_ota_result(
    state_dir: str, result: str, detail: str = "", version: str = ""
) -> None:
    """Persist the final OTA outcome so _collect_slot can surface it in telemetry.

    Wrapped in try/except — must never break the OTA flow.
    """
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, _LAST_OTA_RESULT_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"result": result, "detail": detail, "version": version}, f)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("could not write last_ota_result: %s", exc)


def read_last_ota_result(state_dir: str) -> dict | None:
    """Return the persisted OTA result dict, or None if absent/unreadable."""
    try:
        with open(os.path.join(state_dir, _LAST_OTA_RESULT_FILE)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pstore_has_crash() -> bool:
    try:
        return any(name.startswith("dmesg-") for name in os.listdir(_PSTORE_DIR))
    except OSError:
        return False


def _consume_pstore() -> None:
    try:
        for name in os.listdir(_PSTORE_DIR):
            if name.startswith("dmesg-"):
                try:
                    os.remove(os.path.join(_PSTORE_DIR, name))
                except OSError:
                    pass
    except OSError:
        pass


def _bootloader_rollback(bootloader, state_dir: str) -> bool:
    """Detect a bootloader rollback — requires a persisted pending-upgrade marker.

    Only returns True when BOTH conditions hold:
      1. The agent previously wrote a pending_upgrade.marker (i.e. it deliberately
         armed a trial boot via apply_update / set_upgrade_pending).
      2. The bootloader tuple (upgrade_available, bootcount) indicates the trial
         was abandoned (upgrade_available=="0" and bootcount not "0").

    Without the marker an ordinary reboot after a committed upgrade has the same
    bootloader tuple (upgrade_available="0", bootcount increments each boot), so
    we would falsely label it ota_rollback.  With this guard the marker is only
    present if the agent itself armed the trial, and it is consumed here so the
    *next* reboot is not mis-labelled.
    """
    if not _pending_upgrade_marker_present(state_dir):
        return False
    try:
        from station_agent import bootloader as bl_mod

        ua = bl_mod.get_env(bootloader, "upgrade_available")
        bc = bl_mod.get_env(bootloader, "bootcount")
        return ua == "0" and bc not in (None, "0")
    except Exception as exc:  # noqa: BLE001 - never fail boot detection
        logger.debug("bootloader rollback probe failed: %s", exc)
        return False


def _dmesg_watchdog() -> bool:
    """Detect a watchdog reset in the PRIOR boot's kernel log.

    Uses ``journalctl -b -1 -k`` (boot=-1 gives the previous boot's kernel
    messages) so we inspect the boot that ended, not the current one.  A
    ``bcm2835-wdt`` line in the current dmesg only means the driver registered
    — it does not indicate that the watchdog fired last time.

    Feature-detects journalctl via shutil.which; degrades to False when absent
    (QEMU / no systemd-journal).  Requires an actual watchdog-reset message
    (bcm2835-wdt + timeout/reset context) to avoid false positives from driver
    registration lines.
    """
    if shutil.which("journalctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["journalctl", "-b", "-1", "-k", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return False
        text = proc.stdout.lower()
        # Require both the watchdog driver and a reset/timeout indicator in
        # the *prior* boot log — not just driver registration.
        watchdog_reset = "watchdog" in text and ("reset" in text or "timeout" in text)
        if "bcm2835-wdt" in text and watchdog_reset:
            return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _gather_evidence(bootloader, state_dir: str) -> dict:
    from station_agent import power

    throttle = power.read_throttle() or {}
    return {
        "pstore_crash": _pstore_has_crash(),
        "ota_rollback": _bootloader_rollback(bootloader, state_dir),
        "watchdog": _dmesg_watchdog(),
        "undervoltage_occurred": bool(throttle.get("undervoltage_occurred")),
        "clean_marker": clean_marker_present(state_dir),
    }


def detect_boot(state_dir: str, bootloader=None) -> dict:
    """Detect current boot, classify a reboot once per new boot_id."""
    boot_id = read_boot_id()
    try:
        state = _load_state(state_dir)
    except (OSError, ValueError):
        state = {}

    # M10: if boot_id is unreadable, return existing state unchanged — do not
    # manufacture a reboot on every heartbeat call.
    if boot_id is None:
        return {
            "boot_id": None,
            "boot_count": state.get("boot_count", 0),
            "reboot_reason": "unknown",
        }

    if state.get("boot_id") == boot_id:
        # M11: same boot, but a previous agent process may have written a
        # clean-shutdown marker that hasn't been consumed by a real reboot yet.
        # Consuming it here ensures that only a marker that survives an actual
        # power cycle (new boot_id) is honoured as evidence of a clean shutdown.
        _clear_clean_marker(state_dir)
        return {
            "boot_id": boot_id,
            "boot_count": state.get("boot_count", 0),
            "reboot_reason": state.get("reboot_reason", "unknown"),
        }

    # New boot (or first run / unreadable state). Evidence gathering must
    # never raise out of detect_boot — a collector (e.g. power.read_throttle)
    # that raises degrades to empty evidence (reason -> "unknown") while we
    # still persist and increment the boot count for the new boot.
    try:
        evidence = _gather_evidence(bootloader, state_dir)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break boot detection
        logger.debug("evidence gathering failed, treating as empty: %s", exc)
        evidence = {}
    reason = compute_reboot_reason(evidence)
    count = state.get("boot_count", 0) + 1
    new_state = {"boot_id": boot_id, "boot_count": count, "reboot_reason": reason}
    try:
        _save_state(state_dir, new_state)
        _consume_pstore()
        _clear_clean_marker(state_dir)
        # H2: consume the pending-upgrade marker now that we've classified the
        # reboot (whether it was an ota_rollback or a successful trial boot).
        _consume_pending_upgrade_marker(state_dir)
    except OSError as exc:
        logger.debug("boot state persistence failed, degrading: %s", exc)
        return {"boot_id": boot_id, "boot_count": 0, "reboot_reason": "unknown"}
    return new_state
